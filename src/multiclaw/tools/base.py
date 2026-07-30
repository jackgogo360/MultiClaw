import json
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

TParams = TypeVar("TParams", bound=BaseModel)


class ToolStatus(str, Enum):
    SCHEDULED = "scheduled"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class ToolExecutionResult(BaseModel):
    status: ToolStatus
    content: str
    data: dict[str, Any] = Field(default_factory=dict)


class ToolInvocation(ABC, Generic[TParams]):
    def __init__(self, name: str, params: TParams) -> None:
        self.name = name
        self.params = params
        self.approved_roots: list[Path] = []

    def configure_permission(self, approved_roots: list[str] | None = None) -> None:
        self.approved_roots = [Path(root).resolve() for root in (approved_roots or [])]

    @abstractmethod
    async def execute(self) -> ToolExecutionResult:
        raise NotImplementedError


class ToolBuilder(ABC, Generic[TParams]):
    name: str
    description: str
    parameters_schema: type[TParams]
    read_only: bool = False

    @abstractmethod
    def validate(self, params: dict[str, Any]) -> TParams:
        raise NotImplementedError

    @abstractmethod
    def build(self, params: TParams) -> ToolInvocation[TParams]:
        raise NotImplementedError

    def approval_description(self, params: dict[str, Any]) -> str:
        """Human-readable description of what this tool invocation will do.
        Override in subclasses for tool-specific formatting."""
        return json.dumps(params, ensure_ascii=False)
