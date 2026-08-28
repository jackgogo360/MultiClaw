from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect
from multiclaw.storage.repositories.auth import _ConnectionBoundRepository
from multiclaw.storage.schema import (
    agent_runs,
    approval_requests,
    audit_logs,
    chat_sessions,
    deletion_jobs,
    execution_checkpoints,
    memory_entries,
    tool_executions,
    user_secrets,
    users,
    verification_codes,
    workspaces,
)
from multiclaw.workflow.models import ApprovalStatus, ExecutionStatus, RunStatus, TERMINAL_RUN_STATUSES


Dialect = SQLiteDialect | MySQLDialect
BLOCKING_RUN_STATUSES = (
    RunStatus.RUNNING.value,
    RunStatus.RESUMING.value,
)
TERMINAL_RUN_STATUS_VALUES = tuple(status.value for status in TERMINAL_RUN_STATUSES)
BLOCKING_EXECUTION_STATUSES = (
    ExecutionStatus.REPLAYING.value,
    ExecutionStatus.EXECUTING.value,
)


@dataclass(frozen=True, slots=True)
class DeletionUserRecord:
    id: str
    email: str
    status: str
    auth_epoch: int
    default_workspace_id: str | None
    purge_requested_at: int | None
    purge_after: int | None


@dataclass(frozen=True, slots=True)
class DeletionJobRecord:
    job_id: str
    tenant_id: str
    status: str
    purge_after: int
    requested_at: int
    started_at: int | None
    worker_id: str | None
    lease_expires_at: int | None
    heartbeat_at: int | None
    fencing_token: int
    version: int
    attempt_count: int
    last_error: str | None


def _user_from_row(row: object) -> DeletionUserRecord:
    mapping = row
    return DeletionUserRecord(
        id=str(mapping["id"]),
        email=str(mapping["email"]),
        status=str(mapping["status"]),
        auth_epoch=int(mapping["auth_epoch"]),
        default_workspace_id=None
        if mapping["default_workspace_id"] is None
        else str(mapping["default_workspace_id"]),
        purge_requested_at=None
        if mapping["purge_requested_at"] is None
        else int(mapping["purge_requested_at"]),
        purge_after=None if mapping["purge_after"] is None else int(mapping["purge_after"]),
    )


def _job_from_row(row: object) -> DeletionJobRecord:
    mapping = row
    return DeletionJobRecord(
        job_id=str(mapping["job_id"]),
        tenant_id=str(mapping["tenant_id"]),
        status=str(mapping["status"]),
        purge_after=int(mapping["purge_after"]),
        requested_at=int(mapping["requested_at"]),
        started_at=None if mapping["started_at"] is None else int(mapping["started_at"]),
        worker_id=None if mapping["worker_id"] is None else str(mapping["worker_id"]),
        lease_expires_at=None if mapping["lease_expires_at"] is None else int(mapping["lease_expires_at"]),
        heartbeat_at=None if mapping["heartbeat_at"] is None else int(mapping["heartbeat_at"]),
        fencing_token=int(mapping["fencing_token"]),
        version=int(mapping["version"]),
        attempt_count=int(mapping["attempt_count"]),
        last_error=None if mapping["last_error"] is None else str(mapping["last_error"]),
    )


class _ScopedDeletionRepository(_ConnectionBoundRepository):
    def __init__(self, conn: AsyncConnection, dialect: Dialect, tenant_id: str) -> None:
        super().__init__(conn, dialect)
        self._tenant_id = tenant_id

    def _scope_tenant(self, tenant_id: str | None = None) -> str:
        return self._tenant_id if tenant_id is None else tenant_id


