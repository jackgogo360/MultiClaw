import pytest
from unittest.mock import AsyncMock, Mock, patch

from pydantic import BaseModel

from multiclaw.config import Settings
from multiclaw.events import EventBus
from multiclaw.governance import InMemoryAuditLogger, PermissionChecker, ProcessSandbox
from multiclaw.llm import ModelRouter
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

        assert messages[1]["role"] == "system"
        assert "Relevant memory:" in messages[1]["content"]
        assert "alpha project uses SQLite memory" in messages[1]["content"]
        assert messages[2] == {"role": "user", "content": "what does alpha use?"}

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
