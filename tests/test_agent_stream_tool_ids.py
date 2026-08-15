import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from multiclaw.agent.base import BaseAgent
from multiclaw.agent.multiclaw import MultiClawAgent
from multiclaw.agent.models import Observation, ObservationType
from multiclaw.agent.tool_batch import ToolBatchExecutor
from multiclaw.context import ContextBuildReport, ContextBuildResult
from multiclaw.events import AgentState, AgentStateEvent, EventBus
from multiclaw.llm import LLMResponse
from multiclaw.tenancy import TenantContext
from multiclaw.tools import (
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)


class _DummySkillManager:
    def process_message(self, _message: str):
        return []

    def get_active_skill_prompts(self):
        return []

    def invoke(self, _skill_name: str, _args: str):
        return None


class _DummyContextBuilder:
    async def build(self, _request):
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]

    async def build_with_report(self, request):
        return ContextBuildResult(
            messages=await self.build(request),
            report=ContextBuildReport(
                limit_tokens=1000,
                reserved_response_tokens=0,
                used_tokens_by_level={"L0": 1, "L1": 0, "L2": 0},
                dropped_by_level={"L0": 0, "L1": 0, "L2": 0},
            ),
        )


class _ReportOnlyContextBuilder:
    def __init__(self, messages: list[dict[str, str]]) -> None:
        self.build_with_report = AsyncMock(
            return_value=ContextBuildResult(
                messages=messages,
                report=ContextBuildReport(
                    limit_tokens=321,
                    reserved_response_tokens=123,
                    used_tokens_by_level={"L0": 3, "L1": 4, "L2": 5},
                    dropped_by_level={"L0": 0, "L1": 1, "L2": 2},
                ),
            )
        )

    async def build(self, _request):
        raise AssertionError("handle_message_stream should use build_with_report")


class _DummyMemory:
    async def recent(self, _context, limit: int, entry_type: str | None = None):
        del limit, entry_type
        return []

    async def save(self, _entry):
        return None


class _DummyRouter:
    async def stream_completion(self, **_kwargs):
        yield {
            "type": "tool_calls",
            "calls": [
                {
                    "id": "call_123",
                    "name": "web_search",
                    "arguments": {"query": "hello"},
                }
            ],
            "reasoning_content": "",
        }


class _QueuedStreamRouter:
    def __init__(self, stream_sequences, completion_responses=None) -> None:
        self.stream_sequences = list(stream_sequences)
        self.completion_responses = list(completion_responses or [])
        self.stream_calls: list[dict] = []
        self.completion_calls: list[dict] = []

    async def stream_completion(self, **kwargs):
        self.stream_calls.append(kwargs)
        events = self.stream_sequences.pop(0)
        for event in events:
            yield event

    async def completion(self, **kwargs):
        self.completion_calls.append(kwargs)
        response = self.completion_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _DummyDsmlRetryRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_completion(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_calls",
                "calls": [
                    {
                        "id": "call_123",
                        "name": "web_search",
                        "arguments": {"query": "hello"},
                    }
                ],
                "reasoning_content": "",
            }
            return

        if self.calls == 2:
            yield {
                "type": "token",
                "content": "<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name=\"web_search\"></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>",
            }
            return

        yield {"type": "token", "content": "Final summary"}


class _SequentialStreamRouter:
    def __init__(self, stream_sequences) -> None:
        self.stream_sequences = list(stream_sequences)
        self.stream_calls: list[dict] = []

    async def stream_completion(self, **kwargs):
        self.stream_calls.append(kwargs)
        events = self.stream_sequences.pop(0)
        if isinstance(events, Exception):
            raise events
        for event in events:
            yield event


class _TokenThenBlockRouter:
    def __init__(self) -> None:
        self.block = asyncio.Event()

    async def stream_completion(self, **_kwargs):
        yield {"type": "token", "content": "partial"}
        await self.block.wait()


