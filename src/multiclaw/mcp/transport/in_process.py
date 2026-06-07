"""In-Process Transport — 内存传输对，避免子进程开销"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from .base import BaseTransport

logger = logging.getLogger(__name__)


class InProcessTransport(BaseTransport):
    """进程内传输，通过内存流对通信。

    使用 create_linked_transport_pair() 创建配对的两端。
    """

    def __init__(self) -> None:
        super().__init__()
        self._peer: Optional["InProcessTransport"] = None
        self._closed = False
        self._incoming_send: Any = None
        self._outgoing_recv: Any = None

    def _set_peer(self, peer: "InProcessTransport") -> None:
        self._peer = peer

    async def connect(self) -> "InProcessTransport":
        if not self._peer:
            raise RuntimeError("InProcessTransport must be created via create_linked_transport_pair()")

        incoming_send, incoming_recv = anyio.create_memory_object_stream(max_buffer_size=100)
        outgoing_send, outgoing_recv = anyio.create_memory_object_stream(max_buffer_size=100)

        self._incoming_send = incoming_send
        self._outgoing_recv = outgoing_recv

        self._peer._incoming_send = outgoing_send
        self._peer._outgoing_recv = incoming_recv

        self._set_streams(incoming_recv, outgoing_send)
        self._peer._set_streams(outgoing_recv, incoming_send)

        logger.debug("InProcess transport pair connected")
        return self

    async def disconnect(self) -> None:
        self._closed = True
        if self._incoming_send:
            await self._incoming_send.aclose()
        if self._outgoing_recv:
            await self._outgoing_recv.aclose()
        self._clear_streams()
        if self._peer and not self._peer._closed:
            await self._peer.disconnect()
        logger.debug("InProcess transport disconnected")


def create_linked_transport_pair() -> tuple[InProcessTransport, InProcessTransport]:
    a = InProcessTransport()
    b = InProcessTransport()
    a._set_peer(b)
    b._set_peer(a)
    return a, b
