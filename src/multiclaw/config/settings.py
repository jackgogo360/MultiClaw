from pathlib import Path
from typing import Any
import os
import tomllib

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseModel):
    name: str = "MultiClaw"
    version: str = "0.1.0"
    debug: bool = False


class DatabaseSettings(BaseModel):
    path: str = "data/multiclaw.db"


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
    recent_turns: int = 8
    context_history_ratio: float = 0.5
    include_legacy_memory_in_retrieval: bool = False


class GovernanceSettings(BaseModel):
    sandbox_mode: str = "process"
    audit_enabled: bool = True


class AgentSettings(BaseModel):
    max_tool_rounds: int = 10
    system_prompt: str = (
        "You are a helpful assistant with access to tools. "
        "Use tools to read files, inspect directories, and gather information. "
        "When a tool requires user approval, inform the user briefly. "
        "After receiving tool results, summarize them clearly for the user. "
        "If you cannot complete a task with the available tools, explain why "
        "and suggest alternatives."
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MULTICLAW_",
        env_nested_delimiter="__",
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)

    def __init__(self, _config_file: str | None = None, **kwargs: Any):
        config_path = Path(_config_file) if _config_file else Path("multiclaw.toml")
        if config_path.exists():
            toml_kwargs = self._build_toml_kwargs(config_path)
            kwargs = self._apply_env_overrides(toml_kwargs) | kwargs
        super().__init__(**kwargs)

    @staticmethod
    def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
        import copy
        result = copy.deepcopy(data)
        prefix = "MULTICLAW_"
        for key, value in os.environ.items():
            if not key.startswith(prefix):
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
        lower = value.lower()
        if lower in ("true", "false"):
            return lower == "true"
        try:
            return int(value)
        except ValueError:
            pass
        return value
    def _build_toml_kwargs(self, path: Path) -> dict[str, Any]:
        with open(path, "rb") as f:
            data = tomllib.load(f)

        result: dict[str, Any] = {}
        if "app" in data:
            result["app"] = data["app"]
        if "database" in data:
            result["database"] = data["database"]
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
        if "agent" in data:
            result["agent"] = data["agent"]
        return result
