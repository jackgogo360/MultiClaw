from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from multiclaw.context import estimate_tokens
from multiclaw.memory import MemoryEntry
from multiclaw.tenancy.context import TenantContext


ROOT_CONTEXT = TenantContext(
    tenant_id="00000000-0000-0000-0000-000000000001",
    workspace_id="00000000-0000-0000-0000-000000000002",
)
SESSION_CONTEXT = ROOT_CONTEXT.for_session("00000000-0000-0000-0000-000000000003")
OTHER_SESSION_CONTEXT = ROOT_CONTEXT.for_session("00000000-0000-0000-0000-000000000004")


@dataclass
class _ScopedMemoryFake:
    def __init__(self) -> None:
        self._entries: list[tuple[TenantContext, MemoryEntry]] = []
        self.recent_calls: list[tuple[TenantContext, int, str | None]] = []
        self.query_calls: list[tuple[TenantContext, str, int, str | None]] = []

    async def save(self, context: TenantContext, entry: MemoryEntry) -> MemoryEntry:
        if entry.type == "chat_message":
            if context.session_id is None:
                raise ValueError("session_id is required for chat_message entries")
            session_id = context.session_id if entry.session_id is None else entry.session_id
            if session_id != context.session_id:
                raise ValueError("session_id must match the current context")
            entry = entry.model_copy(update={"session_id": session_id})
        self._entries.append((context, entry))
        return entry

    async def query(
        self,
        context: TenantContext,
        query: str,
        top_k: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        self.query_calls.append((context, query, top_k, entry_type))
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
        self.recent_calls.append((context, limit, entry_type))
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


def request(**overrides):
    from multiclaw.agent.context import ContextRequest

    defaults = {
        "system_prompt": "system",
        "user_input": "current question",
        "context": SESSION_CONTEXT,
        "context_window_limit": 1000,
        "skill_prompts": [],
    }
    return ContextRequest(**(defaults | overrides))


def contents(messages: list[dict]) -> str:
    return "\n".join(str(message["content"]) for message in messages)


@pytest.mark.asyncio
async def test_context_builder_orders_recent_history_then_relevant_memory() -> None:
    from multiclaw.agent.context import ContextBuilder, ContextRequest

    memory = _ScopedMemoryFake()
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="older note about alpha", type="note"),
    )
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="hello", type="chat_message", role="user", turn_index=1),
    )
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="hi there", type="chat_message", role="assistant", turn_index=2),
    )

    builder = ContextBuilder(memory=memory, recent_turns=8, context_history_ratio=0.5)
    messages = await builder.build(
        ContextRequest(
            system_prompt="system",
            user_input="alpha status?",
            context=SESSION_CONTEXT,
            context_window_limit=1000,
        )
    )

    assert messages[0] == {"role": "system", "content": "system"}
    assert messages[1]["role"] == "system"
    assert "Current date:" in messages[1]["content"]
    assert messages[2] == {"role": "user", "content": "hello"}
    assert messages[3] == {"role": "assistant", "content": "hi there"}
    assert messages[4]["role"] == "system"
    assert "Relevant memory:" in messages[4]["content"]
    assert messages[5] == {"role": "user", "content": "alpha status?"}
    assert memory.recent_calls[-1][0] == SESSION_CONTEXT
    assert memory.query_calls[-1][0] == SESSION_CONTEXT


@pytest.mark.asyncio
async def test_context_builder_does_not_duplicate_recent_history_in_relevant_memory() -> None:
    from multiclaw.agent.context import ContextBuilder, ContextRequest

    memory = _ScopedMemoryFake()
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="alpha project", type="chat_message", role="user", turn_index=1),
    )

    builder = ContextBuilder(memory=memory, recent_turns=8, context_history_ratio=0.5)
    messages = await builder.build(
        ContextRequest(
            system_prompt="system",
            user_input="alpha project",
            context=SESSION_CONTEXT,
            context_window_limit=1000,
        )
    )

    system_messages = [msg for msg in messages if msg["role"] == "system"]

    assert len(system_messages) == 2
    assert system_messages[0] == {"role": "system", "content": "system"}
    assert "Current date:" in system_messages[1]["content"]


@pytest.mark.asyncio
async def test_context_builder_injects_current_date_anchor() -> None:
    from multiclaw.agent.context import ContextBuilder, ContextRequest

    memory = _ScopedMemoryFake()
    builder = ContextBuilder(memory=memory, recent_turns=8, context_history_ratio=0.5)

    messages = await builder.build(
        ContextRequest(
            system_prompt="system",
            user_input="总结最近一个月的招聘信息",
            context=SESSION_CONTEXT,
            context_window_limit=1000,
        )
    )

    today = datetime.now(timezone.utc).date().isoformat()

    assert messages[0] == {"role": "system", "content": "system"}
    assert messages[1]["role"] == "system"
    assert f"Current date: {today} (UTC)." in messages[1]["content"]
    assert "Use this date to resolve relative time references" in messages[1]["content"]
    assert messages[-1] == {"role": "user", "content": "总结最近一个月的招聘信息"}


