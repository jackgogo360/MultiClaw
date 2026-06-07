"""配置加载 — 从 JSON 文件加载 MCP 服务器配置"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

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
            server_data = _expand_env_vars(server_data)
            config = _parse_server_config(server_data)
            configs[name] = config
        except Exception as e:
            logger.warning("Failed to parse server '%s': %s", name, e)
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
        return StdioServerConfig(
            command=data["command"],
            args=data.get("args", []),
            env=data.get("env", {}),
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
