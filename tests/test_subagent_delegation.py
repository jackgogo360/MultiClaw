import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from multiclaw.events import EventRouter, EventScope
from multiclaw.llm import LLMResponse
from multiclaw.llm.providers import ToolCall
from multiclaw.tenancy import TenantContext
from multiclaw.tools import ToolExecutionResult, ToolRegistry, ToolStatus
from multiclaw.tools.read_file import ReadFileToolBuilder
from multiclaw.tools.web_fetch import WebFetchToolBuilder
from multiclaw.tools.web_search import WebSearchToolBuilder
from multiclaw.tools.write_file import WriteFileToolBuilder


RUN_CONTEXT = TenantContext(
    tenant_id="11111111-1111-4111-8111-111111111111",
    workspace_id="22222222-2222-4222-8222-222222222222",
    session_id="33333333-3333-4333-8333-333333333333",
    run_id="44444444-4444-4444-8444-444444444444",
)


def _settings(**agent_overrides):
    agent = {
        "subagent_max_tasks": 3,
        "subagent_max_rounds": 3,
        "subagent_max_tokens": 10000,
        "subagent_timeout_seconds": 120,
    }
    agent.update(agent_overrides)
    return SimpleNamespace(
        agent=SimpleNamespace(**agent),
        llm=SimpleNamespace(default_model="test-model", model_providers={"alt-model": "openai"}),
    )


def _registry(tmp_path):
    registry = ToolRegistry()
    read = ReadFileToolBuilder(tmp_path)
    registry.register(read)
    registry.register(WriteFileToolBuilder(tmp_path, read))
    return registry


def test_delegate_tasks_contract_rejects_empty_and_oversized_batch():
    from multiclaw.agent.delegation import DelegateTasksParams

    with pytest.raises(ValidationError):
        DelegateTasksParams(tasks=[])
    with pytest.raises(ValidationError):
        DelegateTasksParams(tasks=[{"title": str(i), "objective": "inspect"} for i in range(4)])


def test_subagent_settings_default_off_and_bound_task_fanout():
    from multiclaw.config import Settings

    assert Settings(_config_file="/nonexistent").agent.subagents_enabled is False
    with pytest.raises(ValidationError):
        Settings(_config_file="/nonexistent", agent={"subagent_max_tasks": 4})


def test_runtime_registers_delegation_only_when_enabled():
    from multiclaw.config import Settings
    from multiclaw.runtime.factory import RuntimeFactory

    disabled = RuntimeFactory(
        settings=Settings(_config_file="/nonexistent"),
        database=SimpleNamespace(), workspace_resolver=SimpleNamespace(),
    )
    enabled = RuntimeFactory(
        settings=Settings(_config_file="/nonexistent", agent={"subagents_enabled": True}),
        database=SimpleNamespace(), workspace_resolver=SimpleNamespace(),
    )
    disabled_registry = ToolRegistry()
    enabled_registry = ToolRegistry()
    args = (SimpleNamespace(), SimpleNamespace(), EventRouter())

    disabled._register_delegation(disabled_registry, *args)
    enabled._register_delegation(enabled_registry, *args)

    assert disabled_registry.get("delegate_tasks") is None
    assert enabled_registry.get("delegate_tasks") is not None


