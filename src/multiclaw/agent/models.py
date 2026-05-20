from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    TOOL_CALL = "tool_call"
    RESPONSE = "response"
    PLAN = "plan"
    ASK_USER = "ask_user"


class ObservationType(str, Enum):
    TOOL_RESULT = "tool_result"
    USER_RESPONSE = "user_response"
    PLAN_APPROVED = "plan_approved"
    ERROR = "error"


class Action(BaseModel):
    type: ActionType
    content: str = ""
    tool_name: str = ""
    tool_params: dict[str, Any] = Field(default_factory=dict)


class Observation(BaseModel):
    type: ObservationType
    content: str
    data: dict[str, Any] = Field(default_factory=dict)


class UserMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    content: str


class AgentMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    content: str