class _ScriptedParams(BaseModel):
    query: str | None = None
    label: str | None = None
    delay: float = 0.0


class _ScriptedInvocation(ToolInvocation[_ScriptedParams]):
    def __init__(self, name: str, params: _ScriptedParams, runner) -> None:
        super().__init__(name=name, params=params)
        self._runner = runner

    async def execute(self) -> ToolExecutionResult:
        return await self._runner(self.params)


class _ScriptedToolBuilder(ToolBuilder[_ScriptedParams]):
    description = "Scripted stream test tool"
    parameters_schema = _ScriptedParams

    def __init__(self, name: str, runner, *, read_only: bool) -> None:
        self.name = name
        self._runner = runner
        self.read_only = read_only

    def validate(self, params: dict) -> _ScriptedParams:
        return _ScriptedParams(**params)

    def build(self, params: _ScriptedParams) -> ToolInvocation[_ScriptedParams]:
        return _ScriptedInvocation(self.name, params, self._runner)


class _BatchScheduler:
    def __init__(self) -> None:
        self.run_calls: list[tuple[str, dict]] = []

    async def can_run_concurrently(self, builder, raw_params: dict) -> bool:
        return builder.read_only

    async def run(self, builder, raw_params: dict, **_kwargs) -> ToolExecutionResult:
        self.run_calls.append((builder.name, raw_params))
        params = builder.validate(raw_params)
        return await builder.build(params).execute()


def _stream_context(session_id: str = "s1", run_id: str = "r1") -> TenantContext:
    return TenantContext("tenant-a", "workspace-a").for_run(session_id, run_id)


@pytest.mark.asyncio
async def test_handle_message_stream_preserves_tool_call_ids(monkeypatch):
    agent = _build_custom_stream_agent(
        router=_DummyRouter(),
        registry=_single_tool_registry("web_search", ["search results"]),
        scheduler=_BatchScheduler(),
        parallel_enabled=True,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=1,
    )

    events = []
    async for event in agent.handle_message_stream("hello", context=_stream_context()):
        events.append(event)

    tool_call = next(event for event in events if event["type"] == "tool_call")
    tool_result = next(event for event in events if event["type"] == "tool_result")

    assert tool_call["call_id"] == "call_123"
    assert tool_result["call_id"] == "call_123"


@pytest.mark.asyncio
async def test_handle_message_stream_uses_context_build_with_report_and_only_prompt_messages():
    expected_messages = [
        {"role": "system", "content": "sys"},
        {"role": "system", "content": "skill guidance"},
        {"role": "user", "content": "hello"},
    ]
    router = _QueuedStreamRouter(
        stream_sequences=[[{"type": "token", "content": "streamed"}]]
    )
    agent = _build_custom_stream_agent(
        router=router,
        registry=ToolRegistry(),
        scheduler=_BatchScheduler(),
        parallel_enabled=True,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=1,
    )
    agent.context_builder = _ReportOnlyContextBuilder(expected_messages)

    events = []
    with patch("multiclaw.agent.multiclaw.logger.info") as mock_info:
        async for event in agent.handle_message_stream("hello", context=_stream_context()):
            events.append(event)

    assert events[-1] == {"type": "done", "content": "streamed", "data": {}}
    agent.context_builder.build_with_report.assert_awaited_once()
    request = agent.context_builder.build_with_report.await_args.args[0]
    assert request.user_input == "hello"
    assert request.context.session_id == "s1"
    assert request.context_window_limit == 1000
    assert request.skill_prompts == []
    assert router.stream_calls[0]["messages"] == expected_messages
    matching_calls = [
        call.args
        for call in mock_info.call_args_list
        if call.args
        and call.args[0] == "context_budget used=%s dropped=%s limit=%d reserve=%d"
    ]
    assert matching_calls == [
        (
            "context_budget used=%s dropped=%s limit=%d reserve=%d",
            {"L0": 3, "L1": 4, "L2": 5},
            {"L0": 0, "L1": 1, "L2": 2},
            321,
            123,
        )
    ]
    assert mock_info.call_count >= 1
    assert all(set(message.keys()) <= {"role", "content"} for message in expected_messages)


