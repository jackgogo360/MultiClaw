from datetime import datetime, timezone
import uuid

from pydantic import BaseModel, Field


class User(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    email: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VerificationCode(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    email: str
    code: str
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    used: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