@pytest.mark.asyncio
async def test_children_run_concurrently_with_independent_histories(tmp_path):
    from multiclaw.agent.delegation import DelegatedTask, ReadOnlyDelegationRunner

    active = 0
    maximum_active = 0
    seen = []

    class Router:
        async def completion(self, *, model, messages, tools, **kwargs):
            nonlocal active, maximum_active
            assert model == "test-model"
            assert all(schema["function"]["name"] == "read_file" for schema in tools)
            active += 1
            maximum_active = max(maximum_active, active)
            seen.append([dict(message) for message in messages])
            try:
                await asyncio.sleep(0.02)
                return LLMResponse(content=messages[1]["content"] + " done")
            finally:
                active -= 1

    runner = ReadOnlyDelegationRunner(
        settings=_settings(), router=Router(), registry=_registry(tmp_path),
        scheduler=SimpleNamespace(), event_router=None,
    )
    results = await runner.run(
        [DelegatedTask(title="one", objective="inspect alpha"),
         DelegatedTask(title="two", objective="inspect beta")],
        context=RUN_CONTEXT,
    )

    assert maximum_active == 2
    assert [item.title for item in results] == ["one", "two"]
    assert [item.status for item in results] == ["completed", "completed"]
    assert [item.summary for item in results] == ["inspect alpha done", "inspect beta done"]
    assert len(seen) == 2
    assert {history[1]["content"] for history in seen} == {"inspect alpha", "inspect beta"}
    assert all(len(history) == 2 for history in seen)


@pytest.mark.asyncio
async def test_child_model_choice_is_limited_to_configured_models(tmp_path):
    from multiclaw.agent.delegation import DelegatedTask, ReadOnlyDelegationRunner

    class Router:
        models = []

        async def completion(self, *, model, messages, tools, **kwargs):
            self.models.append(model)
            return LLMResponse(content="done")

    router = Router()
    runner = ReadOnlyDelegationRunner(
        settings=_settings(), router=router, registry=_registry(tmp_path),
        scheduler=SimpleNamespace(), event_router=None,
    )
    results = await runner.run([
        DelegatedTask(title="allowed", objective="inspect", model="alt-model"),
        DelegatedTask(title="denied", objective="inspect", model="unconfigured-model"),
    ], context=RUN_CONTEXT)

    assert results[0].status == "completed"
    assert results[1].status == "failed"
    assert router.models == ["alt-model"]


@pytest.mark.asyncio
async def test_child_cannot_dispatch_write_tool_even_if_model_forges_call(tmp_path):
    from multiclaw.agent.delegation import DelegatedTask, ReadOnlyDelegationRunner

    class Router:
        async def completion(self, *, model, messages, tools, **kwargs):
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="write-1", name="write_file", arguments={
                    "file_path": "forbidden.txt", "content": "bad",
                })],
            )

    class Scheduler:
        calls = 0

        async def run(self, *args, **kwargs):
            self.calls += 1
            return ToolExecutionResult(status=ToolStatus.SUCCESS, content="unexpected")

    scheduler = Scheduler()
    runner = ReadOnlyDelegationRunner(
        settings=_settings(), router=Router(), registry=_registry(tmp_path),
        scheduler=scheduler, event_router=None,
    )
    result, = await runner.run(
        [DelegatedTask(title="inspect", objective="read files")], context=RUN_CONTEXT,
    )

    assert result.status == "failed"
    assert "not permitted" in result.summary
    assert scheduler.calls == 0
    assert not (tmp_path / "forbidden.txt").exists()


@pytest.mark.asyncio
async def test_child_never_advertises_network_tools(tmp_path):
    from multiclaw.agent.delegation import DelegatedTask, ReadOnlyDelegationRunner

    registry = _registry(tmp_path)
    registry.register(WebFetchToolBuilder(tmp_path))
    registry.register(WebSearchToolBuilder(tmp_path))

    class Router:
        async def completion(self, *, model, messages, tools, **kwargs):
            assert {schema["function"]["name"] for schema in tools} == {"read_file"}
            return LLMResponse(content="done")

    runner = ReadOnlyDelegationRunner(
        settings=_settings(), router=Router(), registry=registry,
        scheduler=SimpleNamespace(), event_router=None,
    )
    result, = await runner.run(
        [DelegatedTask(title="inspect", objective="read local files")], context=RUN_CONTEXT,
    )
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_child_can_read_workspace_file_through_governed_scheduler(tmp_path):
    from multiclaw.agent.delegation import DelegatedTask, ReadOnlyDelegationRunner
    from multiclaw.events import EventBus
    from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
    from multiclaw.tools import CoreToolScheduler

    (tmp_path / "notes.txt").write_text("tenant scoped evidence", encoding="utf-8")

    class Router:
        calls = 0

        async def completion(self, *, model, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(
                    id="read-1", name="read_file", arguments={"file_path": "notes.txt"},
                )])
            assert messages[-1]["role"] == "tool"
            assert "tenant scoped evidence" in messages[-1]["content"]
            return LLMResponse(content="Found tenant scoped evidence in notes.txt")

    scheduler = CoreToolScheduler(
        permission_checker=PermissionChecker(), execution_guard=ExecutionGuard(),
        audit_logger=InMemoryAuditLogger(), event_bus=EventBus(),
    )
    runner = ReadOnlyDelegationRunner(
        settings=_settings(), router=Router(), registry=_registry(tmp_path),
        scheduler=scheduler, event_router=None,
    )
    result, = await runner.run(
        [DelegatedTask(title="inspect", objective="read notes")], context=RUN_CONTEXT,
    )

    assert result.status == "completed"
    assert result.tool_calls == 1
    assert "notes.txt" in result.summary


