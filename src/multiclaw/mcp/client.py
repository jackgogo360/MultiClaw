"""MCPClient — 单服务器连接管理、工具发现与调用"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from typing import Any, Optional

from mcp import ClientSession
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    Tool,
)

from .transport.base import BaseTransport
from .types import ServerStatus, ToolCallResult, ToolInfo

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_MAX_BACKOFF = 60.0
_CONNECT_TIMEOUT = 30.0
_TOOL_CALL_TIMEOUT = 300.0
_DISCOVERY_TIMEOUT = 30.0


class MCPClient:
    """管理与单个 MCP 服务器的连接。

    支持：连接/断开、工具发现、工具调用、自动重连、动态刷新。
    """

    def __init__(
        self,
        name: str,
        transport: BaseTransport,
        *,
        tool_call_timeout: float = _TOOL_CALL_TIMEOUT,
        connect_timeout: float = _CONNECT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self.name = name
        self._transport = transport
        self._tool_call_timeout = tool_call_timeout
        self._connect_timeout = connect_timeout
        self._max_retries = max_retries

        self._session: Optional[ClientSession] = None
        self._exit_stack: Optional[AsyncExitStack] = None
        self._status = ServerStatus.PENDING
        self._tools: list[Tool] = []
        self._error: Optional[str] = None
        self._rpc_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._on_tools_changed: Optional[Any] = None
        self.initialize_result: Any = None

    @property
    def status(self) -> ServerStatus:
        return self._status

    @property
    def connected(self) -> bool:
        return self._status == ServerStatus.CONNECTED and self._session is not None

    async def connect(self) -> None:
        self._exit_stack = AsyncExitStack()
        try:
            await self._exit_stack.enter_async_context(self._transport)
            self._session = ClientSession(
                self._transport.read_stream,
                self._transport.write_stream,
            )
            await self._exit_stack.enter_async_context(self._session)
            self.initialize_result = await asyncio.wait_for(
                self._session.initialize(),
                timeout=self._connect_timeout,
            )
            self._status = ServerStatus.CONNECTED
            self._error = None
            self._register_notifications()
            logger.info("Connected to MCP server: %s", self.name)
        except Exception as e:
            await self._cleanup()
            self._status = ServerStatus.FAILED
            self._error = str(e)
            raise

    async def disconnect(self) -> None:
        await self._cleanup()
        self._status = ServerStatus.DISCONNECTED
        logger.info("Disconnected from MCP server: %s", self.name)

    async def _cleanup(self) -> None:
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception:
                pass
        self._session = None
        self._exit_stack = None

    def _register_notifications(self) -> None:
        if not self._session:
            return
        try:
            self._session.on_notification(
                "notifications/tools/list_changed",
                self._handle_tools_changed,
            )
        except (AttributeError, TypeError):
            pass

    async def _handle_tools_changed(self, *args: Any) -> None:
        logger.debug("Tools list changed notification from %s", self.name)
        await self.refresh_tools()

    async def discover_tools(self) -> list[ToolInfo]:
        if not self._session:
            raise RuntimeError(f"Not connected to {self.name}")

        async with self._rpc_lock:
            result: ListToolsResult = await asyncio.wait_for(
                self._session.list_tools(),
                timeout=_DISCOVERY_TIMEOUT,
            )

        self._tools = result.tools if hasattr(result, "tools") else []
        return self._build_tool_infos()

    async def refresh_tools(self) -> list[ToolInfo]:
        async with self._refresh_lock:
            tools = await self.discover_tools()
            if self._on_tools_changed:
                self._on_tools_changed(self.name, tools)
            return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolCallResult:
        if not self._session:
            raise RuntimeError(f"Not connected to {self.name}")

        async with self._rpc_lock:
            result: CallToolResult = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments=arguments),
                timeout=self._tool_call_timeout,
            )

        content = []
        for item in result.content:
            if hasattr(item, "model_dump"):
                content.append(item.model_dump())
            else:
                content.append({"type": "text", "text": str(item)})

        return ToolCallResult(
            content=content,
            is_error=bool(result.isError),
            external_request_id=_extract_external_request_id(result),
        )

    async def call_tool_with_retry(self, tool_name: str, arguments: dict[str, Any]) -> ToolCallResult:
        last_error: Optional[Exception] = None
        backoff = 1.0

        for attempt in range(self._max_retries):
            try:
                return await self.call_tool(tool_name, arguments)
            except asyncio.TimeoutError:
                last_error = TimeoutError(
                    f"Tool call '{tool_name}' timed out after {self._tool_call_timeout}s"
                )
            except Exception as e:
                err_str = str(e).lower()
                if "authentication" in err_str or "unauthorized" in err_str:
                    raise
                if "not found" in err_str:
                    raise
                last_error = e

            if attempt < self._max_retries - 1:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

        raise ConnectionError(
            f"Tool call '{tool_name}' failed after {self._max_retries} attempts: {last_error}"
        )

    def _build_tool_infos(self) -> list[ToolInfo]:
        infos = []
        for tool in self._tools:
            annotations = getattr(tool, "annotations", None) or {}
            if hasattr(annotations, "model_dump"):
                annotations = annotations.model_dump()

            infos.append(ToolInfo(
                name=f"mcp__{_sanitize(self.name)}__{_sanitize(tool.name)}",
                server_name=self.name,
                original_name=tool.name,
                description=_truncate(tool.description or "", 2048),
                input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                read_only=annotations.get("readOnlyHint", False),
                destructive=annotations.get("destructiveHint", True),
                open_world=annotations.get("openWorldHint", True),
            ))
        return infos

    def set_on_tools_changed(self, callback: Any) -> None:
        self._on_tools_changed = callback


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 13] + "… [truncated]"


def _extract_external_request_id(result: CallToolResult) -> str | None:
    candidates = []
    for attr in ("meta", "_meta"):
        value = getattr(result, attr, None)
        if value is not None:
            candidates.append(value)
    if hasattr(result, "model_dump"):
        dumped = result.model_dump()
        if isinstance(dumped, dict):
            candidates.append(dumped.get("_meta"))
            candidates.append(dumped.get("meta"))
    for candidate in candidates:
        extracted = _search_request_id(candidate)
        if extracted:
            return extracted
    return None


def _search_request_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in (
            "requestId",
            "request_id",
            "x-request-id",
            "x_request_id",
            "external_request_id",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str):
                candidate = candidate.strip()
                if 0 < len(candidate) <= 255:
                    return candidate
        for nested in value.values():
            found = _search_request_id(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _search_request_id(nested)
            if found:
                return found
    return None
