import pytest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from pydantic import BaseModel

from multiclaw.agent.models import Observation, ObservationType
from multiclaw.config import Settings
from multiclaw.events import EventBus
from multiclaw.governance import InMemoryAuditLogger, PermissionChecker, ProcessSandbox
from multiclaw.llm import LLMResponse, ModelRouter, ToolCall
from multiclaw.memory import InMemoryMemory
from multiclaw.planner import Planner
from multiclaw.tools import (
    CoreToolScheduler,
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)


@pytest.fixture(autouse=True)
def _mock_httpx():
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": '{"action": "mock_response", "message": "This is a mock LLM response"}'}}]
    }
    mock_response.raise_for_status = Mock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.post.return_value = mock_response

    with patch("httpx.AsyncClient", return_value=mock_client):
        yield


class EchoParams(BaseModel):
    text: str


class EchoInvocation(ToolInvocation[EchoParams]):
    async def execute(self) -> ToolExecutionResult:
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=self.params.text,
            data={"echoed": self.params.text},
        )


class EchoToolBuilder(ToolBuilder[EchoParams]):
    name = "echo"
    description = "Echo tool"
    parameters_schema = EchoParams

    def validate(self, params: dict) -> EchoParams:
        return EchoParams(**params)

    def build(self, params: EchoParams) -> ToolInvocation[EchoParams]:
        return EchoInvocation(name=self.name, params=params)


@pytest.fixture
def agent(test_config_path):
    from multiclaw.agent import MultiClawAgent

    settings = Settings(_config_file=str(test_config_path))
    registry = ToolRegistry()
    registry.register(EchoToolBuilder())
    scheduler = CoreToolScheduler(
        permission_checker=PermissionChecker(),
        sandbox=ProcessSandbox(),
        audit_logger=InMemoryAuditLogger(),
        event_bus=EventBus(),
    )
    return MultiClawAgent(
        settings=settings,
        router=ModelRouter(settings),
        registry=registry,
        scheduler=scheduler,
        memory=InMemoryMemory(),
        planner=Planner(),
        event_bus=EventBus(),
    )


