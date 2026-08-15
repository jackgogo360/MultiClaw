from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection

from multiclaw.config import Settings
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.models import (
    ApprovalRecord,
    InvalidTransitionError,
    LeaseConflictError,
    RunLease,
    RunStatus,
    StaleFenceError,
    TenantRunQuotaError,
    VersionConflictError,
)


class WorkflowCoordinator:
    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        connection: AsyncConnection | None = None,
    ) -> None:
        self._database = database
        self._settings = settings or Settings(_config_file="/nonexistent")
        self._connection = connection

    async def start_run(self, context: TenantContext, runtime_instance_id: str) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            await repository.lock_tenant(context.tenant_id)
            active_runs = await repository.count_active_runs(context.tenant_id)
            if active_runs >= self._settings.runtime.max_concurrent_runs_per_tenant:
                raise TenantRunQuotaError("tenant run quota exceeded")
            return await repository.create_run(
                context,
                runtime_instance_id=runtime_instance_id,
            )

    async def acquire_run(self, context: TenantContext, runtime_instance_id: str) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            lease = await repository.take_over_run(
                context,
                runtime_instance_id=runtime_instance_id,
            )
            if lease is None:
                raise LeaseConflictError("run lease is still current")
            return lease

    async def heartbeat(self, lease: RunLease) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            refreshed = await repository.refresh_lease(lease)
            if refreshed is None:
                raise StaleFenceError("run lease is stale")
            return refreshed

    async def transition_run(self, lease: RunLease, target: RunStatus) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            transitioned = await repository.transition_run(lease, target)
            if transitioned is None:
                raise StaleFenceError("run lease is stale")
            return transitioned

    async def finish_run(self, lease: RunLease, target: RunStatus) -> RunLease:
        if target not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
            RunStatus.BLOCKED_CORRUPT,
            RunStatus.BLOCKED_INCOMPATIBLE,
        }:
            raise InvalidTransitionError(f"{target.value} is not a terminal run status")

        async with self._write_connection() as conn:
            repository = self._repository(conn)
            if target is RunStatus.COMPLETED and await repository.has_nonterminal_execution(lease.context):
                raise InvalidTransitionError("run cannot complete while executions are nonterminal")
            transitioned = await repository.transition_run(lease, target)
            if transitioned is None:
                raise StaleFenceError("run lease is stale")
            return transitioned

    async def decide_approval(
        self,
        context: TenantContext,
        approval_id: str,
        *,
        approved: bool,
        version: int,
    ) -> ApprovalRecord:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            resolved = await repository.resolve_approval(
                context,
                approval_id,
                approved=approved,
                version=version,
            )
            if resolved is not None:
                return resolved

            if await repository.mark_approval_expired(context, approval_id, version):
                raise InvalidTransitionError("approval expired")

            current = await repository.get_approval(context, approval_id)
            if current is None:
                raise VersionConflictError("approval record not found")
            if current.status.value != "awaiting_user":
                raise VersionConflictError("approval already resolved")
            if current.version != version:
                raise VersionConflictError("approval version conflict")
            raise InvalidTransitionError("approval could not be resolved")

    @asynccontextmanager
    async def _write_connection(self):
        if self._connection is not None:
            yield self._connection
            return
        async with self._database.write_transaction() as conn:
            yield conn

    def _repository(self, conn) -> WorkflowRepository:
        return WorkflowRepository(
            conn,
            self._database.dialect,
            self._settings.workflow.heartbeat_ms,
            self._settings.workflow.lease_ttl_ms,
        )
