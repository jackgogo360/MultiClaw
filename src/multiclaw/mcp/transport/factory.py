"""Transport 工厂 — 根据配置创建对应的 transport 实例"""

from __future__ import annotations

from ..types import (
    HTTPServerConfig,
    InProcessServerConfig,
    SSEServerConfig,
    ServerConfig,
    StdioServerConfig,
    WebSocketServerConfig,
)
from .base import BaseTransport
from .http import StreamableHTTPTransport
from .in_process import InProcessTransport
from .sse import SSETransport
from .stdio import StdioTransport
from .ws import WebSocketTransport


def create_transport(config: ServerConfig) -> BaseTransport:
    match config:
        case StdioServerConfig():
            return StdioTransport(
                command=config.command,
                args=config.args,
                env=config.env,
            )
        case SSEServerConfig():
            return SSETransport(
                url=config.url,
                headers=config.headers,
            )
        case HTTPServerConfig():
            return StreamableHTTPTransport(
                url=config.url,
                headers=config.headers,
            )
        case WebSocketServerConfig():
            return WebSocketTransport(
                url=config.url,
                headers=config.headers,
            )
        case InProcessServerConfig():
            return InProcessTransport()
        case _:
            raise ValueError(f"Unknown server config type: {type(config)}")