class TestMultiClawAgent:
    @pytest.mark.asyncio
    async def test_llm_calls_echo_tool(self, agent):
        from multiclaw.agent import ObservationType

        # Round 1: LLM decides to call echo tool
        tool_call_response = Mock()
        tool_call_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "echo",
                                    "arguments": '{"text": "hello"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
        tool_call_response.raise_for_status = Mock()

        # Round 2: LLM receives tool result and responds with text
        final_response = Mock()
        final_response.json.return_value = {
            "choices": [
                {"message": {"role": "assistant", "content": "I echoed: hello"}}
            ]
        }
        final_response.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.side_effect = [tool_call_response, final_response]

        with patch("httpx.AsyncClient", return_value=mock_client):
            observation = await agent.handle_message("echo hello")

        assert observation.type == ObservationType.USER_RESPONSE
        assert "hello" in observation.content
        # Tool result stored in memory
        matches = await agent.memory.query("hello", top_k=5)
        tool_results = [m for m in matches if m.type == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0].content == "hello"

    @pytest.mark.asyncio
    async def test_uses_planner_for_plan_mode(self, agent):
        from multiclaw.agent import ObservationType

        observation = await agent.handle_message(
            "plan: collect facts and summarize findings"
        )

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "1. collect facts | 2. summarize findings"

    @pytest.mark.asyncio
    async def test_plain_message_returns_llm_text(self, agent):
        from multiclaw.agent import ObservationType

        observation = await agent.handle_message("hello")

        assert observation.type == ObservationType.USER_RESPONSE
        assert "mock_response" in observation.content

    @pytest.mark.asyncio
    async def test_saves_user_messages_to_memory(self, agent):
        await agent.handle_message("remember this")

        matches = await agent.memory.query("remember", top_k=5)

        assert len(matches) == 1
        assert matches[0].content == "remember this"

    @pytest.mark.asyncio
    async def test_injects_relevant_memory_before_user_message(self, agent):
        from multiclaw.memory import MemoryEntry

        await agent.memory.save(
            MemoryEntry(content="alpha project uses SQLite memory", type="note")
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "noted"}}]
        }
        mock_response.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with patch("httpx.AsyncClient", return_value=mock_client):
            await agent.handle_message("what does alpha use?")

        request_body = mock_client.post.call_args.kwargs["json"]
        messages = request_body["messages"]

        relevant_memory_message = next(
            (
                message
                for message in messages
                if message["role"] == "system"
                and "Relevant memory:" in message["content"]
            ),
            None,
        )

        assert relevant_memory_message is not None
        assert (
            "alpha project uses SQLite memory"
            in relevant_memory_message["content"]
        )
        assert messages[-1] == {"role": "user", "content": "what does alpha use?"}

    @pytest.mark.asyncio
    async def test_agent_saves_user_and_assistant_messages_with_session_id(self, agent):
        from multiclaw.memory import MemoryEntry

        mock_response = Mock()
        mock_response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "response"}}]
        }
        mock_response.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with patch("httpx.AsyncClient", return_value=mock_client):
            await agent.handle_message("hello", session_id="session-1")

        recent = await agent.memory.recent(
            limit=2, entry_type="chat_message", session_id="session-1"
        )

        assert [entry.role for entry in recent] == ["assistant", "user"]
        assert [entry.content for entry in recent] == ["response", "hello"]

    @pytest.mark.asyncio
    async def test_agent_uses_recent_chat_history_for_same_session(self, agent):
        from multiclaw.memory import MemoryEntry

        # Pre-populate chat history for two different sessions
        await agent.memory.save(
            MemoryEntry(content="session one user", type="chat_message", session_id="s1", role="user", turn_index=1)
        )
        await agent.memory.save(
            MemoryEntry(content="session two user", type="chat_message", session_id="s2", role="user", turn_index=1)
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }
        mock_response.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with patch("httpx.AsyncClient", return_value=mock_client):
            await agent.handle_message("follow-up", session_id="s1")

        payload = mock_client.post.call_args.kwargs["json"]["messages"]
        contents = [msg["content"] for msg in payload if "content" in msg and msg["content"] is not None]

        assert "session one user" in contents
        assert "session two user" not in contents

    @pytest.mark.asyncio
    async def test_handle_message_reflects_on_third_repeated_tool_batch(self):
        from multiclaw.agent.multiclaw import MultiClawAgent

        agent = _build_stub_agent(
            completion_responses=[
                _tool_call_response("call_1", {"query": "alpha"}),
                _tool_call_response("call_2", {"query": "alpha"}),
                _tool_call_response("call_3", {"query": "alpha"}),
                LLMResponse(content="Root cause analysis"),
                LLMResponse(content="changed approach"),
            ],
            act_results=["first result", "second result"],
            resilience_enabled=True,
            repeat_limit=3,
            max_reflections=1,
        )

        observation = await agent.handle_message("hello", session_id="s1")

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "changed approach"
        assert agent.act.await_count == 2

        reflection_call = _find_reflection_call(agent.router.completion.await_args_list)
        assert reflection_call.kwargs["tools"] is None
        assert reflection_call.kwargs["messages"][-1]["role"] == "system"
        assert "Runtime reflection required." in reflection_call.kwargs["messages"][-1]["content"]

    @pytest.mark.asyncio
    async def test_handle_message_reflects_then_forces_summary_on_repeated_results(self):
        agent = _build_stub_agent(
            completion_responses=[
                _tool_call_response("call_1", {"query": "alpha"}),
                _tool_call_response("call_2", {"query": "beta"}),
                LLMResponse(content="Try a different search strategy"),
                _tool_call_response("call_3", {"query": "gamma"}),
                LLMResponse(content="forced summary"),
            ],
            act_results=["stuck", "stuck", "stuck"],
            resilience_enabled=True,
            repeat_limit=2,
            max_reflections=1,
        )

        observation = await agent.handle_message("hello", session_id="s1")

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "forced summary"
        assert agent.act.await_count == 3

        reflection_calls = [
            call
            for call in agent.router.completion.await_args_list
            if call.kwargs["tools"] is None
            and call.kwargs["messages"][-1]["role"] == "system"
            and "Runtime reflection required." in call.kwargs["messages"][-1]["content"]
        ]
        assert len(reflection_calls) == 1

        forced_summary_call = agent.router.completion.await_args_list[-1]
        assert forced_summary_call.kwargs["tools"] is None
        assert forced_summary_call.kwargs["messages"][-1]["role"] == "tool"
        assert forced_summary_call.kwargs["messages"][-1]["content"] == "stuck"

    @pytest.mark.asyncio
    async def test_handle_message_keeps_legacy_behavior_when_resilience_disabled(self):
        agent = _build_stub_agent(
            completion_responses=[
                _tool_call_response("call_1", {"query": "alpha"}),
                _tool_call_response("call_2", {"query": "alpha"}),
                _tool_call_response("call_3", {"query": "alpha"}),
                LLMResponse(content="done"),
            ],
            act_results=["same", "same", "same"],
            resilience_enabled=False,
            repeat_limit=3,
            max_reflections=1,
        )

        observation = await agent.handle_message("hello", session_id="s1")

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "done"
        assert agent.act.await_count == 3
        assert all(call.kwargs["tools"] is not None for call in agent.router.completion.await_args_list[:-1])
        assert all(
            "Runtime reflection required." not in message["content"]
            for call in agent.router.completion.await_args_list
            for message in call.kwargs["messages"]
            if message.get("role") == "system" and message.get("content")
        )

    @pytest.mark.asyncio
    async def test_handle_message_forces_summary_when_reflection_generation_fails(self):
        agent = _build_stub_agent(
            completion_responses=[
                _tool_call_response("call_1", {"query": "alpha"}),
                _tool_call_response("call_2", {"query": "alpha"}),
                _tool_call_response("call_3", {"query": "alpha"}),
                RuntimeError("reflection failed"),
                LLMResponse(content="forced summary"),
            ],
            act_results=["first result", "second result"],
            resilience_enabled=True,
            repeat_limit=3,
            max_reflections=1,
        )

        observation = await agent.handle_message("hello", session_id="s1")

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "forced summary"
        assert agent.act.await_count == 2
        assert agent.router.completion.await_count == 5

        reflection_call = _find_reflection_call(agent.router.completion.await_args_list)
        forced_summary_call = agent.router.completion.await_args_list[-1]

        assert reflection_call.kwargs["tools"] is None
        assert forced_summary_call.kwargs["tools"] is None
        assert forced_summary_call.kwargs["messages"] == reflection_call.kwargs["messages"][:-1]
        assert all(
            message.get("content") != "Runtime reflection feedback: forced summary"
            for message in forced_summary_call.kwargs["messages"]
            if message.get("role") == "system"
        )