@pytest.mark.asyncio
async def test_child_events_inherit_exact_parent_scope(tmp_path):
    from multiclaw.agent.delegation import DelegatedTask, ReadOnlyDelegationRunner

    class Router:
        async def completion(self, *, model, messages, tools, **kwargs):
            return LLMResponse(content="found it")

    event_router = EventRouter()
    events = []

    async def collect(event):
        events.append(event)

    subscription = event_router.subscribe(EventScope.from_context(RUN_CONTEXT), collect)
    runner = ReadOnlyDelegationRunner(
        settings=_settings(), router=Router(), registry=_registry(tmp_path),
        scheduler=SimpleNamespace(), event_router=event_router,
    )
    result, = await runner.run(
        [DelegatedTask(title="inspect", objective="read files")], context=RUN_CONTEXT,
    )
    subscription.close()

    assert [event.event_type for event in events] == ["subagent.started", "subagent.completed"]
    assert all(event.run_id == RUN_CONTEXT.run_id for event in events)
    assert all(event.tenant_id == RUN_CONTEXT.tenant_id for event in events)
    assert events[0].data["child_id"] == events[1].data["child_id"] == result.child_id


@pytest.mark.asyncio
async def test_real_inference_wrapper_keeps_parent_steering_out_of_children(tmp_path):
    from multiclaw.agent.delegation import DelegatedTask, ReadOnlyDelegationRunner
    from multiclaw.config import Settings
    from multiclaw.runtime.inference import InferenceRouter, inference_scope
    from multiclaw.runtime.run_control import RunControl, current_run_control

    observed = []

    async def completion(**kwargs):
        observed.append(kwargs)
        return LLMResponse(content="child report", usage={})

    settings = Settings(_config_file="/nonexistent")
    router = InferenceRouter(
        SimpleNamespace(completion=completion, supports_output_limit=True),
        settings=settings, workspace_root=tmp_path,
    )
    runner = ReadOnlyDelegationRunner(
        settings=settings, router=router, registry=_registry(tmp_path),
        scheduler=SimpleNamespace(), event_router=None,
    )
    control = RunControl(RUN_CONTEXT)
    control.steering.append("private correction for parent")
    token = current_run_control.set(control)
    try:
        with inference_scope(RUN_CONTEXT, settings=settings) as budget:
            budget.objective = "private parent objective"
            result, = await runner.run(
                [DelegatedTask(title="inspect", objective="inspect local files")],
                context=RUN_CONTEXT,
            )
            assert list(control.steering) == ["private correction for parent"]
            assert budget.steering == []
    finally:
        current_run_control.reset(token)

    assert result.status == "completed"
    child_prompt = str(observed[0]["messages"])
    assert "private correction for parent" not in child_prompt
    assert "private parent objective" not in child_prompt