@pytest.mark.asyncio
async def test_handle_message_stream_retries_when_final_summary_contains_dsml():
    router = _DummyDsmlRetryRouter()
    agent = _build_custom_stream_agent(
        router=router,
        registry=_single_tool_registry("web_search", ["search results"]),
        scheduler=_BatchScheduler(),
        parallel_enabled=True,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=1,
    )

    events = []
    async for event in agent.handle_message_stream("hello", context=_stream_context()):
        events.append(event)

    done = next(event for event in events if event["type"] == "done")
    tokens = [event["content"] for event in events if event["type"] == "token"]

    assert router.calls == 3
    assert done["content"] == "Final summary"
    assert "".join(tokens) == "Final summary"


@pytest.mark.asyncio
async def test_handle_message_stream_parallel_read_only_batch_overlaps_when_enabled():
    timeline: list[str] = []
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    active = 0
    max_active = 0

    async def runner(params: _ScriptedParams) -> ToolExecutionResult:
        nonlocal active, max_active
        label = params.label or ""
        timeline.append(f"exec:{label}")
        active += 1
        max_active = max(max_active, active)
        try:
            if label == "first":
                first_started.set()
                await asyncio.wait_for(second_started.wait(), timeout=0.1)
                await asyncio.sleep(0.02)
            else:
                second_started.set()
                await asyncio.sleep(0.0)
            return ToolExecutionResult(
                status=ToolStatus.SUCCESS,
                content=f"done:{label}",
            )
        finally:
            active -= 1

    registry = ToolRegistry()
    registry.register(_ScriptedToolBuilder("web_search", runner, read_only=True))
    agent = _build_custom_stream_agent(
        router=_QueuedStreamRouter(
            stream_sequences=[
                [
                    _tool_call_batch_event(
                        ("call_1", "web_search", {"label": "first"}),
                        ("call_2", "web_search", {"label": "second"}),
                    )
                ],
                [{"type": "token", "content": "finished"}],
            ]
        ),
        registry=registry,
        scheduler=_BatchScheduler(),
        parallel_enabled=True,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=2,
    )

    events = []
    async for event in agent.handle_message_stream("hello", context=_stream_context()):
        events.append(event)
        if event["type"] == "tool_call":
            timeline.append(f"ui_call:{event['call_id']}")
        if event["type"] == "tool_result":
            timeline.append(f"ui_result:{event['call_id']}")

    assert first_started.is_set()
    assert second_started.is_set()
    assert max_active == 2
    assert timeline[:2] == ["ui_call:call_1", "ui_call:call_2"]
    assert [event["call_id"] for event in events if event["type"] == "tool_result"] == [
        "call_1",
        "call_2",
    ]
    assert [call.args[1] for call in agent.remember.await_args_list] == [
        "done:first",
        "done:second",
    ]
    assert agent.act.await_count == 0


