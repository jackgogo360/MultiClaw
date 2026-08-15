import asyncio
import pytest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from pydantic import BaseModel

from multiclaw.agent.models import Observation, ObservationType
from multiclaw.agent.tool_batch import ToolBatchExecutor
from multiclaw.config import Settings
from multiclaw.context import ContextBuildReport, ContextBuildResult
from multiclaw.events import EventBus
from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
from multiclaw.llm import LLMResponse, ModelRouter, ToolCall
from multiclaw.memory import MemoryEntry
from multiclaw.planner import Planner
from multiclaw.tenancy.context import TenantContext
from multiclaw.tools import (
    CoreToolScheduler,
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)


ROOT_CONTEXT = TenantContext(
    tenant_id="00000000-0000-0000-0000-000000000001",
    workspace_id="00000000-0000-0000-0000-000000000002",
)
SESSION_CONTEXT = ROOT_CONTEXT.for_session("00000000-0000-0000-0000-000000000101")
OTHER_SESSION_CONTEXT = ROOT_CONTEXT.for_session("00000000-0000-0000-0000-000000000102")
ALT_SESSION_CONTEXT = ROOT_CONTEXT.for_session("00000000-0000-0000-0000-000000000103")


class _ScopedMemoryFake:
    def __init__(self) -> None:
        self._entries: list[tuple[TenantContext, MemoryEntry]] = []

    async def save(self, context: TenantContext, entry: MemoryEntry) -> MemoryEntry:
        if entry.type == "chat_message":
            if context.session_id is None:
                raise ValueError("session_id is required for chat_message entries")
            session_id = context.session_id if entry.session_id is None else entry.session_id
            if session_id != context.session_id:
                raise ValueError("session_id must match the current context")
            entry = entry.model_copy(update={"session_id": session_id})
        elif entry.session_id is not None:
            if context.session_id is None or entry.session_id != context.session_id:
                raise ValueError("session_id must match the current context")
        self._entries.append((context, entry))
        return entry

    async def query(
        self,
        context: TenantContext,
        query: str,
        top_k: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        terms = set(query.lower().split())
        matches: list[tuple[int, int, MemoryEntry]] = []
        for index, (scope, entry) in enumerate(self._entries):
            if scope.tenant_id != context.tenant_id or scope.workspace_id != context.workspace_id:
                continue
            if entry_type is not None and entry.type != entry_type:
                continue
            if context.session_id is None:
                if entry.session_id is not None:
                    continue
            elif entry.session_id not in {None, context.session_id}:
                continue
            score = len(terms & set(entry.content.lower().split()))
            if score > 0:
                matches.append((score, index, entry))
        matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [entry for _, _, entry in matches[:top_k]]

    async def recent(
        self,
        context: TenantContext,
        limit: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        if entry_type == "chat_message" and context.session_id is None:
            raise ValueError("session_id is required for chat_message recent lookups")
        results: list[MemoryEntry] = []
        for scope, entry in reversed(self._entries):
            if scope.tenant_id != context.tenant_id or scope.workspace_id != context.workspace_id:
                continue
            if entry_type is not None and entry.type != entry_type:
                continue
            if context.session_id is None:
                if entry.session_id is not None:
                    continue
            elif entry_type == "chat_message" and entry.session_id != context.session_id:
                continue
            results.append(entry)
        return results[:limit]

    async def context(
        self,
        context: TenantContext,
        max_chars: int,
        limit: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        selected: list[MemoryEntry] = []
        used = 0
        for entry in await self.recent(context, limit=limit, entry_type=entry_type):
            entry_len = len(entry.content)
            separator = 1 if selected else 0
            if used + separator + entry_len > max_chars:
                continue
            selected.append(entry)
            used += separator + entry_len
        return list(reversed(selected))

    async def forget(self, context: TenantContext, entry_id: str) -> None:
        self._entries = [
            (scope, entry)
            for scope, entry in self._entries
            if not (
                scope.tenant_id == context.tenant_id
                and scope.workspace_id == context.workspace_id
                and entry.id == entry_id
            )
        ]


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
    description = "Scripted test tool"
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

    async def run(self, builder, raw_params: dict) -> ToolExecutionResult:
        self.run_calls.append((builder.name, raw_params))
        params = builder.validate(raw_params)
        return await builder.build(params).execute()


@pytest.fixture
def agent(test_config_path):
    from multiclaw.agent import MultiClawAgent

    settings = Settings(_config_file=str(test_config_path))
    registry = ToolRegistry()
    registry.register(EchoToolBuilder())
    scheduler = CoreToolScheduler(
        permission_checker=PermissionChecker(),
        execution_guard=ExecutionGuard(),
        audit_logger=InMemoryAuditLogger(),
        event_bus=EventBus(),
    )
    return MultiClawAgent(
        settings=settings,
        router=ModelRouter(settings),
        registry=registry,
        scheduler=scheduler,
        memory=_ScopedMemoryFake(),
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
            observation = await agent.handle_message("echo hello", context=SESSION_CONTEXT)

        assert observation.type == ObservationType.USER_RESPONSE
        assert "hello" in observation.content
        # Tool result stored in memory
        matches = await agent.memory.query(SESSION_CONTEXT, "hello", top_k=5)
        tool_results = [m for m in matches if m.type == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0].content == "hello"

    @pytest.mark.asyncio
    async def test_uses_planner_for_plan_mode(self, agent):
        from multiclaw.agent import ObservationType

        observation = await agent.handle_message(
            "plan: collect facts and summarize findings",
            context=SESSION_CONTEXT,
        )

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "1. collect facts | 2. summarize findings"

    @pytest.mark.asyncio
    async def test_plain_message_returns_llm_text(self, agent):
        from multiclaw.agent import ObservationType

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation.type == ObservationType.USER_RESPONSE
        assert "mock_response" in observation.content

    @pytest.mark.asyncio
    async def test_saves_user_messages_to_memory(self, agent):
        await agent.handle_message("remember this", context=SESSION_CONTEXT)

        matches = await agent.memory.query(SESSION_CONTEXT, "remember", top_k=5)

        assert len(matches) == 1
        assert matches[0].content == "remember this"

    @pytest.mark.asyncio
    async def test_tool_results_stay_in_session_scope_and_base_remember_rejects_missing_session(self, agent):
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
                                    "arguments": '{"text": "scoped_tool_result"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
        tool_call_response.raise_for_status = Mock()

        final_response = Mock()
        final_response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "done"}}]
        }
        final_response.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.side_effect = [tool_call_response, final_response]

        with patch("httpx.AsyncClient", return_value=mock_client):
            await agent.handle_message("echo local", context=SESSION_CONTEXT)

        assert [entry.content for entry in await agent.memory.query(SESSION_CONTEXT, "scoped_tool_result", top_k=5)] == [
            "scoped_tool_result"
        ]
        assert await agent.memory.query(OTHER_SESSION_CONTEXT, "scoped_tool_result", top_k=5) == []

        with pytest.raises(ValueError, match="session_id"):
            await agent.remember(ROOT_CONTEXT, "orphan tool result", "tool_result")

    @pytest.mark.asyncio
    async def test_injects_relevant_memory_before_user_message(self, agent):
        await agent.memory.save(
            SESSION_CONTEXT,
            MemoryEntry(content="alpha project uses SQLite memory", type="note"),
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
            await agent.handle_message("what does alpha use?", context=SESSION_CONTEXT)

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
        mock_response = Mock()
        mock_response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "response"}}]
        }
        mock_response.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        with patch("httpx.AsyncClient", return_value=mock_client):
            await agent.handle_message(
                "hello",
                context=ROOT_CONTEXT.for_session("00000000-0000-0000-0000-000000000111"),
            )

        recent = await agent.memory.recent(
            ROOT_CONTEXT.for_session("00000000-0000-0000-0000-000000000111"),
            limit=2,
            entry_type="chat_message",
        )

        assert [entry.role for entry in recent] == ["assistant", "user"]
        assert [entry.content for entry in recent] == ["response", "hello"]

    @pytest.mark.asyncio
    async def test_agent_uses_recent_chat_history_for_same_session(self, agent):
        # Pre-populate chat history for two different sessions
        await agent.memory.save(
            SESSION_CONTEXT,
            MemoryEntry(content="session one user", type="chat_message", role="user", turn_index=1),
        )
        await agent.memory.save(
            OTHER_SESSION_CONTEXT,
            MemoryEntry(content="session two user", type="chat_message", role="user", turn_index=1),
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
            await agent.handle_message("follow-up", context=SESSION_CONTEXT)

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

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "changed approach"
        assert len(agent._batch_scheduler.run_calls) == 2

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

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "forced summary"
        assert len(agent._batch_scheduler.run_calls) == 3

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

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "done"
        assert len(agent._batch_scheduler.run_calls) == 3
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

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "forced summary"
        assert len(agent._batch_scheduler.run_calls) == 2
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

    @pytest.mark.asyncio
    async def test_handle_message_retries_when_forced_summary_contains_dsml(self):
        registry = ToolRegistry()
        registry.register(EchoToolBuilder())
        agent = _build_custom_batch_agent(
            completion_responses=[
                _tool_batch_response(
                    ("call_1", "echo", {"text": "search results"}),
                ),
                LLMResponse(
                    content=(
                        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="echo">'
                        "</｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>"
                    )
                ),
                LLMResponse(content="Final summary"),
            ],
            registry=registry,
            scheduler=_BatchScheduler(),
            parallel_enabled=True,
        )
        agent.settings.agent.max_tool_rounds = 1

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation == Observation(
            type=ObservationType.USER_RESPONSE,
            content="Final summary",
        )
        assert agent.router.completion.await_count == 3
        retry_call = agent.router.completion.await_args_list[-1]
        assert retry_call.kwargs["tools"] is None
        assert "Do not output DSML" in retry_call.kwargs["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_handle_message_parallel_read_only_batch_overlaps_when_enabled(self):
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        active = 0
        max_active = 0
        finished: list[str] = []

        async def runner(params: _ScriptedParams) -> ToolExecutionResult:
            nonlocal active, max_active
            label = params.label or ""
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
                finished.append(label)
                return ToolExecutionResult(
                    status=ToolStatus.SUCCESS,
                    content=f"done:{label}",
                )
            finally:
                active -= 1

        registry = ToolRegistry()
        registry.register(_ScriptedToolBuilder("echo", runner, read_only=True))
        scheduler = _BatchScheduler()
        agent = _build_custom_batch_agent(
            completion_responses=[
                _tool_batch_response(
                    ("call_1", "echo", {"label": "first"}),
                    ("call_2", "echo", {"label": "second"}),
                ),
                LLMResponse(content="finished"),
            ],
            registry=registry,
            scheduler=scheduler,
            parallel_enabled=True,
        )

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "finished"
        assert first_started.is_set()
        assert second_started.is_set()
        assert max_active == 2
        assert finished == ["second", "first"]
        assert agent.act.await_count == 0

    @pytest.mark.asyncio
    async def test_handle_message_read_write_read_batch_preserves_serial_barriers(self):
        active = 0
        max_active = 0
        execution_order: list[str] = []

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
        agent = _build_custom_batch_agent(
            completion_responses=[
                _tool_batch_response(
                    ("call_1", "read_probe", {"label": "read-1"}),
                    ("call_2", "write_probe", {"label": "write"}),
                    ("call_3", "read_probe", {"label": "read-2"}),
                ),
                LLMResponse(content="finished"),
            ],
            registry=registry,
            scheduler=_BatchScheduler(),
            parallel_enabled=True,
        )

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "finished"
        assert max_active == 1
        assert execution_order == [
            "start:read-1",
            "end:read-1",
            "start:write",
            "end:write",
            "start:read-2",
            "end:read-2",
        ]
        assert [call.args[1] for call in agent.remember.await_args_list] == [
            "read-1",
            "write",
            "read-2",
        ]

    @pytest.mark.asyncio
    async def test_handle_message_preserves_original_tool_call_ids_and_result_order(self):
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
                    content=f"result:{label}",
                )
            finally:
                active -= 1

        registry = ToolRegistry()
        registry.register(_ScriptedToolBuilder("echo", runner, read_only=True))
        agent = _build_custom_batch_agent(
            completion_responses=[
                _tool_batch_response(
                    ("call_beta", "echo", {"label": "first", "delay": 0.03}),
                    ("call_alpha", "echo", {"label": "second", "delay": 0.0}),
                ),
                LLMResponse(content="finished"),
            ],
            registry=registry,
            scheduler=_BatchScheduler(),
            parallel_enabled=True,
        )

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)
        second_call_messages = agent.router.completion.await_args_list[1].kwargs["messages"]
        assistant_message = next(
            message for message in second_call_messages if message.get("tool_calls")
        )
        tool_messages = [message for message in second_call_messages if message["role"] == "tool"]

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "finished"
        assert max_active == 2
        assert finished == ["second", "first"]
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
        assert [call.args[1] for call in agent.remember.await_args_list] == [
            "result:first",
            "result:second",
        ]

    @pytest.mark.asyncio
    async def test_handle_message_parallel_flag_disabled_keeps_read_only_batch_serial(self):
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
        registry.register(_ScriptedToolBuilder("echo", runner, read_only=True))
        agent = _build_custom_batch_agent(
            completion_responses=[
                _tool_batch_response(
                    ("call_1", "echo", {"label": "first", "delay": 0.01}),
                    ("call_2", "echo", {"label": "second", "delay": 0.0}),
                ),
                LLMResponse(content="finished"),
            ],
            registry=registry,
            scheduler=scheduler,
            parallel_enabled=False,
        )

        observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "finished"
        assert max_active == 1
        assert finished == ["first", "second"]
        assert scheduler.run_calls == [
            ("echo", {"label": "first", "delay": 0.01}),
            ("echo", {"label": "second", "delay": 0.0}),
        ]
        assert agent.act.await_count == 0

    @pytest.mark.asyncio
    async def test_handle_message_uses_context_build_with_report_and_only_prompt_messages(self):
        expected_messages = [
            {"role": "system", "content": "sys"},
            {"role": "system", "content": "skill guidance"},
            {"role": "user", "content": "hello"},
        ]
        agent = _build_custom_batch_agent(
            completion_responses=[LLMResponse(content="done")],
            registry=ToolRegistry(),
            scheduler=_BatchScheduler(),
            parallel_enabled=True,
        )
        agent.context_builder = _ReportOnlyContextBuilder(expected_messages)

        with patch("multiclaw.agent.multiclaw.logger.info") as mock_info:
            observation = await agent.handle_message("hello", context=SESSION_CONTEXT)

        assert observation == Observation(
            type=ObservationType.USER_RESPONSE,
            content="done",
        )
        agent.context_builder.build_with_report.assert_awaited_once()
        request = agent.context_builder.build_with_report.await_args.args[0]
        assert request.user_input == "hello"
        assert request.context == SESSION_CONTEXT
        assert request.context_window_limit == 1000
        assert request.skill_prompts == []
        assert agent.router.completion.await_args_list[0].kwargs["messages"] == expected_messages
        mock_info.assert_called_once_with(
            "context_budget used=%s dropped=%s limit=%d reserve=%d",
            {"L0": 3, "L1": 4, "L2": 5},
            {"L0": 0, "L1": 1, "L2": 2},
            321,
            123,
        )
        assert all(
            set(message.keys()) <= {"role", "content"} for message in expected_messages
        )

    def test_constructor_wires_progressive_context_settings(self, test_config_path):
        from multiclaw.agent import MultiClawAgent

        settings = Settings(_config_file=str(test_config_path))
        settings.memory.progressive_context_enabled = True
        settings.memory.context_response_reserve_tokens = 2048
        settings.memory.context_l1_ratio = 0.75
        registry = ToolRegistry()
        registry.register(EchoToolBuilder())
        scheduler = CoreToolScheduler(
            permission_checker=PermissionChecker(),
            execution_guard=ExecutionGuard(),
            audit_logger=InMemoryAuditLogger(),
            event_bus=EventBus(),
        )

        agent = MultiClawAgent(
            settings=settings,
            router=ModelRouter(settings),
            registry=registry,
            scheduler=scheduler,
            memory=_ScopedMemoryFake(),
            planner=Planner(),
            event_bus=EventBus(),
        )

        assert agent.context_builder.progressive_enabled is True
        assert agent.context_builder.response_reserve_tokens == 2048
        assert agent.context_builder.l1_ratio == 0.75


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
        raise AssertionError("handle_message should use build_with_report")


