import jwt
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


PUBLIC_PREFIXES = ("/auth/",)
PUBLIC_EXACT = {"/multiclaw.png"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        store = request.app.state.auth_store
        token = request.cookies.get("token")
        user = None
        if token:
            try:
                payload = jwt.decode(token, store.jwt_secret, algorithms=["HS256"])
                user = {"id": payload["sub"], "email": payload["email"]}
            except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
                pass

        request.state.user = user

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


def require_auth(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user
