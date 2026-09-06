from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from multiclaw.tenancy.context import TenantContext

if TYPE_CHECKING:
    from multiclaw.workflow.recovery import PlanRecoveryContext


class RunStatus(str, Enum):
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"
    BLOCKED_CORRUPT = "blocked_corrupt"
    BLOCKED_INCOMPATIBLE = "blocked_incompatible"


class ApprovalStatus(str, Enum):
    AWAITING_USER = "awaiting_user"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ExecutionStatus(str, Enum):
    NOT_STARTED = "not_started"
    REPLAYING = "replaying"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    UNCERTAIN = "uncertain"
    BLOCKED_INCOMPATIBLE = "blocked_incompatible"
    BLOCKED_CORRUPT = "blocked_corrupt"


class RecoveryStrategy(str, Enum):
    READ_ONLY_REPLAY = "read_only_replay"
    IDEMPOTENT_RETRY = "idempotent_retry"
    MANUAL_UNCERTAIN = "manual_uncertain"


class CheckpointPhase(StrEnum):
    RUN_STARTED = "run_started"
    PLAN_AWAITING_APPROVAL = "plan_awaiting_approval"
    PLAN_STEP_READY = "plan_step_ready"
    PLAN_REPLAN_REQUIRED = "plan_replan_required"
    MODEL_OUTPUT_COMMITTED = "model_output_committed"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTION_DISPATCHING = "execution_dispatching"
    EXECUTION_RESULT_OBSERVED = "execution_result_observed"
    RUN_TERMINAL = "run_terminal"


class RecoveryAction(StrEnum):
    RESUME_MODEL = "resume_model"
    AWAIT_USER = "await_user"
    AWAIT_PLAN_DECISION = "await_plan_decision"
    RESUME_PLAN_STEP = "resume_plan_step"
    RESUME_PLAN_REVISION = "resume_plan_revision"
    REPLAY_READ_ONLY = "replay_read_only"
    RETRY_IDEMPOTENT = "retry_idempotent"
    MARK_MANUAL_UNCERTAIN = "mark_manual_uncertain"
    TERMINAL_NOOP = "terminal_noop"


UUID_FIELD = Field(min_length=36, max_length=36)
MESSAGE_ID_FIELD = Field(min_length=1, max_length=128)
TOOL_CALL_ID_FIELD = Field(min_length=1, max_length=128)
CURSOR_FIELD = Field(min_length=1, max_length=255)
REF_FIELD = Field(min_length=1, max_length=255)
OPTIONAL_REQUEST_ID_FIELD = Field(default=None, min_length=1, max_length=255)
DIGEST_FIELD = Field(min_length=64, max_length=64)
UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
SHA256_PATTERN = r"^[0-9a-f]{64}$"
OPTIONAL_IDEMPOTENCY_KEY_FIELD = Field(default=None, min_length=1, max_length=128)


class CheckpointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1


class RunStartedPayload(CheckpointPayload):
    tenant_id: str = UUID_FIELD
    workspace_id: str = UUID_FIELD
    session_id: str = UUID_FIELD
    run_id: str = UUID_FIELD
    started_at_ms: StrictInt
    model_cursor: str = CURSOR_FIELD
    next_step: Literal["model_inference"] = "model_inference"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self) -> RunStartedPayload:
        if self.cursor != self.model_cursor:
            raise ValueError("cursor must match model_cursor")
        return self


class PlanAwaitingApprovalPayload(CheckpointPayload):
    run_id: str = Field(min_length=36, max_length=36, pattern=UUID_PATTERN)
    plan_id: str = Field(min_length=36, max_length=36, pattern=UUID_PATTERN)
    plan_version: StrictInt = Field(ge=1)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=SHA256_PATTERN)
    decision_cursor: str = CURSOR_FIELD
    next_step: Literal["plan_decision"] = "plan_decision"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self) -> PlanAwaitingApprovalPayload:
        if self.cursor != self.decision_cursor:
            raise ValueError("cursor must match decision_cursor")
        return self


class PlanStepReadyPayload(CheckpointPayload):
    run_id: str = Field(min_length=36, max_length=36, pattern=UUID_PATTERN)
    plan_id: str = Field(min_length=36, max_length=36, pattern=UUID_PATTERN)
    plan_version: StrictInt = Field(ge=1)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=SHA256_PATTERN)
    step_id: str = Field(min_length=36, max_length=36, pattern=UUID_PATTERN)
    step_run_id: str = Field(min_length=36, max_length=36, pattern=UUID_PATTERN)
    attempt: StrictInt = Field(ge=1, le=20)
    execution_cursor: Literal[
        "dispatch_step",
        "continue_step",
        "select_next",
        "final_summary",
    ]
    next_step: Literal["plan_step_execution"] = "plan_step_execution"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self) -> PlanStepReadyPayload:
        if self.cursor != self.execution_cursor:
            raise ValueError("cursor must match execution_cursor")
        return self


