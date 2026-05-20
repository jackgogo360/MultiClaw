from enum import Enum
import uuid

from pydantic import BaseModel, Field


class PlanStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


class PlanStep(BaseModel):
    order: int
    description: str
    tool_name: str | None = None
    expected_outcome: str = ""


class Plan(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    steps: list[PlanStep]
    status: PlanStatus = PlanStatus.DRAFT
    approved_by: str | None = None