@pytest.mark.asyncio
async def test_handle_message_stream_read_write_read_batch_preserves_serial_barriers():
    execution_order: list[str] = []
    active = 0
    max_active = 0

    async def runner(params: _ScriptedParams) -> ToolExecutionResult:
        nonlocal active, max_active
        label = params.label or ""
        execution_order.append(f"start:{label}")
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            execution_order.append(f"end:{label}")
            return ToolExecutionResult(
                status=ToolStatus.SUCCESS,
                content=label,
            )
        finally:
            active -= 1

    registry = ToolRegistry()
    registry.register(_ScriptedToolBuilder("read_probe", runner, read_only=True))
    registry.register(_ScriptedToolBuilder("write_probe", runner, read_only=False))
    agent = _build_custom_stream_agent(
        router=_QueuedStreamRouter(
            stream_sequences=[
                [
                    _tool_call_batch_event(
                        ("call_1", "read_probe", {"label": "read-1"}),
                        ("call_2", "write_probe", {"label": "write"}),
                        ("call_3", "read_probe", {"label": "read-2"}),
                    )
                ],
                [{"type": "token", "content": "finished"}],
            ]
        ),
        registry=registry,
        scheduler=_BatchScheduler(),
        parallel_enabled=True,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=2,
    )

    events = []
    async for event in agent.handle_message_stream("hello", context=_stream_context()):
        events.append(event)

    assert max_active == 1
    assert execution_order == [
        "start:read-1",
        "end:read-1",
        "start:write",
        "end:write",
        "start:read-2",
        "end:read-2",
    ]
    assert [event["call_id"] for event in events if event["type"] == "tool_call"] == [
        "call_1",
        "call_2",
        "call_3",
    ]
    assert [event["content"] for event in events if event["type"] == "tool_result"] == [
        "read-1",
        "write",
        "read-2",
    ]


@pytest.mark.asyncio
async def test_handle_message_stream_preserves_original_tool_call_ids_and_result_order():
    finished: list[str] = []
    active = 0
    max_active = 0

    async def runner(params: _ScriptedParams) -> ToolExecutionResult:
        nonlocal active, max_active
        label = params.label or ""
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(params.delay)
            finished.append(label)
            return ToolExecutionResult(
                status=ToolStatus.SUCCESS,
                content=f"result:{label}",
            )
        finally:
            active -= 1

    registry = ToolRegistry()
    registry.register(_ScriptedToolBuilder("web_search", runner, read_only=True))
    router = _QueuedStreamRouter(
        stream_sequences=[
            [
                _tool_call_batch_event(
                    ("call_beta", "web_search", {"label": "first", "delay": 0.03}),
                    ("call_alpha", "web_search", {"label": "second", "delay": 0.0}),
                )
            ],
            [{"type": "token", "content": "finished"}],
        ]
    )
    agent = _build_custom_stream_agent(
        router=router,
        registry=registry,
        scheduler=_BatchScheduler(),
        parallel_enabled=True,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=2,
    )

    events = []
    async for event in agent.handle_message_stream("hello", context=_stream_context()):
        events.append(event)

    followup_messages = router.stream_calls[1]["messages"]
    assistant_message = next(
        message for message in followup_messages if message.get("tool_calls")
    )
    tool_messages = [message for message in followup_messages if message["role"] == "tool"]

    assert max_active == 2
    assert finished == ["second", "first"]
    assert [event["call_id"] for event in events if event["type"] == "tool_result"] == [
        "call_beta",
        "call_alpha",
    ]
    assert [tool_call["id"] for tool_call in assistant_message["tool_calls"]] == [
        "call_beta",
        "call_alpha",
    ]
    assert [message["tool_call_id"] for message in tool_messages[-2:]] == [
        "call_beta",
        "call_alpha",
    ]
    assert [message["content"] for message in tool_messages[-2:]] == [
        "result:first",
        "result:second",
    ]


@pytest.mark.asyncio
async def test_handle_message_stream_parallel_flag_disabled_keeps_read_only_batch_serial():
    active = 0
    max_active = 0
    finished: list[str] = []

    async def runner(params: _ScriptedParams) -> ToolExecutionResult:
        nonlocal active, max_active
        label = params.label or ""
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(params.delay)
            finished.append(label)
            return ToolExecutionResult(
                status=ToolStatus.SUCCESS,
                content=label,
            )
        finally:
            active -= 1

    registry = ToolRegistry()
    scheduler = _BatchScheduler()
    registry.register(_ScriptedToolBuilder("web_search", runner, read_only=True))
    agent = _build_custom_stream_agent(
        router=_QueuedStreamRouter(
            stream_sequences=[
                [
                    _tool_call_batch_event(
                        ("call_1", "web_search", {"label": "first", "delay": 0.01}),
                        ("call_2", "web_search", {"label": "second", "delay": 0.0}),
                    )
                ],
                [{"type": "token", "content": "finished"}],
            ]
        ),
        registry=registry,
        scheduler=scheduler,
        parallel_enabled=False,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=2,
    )

    events = []
    async for event in agent.handle_message_stream("hello", context=_stream_context()):
        events.append(event)

    assert max_active == 1
    assert finished == ["first", "second"]
    assert scheduler.run_calls == [
        ("web_search", {"label": "first", "delay": 0.01}),
        ("web_search", {"label": "second", "delay": 0.0}),
    ]
    assert [event["call_id"] for event in events if event["type"] == "tool_result"] == [
        "call_1",
        "call_2",
    ]