class PlanReplanRequiredPayload(CheckpointPayload):
    run_id: str = Field(min_length=36, max_length=36, pattern=UUID_PATTERN)
    plan_id: str = Field(min_length=36, max_length=36, pattern=UUID_PATTERN)
    plan_version: StrictInt = Field(ge=1)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=SHA256_PATTERN)
    failed_step_run_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=UUID_PATTERN,
    )
    failure_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=SHA256_PATTERN,
    )
    revision_cursor: Literal["generate_revision"] = "generate_revision"
    next_step: Literal["plan_revision"] = "plan_revision"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self) -> PlanReplanRequiredPayload:
        if self.cursor != self.revision_cursor:
            raise ValueError("cursor must match revision_cursor")
        return self


class ModelOutputPayload(CheckpointPayload):
    run_id: str = UUID_FIELD
    message_id: str = MESSAGE_ID_FIELD
    output_digest: str = DIGEST_FIELD
    model_cursor: str = CURSOR_FIELD
    next_step: Literal["tool_plan_or_terminal"] = "tool_plan_or_terminal"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self) -> ModelOutputPayload:
        if self.cursor != self.model_cursor:
            raise ValueError("cursor must match model_cursor")
        return self


class AwaitingApprovalPayload(CheckpointPayload):
    run_id: str = UUID_FIELD
    approval_id: str = UUID_FIELD
    tool_call_id: str = TOOL_CALL_ID_FIELD
    approval_expires_at_ms: StrictInt
    resume_cursor: str = CURSOR_FIELD
    next_step: Literal["approval_resolution"] = "approval_resolution"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self) -> AwaitingApprovalPayload:
        if self.cursor != self.resume_cursor:
            raise ValueError("cursor must match resume_cursor")
        return self


class ExecutionDispatchingPayload(CheckpointPayload):
    run_id: str = UUID_FIELD
    execution_id: str = UUID_FIELD
    tool_call_id: str = TOOL_CALL_ID_FIELD
    recovery_strategy: RecoveryStrategy
    input_hash: str = DIGEST_FIELD
    input_ref: str = REF_FIELD
    idempotency_key: str | None = OPTIONAL_IDEMPOTENCY_KEY_FIELD
    dispatch_cursor: str = CURSOR_FIELD
    next_step: Literal["execution_observation"] = "execution_observation"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_dispatch(self) -> ExecutionDispatchingPayload:
        if self.cursor != self.dispatch_cursor:
            raise ValueError("cursor must match dispatch_cursor")
        if self.recovery_strategy is RecoveryStrategy.IDEMPOTENT_RETRY and not self.idempotency_key:
            raise ValueError("idempotent_retry requires idempotency_key")
        return self


class ExecutionResultObservedPayload(CheckpointPayload):
    run_id: str = UUID_FIELD
    execution_id: str = UUID_FIELD
    result_status: ExecutionStatus
    result_digest: str = DIGEST_FIELD
    result_ref: str = REF_FIELD
    external_request_id: str | None = OPTIONAL_REQUEST_ID_FIELD
    resume_cursor: str = CURSOR_FIELD
    next_step: Literal["continue_or_terminal"] = "continue_or_terminal"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self) -> ExecutionResultObservedPayload:
        if self.cursor != self.resume_cursor:
            raise ValueError("cursor must match resume_cursor")
        return self


class RunTerminalPayload(CheckpointPayload):
    run_id: str = UUID_FIELD
    terminal_status: RunStatus
    finished_at_ms: StrictInt
    final_digest: str = DIGEST_FIELD
    next_step: None = None
    cursor: None = None

    @model_validator(mode="after")
    def validate_terminal_status(self) -> RunTerminalPayload:
        if self.terminal_status not in TERMINAL_RUN_STATUSES:
            raise ValueError("terminal_status must be terminal")
        return self


PHASE_PAYLOADS: dict[CheckpointPhase, type[CheckpointPayload]] = {
    CheckpointPhase.RUN_STARTED: RunStartedPayload,
    CheckpointPhase.PLAN_AWAITING_APPROVAL: PlanAwaitingApprovalPayload,
    CheckpointPhase.PLAN_STEP_READY: PlanStepReadyPayload,
    CheckpointPhase.PLAN_REPLAN_REQUIRED: PlanReplanRequiredPayload,
    CheckpointPhase.MODEL_OUTPUT_COMMITTED: ModelOutputPayload,
    CheckpointPhase.AWAITING_APPROVAL: AwaitingApprovalPayload,
    CheckpointPhase.EXECUTION_DISPATCHING: ExecutionDispatchingPayload,
    CheckpointPhase.EXECUTION_RESULT_OBSERVED: ExecutionResultObservedPayload,
    CheckpointPhase.RUN_TERMINAL: RunTerminalPayload,
}


TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED_TERMINAL,
        RunStatus.CANCELLED,
        RunStatus.BLOCKED_CORRUPT,
        RunStatus.BLOCKED_INCOMPATIBLE,
    }
)

TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED_TERMINAL,
        ExecutionStatus.UNCERTAIN,
        ExecutionStatus.BLOCKED_INCOMPATIBLE,
        ExecutionStatus.BLOCKED_CORRUPT,
    }
)

