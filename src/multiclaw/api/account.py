from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from multiclaw.auth.models import (
    CSRF_COOKIE_NAME,
    DELETION_RECOVERY_TOKEN_AUDIENCE,
    RECENT_AUTH_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
)
from multiclaw.deletion.service import (
    ActiveDeletionRunsError,
    DeletionRecoveryExpiredError,
    DeletionService,
)
from multiclaw.storage.schema import deletion_jobs, users
from multiclaw.storage.uow import AuthUnitOfWork


RECOVERY_COOKIE_NAME = "recovery_token"
RECOVERY_PURPOSE = "deletion_recovery"
RECOVERY_STATUS_PATH = "/api/account/deletion"
RECOVERY_RESTORE_PATH = "/api/account/deletion/recover"
router = APIRouter(prefix="/api/account")


@dataclass(frozen=True, slots=True)
class RecoveryAuthContext:
    tenant_id: str
    email: str
    job_id: str


def _service(request: Request) -> DeletionService:
    service = getattr(request.app.state, "deletion_service", None)
    if service is not None:
        return service
    return DeletionService(
        database=request.app.state.database,
        runtime_pool=request.app.state.runtime_pool,
        settings=request.app.state.settings,
    )


async def require_recent_deletion_auth(request: Request):
    user = getattr(request.state, "authenticated_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    token_iat = getattr(request.state, "authenticated_iat", None)
    if type(token_iat) is not int:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async with request.app.state.database.connect() as conn:
        result = await conn.execute(select(request.app.state.database.dialect.db_now_ms()))
        db_now_seconds = int(result.scalar_one()) // 1000
    if db_now_seconds - token_iat > RECENT_AUTH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Recent authentication required")
    return user


async def require_recovery_auth(request: Request) -> RecoveryAuthContext:
    token = request.cookies.get(RECOVERY_COOKIE_NAME)
    authorization = request.headers.get("authorization", "")
    if not token and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = jwt.decode(
            token,
            request.app.state.auth.signing_key,
            algorithms=["HS256"],
            audience=DELETION_RECOVERY_TOKEN_AUDIENCE,
        )
    except jwt.InvalidTokenError as error:
        raise HTTPException(status_code=401, detail="Unauthorized") from error

    subject = payload.get("sub")
    email = payload.get("email")
    job_id = payload.get("job_id")
    purpose = payload.get("purpose")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if not all(isinstance(value, str) and value for value in (subject, email, job_id)):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if purpose != RECOVERY_PURPOSE or type(issued_at) is not int or type(expires_at) is not int:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async with AuthUnitOfWork(request.app.state.database, read_only=True) as uow:
        row = (
            await uow.conn.execute(
                select(
                    users.c.id,
                    users.c.email,
                    users.c.status,
                    deletion_jobs.c.job_id,
                    deletion_jobs.c.status.label("job_status"),
                    deletion_jobs.c.purge_after,
                )
                .select_from(users.join(deletion_jobs, deletion_jobs.c.tenant_id == users.c.id))
                .where(
                    users.c.id == subject,
                    deletion_jobs.c.job_id == job_id,
                )
                .limit(1)
            )
        ).mappings().first()
        now_ms = await uow.verification_codes.db_now_ms()

    if row is None or row["email"] != email or row["status"] != "pending_purge":
        raise HTTPException(status_code=401, detail="Unauthorized")
    if row["job_status"] != "scheduled" or now_ms >= int(row["purge_after"]):
        raise HTTPException(status_code=410, detail="Deletion recovery window expired")
    return RecoveryAuthContext(tenant_id=subject, email=email, job_id=job_id)


@router.post("/deletion")
async def request_account_deletion(
    request: Request,
    response: Response,
    _recent_user=Depends(require_recent_deletion_auth),
):
    tenant_id = _recent_user.id
    try:
        scheduled = await _service(request).request_account_deletion(tenant_id)
    except ActiveDeletionRunsError:
        raise HTTPException(
            status_code=409,
            detail={"code": "ACTIVE_RUNS", "message": "Active runs must finish first"},
        ) from None
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")
    return {
        "status": scheduled.status,
        "job_id": scheduled.job_id,
        "requested_at": scheduled.requested_at,
        "purge_after": scheduled.purge_after,
    }


@router.get("/deletion")
async def get_account_deletion_status(
    request: Request,
    recovery: RecoveryAuthContext = Depends(require_recovery_auth),
):
    status = await _service(request).get_status(recovery.tenant_id)
    return {
        "status": status["status"],
        "purge_after": status["purge_after"],
    }


@router.post("/deletion/recover")
async def recover_account_deletion(
    request: Request,
    response: Response,
    recovery: RecoveryAuthContext = Depends(require_recovery_auth),
):
    try:
        await _service(request).recover_account_deletion(
            tenant_id=recovery.tenant_id,
            job_id=recovery.job_id,
        )
    except DeletionRecoveryExpiredError:
        raise HTTPException(status_code=410, detail="Deletion recovery window expired") from None
    response.delete_cookie(key=RECOVERY_COOKIE_NAME, path="/")
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")
    return {"ok": True}
