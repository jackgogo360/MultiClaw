import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.models import RunStatus


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
    def validate_feedback(self) -> "PlanDecisionRequest":
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


class PlanningDecision(BaseModel):
    mode: PlanningRoute
    reason: str = Field(max_length=500)


LOGICAL_STEP_KEY = r"^[a-z0-9_-]{1,64}$"
ConstraintText = Annotated[str, Field(min_length=1, max_length=1_000)]
EvidenceText = Annotated[str, Field(min_length=1, max_length=2_000)]


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


class CompletedStepContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_step_key: str = Field(pattern=LOGICAL_STEP_KEY)
    summary: str = Field(min_length=1, max_length=4_000)
    result_digest: str = Field(min_length=1, max_length=128)


class PlanRevisionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    parent_version: int
    feedback: str | None
    failed_step_key: str | None
    failed_error: str | None
    completed: list[CompletedStepContext] = Field(max_length=20)


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
