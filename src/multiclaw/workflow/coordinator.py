from __future__ import annotations

import copy
import hashlib
from contextlib import asynccontextmanager
from typing import cast
from uuid import uuid4

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import AsyncConnection

from multiclaw.config import Settings
from multiclaw.events import EventRouter, ScopedEvent
from multiclaw.planner.models import PlanReference
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.plans import PlanRepository
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.models import (
    ApprovalRecord,
    AwaitingApprovalPayload,
    CheckpointPayload,
    CheckpointPhase,
    CheckpointRecord,
    CheckpointWrite,
    ExecutionDispatchingPayload,
    ExecutionRecord,
    ExecutionResultObservedPayload,
    ExecutionStatus,
    InvalidTransitionError,
    LeaseConflictError,
    PlanAwaitingApprovalPayload,
    RecoveryStrategy,
    RunLease,
    RunRecord,
    RunStartedPayload,
    RunStatus,
    StaleFenceError,
    TenantRunQuotaError,
    VersionConflictError,
    WorkflowRuntimeCounters,
)

PHASE_ALLOWED_EXECUTION_STATUSES: dict[CheckpointPhase, frozenset[ExecutionStatus]] = {
    CheckpointPhase.EXECUTION_DISPATCHING: frozenset(
        {
            ExecutionStatus.REPLAYING,
            ExecutionStatus.EXECUTING,
        }
    ),
    CheckpointPhase.EXECUTION_RESULT_OBSERVED: frozenset(
        {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED_RETRYABLE,
            ExecutionStatus.FAILED_TERMINAL,
            ExecutionStatus.UNCERTAIN,
            ExecutionStatus.BLOCKED_INCOMPATIBLE,
            ExecutionStatus.BLOCKED_CORRUPT,
        }
    ),
}


class PostCommitEventQueue:
    """Explicit post-commit event handoff for callers that own a transaction.

    The transaction owner must call ``publish`` only after its transaction has
    committed, or ``discard`` after rollback. Draining before publication makes
    repeated post-commit handling idempotent.
    """

    def __init__(self) -> None:
        self._events: list[ScopedEvent] = []
        self._connection: AsyncConnection | None = None

    @property
    def pending_count(self) -> int:
        return len(self._events)

    def defer(self, event: ScopedEvent) -> None:
        self._events.append(event)

    def bind(self, connection: AsyncConnection) -> None:
        if self._connection is not None and self._connection is not connection:
            raise ValueError("PostCommitEventQueue is already bound to another transaction")
        if self._connection is connection:
            return

        self._connection = connection

        def discard_on_rollback(_connection) -> None:
            self.discard()

        sqlalchemy_event.listen(connection.sync_connection, "rollback", discard_on_rollback)

    def discard(self) -> None:
        self._events.clear()

    async def publish(self, event_router: EventRouter) -> None:
        if self._connection is not None and self._connection.in_transaction():
            raise RuntimeError("post-commit events cannot publish before the transaction is committed")
        events, self._events = self._events, []
        for event in events:
            await event_router.publish(event)


