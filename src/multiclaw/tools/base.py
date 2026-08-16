import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from multiclaw.workflow.models import RecoveryStrategy

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
    audit: dict[str, Any] = Field(default_factory=dict, exclude=True)
    external_request_id: str | None = Field(default=None, exclude=True)
    result_ref: str | None = Field(default=None, exclude=True)
    result_digest: str | None = Field(default=None, exclude=True)


class ToolRecoveryMetadata(BaseModel):
    tool_kind: Literal["native", "mcp"]
    recovery_strategy: RecoveryStrategy
    idempotency_key: str | None = None


class ToolProgressRecorder(ABC):
    @abstractmethod
    async def record_external_request_id(self, external_request_id: str) -> None:
        raise NotImplementedError


class ToolInvocation(ABC, Generic[TParams]):
    def __init__(self, name: str, params: TParams) -> None:
        self.name = name
        self.params = params
        self.approved_roots: list[Path] = []
        self.progress_recorder: ToolProgressRecorder | None = None

    def configure_permission(
        self,
        approved_roots: Sequence[str | Path] | None = None,
    ) -> None:
        self.approved_roots = [Path(root).resolve() for root in (approved_roots or [])]

    def configure_progress(
        self,
        recorder: ToolProgressRecorder | None,
    ) -> None:
        self.progress_recorder = recorder

    @abstractmethod
    async def execute(self) -> ToolExecutionResult:
        raise NotImplementedError


class ToolBuilder(ABC, Generic[TParams]):
    tool_kind: ClassVar[Literal["native", "mcp"]] = "native"
    recovery_strategy: ClassVar[RecoveryStrategy]
    idempotency_key_field: ClassVar[str | None] = None
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

    def recovery_metadata(self, params: TParams) -> ToolRecoveryMetadata:
        strategy = getattr(self, "recovery_strategy", None)
        if not isinstance(strategy, RecoveryStrategy):
            raise ValueError(f"tool {self.name!r} is missing recovery_strategy declaration")

        key: str | None = None
        if self.idempotency_key_field:
            value = getattr(params, self.idempotency_key_field, None)
            key = None if value is None else str(value)
        if strategy is RecoveryStrategy.IDEMPOTENT_RETRY and not key:
            raise ValueError(
                f"tool {self.name!r} requires idempotency_key_field for idempotent_retry"
            )
        return ToolRecoveryMetadata(
            tool_kind=self.tool_kind,
            recovery_strategy=strategy,
            idempotency_key=key,
        )
