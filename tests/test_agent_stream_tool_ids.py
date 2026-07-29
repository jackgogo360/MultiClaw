from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from multiclaw.agent.multiclaw import MultiClawAgent
from multiclaw.agent.models import Observation, ObservationType
from multiclaw.llm import LLMResponse


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


class _DummyRegistry:
    def to_openai_schemas(self):
        return []


class _DummyMemory:
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


class _DummyObservation:
    def __init__(self, content: str):
        self.content = content


@pytest.mark.asyncio
async def test_handle_message_stream_preserves_tool_call_ids(monkeypatch):
    agent = MultiClawAgent.__new__(MultiClawAgent)
    agent.skill_manager = _DummySkillManager()
    agent.context_builder = _DummyContextBuilder()
    agent.registry = _DummyRegistry()
    agent.memory = _DummyMemory()
    agent.router = _DummyRouter()
    agent.settings = type(
        "Settings",
        (),
        {
            "agent": type(
                "AgentSettings",
                (),
                {
                    "system_prompt": "sys",
                    "max_tool_rounds": 1,
                    "resilience_enabled": False,
                    "no_progress_repeat_limit": 3,
                    "reflection_max_attempts": 1,
                },
            )(),
            "memory": type("MemorySettings", (), {"context_window_limit": 1000})(),
            "llm": type("LLMSettings", (), {"default_model": "x"})(),
        },
    )()
    agent.transition = lambda *_args, **_kwargs: _async_none()
    agent._save_chat_msg = lambda *_args, **_kwargs: _async_none()
    agent.remember = lambda *_args, **_kwargs: _async_none()
    agent.act = lambda *_args, **_kwargs: _async_obs("search results")

    events = []
    async for event in agent.handle_message_stream("hello", session_id="s1"):
        events.append(event)

    tool_call = next(event for event in events if event["type"] == "tool_call")
    tool_result = next(event for event in events if event["type"] == "tool_result")

    assert tool_call["call_id"] == "call_123"
    assert tool_result["call_id"] == "call_123"


@pytest.mark.asyncio
async def test_handle_message_stream_retries_when_final_summary_contains_dsml():
    agent = MultiClawAgent.__new__(MultiClawAgent)
    router = _DummyDsmlRetryRouter()
    agent.skill_manager = _DummySkillManager()
    agent.context_builder = _DummyContextBuilder()
    agent.registry = _DummyRegistry()
    agent.memory = _DummyMemory()
    agent.router = router
    agent.settings = type(
        "Settings",
        (),
        {
            "agent": type(
                "AgentSettings",
                (),
                {
                    "system_prompt": "sys",
                    "max_tool_rounds": 1,
                    "resilience_enabled": False,
                    "no_progress_repeat_limit": 3,
                    "reflection_max_attempts": 1,
                },
            )(),
            "memory": type("MemorySettings", (), {"context_window_limit": 1000})(),
            "llm": type("LLMSettings", (), {"default_model": "x"})(),
        },
    )()
    agent.transition = lambda *_args, **_kwargs: _async_none()
    agent._save_chat_msg = lambda *_args, **_kwargs: _async_none()
    agent.remember = lambda *_args, **_kwargs: _async_none()
    agent.act = lambda *_args, **_kwargs: _async_obs("search results")

    events = []
    async for event in agent.handle_message_stream("hello", session_id="s1"):
        events.append(event)

    done = next(event for event in events if event["type"] == "done")
    tokens = [event["content"] for event in events if event["type"] == "token"]

    assert router.calls == 3
    assert done["content"] == "Final summary"
    assert "".join(tokens) == "Final summary"


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
    async for event in agent.handle_message_stream("hello", session_id="s1"):
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
    async for event in agent.handle_message_stream("hello", session_id="s1"):
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


async def _async_none():
    return None


async def _async_obs(content: str):
    return _DummyObservation(content)


def _build_stub_stream_agent(
    *,
    router,
    act_results: list[str],
    resilience_enabled: bool,
    repeat_limit: int,
    max_reflections: int,
    max_tool_rounds: int,
):
    agent = MultiClawAgent.__new__(MultiClawAgent)
    agent.skill_manager = _DummySkillManager()
    agent.context_builder = _DummyContextBuilder()
    agent.registry = _DummyRegistry()
    agent.memory = _DummyMemory()
    agent.router = router
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
    )
    agent.transition = AsyncMock()
    agent._save_chat_msg = AsyncMock()
    agent.remember = AsyncMock()
    agent.act = AsyncMock(
        side_effect=[
            Observation(type=ObservationType.TOOL_RESULT, content=content)
            for content in act_results
        ]
    )
    return agent


def _tool_calls_event(call_id: str, arguments: dict[str, str]):
    return {
        "type": "tool_calls",
        "calls": [{"id": call_id, "name": "web_search", "arguments": arguments}],
        "reasoning_content": "",
    }
