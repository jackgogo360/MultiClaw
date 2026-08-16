from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from multiclaw.config.settings import Settings
from multiclaw.storage import Database
from multiclaw.storage.repositories.deletions import DeletionJobRecord
from multiclaw.storage.uow import DeletionUnitOfWork
from multiclaw.tenancy.context import TenantContext


class RuntimePool(Protocol):
    async def revoke(self, tenant_id: str) -> None: ...


class ActiveRunsError(RuntimeError):
    pass


class RecoveryWindowClosedError(RuntimeError):
    pass


ActiveDeletionRunsError = ActiveRunsError
DeletionRecoveryExpiredError = RecoveryWindowClosedError


@dataclass(frozen=True, slots=True)
class DeletionStatus:
    job_id: str
    status: str
    requested_at: int
    purge_after: int


class DeletionService:
    def __init__(
        self,
        *,
        database: Database,
        runtime_pool: RuntimePool | None,
        settings: Settings,
    ) -> None:
        self._database = database
        self._runtime_pool = runtime_pool
        self._settings = settings

    async def request(
        self,
        tenant: TenantContext | str,
        *,
        retention_days: int | None = None,
    ) -> DeletionStatus:
        tenant_id = _tenant_id_from(tenant)
        retention = self._settings.deletion.retention_days if retention_days is None else retention_days
        should_revoke = False

        async with DeletionUnitOfWork(self._database, tenant_id) as uow:
            user = await uow.users.get_current(for_update=True)
            if user is None:
                raise RuntimeError("account not found")

            existing = await uow.deletions.get_current(for_update=True)
            if user.status == "pending_purge" and existing is not None and existing.status == "scheduled":
                return _status_from_job(existing)
            if user.status != "active":
                raise RuntimeError("account is not eligible for deletion scheduling")

            if await uow.workflow.count_blocking_activity() > 0:
                raise ActiveRunsError("active runs or tool executions must finish before deletion")

            now_ms = await uow.deletions.db_now_ms()
            await uow.workflow.expire_waiting_work(now_ms=now_ms)

            job = existing
            if job is None:
                job = await uow.deletions.create_scheduled(
                    requested_at=now_ms,
                    purge_after=now_ms + retention * 86_400_000,
                )

            updated = await uow.users.mark_pending_purge(
                expected_epoch=user.auth_epoch,
                requested_at=job.requested_at,
                purge_after=job.purge_after,
            )
            if not updated:
                raise RuntimeError("deletion request lost update")

            should_revoke = True
            result = _status_from_job(job)

        await self._revoke_runtime(tenant_id, should_revoke)
        return result

    async def request_account_deletion(self, tenant_id: str) -> DeletionStatus:
        return await self.request(tenant_id)

    async def recover(
        self,
        tenant: TenantContext | str,
        job_id: str,
    ) -> None:
        tenant_id = _tenant_id_from(tenant)
        should_revoke = False

        async with DeletionUnitOfWork(self._database, tenant_id) as uow:
            user = await uow.users.get_current(for_update=True)
            job = await uow.deletions.get_by_job_id(job_id, for_update=True)
            now_ms = await uow.deletions.db_now_ms()

            if user is None or user.status != "pending_purge":
                raise RecoveryWindowClosedError("recovery window is closed")
            if job is None:
                raise RecoveryWindowClosedError("recovery window is closed")
            if job.status != "scheduled":
                raise RecoveryWindowClosedError("recovery requires a scheduled deletion job")
            if now_ms >= job.purge_after:
                raise RecoveryWindowClosedError("recovery window is closed")

            deleted = await uow.deletions.delete_scheduled(job.job_id, expected_version=job.version)
            if not deleted:
                raise RecoveryWindowClosedError("recovery window is closed")

            restored = await uow.users.restore_active(
                expected_epoch=user.auth_epoch,
                now_ms=now_ms,
            )
            if not restored:
                raise RecoveryWindowClosedError("recovery window is closed")

            should_revoke = True

        await self._revoke_runtime(tenant_id, should_revoke)

    async def recover_account_deletion(
        self,
        *,
        tenant_id: str,
        job_id: str,
        now_ms: int | None = None,
    ) -> None:
        del now_ms
        await self.recover(tenant_id, job_id)

    async def get_status(self, tenant_id: str) -> dict[str, int | str | None]:
        async with DeletionUnitOfWork(self._database, tenant_id, read_only=True) as uow:
            user = await uow.users.get_current()
            job = await uow.deletions.get_current()
        if user is None:
            raise RuntimeError("account not found")
        return {
            "status": user.status,
            "requested_at": user.purge_requested_at,
            "purge_after": user.purge_after,
            "job_status": None if job is None else job.status,
        }

    async def force_job_purge_after(self, *, tenant_id: str, purge_after: int) -> None:
        async with DeletionUnitOfWork(self._database, tenant_id) as uow:
            await uow.deletions.force_purge_after(purge_after=purge_after)

    async def _revoke_runtime(self, tenant_id: str, should_revoke: bool) -> None:
        if not should_revoke or self._runtime_pool is None:
            return
        await self._runtime_pool.revoke(tenant_id)


def _tenant_id_from(tenant: TenantContext | str) -> str:
    if isinstance(tenant, TenantContext):
        return tenant.tenant_id
    return tenant


def _status_from_job(job: DeletionJobRecord) -> DeletionStatus:
    return DeletionStatus(
        job_id=job.job_id,
        status=job.status,
        requested_at=job.requested_at,
        purge_after=job.purge_after,
    )
