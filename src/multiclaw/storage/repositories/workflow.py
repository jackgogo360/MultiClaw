from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, exists, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement

from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect
from multiclaw.storage.schema import (
    agent_runs,
    approval_requests,
    execution_checkpoints,
    tool_executions,
    users,
)
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.models import (
    ApprovalRecord,
    ApprovalStatus,
    CheckpointPhase,
    CheckpointRecord,
    ExecutionRecord,
    ExecutionRecoveryRecord,
    ExecutionStatus,
    InvalidTransitionError,
    LEGAL_EXECUTION_TRANSITIONS,
    LEGAL_RUN_TRANSITIONS,
    RecoveryStrategy,
    TERMINAL_EXECUTION_STATUSES,
    RunLease,
    RunRecord,
    RunStatus,
    VersionConflictError,
    WorkflowRuntimeCounters,
)


Dialect = SQLiteDialect | MySQLDialect
ACTIVE_RUN_STATUSES = (RunStatus.RUNNING.value, RunStatus.AWAITING_USER.value, RunStatus.RESUMING.value)
TAKEOVER_ELIGIBLE_STATUSES = {RunStatus.RUNNING, RunStatus.AWAITING_USER, RunStatus.RESUMING}
NONTERMINAL_EXECUTION_STATUSES = tuple(
    status.value
    for status in ExecutionStatus
    if status not in TERMINAL_EXECUTION_STATUSES
)


def current_lease_predicate(lease: RunLease, dialect: Dialect) -> ColumnElement[bool]:
    return and_(
        agent_runs.c.tenant_id == lease.context.tenant_id,
        agent_runs.c.workspace_id == lease.context.workspace_id,
        agent_runs.c.session_id == lease.context.session_id,
        agent_runs.c.run_id == lease.context.run_id,
        agent_runs.c.lease_owner == lease.lease_owner,
        agent_runs.c.fencing_token == lease.fencing_token,
        agent_runs.c.version == lease.version,
        agent_runs.c.lease_expires_at > dialect.db_now_ms(),
    )


def _context_predicate(context: TenantContext) -> ColumnElement[bool]:
    return and_(
        agent_runs.c.tenant_id == context.tenant_id,
        agent_runs.c.workspace_id == context.workspace_id,
        agent_runs.c.session_id == context.session_id,
        agent_runs.c.run_id == context.run_id,
    )


def _approval_scope_predicate(context: TenantContext, approval_id: str) -> ColumnElement[bool]:
    return and_(
        approval_requests.c.tenant_id == context.tenant_id,
        approval_requests.c.workspace_id == context.workspace_id,
        approval_requests.c.session_id == context.session_id,
        approval_requests.c.run_id == context.run_id,
        approval_requests.c.approval_id == approval_id,
    )


def _checkpoint_scope_predicate(context: TenantContext) -> ColumnElement[bool]:
    return and_(
        execution_checkpoints.c.tenant_id == context.tenant_id,
        execution_checkpoints.c.workspace_id == context.workspace_id,
        execution_checkpoints.c.session_id == context.session_id,
        execution_checkpoints.c.run_id == context.run_id,
    )


