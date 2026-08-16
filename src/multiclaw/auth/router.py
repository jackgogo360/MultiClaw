import logging

import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from multiclaw.auth.email_sender import is_mock_enabled, send_verification_code
from multiclaw.auth.models import (
    AuthResponse,
    CSRF_COOKIE_NAME,
    DELETION_RECOVERY_TOKEN_AUDIENCE,
    DELETION_RECOVERY_TOKEN_TTL_SECONDS,
    DELETION_RECOVERY_CODE_PURPOSE,
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
from multiclaw.api.account import RECOVERY_COOKIE_NAME
from multiclaw.config import Settings
from multiclaw.security.csrf import generate_token
from multiclaw.storage.schema import deletion_jobs, users
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
    reserved_code_id: str | None = None

    async with AuthUnitOfWork(request.app.state.database) as uow:
        await uow.verification_codes.acquire_rate_limit_lock(
            email=email,
            purpose=LOGIN_CODE_PURPOSE,
        )
        recent = await uow.verification_codes.count_recent_codes(
            email,
            purpose=LOGIN_CODE_PURPOSE,
            window_ms=24 * 60 * 60 * 1000,
        )
        if recent >= MAX_SENDS_PER_DAY:
            raise HTTPException(
                status_code=429, detail="Too many attempts, please try again tomorrow"
            )
        reserved_code_id = await uow.verification_codes.issue_code(
            email=email,
            purpose=code_issue.purpose,
            code_digest=code_issue.code_digest,
            ttl_seconds=VERIFICATION_CODE_TTL_SECONDS,
        )

    if is_mock_enabled(settings):
        return AuthResponse()

    # No-schema tradeoff: if the process crashes after the reservation commits
    # but before provider result/compensation, an undelivered code can remain
    # until expiry. We keep provider I/O outside the DB write lock to avoid
    # stalling unrelated writes, then compensate precisely by code id on error.
    try:
        await send_verification_code(settings, email, code_issue.code)
    except BaseException as error:
        cleanup_error: BaseException | None = None
        if reserved_code_id is not None:
            try:
                async with AuthUnitOfWork(request.app.state.database) as uow:
                    await uow.verification_codes.delete_code_by_id(
                        code_id=reserved_code_id,
                        email=email,
                        purpose=LOGIN_CODE_PURPOSE,
                    )
            except BaseException as delete_error:
                cleanup_error = delete_error
                logger.error(
                    "Failed to delete reserved verification code: %s",
                    type(delete_error).__name__,
                )

        if not isinstance(error, Exception):
            if cleanup_error is not None:
                error.add_note(
                    f"verification code cleanup failed: {type(cleanup_error).__name__}"
                )
            raise error

        logger.error(
            "Failed to send verification email: %s",
            type(error).__name__,
        )
        http_error = HTTPException(
            status_code=502,
            detail="Failed to send email, please try again later",
        )
        if cleanup_error is not None:
            http_error.add_note(
                f"verification code cleanup failed: {type(cleanup_error).__name__}"
            )
        raise http_error from error

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
            await uow.commit()
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


@router.post("/deletion-recovery/send-code", response_model=AuthResponse)
async def send_deletion_recovery_code(body: SendCodeRequest, request: Request):
    email = body.email.strip().lower()
    if "@" not in email or len(email) < 5:
        raise HTTPException(status_code=422, detail="Please enter a valid email")

    settings = _get_settings(request)
    forced_code = getattr(request.app.state, "auth_forced_code", None)
    code_issue = issue_verification_code(
        request.app.state.auth.signing_key,
        email=email,
        purpose=DELETION_RECOVERY_CODE_PURPOSE,
        forced_code=forced_code,
    )
    reserved_code_id: str | None = None
    should_send = False

    async with AuthUnitOfWork(request.app.state.database) as uow:
        await uow.verification_codes.acquire_rate_limit_lock(
            email=email,
            purpose=DELETION_RECOVERY_CODE_PURPOSE,
        )
        now_ms = await uow.verification_codes.db_now_ms()
        scheduled = (
            await uow.conn.execute(
                select(deletion_jobs.c.job_id)
                .select_from(users.join(deletion_jobs, deletion_jobs.c.tenant_id == users.c.id))
                .where(
                    users.c.email == email,
                    users.c.status == "pending_purge",
                    deletion_jobs.c.status == "scheduled",
                    deletion_jobs.c.purge_after > now_ms,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if scheduled is None:
            return AuthResponse()
        recent = await uow.verification_codes.count_recent_codes(
            email,
            purpose=DELETION_RECOVERY_CODE_PURPOSE,
            window_ms=24 * 60 * 60 * 1000,
        )
        if recent >= MAX_SENDS_PER_DAY:
            raise HTTPException(
                status_code=429, detail="Too many attempts, please try again tomorrow"
            )
        reserved_code_id = await uow.verification_codes.issue_code(
            email=email,
            purpose=code_issue.purpose,
            code_digest=code_issue.code_digest,
            ttl_seconds=VERIFICATION_CODE_TTL_SECONDS,
        )
        should_send = True

    if not should_send or is_mock_enabled(settings):
        return AuthResponse()

    try:
        await send_verification_code(settings, email, code_issue.code)
    except BaseException as error:
        cleanup_error: BaseException | None = None
        if reserved_code_id is not None:
            try:
                async with AuthUnitOfWork(request.app.state.database) as uow:
                    await uow.verification_codes.delete_code_by_id(
                        code_id=reserved_code_id,
                        email=email,
                        purpose=DELETION_RECOVERY_CODE_PURPOSE,
                    )
            except BaseException as delete_error:
                cleanup_error = delete_error
                logger.error(
                    "Failed to delete reserved deletion recovery code: %s",
                    type(delete_error).__name__,
                )

        if not isinstance(error, Exception):
            if cleanup_error is not None:
                error.add_note(
                    f"deletion recovery code cleanup failed: {type(cleanup_error).__name__}"
                )
            raise error

        logger.error(
            "Failed to send deletion recovery email: %s",
            type(error).__name__,
        )
        http_error = HTTPException(
            status_code=502,
            detail="Failed to send email, please try again later",
        )
        if cleanup_error is not None:
            http_error.add_note(
                f"deletion recovery code cleanup failed: {type(cleanup_error).__name__}"
            )
        raise http_error from error
    return AuthResponse()


@router.post("/deletion-recovery/verify", response_model=AuthResponse)
async def verify_deletion_recovery_code(body: VerifyRequest, request: Request, response: Response):
    email = body.email.strip().lower()
    code = body.code.strip()
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=422, detail="Invalid code format")

    async with AuthUnitOfWork(request.app.state.database) as uow:
        now_ms = await uow.verification_codes.db_now_ms()
        row = (
            await uow.conn.execute(
                select(
                    users.c.id,
                    users.c.email,
                    deletion_jobs.c.job_id,
                    deletion_jobs.c.purge_after,
                )
                .select_from(users.join(deletion_jobs, deletion_jobs.c.tenant_id == users.c.id))
                .where(
                    users.c.email == email,
                    users.c.status == "pending_purge",
                    deletion_jobs.c.status == "scheduled",
                    deletion_jobs.c.purge_after > now_ms,
                )
                .limit(1)
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=401, detail="Invalid or expired verification code")
        code_digest = issue_verification_code(
            request.app.state.auth.signing_key,
            email=email,
            purpose=DELETION_RECOVERY_CODE_PURPOSE,
            forced_code=code,
        ).code_digest
        vc = await uow.verification_codes.consume_latest_code(
            email=email,
            purpose=DELETION_RECOVERY_CODE_PURPOSE,
            code_digest=code_digest,
        )
        if vc is None:
            await uow.commit()
            raise HTTPException(status_code=401, detail="Invalid or expired verification code")
        now_seconds = now_ms // 1000

    token = _make_deletion_recovery_jwt(
        user_id=str(row["id"]),
        email=str(row["email"]),
        job_id=str(row["job_id"]),
        signing_key=request.app.state.auth.signing_key,
        issued_at=now_seconds,
    )
    response.set_cookie(
        key=RECOVERY_COOKIE_NAME,
        value=token,
        secure=_secure_cookie(request),
        httponly=True,
        samesite="lax",
        max_age=DELETION_RECOVERY_TOKEN_TTL_SECONDS,
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