class _StubSkillManager:
    def process_message(self, _message: str):
        return []

    def get_active_skill_prompts(self):
        return []

    def invoke(self, _skill_name: str, _args: str):
        return None


class _StubContextBuilder:
    async def build(self, _request):
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]


class _StubRegistry:
    def to_openai_schemas(self):
        return [{"type": "function", "function": {"name": "echo"}}]


class _StubMemory:
    async def save(self, _entry):
        return None


def _build_stub_agent(
    *,
    completion_responses: list[LLMResponse],
    act_results: list[str],
    resilience_enabled: bool,
    repeat_limit: int,
    max_reflections: int,
):
    from multiclaw.agent.multiclaw import MultiClawAgent

    agent = MultiClawAgent.__new__(MultiClawAgent)
    agent.skill_manager = _StubSkillManager()
    agent.context_builder = _StubContextBuilder()
    agent.registry = _StubRegistry()
    agent.memory = _StubMemory()
    agent.router = SimpleNamespace(
        completion=AsyncMock(side_effect=completion_responses)
    )
    agent.settings = _stub_settings(
        resilience_enabled=resilience_enabled,
        repeat_limit=repeat_limit,
        max_reflections=max_reflections,
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


def _stub_settings(*, resilience_enabled: bool, repeat_limit: int, max_reflections: int):
    return SimpleNamespace(
        agent=SimpleNamespace(
            system_prompt="sys",
            max_tool_rounds=6,
            resilience_enabled=resilience_enabled,
            no_progress_repeat_limit=repeat_limit,
            reflection_max_attempts=max_reflections,
        ),
        memory=SimpleNamespace(context_window_limit=1000),
        llm=SimpleNamespace(default_model="test-model"),
    )


def _tool_call_response(call_id: str, arguments: dict[str, str]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=call_id, name="echo", arguments=arguments)],
    )


def _find_reflection_call(calls):
    return next(
        call
        for call in calls
        if call.kwargs["tools"] is None
        and call.kwargs["messages"][-1]["role"] == "system"
        and "Runtime reflection required." in call.kwargs["messages"][-1]["content"]
    )
