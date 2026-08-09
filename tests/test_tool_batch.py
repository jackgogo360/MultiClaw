from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from multiclaw.agent.models import ObservationType
from multiclaw.agent.tool_batch import ToolBatchExecutor, ToolCallSpec
from multiclaw.events import EventBus
from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
from multiclaw.mcp.tool_adapter import MCPToolBuilder
from multiclaw.mcp.types import ToolInfo
from multiclaw.tools import (
    CoreToolScheduler,
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)
from multiclaw.tools.find_dir import FindDirToolBuilder
from multiclaw.tools.glob import GlobToolBuilder
from multiclaw.tools.grep import GrepToolBuilder
from multiclaw.tools.list_dir import ListDirToolBuilder
from multiclaw.tools.read_file import ReadFileToolBuilder
from multiclaw.tools.web_fetch import WebFetchToolBuilder
from multiclaw.tools.web_search import WebSearchToolBuilder
from multiclaw.tools.write_file import WriteFileToolBuilder


class ScriptedParams(BaseModel):
    label: str
    file_path: str = "."
    delay: float = 0.0


class ScriptedInvocation(ToolInvocation[ScriptedParams]):
    def __init__(
        self,
        name: str,
        params: ScriptedParams,
        runner,
    ) -> None:
        super().__init__(name=name, params=params)
        self._runner = runner

    async def execute(self) -> ToolExecutionResult:
        return await self._runner(self.params)


class ScriptedToolBuilder(ToolBuilder[ScriptedParams]):
    description = "Scriptable test tool"
    parameters_schema = ScriptedParams

    def __init__(self, name: str, runner, workspace_root: Path, read_only: bool) -> None:
        self.name = name
        self._runner = runner
        self.workspace_root = workspace_root
        self.read_only = read_only

    def validate(self, params: dict) -> ScriptedParams:
        return ScriptedParams(**params)

    def build(self, params: ScriptedParams) -> ToolInvocation[ScriptedParams]:
        return ScriptedInvocation(self.name, params, self._runner)


@pytest.fixture
def scheduler() -> CoreToolScheduler:
    return CoreToolScheduler(
        permission_checker=PermissionChecker(),
        execution_guard=ExecutionGuard(timeout=1.0),
        audit_logger=InMemoryAuditLogger(),
        event_bus=EventBus(),
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


def _executor(
    registry: ToolRegistry,
    scheduler: CoreToolScheduler,
    *,
    enabled: bool = True,
    max_concurrency: int = 4,
) -> ToolBatchExecutor:
    return ToolBatchExecutor(
        registry=registry,
        scheduler=scheduler,
        max_concurrency=max_concurrency,
        enabled=enabled,
    )


def test_native_read_builders_are_marked_read_only(tmp_path: Path) -> None:
    assert ReadFileToolBuilder(tmp_path).read_only is True
    assert ListDirToolBuilder(tmp_path).read_only is True
    assert GlobToolBuilder(tmp_path).read_only is True
    assert GrepToolBuilder(tmp_path).read_only is True
    assert FindDirToolBuilder(tmp_path).read_only is True
    assert WebSearchToolBuilder(tmp_path).read_only is True
    assert WebFetchToolBuilder(tmp_path).read_only is True
    assert WriteFileToolBuilder(tmp_path).read_only is False


def test_mcp_tool_builder_requires_safe_annotations_for_read_only() -> None:
    manager = SimpleNamespace()

    safe_builder = MCPToolBuilder.from_tool_info(
        ToolInfo(
            name="mcp__demo__read_file",
            server_name="demo",
            original_name="read_file",
            description="Read a file",
            input_schema={},
            read_only=True,
            destructive=False,
            open_world=False,
        ),
        manager,
    )
    assert safe_builder.read_only is True

    destructive_builder = MCPToolBuilder.from_tool_info(
        ToolInfo(
            name="mcp__demo__mutating_read",
            server_name="demo",
            original_name="mutating_read",
            description="Not actually safe",
            input_schema={},
            read_only=True,
            destructive=True,
            open_world=False,
        ),
        manager,
    )
    assert destructive_builder.read_only is False

    missing_metadata_builder = MCPToolBuilder.from_tool_info(
        SimpleNamespace(
            name="mcp__demo__unknown",
            server_name="demo",
            original_name="unknown",
            description="Missing metadata",
            input_schema={},
        ),
        manager,
    )
    assert missing_metadata_builder.read_only is False


@pytest.mark.asyncio
async def test_two_consecutive_eligible_reads_overlap_and_preserve_call_order(
    tmp_path: Path,
    registry: ToolRegistry,
    scheduler: CoreToolScheduler,
) -> None:
    active = 0
    max_active = 0
    finished: list[str] = []

    async def runner(params: ScriptedParams) -> ToolExecutionResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(params.delay)
            finished.append(params.label)
            return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)
        finally:
            active -= 1

    registry.register(
        ScriptedToolBuilder("read_probe", runner, tmp_path, read_only=True)
    )
    executor = _executor(registry, scheduler, max_concurrency=2)

    outcomes = await executor.execute(
        [
            ToolCallSpec(call_id="call-1", name="read_probe", arguments={"label": "first", "delay": 0.05}),
            ToolCallSpec(call_id="call-2", name="read_probe", arguments={"label": "second", "delay": 0.01}),
        ]
    )

    assert max_active == 2
    assert finished == ["second", "first"]
    assert [outcome.call_id for outcome in outcomes] == ["call-1", "call-2"]
    assert [outcome.observation.content for outcome in outcomes] == ["first", "second"]
    assert [outcome.observation.type for outcome in outcomes] == [
        ObservationType.TOOL_RESULT,
        ObservationType.TOOL_RESULT,
    ]


