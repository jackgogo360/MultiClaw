"""Stdio Transport — 通过子进程 stdin/stdout 通信"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any, Optional

from mcp import StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from multiclaw.governance.sandbox.models import SandboxedLaunchSpec

from .base import BaseTransport

logger = logging.getLogger(__name__)


class StdioTransport(BaseTransport):

    def __init__(self, *, server_name: str, launch_spec: SandboxedLaunchSpec) -> None:
        super().__init__()
        self._server_name = server_name
        self._launch_spec = launch_spec
        self._context: Any = None
        self._cleaned_up = False

    async def connect(self) -> "StdioTransport":
        if self._cleaned_up or not self._launch_spec.private_root.exists():
            raise RuntimeError(
                f"MCP server '{self._server_name}' private root is unavailable for reconnect"
            )
        controlled_env = {key: "" for key in get_default_environment()}
        controlled_env.update(self._launch_spec.env)
        params = StdioServerParameters(
            command=self._launch_spec.executable,
            args=list(self._launch_spec.args),
            env=controlled_env,
            cwd=self._launch_spec.cwd,
        )
        self._context = stdio_client(params)
        try:
            streams = await self._context.__aenter__()
        except Exception:
            await self._cleanup_private_root()
            self._context = None
            raise
        self._set_streams(streams[0], streams[1])
        logger.debug(
            "Stdio transport connected: server=%s correlation_id=%s arg_count=%d",
            self._server_name,
            self._launch_spec.correlation_id,
            len(self._launch_spec.args),
        )
        return self

    async def disconnect(self) -> None:
        if self._context:
            try:
                await self._context.__aexit__(None, None, None)
            except (RuntimeError, asyncio.CancelledError):
                pass
            finally:
                self._context = None
        await self._cleanup_private_root()
        self._clear_streams()
        logger.debug("Stdio transport disconnected")

    async def _cleanup_private_root(self) -> None:
        if self._cleaned_up:
            return
        try:
            shutil.rmtree(self._launch_spec.private_root, ignore_errors=False)
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning(
                "Failed to clean up stdio launch state for server '%s' correlation_id=%s",
                self._server_name,
                self._launch_spec.correlation_id,
            )
        finally:
            self._cleaned_up = True
