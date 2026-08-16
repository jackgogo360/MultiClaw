from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import os
import stat
from pathlib import Path
import secrets
import uuid
from typing import Any, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, Field


SESSION_COOKIE_NAME = "token"
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
SESSION_TOKEN_AUDIENCE = "multiclaw-api"
DELETION_RECOVERY_TOKEN_AUDIENCE = "multiclaw-deletion-recovery"
LOGIN_CODE_PURPOSE = "login"
DELETION_RECOVERY_CODE_PURPOSE = "deletion_recovery"
VERIFICATION_CODE_KEY_CONTEXT = b"multiclaw.verification-code-key.v1"
SESSION_TTL_SECONDS = 10 * 24 * 60 * 60
VERIFICATION_CODE_TTL_SECONDS = 15 * 60
DELETION_RECOVERY_TOKEN_TTL_SECONDS = 10 * 60
RECENT_AUTH_MAX_AGE_SECONDS = 5 * 60
MAX_SENDS_PER_DAY = 3
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost",
    "http://localhost:5173",
    "http://127.0.0.1",
    "http://127.0.0.1:5173",
    "http://testserver",
)


class AuthConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthRuntime:
    signing_key: bytes
    allowed_origins: frozenset[str]

    async def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class VerificationCodeIssue:
    code: str
    code_digest: str
    purpose: str
    email: str


@dataclass(frozen=True, slots=True)
class VerificationCodeRecord:
    id: str
    email: str
    code_digest: str
    purpose: str
    expires_at: int
    used_at: int | None
    created_at: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "VerificationCodeRecord":
        return cls(
            id=str(row["id"]),
            email=str(row["email"]),
            code_digest=str(row["code_digest"]),
            purpose=str(row["purpose"]),
            expires_at=int(row["expires_at"]),
            used_at=None if row["used_at"] is None else int(row["used_at"]),
            created_at=int(row["created_at"]),
        )


class User(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    email: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class VerificationCode(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    email: str
    code_digest: str
    purpose: str = LOGIN_CODE_PURPOSE
    expires_at: datetime
    used_at: datetime | None = None
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


def build_auth_runtime(settings, *, environ: Mapping[str, str] | None = None) -> AuthRuntime:
    app_settings = getattr(settings, "app", None)
    raw_origins = getattr(app_settings, "allowed_origins", DEFAULT_ALLOWED_ORIGINS)
    return AuthRuntime(
        signing_key=load_jwt_signing_key(settings, environ=environ),
        allowed_origins=frozenset(
            normalize_origin(origin) for origin in raw_origins
        ),
    )


def load_jwt_signing_key(settings, *, environ: Mapping[str, str] | None = None) -> bytes:
    source_env = (environ or os.environ).get("MULTICLAW_AUTH_JWT_SIGNING_KEY", "")
    source_file = str(getattr(getattr(settings, "auth", None), "jwt_signing_key_file", "") or "").strip()
    if bool(source_env) == bool(source_file):
        raise AuthConfigurationError("auth signing key requires exactly one configured source")

    raw = source_env.encode("utf-8") if source_env else _load_secret_file(Path(source_file))
    if len(raw) < 32:
        raise AuthConfigurationError("auth signing key must contain at least 32 bytes")
    return raw


def normalize_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        raise AuthConfigurationError(f"invalid allowed origin: {value!r}")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def derive_verification_code_digest(signing_key: bytes, *, purpose: str, email: str, code: str) -> str:
    normalized_email = email.strip().lower().encode("utf-8")
    digest_key = hmac.digest(signing_key, VERIFICATION_CODE_KEY_CONTEXT, "sha256")
    message = b"\0".join((purpose.encode("utf-8"), normalized_email, code.encode("utf-8")))
    return hmac.new(digest_key, message, "sha256").hexdigest()


def issue_verification_code(
    signing_key: bytes,
    *,
    email: str,
    purpose: str = LOGIN_CODE_PURPOSE,
    forced_code: str | None = None,
) -> VerificationCodeIssue:
    code = forced_code or f"{secrets.randbelow(1_000_000):06d}"
    return VerificationCodeIssue(
        code=code,
        code_digest=derive_verification_code_digest(
            signing_key,
            purpose=purpose,
            email=email,
            code=code,
        ),
        purpose=purpose,
        email=email.strip().lower(),
    )


def digests_match(expected: str, actual: str) -> bool:
    return hmac.compare_digest(expected, actual)


def _load_secret_file(path: Path) -> bytes:
    fd: int | None = None
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            raise AuthConfigurationError("auth signing key file cannot be read safely")
        fd = os.open(
            os.fspath(path),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
        )
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise AuthConfigurationError("auth signing key file is unavailable")
        mode = stat.S_IMODE(file_stat.st_mode)
        if mode & 0o077:
            raise AuthConfigurationError("auth signing key file permissions are too broad")
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = None
            return handle.read(65536)
    except OSError as exc:
        raise AuthConfigurationError("auth signing key file cannot be read") from exc
    finally:
        if fd is not None:
            os.close(fd)
