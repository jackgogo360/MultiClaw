from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field


class PermissionDecision(BaseModel):
    allow: bool
    requires_approval: bool
    reason: str
    approved_roots: list[str] = Field(default_factory=list)


class AuditLog(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_name: str
    status: str
    detail: str
    event_type: str = "tool.event"
    user_id: str = ""
    tenant_id: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
