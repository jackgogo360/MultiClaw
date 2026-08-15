import jwt
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from multiclaw.auth.models import UserRecord
from multiclaw.storage.uow import AuthUnitOfWork


PUBLIC_PREFIXES = ("/auth/", "/api/auth/")
PUBLIC_EXACT = {"/multiclaw.png", "/health/ready"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        store = request.app.state.auth_store
        token = request.cookies.get("token")
        user = None
        request.state.request_started_at_ms = getattr(request.state, "request_started_at_ms", 0)
        if token and hasattr(request.app.state, "database"):
            try:
                payload = jwt.decode(token, store.jwt_secret, algorithms=["HS256"])
                subject = str(payload["sub"])
                token_auth_epoch = int(payload["auth_epoch"])
            except (
                jwt.ExpiredSignatureError,
                jwt.InvalidTokenError,
                KeyError,
                TypeError,
                ValueError,
            ):
                payload = None
            else:
                async with AuthUnitOfWork(request.app.state.database) as uow:
                    current_user = await uow.users.get_by_id(subject)
                if current_user is not None and current_user.auth_epoch == token_auth_epoch:
                    user = current_user

        request.state.authenticated_user = user
        request.state.user = _user_payload(user)

        path = request.url.path
        # Public paths
        if path.startswith(PUBLIC_PREFIXES) or path in PUBLIC_EXACT:
            return await call_next(request)

        # HTML index page is always served (frontend handles auth state)
        if path == "/":
            return await call_next(request)

        # All other routes require auth
        if not user:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)


def require_auth(request: Request) -> UserRecord:
    user = getattr(request.state, "authenticated_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


def _user_payload(user: UserRecord | None) -> dict[str, str] | None:
    if user is None:
        return None
    return {"id": user.id, "email": user.email}
