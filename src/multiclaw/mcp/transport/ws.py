"""WebSocket Transport"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from .base import BaseTransport

logger = logging.getLogger(__name__)


class WebSocketTransport(BaseTransport):

    def __init__(self, url: str, headers: Optional[dict[str, str]] = None) -> None:
        super().__init__()
        self._url = url
        self._headers = headers or {}
        self._ws: Any = None
        self._task_group: Any = None
        self._send_stream_writer: Any = None

    async def connect(self) -> "WebSocketTransport":
        try:
            import websockets
        except ImportError:
            raise ImportError("websockets package required for WebSocket transport: pip install websockets")

        self._ws = await websockets.connect(
            self._url,
            additional_headers=self._headers,
        )

        read_send, read_recv = anyio.create_memory_object_stream(max_buffer_size=100)
        write_send, write_recv = anyio.create_memory_object_stream(max_buffer_size=100)

        self._send_stream_writer = write_send
        self._set_streams(read_recv, write_send)

        self._read_task = asyncio.create_task(self._read_loop(read_send))
        self._write_task = asyncio.create_task(self._write_loop(write_recv))

        logger.debug("WebSocket transport connected: %s", self._url)
        return self

    async def _read_loop(self, send_stream: MemoryObjectSendStream) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                from mcp.types import JSONRPCMessage
                parsed = JSONRPCMessage.model_validate_json(message)
                await send_stream.send(parsed)
        except Exception as e:
            logger.debug("WebSocket read loop ended: %s", e)
        finally:
            await send_stream.aclose()

    async def _write_loop(self, recv_stream: MemoryObjectReceiveStream) -> None:
        try:
            async for message in recv_stream:
                data = message.model_dump_json()
                await self._ws.send(data)
        except Exception as e:
            logger.debug("WebSocket write loop ended: %s", e)

    async def disconnect(self) -> None:
        if hasattr(self, "_read_task") and self._read_task:
            self._read_task.cancel()
        if hasattr(self, "_write_task") and self._write_task:
            self._write_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._clear_streams()
        logger.debug("WebSocket transport disconnected")
