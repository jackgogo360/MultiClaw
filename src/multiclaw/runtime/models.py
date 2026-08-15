from __future__ import annotations

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
    active_run_count: int = 0
    awaiting_user_run_count: int = 0
    active_executing_run_count: int = 0
    active_tool_execution_count: int = 0
    _closed: bool = field(default=False, init=False, repr=False)

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
            self.active_run_count == self.awaiting_user_run_count
            and self.active_tool_execution_count == 0
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        primary: BaseException | None = None

        def _remember(error: BaseException, phase: str) -> None:
            nonlocal primary
            if primary is None:
                primary = error
                return
            primary.add_note(f"{phase} failed: {type(error).__name__}: {error}")

        if self.mcp_manager is not None:
            try:
                self.mcp_manager.stop()
            except BaseException as error:
                _remember(error, "mcp_manager.stop")

        try:
            self.skill_manager.close()
        except BaseException as error:
            _remember(error, "skill_manager.close")

        try:
            self.registry.clear()
        except BaseException as error:
            _remember(error, "registry.clear")

        try:
            self.event_router.clear()
        except BaseException as error:
            _remember(error, "event_router.clear")

        for index, handle in enumerate(list(self.secret_handles)):
            closer = getattr(handle, "close", None)
            if closer is None:
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except BaseException as error:
                _remember(error, f"secret_handles[{index}].close")
        self.secret_handles.clear()

        if self.sandbox_controller is not None:
            try:
                self.sandbox_controller.close()
            except BaseException as error:
                _remember(error, "sandbox_controller.close")

        if primary is not None:
            raise primary
