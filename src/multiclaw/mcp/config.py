"""配置加载 — 从 JSON 文件加载 MCP 服务器配置"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
)

from .types import (
    HTTPServerConfig,
    InProcessServerConfig,
    OAuthConfig,
    SSEServerConfig,
    ServerConfig,
    StdioServerConfig,
    WebSocketServerConfig,
)

logger = logging.getLogger(__name__)

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

DEFAULT_CONFIG_PATHS = [
    Path.home() / ".mcp.json",
    Path(".mcp.json"),
]


class _StdioServerConfigInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: StrictStr
    args: list[StrictStr] = Field(default_factory=list)
    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    cwd: StrictStr | None = None
    sandbox_network: Literal["disabled", "inherit"] = Field(
        default="disabled",
        validation_alias=AliasChoices("sandbox_network", "sandboxNetwork"),
    )
    sandbox_workspace: Literal["ro", "rw"] = Field(
        default="ro",
        validation_alias=AliasChoices("sandbox_workspace", "sandboxWorkspace"),
    )
    sandbox_allow_subprocesses: StrictBool = Field(
        default=False,
        validation_alias=AliasChoices("sandbox_allow_subprocesses", "sandboxAllowSubprocesses"),
    )
    sandbox_env_allowlist: list[StrictStr] = Field(
        default_factory=list,
        validation_alias=AliasChoices("sandbox_env_allowlist", "sandboxEnvAllowlist"),
    )
    sandbox_read_only_paths: list[StrictStr] = Field(
        default_factory=list,
        validation_alias=AliasChoices("sandbox_read_only_paths", "sandboxReadOnlyPaths"),
    )

    @field_validator("args", "sandbox_env_allowlist", "sandbox_read_only_paths", mode="before")
    @classmethod
    def _validate_list_shape(cls, value: object) -> object:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("must be a list")
        return value

    @field_validator("env", mode="before")
    @classmethod
    def _validate_env_shape(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("must be an object")
        return value


def _normalize_config_key(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _sanitize_config_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        field_name = ".".join(
            _normalize_config_key(str(part))
            for part in exc.errors()[0].get("loc", ())
        ) or "config"
        return f"invalid config key {field_name}"
    return str(exc)


def load_mcp_config(
    path: Optional[str | Path] = None,
    *,
    search_parents: bool = True,
) -> dict[str, ServerConfig]:
    if path:
        return _load_from_file(Path(path))

    configs: dict[str, ServerConfig] = {}
    for config_path in _find_config_files(search_parents):
        try:
            file_configs = _load_from_file(config_path)
            for name, config in file_configs.items():
                if name not in configs:
                    configs[name] = config
        except Exception as e:
            logger.warning("Failed to load config from %s: %s", config_path, e)
    return configs


def _find_config_files(search_parents: bool) -> list[Path]:
    found = []
    for p in DEFAULT_CONFIG_PATHS:
        if p.exists():
            found.append(p)

    if search_parents:
        cwd = Path.cwd()
        for parent in [cwd, *cwd.parents]:
            candidate = parent / ".mcp.json"
            if candidate.exists() and candidate not in found:
                found.append(candidate)
            if (parent / ".git").exists():
                break
    return found


def _load_from_file(path: Path) -> dict[str, ServerConfig]:
    raw = json.loads(path.read_text())
    servers = raw.get("mcpServers", raw.get("servers", {}))
    configs: dict[str, ServerConfig] = {}

    for name, server_data in servers.items():
        try:
            _validate_raw_stdio_env_expansions(server_data)
            server_data = _expand_env_vars(server_data)
            config = _parse_server_config(server_data)
            configs[name] = config
        except Exception as e:
            logger.warning("Failed to parse server '%s': %s", name, _sanitize_config_error(e))
    return configs


def _parse_server_config(data: dict[str, Any]) -> ServerConfig:
    transport_type = data.get("type", "").lower()

    if transport_type == "sse":
        return SSEServerConfig(
            url=data["url"],
            headers=data.get("headers", {}),
        )
    elif transport_type == "http":
        oauth_data = data.get("oauth")
        oauth = _parse_oauth(oauth_data) if oauth_data else None
        return HTTPServerConfig(
            url=data["url"],
            headers=data.get("headers", {}),
            oauth=oauth,
        )
    elif transport_type == "ws":
        return WebSocketServerConfig(
            url=data["url"],
            headers=data.get("headers", {}),
        )
    elif "command" in data:
        try:
            parsed = _StdioServerConfigInput.model_validate(data)
        except ValidationError as exc:
            raise ValueError(_sanitize_config_error(exc)) from exc
        return StdioServerConfig(
            command=parsed.command,
            args=list(parsed.args),
            env=dict(parsed.env),
            cwd=Path(parsed.cwd) if parsed.cwd is not None else None,
            sandbox_network=parsed.sandbox_network,
            sandbox_workspace=parsed.sandbox_workspace,
            sandbox_allow_subprocesses=parsed.sandbox_allow_subprocesses,
            sandbox_env_allowlist=list(parsed.sandbox_env_allowlist),
            sandbox_read_only_paths=[Path(path) for path in parsed.sandbox_read_only_paths],
        )
    elif "url" in data:
        oauth_data = data.get("oauth")
        oauth = _parse_oauth(oauth_data) if oauth_data else None
        return HTTPServerConfig(
            url=data["url"],
            headers=data.get("headers", {}),
            oauth=oauth,
        )
    else:
        raise ValueError(f"Cannot determine transport type from config: {data}")


def _parse_oauth(data: dict[str, Any]) -> OAuthConfig:
    return OAuthConfig(
        client_id=data.get("clientId") or data.get("client_id"),
        client_secret=data.get("clientSecret") or data.get("client_secret"),
        auth_url=data.get("authUrl") or data.get("auth_url") or data.get("authorization_endpoint"),
        token_url=data.get("tokenUrl") or data.get("token_url") or data.get("token_endpoint"),
        scopes=data.get("scopes", []),
        callback_port=data.get("callbackPort", data.get("callback_port", 8085)),
        metadata_url=data.get("metadataUrl") or data.get("metadata_url") or data.get("authServerMetadataUrl"),
    )


def load_mcp_tools_config(
    path: Optional[str | Path] = None,
    *,
    search_parents: bool = True,
) -> dict[str, dict[str, list[str]]]:
    """Load per-server tool filter config from .mcp.json.

    Returns {server_name: {"include": [...], "exclude": [...]}}
    """
    paths = [Path(path)] if path else _find_config_files(search_parents)
    result: dict[str, dict[str, list[str]]] = {}
    for config_path in paths:
        try:
            raw = json.loads(config_path.read_text())
            servers = raw.get("mcpServers", raw.get("servers", {}))
            for name, server_data in servers.items():
                tools = server_data.get("tools", {})
                if isinstance(tools, dict):
                    inc = tools.get("include", [])
                    exc = tools.get("exclude", [])
                    if inc or exc:
                        result[name] = {
                            "include": inc if isinstance(inc, list) else [],
                            "exclude": exc if isinstance(exc, list) else [],
                        }
        except Exception as e:
            logger.warning("Failed to load tools config from %s: %s", config_path, e)
    return result


def _matches_tool_filter(tool_name: str, tool_config: dict[str, list[str]] | None) -> bool:
    """Check if a tool name passes the include/exclude filter."""
    if not tool_config:
        return True
    include = tool_config.get("include", [])
    exclude = tool_config.get("exclude", [])
    if include and not any(tool_name.startswith(prefix) for prefix in include):
        return False
    if exclude and any(tool_name.startswith(prefix) for prefix in exclude):
        return False
    return True


def _expand_env_vars(data: Any) -> Any:
    if isinstance(data, str):
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))
        return _ENV_VAR_PATTERN.sub(_replace, data)
    elif isinstance(data, dict):
        return {k: _expand_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_expand_env_vars(item) for item in data]
    return data


def _validate_raw_stdio_env_expansions(server_data: Any) -> None:
    if not isinstance(server_data, dict) or "command" not in server_data:
        return

    raw_env = server_data.get("env")
    if not isinstance(raw_env, dict):
        return

    raw_allowlist = (
        server_data.get("sandbox_env_allowlist")
        if "sandbox_env_allowlist" in server_data
        else server_data.get("sandboxEnvAllowlist")
    )
    allowlist = (
        {entry for entry in raw_allowlist if isinstance(entry, str)}
        if isinstance(raw_allowlist, list)
        else None
    )

    for dest_key, raw_value in raw_env.items():
        if not isinstance(dest_key, str) or not isinstance(raw_value, str):
            continue
        matches = list(_ENV_VAR_PATTERN.finditer(raw_value))
        if not matches:
            continue

        full_match = _ENV_VAR_PATTERN.fullmatch(raw_value)
        if full_match is None or len(matches) != 1:
            raise ValueError(f"invalid secret env expansion for {dest_key}: exact whole-value reference required")

        source_key = full_match.group(1)
        if dest_key != source_key:
            raise ValueError(
                f"invalid secret env expansion for {dest_key}: destination must match source {source_key}"
            )
        if allowlist is None or source_key not in allowlist:
            raise ValueError(
                f"invalid secret env expansion for {source_key}: sandbox_env_allowlist entry required"
            )
