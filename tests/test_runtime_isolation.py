import asyncio
from pathlib import Path

import pytest

from multiclaw.config.settings import DatabaseSettings, McpSettings, Settings, SkillSettings
from multiclaw.storage import Database
from multiclaw.tenancy import TenantContext, WorkspaceResolver

from sandbox_fakes import ReadyRecordingSandboxController


def _settings_for_runtime(root: Path) -> Settings:
    return Settings(
        database=DatabaseSettings(
            driver="sqlite",
            url=f"sqlite+aiosqlite:///{root / 'runtime.db'}",
        ),
        mcp=McpSettings(enabled=False),
        skill=SkillSettings(enabled=True, max_active=3),
    )


@pytest.mark.asyncio
async def test_runtime_factory_builds_distinct_mutable_components_per_tenant(tmp_path: Path):
    from multiclaw.runtime.factory import RuntimeFactory

    settings = _settings_for_runtime(tmp_path)
    database = Database.create(settings.database)
    resolver = WorkspaceResolver(tmp_path)
    factory = RuntimeFactory(
        settings=settings,
        database=database,
        workspace_resolver=resolver,
        sandbox_controller_factory=lambda workspace_root, event_bus: ReadyRecordingSandboxController(
            workspace_root=workspace_root
        ),
    )

    try:
        runtime_a = await factory.create(TenantContext("tenant-a", "workspace-a"))
        runtime_b = await factory.create(TenantContext("tenant-b", "workspace-b"))
    finally:
        await database.dispose()

    assert runtime_a.workspace_root != runtime_b.workspace_root
    assert runtime_a.agent is not runtime_b.agent
    assert runtime_a.event_bus is not runtime_b.event_bus
    assert runtime_a.event_router is not runtime_b.event_router
    assert runtime_a.scheduler is not runtime_b.scheduler
    assert runtime_a.registry is not runtime_b.registry
    assert runtime_a.skill_manager is not runtime_b.skill_manager
    assert runtime_a.sandbox_controller is not runtime_b.sandbox_controller


@pytest.mark.asyncio
async def test_runtime_factory_scopes_skill_discovery_to_each_workspace(tmp_path: Path):
    from multiclaw.runtime.factory import RuntimeFactory

    settings = _settings_for_runtime(tmp_path)
    database = Database.create(settings.database)
    resolver = WorkspaceResolver(tmp_path)
    factory = RuntimeFactory(
        settings=settings,
        database=database,
        workspace_resolver=resolver,
        sandbox_controller_factory=lambda workspace_root, event_bus: ReadyRecordingSandboxController(
            workspace_root=workspace_root
        ),
    )

    try:
        runtime_a = await factory.create(TenantContext("tenant-a", "workspace-a"))
        runtime_b = await factory.create(TenantContext("tenant-b", "workspace-b"))

        skill_a_dir = runtime_a.workspace_root / ".multiclaw" / "skills" / "tenant-a-only"
        skill_a_dir.mkdir(parents=True)
        (skill_a_dir / "SKILL.md").write_text(
            "---\nname: tenant-a-only\ndescription: tenant a\n---\nA only.\n",
            encoding="utf-8",
        )

        skill_b_dir = runtime_b.workspace_root / ".multiclaw" / "skills" / "tenant-b-only"
        skill_b_dir.mkdir(parents=True)
        (skill_b_dir / "SKILL.md").write_text(
            "---\nname: tenant-b-only\ndescription: tenant b\n---\nB only.\n",
            encoding="utf-8",
        )

        runtime_a.skill_manager.discover()
        runtime_b.skill_manager.discover()

        assert "tenant-a-only" in runtime_a.skill_manager.skills
        assert "tenant-a-only" not in runtime_b.skill_manager.skills
        assert "tenant-b-only" in runtime_b.skill_manager.skills
        assert "tenant-b-only" not in runtime_a.skill_manager.skills
    finally:
        await asyncio.gather(runtime_a.close(), runtime_b.close())
        await database.dispose()


@pytest.mark.asyncio
async def test_runtime_factory_closes_partial_runtime_resources_when_create_fails(tmp_path: Path):
    from multiclaw.runtime.factory import RuntimeFactory

    class TrackingController(ReadyRecordingSandboxController):
        def __init__(self, *, workspace_root: Path) -> None:
            super().__init__(workspace_root=workspace_root)
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    class TrackingManager:
        def __init__(self) -> None:
            self.stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1

    captured: dict[str, object] = {}

    class FailingFactory(RuntimeFactory):
        def _build_mcp_manager(self, workspace_root, event_bus, registry, sandbox_controller):
            del workspace_root, event_bus, registry, sandbox_controller
            manager = TrackingManager()
            captured["manager"] = manager
            return manager

        def _build_agent(self, context, registry, scheduler, event_bus, skill_manager):
            del context, registry, scheduler, event_bus, skill_manager
            raise RuntimeError("agent assembly failed")

    settings = _settings_for_runtime(tmp_path).model_copy(update={"mcp": McpSettings(enabled=True)})
    database = Database.create(settings.database)
    resolver = WorkspaceResolver(tmp_path)
    controllers: list[TrackingController] = []

    def controller_factory(workspace_root: Path, event_bus):
        del event_bus
        controller = TrackingController(workspace_root=workspace_root)
        controllers.append(controller)
        return controller

    factory = FailingFactory(
        settings=settings,
        database=database,
        workspace_resolver=resolver,
        sandbox_controller_factory=controller_factory,
    )

    try:
        with pytest.raises(RuntimeError, match="agent assembly failed"):
            await factory.create(TenantContext("tenant-a", "workspace-a"))
    finally:
        await database.dispose()

    assert len(controllers) == 1
    assert controllers[0].close_calls == 1
    assert captured["manager"].stop_calls == 1


