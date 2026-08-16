from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, or_, select

from pydantic import ValidationError

from multiclaw.config import Settings
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.storage.schema import agent_runs, approval_requests
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.continuation import WorkflowContinuationService
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    ApprovalStatus,
    AwaitingApprovalPayload,
    CheckpointPayload,
    CheckpointPhase,
    CheckpointRecord,
    CheckpointTooLargeError,
    CorruptCheckpointError,
    ExecutionDispatchingPayload,
    ExecutionResultObservedPayload,
    ExecutionStatus,
    IncompatibleCheckpointError,
    InvalidTransitionError,
    ModelOutputPayload,
    PHASE_PAYLOADS,
    LeaseConflictError,
    RecoveryAction,
    RecoveryOutcome,
    RecoveryStrategy,
    RunLeaseHandle,
    TERMINAL_RUN_STATUSES,
    RunStartedPayload,
    RunRecord,
    RunStatus,
    RunTerminalPayload,
)


SECRET_KEY_MARKERS = {"secret", "token", "password", "apikey", "authorization"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EncodedCheckpointPayload:
    payload_json: str
    payload_hash: str
    schema_version: int


def canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def reject_secret_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = _normalize_key(key)
            if any(marker in normalized_key for marker in SECRET_KEY_MARKERS):
                raise CorruptCheckpointError(f"checkpoint payload contains forbidden key {key!r}")
            reject_secret_fields(item)
        return
    if isinstance(value, list):
        for item in value:
            reject_secret_fields(item)
        return
    if isinstance(value, tuple):
        for item in value:
            reject_secret_fields(item)


def encode_checkpoint_payload(
    payload: CheckpointPayload,
    *,
    max_bytes: int,
) -> EncodedCheckpointPayload:
    data = payload.model_dump(mode="json")
    reject_secret_fields(payload.model_dump(mode="python"))
    encoded = canonical_json(data)
    if len(encoded) > max_bytes:
        raise CheckpointTooLargeError(
            f"checkpoint payload exceeds {max_bytes} bytes ({len(encoded)} bytes)"
        )
    return EncodedCheckpointPayload(
        payload_json=encoded.decode("utf-8"),
        payload_hash=hashlib.sha256(encoded).hexdigest(),
        schema_version=payload.schema_version,
    )


def parse_phase(value: CheckpointPhase | str) -> CheckpointPhase:
    try:
        return value if isinstance(value, CheckpointPhase) else CheckpointPhase(str(value))
    except ValueError as error:
        raise IncompatibleCheckpointError(f"unsupported checkpoint phase {value!r}") from error


def validate_phase_payload(
    phase: CheckpointPhase | str,
    payload: CheckpointPayload | dict[str, Any],
) -> tuple[CheckpointPhase, CheckpointPayload]:
    normalized_phase = parse_phase(phase)
    payload_model = PHASE_PAYLOADS[normalized_phase]
    try:
        validated = payload if isinstance(payload, payload_model) else payload_model.model_validate(payload)
    except ValidationError as error:
        raise CorruptCheckpointError("checkpoint payload does not match phase schema") from error
    return normalized_phase, validated


def decode_checkpoint(
    checkpoint: CheckpointRecord,
) -> tuple[CheckpointPhase, CheckpointPayload]:
    if checkpoint.schema_version != 1:
        raise IncompatibleCheckpointError(
            f"unsupported checkpoint schema_version {checkpoint.schema_version}"
        )
    payload_data = _decode_json_object(checkpoint.payload_json)
    payload_schema_version = payload_data.get("schema_version")
    if payload_schema_version != checkpoint.schema_version:
        raise CorruptCheckpointError("checkpoint schema version does not match payload")
    phase = parse_phase(checkpoint.phase)
    try:
        payload = PHASE_PAYLOADS[phase].model_validate(payload_data)
    except ValidationError as error:
        raise CorruptCheckpointError("checkpoint payload does not match phase schema") from error
    encoded = canonical_json(payload.model_dump(mode="json"))
    actual_hash = hashlib.sha256(encoded).hexdigest()
    if actual_hash != checkpoint.payload_hash:
        raise CorruptCheckpointError("checkpoint payload hash mismatch")
    reject_secret_fields(payload.model_dump(mode="python"))
    _validate_checkpoint_scope(checkpoint, payload)
    return phase, payload


class RecoveryService:
    def __init__(self, database: Database, *, settings: Settings | None = None) -> None:
        self._database = database
        self._settings = settings or Settings(_config_file="/nonexistent")

    async def recover(self, context: TenantContext, runtime_instance_id: str) -> RecoveryOutcome:
        run = await self._run(context)
        if run is None:
            return RecoveryOutcome(
                status=RunStatus.BLOCKED_CORRUPT,
                executions_started=0,
                reason="missing run",
            )
        checkpoint = await self._latest_checkpoint(context)
        if checkpoint is None:
            return RecoveryOutcome(
                status=RunStatus.BLOCKED_CORRUPT,
                executions_started=0,
                reason="missing checkpoint",
            )

        try:
            phase, payload = decode_checkpoint(checkpoint)
            outcome = await self._classify(context, checkpoint, phase, payload)
        except IncompatibleCheckpointError as error:
            return RecoveryOutcome(
                status=RunStatus.BLOCKED_INCOMPATIBLE,
                executions_started=0,
                reason=str(error),
            )
        except CorruptCheckpointError as error:
            return RecoveryOutcome(
                status=RunStatus.BLOCKED_CORRUPT,
                executions_started=0,
                reason=str(error),
            )

        if run.status in TERMINAL_RUN_STATUSES:
            return RecoveryOutcome(
                action=RecoveryAction.TERMINAL_NOOP,
                status=run.status,
                executions_started=0,
            )

        if outcome.action is RecoveryAction.TERMINAL_NOOP:
            return outcome

        lease = await self._acquire_recovery_lease(context, runtime_instance_id)
        return RecoveryOutcome(
            action=outcome.action,
            status=outcome.status,
            lease=lease,
            execution_id=outcome.execution_id,
            executions_started=0,
            reason=outcome.reason,
        )

    async def validate_live_run(self, context: TenantContext) -> RecoveryOutcome:
        run = await self._run(context)
        if run is None:
            return RecoveryOutcome(
                status=RunStatus.BLOCKED_CORRUPT,
                executions_started=0,
                reason="missing run",
            )
        checkpoint = await self._latest_checkpoint(context)
        if checkpoint is None:
            return RecoveryOutcome(
                status=RunStatus.BLOCKED_CORRUPT,
                executions_started=0,
                reason="missing checkpoint",
            )

        try:
            phase, payload = decode_checkpoint(checkpoint)
            outcome = await self._classify(context, checkpoint, phase, payload)
        except IncompatibleCheckpointError as error:
            return RecoveryOutcome(
                status=RunStatus.BLOCKED_INCOMPATIBLE,
                executions_started=0,
                reason=str(error),
            )
        except CorruptCheckpointError as error:
            return RecoveryOutcome(
                status=RunStatus.BLOCKED_CORRUPT,
                executions_started=0,
                reason=str(error),
            )

        if run.status in TERMINAL_RUN_STATUSES:
            return RecoveryOutcome(
                action=RecoveryAction.TERMINAL_NOOP,
                status=run.status,
                executions_started=0,
            )

        return outcome

    async def _classify(
        self,
        context: TenantContext,
        checkpoint: CheckpointRecord,
        phase: CheckpointPhase,
        payload: CheckpointPayload,
    ) -> RecoveryOutcome:
        if phase in {
            CheckpointPhase.RUN_STARTED,
            CheckpointPhase.MODEL_OUTPUT_COMMITTED,
            CheckpointPhase.EXECUTION_RESULT_OBSERVED,
        }:
            if phase is CheckpointPhase.EXECUTION_RESULT_OBSERVED:
                await self._validate_execution_result(context, checkpoint, payload)
            return RecoveryOutcome(action=RecoveryAction.RESUME_MODEL)

        if phase is CheckpointPhase.AWAITING_APPROVAL:
            approval_payload = payload if isinstance(payload, AwaitingApprovalPayload) else None
            assert approval_payload is not None
            approval = await self._approval(context, approval_payload.approval_id)
            if approval is None:
                raise CorruptCheckpointError("checkpoint references a missing approval")
            if approval.tool_call_id != approval_payload.tool_call_id:
                raise CorruptCheckpointError("checkpoint tool_call_id does not match approval")
            if approval.status is ApprovalStatus.AWAITING_USER:
                return RecoveryOutcome(action=RecoveryAction.AWAIT_USER)
            return RecoveryOutcome(action=RecoveryAction.RESUME_MODEL)

        if phase is CheckpointPhase.EXECUTION_DISPATCHING:
            dispatch_payload = payload if isinstance(payload, ExecutionDispatchingPayload) else None
            assert dispatch_payload is not None
            await self._validate_execution_dispatch(context, checkpoint, dispatch_payload)
            return RecoveryOutcome(
                action=_dispatch_recovery_action(dispatch_payload.recovery_strategy),
                execution_id=dispatch_payload.execution_id,
            )

        if phase is CheckpointPhase.RUN_TERMINAL:
            terminal_payload = payload if isinstance(payload, RunTerminalPayload) else None
            assert terminal_payload is not None
            return RecoveryOutcome(
                action=RecoveryAction.TERMINAL_NOOP,
                status=terminal_payload.terminal_status,
            )

        raise IncompatibleCheckpointError(f"unsupported checkpoint phase {phase.value}")

    async def _validate_execution_dispatch(
        self,
        context: TenantContext,
        checkpoint: CheckpointRecord,
        payload: ExecutionDispatchingPayload,
    ) -> None:
        execution = await self._execution(context, payload.execution_id)
        if execution is None:
            raise CorruptCheckpointError("checkpoint references a missing execution")
        if checkpoint.execution_id != payload.execution_id:
            raise CorruptCheckpointError("checkpoint execution_id does not match payload")
        if checkpoint.approval_id != execution.approval_id:
            raise CorruptCheckpointError("checkpoint approval scope does not match execution")
        if execution.tool_call_id != payload.tool_call_id:
            raise CorruptCheckpointError("checkpoint tool_call_id does not match execution")
        if execution.recovery_strategy is not payload.recovery_strategy:
            raise CorruptCheckpointError("checkpoint recovery_strategy does not match execution")
        if execution.input_hash != payload.input_hash:
            raise CorruptCheckpointError("checkpoint input_hash does not match execution")
        if execution.input_ref != payload.input_ref:
            raise CorruptCheckpointError("checkpoint input_ref does not match execution")
        if payload.recovery_strategy is RecoveryStrategy.IDEMPOTENT_RETRY:
            if payload.idempotency_key != execution.idempotency_key:
                raise CorruptCheckpointError("checkpoint idempotency_key does not match execution")

    async def _validate_execution_result(
        self,
        context: TenantContext,
        checkpoint: CheckpointRecord,
        payload: CheckpointPayload,
    ) -> None:
        result_payload = payload if isinstance(payload, ExecutionResultObservedPayload) else None
        assert result_payload is not None
        execution = await self._execution(context, result_payload.execution_id)
        if execution is None:
            raise CorruptCheckpointError("checkpoint references a missing execution")
        if checkpoint.execution_id != result_payload.execution_id:
            raise CorruptCheckpointError("checkpoint execution_id does not match payload")
        if execution.status is not result_payload.result_status:
            raise CorruptCheckpointError("checkpoint result_status does not match execution")
        if execution.result_digest != result_payload.result_digest:
            raise CorruptCheckpointError("checkpoint result_digest does not match execution")
        if execution.result_ref != result_payload.result_ref:
            raise CorruptCheckpointError("checkpoint result_ref does not match execution")
        if execution.external_request_id != result_payload.external_request_id:
            raise CorruptCheckpointError("checkpoint external_request_id does not match execution")

    async def _latest_checkpoint(self, context: TenantContext) -> CheckpointRecord | None:
        async with self._database.connect() as conn:
            repository = self._repository(conn)
            return await repository.get_latest_checkpoint(context)

    async def _run(self, context: TenantContext) -> RunRecord | None:
        async with self._database.connect() as conn:
            repository = self._repository(conn)
            return await repository.get_run(context)

    async def _approval(self, context: TenantContext, approval_id: str):
        async with self._database.connect() as conn:
            repository = self._repository(conn)
            return await repository.get_approval(context, approval_id)

    async def _execution(self, context: TenantContext, execution_id: str):
        async with self._database.connect() as conn:
            repository = self._repository(conn)
            return await repository.get_execution_recovery(context, execution_id)

    async def _acquire_recovery_lease(
        self,
        context: TenantContext,
        runtime_instance_id: str,
    ):
        coordinator = WorkflowCoordinator(self._database, settings=self._settings)
        try:
            return await coordinator.acquire_run(context, runtime_instance_id)
        except LeaseConflictError:
            async with self._database.write_transaction() as conn:
                repository = self._repository(conn)
                live_lease = await repository.get_live_lease(context, runtime_instance_id)
                if live_lease is not None:
                    return live_lease
            raise

    def _repository(self, conn) -> WorkflowRepository:
        return WorkflowRepository(
            conn,
            self._database.dialect,
            self._settings.workflow.heartbeat_ms,
            self._settings.workflow.lease_ttl_ms,
        )


def _normalize_key(key: Any) -> str:
    if not isinstance(key, str):
        raise CorruptCheckpointError("checkpoint payload keys must be strings")
    return "".join(character for character in key.lower() if character.isalnum())


def _decode_json_object(payload_json: str) -> dict[str, Any]:
    try:
        value = json.loads(payload_json)
    except json.JSONDecodeError as error:
        raise CorruptCheckpointError("checkpoint payload_json is not valid JSON") from error
    if not isinstance(value, dict):
        raise CorruptCheckpointError("checkpoint payload_json must decode to an object")
    return value


def _validate_checkpoint_scope(checkpoint: CheckpointRecord, payload: CheckpointPayload) -> None:
    if payload.run_id != checkpoint.run_id:
        raise CorruptCheckpointError("checkpoint payload run_id does not match row scope")
    if isinstance(payload, RunStartedPayload):
        if payload.tenant_id != checkpoint.tenant_id:
            raise CorruptCheckpointError("checkpoint payload tenant_id does not match row scope")
        if payload.workspace_id != checkpoint.workspace_id:
            raise CorruptCheckpointError("checkpoint payload workspace_id does not match row scope")
        if payload.session_id != checkpoint.session_id:
            raise CorruptCheckpointError("checkpoint payload session_id does not match row scope")
        return
    if isinstance(payload, AwaitingApprovalPayload) and checkpoint.approval_id != payload.approval_id:
        raise CorruptCheckpointError("checkpoint approval_id does not match payload")
    if isinstance(payload, ExecutionDispatchingPayload) and checkpoint.execution_id != payload.execution_id:
        raise CorruptCheckpointError("checkpoint execution_id does not match payload")
    if isinstance(payload, ExecutionResultObservedPayload) and checkpoint.execution_id != payload.execution_id:
        raise CorruptCheckpointError("checkpoint execution_id does not match payload")


def _dispatch_recovery_action(strategy: RecoveryStrategy) -> RecoveryAction:
    if strategy is RecoveryStrategy.READ_ONLY_REPLAY:
        return RecoveryAction.REPLAY_READ_ONLY
    if strategy is RecoveryStrategy.IDEMPOTENT_RETRY:
        return RecoveryAction.RETRY_IDEMPOTENT
    return RecoveryAction.MARK_MANUAL_UNCERTAIN


@dataclass(frozen=True, slots=True)
class _RecoveryCandidate:
    context: TenantContext
    awaiting_resolution: bool


class WorkflowRecoveryWorker:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings | None = None,
        runtime_pool,
        batch_size: int = 20,
    ) -> None:
        self._database = database
        self._settings = settings or Settings(_config_file="/nonexistent")
        self._runtime_pool = runtime_pool
        self._batch_size = batch_size
        self._recovery_service = RecoveryService(database, settings=self._settings)

    async def run_once(self) -> None:
        candidates = await self._load_candidates()
        for candidate in candidates:
            try:
                await self._process_candidate(candidate)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "workflow recovery candidate failed tenant_id=%s run_id=%s",
                    candidate.context.tenant_id,
                    candidate.context.run_id,
                )

    async def _load_candidates(self) -> list[_RecoveryCandidate]:
        now_ms = self._database.dialect.db_now_ms()
        async with self._database.connect() as conn:
            result = await conn.execute(
                select(
                    agent_runs.c.tenant_id,
                    agent_runs.c.workspace_id,
                    agent_runs.c.session_id,
                    agent_runs.c.run_id,
                    exists(
                        select(1)
                        .select_from(approval_requests)
                        .where(
                            approval_requests.c.tenant_id == agent_runs.c.tenant_id,
                            approval_requests.c.workspace_id == agent_runs.c.workspace_id,
                            approval_requests.c.session_id == agent_runs.c.session_id,
                            approval_requests.c.run_id == agent_runs.c.run_id,
                            approval_requests.c.approval_status.in_(("approved", "expired")),
                        )
                    ).label("awaiting_resolution"),
                )
                .where(
                    or_(
                        (
                            (agent_runs.c.run_status == RunStatus.AWAITING_USER.value)
                            & exists(
                                select(1)
                                .select_from(approval_requests)
                                .where(
                                    approval_requests.c.tenant_id == agent_runs.c.tenant_id,
                                    approval_requests.c.workspace_id == agent_runs.c.workspace_id,
                                    approval_requests.c.session_id == agent_runs.c.session_id,
                                    approval_requests.c.run_id == agent_runs.c.run_id,
                                    approval_requests.c.approval_status.in_(("approved", "expired")),
                                )
                            )
                        ),
                        (
                            agent_runs.c.run_status.in_(
                                (
                                    RunStatus.RUNNING.value,
                                    RunStatus.AWAITING_USER.value,
                                    RunStatus.RESUMING.value,
                                )
                            )
                            & (agent_runs.c.lease_expires_at <= now_ms)
                        ),
                    )
                )
                .order_by(
                    agent_runs.c.tenant_id.asc(),
                    agent_runs.c.workspace_id.asc(),
                    agent_runs.c.session_id.asc(),
                    agent_runs.c.run_id.asc(),
                )
                .limit(self._batch_size)
            )
            rows = result.mappings().all()
        return [
            _RecoveryCandidate(
                context=TenantContext(
                    tenant_id=str(row["tenant_id"]),
                    workspace_id=str(row["workspace_id"]),
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                ),
                awaiting_resolution=bool(row["awaiting_resolution"]),
            )
            for row in rows
        ]

    async def _process_candidate(self, candidate: _RecoveryCandidate) -> None:
        runtime = await self._maybe_await(
            self._runtime_pool.acquire(
                TenantContext(
                    tenant_id=candidate.context.tenant_id,
                    workspace_id=candidate.context.workspace_id,
                )
            )
        )
        runtime_lease = None
        begin_run = getattr(runtime, "begin_run", None)
        if callable(begin_run):
            runtime_lease = begin_run()
        runtime_instance_id = getattr(runtime, "runtime_instance_id", "recovery-worker")
        coordinator = WorkflowCoordinator(self._database, settings=self._settings)
        try:
            try:
                run = await coordinator.get_run(candidate.context)
                if run is None or run.status in TERMINAL_RUN_STATUSES:
                    return

                if candidate.awaiting_resolution and run.status is RunStatus.AWAITING_USER:
                    lease = await coordinator.resume_waiting_run(candidate.context, runtime_instance_id)
                else:
                    lease = await coordinator.acquire_run(candidate.context, runtime_instance_id)
            except LeaseConflictError:
                return
            run_lease_handle = RunLeaseHandle(lease)

            if candidate.awaiting_resolution and await self._resume_approved_plan_if_present(
                runtime=runtime,
                context=candidate.context,
                run_lease_handle=run_lease_handle,
            ):
                return

            outcome = await self._recovery_service.recover(candidate.context, runtime_instance_id)
            await self._consume_outcome(
                runtime=runtime,
                context=candidate.context,
                run_lease_handle=run_lease_handle,
                outcome=outcome,
            )
        finally:
            if runtime_lease is not None:
                runtime_lease.close()

    async def _resume_approved_plan_if_present(
        self,
        *,
        runtime,
        context: TenantContext,
        run_lease_handle: RunLeaseHandle,
    ) -> bool:
        coordinator = WorkflowCoordinator(self._database, settings=self._settings)
        checkpoint = await coordinator.get_latest_checkpoint(context)
        if checkpoint is None or checkpoint.approval_id is None:
            return False
        approval = await self._recovery_service._approval(context, checkpoint.approval_id)
        if approval is None or approval.status is not ApprovalStatus.APPROVED:
            return False
        execution = await coordinator.get_execution_by_approval_id(context, checkpoint.approval_id)
        if execution is None or execution.status is not ExecutionStatus.NOT_STARTED:
            return False
        builder = runtime.registry.get(execution.tool_name)
        if builder is None:
            await runtime.scheduler._block_execution(
                context=context,
                execution=execution,
                run_lease_handle=run_lease_handle,
                status=ExecutionStatus.BLOCKED_INCOMPATIBLE,
                detail="missing builder during recovery",
            )
            return True
        await runtime.scheduler.recover_execution(
            builder=builder,
            context=context,
            execution=execution,
            action=_dispatch_recovery_action(execution.recovery_strategy),
            run_lease_handle=run_lease_handle,
            force_execute=True,
        )
        await self._invoke_continuation(runtime=runtime, context=context, run_lease_handle=run_lease_handle)
        return True

    async def _consume_outcome(
        self,
        *,
        runtime,
        context: TenantContext,
        run_lease_handle: RunLeaseHandle,
        outcome: RecoveryOutcome,
    ) -> None:
        if outcome.status in {RunStatus.BLOCKED_CORRUPT, RunStatus.BLOCKED_INCOMPATIBLE}:
            checkpoint = await WorkflowCoordinator(
                self._database,
                settings=self._settings,
            ).get_latest_checkpoint(context)
            if checkpoint is not None and checkpoint.execution_id is not None:
                execution = await WorkflowCoordinator(
                    self._database,
                    settings=self._settings,
                ).get_execution_recovery(context, checkpoint.execution_id)
                if execution is not None:
                    await runtime.scheduler._block_execution(
                        context=context,
                        execution=execution,
                        run_lease_handle=run_lease_handle,
                        status=(
                            ExecutionStatus.BLOCKED_CORRUPT
                            if outcome.status is RunStatus.BLOCKED_CORRUPT
                            else ExecutionStatus.BLOCKED_INCOMPATIBLE
                        ),
                        detail=outcome.reason or "recovery blocked",
                    )
            return
        if outcome.action is None or outcome.action is RecoveryAction.TERMINAL_NOOP:
            return
        if outcome.action is RecoveryAction.AWAIT_USER:
            return

        coordinator = WorkflowCoordinator(self._database, settings=self._settings)
        execution = None
        if outcome.execution_id is not None:
            execution = await coordinator.get_execution_recovery(context, outcome.execution_id)
            if execution is None:
                return

        if outcome.action in {
            RecoveryAction.REPLAY_READ_ONLY,
            RecoveryAction.RETRY_IDEMPOTENT,
            RecoveryAction.MARK_MANUAL_UNCERTAIN,
        }:
            if execution is None:
                return
            builder = runtime.registry.get(execution.tool_name)
            if builder is None:
                await runtime.scheduler._block_execution(
                    context=context,
                    execution=execution,
                    run_lease_handle=run_lease_handle,
                    status=ExecutionStatus.BLOCKED_INCOMPATIBLE,
                    detail="missing builder during recovery",
                )
                return
            await runtime.scheduler.recover_execution(
                builder=builder,
                context=context,
                execution=execution,
                action=outcome.action,
                run_lease_handle=run_lease_handle,
            )
            if outcome.action is not RecoveryAction.MARK_MANUAL_UNCERTAIN:
                await self._invoke_continuation(
                    runtime=runtime,
                    context=context,
                    run_lease_handle=run_lease_handle,
                )
            return

        if outcome.action is RecoveryAction.RESUME_MODEL:
            await self._invoke_continuation(
                runtime=runtime,
                context=context,
                run_lease_handle=run_lease_handle,
                recovery_outcome=outcome,
            )

    async def _invoke_continuation(
        self,
        *,
        runtime,
        context: TenantContext,
        run_lease_handle: RunLeaseHandle,
        recovery_outcome: RecoveryOutcome | None = None,
    ) -> None:
        continuation = getattr(runtime, "recovery_continuation", None)
        if continuation is None or not hasattr(continuation, "resume"):
            return
        await self._maybe_await(
            continuation.resume(
                runtime=runtime,
                context=context,
                run_lease_handle=run_lease_handle,
                recovery_outcome=recovery_outcome
                or RecoveryOutcome(action=RecoveryAction.RESUME_MODEL),
            )
        )

    @staticmethod
    async def _maybe_await(value):
        if inspect.isawaitable(value):
            return await value
        return value