@pytest.mark.asyncio
async def test_progressive_context_prioritizes_l0_then_newest_l1_then_l2() -> None:
    from multiclaw.agent.context import ContextBuilder

    memory = _ScopedMemoryFake()
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(
            content="oldest history with extra detail that should not fit within the l1 budget",
            type="chat_message",
            role="user",
            turn_index=1,
        ),
    )
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="middle history", type="chat_message", role="assistant", turn_index=2),
    )
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="newest history", type="chat_message", role="user", turn_index=3),
    )
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="current question memory", type="note"),
    )

    builder = ContextBuilder(
        memory=memory,
        recent_turns=8,
        context_history_ratio=0.5,
        progressive_enabled=True,
        response_reserve_tokens=16,
        l1_ratio=0.6,
    )

    result = await builder.build_with_report(
        request(
            context_window_limit=60,
            skill_prompts=[("skill", "skill prompt with additional guidance text")],
        )
    )

    assert result.messages[0]["content"] == "system"
    assert result.messages[-1]["content"] == "current question"
    assert [message["content"] for message in result.messages[2:4]] == [
        "middle history",
        "newest history",
    ]
    assert "oldest history" not in contents(result.messages)
    assert "current question memory" in contents(result.messages)
    assert result.report.dropped_by_level["L1"] >= 1


def test_estimate_tokens_is_conservative_and_deterministic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


@pytest.mark.asyncio
async def test_progressive_context_drops_anchor_before_current_user_input() -> None:
    from multiclaw.agent.context import ContextBuilder

    memory = _ScopedMemoryFake()
    builder = ContextBuilder(
        memory=memory,
        recent_turns=8,
        context_history_ratio=0.5,
        progressive_enabled=True,
        response_reserve_tokens=0,
        l1_ratio=0.6,
    )

    result = await builder.build_with_report(
        request(
            system_prompt="system prompt that still needs to stay intact",
            user_input="current question must stay intact",
            context_window_limit=10,
        )
    )

    assert [message["role"] for message in result.messages] == ["system", "user"]
    assert result.messages[-1]["content"] == "current question must stay intact"
    assert result.report.dropped_by_level["L0"] == 1


@pytest.mark.asyncio
async def test_build_with_report_matches_legacy_when_progressive_mode_disabled() -> None:
    from multiclaw.agent.context import ContextBuilder

    memory = _ScopedMemoryFake()
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="recent user", type="chat_message", role="user", turn_index=1),
    )
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="recent assistant", type="chat_message", role="assistant", turn_index=2),
    )
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="relevant note", type="note"),
    )

    builder = ContextBuilder(memory=memory, recent_turns=8, context_history_ratio=0.5)

    result = await builder.build_with_report(
        request(
            user_input="relevant",
            skill_prompts=[("skill", "skill prompt body")],
        )
    )

    assert result.messages[0] == {"role": "system", "content": "system"}
    assert result.messages[1]["role"] == "system"
    assert "Current date:" in result.messages[1]["content"]
    assert result.messages[2] == {"role": "system", "content": "skill prompt body"}
    assert result.messages[3] == {"role": "user", "content": "recent user"}
    assert result.messages[4] == {"role": "assistant", "content": "recent assistant"}
    assert result.messages[5]["role"] == "system"
    assert "Relevant memory:" in result.messages[5]["content"]
    assert "relevant note" in result.messages[5]["content"]
    assert result.messages[6] == {"role": "user", "content": "relevant"}
    assert result.report.limit_tokens == 1000


@pytest.mark.asyncio
async def test_progressive_context_drops_oversized_skill_and_keeps_newest_chat_that_fits() -> None:
    from multiclaw.agent.context import ContextBuilder

    memory = _ScopedMemoryFake()
    await memory.save(
        SESSION_CONTEXT,
        MemoryEntry(content="tiny newest chat", type="chat_message", role="user", turn_index=1),
    )

    builder = ContextBuilder(
        memory=memory,
        recent_turns=8,
        context_history_ratio=0.5,
        progressive_enabled=True,
        response_reserve_tokens=16,
        l1_ratio=0.6,
    )

    result = await builder.build_with_report(
        request(
            context_window_limit=160,
            skill_prompts=[
                ("big", "x" * 200),
                ("small", "fit"),
            ],
        )
    )

    assert "tiny newest chat" in contents(result.messages)
    assert "fit" in contents(result.messages)
    assert "x" * 200 not in contents(result.messages)
    assert result.report.dropped_by_level["L1"] == 1
