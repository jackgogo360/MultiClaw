from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

_SECRET_PATTERNS = (
    "*TOKEN*",
    "*SECRET*",
    "*PASSWORD*",
    "*API_KEY*",
    "*ACCESS_KEY*",
    "*PRIVATE_KEY*",
)
_REDACTED = "[REDACTED]"


class _ImmutableDict(dict):
    def _blocked(self, *args, **kwargs):
        del args, kwargs
        raise TypeError("mapping is immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked
    __ior__ = _blocked


def _freeze_mapping(mapping: dict) -> _ImmutableDict:
    return _ImmutableDict(dict(mapping))


def _is_secret_env_key(key: str) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(key.upper(), pattern) for pattern in _SECRET_PATTERNS)


def _redact_env_mapping(mapping: dict[str, str]) -> dict[str, str]:
    return {
        key: (_REDACTED if _is_secret_env_key(key) else value)
        for key, value in mapping.items()
    }


class SandboxExecRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_name: str
    profile_name: str
    mode: Literal["shell_string", "exec_argv"]
    command: str | None = None
    argv: tuple[str, ...] | None = None
    workspace_root: Path
    cwd: Path
    stdin_bytes: bytes | None = None
    timeout_seconds: float = Field(gt=0)
    env_overrides: dict[str, str] = Field(default_factory=dict, repr=False)
    allowed_secret_env: frozenset[str] = frozenset()
    network_mode: Literal["disabled", "inherit"] | None = None
    workspace_mode: Literal["ro", "rw"] | None = None
    allow_subprocesses: bool | None = None
    read_only_paths: tuple[Path, ...] = ()
    correlation_id: str = ""
    mcp_server_name: str | None = None

    @model_validator(mode="after")
    def validate_launch_payload(self) -> "SandboxExecRequest":
        if self.mode == "shell_string":
            valid_payload = self.command is not None and self.argv is None
        else:
            valid_payload = self.argv is not None and self.command is None

        if not valid_payload:
            raise ValueError("exactly one launch payload must match mode")
        if len(self.read_only_paths) > 16:
            raise ValueError("read_only_paths cannot exceed 16 entries")
        object.__setattr__(self, "env_overrides", _freeze_mapping(self.env_overrides))
        return self

    @field_serializer("env_overrides", when_used="always")
    def serialize_env_overrides(self, value: dict[str, str]) -> dict[str, str]:
        return _redact_env_mapping(value)


class SandboxEnvironment(BaseModel):
    model_config = ConfigDict(frozen=True)

    env: dict[str, str] = Field(repr=False)
    private_root: Path
    home: Path
    tmp: Path

    @model_validator(mode="after")
    def freeze_env(self) -> "SandboxEnvironment":
        object.__setattr__(self, "env", _freeze_mapping(self.env))
        return self

    @field_serializer("env", when_used="always")
    def serialize_env(self, value: dict[str, str]) -> dict[str, str]:
        return _redact_env_mapping(value)


class SandboxProfilePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    workspace_mode: Literal["ro", "rw"]
    network_mode: Literal["disabled", "inherit"]
    allow_subprocesses: bool
    entrypoints: tuple[Path, ...]
    runtime_read_only_paths: tuple[Path, ...] = ()
    write_protected_patterns: tuple[str, ...] = (".git",)
    read_hidden_patterns: tuple[str, ...] = (".env", ".env.*")


class SandboxedLaunchSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    executable: str
    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = Field(repr=False)
    stdin_bytes: bytes | None
    private_root: Path
    backend_name: str
    profile_name: str
    correlation_id: str
    unsafe_fallback_used: bool = False

    @model_validator(mode="after")
    def freeze_env(self) -> "SandboxedLaunchSpec":
        object.__setattr__(self, "env", _freeze_mapping(self.env))
        return self

    @field_serializer("env", when_used="always")
    def serialize_env(self, value: dict[str, str]) -> dict[str, str]:
        return _redact_env_mapping(value)


class SandboxExecResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_code: int | None
    timed_out: bool
    signal: str | None
    stdout: bytes
    stderr: bytes
    backend_name: str
    profile_name: str
    unsafe_fallback_used: bool = False


class SandboxProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    backend_name: str
    available: bool
    capabilities: dict[str, bool]
    reason: str = ""

    @model_validator(mode="after")
    def freeze_capabilities(self) -> "SandboxProbeResult":
        object.__setattr__(self, "capabilities", _freeze_mapping(self.capabilities))
        return self


class SandboxReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool
    mode: Literal["auto", "host_unsafe_dev_only"]
    backend_name: str
    probe: SandboxProbeResult
    profiles: dict[str, bool]
    skipped_capabilities: dict[str, str]
    unsafe_fallback_active: bool = False

    @model_validator(mode="after")
    def freeze_mappings(self) -> "SandboxReadiness":
        object.__setattr__(self, "profiles", _freeze_mapping(self.profiles))
        object.__setattr__(
            self,
            "skipped_capabilities",
            _freeze_mapping(self.skipped_capabilities),
        )
        return self
