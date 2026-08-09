"""Streamable HTTP Transport — MCP 2025-03-26 规范"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Optional

from mcp.client.streamable_http import streamablehttp_client

from .base import BaseTransport

logger = logging.getLogger(__name__)

if sys.version_info >= (3, 11):
    from builtins import BaseExceptionGroup
else:
    try:
        from exceptiongroup import BaseExceptionGroup
    except ImportError:
        BaseExceptionGroup = BaseException  # type: ignore[assignment,misc]

_CONNECT_TIMEOUT = 30.0


class StreamableHTTPTransport(BaseTransport):

    def __init__(self, url: str, headers: Optional[dict[str, str]] = None) -> None:
        super().__init__()
        self._url = url
        self._headers = headers or {}
        self._context: Any = None

    async def connect(self) -> "StreamableHTTPTransport":
        self._context = streamablehttp_client(
            self._url,
            headers=self._headers or None,
            terminate_on_close=True,
        )
        try:
            result = await asyncio.wait_for(
                self._context.__aenter__(),
                timeout=_CONNECT_TIMEOUT,
            )
            self._set_streams(result[0], result[1])
        except asyncio.TimeoutError:
            await self._force_close()
            raise ConnectionError(f"HTTP transport connection timed out after {_CONNECT_TIMEOUT}s")
        logger.debug("HTTP transport connected: %s", self._url)
        return self

    async def disconnect(self) -> None:
        if self._context:
            await asyncio.sleep(0.1)
            try:
                await self._context.__aexit__(None, None, None)
            except (RuntimeError, asyncio.CancelledError, BaseExceptionGroup):
                pass
            finally:
                self._context = None
        self._clear_streams()
        logger.debug("HTTP transport disconnected")

    async def _force_close(self) -> None:
        if self._context:
            try:
                await self._context.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self._context = None
        self._clear_streams()