@pytest.mark.asyncio
async def test_handle_message_stream_reflects_without_emitting_repeated_tool_ui_events():
    router = _QueuedStreamRouter(
        stream_sequences=[
            [_tool_calls_event("call_1", {"query": "alpha"})],
            [_tool_calls_event("call_2", {"query": "alpha"})],
            [_tool_calls_event("call_3", {"query": "alpha"})],
            [{"type": "token", "content": "changed approach"}],
        ],
        completion_responses=[LLMResponse(content="Use a new tool path")],
    )
    agent = _build_stub_stream_agent(
        router=router,
        act_results=["first result", "second result"],
        resilience_enabled=True,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=6,
    )

    events = []
    async for event in agent.handle_message_stream("hello", context=_stream_context()):
        events.append(event)

    tool_calls = [event for event in events if event["type"] == "tool_call"]
    tool_results = [event for event in events if event["type"] == "tool_result"]
    reflection_state = next(event for event in events if event["type"] == "state")
    done = next(event for event in events if event["type"] == "done")

    assert [event["call_id"] for event in tool_calls] == ["call_1", "call_2"]
    assert [event["call_id"] for event in tool_results] == ["call_1", "call_2"]
    assert reflection_state == {
        "type": "state",
        "name": "reflection",
        "content": "Use a new tool path",
    }
    assert done == {"type": "done", "content": "changed approach", "data": {}}

    reflection_call = next(
        call
        for call in router.completion_calls
        if call["tools"] is None
        and call["messages"][-1]["role"] == "system"
        and "Runtime reflection required." in call["messages"][-1]["content"]
    )
    assert reflection_call["tools"] is None


@pytest.mark.asyncio
async def test_handle_message_stream_forces_summary_when_reflection_generation_fails():
    router = _QueuedStreamRouter(
        stream_sequences=[
            [_tool_calls_event("call_1", {"query": "alpha"})],
            [_tool_calls_event("call_2", {"query": "alpha"})],
            [_tool_calls_event("call_3", {"query": "alpha"})],
            [{"type": "token", "content": "forced summary"}],
        ],
        completion_responses=[RuntimeError("reflection failed")],
    )
    agent = _build_stub_stream_agent(
        router=router,
        act_results=["first result", "second result"],
        resilience_enabled=True,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=6,
    )

    events = []
    async for event in agent.handle_message_stream("hello", context=_stream_context()):
        events.append(event)

    tool_calls = [event for event in events if event["type"] == "tool_call"]
    tool_results = [event for event in events if event["type"] == "tool_result"]
    states = [event for event in events if event["type"] == "state"]
    done = next(event for event in events if event["type"] == "done")

    assert [event["call_id"] for event in tool_calls] == ["call_1", "call_2"]
    assert [event["call_id"] for event in tool_results] == ["call_1", "call_2"]
    assert states == []
    assert done == {"type": "done", "content": "forced summary", "data": {}}
    assert router.completion_calls[0]["tools"] is None
    assert router.stream_calls[-1]["tools"] is None