class _StubRegistry:
    def to_openai_schemas(self):
        return [{"type": "function", "function": {"name": "echo"}}]


class _StubMemory:
    async def save(self, _context, _entry):
        return None

    async def recent(self, _context, limit: int, entry_type: str | None = None):
        assert limit >= 1
        assert entry_type == "chat_message"
        return []


def _build_stub_agent(
    *,
    completion_responses: list[LLMResponse],
    act_results: list[str],
    resilience_enabled: bool,
    repeat_limit: int,
    max_reflections: int,
):
    tool_results = list(act_results)

    async def runner(_params: _ScriptedParams) -> ToolExecutionResult:
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=tool_results.pop(0),
        )

    registry = ToolRegistry()
    registry.register(_ScriptedToolBuilder("echo", runner, read_only=True))
    scheduler = _BatchScheduler()

    agent = _build_custom_batch_agent(
        completion_responses=completion_responses,
        registry=registry,
        scheduler=scheduler,
        parallel_enabled=True,
        resilience_enabled=resilience_enabled,
        repeat_limit=repeat_limit,
        max_reflections=max_reflections,
    )
    agent._batch_scheduler = scheduler
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
        tools=SimpleNamespace(
            parallel_read_only_enabled=True,
            parallel_max_concurrency=4,
        ),
    )


