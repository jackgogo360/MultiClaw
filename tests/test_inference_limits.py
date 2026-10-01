from types import SimpleNamespace

import pytest

from multiclaw.config.settings import Settings
from multiclaw.tenancy import TenantContext


def configuration(tmp_path, **runtime):
    return Settings(_config_file=str(tmp_path / "absent.toml"), runtime=runtime)


def test_p0_limits_are_validated(tmp_path):
    settings = configuration(tmp_path, max_run_tokens=1200, max_run_seconds=30)
    assert settings.runtime.max_run_tokens == 1200
    assert settings.runtime.max_run_seconds == 30
    with pytest.raises(ValueError):
        configuration(tmp_path, max_run_tokens=-1)


async def test_run_limit_blocks_next_model_call(tmp_path):
    from multiclaw.runtime.inference import (
        InferenceRouter,
        RunBudgetExceeded,
        inference_scope,
    )

    settings = configuration(tmp_path, max_run_tokens=40)
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content="ok", usage={"input_tokens": 20, "output_tokens": 20, "total_tokens": 40})

    router = InferenceRouter(SimpleNamespace(completion=completion), settings=settings)
    context = TenantContext(tenant_id="tenant", workspace_id="workspace", session_id="session", run_id="run")
    with inference_scope(context, settings=settings):
        await router.completion(model="test", messages=[{"role": "user", "content": "hello"}])
        with pytest.raises(RunBudgetExceeded):
            await router.completion(model="test", messages=[{"role": "user", "content": "again"}])
    assert len(calls) == 1


async def test_usage_without_provider_usage_is_estimated(tmp_path):
    from multiclaw.runtime.inference import InferenceRouter, inference_scope

    settings = configuration(tmp_path)

    async def completion(**kwargs):
        return SimpleNamespace(content="a response", usage={})

    router = InferenceRouter(SimpleNamespace(completion=completion), settings=settings)
    context = TenantContext(tenant_id="tenant", workspace_id="workspace", session_id="session", run_id="run")
    with inference_scope(context, settings=settings) as budget:
        await router.completion(model="test", messages=[{"role": "user", "content": "hello"}])
        assert budget.total_tokens > 0
        assert budget.estimated is True
        assert budget.model_calls == 1


async def test_stream_budget_is_isolated_between_runs(tmp_path):
    from multiclaw.runtime.inference import InferenceRouter, inference_scope

    settings = configuration(tmp_path)

    async def stream_completion(**kwargs):
        yield {"type": "token", "content": "answer"}
        yield {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}

    router = InferenceRouter(SimpleNamespace(stream_completion=stream_completion), settings=settings)
    context = TenantContext(tenant_id="tenant", workspace_id="workspace", session_id="session", run_id="run")
    with inference_scope(context, settings=settings) as budget:
        events = [event async for event in router.stream_completion(model="test", messages=[{"role": "user", "content": "hi"}])]
        assert budget.total_tokens == 5
        assert events[0]["content"] == "answer"
    with inference_scope(context.for_run("session", "other-run"), settings=settings) as other:
        assert other.total_tokens == 0


def test_signed_reasoning_is_preserved_with_tool_calls():
    from multiclaw.agent.multiclaw import _build_assistant_tool_calls_msg

    blocks = [{"type": "thinking", "thinking": "reason", "signature": "signed"}]
    message = _build_assistant_tool_calls_msg(
        [{"id": "call", "name": "read_file", "arguments": {"file_path": "README.md"}}],
        "reason", reasoning_blocks=blocks,
    )
    assert message["reasoning_blocks"] == blocks


async def test_project_instruction_order_and_current_objective(tmp_path):
    from multiclaw.runtime.inference import InferenceRouter, inference_scope

    (tmp_path / "AGENTS.md").write_text("root instructions")
    child = tmp_path / "src"
    child.mkdir()
    (child / "AGENTS.md").write_text("child instructions")
    observed = []

    async def completion(**kwargs):
        observed.extend(kwargs["messages"])
        return SimpleNamespace(content="done", usage={})

    settings = configuration(tmp_path)
    router = InferenceRouter(SimpleNamespace(completion=completion), settings=settings, workspace_root=tmp_path)
    messages = [
        {"role": "system", "content": "assistant"},
        {"role": "user", "content": "old unrelated conversation"},
        {"role": "user", "content": "current objective"},
        {"role": "assistant", "tool_calls": [{"id": "call", "type": "function", "function": {"name": "read_file", "arguments": '{"file_path":"src/file.py"}'}}]},
        {"role": "tool", "tool_call_id": "call", "content": "file content"},
    ]
    context = TenantContext(tenant_id="tenant", workspace_id="workspace", session_id="session", run_id="run")
    with inference_scope(context, settings=settings):
        await router.completion(model="test", messages=messages)
    instructions = [message["content"] for message in observed if message.get("role") == "system"]
    assert next(i for i, text in enumerate(instructions) if "root instructions" in text) < next(i for i, text in enumerate(instructions) if "child instructions" in text)
    assert any("Current task objective:\ncurrent objective" in text for text in instructions)


