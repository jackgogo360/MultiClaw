"""MCPClientManager — 多服务器生命周期管理与并发编排"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import replace
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .circuit_breaker import CircuitBreaker
from .client import MCPClient
from .transport.factory import create_transport
from .types import (
    HTTPServerConfig,
    SSEServerConfig,
    StdioServerConfig,
    ServerConfig,
    ServerState,
    ServerStatus,
    ToolCallResult,
    ToolInfo,
    WebSocketServerConfig,
)

logger = logging.getLogger(__name__)

_LOCAL_BATCH_SIZE = 3
_REMOTE_BATCH_SIZE = 20
_POLL_INTERVAL = 0.1


class MCPClientManager:
    """管理所有 MCP 服务器连接的中心协调器。

    核心职责：
    - 后台事件循环管理（daemon thread）
    - 并发连接多服务器（滑动窗口）
    - 工具注册与路由
    - 断路器保护
    - 动态工具刷新
    - 同步/异步桥接
    """

    def __init__(
        self,
        *,
        local_batch_size: int = _LOCAL_BATCH_SIZE,
        remote_batch_size: int = _REMOTE_BATCH_SIZE,
        sandbox_controller=None,
        workspace_root: Path | None = None,
        secret_resolver=None,
        tenant_context=None,
    ) -> None:
        self._local_batch_size = local_batch_size
        self._remote_batch_size = remote_batch_size
        self._sandbox_controller = sandbox_controller
        self._workspace_root = workspace_root
        self._secret_resolver = secret_resolver
        self._tenant_context = tenant_context
        self._clients: dict[str, MCPClient] = {}
        self._states: dict[str, ServerState] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._tools_changed_callback: Callable[[str, list[ToolInfo]], None] | None = None
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._loop = asyncio.new_event_loop()
            self._loop.set_exception_handler(self._loop_exception_handler)
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                name="mcp-event-loop",
                daemon=True,
            )
            self._thread.start()
            self._started = True
            logger.info("MCP event loop started")

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
        self._run_sync(self._disconnect_all())
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5.0)
        self._started = False
        self._states.clear()
        self._breakers.clear()
        self._loop = None
        self._thread = None
        logger.info("MCP event loop stopped")

    def connect_servers(self, configs: dict[str, ServerConfig]) -> dict[str, ServerState]:
        self.start()
        return self._run_sync(self._connect_all(configs))

    def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolCallResult:
        breaker = self._breakers.get(server_name)
        if breaker and breaker.is_open:
            remaining = int(breaker.remaining_cooldown)
            raise RuntimeError(
                f"Server '{server_name}' circuit breaker open. Retry in ~{remaining}s."
            )
        try:
            result = self._run_sync(self._call_tool_async(server_name, tool_name, arguments))
            if breaker:
                breaker.record_success()
            return result
        except Exception as e:
            if breaker:
                breaker.record_failure()
            raise

    def get_all_tools(self) -> list:
        tools = []
        for state in self._states.values():
            tools.extend(state.tools)
        return tools

    def get_server_states(self) -> dict[str, ServerState]:
        return dict(self._states)

    def refresh_server_tools(self, server_name: str) -> list[ToolInfo]:
        return self._run_sync(self._refresh_server(server_name))

    def set_tools_changed_callback(
        self,
        callback: Callable[[str, list[ToolInfo]], None] | None,
    ) -> None:
        self._tools_changed_callback = callback

    # --- 内部异步方法 ---

    async def _connect_all(self, configs: dict[str, ServerConfig]) -> dict[str, ServerState]:
        from .types import StdioServerConfig, InProcessServerConfig

        local_configs = {
            k: v for k, v in configs.items()
            if isinstance(v, (StdioServerConfig, InProcessServerConfig))
        }
        remote_configs = {k: v for k, v in configs.items() if k not in local_configs}

        await asyncio.gather(
            self._connect_batch(local_configs, self._local_batch_size),
            self._connect_batch(remote_configs, self._remote_batch_size),
        )
        return dict(self._states)

    async def _connect_batch(self, configs: dict[str, ServerConfig], concurrency: int) -> None:
        sem = asyncio.Semaphore(concurrency)

        async def _connect_one(name: str, config: ServerConfig) -> None:
            async with sem:
                await self._connect_server(name, config)

        await asyncio.gather(
            *[_connect_one(name, config) for name, config in configs.items()],
            return_exceptions=True,
        )

    async def _connect_server(self, name: str, config: ServerConfig) -> None:
        state = ServerState(name=name, config=config, status=ServerStatus.PENDING)
        self._states[name] = state
        self._breakers.setdefault(name, CircuitBreaker())

        try:
            resolved_config = await self._resolve_config_secrets(config)
            transport = create_transport(
                resolved_config,
                sandbox_controller=self._sandbox_controller,
                workspace_root=self._workspace_root,
                server_name=name,
            )
            client = MCPClient(name=name, transport=transport)
            client.set_on_tools_changed(self._on_tools_changed)
            await client.connect()
            tools = await client.discover_tools()
            self._clients[name] = client
            state.status = ServerStatus.CONNECTED
            state.tools = tools
            self._register_tools(name, tools, client)
            logger.info("Server '%s' connected with %d tools", name, len(tools))
        except Exception as e:
            state.status = ServerStatus.FAILED
            state.error = _sanitize_error(str(e))
            logger.warning("Server '%s' connection failed: %s", name, state.error)

    async def _resolve_config_secrets(self, config: ServerConfig) -> ServerConfig:
        if self._secret_resolver is None or self._tenant_context is None:
            return config
        match config:
            case StdioServerConfig():
                return replace(
                    config,
                    env=await self._resolve_mapping(config.env),
                )
            case SSEServerConfig() | HTTPServerConfig() | WebSocketServerConfig():
                return replace(
                    config,
                    headers=await self._resolve_mapping(config.headers),
                )
            case _:
                return config

    async def _resolve_mapping(self, values: dict[str, str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for key, value in values.items():
            if isinstance(value, str) and value.startswith("secret://"):
                secret = await self._secret_resolver.resolve_reference(self._tenant_context, value)
                with secret.reveal() as plaintext:
                    resolved[key] = bytes(plaintext).decode("utf-8")
            else:
                resolved[key] = value
        return resolved

    async def _disconnect_all(self) -> None:
        tasks = []
        for name, client in list(self._clients.items()):
            tasks.append(self._disconnect_server(name, client))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()

    async def _disconnect_server(self, name: str, client: MCPClient) -> None:
        try:
            await client.disconnect()
        except Exception as e:
            logger.debug("Error disconnecting '%s': %s", name, e)
        if name in self._states:
            self._states[name].status = ServerStatus.DISCONNECTED

    async def _call_tool_async(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolCallResult:
        client = self._clients.get(server_name)
        if not client or not client.connected:
            raise RuntimeError(f"Server '{server_name}' not connected")
        return await client.call_tool_with_retry(tool_name, arguments)

    async def _refresh_server(self, server_name: str) -> list[ToolInfo]:
        client = self._clients.get(server_name)
        if not client or not client.connected:
            raise RuntimeError(f"Server '{server_name}' not connected")

        tools = await client.refresh_tools()
        self._register_tools(server_name, tools, client)
        if server_name in self._states:
            self._states[server_name].tools = tools
        return tools

    def _register_tools(self, server_name: str, tools: list[ToolInfo], client: MCPClient) -> None:
        pass  # tools are registered externally via tool_adapter.py

    def _on_tools_changed(self, server_name: str, tools: list[ToolInfo]) -> None:
        if server_name in self._states:
            self._states[server_name].tools = tools
        logger.info("Tools refreshed for server '%s': %d tools", server_name, len(tools))
        if self._tools_changed_callback is not None:
            try:
                self._tools_changed_callback(server_name, tools)
            except Exception:
                logger.exception(
                    "Tools changed callback failed for server '%s'",
                    server_name,
                )

    # --- 同步/异步桥接 ---

    def _run_sync(self, coro: Any) -> Any:
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("MCP event loop not running. Call start() first.")

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        while True:
            try:
                return future.result(timeout=_POLL_INTERVAL)
            except concurrent.futures.TimeoutError:
                continue

    @staticmethod
    def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        if isinstance(exception, RuntimeError) and "Event loop is closed" in str(exception):
            return
        loop.default_exception_handler(context)


def _sanitize_error(text: str) -> str:
    import re
    pattern = re.compile(
        r"(?:ghp_[A-Za-z0-9_]{1,255}|sk-[A-Za-z0-9_]{1,255}|Bearer\s+\S+"
        r"|token=[^\s&,;\"']{1,255}|key=[^\s&,;\"']{1,255})",
        re.IGNORECASE,
    )
    sanitized = pattern.sub("[REDACTED]", text)
    if "/" in sanitized or "\\" in sanitized:
        return "details redacted"
    return sanitized