class DeletionUserRepository(_ScopedDeletionRepository):
    async def get_current(self, *, for_update: bool = False) -> DeletionUserRecord | None:
        return await self.get_by_id(for_update=for_update)

    async def get_by_id(
        self,
        tenant_id: str | None = None,
        *,
        for_update: bool = False,
    ) -> DeletionUserRecord | None:
        query = (
            select(
                users.c.id,
                users.c.email,
                users.c.status,
                users.c.auth_epoch,
                users.c.default_workspace_id,
                users.c.purge_requested_at,
                users.c.purge_after,
            )
            .where(users.c.id == self._scope_tenant(tenant_id))
            .limit(1)
        )
        if for_update and self._dialect.name == "mysql":
            query = query.with_for_update()
        row = (await self._conn.execute(query)).mappings().first()
        return None if row is None else _user_from_row(row)

    async def get_pending_by_email(self, email: str, *, for_update: bool = False) -> DeletionUserRecord | None:
        query = (
            select(
                users.c.id,
                users.c.email,
                users.c.status,
                users.c.auth_epoch,
                users.c.default_workspace_id,
                users.c.purge_requested_at,
                users.c.purge_after,
            )
            .where(users.c.email == email, users.c.status == "pending_purge")
            .limit(1)
        )
        if for_update and self._dialect.name == "mysql":
            query = query.with_for_update()
        row = (await self._conn.execute(query)).mappings().first()
        return None if row is None else _user_from_row(row)

    async def mark_pending_purge(
        self,
        *,
        expected_epoch: int,
        requested_at: int,
        purge_after: int,
        tenant_id: str | None = None,
    ) -> bool:
        result = await self._conn.execute(
            update(users)
            .where(
                users.c.id == self._scope_tenant(tenant_id),
                users.c.status == "active",
                users.c.auth_epoch == expected_epoch,
            )
            .values(
                status="pending_purge",
                purge_requested_at=requested_at,
                purge_after=purge_after,
                auth_epoch=expected_epoch + 1,
                updated_at=requested_at,
            )
        )
        return int(result.rowcount or 0) == 1

    async def restore_active(
        self,
        *,
        expected_epoch: int,
        now_ms: int,
        tenant_id: str | None = None,
    ) -> bool:
        result = await self._conn.execute(
            update(users)
            .where(
                users.c.id == self._scope_tenant(tenant_id),
                users.c.status == "pending_purge",
                users.c.auth_epoch == expected_epoch,
            )
            .values(
                status="active",
                purge_requested_at=None,
                purge_after=None,
                auth_epoch=expected_epoch + 1,
                updated_at=now_ms,
            )
        )
        return int(result.rowcount or 0) == 1


class DeletionWorkflowRepository(_ScopedDeletionRepository):
    async def count_blocking_activity(self, tenant_id: str | None = None) -> int:
        scoped_tenant_id = self._scope_tenant(tenant_id)
        now_ms = self._dialect.db_now_ms()
        active_runs = await self._conn.scalar(
            select(func.count())
            .select_from(agent_runs)
            .where(
                agent_runs.c.tenant_id == scoped_tenant_id,
                agent_runs.c.run_status.in_(BLOCKING_RUN_STATUSES),
            )
        )
        valid_leases = await self._conn.scalar(
            select(func.count())
            .select_from(agent_runs)
            .where(
                agent_runs.c.tenant_id == scoped_tenant_id,
                ~agent_runs.c.run_status.in_(TERMINAL_RUN_STATUS_VALUES),
                agent_runs.c.lease_expires_at.is_not(None),
                agent_runs.c.lease_expires_at > now_ms,
            )
        )
        active_tools = await self._conn.scalar(
            select(func.count())
            .select_from(tool_executions)
            .where(
                tool_executions.c.tenant_id == scoped_tenant_id,
                tool_executions.c.execution_status.in_(BLOCKING_EXECUTION_STATUSES),
            )
        )
        return int(active_runs or 0) + int(valid_leases or 0) + int(active_tools or 0)

    async def expire_waiting_work(self, *, now_ms: int, tenant_id: str | None = None) -> None:
        scoped_tenant_id = self._scope_tenant(tenant_id)
        await self._conn.execute(
            update(approval_requests)
            .where(
                approval_requests.c.tenant_id == scoped_tenant_id,
                approval_requests.c.approval_status == ApprovalStatus.AWAITING_USER.value,
            )
            .values(
                approval_status=ApprovalStatus.EXPIRED.value,
                resolved_at=now_ms,
                version=approval_requests.c.version + 1,
            )
        )
        await self._conn.execute(
            update(agent_runs)
            .where(
                agent_runs.c.tenant_id == scoped_tenant_id,
                agent_runs.c.run_status == RunStatus.AWAITING_USER.value,
                (
                    agent_runs.c.lease_expires_at.is_(None)
                    | (agent_runs.c.lease_expires_at <= now_ms)
                ),
            )
            .values(
                run_status=RunStatus.CANCELLED.value,
                runtime_instance_id=None,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=now_ms,
                updated_at=now_ms,
                finished_at=now_ms,
                version=agent_runs.c.version + 1,
            )
        )

    async def expire_pending_work(self, tenant_id: str | None = None) -> None:
        await self.expire_waiting_work(now_ms=await self.db_now_ms(), tenant_id=tenant_id)

    async def db_now_ms(self) -> int:
        return int((await self._conn.execute(select(self._dialect.db_now_ms()))).scalar_one())


