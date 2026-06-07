"""SSE Transport — Server-Sent Events 传输"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from mcp.client.sse import sse_client

from .base import BaseTransport

logger = logging.getLogger(__name__)


class SSETransport(BaseTransport):

    def __init__(self, url: str, headers: Optional[dict[str, str]] = None) -> None:
        super().__init__()
        self._url = url
        self._headers = headers or {}
        self._context: Any = None

    async def connect(self) -> "SSETransport":
        self._context = sse_client(self._url, headers=self._headers or None)
        streams = await self._context.__aenter__()
        self._set_streams(streams[0], streams[1])
        logger.debug("SSE transport connected: %s", self._url)
        return self

    async def disconnect(self) -> None:
        if self._context:
            try:
                await self._context.__aexit__(None, None, None)
            except (RuntimeError, asyncio.CancelledError):
                pass
            finally:
                self._context = None
        self._clear_streams()
        logger.debug("SSE transport disconnected")
