"""Transport 抽象基类"""

from __future__ import annotations

import abc
from typing import Any, Optional

from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream


MCPReadStream = MemoryObjectReceiveStream
MCPWriteStream = MemoryObjectSendStream


class BaseTransport(abc.ABC):
    """所有 MCP transport 的抽象基类。

    子类需实现 connect/disconnect 和 async context manager 协议。
    流的生命周期通过 _set_streams/_clear_streams 管理。
    """

    def __init__(self) -> None:
        self._read_stream: Optional[MCPReadStream] = None
        self._write_stream: Optional[MCPWriteStream] = None
        self._connected: bool = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def read_stream(self) -> MCPReadStream:
        if not self._read_stream:
            raise RuntimeError("Transport not connected")
        return self._read_stream

    @property
    def write_stream(self) -> MCPWriteStream:
        if not self._write_stream:
            raise RuntimeError("Transport not connected")
        return self._write_stream

    @abc.abstractmethod
    async def connect(self) -> "BaseTransport":
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        ...

    def scrub_sensitive_state(self) -> None:
        return None

    async def __aenter__(self) -> "BaseTransport":
        return await self.connect()

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()

    def _set_streams(self, read: MCPReadStream, write: MCPWriteStream) -> None:
        self._read_stream = read
        self._write_stream = write
        self._connected = True

    def _clear_streams(self) -> None:
        self._read_stream = None
        self._write_stream = None
        self._connected = False
