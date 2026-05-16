from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


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