class DeletionJobRepository(_ScopedDeletionRepository):
    async def db_now_ms(self) -> int:
        return int((await self._conn.execute(select(self._dialect.db_now_ms()))).scalar_one())

    async def get_current(
        self,
        *,
        status: str | None = None,
        for_update: bool = False,
    ) -> DeletionJobRecord | None:
        return await self.get_for_tenant(status=status, for_update=for_update)

    async def get_for_tenant(
        self,
        tenant_id: str | None = None,
        *,
        status: str | None = None,
        for_update: bool = False,
    ) -> DeletionJobRecord | None:
        query = select(
            deletion_jobs.c.job_id,
            deletion_jobs.c.tenant_id,
            deletion_jobs.c.status,
            deletion_jobs.c.purge_after,
            deletion_jobs.c.requested_at,
            deletion_jobs.c.started_at,
            deletion_jobs.c.worker_id,
            deletion_jobs.c.lease_expires_at,
            deletion_jobs.c.heartbeat_at,
            deletion_jobs.c.fencing_token,
            deletion_jobs.c.version,
            deletion_jobs.c.attempt_count,
            deletion_jobs.c.last_error,
        ).where(deletion_jobs.c.tenant_id == self._scope_tenant(tenant_id))
        if status is not None:
            query = query.where(deletion_jobs.c.status == status)
        query = query.limit(1)
        if for_update and self._dialect.name == "mysql":
            query = query.with_for_update()
        row = (await self._conn.execute(query)).mappings().first()
        return None if row is None else _job_from_row(row)

    async def get_by_job_id(
        self,
        job_id: str,
        *,
        for_update: bool = False,
    ) -> DeletionJobRecord | None:
        query = (
            select(
                deletion_jobs.c.job_id,
                deletion_jobs.c.tenant_id,
                deletion_jobs.c.status,
                deletion_jobs.c.purge_after,
                deletion_jobs.c.requested_at,
                deletion_jobs.c.started_at,
                deletion_jobs.c.worker_id,
                deletion_jobs.c.lease_expires_at,
                deletion_jobs.c.heartbeat_at,
                deletion_jobs.c.fencing_token,
                deletion_jobs.c.version,
                deletion_jobs.c.attempt_count,
                deletion_jobs.c.last_error,
            )
            .where(
                deletion_jobs.c.tenant_id == self._tenant_id,
                deletion_jobs.c.job_id == job_id,
            )
            .limit(1)
        )
        if for_update and self._dialect.name == "mysql":
            query = query.with_for_update()
        row = (await self._conn.execute(query)).mappings().first()
        return None if row is None else _job_from_row(row)

    async def get_pending_by_email(self, email: str) -> tuple[DeletionUserRecord, DeletionJobRecord] | None:
        row = (
            await self._conn.execute(
                select(
                    users.c.id,
                    users.c.email,
                    users.c.status,
                    users.c.auth_epoch,
                    users.c.default_workspace_id,
                    users.c.purge_requested_at,
                    users.c.purge_after,
                    deletion_jobs.c.job_id,
                    deletion_jobs.c.tenant_id,
                    deletion_jobs.c.status.label("job_status"),
                    deletion_jobs.c.purge_after.label("job_purge_after"),
                    deletion_jobs.c.requested_at,
                    deletion_jobs.c.started_at,
                    deletion_jobs.c.worker_id,
                    deletion_jobs.c.lease_expires_at,
                    deletion_jobs.c.heartbeat_at,
                    deletion_jobs.c.fencing_token,
                    deletion_jobs.c.version,
                    deletion_jobs.c.attempt_count,
                    deletion_jobs.c.last_error,
                )
                .select_from(users.join(deletion_jobs, deletion_jobs.c.tenant_id == users.c.id))
                .where(
                    users.c.email == email,
                    users.c.status == "pending_purge",
                    deletion_jobs.c.status == "scheduled",
                )
                .limit(1)
            )
        ).mappings().first()
        if row is None:
            return None
        user = _user_from_row(row)
        job = DeletionJobRecord(
            job_id=str(row["job_id"]),
            tenant_id=str(row["tenant_id"]),
            status=str(row["job_status"]),
            purge_after=int(row["job_purge_after"]),
            requested_at=int(row["requested_at"]),
            started_at=None if row["started_at"] is None else int(row["started_at"]),
            worker_id=None if row["worker_id"] is None else str(row["worker_id"]),
            lease_expires_at=None if row["lease_expires_at"] is None else int(row["lease_expires_at"]),
            heartbeat_at=None if row["heartbeat_at"] is None else int(row["heartbeat_at"]),
            fencing_token=int(row["fencing_token"]),
            version=int(row["version"]),
            attempt_count=int(row["attempt_count"]),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
        )
        return user, job

    async def create_scheduled(
        self,
        *,
        requested_at: int,
        purge_after: int,
        tenant_id: str | None = None,
    ) -> DeletionJobRecord:
        scoped_tenant_id = self._scope_tenant(tenant_id)
        job_id = str(uuid4())
        await self._conn.execute(
            insert(deletion_jobs).values(
                job_id=job_id,
                tenant_id=scoped_tenant_id,
                status="scheduled",
                purge_after=purge_after,
                requested_at=requested_at,
                started_at=None,
                worker_id=None,
                lease_expires_at=None,
                heartbeat_at=None,
                fencing_token=0,
                version=0,
                attempt_count=0,
                last_error=None,
            )
        )
        record = await self.get_for_tenant(scoped_tenant_id)
        assert record is not None
        return record

    async def delete_scheduled(
        self,
        job_id: str,
        *,
        expected_version: int,
        tenant_id: str | None = None,
    ) -> bool:
        result = await self._conn.execute(
            delete(deletion_jobs).where(
                deletion_jobs.c.tenant_id == self._scope_tenant(tenant_id),
                deletion_jobs.c.job_id == job_id,
                deletion_jobs.c.status == "scheduled",
                deletion_jobs.c.version == expected_version,
            )
        )
        return int(result.rowcount or 0) == 1

    async def force_purge_after(
        self,
        *,
        purge_after: int,
        tenant_id: str | None = None,
    ) -> None:
        scoped_tenant_id = self._scope_tenant(tenant_id)
        await self._conn.execute(
            update(deletion_jobs)
            .where(deletion_jobs.c.tenant_id == scoped_tenant_id)
            .values(purge_after=purge_after)
        )
        await self._conn.execute(
            update(users)
            .where(users.c.id == scoped_tenant_id)
            .values(purge_after=purge_after)
        )

    async def claim_due(
        self,
        *,
        worker_id: str,
        lease_ttl_ms: int,
        tenant_id: str | None = None,
    ) -> DeletionJobRecord | None:
        scoped_tenant_id = self._scope_tenant(tenant_id)
        current = await self.get_for_tenant(scoped_tenant_id, for_update=self._dialect.name == "mysql")
        if current is None:
            return None

        now_ms = await self.db_now_ms()
        if current.status == "scheduled":
            if current.purge_after > now_ms:
                return None
            predicate = and_(
                deletion_jobs.c.tenant_id == scoped_tenant_id,
                deletion_jobs.c.status == "scheduled",
                deletion_jobs.c.version == current.version,
                deletion_jobs.c.purge_after <= now_ms,
            )
            started_at = now_ms
        else:
            if current.lease_expires_at is None or current.lease_expires_at > now_ms:
                return None
            predicate = and_(
                deletion_jobs.c.tenant_id == scoped_tenant_id,
                deletion_jobs.c.status == "running",
                deletion_jobs.c.version == current.version,
                deletion_jobs.c.fencing_token == current.fencing_token,
                deletion_jobs.c.lease_expires_at <= now_ms,
            )
            started_at = current.started_at or now_ms

        result = await self._conn.execute(
            update(deletion_jobs)
            .where(predicate)
            .values(
                status="running",
                started_at=started_at,
                worker_id=worker_id,
                lease_expires_at=now_ms + lease_ttl_ms,
                heartbeat_at=now_ms,
                fencing_token=current.fencing_token + 1,
                version=current.version + 1,
                attempt_count=current.attempt_count + 1,
                last_error=None,
            )
        )
        if int(result.rowcount or 0) != 1:
            return None
        claimed = await self.get_for_tenant(scoped_tenant_id)
        assert claimed is not None
        return claimed

    async def claim_for_tenant(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        lease_ttl_ms: int,
    ) -> DeletionJobRecord | None:
        return await self.claim_due(
            tenant_id=tenant_id,
            worker_id=worker_id,
            lease_ttl_ms=lease_ttl_ms,
        )

    async def mark_retryable_error(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        version: int,
        error: str,
    ) -> None:
        now_ms = await self.db_now_ms()
        await self._conn.execute(
            update(deletion_jobs)
            .where(
                deletion_jobs.c.tenant_id == self._scope_tenant(tenant_id),
                deletion_jobs.c.status == "running",
                deletion_jobs.c.worker_id == worker_id,
                deletion_jobs.c.fencing_token == fencing_token,
                deletion_jobs.c.version == version,
            )
            .values(
                heartbeat_at=now_ms,
                last_error=error,
                version=version + 1,
            )
        )

    async def list_workspace_ids(self, tenant_id: str | None = None) -> list[str]:
        rows = (
            await self._conn.execute(
                select(workspaces.c.id)
                .where(workspaces.c.tenant_id == self._scope_tenant(tenant_id))
                .order_by(workspaces.c.id.asc())
            )
        ).scalars()
        return [str(row) for row in rows]

    async def heartbeat(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        version: int,
        lease_ttl_ms: int,
    ) -> DeletionJobRecord | None:
        now_ms = await self.db_now_ms()
        result = await self._conn.execute(
            update(deletion_jobs)
            .where(
                deletion_jobs.c.tenant_id == self._scope_tenant(tenant_id),
                deletion_jobs.c.status == "running",
                deletion_jobs.c.worker_id == worker_id,
                deletion_jobs.c.fencing_token == fencing_token,
                deletion_jobs.c.version == version,
            )
            .values(
                heartbeat_at=now_ms,
                lease_expires_at=now_ms + lease_ttl_ms,
                version=version + 1,
            )
        )
        if int(result.rowcount or 0) != 1:
            return None
        return await self.get_for_tenant(tenant_id)

    async def purge_account(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        version: int,
    ) -> bool:
        scoped_tenant_id = self._scope_tenant(tenant_id)
        user_row = await self._conn.execute(
            select(users.c.email, users.c.status).where(users.c.id == scoped_tenant_id).limit(1)
        )
        current_user = user_row.mappings().first()
        current_job = await self.get_for_tenant(scoped_tenant_id)
        if current_user is None and current_job is None:
            return True
        if current_user is None or current_job is None:
            return False
        if current_user["status"] != "pending_purge":
            return False
        if (
            current_job.status != "running"
            or current_job.worker_id != worker_id
            or current_job.fencing_token != fencing_token
            or current_job.version != version
        ):
            return False

        retained_email = str(current_user["email"])
        now_ms = self._dialect.db_now_ms()

        await self._conn.execute(
            update(users)
            .where(users.c.id == scoped_tenant_id)
            .values(default_workspace_id=None, updated_at=now_ms)
        )
        await self._conn.execute(
            delete(execution_checkpoints).where(execution_checkpoints.c.tenant_id == scoped_tenant_id)
        )
        await self._conn.execute(delete(audit_logs).where(audit_logs.c.tenant_id == scoped_tenant_id))
        await self._conn.execute(delete(tool_executions).where(tool_executions.c.tenant_id == scoped_tenant_id))
        await self._conn.execute(delete(approval_requests).where(approval_requests.c.tenant_id == scoped_tenant_id))
        await self._conn.execute(delete(agent_runs).where(agent_runs.c.tenant_id == scoped_tenant_id))
        await self._conn.execute(delete(memory_entries).where(memory_entries.c.tenant_id == scoped_tenant_id))
        await self._conn.execute(delete(chat_sessions).where(chat_sessions.c.tenant_id == scoped_tenant_id))
        await self._conn.execute(delete(user_secrets).where(user_secrets.c.tenant_id == scoped_tenant_id))
        await self._conn.execute(delete(workspaces).where(workspaces.c.tenant_id == scoped_tenant_id))
        await self._conn.execute(
            delete(verification_codes).where(
                verification_codes.c.email == retained_email,
                verification_codes.c.expires_at > self._dialect.db_now_ms(),
            )
        )
        await self._conn.execute(delete(deletion_jobs).where(deletion_jobs.c.tenant_id == scoped_tenant_id))
        await self._conn.execute(delete(users).where(users.c.id == scoped_tenant_id))
        return True


async def next_claimable_tenant_id(connection: AsyncConnection, dialect: Dialect) -> str | None:
    predicate: ColumnElement[bool] = (
        (
            (deletion_jobs.c.status == "scheduled")
            & (deletion_jobs.c.purge_after <= dialect.db_now_ms())
        )
        | (
            (deletion_jobs.c.status == "running")
            & deletion_jobs.c.lease_expires_at.is_not(None)
            & (deletion_jobs.c.lease_expires_at <= dialect.db_now_ms())
        )
    )
    row = (
        await connection.execute(
            select(deletion_jobs.c.tenant_id)
            .where(predicate)
            .order_by(deletion_jobs.c.purge_after.asc(), deletion_jobs.c.requested_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return None if row is None else str(row)