LEGAL_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.AWAITING_USER,
            RunStatus.COMPLETED,
            RunStatus.FAILED_TERMINAL,
            RunStatus.BLOCKED_INCOMPATIBLE,
            RunStatus.BLOCKED_CORRUPT,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.AWAITING_USER: frozenset({RunStatus.RESUMING, RunStatus.CANCELLED}),
    RunStatus.RESUMING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.AWAITING_USER,
            RunStatus.FAILED_TERMINAL,
            RunStatus.BLOCKED_INCOMPATIBLE,
            RunStatus.BLOCKED_CORRUPT,
            RunStatus.CANCELLED,
        }
    ),
}

LEGAL_APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.AWAITING_USER: frozenset(
        {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
        }
    )
}

LEGAL_EXECUTION_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.NOT_STARTED: frozenset(
        {
            ExecutionStatus.REPLAYING,
            ExecutionStatus.EXECUTING,
            ExecutionStatus.BLOCKED_INCOMPATIBLE,
            ExecutionStatus.BLOCKED_CORRUPT,
        }
    ),
    ExecutionStatus.REPLAYING: frozenset(
        {
            ExecutionStatus.EXECUTING,
            ExecutionStatus.BLOCKED_INCOMPATIBLE,
            ExecutionStatus.BLOCKED_CORRUPT,
        }
    ),
    ExecutionStatus.EXECUTING: frozenset(
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


class WorkflowError(RuntimeError):
    pass


class LeaseConflictError(WorkflowError):
    pass


class StaleFenceError(WorkflowError):
    pass


class InvalidTransitionError(WorkflowError):
    pass


class VersionConflictError(WorkflowError):
    pass


class TenantRunQuotaError(WorkflowError):
    pass


class CheckpointTooLargeError(WorkflowError):
    pass


class CorruptCheckpointError(WorkflowError):
    pass


class IncompatibleCheckpointError(WorkflowError):
    pass


@dataclass(frozen=True, slots=True)
class WorkflowRuntimeCounters:
    active_run_count: int = 0
    active_executing_run_count: int = 0
    awaiting_user_run_count: int = 0
    checkpointed_awaiting_user_run_count: int = 0


@dataclass(frozen=True, slots=True)
class RunLease:
    context: TenantContext
    lease_owner: str
    fencing_token: int
    version: int
    lease_expires_at: int


class RunLeaseHandle:
    def __init__(self, lease: RunLease) -> None:
        self._lease = lease
        self._lock = asyncio.Lock()

    async def current(self) -> RunLease:
        async with self._lock:
            return self._lease

    async def replace(self, lease: RunLease) -> RunLease:
        async with self._lock:
            self._lease = lease
            return lease

    async def use_current(self, operation):
        async with self._lock:
            return await operation(self._lease)

    async def refresh(self, refresher) -> RunLease:
        async with self._lock:
            self._lease = await refresher(self._lease)
            return self._lease


@dataclass(frozen=True, slots=True)
class RunRecord:
    context: TenantContext
    status: RunStatus
    plan_id: str | None
    initial_plan_version: int | None
    active_plan_version: int | None
    cancel_requested_at: int | None
    runtime_instance_id: str | None
    lease_owner: str | None
    fencing_token: int
    lease_expires_at: int | None
    heartbeat_at: int | None
    version: int
    created_at: int
    updated_at: int
    finished_at: int | None


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    context: TenantContext
    execution_id: str
    approval_id: str | None
    tool_call_id: str
    tool_name: str
    tool_kind: str
    status: ExecutionStatus
    recovery_strategy: RecoveryStrategy
    version: int
    created_at: int
    updated_at: int
    finished_at: int | None


@dataclass(frozen=True, slots=True)
class ExecutionRecoveryRecord:
    context: TenantContext
    execution_id: str
    approval_id: str | None
    tool_call_id: str
    tool_name: str
    tool_kind: str
    status: ExecutionStatus
    recovery_strategy: RecoveryStrategy
    idempotency_key: str | None
    input_payload_json: str
    input_hash: str
    input_ref: str
    external_request_id: str | None
    result_ref: str | None
    result_digest: str | None
    version: int


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    checkpoint_id: str
    tenant_id: str
    workspace_id: str
    session_id: str
    run_id: str
    approval_id: str | None
    execution_id: str | None
    phase: CheckpointPhase | str
    checkpoint_seq: int
    payload_json: str
    payload_hash: str
    schema_version: int
    created_at: int


@dataclass(frozen=True, slots=True)
class CheckpointWrite:
    checkpoint_id: str
    checkpoint_seq: int
    phase: CheckpointPhase
    payload_json: str
    payload_hash: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    action: RecoveryAction | None = None
    status: RunStatus | None = None
    lease: RunLease | None = None
    execution_id: str | None = None
    plan_recovery_context: PlanRecoveryContext | None = None
    executions_started: int = 0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    context: TenantContext
    approval_id: str
    tool_call_id: str
    status: ApprovalStatus
    requested_at: int
    resolved_at: int | None
    expires_at: int
    version: int