@pytest.mark.asyncio
async def test_runtime_factory_preserves_primary_create_failure_and_notes_cleanup_failures(
    tmp_path: Path,
):
    from multiclaw.runtime.factory import RuntimeFactory

    class TrackingController(ReadyRecordingSandboxController):
        def __init__(self, *, workspace_root: Path) -> None:
            super().__init__(workspace_root=workspace_root)
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("controller close failed")

    class FailingManager:
        def __init__(self) -> None:
            self.stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1
            raise RuntimeError("cleanup stop failed")

    class FailingRegistry:
        def __init__(self) -> None:
            self.clear_calls = 0

        def clear(self) -> None:
            self.clear_calls += 1
            raise RuntimeError("registry clear failed")

    captured: dict[str, object] = {}

    class FailingFactory(RuntimeFactory):
        def _build_registry(self, workspace_root, event_bus, sandbox_controller):
            del workspace_root, event_bus, sandbox_controller
            registry = FailingRegistry()
            captured["registry"] = registry
            return registry

        def _build_mcp_manager(self, workspace_root, event_bus, registry, sandbox_controller):
            del workspace_root, event_bus, registry, sandbox_controller
            manager = FailingManager()
            captured["manager"] = manager
            return manager

        def _build_agent(self, context, registry, scheduler, event_bus, skill_manager):
            del context, registry, scheduler, event_bus, skill_manager
            raise RuntimeError("agent assembly failed")

    settings = _settings_for_runtime(tmp_path).model_copy(update={"mcp": McpSettings(enabled=True)})
    database = Database.create(settings.database)
    resolver = WorkspaceResolver(tmp_path)
    controllers: list[TrackingController] = []

    def controller_factory(workspace_root: Path, event_bus):
        del event_bus
        controller = TrackingController(workspace_root=workspace_root)
        controllers.append(controller)
        return controller

    factory = FailingFactory(
        settings=settings,
        database=database,
        workspace_resolver=resolver,
        sandbox_controller_factory=controller_factory,
    )

    try:
        with pytest.raises(RuntimeError, match="agent assembly failed") as error:
            await factory.create(TenantContext("tenant-a", "workspace-a"))
    finally:
        await database.dispose()

    assert len(controllers) == 1
    assert controllers[0].close_calls == 1
    assert captured["manager"].stop_calls == 1
    assert captured["registry"].clear_calls == 1
    assert error.value.__notes__
    assert any("cleanup stop failed" in note for note in error.value.__notes__)
    assert any("registry clear failed" in note for note in error.value.__notes__)
    assert any("controller close failed" in note for note in error.value.__notes__)


def test_runtime_factory_probe_startup_closes_controller_when_initialize_fails(tmp_path: Path):
    from multiclaw.runtime.factory import RuntimeFactory

    class InitializeFailsController(ReadyRecordingSandboxController):
        def __init__(self, *, workspace_root: Path) -> None:
            super().__init__(workspace_root=workspace_root)
            self.initialize_calls = 0
            self.close_calls = 0

        def initialize(self) -> None:
            self.initialize_calls += 1
            raise RuntimeError("initialize failed")

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    settings = _settings_for_runtime(tmp_path)
    database = Database.create(settings.database)
    controller = InitializeFailsController(workspace_root=tmp_path)
    factory = RuntimeFactory(
        settings=settings,
        database=database,
        workspace_resolver=WorkspaceResolver(tmp_path),
        sandbox_controller_factory=lambda workspace_root, event_bus: controller,
    )

    try:
        with pytest.raises(RuntimeError, match="initialize failed"):
            factory.probe_startup()
    finally:
        asyncio.run(database.dispose())

    assert controller.initialize_calls == 1
    assert controller.close_calls == 1


def test_runtime_factory_probe_startup_preserves_initialize_failure_with_close_note(tmp_path: Path):
    from multiclaw.runtime.factory import RuntimeFactory

    class InitializeAndCloseFailController(ReadyRecordingSandboxController):
        def __init__(self, *, workspace_root: Path) -> None:
            super().__init__(workspace_root=workspace_root)
            self.initialize_calls = 0
            self.close_calls = 0

        def initialize(self) -> None:
            self.initialize_calls += 1
            raise RuntimeError("initialize failed")

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("close failed")

    settings = _settings_for_runtime(tmp_path)
    database = Database.create(settings.database)
    controller = InitializeAndCloseFailController(workspace_root=tmp_path)
    factory = RuntimeFactory(
        settings=settings,
        database=database,
        workspace_resolver=WorkspaceResolver(tmp_path),
        sandbox_controller_factory=lambda workspace_root, event_bus: controller,
    )

    try:
        with pytest.raises(RuntimeError, match="initialize failed") as error:
            factory.probe_startup()
    finally:
        asyncio.run(database.dispose())

    assert controller.initialize_calls == 1
    assert controller.close_calls == 1
    assert error.value.__notes__
    assert any("close failed" in note for note in error.value.__notes__)
