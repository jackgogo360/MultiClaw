from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChatSession(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    title: str = "New Chat"
    status: SessionStatus = SessionStatus.ACTIVE
    user_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_message_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionError(Exception):
    pass


class InvalidSessionTitleError(SessionError):
    pass
