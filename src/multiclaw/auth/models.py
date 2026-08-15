from datetime import datetime, timezone
import uuid
from typing import Any, Mapping

from pydantic import BaseModel, Field


class User(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    email: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VerificationCode(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    email: str
    code: str
    expires_at: datetime
    used: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UserRecord(BaseModel):
    id: str
    email: str
    status: str
    default_workspace_id: str | None = None
    auth_epoch: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "UserRecord":
        return cls(
            id=str(row["id"]),
            email=str(row["email"]),
            status=str(row["status"]),
            default_workspace_id=(
                None if row["default_workspace_id"] is None else str(row["default_workspace_id"])
            ),
            auth_epoch=int(row["auth_epoch"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


class WorkspaceRecord(BaseModel):
    id: str
    tenant_id: str
    slug: str
    name: str
    status: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "WorkspaceRecord":
        return cls(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            slug=str(row["slug"]),
            name=str(row["name"]),
            status=str(row["status"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


class SendCodeRequest(BaseModel):
    email: str


class VerifyRequest(BaseModel):
    email: str
    code: str


class AuthResponse(BaseModel):
    ok: bool = True


class MeResponse(BaseModel):
    email: str | None = None
    user_id: str | None = None