def _tool_call_response(call_id: str, arguments: dict[str, str]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=call_id, name="echo", arguments=arguments)],
    )


def _tool_batch_response(*calls: tuple[str, str, dict]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(id=call_id, name=name, arguments=arguments)
            for call_id, name, arguments in calls
        ],
    )


def _build_custom_batch_agent(
    *,
    completion_responses: list[LLMResponse],
    registry: ToolRegistry,
    scheduler,
    parallel_enabled: bool,
    resilience_enabled: bool = False,
    repeat_limit: int = 3,
    max_reflections: int = 1,
):
    from multiclaw.agent.multiclaw import MultiClawAgent

    agent = MultiClawAgent.__new__(MultiClawAgent)
    agent.skill_manager = _StubSkillManager()
    agent.context_builder = _StubContextBuilder()
    agent.registry = registry
    agent.scheduler = scheduler
    agent.memory = _StubMemory()
    agent.router = SimpleNamespace(completion=AsyncMock(side_effect=completion_responses))
    agent.settings = SimpleNamespace(
        **_stub_settings(
            resilience_enabled=resilience_enabled,
            repeat_limit=repeat_limit,
            max_reflections=max_reflections,
        ).__dict__
    )
    agent.settings.tools = SimpleNamespace(
        parallel_read_only_enabled=parallel_enabled,
        parallel_max_concurrency=4,
    )
    agent.tool_batch_executor = ToolBatchExecutor(
        registry=registry,
        scheduler=scheduler,
        max_concurrency=agent.settings.tools.parallel_max_concurrency,
        enabled=parallel_enabled,
    )
    agent.transition = AsyncMock()
    agent._save_chat_msg = AsyncMock()
    agent.remember = AsyncMock()
    agent.act = AsyncMock(
        side_effect=AssertionError("legacy per-call act path should not run")
    )
    return agent


def _find_reflection_call(calls):
    return next(
        call
        for call in calls
        if call.kwargs["tools"] is None
        and call.kwargs["messages"][-1]["role"] == "system"
        and "Runtime reflection required." in call.kwargs["messages"][-1]["content"]
    )
