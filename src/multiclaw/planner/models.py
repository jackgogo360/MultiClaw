import uuid
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class ValidatedPlanStep(PlanDraftStep):
    ordinal: int = Field(ge=1, le=20)


class ValidatedPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=16_000)
    constraints: list[ConstraintText] = Field(default_factory=list, max_length=20)
    generation_reason: str = Field(min_length=1, max_length=1_000)
    steps: list[ValidatedPlanStep] = Field(min_length=1, max_length=20)


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
