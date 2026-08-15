from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from multiclaw.config import Settings
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.tenancy.context import TenantContext
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
    ModelOutputPayload,
    PHASE_PAYLOADS,
    LeaseConflictError,
    RecoveryAction,
    RecoveryOutcome,
    RecoveryStrategy,
    TERMINAL_RUN_STATUSES,
    RunStartedPayload,
    RunRecord,
    RunStatus,
    RunTerminalPayload,
)


SECRET_KEY_MARKERS = {"secret", "token", "password", "apikey", "authorization"}


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
            return await self._classify(context, checkpoint, phase, payload)
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
