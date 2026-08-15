from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from multiclaw.tenancy import TenantContext


class AgentState(str, Enum):
    IDLE = "IDLE"
    THINKING = "THINKING"
    ACTING = "ACTING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    FINISHED = "FINISHED"
    ERROR = "ERROR"


class Event(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentStateEvent(Event):
    type: str = "agent.state_change"
    agent_id: str
    from_state: AgentState
    to_state: AgentState


class EventScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(min_length=1, max_length=36)
    workspace_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    run_id: str = Field(min_length=1, max_length=36)

    @field_validator("tenant_id", "workspace_id", "session_id", "run_id")
    @classmethod
    def _reject_wildcard(cls, value: str) -> str:
        if value == "*":
            raise ValueError("wildcard scope values are not allowed")
        return value

    @classmethod
    def from_context(cls, context: TenantContext) -> "EventScope":
        if context.session_id is None or context.run_id is None:
            raise ValueError("event scope requires session and run")
        return cls(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            run_id=context.run_id,
        )


class ScopedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(min_length=1, max_length=36)
    workspace_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    run_id: str = Field(min_length=1, max_length=36)
    event_type: str = Field(min_length=1, max_length=128)
    occurred_at_ms: int = Field(
        default_factory=lambda: int(datetime.now(timezone.utc).timestamp() * 1000)
    )
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tenant_id", "workspace_id", "session_id", "run_id")
    @classmethod
    def _reject_wildcard_scope(cls, value: str) -> str:
        if value == "*":
            raise ValueError("wildcard scope values are not allowed")
        return value

    @field_validator("event_type")
    @classmethod
    def _reject_wildcard_event_type(cls, value: str) -> str:
        if value == "*":
            raise ValueError("wildcard event types are not allowed")
        return value

    @model_validator(mode="after")
    def _ensure_scope_is_valid(self) -> "ScopedEvent":
        EventScope(
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            run_id=self.run_id,
        )
        return self

    @classmethod
    def from_scope(
        cls,
        scope: EventScope,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> "ScopedEvent":
        return cls(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            session_id=scope.session_id,
            run_id=scope.run_id,
            event_type=event_type,
            data=data or {},
        )

    @classmethod
    def from_context(
        cls,
        context: TenantContext,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> "ScopedEvent":
        return cls.from_scope(EventScope.from_context(context), event_type, data)
