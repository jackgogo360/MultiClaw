from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from multiclaw.events import EventBus
from multiclaw.governance import SandboxController, SandboxReadiness
from multiclaw.mcp import MCPClientManager
from multiclaw.skills import SkillManager
from multiclaw.tools import CoreToolScheduler, ToolRegistry


class RuntimeClock(Protocol):
    def now_ms(self) -> int: ...


class SecretHandle(Protocol):
    def close(self) -> Any: ...


class EventRouter:
    def __init__(self) -> None:
        self._routes: dict[str, list[str]] = {}

    def clear(self) -> None:
        self._routes.clear()


class RuntimeExecutionLease:
    def __init__(self, runtime: TenantRuntime) -> None:
        self._runtime = runtime
        self._closed = False
        self._executing = True
        self._awaiting_user = False
        self._checkpoint_persisted = False
        self._active_tool_executions = 0

        runtime.active_run_count += 1
        runtime.active_executing_run_count += 1
        runtime.touch()

    def mark_tool_execution_started(self) -> None:
        if self._closed:
            return
        self._active_tool_executions += 1
        self._runtime.active_tool_execution_count += 1
        self._runtime.touch()

    def mark_tool_execution_finished(self) -> None:
        if self._closed or self._active_tool_executions == 0:
            return
        self._active_tool_executions -= 1
        self._runtime.active_tool_execution_count = max(
            0,
            self._runtime.active_tool_execution_count - 1,
        )
        self._runtime.touch()

    def mark_awaiting_user(self, *, checkpoint_persisted: bool) -> None:
        if self._closed or self._awaiting_user:
            return
        if self._executing:
            self._runtime.active_executing_run_count = max(
                0,
                self._runtime.active_executing_run_count - 1,
            )
            self._executing = False
        self._runtime.awaiting_user_run_count += 1
        self._awaiting_user = True
        if checkpoint_persisted:
            self._runtime.checkpointed_awaiting_user_run_count += 1
            self._checkpoint_persisted = True
        self._runtime.touch()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._awaiting_user:
            self._runtime.awaiting_user_run_count = max(
                0,
                self._runtime.awaiting_user_run_count - 1,
            )
            if self._checkpoint_persisted:
                self._runtime.checkpointed_awaiting_user_run_count = max(
                    0,
                    self._runtime.checkpointed_awaiting_user_run_count - 1,
                )
        elif self._executing:
            self._runtime.active_executing_run_count = max(
                0,
                self._runtime.active_executing_run_count - 1,
            )

        if self._active_tool_executions:
            self._runtime.active_tool_execution_count = max(
                0,
                self._runtime.active_tool_execution_count - self._active_tool_executions,
            )
            self._active_tool_executions = 0

        self._runtime.active_run_count = max(0, self._runtime.active_run_count - 1)
        self._runtime.touch()


@dataclass(slots=True)
class TenantRuntime:
    tenant_id: str
    runtime_instance_id: str
    workspace_root: Path
    agent: Any
    event_bus: EventBus
    event_router: EventRouter
    scheduler: CoreToolScheduler
    registry: ToolRegistry
    skill_manager: SkillManager
    mcp_manager: MCPClientManager | None
    sandbox_controller: SandboxController | None
    sandbox_readiness: SandboxReadiness | None
    last_used_at_ms: int
    secret_handles: list[SecretHandle] = field(default_factory=list)
    clock: RuntimeClock | None = None
    active_run_count: int = 0
    awaiting_user_run_count: int = 0
    checkpointed_awaiting_user_run_count: int = 0
    active_executing_run_count: int = 0
    active_tool_execution_count: int = 0
    _available: bool = field(default=True, init=False, repr=False)
    _close_complete: bool = field(default=False, init=False, repr=False)
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _mcp_stopped: bool = field(default=False, init=False, repr=False)
    _skill_closed: bool = field(default=False, init=False, repr=False)
    _registry_cleared: bool = field(default=False, init=False, repr=False)
    _event_router_cleared: bool = field(default=False, init=False, repr=False)
    _sandbox_closed: bool = field(default=False, init=False, repr=False)

    def idle_expired(self, now_ms: int, idle_ttl_ms: int) -> bool:
        return (now_ms - self.last_used_at_ms) > idle_ttl_ms

    def can_evict(self, now_ms: int, idle_ttl_ms: int) -> bool:
        if not self.idle_expired(now_ms, idle_ttl_ms):
            return False
        if self.active_executing_run_count > 0:
            return False
        if self.active_run_count == 0:
            return True
        return (
            self.active_run_count == self.checkpointed_awaiting_user_run_count
            and self.active_tool_execution_count == 0
        )

    @property
    def is_available(self) -> bool:
        return self._available and not self._close_complete

    def mark_unavailable(self) -> None:
        self._available = False

    def touch(self, now_ms: int | None = None) -> None:
        if now_ms is None:
            if self.clock is None:
                return
            now_ms = self.clock.now_ms()
        self.last_used_at_ms = now_ms

    def begin_run(self) -> RuntimeExecutionLease:
        if not self.is_available:
            raise RuntimeError("runtime is unavailable")
        self.touch()
        return RuntimeExecutionLease(self)

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_complete:
                return
            self._available = False

            primary: BaseException | None = None

            def _remember(error: BaseException, phase: str) -> None:
                nonlocal primary
                if primary is None:
                    primary = error
                    return
                primary.add_note(f"{phase} failed: {type(error).__name__}: {error}")

            if not self._mcp_stopped and self.mcp_manager is not None:
                try:
                    self.mcp_manager.stop()
                except BaseException as error:
                    _remember(error, "mcp_manager.stop")
                else:
                    self._mcp_stopped = True

            if not self._skill_closed:
                try:
                    self.skill_manager.close()
                except BaseException as error:
                    _remember(error, "skill_manager.close")
                else:
                    self._skill_closed = True

            if not self._registry_cleared:
                try:
                    self.registry.clear()
                except BaseException as error:
                    _remember(error, "registry.clear")
                else:
                    self._registry_cleared = True

            if not self._event_router_cleared:
                try:
                    self.event_router.clear()
                except BaseException as error:
                    _remember(error, "event_router.clear")
                else:
                    self._event_router_cleared = True

            remaining_handles: list[SecretHandle] = []
            original_handles = list(self.secret_handles)
            for index, handle in enumerate(original_handles):
                closer = getattr(handle, "close", None)
                if closer is None:
                    continue
                try:
                    result = closer()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as error:
                    remaining_handles.append(handle)
                    _remember(error, f"secret_handles[{index}].close")
            self.secret_handles = remaining_handles

            if not self._sandbox_closed and self.sandbox_controller is not None:
                try:
                    self.sandbox_controller.close()
                except BaseException as error:
                    _remember(error, "sandbox_controller.close")
                else:
                    self._sandbox_closed = True

            if primary is not None:
                raise primary

            self._close_complete = True