@pytest.fixture
async def persisted_scope(tmp_path):
    from uuid import uuid4

    from multiclaw.config.settings import DatabaseSettings
    from multiclaw.storage import Database
    from multiclaw.storage.schema import metadata
    from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork

    database = Database.create(DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}"))
    async with database.engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(f"{uuid4()}@example.com")
    context = TenantContext(user.id, user.default_workspace_id)
    async with TenantUnitOfWork(database, context) as uow:
        session = await uow.sessions.create()
    yield database, context.for_run(session.id, str(uuid4()))
    await database.dispose()


async def test_daily_usage_survives_session_deletion(persisted_scope, tmp_path):
    from multiclaw.runtime.inference import InferenceRouter, inference_scope
    from multiclaw.storage.uow import TenantUnitOfWork

    database, context = persisted_scope
    settings = configuration(tmp_path, tenant_daily_token_limit=10000)

    async def completion(**kwargs):
        return SimpleNamespace(content="done", usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})

    router = InferenceRouter(SimpleNamespace(completion=completion), settings=settings)
    with inference_scope(context, settings=settings, database=database):
        await router.completion(model="test", messages=[{"role": "user", "content": "hello"}])
    root_context = TenantContext(context.tenant_id, context.workspace_id)
    async with TenantUnitOfWork(database, root_context) as uow:
        await uow.sessions.delete(context.session_id)
    async with TenantUnitOfWork(database, root_context) as uow:
        usage = await uow.memory.recent(10, entry_type="tenant_usage")
    assert usage[0].metadata["total_tokens"] == 150


async def test_resumed_run_loads_usage_before_call(persisted_scope, tmp_path):
    from multiclaw.runtime.inference import (
        InferenceRouter,
        RunBudgetExceeded,
        inference_scope,
    )

    database, context = persisted_scope
    settings = configuration(tmp_path, max_run_tokens=100)
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content="done", usage={"input_tokens": 70, "output_tokens": 30, "total_tokens": 100})

    router = InferenceRouter(SimpleNamespace(completion=completion), settings=settings)
    with inference_scope(context, settings=settings, database=database):
        await router.completion(model="test", messages=[{"role": "user", "content": "hello"}])
    with inference_scope(context, settings=settings, database=database), pytest.raises(RunBudgetExceeded):
        await router.completion(model="test", messages=[{"role": "user", "content": "again"}])
    assert len(calls) == 1


async def test_steering_survives_a_reflection_copy(tmp_path):
    from multiclaw.runtime.inference import InferenceRouter, inference_scope
    from multiclaw.runtime.run_control import RunControl, current_run_control

    context = TenantContext("tenant", "workspace", "session", "run")
    control = RunControl(context)
    control.steering.append("new constraint")
    observed = []

    async def completion(**kwargs):
        observed.append([item.copy() for item in kwargs["messages"]])
        return SimpleNamespace(content="ok", usage={})

    settings = configuration(tmp_path)
    router = InferenceRouter(SimpleNamespace(completion=completion), settings=settings)
    history = [{"role": "user", "content": "task"}]
    token = current_run_control.set(control)
    try:
        with inference_scope(context, settings=settings):
            await router.completion(model="test", messages=[*history, {"role": "system", "content": "reflect"}])
            await router.completion(model="test", messages=history)
    finally:
        current_run_control.reset(token)
    assert any(item.get("content") == "new constraint" for item in observed[-1])


async def test_concurrent_calls_reserve_run_budget(tmp_path):
    import asyncio

    from multiclaw.runtime.inference import (
        InferenceRouter,
        RunBudgetExceeded,
        inference_scope,
    )

    entered, release = asyncio.Event(), asyncio.Event()

    async def completion(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(content="ok", usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20})

    settings = configuration(tmp_path, max_run_tokens=100)
    router = InferenceRouter(SimpleNamespace(completion=completion), settings=settings)
    context = TenantContext("tenant", "workspace", "session", "run")
    with inference_scope(context, settings=settings):
        first = asyncio.create_task(router.completion(model="test", messages=[{"role": "user", "content": "hello"}]))
        await entered.wait()
        try:
            with pytest.raises(RunBudgetExceeded):
                await asyncio.wait_for(router.completion(model="test", messages=[{"role": "user", "content": "hello"}]), 0.5)
        finally:
            release.set()
            await first
