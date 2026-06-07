"""MCP 客户端类型定义"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class TransportType(enum.Enum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"
    WS = "ws"
    IN_PROCESS = "in_process"


class ServerStatus(enum.Enum):
    PENDING = "pending"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    NEEDS_AUTH = "needs_auth"


@dataclass
class StdioServerConfig:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport_type: TransportType = field(default=TransportType.STDIO, init=False)


@dataclass
class SSEServerConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    transport_type: TransportType = field(default=TransportType.SSE, init=False)


@dataclass
class HTTPServerConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    oauth: Optional[OAuthConfig] = None
    transport_type: TransportType = field(default=TransportType.HTTP, init=False)


@dataclass
class WebSocketServerConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    transport_type: TransportType = field(default=TransportType.WS, init=False)


@dataclass
class InProcessServerConfig:
    server_factory: Callable
    transport_type: TransportType = field(default=TransportType.IN_PROCESS, init=False)


ServerConfig = StdioServerConfig | SSEServerConfig | HTTPServerConfig | WebSocketServerConfig | InProcessServerConfig


@dataclass
class OAuthConfig:
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    auth_url: Optional[str] = None
    token_url: Optional[str] = None
    scopes: list[str] = field(default_factory=list)
    callback_port: int = 8085
    metadata_url: Optional[str] = None


@dataclass
class ToolInfo:
    name: str
    server_name: str
    original_name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False

    @property
    def qualified_name(self) -> str:
        return f"mcp__{_sanitize(self.server_name)}__{_sanitize(self.original_name)}"


@dataclass
class ToolCallResult:
    content: list[dict[str, Any]]
    is_error: bool = False


@dataclass
class ServerState:
    name: str
    config: ServerConfig
    status: ServerStatus = ServerStatus.PENDING
    error: Optional[str] = None
    tools: list[ToolInfo] = field(default_factory=list)


ToolFilter = Callable[[ToolInfo], bool]
ElicitationHandler = Callable[[str, dict[str, Any]], Optional[str]]
SamplingHandler = Callable[[dict[str, Any]], dict[str, Any]]


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name)
