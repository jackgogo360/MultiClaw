from pathlib import Path
from copy import deepcopy
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

    def __deepcopy__(self, memo):
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        copied = _ImmutableDict(deepcopy(dict(self), memo))
        memo[id(self)] = copied
        return copied

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


class _SandboxModel(BaseModel):
    def model_copy(self, *, update: dict | None = None, deep: bool = False):
        data = {
            field_name: getattr(self, field_name)
            for field_name in type(self).model_fields
        }
        if deep:
            data = deepcopy(data)
        if update:
            data.update(update)
        return type(self).model_validate(data)


class SandboxExecRequest(_SandboxModel):
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


class SandboxEnvironment(_SandboxModel):
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


class SandboxProfilePolicy(_SandboxModel):
    model_config = ConfigDict(frozen=True)

    name: str
    profile_kind: Literal["shell", "code_exec", "mcp_stdio"] = "shell"
    workspace_mode: Literal["ro", "rw"]
    network_mode: Literal["disabled", "inherit"]
    allow_subprocesses: bool
    entrypoints: tuple[Path, ...]
    runtime_read_only_paths: tuple[Path, ...] = ()
    write_protected_patterns: tuple[str, ...] = (".git",)
    read_hidden_patterns: tuple[str, ...] = (".env", ".env.*")


class SandboxedLaunchSpec(_SandboxModel):
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


class SandboxExecResult(_SandboxModel):
    model_config = ConfigDict(frozen=True)

    exit_code: int | None
    timed_out: bool
    completion_state: (
        Literal["completed", "timed_out", "output_limit_exceeded"] | None
    ) = None
    output_limit_stream: Literal["stdout", "stderr"] | None = None
    signal: str | None
    stdout: bytes
    stderr: bytes
    backend_name: str
    profile_name: str
    unsafe_fallback_used: bool = False

    @model_validator(mode="after")
    def validate_completion_state(self) -> "SandboxExecResult":
        completion_state = self.completion_state
        if completion_state is None:
            completion_state = "timed_out" if self.timed_out else "completed"

        if completion_state == "completed":
            if self.timed_out:
                raise ValueError("timed_out must be false when completion_state is completed")
            if self.output_limit_stream is not None:
                raise ValueError(
                    "output_limit_stream must be omitted unless completion_state is output_limit_exceeded"
                )
        elif completion_state == "timed_out":
            if not self.timed_out:
                raise ValueError("timed_out must be true when completion_state is timed_out")
            if self.output_limit_stream is not None:
                raise ValueError(
                    "output_limit_stream must be omitted unless completion_state is output_limit_exceeded"
                )
        else:
            if self.timed_out:
                raise ValueError(
                    "timed_out must be false when completion_state is output_limit_exceeded"
                )
            if self.output_limit_stream is None:
                raise ValueError(
                    "output_limit_stream is required when completion_state is output_limit_exceeded"
                )

        object.__setattr__(self, "completion_state", completion_state)
        return self


class SandboxProbeResult(_SandboxModel):
    model_config = ConfigDict(frozen=True)

    backend_name: str
    available: bool
    capabilities: dict[str, bool]
    reason: str = ""

    @model_validator(mode="after")
    def freeze_capabilities(self) -> "SandboxProbeResult":
        object.__setattr__(self, "capabilities", _freeze_mapping(self.capabilities))
        return self


class SandboxReadiness(_SandboxModel):
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
