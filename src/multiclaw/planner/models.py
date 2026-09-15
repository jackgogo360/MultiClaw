from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from multiclaw.events.types import ScopedEvent
from multiclaw.security.redaction import redact
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.models import RunLease, RunRecord, RunStatus

if TYPE_CHECKING:
    from multiclaw.config.settings import Settings
    from multiclaw.llm.router import CompletionRouter
    from multiclaw.skills import SkillManager
    from multiclaw.tools import ToolRegistry
    from multiclaw.workflow.continuation import (
        ContinuationOutcome,
        PersistedToolResult,
        WorkflowContinuationService,
    )
    from multiclaw.workflow.models import RunLeaseHandle


class PlanningMode(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class PlanningRoute(StrEnum):
    DIRECT = "direct"
    PLAN = "plan"


class PlanStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"

    # Keep the legacy Planner vocabulary available until its callers migrate.
    DRAFT = "awaiting_approval"
    COMPLETED = "archived"


class PlanTriggerMode(StrEnum):
    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"


class PlanDecisionAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"


class PlanStepRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class PlanDecisionRecord:
    decision_id: str
    plan_version: int
    expected_plan_cas_version: int
    action: PlanDecisionAction
    feedback: str | None
    decided_by: str
    resulting_plan_version: int | None
    created_at: int


@dataclass(frozen=True, slots=True)
class PlanStepRecord:
    step_id: str
    logical_step_key: str
    supersedes_step_id: str | None
    ordinal: int
    title: str
    description: str
    expected_outcome: str
    assigned_agent_profile_id: str | None
    max_attempts: int
    definition_digest: str


@dataclass(frozen=True, slots=True)
class PlanStepRunRecord:
    step_run_id: str
    run_id: str
    plan_id: str
    plan_version: int
    step_id: str
    attempt: int
    status: PlanStepRunStatus
    result_summary: str | None
    result_ref: str | None
    result_digest: str | None
    error_code: str | None
    error_detail_redacted: str | None
    reused_from_step_run_id: str | None
    version: int
    started_at: int
    finished_at: int | None


TERMINAL_PLAN_STEP_STATUSES = frozenset(
    {PlanStepRunStatus.FAILED_TERMINAL, PlanStepRunStatus.CANCELLED}
)
EXECUTABLE_PLAN_RUN_STATUSES = frozenset({RunStatus.RESUMING, RunStatus.RUNNING})
NON_DISPATCHABLE_PLAN_STEP_STATUSES = frozenset(
    {
        PlanStepRunStatus.RUNNING,
        PlanStepRunStatus.SUCCEEDED,
        *TERMINAL_PLAN_STEP_STATUSES,
    }
)


def is_plan_run_executable(
    status: RunStatus,
    cancel_requested_at: int | None,
) -> bool:
    return status in EXECUTABLE_PLAN_RUN_STATUSES and cancel_requested_at is None


def is_plan_step_ready(
    step_id: str,
    dependency_ids: tuple[str, ...],
    latest: Mapping[str, PlanStepRunRecord],
) -> bool:
    current = latest.get(step_id)
    if (
        current is not None
        and current.status in NON_DISPATCHABLE_PLAN_STEP_STATUSES
    ):
        return False
    return all(
        (dependency := latest.get(dependency_id)) is not None
        and dependency.status is PlanStepRunStatus.SUCCEEDED
        for dependency_id in dependency_ids
    )


@dataclass(frozen=True, slots=True)
class PlanVersionRecord:
    plan_id: str
    plan_version: int
    objective: str
    constraints: tuple[str, ...]
    generation_reason: str
    parent_version: int | None
    revision_feedback: str | None
    schema_version: int
    content_digest: str
    created_at: int
    steps: tuple[PlanStepRecord, ...]
    dependencies: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    context: TenantContext
    plan_id: str
    source_message_id: str
    trigger_mode: PlanTriggerMode
    status: PlanStatus
    current_version: int
    approved_version: int | None
    aggregate_version: int
    created_at: int
    updated_at: int
    current: PlanVersionRecord
    versions: tuple[PlanVersionRecord, ...]
    decisions: tuple[PlanDecisionRecord, ...]


class PlanDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=36, max_length=36)
    plan_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    action: PlanDecisionAction
    feedback: str | None = Field(default=None, max_length=8_000)

    @model_validator(mode="after")
    def validate_feedback(self) -> PlanDecisionRequest:
        if self.action is PlanDecisionAction.REVISE:
            if self.feedback is None or not self.feedback.strip():
                raise ValueError("revision feedback is required")
        elif self.feedback is not None:
            raise ValueError("feedback is valid only for revise")
        return self


