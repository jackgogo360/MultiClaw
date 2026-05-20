from datetime import datetime, timezone
import uuid
from typing import Any

from pydantic import BaseModel, Field


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    content: str
    type: str
    tenant_id: str = ""
    session_id: str = ""
    role: str = "note"
    turn_index: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
