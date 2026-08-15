import json
from enum import Enum
from time import time
from typing import Any, Mapping
import uuid

from pydantic import BaseModel, Field, field_validator, model_validator


def _now_ms() -> int:
    return int(time() * 1000)


def _require_non_empty(name: str, value: str) -> str:
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _normalize_title(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 120:
        raise ValueError("title must be between 1 and 120 characters")
    return normalized


def _load_metadata(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    return json.loads(str(value))


def _dump_metadata(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChatSession(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    workspace_id: str
    title: str = "New Chat"
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)
    last_message_at: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", mode="before")
    @classmethod
    def _validate_title(cls, value: object) -> str:
        return _normalize_title(str(value))

    @model_validator(mode="after")
    def _validate_scope(self) -> "ChatSession":
        _require_non_empty("tenant_id", self.tenant_id)
        _require_non_empty("workspace_id", self.workspace_id)
        return self

    def metadata_json(self) -> str:
        return _dump_metadata(self.metadata)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ChatSession":
        return cls(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            workspace_id=str(row["workspace_id"]),
            title=str(row["title"]),
            status=SessionStatus(str(row["status"])),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            last_message_at=(
                None if row.get("last_message_at") is None else int(row["last_message_at"])
            ),
            metadata=_load_metadata(row.get("metadata_json", row.get("metadata", "{}"))),
        )


class SessionError(Exception):
    pass


class InvalidSessionTitleError(SessionError):
    pass
