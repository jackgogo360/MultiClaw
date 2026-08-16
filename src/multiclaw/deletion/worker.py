from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.exc import OperationalError, ProgrammingError

from multiclaw.config.settings import Settings
from multiclaw.observability import increment_metric, record_trace_event
from multiclaw.storage import Database
from multiclaw.storage.repositories.deletions import DeletionJobRecord, next_claimable_tenant_id
from multiclaw.storage.uow import DeletionUnitOfWork
from multiclaw.tenancy import TenantContext, WorkspaceResolver


logger = logging.getLogger("multiclaw")


@dataclass(frozen=True, slots=True)
class DeletionBatchResult:
    claimed: int = 0
    completed: int = 0
    failed: int = 0


class DeletionWorker:
    def __init__(
        self,
        *,
        database: Database,
        runtime_pool,
        workspace_resolver: WorkspaceResolver,
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self._database = database
        self._runtime_pool = runtime_pool
        self._workspace_resolver = workspace_resolver
        self._settings = settings
        self._worker_id = worker_id or f"deletion-worker-{uuid4()}"
        self._lease_ttl_ms = settings.workflow.lease_ttl_ms
        self._schema_unavailable_logged = False

    async def purge_due_jobs(self, *, batch_size: int = 10) -> int:
        return (await self.run_batch(batch_size=batch_size)).completed

    async def run_batch(self, *, batch_size: int = 10) -> DeletionBatchResult:
        claimed = 0
        completed = 0
        failed = 0
        for _ in range(batch_size):
            tenant_id = await self._next_claimable_tenant_id()
            if tenant_id is None:
                break
            job = await self._claim_job(tenant_id)
            if job is None:
                continue
            claimed += 1
            if await self._purge_job(job):
                completed += 1
            else:
                failed += 1
        return DeletionBatchResult(claimed=claimed, completed=completed, failed=failed)

    async def run_until_stopped(
        self,
        *,
        stop_event: asyncio.Event,
        batch_size: int = 10,
        interval_seconds: float = 1.0,
    ) -> None:
        while not stop_event.is_set():
            await self.run_batch(batch_size=batch_size)
            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _next_claimable_tenant_id(self) -> str | None:
        try:
            async with self._database.connect() as conn:
                return await next_claimable_tenant_id(conn, self._database.dialect)
        except (OperationalError, ProgrammingError) as error:
            if _is_missing_deletion_table(error):
                if not self._schema_unavailable_logged:
                    logger.info("deletion worker disabled until deletion schema exists")
                    self._schema_unavailable_logged = True
                return None
            raise

    async def _claim_job(self, tenant_id: str) -> DeletionJobRecord | None:
        async with DeletionUnitOfWork(self._database, tenant_id) as uow:
            return await uow.deletions.claim_for_tenant(
                tenant_id,
                worker_id=self._worker_id,
                lease_ttl_ms=self._lease_ttl_ms,
            )

    async def _purge_job(self, job: DeletionJobRecord) -> bool:
        await self._runtime_pool.revoke(job.tenant_id)
        async with DeletionUnitOfWork(self._database, job.tenant_id, read_only=True) as uow:
            workspace_ids = await uow.deletions.list_workspace_ids(job.tenant_id)
            if await uow.workflow.count_blocking_activity(job.tenant_id) > 0:
                await self._record_retryable_error(job, "ACTIVE_RUNS")
                return False

        try:
            for workspace_id in workspace_ids:
                refreshed = await self._heartbeat_job(job)
                if refreshed is None:
                    return False
                job = refreshed
                workspace = self._workspace_resolver.resolve(TenantContext(job.tenant_id, workspace_id))
                self._remove_workspace_tree(workspace)
        except Exception as error:
            await self._record_retryable_error(job, type(error).__name__)
            return False

        refreshed = await self._heartbeat_job(job)
        if refreshed is None:
            return False
        job = refreshed

        async with DeletionUnitOfWork(self._database, job.tenant_id) as uow:
            purged = await uow.deletions.purge_account(
                job.tenant_id,
                worker_id=self._worker_id,
                fencing_token=job.fencing_token,
                version=job.version,
            )
        return purged

    async def _heartbeat_job(self, job: DeletionJobRecord) -> DeletionJobRecord | None:
        async with DeletionUnitOfWork(self._database, job.tenant_id) as uow:
            return await uow.deletions.heartbeat(
                job.tenant_id,
                worker_id=self._worker_id,
                fencing_token=job.fencing_token,
                version=job.version,
                lease_ttl_ms=self._lease_ttl_ms,
            )

    async def _record_retryable_error(self, job: DeletionJobRecord, error: str) -> None:
        increment_metric(
            "multiclaw_purge_retry_total",
            labels={
                "backend": getattr(self._database.dialect, "name", "unknown"),
                "operation": "purge_retry",
                "status": "error",
                "error_class": error.lower(),
            },
        )
        record_trace_event(
            "purge_retry",
            attributes={"tenant_id": job.tenant_id, "error": error},
        )
        async with DeletionUnitOfWork(self._database, job.tenant_id) as uow:
            await uow.deletions.mark_retryable_error(
                job.tenant_id,
                worker_id=self._worker_id,
                fencing_token=job.fencing_token,
                version=job.version,
                error=error,
            )

    def _remove_workspace_tree(self, path: Path) -> None:
        if not path.exists():
            return
        shutil.rmtree(path)


def _is_missing_deletion_table(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        table in text and ("no such table" in text or "doesn't exist" in text)
        for table in ("deletion_jobs", "users", "workspaces")
    )
