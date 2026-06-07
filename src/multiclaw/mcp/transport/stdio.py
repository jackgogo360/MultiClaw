"""Stdio Transport — 通过子进程 stdin/stdout 通信"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from .base import BaseTransport

logger = logging.getLogger(__name__)

_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})


def _build_safe_env(user_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_KEYS or key.startswith("XDG_"):
            env[key] = value
    if user_env:
        env.update(user_env)
    return env


class StdioTransport(BaseTransport):

    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        super().__init__()
        self._command = command
        self._args = args or []
        self._env = env
        self._context: Any = None

    async def connect(self) -> "StdioTransport":
        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=_build_safe_env(self._env),
        )
        self._context = stdio_client(params)
        streams = await self._context.__aenter__()
        self._set_streams(streams[0], streams[1])
        logger.debug("Stdio transport connected: %s %s", self._command, self._args)
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
        logger.debug("Stdio transport disconnected")