@pytest.mark.asyncio
async def test_read_write_read_uses_serial_barriers(
    tmp_path: Path,
    registry: ToolRegistry,
    scheduler: CoreToolScheduler,
) -> None:
    active = 0
    max_active = 0
    execution_order: list[str] = []

    async def runner(params: ScriptedParams) -> ToolExecutionResult:
        nonlocal active, max_active
        execution_order.append(f"start:{params.label}")
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            execution_order.append(f"end:{params.label}")
            return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)
        finally:
            active -= 1

    registry.register(
        ScriptedToolBuilder("read_probe", runner, tmp_path, read_only=True)
    )
    registry.register(
        ScriptedToolBuilder("write_probe", runner, tmp_path, read_only=False)
    )
    executor = _executor(registry, scheduler, max_concurrency=3)

    outcomes = await executor.execute(
        [
            ToolCallSpec(call_id="call-1", name="read_probe", arguments={"label": "read-1"}),
            ToolCallSpec(call_id="call-2", name="write_probe", arguments={"label": "write"}),
            ToolCallSpec(call_id="call-3", name="read_probe", arguments={"label": "read-2"}),
        ]
    )

    assert max_active == 1
    assert execution_order == [
        "start:read-1",
        "end:read-1",
        "start:write",
        "end:write",
        "start:read-2",
        "end:read-2",
    ]
    assert [outcome.observation.content for outcome in outcomes] == [
        "read-1",
        "write",
        "read-2",
    ]


@pytest.mark.asyncio
async def test_approval_eligible_read_is_not_concurrency_eligible(
    tmp_path: Path,
    registry: ToolRegistry,
    scheduler: CoreToolScheduler,
) -> None:
    inside = tmp_path / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    async def runner(params: ScriptedParams) -> ToolExecutionResult:
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)

    builder = ScriptedToolBuilder("read_probe", runner, tmp_path, read_only=True)
    registry.register(builder)

    assert await scheduler.can_run_concurrently(
        builder,
        {"label": "safe", "file_path": str(inside)},
    ) is True
    assert await scheduler.can_run_concurrently(
        builder,
        {"label": "needs-approval", "file_path": str(outside)},
    ) is False


@pytest.mark.asyncio
async def test_disabled_executor_preserves_serial_behavior(
    tmp_path: Path,
    registry: ToolRegistry,
    scheduler: CoreToolScheduler,
) -> None:
    active = 0
    max_active = 0

    async def runner(params: ScriptedParams) -> ToolExecutionResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)
        finally:
            active -= 1

    registry.register(
        ScriptedToolBuilder("read_probe", runner, tmp_path, read_only=True)
    )
    executor = _executor(registry, scheduler, enabled=False, max_concurrency=2)

    outcomes = await executor.execute(
        [
            ToolCallSpec(call_id="call-1", name="read_probe", arguments={"label": "first"}),
            ToolCallSpec(call_id="call-2", name="read_probe", arguments={"label": "second"}),
        ]
    )

    assert max_active == 1
    assert [outcome.observation.content for outcome in outcomes] == ["first", "second"]


@pytest.mark.asyncio
async def test_cancellation_cleans_up_child_tasks(
    tmp_path: Path,
    registry: ToolRegistry,
    scheduler: CoreToolScheduler,
) -> None:
    started = 0
    both_started = asyncio.Event()
    cleaned: list[str] = []

    async def runner(params: ScriptedParams) -> ToolExecutionResult:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(params.label)

    registry.register(
        ScriptedToolBuilder("read_probe", runner, tmp_path, read_only=True)
    )
    executor = _executor(registry, scheduler, max_concurrency=2)

    task = asyncio.create_task(
        executor.execute(
            [
                ToolCallSpec(call_id="call-1", name="read_probe", arguments={"label": "first"}),
                ToolCallSpec(call_id="call-2", name="read_probe", arguments={"label": "second"}),
            ]
        )
    )

    await asyncio.wait_for(both_started.wait(), timeout=0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(cleaned) == ["first", "second"]


@pytest.mark.asyncio
async def test_unknown_tool_matches_agent_convention(
    tmp_path: Path,
    registry: ToolRegistry,
    scheduler: CoreToolScheduler,
) -> None:
    async def runner(params: ScriptedParams) -> ToolExecutionResult:
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)

    registry.register(
        ScriptedToolBuilder("read_probe", runner, tmp_path, read_only=True)
    )
    executor = _executor(registry, scheduler)

    outcomes = await executor.execute(
        [
            ToolCallSpec(call_id="call-1", name="missing_tool", arguments={"label": "missing"}),
            ToolCallSpec(call_id="call-2", name="read_probe", arguments={"label": "known"}),
        ]
    )

    assert outcomes[0].observation.type == ObservationType.ERROR
    assert outcomes[0].observation.content == "unknown tool: missing_tool"
    assert outcomes[1].observation.type == ObservationType.TOOL_RESULT
    assert outcomes[1].observation.content == "known"
