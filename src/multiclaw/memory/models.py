import json
from time import time
import uuid
from typing import Any, Mapping

from pydantic import BaseModel, Field, model_validator


def _now_ms() -> int:
    return int(time() * 1000)


def _load_metadata(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value in (None, ""):
        return {}
    return json.loads(str(value))


def _dump_metadata(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str
    type: str
    session_id: str | None = None
    role: str = "note"
    turn_index: int = 0
    created_at: int = Field(default_factory=_now_ms)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_session_scope(self) -> "MemoryEntry":
        if self.session_id == "":
            self.session_id = None
        return self

    def metadata_json(self) -> str:
        return _dump_metadata(self.metadata)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MemoryEntry":
        return cls(
            id=str(row["id"]),
            content=str(row["content"]),
            type=str(row["type"]),
            session_id=None if row.get("session_id") is None else str(row["session_id"]),
            role=str(row["role"]),
            turn_index=int(row["turn_index"]),
            created_at=int(row["created_at"]),
            metadata=_load_metadata(row.get("metadata_json", row.get("metadata", "{}"))),
        )