class WorkflowCoordinator:
    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        connection: AsyncConnection | None = None,
        event_router: EventRouter | None = None,
        post_commit_events: PostCommitEventQueue | None = None,
    ) -> None:
        self._database = database
        self._settings = settings or Settings(_config_file="/nonexistent")
        self._connection = connection
        self._event_router = event_router
        self._post_commit_events = post_commit_events
        if connection is not None and post_commit_events is not None:
            post_commit_events.bind(connection)

    async def start_run(self, context: TenantContext, runtime_instance_id: str) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            await repository._lock_tenant(context.tenant_id)
            await self._enforce_run_quota(repository, context.tenant_id)
            return await repository._create_run(
                context,
                runtime_instance_id=runtime_instance_id,
            )

    async def start_run_with_checkpoint(self, context: TenantContext, runtime_instance_id: str) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            await repository._lock_tenant(context.tenant_id)
            await self._enforce_run_quota(repository, context.tenant_id)

            lease = await repository._create_run(
                context,
                runtime_instance_id=runtime_instance_id,
            )
            record = await repository.get_run(context)
            if record is None:
                raise RuntimeError("run record missing after creation")
            await self._scoped(conn).checkpoint(
                lease,
                CheckpointPhase.RUN_STARTED,
                {
                    "tenant_id": context.tenant_id,
                    "workspace_id": context.workspace_id,
                    "session_id": context.session_id,
                    "run_id": context.run_id,
                    "started_at_ms": record.created_at,
                    "model_cursor": self._run_started_cursor(context),
                    "cursor": self._run_started_cursor(context),
                },
                checkpoint_seq=1,
            )
            return lease

    async def start_plan_run_with_checkpoint(
        self,
        context: TenantContext,
        runtime_instance_id: str,
        *,
        plan_id: str,
        plan_version: int,
        plan_digest: str,
    ) -> RunLease:
        run_id = cast(str, context.run_id)
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            await repository._lock_tenant(context.tenant_id)
            if not await repository._plan_version_exists(
                context,
                plan_id,
                plan_version,
                content_digest=plan_digest,
            ):
                raise StaleFenceError("Plan version or digest is stale")
            await self._enforce_run_quota(repository, context.tenant_id)
            lease = await repository._create_run(
                context,
                runtime_instance_id=runtime_instance_id,
                status=RunStatus.AWAITING_USER,
                plan_id=plan_id,
                initial_plan_version=plan_version,
                active_plan_version=plan_version,
            )
            cursor = f"plan:{plan_id}:v{plan_version}:decision"
            await self._scoped(conn).checkpoint(
                lease,
                CheckpointPhase.PLAN_AWAITING_APPROVAL,
                PlanAwaitingApprovalPayload(
                    run_id=run_id,
                    plan_id=plan_id,
                    plan_version=plan_version,
                    plan_digest=plan_digest,
                    decision_cursor=cursor,
                    cursor=cursor,
                ),
                checkpoint_seq=1,
            )
            return lease

    async def acquire_run(self, context: TenantContext, runtime_instance_id: str) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            lease = await repository._take_over_run(
                context,
                runtime_instance_id=runtime_instance_id,
            )
            if lease is None:
                raise LeaseConflictError("run lease is still current")
            return lease

    async def resume_waiting_run(self, context: TenantContext, runtime_instance_id: str) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            lease = await repository._resume_waiting_run(
                context,
                runtime_instance_id=runtime_instance_id,
            )
            if lease is None:
                raise LeaseConflictError("awaiting_user run could not be resumed")
            return lease

    async def resume_waiting_plan_run(
        self,
        context: TenantContext,
        *,
        runtime_instance_id: str,
        plan_id: str,
        plan_version: int,
        expected_run_version: int,
    ) -> RunLease:
        async with self._write_connection() as conn:
            lease = await self._repository(conn)._resume_waiting_plan_run(
                context,
                runtime_instance_id=runtime_instance_id,
                plan_id=plan_id,
                plan_version=plan_version,
                expected_run_version=expected_run_version,
            )
            if lease is None:
                raise StaleFenceError("waiting Plan run fence is stale")
            return lease

    async def fence_waiting_plan_run(
        self,
        context: TenantContext,
        *,
        runtime_instance_id: str,
        plan_id: str,
        plan_version: int,
        expected_run_version: int,
        plan_digest: str,
    ) -> RunLease:
        run_id = cast(str, context.run_id)
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            lease = await repository._fence_waiting_plan_run(
                context,
                runtime_instance_id=runtime_instance_id,
                plan_id=plan_id,
                plan_version=plan_version,
                plan_digest=plan_digest,
                expected_run_version=expected_run_version,
            )
            if lease is None:
                raise StaleFenceError("waiting Plan run fence is stale")
            cursor = f"plan:{plan_id}:v{plan_version}:decision"
            await self._scoped(conn).checkpoint(
                lease,
                CheckpointPhase.PLAN_AWAITING_APPROVAL,
                PlanAwaitingApprovalPayload(
                    run_id=run_id,
                    plan_id=plan_id,
                    plan_version=plan_version,
                    plan_digest=plan_digest,
                    decision_cursor=cursor,
                    cursor=cursor,
                ),
                checkpoint_seq=await repository.get_next_checkpoint_seq(context),
            )
            return lease

    async def cancel_waiting_plan_run(
        self,
        context: TenantContext,
        *,
        runtime_instance_id: str,
        plan_id: str,
        plan_version: int,
        expected_run_version: int,
    ) -> RunLease:
        run_id = cast(str, context.run_id)
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            lease = await repository._cancel_waiting_plan_run(
                context,
                runtime_instance_id=runtime_instance_id,
                plan_id=plan_id,
                plan_version=plan_version,
                expected_run_version=expected_run_version,
            )
            if lease is None:
                raise StaleFenceError("waiting Plan run fence is stale")
            record = await repository.get_run(context)
            if record is None or record.finished_at is None:
                raise RuntimeError("cancelled Plan run missing finished_at")
            await self._scoped(conn).checkpoint(
                lease,
                CheckpointPhase.RUN_TERMINAL,
                {
                    "run_id": run_id,
                    "terminal_status": RunStatus.CANCELLED.value,
                    "finished_at_ms": record.finished_at,
                    "final_digest": self._terminal_digest(
                        run_id,
                        RunStatus.CANCELLED,
                        record.finished_at,
                    ),
                },
                checkpoint_seq=await repository.get_next_checkpoint_seq(context),
            )
            return lease

    async def request_cancellation(self, context: TenantContext) -> RunRecord:
        """Persist a cancellation request, terminalizing waiting runs atomically."""
        if (
            self._connection is not None
            and self._event_router is not None
            and self._post_commit_events is None
        ):
            raise ValueError(
                "externally owned cancellation transactions require a PostCommitEventQueue"
            )
        run_id = cast(str, context.run_id)
        post_commit_event: ScopedEvent | None = None
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            before = await repository.get_run(context)
            record = await repository._request_cancellation(context)
            if record is None:
                raise RuntimeError("run record missing for cancellation")
            if record.status is not RunStatus.CANCELLED or record.finished_at is None:
                return record

            latest = await repository.get_latest_checkpoint(context)
            if latest is not None:
                phase = CheckpointPhase(latest.phase)
                if phase is CheckpointPhase.RUN_TERMINAL:
                    return record
            lease = RunLease(
                context=context,
                lease_owner=record.lease_owner or "",
                fencing_token=record.fencing_token,
                version=record.version,
                lease_expires_at=record.lease_expires_at or 0,
            )
            await self._scoped(conn).checkpoint(
                lease,
                CheckpointPhase.RUN_TERMINAL,
                {
                    "run_id": run_id,
                    "terminal_status": RunStatus.CANCELLED.value,
                    "finished_at_ms": record.finished_at,
                    "final_digest": self._terminal_digest(
                        run_id,
                        RunStatus.CANCELLED,
                        record.finished_at,
                    ),
                },
                checkpoint_seq=await repository.get_next_checkpoint_seq(context),
            )
            if (
                before is not None
                and before.status is RunStatus.AWAITING_USER
                and record.plan_id is not None
            ):
                plan = await PlanRepository(
                    conn,
                    self._database.dialect,
                    context,
                    self._settings.planning,
                ).get(record.plan_id)
                if plan is not None:
                    assert context.session_id is not None and context.run_id is not None
                    data = PlanReference(
                        tenant_id=context.tenant_id,
                        workspace_id=context.workspace_id,
                        session_id=context.session_id,
                        run_id=context.run_id,
                        plan_id=plan.plan_id,
                        plan_version=plan.current_version,
                        aggregate_version=plan.aggregate_version,
                    ).model_dump(mode="json")
                    post_commit_event = ScopedEvent.from_context(
                        context,
                        "plan.run_status",
                        {**data, "status": RunStatus.CANCELLED.value},
                    )
        if post_commit_event is not None and self._event_router is not None:
            if self._connection is None:
                await self._event_router.publish(post_commit_event)
            else:
                assert self._post_commit_events is not None
                self._post_commit_events.defer(post_commit_event)
        return record

    async def raise_if_cancel_requested(self, context: TenantContext) -> None:
        run = await self.get_run(context)
        if run is None:
            from multiclaw.planner.models import PlanExecutionBlocked

            raise PlanExecutionBlocked("run is missing")
        if run.cancel_requested_at is not None or run.status is RunStatus.CANCELLED:
            from multiclaw.planner.models import PlanCancellationRequested

            raise PlanCancellationRequested

    async def heartbeat(self, lease: RunLease) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            refreshed = await repository._refresh_lease(lease)
            if refreshed is None:
                raise StaleFenceError("run lease is stale")
            return refreshed

    async def transition_run(self, lease: RunLease, target: RunStatus) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            transitioned = await repository._transition_run(lease, target)
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
            transitioned = await repository._transition_run(lease, target)
            if transitioned is None:
                raise StaleFenceError("run lease is stale")
            return transitioned

    async def finish_run_with_checkpoint(self, lease: RunLease, target: RunStatus) -> RunLease:
        if target not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
            RunStatus.BLOCKED_CORRUPT,
            RunStatus.BLOCKED_INCOMPATIBLE,
        }:
            raise InvalidTransitionError(f"{target.value} is not a terminal run status")

        run_id = cast(str, lease.context.run_id)
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            if target is RunStatus.COMPLETED and await repository.has_nonterminal_execution(lease.context):
                raise InvalidTransitionError("run cannot complete while executions are nonterminal")
            transitioned = await repository._transition_run(lease, target)
            if transitioned is None:
                raise StaleFenceError("run lease is stale")

            record = await repository.get_run(lease.context)
            if record is None or record.finished_at is None:
                raise RuntimeError("terminal run record missing finished_at")

            next_seq = await repository.get_next_checkpoint_seq(lease.context)
            await self._scoped(conn).checkpoint(
                transitioned,
                CheckpointPhase.RUN_TERMINAL,
                {
                    "run_id": run_id,
                    "terminal_status": target.value,
                    "finished_at_ms": record.finished_at,
                    "final_digest": self._terminal_digest(run_id, target, record.finished_at),
                },
                checkpoint_seq=next_seq,
            )
            return transitioned

    async def decide_approval(
        self,
        context: TenantContext,
        approval_id: str,
        *,
        approved: bool,
        version: int,
    ) -> ApprovalRecord:
        expired = False
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            resolved = await repository._resolve_approval(
                context,
                approval_id,
                approved=approved,
                version=version,
            )
            if resolved is not None:
                return resolved

            expired = await repository._mark_approval_expired(context, approval_id, version)
            if not expired:
                current = await repository.get_approval(context, approval_id)
                if current is None:
                    raise VersionConflictError("approval record not found")
                if current.status.value != "awaiting_user":
                    raise VersionConflictError("approval already resolved")
                if current.version != version:
                    raise VersionConflictError("approval version conflict")
                raise InvalidTransitionError("approval could not be resolved")

        if expired:
            raise InvalidTransitionError("approval expired")
        raise RuntimeError("approval decision reached an invalid state")

    async def create_approval(
        self,
        lease: RunLease,
        *,
        approval_id: str,
        tool_call_id: str,
        expires_at: int,
    ) -> ApprovalRecord:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            inserted = await repository._insert_approval(
                lease,
                approval_id=approval_id,
                tool_call_id=tool_call_id,
                expires_at=expires_at,
            )
            if inserted is None:
                raise StaleFenceError("run lease is stale")
            return inserted

    async def create_execution(
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
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            return await repository._insert_execution(
                lease,
                execution_id=execution_id,
                approval_id=approval_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_kind=tool_kind,
                recovery_strategy=recovery_strategy,
                idempotency_key=idempotency_key,
                input_payload_json=input_payload_json,
                input_hash=input_hash,
                status=status,
            )

    async def transition_execution(
        self,
        lease: RunLease,
        execution_id: str,
        *,
        expected_status: ExecutionStatus,
        expected_version: int,
        target: ExecutionStatus,
    ) -> RunLease:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            transitioned = await repository._transition_execution(
                lease,
                execution_id,
                expected_status=expected_status,
                expected_version=expected_version,
                target=target,
            )
            if transitioned is not None:
                return lease
            if not await repository._has_current_lease(lease):
                raise StaleFenceError("run lease is stale")
            raise VersionConflictError("execution version conflict")

    async def write_checkpoint(
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
        execution_expected_status: ExecutionStatus | None = None,
        execution_expected_version: int | None = None,
    ) -> int:
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            inserted_seq = await repository._insert_checkpoint(
                lease,
                checkpoint_id=checkpoint_id,
                checkpoint_seq=checkpoint_seq,
                phase=phase,
                payload_json=payload_json,
                payload_hash=payload_hash,
                schema_version=schema_version,
                approval_id=approval_id,
                execution_id=execution_id,
                expected_execution_status=execution_expected_status,
                expected_execution_version=execution_expected_version,
            )
            if inserted_seq is None:
                raise StaleFenceError("run lease is stale")
            return inserted_seq

    async def checkpoint(
        self,
        lease: RunLease,
        phase: CheckpointPhase | str,
        payload: CheckpointPayload | dict,
        *,
        checkpoint_id: str | None = None,
        checkpoint_seq: int | None = None,
        approval_id: str | None = None,
        execution_id: str | None = None,
        execution_expected_status: ExecutionStatus | None = None,
        execution_expected_version: int | None = None,
    ) -> CheckpointWrite:
        from multiclaw.workflow.recovery import (
            encode_checkpoint_payload,
            validate_phase_payload,
        )

        normalized_phase, validated_payload = validate_phase_payload(phase, payload)
        resolved_approval_id, resolved_execution_id = self._validate_checkpoint_scope(
            lease,
            normalized_phase,
            validated_payload,
            approval_id=approval_id,
            execution_id=execution_id,
            execution_expected_status=execution_expected_status,
            execution_expected_version=execution_expected_version,
        )
        encoded = encode_checkpoint_payload(
            validated_payload,
            max_bytes=self._settings.workflow.max_checkpoint_payload_bytes,
        )
        resolved_checkpoint_id = checkpoint_id or str(uuid4())
        persisted_checkpoint_seq = await self.write_checkpoint(
            lease,
            checkpoint_id=resolved_checkpoint_id,
            checkpoint_seq=checkpoint_seq,
            phase=normalized_phase.value,
            payload_json=encoded.payload_json,
            payload_hash=encoded.payload_hash,
            schema_version=encoded.schema_version,
            approval_id=resolved_approval_id,
            execution_id=resolved_execution_id,
            execution_expected_status=execution_expected_status,
            execution_expected_version=execution_expected_version,
        )
        checkpoint_seq = persisted_checkpoint_seq
        return CheckpointWrite(
            checkpoint_id=resolved_checkpoint_id,
            checkpoint_seq=checkpoint_seq,
            phase=normalized_phase,
            payload_json=encoded.payload_json,
            payload_hash=encoded.payload_hash,
            schema_version=encoded.schema_version,
        )

    async def get_run(self, context: TenantContext) -> RunRecord | None:
        async with self._write_connection() as conn:
            return await self._repository(conn).get_run(context)

    async def get_plan_run(
        self,
        context: TenantContext,
        plan_id: str,
    ) -> RunRecord | None:
        async with self._write_connection() as conn:
            return await self._repository(conn).get_plan_run(context, plan_id)

    async def get_execution(self, context: TenantContext, execution_id: str) -> ExecutionRecord | None:
        async with self._write_connection() as conn:
            return await self._repository(conn).get_execution(context, execution_id)

    async def get_execution_recovery(self, context: TenantContext, execution_id: str):
        async with self._write_connection() as conn:
            return await self._repository(conn).get_execution_recovery(context, execution_id)

    async def get_approval(self, context: TenantContext, approval_id: str):
        async with self._write_connection() as conn:
            return await self._repository(conn).get_approval(context, approval_id)

    async def get_execution_by_approval_id(self, context: TenantContext, approval_id: str):
        async with self._write_connection() as conn:
            return await self._repository(conn).get_execution_by_approval_id(context, approval_id)

    async def get_latest_checkpoint(self, context: TenantContext) -> CheckpointRecord | None:
        async with self._write_connection() as conn:
            return await self._repository(conn).get_latest_checkpoint(context)

    async def complete_execution(
        self,
        lease: RunLease,
        execution_id: str,
        *,
        expected_status: ExecutionStatus,
        expected_version: int,
        target_status: ExecutionStatus,
        external_request_id: str | None = None,
        result_ref: str | None = None,
        result_digest: str | None = None,
    ):
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            updated = await repository._update_execution_metadata(
                lease,
                execution_id,
                expected_status=expected_status,
                expected_version=expected_version,
                external_request_id=external_request_id,
                result_ref=result_ref,
                result_digest=result_digest,
                target_status=target_status,
            )
            if updated is not None:
                return updated
            if not await repository._has_current_lease(lease):
                raise StaleFenceError("run lease is stale")
            raise VersionConflictError("execution version conflict")

    async def record_external_request_id(
        self,
        lease: RunLease,
        execution_id: str,
        *,
        expected_status: ExecutionStatus,
        expected_version: int,
        external_request_id: str,
    ):
        async with self._write_connection() as conn:
            repository = self._repository(conn)
            updated = await repository._update_execution_metadata(
                lease,
                execution_id,
                expected_status=expected_status,
                expected_version=expected_version,
                external_request_id=external_request_id,
            )
            if updated is not None:
                return updated
            if not await repository._has_current_lease(lease):
                raise StaleFenceError("run lease is stale")
            raise VersionConflictError("execution version conflict")

    async def get_runtime_counters(self, context: TenantContext) -> WorkflowRuntimeCounters:
        async with self._write_connection() as conn:
            return await self._repository(conn).get_runtime_counters(context)

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

    async def _enforce_run_quota(
        self,
        repository: WorkflowRepository,
        tenant_id: str,
    ) -> None:
        active_runs = await repository.count_active_runs(tenant_id)
        if active_runs >= self._settings.runtime.max_concurrent_runs_per_tenant:
            raise TenantRunQuotaError("tenant run quota exceeded")

    def _scoped(self, conn) -> WorkflowCoordinator:
        scoped = copy.copy(self)
        scoped._connection = conn
        return scoped

    @staticmethod
    def _validate_checkpoint_scope(
        lease: RunLease,
        phase: CheckpointPhase,
        payload: CheckpointPayload,
        *,
        approval_id: str | None,
        execution_id: str | None,
        execution_expected_status: ExecutionStatus | None,
        execution_expected_version: int | None,
    ) -> tuple[str | None, str | None]:
        if cast(RunStartedPayload, payload).run_id != lease.context.run_id:
            raise InvalidTransitionError("checkpoint payload run_id does not match active run")

        if phase is CheckpointPhase.RUN_STARTED:
            run_started = cast(RunStartedPayload, payload)
            assert hasattr(run_started, "tenant_id")
            if run_started.tenant_id != lease.context.tenant_id:
                raise InvalidTransitionError("checkpoint payload tenant_id does not match active run")
            if run_started.workspace_id != lease.context.workspace_id:
                raise InvalidTransitionError("checkpoint payload workspace_id does not match active run")
            if run_started.session_id != lease.context.session_id:
                raise InvalidTransitionError("checkpoint payload session_id does not match active run")
            return None, None

        if phase is CheckpointPhase.AWAITING_APPROVAL:
            awaiting_approval = payload if isinstance(payload, AwaitingApprovalPayload) else None
            assert awaiting_approval is not None
            if execution_id is not None:
                raise InvalidTransitionError("awaiting_approval checkpoints cannot include execution_id")
            if approval_id is not None and approval_id != awaiting_approval.approval_id:
                raise InvalidTransitionError("checkpoint approval_id does not match payload")
            return awaiting_approval.approval_id, None

        if phase in {
            CheckpointPhase.EXECUTION_DISPATCHING,
            CheckpointPhase.EXECUTION_RESULT_OBSERVED,
        }:
            resolved_execution_id = (
                payload.execution_id
                if isinstance(payload, ExecutionDispatchingPayload | ExecutionResultObservedPayload)
                else None
            )
            if resolved_execution_id is None:
                raise InvalidTransitionError("execution checkpoint payload is missing execution_id")
            if execution_id is not None and execution_id != resolved_execution_id:
                raise InvalidTransitionError("checkpoint execution_id does not match payload")
            if execution_expected_status is not None and execution_expected_version is None:
                raise InvalidTransitionError("execution checkpoint version is required with status guard")
            if execution_expected_version is not None and execution_expected_status is None:
                raise InvalidTransitionError("execution checkpoint status is required with version guard")
            allowed_statuses = PHASE_ALLOWED_EXECUTION_STATUSES[phase]
            if execution_expected_status not in allowed_statuses:
                allowed = ", ".join(status.value for status in sorted(allowed_statuses, key=lambda item: item.value))
                raise InvalidTransitionError(
                    f"{phase.value} checkpoints require execution status in {{{allowed}}}"
                )
            return approval_id, resolved_execution_id

        return approval_id, execution_id

    @staticmethod
    def _run_started_cursor(context: TenantContext) -> str:
        return f"run:{context.run_id}:model_inference"

    @staticmethod
    def _terminal_digest(run_id: str, target: RunStatus, finished_at_ms: int) -> str:
        payload = f"{run_id}:{target.value}:{finished_at_ms}".encode()
        return hashlib.sha256(payload).hexdigest()
