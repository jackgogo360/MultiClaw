from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, HTTPException, Request, Response

from multiclaw.auth.brevo import send_verification_code
from multiclaw.auth.models import (
    AuthResponse,
    MeResponse,
    SendCodeRequest,
    VerifyRequest,
)
from multiclaw.auth.store import MAX_SENDS_PER_DAY, AuthStore

router = APIRouter(prefix="/auth")


def _get_store(request: Request) -> AuthStore:
    return request.app.state.auth_store


def _get_settings(request: Request):
    return request.app.state.settings


def _make_jwt(user_id: str, email: str, secret: str) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "exp": datetime.now(timezone.utc) + timedelta(days=10),
        },
        secret,
        algorithm="HS256",
    )


@router.post("/send-code", response_model=AuthResponse)
async def send_code(body: SendCodeRequest, request: Request):
    email = body.email.strip().lower()
    if "@" not in email or len(email) < 5:
        raise HTTPException(status_code=422, detail="Please enter a valid email")

    store = _get_store(request)
    recent = await store.count_recent_sends(email)
    if recent >= MAX_SENDS_PER_DAY:
        raise HTTPException(
            status_code=429, detail="Too many attempts, please try again tomorrow"
        )

    code = await store.create_code(email)
    settings = _get_settings(request)
    try:
        await send_verification_code(settings, email, code.code)
    except Exception:
        raise HTTPException(
            status_code=502, detail="Failed to send email, please try again later"
        )

    return AuthResponse()


@router.post("/verify", response_model=AuthResponse)
async def verify(body: VerifyRequest, request: Request, response: Response):
    email = body.email.strip().lower()
    code = body.code.strip()

    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=422, detail="Invalid code format")

    store = _get_store(request)
    vc = await store.find_latest_unused_code(email)
    if vc is None or vc.code != code:
        raise HTTPException(
            status_code=401, detail="Invalid or expired verification code"
        )

    await store.mark_code_used(vc.id)
    user = await store.get_or_create_user(email)

    token = _make_jwt(user.id, user.email, store.jwt_secret)
    response.set_cookie(
        key="token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=864000,
        path="/",
    )

    return AuthResponse()


@router.post("/logout", response_model=AuthResponse)
async def logout(response: Response):
    response.delete_cookie(key="token", path="/")
    return AuthResponse()


@router.get("/me", response_model=MeResponse)
async def me(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        return MeResponse()
    return MeResponse(email=user["email"], user_id=user["id"])
