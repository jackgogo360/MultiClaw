from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import exc as sa_exc

from multiclaw.agent import MultiClawAgent
from multiclaw.config import Settings
from multiclaw.events import EventBus, EventRouter
from multiclaw.governance import (
    ExecutionGuard,
    InMemoryAuditLogger,
    PermissionChecker,
    SandboxController,
    SandboxProcessRunner,
    SandboxReadiness,
)
from multiclaw.governance.sandbox.manager import SandboxManager
from multiclaw.llm import ModelRouter
from multiclaw.memory import MemoryEntry, MemoryProtocol
from multiclaw.planner import Planner
from multiclaw.runtime.models import RuntimeClock, TenantRuntime
from multiclaw.skills import SkillManager
from multiclaw.storage import Database
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy import TenantContext, WorkspaceResolver
from multiclaw.tools import CoreToolScheduler, ToolRegistry
from multiclaw.tools.code_exec import CodeExecToolBuilder
from multiclaw.tools.edit_file import EditFileToolBuilder, UndoEditToolBuilder
from multiclaw.tools.find_dir import FindDirToolBuilder
from multiclaw.tools.glob import GlobToolBuilder
from multiclaw.tools.grep import GrepToolBuilder
from multiclaw.tools.list_dir import ListDirToolBuilder
from multiclaw.tools.read_file import ReadFileToolBuilder
from multiclaw.tools.shell import ShellToolBuilder
from multiclaw.tools.web_fetch import WebFetchToolBuilder
from multiclaw.tools.web_search import WebSearchToolBuilder
from multiclaw.tools.write_file import WriteFileToolBuilder

from multiclaw.mcp import MCPClientManager