class RuntimeRecoveryContinuationService:
    async def resume(
        self,
        *,
        runtime,
        context: TenantContext,
        run_lease_handle: RunLeaseHandle,
        recovery_outcome: RecoveryOutcome,
    ) -> None:
        callback = getattr(getattr(runtime, "agent", None), "resume_recovery", None)
        if not callable(callback):
            return

        database = getattr(runtime.agent, "database", None)
        settings = getattr(runtime.agent, "settings", None)
        if database is None or settings is None:
            return
        workflow = WorkflowCoordinator(database, settings=settings)
        continuation = WorkflowContinuationService(database, settings=settings)
        checkpoint = await workflow.get_latest_checkpoint(context)
        recovered_tool_result = None
        recovered_tool_input_json = None
        if checkpoint is not None:
            phase, payload = decode_checkpoint(checkpoint)
            if phase is CheckpointPhase.EXECUTION_RESULT_OBSERVED:
                result_payload = payload if isinstance(payload, ExecutionResultObservedPayload) else None
                assert result_payload is not None
                execution = await workflow.get_execution_recovery(context, result_payload.execution_id)
                if execution is not None:
                    recovered_tool_input_json = execution.input_payload_json
                    recovered_tool_result = await continuation.load_tool_result(
                        context=context,
                        result_ref=result_payload.result_ref,
                        expected_digest=result_payload.result_digest,
                    )
        result = callback(
            context=context,
            run_lease_handle=run_lease_handle,
            workflow_continuation=continuation,
            recovered_tool_result=recovered_tool_result,
            recovered_tool_input_json=recovered_tool_input_json,
        )
        if inspect.isawaitable(result):
            await result
        current_run = await WorkflowCoordinator(database, settings=settings).get_run(context)
        if current_run is not None and current_run.status is RunStatus.RESUMING:
            await run_lease_handle.refresh(
                lambda lease: WorkflowCoordinator(
                    database,
                    settings=settings,
                ).transition_run(lease, RunStatus.RUNNING)
            )
        await run_lease_handle.refresh(
            lambda lease: WorkflowCoordinator(
                database,
                settings=settings,
            ).finish_run_with_checkpoint(lease, RunStatus.COMPLETED)
        )