@pytest.mark.asyncio
async def test_handle_message_stream_resets_state_after_forced_summary_before_next_run():
    router = _SequentialStreamRouter(
        [
            [
                _tool_calls_event("call_1", {"query": "alpha"}),
            ],
            [{"type": "token", "content": "forced summary"}],
            [{"type": "token", "content": "next run"}],
        ]
    )
    agent, state_events = _build_stateful_stream_agent(
        router=router,
        registry=_single_tool_registry("web_search", ["search results"]),
        scheduler=_BatchScheduler(),
        parallel_enabled=True,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=1,
    )

    first_events = []
    async for event in agent.handle_message_stream("first", context=_stream_context("s1", "r1")):
        first_events.append(event)

    second_events = []
    async for event in agent.handle_message_stream("second", context=_stream_context("s1", "r2")):
        second_events.append(event)

    assert next(event for event in first_events if event["type"] == "done") == {
        "type": "done",
        "content": "forced summary",
        "data": {},
    }
    assert agent.state == AgentState.IDLE
    assert [(event.from_state, event.to_state) for event in state_events[:3]] == [
        (AgentState.IDLE, AgentState.THINKING),
        (AgentState.THINKING, AgentState.ACTING),
        (AgentState.ACTING, AgentState.IDLE),
    ]
    assert state_events[3].from_state == AgentState.IDLE
    assert state_events[3].to_state == AgentState.THINKING
    assert next(event for event in second_events if event["type"] == "done") == {
        "type": "done",
        "content": "next run",
        "data": {},
    }


@pytest.mark.asyncio
async def test_handle_message_stream_resets_state_after_timeout_before_next_run():
    router = _SequentialStreamRouter(
        [
            asyncio.TimeoutError("boom"),
            [{"type": "token", "content": "recovered"}],
        ]
    )
    agent, state_events = _build_stateful_stream_agent(
        router=router,
        registry=ToolRegistry(),
        scheduler=_BatchScheduler(),
        parallel_enabled=True,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=1,
    )

    first_events = []
    async for event in agent.handle_message_stream("first", context=_stream_context("s1", "r1")):
        first_events.append(event)

    second_events = []
    async for event in agent.handle_message_stream("second", context=_stream_context("s1", "r2")):
        second_events.append(event)

    assert next(event for event in first_events if event["type"] == "error") == {
        "type": "error",
        "content": "Request timed out: boom",
    }
    assert agent.state == AgentState.IDLE
    assert state_events[0].from_state == AgentState.IDLE
    assert state_events[0].to_state == AgentState.THINKING
    assert state_events[1].from_state == AgentState.THINKING
    assert state_events[1].to_state == AgentState.IDLE
    assert state_events[2].from_state == AgentState.IDLE
    assert state_events[2].to_state == AgentState.THINKING
    assert next(event for event in second_events if event["type"] == "done") == {
        "type": "done",
        "content": "recovered",
        "data": {},
    }


@pytest.mark.asyncio
async def test_handle_message_stream_resets_state_after_generator_close():
    router = _TokenThenBlockRouter()
    agent, state_events = _build_stateful_stream_agent(
        router=router,
        registry=ToolRegistry(),
        scheduler=_BatchScheduler(),
        parallel_enabled=True,
        resilience_enabled=False,
        repeat_limit=3,
        max_reflections=1,
        max_tool_rounds=1,
    )

    stream = agent.handle_message_stream("hello", context=_stream_context("s1", "r1"))
    first = await anext(stream)
    assert first == {"type": "token", "content": "partial"}

    await stream.aclose()

    assert agent.state == AgentState.IDLE
    assert [(event.from_state, event.to_state) for event in state_events] == [
        (AgentState.IDLE, AgentState.THINKING),
        (AgentState.THINKING, AgentState.IDLE),
    ]