@dataclass(frozen=True, slots=True)
class PlanDecisionMutationResult:
    snapshot: PlanSnapshot
    decision: PlanDecisionRecord
    idempotent_replay: bool


class PlanNotFoundError(RuntimeError):
    pass


class PlanDecisionIdempotencyError(RuntimeError):
    pass


class PlanVersionConflictError(RuntimeError):
    def __init__(self, latest: PlanSnapshot):
        super().__init__("Plan version conflict")
        self.latest = latest


@dataclass(frozen=True, slots=True)
class PlanSummary:
    plan_id: str
    session_id: str
    status: PlanStatus
    current_version: int
    approved_version: int | None
    aggregate_version: int
    latest_run_id: str | None
    latest_run_status: RunStatus | None


SessionId = Annotated[str, Field(min_length=36, max_length=36)]


class SessionScopedRequest(BaseModel):
    """Authenticated session scope supplied by API mutation callers."""

    model_config = ConfigDict(extra="forbid")

    session_id: SessionId


class PlanDecisionBody(SessionScopedRequest):
    """Untrusted Plan decision fields; actor identity comes from auth only."""

    decision_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    action: PlanDecisionAction
    feedback: str | None = Field(default=None, max_length=8_000)

    @model_validator(mode="after")
    def validate_feedback(self) -> PlanDecisionBody:
        if self.action is PlanDecisionAction.REVISE:
            if self.feedback is None or not self.feedback.strip():
                raise ValueError("revision feedback is required")
        elif self.feedback is not None:
            raise ValueError("feedback is valid only for revise")
        return self


class PlanStepResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    logical_step_key: str
    supersedes_step_id: str | None
    ordinal: int
    title: str
    description: str
    expected_outcome: str
    assigned_agent_profile_id: str | None
    max_attempts: int
    definition_digest: str
    dependency_ids: tuple[str, ...]


class PlanVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: int
    objective: str
    constraints: tuple[str, ...]
    generation_reason: str
    parent_version: int | None
    revision_feedback: str | None
    schema_version: int
    content_digest: str
    created_at: int
    steps: tuple[PlanStepResponse, ...]


class PlanDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    plan_version: int
    expected_plan_cas_version: int
    action: PlanDecisionAction
    feedback: str | None
    decided_by: str
    resulting_plan_version: int | None
    created_at: int


class PlanStepAttemptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_run_id: str
    plan_version: int
    step_id: str
    attempt: int
    status: PlanStepRunStatus
    result_summary: str | None
    result_digest: str | None
    error_code: str | None
    error_detail_redacted: str | None
    reused_from_step_run_id: str | None
    version: int
    started_at: int
    finished_at: int | None


class RunSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: RunStatus
    initial_plan_version: int | None
    active_plan_version: int | None
    cancel_requested_at: int | None
    final_summary_available: bool


class PlanResponse(BaseModel):
    """Redacted immutable Plan aggregate view; GET is the authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    session_id: str
    source_message_id: str
    trigger_mode: PlanTriggerMode
    status: PlanStatus
    current_version: int
    approved_version: int | None
    aggregate_version: int
    created_at: int
    updated_at: int
    versions: tuple[PlanVersionResponse, ...]
    decisions: tuple[PlanDecisionResponse, ...]
    runs: tuple[RunSummaryResponse, ...]
    latest_attempts: tuple[PlanStepAttemptResponse, ...]


class RunResponse(BaseModel):
    """Redacted Plan-bound run state; no raw tool payload reaches the API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    session_id: str
    plan_id: str | None
    initial_plan_version: int | None
    active_plan_version: int | None
    status: RunStatus
    cancel_requested_at: int | None
    version: int
    created_at: int
    updated_at: int
    finished_at: int | None
    final_summary_available: bool
    latest_attempts: tuple[PlanStepAttemptResponse, ...]


class PlanningDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PlanningRoute
    reason: str = Field(max_length=500)


LOGICAL_STEP_KEY = r"^[a-z0-9_-]{1,64}$"
PLAN_ID = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
RESULT_DIGEST = r"^[0-9a-f]{64}$"
ConstraintText = Annotated[str, Field(min_length=1, max_length=1_000)]
EvidenceText = Annotated[str, Field(min_length=1, max_length=2_000)]


class PlanStepResultDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=36, max_length=36, pattern=PLAN_ID)
    plan_version: int = Field(ge=1, strict=True)
    run_id: str = Field(min_length=36, max_length=36, pattern=PLAN_ID)
    step_id: str = Field(min_length=36, max_length=36, pattern=PLAN_ID)
    step_run_id: str = Field(min_length=36, max_length=36, pattern=PLAN_ID)
    attempt: int = Field(ge=1, le=20, strict=True)
    status: Literal["succeeded", "failed"]
    summary: str = Field(min_length=1, max_length=4_000)
    evidence: list[EvidenceText] = Field(max_length=20)
    definition_digest: str = Field(pattern=RESULT_DIGEST)
    dependency_result_digests: dict[
        Annotated[str, Field(pattern=LOGICAL_STEP_KEY)],
        Annotated[str, Field(pattern=RESULT_DIGEST)],
    ] = Field(max_length=20)
    tool_catalog_digest: str = Field(pattern=RESULT_DIGEST)
    policy_digest: str = Field(pattern=RESULT_DIGEST)
    skill_set_digest: str = Field(pattern=RESULT_DIGEST)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def public_context(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "summary": redact(self.summary),
            "evidence": redact(list(self.evidence)),
            "result_digest": self.digest(),
        }


class PlanDraftStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_step_key: str = Field(pattern=LOGICAL_STEP_KEY)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4_000)
    expected_outcome: str = Field(min_length=1, max_length=2_000)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    max_attempts: int = Field(default=2, ge=1, le=20, strict=True)


class PlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=16_000)
    constraints: list[ConstraintText] = Field(default_factory=list, max_length=20)
    generation_reason: str = Field(min_length=1, max_length=1_000)
    steps: list[PlanDraftStep] = Field(min_length=1, max_length=20)


class PlanReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tenant_id: str = Field(min_length=1, max_length=36)
    workspace_id: str = Field(min_length=1, max_length=36)
    plan_id: str = Field(min_length=36, max_length=36, pattern=PLAN_ID)
    plan_version: int = Field(ge=1, strict=True)
    run_id: str = Field(min_length=36, max_length=36)
    aggregate_version: int = Field(ge=1, strict=True)
    session_id: str = Field(min_length=1, max_length=36)


@dataclass(frozen=True, slots=True)
class PlanMaterializationResult:
    plan: PlanSnapshot
    reference: PlanReference
    reference_message_id: str
    run: RunRecord
    event: ScopedEvent


@dataclass(frozen=True, slots=True)
class PlanDecisionResult:
    snapshot: PlanSnapshot
    decision: PlanDecisionRecord
    run: RunRecord
    lease: RunLease | None
    idempotent_replay: bool
    events: tuple[ScopedEvent, ...]


@dataclass(frozen=True, slots=True)
class MaterializeInitialPlan:
    context: TenantContext
    runtime_instance_id: str
    source_message_id: str
    assistant_turn_index: int
    trigger_mode: PlanTriggerMode
    draft: PlanDraft


class PlanRevisionLimitError(RuntimeError):
    pass


class PlanExecutionBlocked(RuntimeError):
    pass


class PlanCancellationRequested(PlanExecutionBlocked):
    pass


class PlanStepAlreadyRunningError(PlanExecutionBlocked):
    pass


class PlanAttemptLimitError(PlanExecutionBlocked):
    pass


class PlanRoundBudgetExceeded(PlanExecutionBlocked):
    """The durable Plan/run round budget has been exhausted."""

    pass


class CompletedStepContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_step_key: str = Field(pattern=LOGICAL_STEP_KEY)
    summary: str = Field(min_length=1, max_length=4_000)
    result_digest: str = Field(pattern=RESULT_DIGEST)


class ImmutablePlanStepContext(BaseModel):
    """The bounded immutable definition a revision generator may reason about."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_step_key: str = Field(pattern=LOGICAL_STEP_KEY)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4_000)
    expected_outcome: str = Field(min_length=1, max_length=2_000)
    max_attempts: int = Field(ge=1, le=20, strict=True)
    definition_digest: str = Field(pattern=RESULT_DIGEST)
    supersedes_step_id: str | None = Field(
        default=None,
        min_length=36,
        max_length=36,
        pattern=PLAN_ID,
    )
    depends_on: tuple[Annotated[str, Field(pattern=LOGICAL_STEP_KEY)], ...] = Field(
        default_factory=tuple,
        max_length=20,
    )


class ImmutablePlanVersionContext(BaseModel):
    """Structured current Plan state for revision generation, never an opaque blob."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: int = Field(ge=1, strict=True)
    content_digest: str = Field(pattern=RESULT_DIGEST)
    objective: str = Field(min_length=1, max_length=16_000)
    constraints: tuple[ConstraintText, ...] = Field(default_factory=tuple, max_length=20)
    generation_reason: str = Field(min_length=1, max_length=1_000)
    steps: tuple[ImmutablePlanStepContext, ...] = Field(min_length=1, max_length=20)


class PlanRevisionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=36, max_length=36, pattern=PLAN_ID)
    parent_version: int = Field(ge=1, strict=True)
    feedback: str | None = Field(max_length=8_000)
    failed_step_key: str | None = Field(pattern=LOGICAL_STEP_KEY)
    # Failure detail follows the existing 4,000-character plan result bound.
    failed_error: str | None = Field(max_length=4_000)
    completed: list[CompletedStepContext] = Field(max_length=20)
    current_plan: ImmutablePlanVersionContext | None = None


def revision_current_plan_context(
    version: PlanVersionRecord,
) -> ImmutablePlanVersionContext:
    """Map durable IDs to stable logical keys before exposing a revision context."""
    steps = tuple(sorted(version.steps, key=lambda step: (step.ordinal, step.step_id)))
    steps_by_id = {step.step_id: step for step in steps}
    return ImmutablePlanVersionContext(
        plan_version=version.plan_version,
        content_digest=version.content_digest,
        objective=version.objective,
        constraints=version.constraints,
        generation_reason=version.generation_reason,
        steps=tuple(
            ImmutablePlanStepContext(
                logical_step_key=step.logical_step_key,
                title=step.title,
                description=step.description,
                expected_outcome=step.expected_outcome,
                max_attempts=step.max_attempts,
                definition_digest=step.definition_digest,
                supersedes_step_id=step.supersedes_step_id,
                depends_on=tuple(
                    dependency.logical_step_key
                    for dependency in sorted(
                        (
                            steps_by_id[dependency_id]
                            for dependency_id in version.dependencies.get(step.step_id, ())
                        ),
                        key=lambda dependency: (
                            dependency.ordinal,
                            dependency.step_id,
                        ),
                    )
                ),
            )
            for step in steps
        ),
    )


class ValidatedPlanStep(PlanDraftStep):
    model_config = ConfigDict(extra="forbid", frozen=True)

    depends_on: tuple[str, ...] = Field(  # type: ignore[assignment]
        default_factory=tuple,
        max_length=20,
    )
    ordinal: int = Field(ge=1, le=20)


class ValidatedPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1, max_length=16_000)
    constraints: tuple[ConstraintText, ...] = Field(
        default_factory=tuple,
        max_length=20,
    )
    generation_reason: str = Field(min_length=1, max_length=1_000)
    steps: tuple[ValidatedPlanStep, ...] = Field(min_length=1, max_length=20)


class PlanStepCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "failed"]
    summary: str = Field(min_length=1, max_length=4_000)
    evidence: list[EvidenceText] = Field(default_factory=list, max_length=20)
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class PlanStepExecutionRequest:
    context: TenantContext
    lease: RunLease
    plan: PlanSnapshot
    step: PlanStepRecord
    step_run: PlanStepRunRecord
    dependency_results: tuple[PlanStepResultDocument, ...]


class PlanStepRunner(Protocol):
    @property
    def router(self) -> CompletionRouter: ...

    @property
    def settings(self) -> Settings: ...

    @property
    def registry(self) -> ToolRegistry: ...

    @property
    def skill_manager(self) -> SkillManager: ...

    async def run_plan_step(
        self,
        request: PlanStepExecutionRequest,
        *,
        run_lease_handle: RunLeaseHandle,
        workflow_continuation: WorkflowContinuationService,
        recovered_tool_result: PersistedToolResult | None = None,
        recovered_tool_input_json: str | None = None,
    ) -> PlanStepCompletion | ContinuationOutcome: ...


@dataclass(frozen=True, slots=True)
class PlanExecutionOutcome:
    state: Literal[
        "awaiting_user",
        "completed",
        "cancelled",
        "failed_terminal",
        "replan_required",
    ]
    plan: PlanSnapshot
    run: RunRecord
    assistant_content: str | None = None


class PlanStep(BaseModel):
    order: int
    description: str
    tool_name: str | None = None
    expected_outcome: str = ""


class Plan(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    steps: list[PlanStep]
    status: PlanStatus = PlanStatus.AWAITING_APPROVAL
    approved_by: str | None = None
