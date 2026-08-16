import jwt
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from multiclaw.auth.models import RECENT_AUTH_MAX_AGE_SECONDS, SAFE_METHODS, SESSION_TOKEN_AUDIENCE, UserRecord
from multiclaw.security.csrf import csrf_failure_reason, extract_request_origin
from multiclaw.storage.uow import AuthUnitOfWork
from sqlalchemy import select


PUBLIC_PREFIXES = ("/auth/", "/api/auth/", "/assets/")
PUBLIC_EXACT = {"/multiclaw.png", "/health/ready"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth = request.app.state.auth
        token = request.cookies.get("token")
        user = None
        token_iat = None
        request.state.request_started_at_ms = getattr(request.state, "request_started_at_ms", 0)
        if token and hasattr(request.app.state, "database"):
            try:
                payload = jwt.decode(
                    token,
                    auth.signing_key,
                    algorithms=["HS256"],
                    audience=SESSION_TOKEN_AUDIENCE,
                )
                subject = _validated_subject(payload["sub"])
                _validated_email(payload["email"])
                token_auth_epoch = _validated_auth_epoch(payload["auth_epoch"])
                token_iat = _validated_iat(payload["iat"])
            except (
                jwt.ExpiredSignatureError,
                jwt.InvalidTokenError,
                KeyError,
                TypeError,
                ValueError,
            ):
                payload = None
            else:
                async with AuthUnitOfWork(request.app.state.database, read_only=True) as uow:
                    current_user = await uow.users.get_by_id(subject)
                if (
                    current_user is not None
                    and current_user.status == "active"
                    and current_user.auth_epoch == token_auth_epoch
                ):
                    user = current_user

        request.state.authenticated_user = user
        request.state.authenticated_iat = token_iat
        request.state.user = _user_payload(user)

        path = request.url.path
        if request.method not in SAFE_METHODS:
            reason = csrf_failure_reason(request, auth.allowed_origins)
            if reason is not None:
                return _with_cors_headers(
                    request,
                    JSONResponse({"detail": "CSRF validation failed"}, status_code=403),
                )

        # Public paths
        if path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_EXACT:
            response = await _maybe_handle_preflight(request, call_next)
            return _with_cors_headers(request, response)

        # HTML index page is always served (frontend handles auth state)
        if path == "/":
            response = await _maybe_handle_preflight(request, call_next)
            return _with_cors_headers(request, response)

        # All other routes require auth
        if not user:
            return _with_cors_headers(
                request,
                JSONResponse({"detail": "Unauthorized"}, status_code=401),
            )

        response = await _maybe_handle_preflight(request, call_next)
        return _with_cors_headers(request, response)


def require_auth(request: Request) -> UserRecord:
    user = getattr(request.state, "authenticated_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


def _user_payload(user: UserRecord | None) -> dict[str, str] | None:
    if user is None:
        return None
    return {"id": user.id, "email": user.email}


def _validated_subject(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("sub must be a non-empty string")
    return value


def _validated_email(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("email must be a non-empty string")
    return value


def _validated_auth_epoch(value: object) -> int:
    if type(value) is not int:
        raise ValueError("auth_epoch must be an exact int")
    return value


def _validated_iat(value: object) -> int:
    if type(value) is not int:
        raise ValueError("iat must be an exact int")
    return value


async def require_recent_auth(request: Request) -> UserRecord:
    user = require_auth(request)
    token_iat = getattr(request.state, "authenticated_iat", None)
    if type(token_iat) is not int:
        raise HTTPException(status_code=401, detail="Unauthorized")
    async with request.app.state.database.connect() as conn:
        result = await conn.execute(select(request.app.state.database.dialect.db_now_ms()))
        db_now_seconds = int(result.scalar_one()) // 1000
    if db_now_seconds - token_iat > RECENT_AUTH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Recent authentication required")
    return user


async def _maybe_handle_preflight(request: Request, call_next) -> Response:
    if request.method == "OPTIONS" and request.headers.get("origin"):
        return Response(status_code=204)
    return await call_next(request)


def _with_cors_headers(request: Request, response: Response) -> Response:
    origin = extract_request_origin(request)
    allowed_origins = getattr(getattr(request.app.state, "auth", None), "allowed_origins", frozenset())
    if origin is None or origin not in allowed_origins:
        return response
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRF-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET,HEAD,OPTIONS,POST,PUT,PATCH,DELETE"
    return response