def _build_stub_stream_agent(
    *,
    router,
    act_results: list[str],
    resilience_enabled: bool,
    repeat_limit: int,
    max_reflections: int,
    max_tool_rounds: int,
):
    registry = _single_tool_registry("web_search", act_results)
    scheduler = _BatchScheduler()
    agent = _build_custom_stream_agent(
        router=router,
        registry=registry,
        scheduler=scheduler,
        parallel_enabled=True,
        resilience_enabled=resilience_enabled,
        repeat_limit=repeat_limit,
        max_reflections=max_reflections,
        max_tool_rounds=max_tool_rounds,
    )
    agent._batch_scheduler = scheduler
    return agent


def _tool_calls_event(call_id: str, arguments: dict[str, str]):
    return {
        "type": "tool_calls",
        "calls": [{"id": call_id, "name": "web_search", "arguments": arguments}],
        "reasoning_content": "",
    }


def _tool_call_batch_event(*calls: tuple[str, str, dict]):
    return {
        "type": "tool_calls",
        "calls": [
            {"id": call_id, "name": name, "arguments": arguments}
            for call_id, name, arguments in calls
        ],
        "reasoning_content": "",
    }


def _single_tool_registry(name: str, results: list[str]) -> ToolRegistry:
    queued_results = list(results)

    async def runner(_params: _ScriptedParams) -> ToolExecutionResult:
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=queued_results.pop(0),
        )

    registry = ToolRegistry()
    registry.register(_ScriptedToolBuilder(name, runner, read_only=True))
    return registry


def _build_custom_stream_agent(
    *,
    router,
    registry: ToolRegistry,
    scheduler,
    parallel_enabled: bool,
    resilience_enabled: bool,
    repeat_limit: int,
    max_reflections: int,
    max_tool_rounds: int,
):
    agent = MultiClawAgent.__new__(MultiClawAgent)
    agent.skill_manager = _DummySkillManager()
    agent.context_builder = _DummyContextBuilder()
    agent.registry = registry
    agent.scheduler = scheduler
    agent.memory = _DummyMemory()
    agent.router = router
    agent.state = AgentState.IDLE
    agent.event_bus = EventBus()
    agent.event_router = None
    agent.settings = SimpleNamespace(
        agent=SimpleNamespace(
            system_prompt="sys",
            max_tool_rounds=max_tool_rounds,
            resilience_enabled=resilience_enabled,
            no_progress_repeat_limit=repeat_limit,
            reflection_max_attempts=max_reflections,
        ),
        memory=SimpleNamespace(context_window_limit=1000),
        llm=SimpleNamespace(default_model="x"),
        tools=SimpleNamespace(
            parallel_read_only_enabled=parallel_enabled,
            parallel_max_concurrency=4,
        ),
    )
    agent.tool_batch_executor = ToolBatchExecutor(
        registry=registry,
        scheduler=scheduler,
        max_concurrency=4,
        enabled=parallel_enabled,
    )
    agent.transition = AsyncMock()
    agent._save_chat_msg = AsyncMock()
    agent.remember = AsyncMock()
    agent.act = AsyncMock(
        side_effect=AssertionError("legacy per-call act path should not run")
    )
    return agent


def _build_stateful_stream_agent(
    *,
    router,
    registry: ToolRegistry,
    scheduler,
    parallel_enabled: bool,
    resilience_enabled: bool,
    repeat_limit: int,
    max_reflections: int,
    max_tool_rounds: int,
):
    agent = _build_custom_stream_agent(
        router=router,
        registry=registry,
        scheduler=scheduler,
        parallel_enabled=parallel_enabled,
        resilience_enabled=resilience_enabled,
        repeat_limit=repeat_limit,
        max_reflections=max_reflections,
        max_tool_rounds=max_tool_rounds,
    )
    event_bus = EventBus()
    state_events: list[AgentStateEvent] = []

    async def collect(event):
        if isinstance(event, AgentStateEvent):
            state_events.append(event)

    event_bus.subscribe("agent.state_change", collect)
    agent.event_bus = event_bus
    agent.event_router = None
    agent.state = AgentState.IDLE
    agent.transition = BaseAgent.transition.__get__(agent, MultiClawAgent)
    return agent, state_events