class _DatabaseBackedMemory(MemoryProtocol):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def save(self, context: TenantContext, entry: MemoryEntry) -> MemoryEntry:
        async with TenantUnitOfWork(self._database, context) as uow:
            return await uow.memory.save(entry)

    async def query(
        self,
        context: TenantContext,
        query: str,
        top_k: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        async with TenantUnitOfWork(self._database, context) as uow:
            return await uow.memory.query(query, top_k, entry_type)

    async def recent(
        self,
        context: TenantContext,
        limit: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        async with TenantUnitOfWork(self._database, context) as uow:
            return await uow.memory.recent(limit, entry_type)

    async def context(
        self,
        context: TenantContext,
        max_chars: int,
        limit: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        async with TenantUnitOfWork(self._database, context) as uow:
            return await uow.memory.context(max_chars, limit, entry_type)

    async def forget(self, context: TenantContext, entry_id: str) -> None:
        async with TenantUnitOfWork(self._database, context) as uow:
            await uow.memory.forget(entry_id)


class _SystemClock:
    def now_ms(self) -> int:
        import time

        return int(time.time() * 1000)


SandboxControllerFactory = Callable[[Path, EventBus], SandboxController]
McpToolRegistrar = Callable[..., None]


class RuntimeFactory:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        workspace_resolver: WorkspaceResolver,
        secret_resolver: Any | None = None,
        clock: RuntimeClock | None = None,
        sandbox_controller_factory: SandboxControllerFactory | None = None,
        mcp_manager_factory: type[MCPClientManager] = MCPClientManager,
        mcp_tool_registrar: McpToolRegistrar | None = None,
        config_path: str | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.workspace_resolver = workspace_resolver
        self.secret_resolver = secret_resolver
        self.clock = clock or _SystemClock()
        self._sandbox_controller_factory = (
            sandbox_controller_factory or self._default_sandbox_controller_factory
        )
        self._mcp_manager_factory = mcp_manager_factory
        self._mcp_tool_registrar = mcp_tool_registrar
        self._config_path = config_path

    async def create(self, context: TenantContext) -> TenantRuntime:
        workspace_root = self.workspace_resolver.resolve(context, create=True)
        event_bus = EventBus()
        event_router = EventRouter()
        sandbox_controller = None
        skill_manager = None
        registry = None
        mcp_manager = None
        try:
            sandbox_controller = self._sandbox_controller_factory(workspace_root, event_bus)
            sandbox_controller.initialize()

            skill_manager = SkillManager(
                project_root=workspace_root,
                max_active=self.settings.skill.max_active,
            )
            if self.settings.skill.enabled:
                skill_manager.discover()

            registry = self._build_registry(workspace_root, event_bus, sandbox_controller)
            mcp_manager = self._build_mcp_manager(
                workspace_root,
                event_bus,
                registry,
                sandbox_controller,
            )
            readiness = sandbox_controller.finalize_readiness()
            scheduler = self._build_scheduler(event_bus)
            scheduler.event_router = event_router
            agent = self._build_agent(
                context,
                registry,
                scheduler,
                event_bus,
                skill_manager,
            )
        except BaseException as primary:
            self._cleanup_create_failure(
                primary,
                mcp_manager=mcp_manager,
                skill_manager=skill_manager,
                registry=registry,
                event_router=event_router,
                sandbox_controller=sandbox_controller,
            )
            raise

        agent.database = self.database
        agent.event_router = event_router
        agent.mcp_manager = mcp_manager
        agent.sandbox_controller = sandbox_controller
        agent.sandbox_readiness = readiness
        agent.workspace_root = workspace_root
        runtime = TenantRuntime(
            tenant_id=context.tenant_id,
            runtime_instance_id=str(uuid.uuid4()),
            workspace_root=workspace_root,
            agent=agent,
            event_bus=event_bus,
            event_router=event_router,
            scheduler=scheduler,
            registry=registry,
            skill_manager=skill_manager,
            mcp_manager=mcp_manager,
            sandbox_controller=sandbox_controller,
            sandbox_readiness=readiness,
            last_used_at_ms=self.clock.now_ms(),
            clock=self.clock,
        )
        runtime.apply_workflow_counters(await self._load_workflow_counters(context))
        return runtime

    def probe_startup(self) -> tuple[SandboxReadiness, tuple[Any, ...]]:
        event_bus = EventBus()
        controller = self._sandbox_controller_factory(self.workspace_resolver.root, event_bus)
        result: tuple[SandboxReadiness, tuple[Any, ...]] | None = None
        try:
            controller.initialize()
            readiness = controller.finalize_readiness()
            events = controller.drain_startup_events()
            result = (readiness, events)
        except BaseException as primary:
            try:
                controller.close()
            except BaseException as error:
                primary.add_note(
                    f"controller.close failed: {type(error).__name__}: {error}"
                )
            raise
        controller.close()
        assert result is not None
        return result

    def _build_agent(
        self,
        context: TenantContext,
        registry: ToolRegistry,
        scheduler: CoreToolScheduler,
        event_bus: EventBus,
        skill_manager: SkillManager,
    ) -> MultiClawAgent:
        del context
        return MultiClawAgent(
            settings=self.settings,
            router=ModelRouter(self.settings),
            registry=registry,
            scheduler=scheduler,
            memory=_DatabaseBackedMemory(self.database),
            planner=Planner(),
            event_bus=event_bus,
            skill_manager=skill_manager,
        )

    def _build_scheduler(self, event_bus: EventBus) -> CoreToolScheduler:
        return CoreToolScheduler(
            permission_checker=PermissionChecker(
                guarded_tools={
                    "write_file",
                    "edit_file",
                    "undo_edit",
                    "shell",
                    "code_exec",
                }
            ),
            execution_guard=ExecutionGuard(),
            audit_logger=InMemoryAuditLogger(),
            event_bus=event_bus,
        )

    def _build_registry(
        self,
        workspace_root: Path,
        event_bus: EventBus,
        sandbox_controller: SandboxController,
    ) -> ToolRegistry:
        del event_bus
        registry = ToolRegistry()
        read_builder = ReadFileToolBuilder(workspace_root)
        edit_builder = EditFileToolBuilder(workspace_root)
        registry.register(read_builder)
        registry.register(WriteFileToolBuilder(workspace_root, read_builder))
        registry.register(edit_builder)
        registry.register(UndoEditToolBuilder(workspace_root, edit_builder))
        registry.register(GlobToolBuilder(workspace_root))
        registry.register(ListDirToolBuilder(workspace_root))
        registry.register(GrepToolBuilder(workspace_root))
        registry.register(FindDirToolBuilder(workspace_root))

        if sandbox_controller.is_profile_ready(self.settings.governance.sandbox.profiles.shell):
            registry.register(
                ShellToolBuilder(
                    workspace_root,
                    sandbox_controller=sandbox_controller,
                    profile_name=self.settings.governance.sandbox.profiles.shell,
                )
            )
        else:
            sandbox_controller.record_blocked_capability(
                "shell",
                f"sandbox profile {self.settings.governance.sandbox.profiles.shell!r} is not ready",
            )

        if sandbox_controller.is_profile_ready(self.settings.governance.sandbox.profiles.code_exec):
            registry.register(
                CodeExecToolBuilder(
                    workspace_root,
                    sandbox_controller=sandbox_controller,
                    profile_name=self.settings.governance.sandbox.profiles.code_exec,
                )
            )
        else:
            sandbox_controller.record_blocked_capability(
                "code_exec",
                f"sandbox profile {self.settings.governance.sandbox.profiles.code_exec!r} is not ready",
            )

        registry.register(
            WebFetchToolBuilder(
                workspace_root,
                allow_private_networks=self.settings.tools.web_fetch_allow_private_networks,
            )
        )
        registry.register(WebSearchToolBuilder(workspace_root))
        return registry

    async def _load_workflow_counters(self, context: TenantContext):
        try:
            async with self.database.connect() as conn:
                repository = WorkflowRepository(
                    conn,
                    self.database.dialect,
                    self.settings.workflow.heartbeat_ms,
                    self.settings.workflow.lease_ttl_ms,
                )
                return await repository.get_runtime_counters(context)
        except (sa_exc.OperationalError, sa_exc.ProgrammingError) as error:
            message = str(error).lower()
            if "no such table" in message or "doesn't exist" in message:
                from multiclaw.workflow.models import WorkflowRuntimeCounters

                return WorkflowRuntimeCounters()
            raise

    def _build_mcp_manager(
        self,
        workspace_root: Path,
        event_bus: EventBus,
        registry: ToolRegistry,
        sandbox_controller: SandboxController,
    ) -> MCPClientManager | None:
        del event_bus
        if not self.settings.mcp.enabled:
            return None

        manager = self._mcp_manager_factory(
            sandbox_controller=sandbox_controller,
            workspace_root=workspace_root,
        )
        if self._mcp_tool_registrar is not None:
            self._mcp_tool_registrar(
                registry=registry,
                mcp_manager=manager,
                config_path=self._config_path,
                sandbox_controller=sandbox_controller,
                workspace_root=workspace_root,
                mcp_profile_name=self.settings.governance.sandbox.profiles.mcp_stdio,
            )
        return manager

    def _default_sandbox_controller_factory(
        self,
        workspace_root: Path,
        event_bus: EventBus,
    ) -> SandboxController:
        return SandboxManager.create(
            settings=self.settings.governance.sandbox,
            debug=self.settings.app.debug,
            workspace_root=workspace_root,
            event_bus=event_bus,
            runner=SandboxProcessRunner(),
        )

    def _cleanup_create_failure(
        self,
        primary: BaseException,
        *,
        mcp_manager: MCPClientManager | None,
        skill_manager: SkillManager | None,
        registry: ToolRegistry | None,
        event_router: EventRouter,
        sandbox_controller: SandboxController | None,
    ) -> None:
        self._run_cleanup_phase(primary, "mcp_manager.stop", self._sync_cleanup, mcp_manager, "stop")
        self._run_cleanup_phase(primary, "skill_manager.close", self._sync_cleanup, skill_manager, "close")
        self._run_cleanup_phase(primary, "registry.clear", self._sync_cleanup, registry, "clear")
        self._run_cleanup_phase(primary, "event_router.clear", self._sync_cleanup, event_router, "clear")
        self._run_cleanup_phase(
            primary,
            "sandbox_controller.close",
            self._sync_cleanup,
            sandbox_controller,
            "close",
        )

    def _run_cleanup_phase(
        self,
        primary: BaseException,
        phase: str,
        cleanup: Callable[..., Any],
        *args: Any,
    ) -> None:
        try:
            cleanup(*args)
        except BaseException as error:
            primary.add_note(f"{phase} failed: {type(error).__name__}: {error}")

    @staticmethod
    def _sync_cleanup(owner: Any, method_name: str) -> None:
        if owner is None:
            return
        method = getattr(owner, method_name, None)
        if method is None:
            return
        result = method()
        if inspect.isawaitable(result):
            raise RuntimeError(f"{method_name} returned awaitable during sync cleanup")
