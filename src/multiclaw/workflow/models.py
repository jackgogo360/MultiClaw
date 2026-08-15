from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from multiclaw.tenancy.context import TenantContext


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
    RunStatus.AWAITING_USER: frozenset({RunStatus.RESUMING}),
    RunStatus.RESUMING: frozenset(
        {
            RunStatus.RUNNING,
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


@dataclass(frozen=True, slots=True)
class RunLease:
    context: TenantContext
    lease_owner: str
    fencing_token: int
    version: int
    lease_expires_at: int


@dataclass(frozen=True, slots=True)
class RunRecord:
    context: TenantContext
    status: RunStatus
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
class ApprovalRecord:
    context: TenantContext
    approval_id: str
    tool_call_id: str
    status: ApprovalStatus
    requested_at: int
    resolved_at: int | None
    expires_at: int
    version: int