@dataclass(slots=True)
class WorkflowRepository:
    _conn: AsyncConnection
    _dialect: Dialect
    _heartbeat_ms: int
    _lease_ttl_ms: int

    async def _lock_tenant(self, tenant_id: str) -> None:
        if self._dialect.name != "mysql":
            return
        await self._conn.execute(
            select(users.c.id).where(users.c.id == tenant_id).with_for_update()
        )

    async def get_run(self, context: TenantContext) -> RunRecord | None:
        result = await self._conn.execute(
            select(
                agent_runs.c.tenant_id,
                agent_runs.c.workspace_id,
                agent_runs.c.session_id,
                agent_runs.c.run_id,
                agent_runs.c.run_status,
                agent_runs.c.runtime_instance_id,
                agent_runs.c.lease_owner,
                agent_runs.c.fencing_token,
                agent_runs.c.lease_expires_at,
                agent_runs.c.heartbeat_at,
                agent_runs.c.version,
                agent_runs.c.created_at,
                agent_runs.c.updated_at,
                agent_runs.c.finished_at,
            )
            .where(_context_predicate(context))
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._run_from_row(row)

    async def count_active_runs(self, tenant_id: str) -> int:
        result = await self._conn.execute(
            select(func.count())
            .select_from(agent_runs)
            .where(
                agent_runs.c.tenant_id == tenant_id,
                agent_runs.c.run_status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        return int(result.scalar_one())

    async def get_runtime_counters(self, context: TenantContext) -> WorkflowRuntimeCounters:
        scope = and_(
            agent_runs.c.tenant_id == context.tenant_id,
            agent_runs.c.workspace_id == context.workspace_id,
        )
        active_run_count = await self._conn.scalar(
            select(func.count())
            .select_from(agent_runs)
            .where(agent_runs.c.run_status.in_(ACTIVE_RUN_STATUSES), scope)
        )
        active_executing_run_count = await self._conn.scalar(
            select(func.count())
            .select_from(agent_runs)
            .where(
                scope,
                agent_runs.c.run_status.in_((RunStatus.RUNNING.value, RunStatus.RESUMING.value)),
            )
        )
        awaiting_user_run_count = await self._conn.scalar(
            select(func.count())
            .select_from(agent_runs)
            .where(scope, agent_runs.c.run_status == RunStatus.AWAITING_USER.value)
        )
        checkpointed_awaiting_user_run_count = await self._conn.scalar(
            select(func.count())
            .select_from(agent_runs)
            .where(
                scope,
                agent_runs.c.run_status == RunStatus.AWAITING_USER.value,
                exists(
                    select(1)
                    .select_from(execution_checkpoints)
                    .where(
                        execution_checkpoints.c.tenant_id == agent_runs.c.tenant_id,
                        execution_checkpoints.c.workspace_id == agent_runs.c.workspace_id,
                        execution_checkpoints.c.session_id == agent_runs.c.session_id,
                        execution_checkpoints.c.run_id == agent_runs.c.run_id,
                    )
                ),
            )
        )
        return WorkflowRuntimeCounters(
            active_run_count=int(active_run_count or 0),
            active_executing_run_count=int(active_executing_run_count or 0),
            awaiting_user_run_count=int(awaiting_user_run_count or 0),
            checkpointed_awaiting_user_run_count=int(checkpointed_awaiting_user_run_count or 0),
        )

    async def _create_run(
        self,
        context: TenantContext,
        *,
        runtime_instance_id: str,
    ) -> RunLease:
        now_ms = self._dialect.db_now_ms()
        await self._conn.execute(
            insert(agent_runs).values(
                run_id=context.run_id,
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                run_status=RunStatus.RUNNING.value,
                runtime_instance_id=runtime_instance_id,
                lease_owner=runtime_instance_id,
                fencing_token=1,
                lease_expires_at=now_ms + self._lease_ttl_ms,
                heartbeat_at=now_ms,
                schema_version=1,
                version=1,
                created_at=now_ms,
                updated_at=now_ms,
                finished_at=None,
            )
        )
        return await self.require_lease(context, runtime_instance_id, 1, 1)

    async def require_lease(
        self,
        context: TenantContext,
        lease_owner: str,
        fencing_token: int,
        version: int,
    ) -> RunLease:
        result = await self._conn.execute(
            select(
                agent_runs.c.lease_owner,
                agent_runs.c.fencing_token,
                agent_runs.c.version,
                agent_runs.c.lease_expires_at,
            )
            .where(
                agent_runs.c.tenant_id == context.tenant_id,
                agent_runs.c.workspace_id == context.workspace_id,
                agent_runs.c.session_id == context.session_id,
                agent_runs.c.run_id == context.run_id,
                agent_runs.c.lease_owner == lease_owner,
                agent_runs.c.fencing_token == fencing_token,
                agent_runs.c.version == version,
            )
            .limit(1)
        )
        row = result.mappings().one()
        return RunLease(
            context=context,
            lease_owner=str(row["lease_owner"]),
            fencing_token=int(row["fencing_token"]),
            version=int(row["version"]),
            lease_expires_at=int(row["lease_expires_at"]),
        )

    async def _take_over_run(
        self,
        context: TenantContext,
        *,
        runtime_instance_id: str,
    ) -> RunLease | None:
        await self._dialect.lock_run(self._conn, context)
        current = await self.get_run(context)
        if current is None:
            return None
        if current.status not in TAKEOVER_ELIGIBLE_STATUSES:
            raise InvalidTransitionError(
                f"cannot acquire terminal run: {current.status.value}"
            )
        if current.lease_expires_at is None:
            return None

        now_ms = self._dialect.db_now_ms()
        result = await self._conn.execute(
            update(agent_runs)
            .where(
                _context_predicate(context),
                agent_runs.c.lease_expires_at <= now_ms,
            )
            .values(
                runtime_instance_id=runtime_instance_id,
                lease_owner=runtime_instance_id,
                fencing_token=agent_runs.c.fencing_token + 1,
                version=agent_runs.c.version + 1,
                heartbeat_at=now_ms,
                lease_expires_at=now_ms + self._lease_ttl_ms,
                updated_at=now_ms,
            )
        )
        if result.rowcount != 1:
            return None
        return await self.require_lease(
            context,
            runtime_instance_id,
            current.fencing_token + 1,
            current.version + 1,
        )

    async def _resume_waiting_run(
        self,
        context: TenantContext,
        *,
        runtime_instance_id: str,
    ) -> RunLease | None:
        await self._dialect.lock_run(self._conn, context)
        current = await self.get_run(context)
        if current is None or current.status is not RunStatus.AWAITING_USER:
            return None

        now_ms = self._dialect.db_now_ms()
        result = await self._conn.execute(
            update(agent_runs)
            .where(_context_predicate(context), agent_runs.c.run_status == RunStatus.AWAITING_USER.value)
            .values(
                run_status=RunStatus.RESUMING.value,
                runtime_instance_id=runtime_instance_id,
                lease_owner=runtime_instance_id,
                fencing_token=agent_runs.c.fencing_token + 1,
                version=agent_runs.c.version + 1,
                heartbeat_at=now_ms,
                lease_expires_at=now_ms + self._lease_ttl_ms,
                updated_at=now_ms,
            )
        )
        if result.rowcount != 1:
            return None
        return await self.require_lease(
            context,
            runtime_instance_id,
            current.fencing_token + 1,
            current.version + 1,
        )

    async def _refresh_lease(self, lease: RunLease) -> RunLease | None:
        now_ms = self._dialect.db_now_ms()
        result = await self._conn.execute(
            update(agent_runs)
            .where(current_lease_predicate(lease, self._dialect))
            .values(
                heartbeat_at=now_ms,
                lease_expires_at=now_ms + self._lease_ttl_ms,
                updated_at=now_ms,
                version=agent_runs.c.version + 1,
            )
        )
        if result.rowcount != 1:
            return None
        return await self.require_lease(
            lease.context,
            lease.lease_owner,
            lease.fencing_token,
            lease.version + 1,
        )

    async def _transition_run(self, lease: RunLease, target: RunStatus) -> RunLease | None:
        await self._dialect.lock_run(self._conn, lease.context)
        current = await self.get_run(lease.context)
        if current is None:
            return None
        self._validate_run_transition(current, lease, target)

        now_ms = self._dialect.db_now_ms()
        values = {
            "run_status": target.value,
            "updated_at": now_ms,
            "heartbeat_at": now_ms,
            "lease_expires_at": now_ms + self._lease_ttl_ms,
            "version": agent_runs.c.version + 1,
        }
        if target in {
            RunStatus.COMPLETED,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
            RunStatus.BLOCKED_CORRUPT,
            RunStatus.BLOCKED_INCOMPATIBLE,
        }:
            values["finished_at"] = now_ms

        result = await self._conn.execute(
            update(agent_runs)
            .where(current_lease_predicate(lease, self._dialect))
            .values(**values)
        )
        if result.rowcount != 1:
            return None
        return await self.require_lease(
            lease.context,
            lease.lease_owner,
            lease.fencing_token,
            lease.version + 1,
        )

    async def has_nonterminal_execution(self, context: TenantContext) -> bool:
        result = await self._conn.execute(
            select(func.count())
            .select_from(tool_executions)
            .where(
                tool_executions.c.tenant_id == context.tenant_id,
                tool_executions.c.workspace_id == context.workspace_id,
                tool_executions.c.session_id == context.session_id,
                tool_executions.c.run_id == context.run_id,
                tool_executions.c.execution_status.not_in(
                    tuple(status.value for status in TERMINAL_EXECUTION_STATUSES)
                ),
            )
        )
        return int(result.scalar_one()) > 0

    async def get_execution(
        self,
        context: TenantContext,
        execution_id: str,
    ) -> ExecutionRecord | None:
        result = await self._conn.execute(
            select(
                tool_executions.c.execution_id,
                tool_executions.c.approval_id,
                tool_executions.c.tool_call_id,
                tool_executions.c.tool_name,
                tool_executions.c.tool_kind,
                tool_executions.c.execution_status,
                tool_executions.c.recovery_strategy,
                tool_executions.c.version,
                tool_executions.c.created_at,
                tool_executions.c.updated_at,
                tool_executions.c.finished_at,
            )
            .where(
                tool_executions.c.tenant_id == context.tenant_id,
                tool_executions.c.workspace_id == context.workspace_id,
                tool_executions.c.session_id == context.session_id,
                tool_executions.c.run_id == context.run_id,
                tool_executions.c.execution_id == execution_id,
            )
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._execution_from_row(context, row)

    async def get_execution_recovery(
        self,
        context: TenantContext,
        execution_id: str,
    ) -> ExecutionRecoveryRecord | None:
        result = await self._conn.execute(
            select(
                tool_executions.c.execution_id,
                tool_executions.c.approval_id,
                tool_executions.c.tool_call_id,
                tool_executions.c.tool_name,
                tool_executions.c.tool_kind,
                tool_executions.c.execution_status,
                tool_executions.c.recovery_strategy,
                tool_executions.c.idempotency_key,
                tool_executions.c.input_payload_json,
                tool_executions.c.input_hash,
                tool_executions.c.external_request_id,
                tool_executions.c.result_ref,
                tool_executions.c.result_digest,
                tool_executions.c.version,
            )
            .where(
                tool_executions.c.tenant_id == context.tenant_id,
                tool_executions.c.workspace_id == context.workspace_id,
                tool_executions.c.session_id == context.session_id,
                tool_executions.c.run_id == context.run_id,
                tool_executions.c.execution_id == execution_id,
            )
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._execution_recovery_from_row(context, row)

    async def get_execution_by_tool_call_id(
        self,
        context: TenantContext,
        tool_call_id: str,
    ) -> ExecutionRecoveryRecord | None:
        result = await self._conn.execute(
            select(
                tool_executions.c.execution_id,
                tool_executions.c.approval_id,
                tool_executions.c.tool_call_id,
                tool_executions.c.tool_name,
                tool_executions.c.tool_kind,
                tool_executions.c.execution_status,
                tool_executions.c.recovery_strategy,
                tool_executions.c.idempotency_key,
                tool_executions.c.input_payload_json,
                tool_executions.c.input_hash,
                tool_executions.c.external_request_id,
                tool_executions.c.result_ref,
                tool_executions.c.result_digest,
                tool_executions.c.version,
            )
            .where(
                tool_executions.c.tenant_id == context.tenant_id,
                tool_executions.c.workspace_id == context.workspace_id,
                tool_executions.c.session_id == context.session_id,
                tool_executions.c.run_id == context.run_id,
                tool_executions.c.tool_call_id == tool_call_id,
            )
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._execution_recovery_from_row(context, row)

    async def get_execution_by_approval_id(
        self,
        context: TenantContext,
        approval_id: str,
    ) -> ExecutionRecoveryRecord | None:
        result = await self._conn.execute(
            select(
                tool_executions.c.execution_id,
                tool_executions.c.approval_id,
                tool_executions.c.tool_call_id,
                tool_executions.c.tool_name,
                tool_executions.c.tool_kind,
                tool_executions.c.execution_status,
                tool_executions.c.recovery_strategy,
                tool_executions.c.idempotency_key,
                tool_executions.c.input_payload_json,
                tool_executions.c.input_hash,
                tool_executions.c.external_request_id,
                tool_executions.c.result_ref,
                tool_executions.c.result_digest,
                tool_executions.c.version,
            )
            .where(
                tool_executions.c.tenant_id == context.tenant_id,
                tool_executions.c.workspace_id == context.workspace_id,
                tool_executions.c.session_id == context.session_id,
                tool_executions.c.run_id == context.run_id,
                tool_executions.c.approval_id == approval_id,
            )
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._execution_recovery_from_row(context, row)

    async def get_approval(self, context: TenantContext, approval_id: str) -> ApprovalRecord | None:
        result = await self._conn.execute(
            select(
                approval_requests.c.approval_id,
                approval_requests.c.tool_call_id,
                approval_requests.c.approval_status,
                approval_requests.c.requested_at,
                approval_requests.c.resolved_at,
                approval_requests.c.expires_at,
                approval_requests.c.version,
            )
            .where(_approval_scope_predicate(context, approval_id))
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._approval_from_row(context, row)

    async def get_latest_checkpoint(self, context: TenantContext) -> CheckpointRecord | None:
        result = await self._conn.execute(
            select(
                execution_checkpoints.c.checkpoint_id,
                execution_checkpoints.c.tenant_id,
                execution_checkpoints.c.workspace_id,
                execution_checkpoints.c.session_id,
                execution_checkpoints.c.run_id,
                execution_checkpoints.c.approval_id,
                execution_checkpoints.c.execution_id,
                execution_checkpoints.c.phase,
                execution_checkpoints.c.checkpoint_seq,
                execution_checkpoints.c.payload_json,
                execution_checkpoints.c.payload_hash,
                execution_checkpoints.c.schema_version,
                execution_checkpoints.c.created_at,
            )
            .where(_checkpoint_scope_predicate(context))
            .order_by(execution_checkpoints.c.checkpoint_seq.desc())
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._checkpoint_from_row(row)

    async def get_next_checkpoint_seq(self, context: TenantContext) -> int:
        max_seq = await self._conn.scalar(
            select(func.max(execution_checkpoints.c.checkpoint_seq)).where(
                _checkpoint_scope_predicate(context)
            )
        )
        return int(max_seq or 0) + 1

    async def get_live_lease(self, context: TenantContext, lease_owner: str) -> RunLease | None:
        result = await self._conn.execute(
            select(
                agent_runs.c.lease_owner,
                agent_runs.c.fencing_token,
                agent_runs.c.version,
                agent_runs.c.lease_expires_at,
            )
            .where(
                _context_predicate(context),
                agent_runs.c.lease_owner == lease_owner,
                agent_runs.c.lease_expires_at.is_not(None),
                agent_runs.c.lease_expires_at > self._dialect.db_now_ms(),
            )
            .limit(1)
        )
        row = result.mappings().first()
        if row is None:
            return None
        return RunLease(
            context=context,
            lease_owner=str(row["lease_owner"]),
            fencing_token=int(row["fencing_token"]),
            version=int(row["version"]),
            lease_expires_at=int(row["lease_expires_at"]),
        )

    async def _mark_approval_expired(self, context: TenantContext, approval_id: str, version: int) -> bool:
        now_ms = self._dialect.db_now_ms()
        result = await self._conn.execute(
            update(approval_requests)
            .where(
                _approval_scope_predicate(context, approval_id),
                approval_requests.c.approval_status == ApprovalStatus.AWAITING_USER.value,
                approval_requests.c.version == version,
                approval_requests.c.expires_at <= now_ms,
            )
            .values(
                approval_status=ApprovalStatus.EXPIRED.value,
                resolved_at=now_ms,
                version=approval_requests.c.version + 1,
            )
        )
        return result.rowcount == 1

    async def _resolve_approval(
        self,
        context: TenantContext,
        approval_id: str,
        *,
        approved: bool,
        version: int,
    ) -> ApprovalRecord | None:
        target = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        now_ms = self._dialect.db_now_ms()
        result = await self._conn.execute(
            update(approval_requests)
            .where(
                _approval_scope_predicate(context, approval_id),
                approval_requests.c.approval_status == ApprovalStatus.AWAITING_USER.value,
                approval_requests.c.version == version,
                approval_requests.c.expires_at > now_ms,
            )
            .values(
                approval_status=target.value,
                resolved_at=now_ms,
                version=approval_requests.c.version + 1,
            )
        )
        if result.rowcount != 1:
            return None
        resolved = await self.get_approval(context, approval_id)
        assert resolved is not None
        return resolved

    async def _insert_approval(
        self,
        lease: RunLease,
        *,
        approval_id: str,
        tool_call_id: str,
        expires_at: int,
    ) -> ApprovalRecord | None:
        await self._dialect.lock_run(self._conn, lease.context)
        if not await self._has_current_lease(lease):
            return None

        now_ms = self._dialect.db_now_ms()
        await self._conn.execute(
            insert(approval_requests).values(
                approval_id=approval_id,
                tenant_id=lease.context.tenant_id,
                workspace_id=lease.context.workspace_id,
                session_id=lease.context.session_id,
                run_id=lease.context.run_id,
                tool_call_id=tool_call_id,
                approval_status=ApprovalStatus.AWAITING_USER.value,
                requested_at=now_ms,
                resolved_at=None,
                expires_at=expires_at,
                version=1,
            )
        )
        inserted = await self.get_approval(lease.context, approval_id)
        assert inserted is not None
        return inserted

    async def _insert_execution(
        self,
        lease: RunLease,
        *,
        execution_id: str,
        approval_id: str | None,
        tool_call_id: str,
        tool_name: str,
        tool_kind: str,
        recovery_strategy: RecoveryStrategy,
        idempotency_key: str | None,
        input_payload_json: str,
        input_hash: str,
        status: ExecutionStatus = ExecutionStatus.NOT_STARTED,
    ) -> ExecutionRecord | None:
        await self._dialect.lock_run(self._conn, lease.context)
        if not await self._has_current_lease(lease):
            return None

        now_ms = self._dialect.db_now_ms()
        selectable = select(
            literal(execution_id),
            literal(lease.context.tenant_id),
            literal(lease.context.workspace_id),
            literal(lease.context.session_id),
            literal(lease.context.run_id),
            literal(approval_id),
            literal(tool_call_id),
            literal(tool_name),
            literal(tool_kind),
            literal(status.value),
            literal(recovery_strategy.value),
            literal(idempotency_key),
            literal(input_payload_json),
            literal(input_hash),
            literal(None),
            literal(None),
            literal(None),
            literal(1),
            literal(1),
            now_ms,
            now_ms,
            literal(None),
        ).where(
            ~exists(
                select(1)
                .select_from(tool_executions)
                .where(
                    tool_executions.c.tenant_id == lease.context.tenant_id,
                    tool_executions.c.workspace_id == lease.context.workspace_id,
                    tool_executions.c.session_id == lease.context.session_id,
                    tool_executions.c.run_id == lease.context.run_id,
                    tool_executions.c.execution_status.in_(NONTERMINAL_EXECUTION_STATUSES),
                )
            )
        )

        result = await self._conn.execute(
            insert(tool_executions).from_select(
                [
                    tool_executions.c.execution_id,
                    tool_executions.c.tenant_id,
                    tool_executions.c.workspace_id,
                    tool_executions.c.session_id,
                    tool_executions.c.run_id,
                    tool_executions.c.approval_id,
                    tool_executions.c.tool_call_id,
                    tool_executions.c.tool_name,
                    tool_executions.c.tool_kind,
                    tool_executions.c.execution_status,
                    tool_executions.c.recovery_strategy,
                    tool_executions.c.idempotency_key,
                    tool_executions.c.input_payload_json,
                    tool_executions.c.input_hash,
                    tool_executions.c.external_request_id,
                    tool_executions.c.result_ref,
                    tool_executions.c.result_digest,
                    tool_executions.c.schema_version,
                    tool_executions.c.version,
                    tool_executions.c.created_at,
                    tool_executions.c.updated_at,
                    tool_executions.c.finished_at,
                ],
                selectable,
            )
        )
        if result.rowcount != 1:
            return None
        inserted = await self.get_execution(lease.context, execution_id)
        assert inserted is not None
        return inserted

    async def _transition_execution(
        self,
        lease: RunLease,
        execution_id: str,
        *,
        expected_status: ExecutionStatus,
        expected_version: int,
        target: ExecutionStatus,
    ) -> ExecutionRecord | None:
        allowed = LEGAL_EXECUTION_TRANSITIONS.get(expected_status, frozenset())
        if target not in allowed:
            raise InvalidTransitionError(
                f"illegal execution transition: {expected_status.value} -> {target.value}"
            )

        await self._dialect.lock_run(self._conn, lease.context)
        if not await self._has_current_lease(lease):
            return None

        now_ms = self._dialect.db_now_ms()
        values = {
            "execution_status": target.value,
            "updated_at": now_ms,
            "version": tool_executions.c.version + 1,
        }
        if target in TERMINAL_EXECUTION_STATUSES:
            values["finished_at"] = now_ms

        result = await self._conn.execute(
            update(tool_executions)
            .where(
                tool_executions.c.tenant_id == lease.context.tenant_id,
                tool_executions.c.workspace_id == lease.context.workspace_id,
                tool_executions.c.session_id == lease.context.session_id,
                tool_executions.c.run_id == lease.context.run_id,
                tool_executions.c.execution_id == execution_id,
                tool_executions.c.execution_status == expected_status.value,
                tool_executions.c.version == expected_version,
                exists(select(1).select_from(agent_runs).where(current_lease_predicate(lease, self._dialect))),
            )
            .values(**values)
        )
        if result.rowcount != 1:
            return None
        transitioned = await self.get_execution(lease.context, execution_id)
        assert transitioned is not None
        return transitioned

    async def _update_execution_metadata(
        self,
        lease: RunLease,
        execution_id: str,
        *,
        expected_status: ExecutionStatus,
        expected_version: int,
        external_request_id: str | None = None,
        result_ref: str | None = None,
        result_digest: str | None = None,
        target_status: ExecutionStatus | None = None,
    ) -> ExecutionRecoveryRecord | None:
        await self._dialect.lock_run(self._conn, lease.context)
        if not await self._has_current_lease(lease):
            return None

        now_ms = self._dialect.db_now_ms()
        values: dict[str, object] = {
            "updated_at": now_ms,
            "version": tool_executions.c.version + 1,
        }
        if external_request_id is not None:
            values["external_request_id"] = external_request_id
        if result_ref is not None:
            values["result_ref"] = result_ref
        if result_digest is not None:
            values["result_digest"] = result_digest
        if target_status is not None:
            allowed = LEGAL_EXECUTION_TRANSITIONS.get(expected_status, frozenset())
            if target_status not in allowed:
                raise InvalidTransitionError(
                    f"illegal execution transition: {expected_status.value} -> {target_status.value}"
                )
            values["execution_status"] = target_status.value
            if target_status in TERMINAL_EXECUTION_STATUSES:
                values["finished_at"] = now_ms

        result = await self._conn.execute(
            update(tool_executions)
            .where(
                tool_executions.c.tenant_id == lease.context.tenant_id,
                tool_executions.c.workspace_id == lease.context.workspace_id,
                tool_executions.c.session_id == lease.context.session_id,
                tool_executions.c.run_id == lease.context.run_id,
                tool_executions.c.execution_id == execution_id,
                tool_executions.c.execution_status == expected_status.value,
                tool_executions.c.version == expected_version,
                exists(select(1).select_from(agent_runs).where(current_lease_predicate(lease, self._dialect))),
                (tool_executions.c.external_request_id.is_(None) | (tool_executions.c.external_request_id == external_request_id))
                if external_request_id is not None
                else literal(True),
                (tool_executions.c.result_ref.is_(None) | (tool_executions.c.result_ref == result_ref))
                if result_ref is not None
                else literal(True),
                (tool_executions.c.result_digest.is_(None) | (tool_executions.c.result_digest == result_digest))
                if result_digest is not None
                else literal(True),
            )
            .values(**values)
        )
        if result.rowcount != 1:
            return None
        updated = await self.get_execution_recovery(lease.context, execution_id)
        assert updated is not None
        return updated

    async def _insert_checkpoint(
        self,
        lease: RunLease,
        *,
        checkpoint_id: str,
        checkpoint_seq: int | None,
        phase: str,
        payload_json: str,
        payload_hash: str,
        schema_version: int,
        approval_id: str | None = None,
        execution_id: str | None = None,
        expected_execution_status: ExecutionStatus | None = None,
        expected_execution_version: int | None = None,
    ) -> int | None:
        await self._dialect.lock_run(self._conn, lease.context)
        if not await self._has_current_lease(lease):
            return None
        if execution_id is not None:
            execution = await self.get_execution_recovery(lease.context, execution_id)
            if execution is None:
                raise VersionConflictError("execution record not found")
            if approval_id != execution.approval_id:
                raise VersionConflictError("execution approval scope conflict")
            if expected_execution_status is not None and execution.status is not expected_execution_status:
                raise VersionConflictError("execution status conflict")
            if expected_execution_version is not None and execution.version != expected_execution_version:
                raise VersionConflictError("execution version conflict")
        if checkpoint_seq is None:
            checkpoint_seq = await self.get_next_checkpoint_seq(lease.context)
        await self._conn.execute(
            insert(execution_checkpoints).values(
                checkpoint_id=checkpoint_id,
                tenant_id=lease.context.tenant_id,
                workspace_id=lease.context.workspace_id,
                session_id=lease.context.session_id,
                run_id=lease.context.run_id,
                approval_id=approval_id,
                execution_id=execution_id,
                phase=phase,
                checkpoint_seq=int(checkpoint_seq),
                payload_json=payload_json,
                payload_hash=payload_hash,
                schema_version=schema_version,
                created_at=self._dialect.db_now_ms(),
            )
        )
        return int(checkpoint_seq)

    async def _has_current_lease(self, lease: RunLease) -> bool:
        result = await self._conn.execute(
            select(agent_runs.c.run_id)
            .where(current_lease_predicate(lease, self._dialect))
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    def _validate_run_transition(self, current: RunRecord, lease: RunLease, target: RunStatus) -> None:
        if current.lease_owner != lease.lease_owner or current.fencing_token != lease.fencing_token:
            raise InvalidTransitionError("run lease no longer current")
        if current.version != lease.version:
            raise InvalidTransitionError("run version no longer current")
        source = current.status
        allowed = LEGAL_RUN_TRANSITIONS.get(source, frozenset())
        if target not in allowed:
            raise InvalidTransitionError(f"illegal run transition: {source.value} -> {target.value}")

    @staticmethod
    def _run_from_row(row) -> RunRecord:
        context = TenantContext(
            tenant_id=str(row["tenant_id"]),
            workspace_id=str(row["workspace_id"]),
            session_id=str(row["session_id"]),
            run_id=str(row["run_id"]),
        )
        return RunRecord(
            context=context,
            status=RunStatus(str(row["run_status"])),
            runtime_instance_id=None if row["runtime_instance_id"] is None else str(row["runtime_instance_id"]),
            lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
            fencing_token=int(row["fencing_token"]),
            lease_expires_at=None if row["lease_expires_at"] is None else int(row["lease_expires_at"]),
            heartbeat_at=None if row["heartbeat_at"] is None else int(row["heartbeat_at"]),
            version=int(row["version"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            finished_at=None if row["finished_at"] is None else int(row["finished_at"]),
        )

    @staticmethod
    def _approval_from_row(context: TenantContext, row) -> ApprovalRecord:
        return ApprovalRecord(
            context=context,
            approval_id=str(row["approval_id"]),
            tool_call_id=str(row["tool_call_id"]),
            status=ApprovalStatus(str(row["approval_status"])),
            requested_at=int(row["requested_at"]),
            resolved_at=None if row["resolved_at"] is None else int(row["resolved_at"]),
            expires_at=int(row["expires_at"]),
            version=int(row["version"]),
        )

    @staticmethod
    def _execution_from_row(context: TenantContext, row) -> ExecutionRecord:
        return ExecutionRecord(
            context=context,
            execution_id=str(row["execution_id"]),
            approval_id=None if row["approval_id"] is None else str(row["approval_id"]),
            tool_call_id=str(row["tool_call_id"]),
            tool_name=str(row["tool_name"]),
            tool_kind=str(row["tool_kind"]),
            status=ExecutionStatus(str(row["execution_status"])),
            recovery_strategy=RecoveryStrategy(str(row["recovery_strategy"])),
            version=int(row["version"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            finished_at=None if row["finished_at"] is None else int(row["finished_at"]),
        )

    @staticmethod
    def _execution_recovery_from_row(context: TenantContext, row) -> ExecutionRecoveryRecord:
        execution_id = str(row["execution_id"])
        return ExecutionRecoveryRecord(
            context=context,
            execution_id=execution_id,
            approval_id=None if row["approval_id"] is None else str(row["approval_id"]),
            tool_call_id=str(row["tool_call_id"]),
            tool_name=str(row["tool_name"]),
            tool_kind=str(row["tool_kind"]),
            status=ExecutionStatus(str(row["execution_status"])),
            recovery_strategy=RecoveryStrategy(str(row["recovery_strategy"])),
            idempotency_key=None if row["idempotency_key"] is None else str(row["idempotency_key"]),
            input_payload_json=str(row["input_payload_json"]),
            input_hash=str(row["input_hash"]),
            input_ref=f"tool_execution:{execution_id}:input_payload_json",
            external_request_id=None
            if row["external_request_id"] is None
            else str(row["external_request_id"]),
            result_ref=None if row["result_ref"] is None else str(row["result_ref"]),
            result_digest=None if row["result_digest"] is None else str(row["result_digest"]),
            version=int(row["version"]),
        )

    @staticmethod
    def _checkpoint_from_row(row) -> CheckpointRecord:
        return CheckpointRecord(
            checkpoint_id=str(row["checkpoint_id"]),
            tenant_id=str(row["tenant_id"]),
            workspace_id=str(row["workspace_id"]),
            session_id=str(row["session_id"]),
            run_id=str(row["run_id"]),
            approval_id=None if row["approval_id"] is None else str(row["approval_id"]),
            execution_id=None if row["execution_id"] is None else str(row["execution_id"]),
            phase=str(row["phase"]),
            checkpoint_seq=int(row["checkpoint_seq"]),
            payload_json=str(row["payload_json"]),
            payload_hash=str(row["payload_hash"]),
            schema_version=int(row["schema_version"]),
            created_at=int(row["created_at"]),
        )