@pytest.mark.asyncio
async def test_parent_task_cancellation_cancels_children(tmp_path):
    from multiclaw.agent.delegation import DelegatedTask, ReadOnlyDelegationRunner

    started = asyncio.Event()
    cancelled = 0

    class Router:
        async def completion(self, *, model, messages, tools, **kwargs):
            nonlocal cancelled
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled += 1
                raise
            return LLMResponse(content="late")

    runner = ReadOnlyDelegationRunner(
        settings=_settings(), router=Router(), registry=_registry(tmp_path),
        scheduler=SimpleNamespace(), event_router=None,
    )
    parent = asyncio.create_task(runner.run(
        [DelegatedTask(title="one", objective="inspect alpha"),
         DelegatedTask(title="two", objective="inspect beta")],
        context=RUN_CONTEXT,
    ))
    await started.wait()
    await asyncio.sleep(0)
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert cancelled == 2


@pytest.mark.asyncio
async def test_delegate_tool_uses_parent_inference_scope_and_returns_child_ids():
    from multiclaw.agent.delegation import DelegationResult
    from multiclaw.runtime.inference import inference_scope
    from multiclaw.tools.delegate_tasks import DelegateTasksToolBuilder

    class Runner:
        async def run(self, tasks, *, context):
            assert context == RUN_CONTEXT
            assert tasks[0].objective == "inspect"
            return [DelegationResult("child-1", "one", "completed", "evidence")]

    settings = _settings()
    builder = DelegateTasksToolBuilder(settings=settings, runner=Runner())
    params = builder.validate({"tasks": [{"title": "one", "objective": "inspect"}]})
    with inference_scope(RUN_CONTEXT, settings=settings):
        result = await builder.build(params).execute()

    assert result.status is ToolStatus.SUCCESS
    assert result.data["children"][0]["child_id"] == "child-1"
    assert "evidence" in result.content
    with pytest.raises(ValidationError):
        builder.validate({"tasks": [{"title": str(i), "objective": "inspect"} for i in range(4)]})


@pytest.mark.asyncio
async def test_delegate_tool_reports_error_when_every_child_failed():
    from multiclaw.agent.delegation import DelegationResult
    from multiclaw.runtime.inference import inference_scope
    from multiclaw.tools.delegate_tasks import DelegateTasksToolBuilder

    class Runner:
        async def run(self, tasks, *, context):
            return [DelegationResult("child-1", "one", "failed", "No evidence")]

    settings = _settings()
    builder = DelegateTasksToolBuilder(settings=settings, runner=Runner())
    params = builder.validate({"tasks": [{"title": "one", "objective": "inspect"}]})
    with inference_scope(RUN_CONTEXT, settings=settings):
        result = await builder.build(params).execute()

    assert result.status is ToolStatus.ERROR
    assert result.data["children"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_delegate_tool_rejects_mismatched_parent_contexts():
    from multiclaw.runtime.inference import inference_scope
    from multiclaw.runtime.run_control import RunControl, current_run_control
    from multiclaw.tools.delegate_tasks import DelegateTasksToolBuilder

    class Runner:
        calls = 0

        async def run(self, tasks, *, context):
            self.calls += 1
            return []

    runner = Runner()
    settings = _settings()
    builder = DelegateTasksToolBuilder(settings=settings, runner=runner)
    params = builder.validate({"tasks": [{"title": "one", "objective": "inspect"}]})
    other = RUN_CONTEXT.for_run(RUN_CONTEXT.session_id, "55555555-5555-4555-8555-555555555555")
    token = current_run_control.set(RunControl(other))
    try:
        with inference_scope(RUN_CONTEXT, settings=settings):
            result = await builder.build(params).execute()
    finally:
        current_run_control.reset(token)

    assert result.status is ToolStatus.ERROR
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_execution_guard_supports_bounded_tool_timeout_override():
    from multiclaw.governance import ExecutionGuard

    guard = ExecutionGuard(timeout=0.001)
    async def operation():
        await asyncio.sleep(0.02)
        return "finished"

    result = await guard.run(operation, timeout=0.1)
    assert result == "finished"
