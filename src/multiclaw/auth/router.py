import logging

import jwt
from fastapi import APIRouter, HTTPException, Request, Response

from multiclaw.auth.email_sender import is_mock_enabled, send_verification_code
from multiclaw.auth.models import (
    AuthResponse,
    CSRF_COOKIE_NAME,
    DELETION_RECOVERY_TOKEN_AUDIENCE,
    DELETION_RECOVERY_TOKEN_TTL_SECONDS,
    LOGIN_CODE_PURPOSE,
    MAX_SENDS_PER_DAY,
    MeResponse,
    SendCodeRequest,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    SESSION_TOKEN_AUDIENCE,
    VERIFICATION_CODE_TTL_SECONDS,
    VerifyRequest,
    issue_verification_code,
)
from multiclaw.config import Settings
from multiclaw.security.csrf import generate_token
from multiclaw.storage.uow import AuthUnitOfWork

logger = logging.getLogger("multiclaw")
router = APIRouter(prefix="/auth")


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _make_jwt(*, user_id: str, email: str, auth_epoch: int, signing_key: bytes, issued_at: int) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "aud": SESSION_TOKEN_AUDIENCE,
            "auth_epoch": auth_epoch,
            "iat": issued_at,
            "exp": issued_at + SESSION_TTL_SECONDS,
        },
        signing_key,
        algorithm="HS256",
    )


def _make_deletion_recovery_jwt(*, user_id: str, email: str, job_id: str, signing_key: bytes, issued_at: int) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "aud": DELETION_RECOVERY_TOKEN_AUDIENCE,
            "purpose": "deletion_recovery",
            "job_id": job_id,
            "iat": issued_at,
            "exp": issued_at + DELETION_RECOVERY_TOKEN_TTL_SECONDS,
        },
        signing_key,
        algorithm="HS256",
    )


@router.get("/csrf")
async def csrf(response: Response, request: Request):
    token = generate_token()
    _set_csrf_cookie(request, response, token)
    return {"token": token}


@router.post("/send-code", response_model=AuthResponse)
async def send_code(body: SendCodeRequest, request: Request):
    email = body.email.strip().lower()
    if "@" not in email or len(email) < 5:
        raise HTTPException(status_code=422, detail="Please enter a valid email")

    settings = _get_settings(request)
    forced_code = getattr(request.app.state, "auth_forced_code", None)
    code_issue = issue_verification_code(
        request.app.state.auth.signing_key,
        email=email,
        purpose=LOGIN_CODE_PURPOSE,
        forced_code=forced_code,
    )

    async with AuthUnitOfWork(request.app.state.database) as uow:
        recent = await uow.verification_codes.count_recent_codes(
            email,
            purpose=LOGIN_CODE_PURPOSE,
            window_ms=24 * 60 * 60 * 1000,
        )
        if recent >= MAX_SENDS_PER_DAY:
            raise HTTPException(
                status_code=429, detail="Too many attempts, please try again tomorrow"
            )
        await uow.verification_codes.issue_code(
            email=email,
            purpose=code_issue.purpose,
            code_digest=code_issue.code_digest,
            ttl_seconds=VERIFICATION_CODE_TTL_SECONDS,
        )

    if not is_mock_enabled(settings):
        try:
            await send_verification_code(settings, email, code_issue.code)
        except Exception:
            logger.exception("Failed to send verification email to %s", email)
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

    async with AuthUnitOfWork(request.app.state.database) as uow:
        code_digest = issue_verification_code(
            request.app.state.auth.signing_key,
            email=email,
            purpose=LOGIN_CODE_PURPOSE,
            forced_code=code,
        ).code_digest
        vc = await uow.verification_codes.consume_latest_code(
            email=email,
            purpose=LOGIN_CODE_PURPOSE,
            code_digest=code_digest,
        )
        if vc is None:
            raise HTTPException(
                status_code=401, detail="Invalid or expired verification code"
            )
        user = await uow.users.create_user_with_default_workspace(email)
        now_seconds = await uow.verification_codes.db_now_ms() // 1000

    token = _make_jwt(
        user_id=user.id,
        email=user.email,
        auth_epoch=user.auth_epoch,
        signing_key=request.app.state.auth.signing_key,
        issued_at=now_seconds,
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        secure=_secure_cookie(request),
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    _set_csrf_cookie(request, response, generate_token())

    return AuthResponse()


@router.post("/logout", response_model=AuthResponse)
async def logout(response: Response, request: Request):
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    _set_csrf_cookie(request, response, generate_token())
    return AuthResponse()


@router.get("/me", response_model=MeResponse)
async def me(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        return MeResponse()
    return MeResponse(email=user["email"], user_id=user["id"])


def _set_csrf_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        secure=_secure_cookie(request),
        httponly=False,
        samesite="lax",
        path="/",
    )


def _secure_cookie(request: Request) -> bool:
    hostname = (request.url.hostname or "").lower()
    return hostname not in {"localhost", "127.0.0.1", "testserver"}
