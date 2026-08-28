import json
from pathlib import Path
from typing import Any, Literal
import os
import tomllib
import warnings

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _sqlite_url_from_path(path: str) -> str:
    if path == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    return f"sqlite+aiosqlite:///{path}"


def _sqlite_path_from_url(url: str) -> str:
    prefix = "sqlite+aiosqlite:///"
    if url.startswith(prefix):
        return url[len(prefix) :]
    return url


class AppSettings(BaseModel):
    name: str = "MultiClaw"
    version: str = "0.1.0"
    debug: bool = False
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost",
            "http://localhost:5173",
            "http://127.0.0.1",
            "http://127.0.0.1:5173",
            "http://testserver",
        ]
    )


class DeploymentSettings(BaseModel):
    profile: Literal["standalone"] = "standalone"


class DatabaseSettings(BaseModel):
    driver: Literal["sqlite", "mysql"] = "sqlite"
    url: str = "sqlite+aiosqlite:///data/multiclaw.db"
    migration_mode: Literal["validate"] = "validate"
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=1, le=60000)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_path(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        migrated = dict(data)
        legacy_path = migrated.pop("path", None)
        if legacy_path is not None and "url" not in migrated:
            migrated["url"] = _sqlite_url_from_path(legacy_path)
            migrated.setdefault("driver", "sqlite")
        return migrated

    @model_validator(mode="after")
    def validate_driver_url(self) -> "DatabaseSettings":
        expected = {
            "sqlite": "sqlite+aiosqlite://",
            "mysql": "mysql+aiomysql://",
        }[self.driver]
        if not self.url.startswith(expected):
            raise ValueError("database.driver must match database.url")
        return self

    @property
    def path(self) -> str:
        if self.driver == "sqlite":
            return _sqlite_path_from_url(self.url)
        return self.url


class WorkspaceSettings(BaseModel):
    root: str = "data/workspaces"


class RuntimeSettings(BaseModel):
    max_resident_tenants: int = Field(default=32, ge=1, le=1024)
    idle_ttl_seconds: int = Field(default=900, ge=30)
    max_concurrent_runs_per_tenant: int = Field(default=2, ge=1, le=32)


class WorkflowSettings(BaseModel):
    heartbeat_ms: int = Field(default=5000, ge=1000)
    lease_ttl_ms: int = Field(default=20000, ge=5000)
    max_checkpoint_payload_bytes: int = Field(default=262144, ge=1024, le=1048576)

    @model_validator(mode="after")
    def validate_lease_ratio(self) -> "WorkflowSettings":
        if self.lease_ttl_ms < self.heartbeat_ms * 3:
            raise ValueError("workflow.lease_ttl_ms must be at least 3x heartbeat_ms")
        return self


class SecretSettings(BaseModel):
    allow_platform_fallback: bool = False
    keyring_file: str = ""


class DeletionSettings(BaseModel):
    retention_days: int = Field(default=7, ge=0, le=30, strict=True)


class LLMProviderSettings(BaseModel):
    api_key: str = ""
    base_url: str = ""


class LLMSettings(BaseModel):
    default_provider: str = "openai"
    default_model: str = "gpt-4o"
    providers: dict[str, dict[str, str]] = {}
    capability_tags: dict[str, list[str]] = {}


class MemorySettings(BaseModel):
    short_term_limit: int = 100
    context_window_limit: int = 128000
    recent_turns: int = 2
    context_history_ratio: float = 0.5
    include_legacy_memory_in_retrieval: bool = False
    progressive_context_enabled: bool = False
    context_response_reserve_tokens: int = Field(default=4096, ge=256)
    context_l1_ratio: float = Field(default=0.6, gt=0.0, lt=1.0)


class SandboxProfileNames(BaseModel):
    shell: str = "shell_workspace"
    code_exec: str = "code_exec_python"
    mcp_stdio: str = "mcp_stdio_local"


class MacOSSandboxSettings(BaseModel):
    seatbelt_profile_dir: str = ""


class LinuxSandboxSettings(BaseModel):
    nsjail_path: str = "/usr/bin/nsjail"
    nsjail_config_dir: str = ""


class SandboxSettings(BaseModel):
    mode: Literal["auto", "host_unsafe_dev_only"] = "auto"
    backend_probe_on_startup: bool = True
    unsafe_fallback_requires_debug: Literal[True] = True
    write_protected_workspace_paths: list[str] = Field(default_factory=lambda: [".git"])
    read_hidden_workspace_paths: list[str] = Field(default_factory=lambda: [".env", ".env.*"])
    profiles: SandboxProfileNames = Field(default_factory=SandboxProfileNames)
    macos: MacOSSandboxSettings = Field(default_factory=MacOSSandboxSettings)
    linux: LinuxSandboxSettings = Field(default_factory=LinuxSandboxSettings)


class GovernanceSettings(BaseModel):
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    audit_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_sandbox_mode(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        migrated = dict(data)
        legacy_mode = migrated.pop("sandbox_mode", None)
        if legacy_mode is None:
            return migrated

        if "sandbox" in migrated:
            raise ValueError("governance.sandbox_mode cannot be combined with governance.sandbox")

        if legacy_mode == "process":
            warnings.warn(
                "governance.sandbox_mode='process' is deprecated; using governance.sandbox.mode='auto'",
                DeprecationWarning,
                stacklevel=5,
            )
            migrated["sandbox"] = {"mode": "auto"}
            return migrated

        raise ValueError(
            f"Unsupported governance.sandbox_mode {legacy_mode!r}; use governance.sandbox.mode instead"
        )


class ToolSettings(BaseModel):
    parallel_read_only_enabled: bool = False
    parallel_max_concurrency: int = Field(default=4, ge=1, le=16)
    web_fetch_allow_private_networks: bool = False


class AgentSettings(BaseModel):
    max_tool_rounds: int = 10
    resilience_enabled: bool = False
    no_progress_repeat_limit: int = Field(default=3, ge=2, le=10)
    reflection_max_attempts: int = Field(default=1, ge=0, le=3)
    system_prompt: str = (
        "You are MultiClaw, an AI assistant with access to tools. "
        "You are powered by a large language model. "
        "Use web_search to find information, read_file to read files, and other "
        "tools to inspect directories and gather information. "
        "After receiving search results, summarize them directly for the user — "
        "do not fetch every linked page unless the user specifically asks for details. "
        "If a tool fails, try once more with a different approach; if it fails again, "
        "work with what you have and respond. "
        "When a tool requires user approval, inform the user briefly. "
        "If you cannot complete a task with the available tools, explain why "
        "and suggest alternatives."
    )


class SkillSettings(BaseModel):
    enabled: bool = True
    max_active: int = 5
    extra_dirs: list[str] = []
    user_dir: str = ""


class McpSettings(BaseModel):
    enabled: bool = True
    config_path: str = ""


class AuthSettings(BaseModel):
    jwt_signing_key_file: str = ""


class EmailSettings(BaseModel):
    provider: Literal["brevo", "resend"] = "brevo"


class BrevoSettings(BaseModel):
    api_key: str = ""
    sender_email: str = ""
    sender_name: str = "MultiClaw"
    mock: bool = False


class ResendSettings(BaseModel):
    api_key: str = ""
    sender_email: str = ""
    sender_name: str = "MultiClaw"
    mock: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MULTICLAW_",
        env_nested_delimiter="__",
    )

    app: AppSettings = Field(default_factory=AppSettings)
    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    secrets: SecretSettings = Field(default_factory=SecretSettings)
    deletion: DeletionSettings = Field(default_factory=DeletionSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    skill: SkillSettings = Field(default_factory=SkillSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    brevo: BrevoSettings = Field(default_factory=BrevoSettings)
    resend: ResendSettings = Field(default_factory=ResendSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)

    def __init__(self, _config_file: str | None = None, **kwargs: Any):
        config_path = Path(_config_file) if _config_file else Path("multiclaw.toml")
        if config_path.exists():
            toml_kwargs = self._build_toml_kwargs(config_path)
            kwargs = self._apply_env_overrides(toml_kwargs) | kwargs
        super().__init__(**kwargs)

    @model_validator(mode="after")
    def validate_unsafe_sandbox_mode_requires_debug(self) -> "Settings":
        if self.governance.sandbox.mode == "host_unsafe_dev_only" and not self.app.debug:
            raise ValueError("governance.sandbox.mode='host_unsafe_dev_only' requires app.debug=true")
        return self

    @staticmethod
    def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
        import copy
        result = copy.deepcopy(data)
        prefix = "MULTICLAW_"
        env_only_secret_keys = {
            "MULTICLAW_SECRETS_KEYRING_B64",
            "MULTICLAW_AUTH_JWT_SIGNING_KEY",
        }
        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue
            if key in env_only_secret_keys:
                continue
            path_parts = key[len(prefix):].lower().split("__")
            current = result
            for part in path_parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[path_parts[-1]] = Settings._coerce_env_value(value)
        return result

    @staticmethod
    def _coerce_env_value(value: str) -> Any:
        stripped = value.strip()
        lower = value.lower()
        if lower in ("true", "false"):
            return lower == "true"
        try:
            return int(value)
        except ValueError:
            pass
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        return value

    def _build_toml_kwargs(self, path: Path) -> dict[str, Any]:
        with open(path, "rb") as f:
            data = tomllib.load(f)

        result: dict[str, Any] = {}
        if "app" in data:
            result["app"] = data["app"]
        if "deployment" in data:
            result["deployment"] = data["deployment"]
        if "database" in data:
            result["database"] = data["database"]
        if "workspace" in data:
            result["workspace"] = data["workspace"]
        if "runtime" in data:
            result["runtime"] = data["runtime"]
        if "workflow" in data:
            result["workflow"] = data["workflow"]
        if "secrets" in data:
            result["secrets"] = data["secrets"]
        if "deletion" in data:
            result["deletion"] = data["deletion"]
        if "llm" in data:
            llm_data = dict(data["llm"])
            result["llm"] = {
                "providers": llm_data.pop("providers", {}),
                "capability_tags": llm_data.pop("capability_tags", {}),
                **llm_data,
            }
        if "memory" in data:
            result["memory"] = data["memory"]
        if "governance" in data:
            result["governance"] = data["governance"]
        if "tools" in data:
            result["tools"] = data["tools"]
        if "agent" in data:
            result["agent"] = data["agent"]
        if "skills" in data:
            result["skill"] = data["skills"]
        if "auth" in data:
            result["auth"] = data["auth"]
        if "email" in data:
            result["email"] = data["email"]
        if "brevo" in data:
            result["brevo"] = data["brevo"]
        if "resend" in data:
            result["resend"] = data["resend"]
        if "mcp" in data:
            result["mcp"] = data["mcp"]
        return result
