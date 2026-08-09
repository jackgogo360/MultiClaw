import pytest
from datetime import datetime, timezone

from multiclaw.memory import InMemoryMemory, MemoryEntry
from multiclaw.context import estimate_tokens


def request(**overrides):
    from multiclaw.agent.context import ContextRequest

    defaults = {
        "system_prompt": "system",
        "user_input": "current question",
        "session_id": "s1",
        "context_window_limit": 1000,
        "skill_prompts": [],
    }
    return ContextRequest(**(defaults | overrides))


def contents(messages: list[dict]) -> str:
    return "\n".join(str(message["content"]) for message in messages)


@pytest.mark.asyncio
async def test_context_builder_orders_recent_history_then_relevant_memory():
    from multiclaw.agent.context import ContextBuilder, ContextRequest

    memory = InMemoryMemory()
    await memory.save(
        MemoryEntry(content="older note about alpha", type="note", session_id="s1")
    )
    await memory.save(
        MemoryEntry(content="hello", type="chat_message", session_id="s1", role="user", turn_index=1)
    )
    await memory.save(
        MemoryEntry(content="hi there", type="chat_message", session_id="s1", role="assistant", turn_index=2)
    )

    builder = ContextBuilder(memory=memory, recent_turns=8, context_history_ratio=0.5)
    messages = await builder.build(
        ContextRequest(
            system_prompt="system",
            user_input="alpha status?",
            session_id="s1",
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


@pytest.mark.asyncio
async def test_context_builder_does_not_duplicate_recent_history_in_relevant_memory():
    from multiclaw.agent.context import ContextBuilder, ContextRequest

    memory = InMemoryMemory()
    await memory.save(
        MemoryEntry(content="alpha project", type="chat_message", session_id="s1", role="user", turn_index=1)
    )

    builder = ContextBuilder(memory=memory, recent_turns=8, context_history_ratio=0.5)
    messages = await builder.build(
        ContextRequest(
            system_prompt="system",
            user_input="alpha project",
            session_id="s1",
            context_window_limit=1000,
        )
    )

    system_messages = [msg for msg in messages if msg["role"] == "system"]

    assert len(system_messages) == 2
    assert system_messages[0] == {"role": "system", "content": "system"}
    assert "Current date:" in system_messages[1]["content"]


@pytest.mark.asyncio
async def test_context_builder_injects_current_date_anchor():
    from multiclaw.agent.context import ContextBuilder, ContextRequest

    memory = InMemoryMemory()
    builder = ContextBuilder(memory=memory, recent_turns=8, context_history_ratio=0.5)

    messages = await builder.build(
        ContextRequest(
            system_prompt="system",
            user_input="总结最近一个月的招聘信息",
            session_id="s1",
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
async def test_progressive_context_prioritizes_l0_then_newest_l1_then_l2():
    from multiclaw.agent.context import ContextBuilder

    memory = InMemoryMemory()
    await memory.save(
        MemoryEntry(
            content="oldest history with extra detail that should not fit within the l1 budget",
            type="chat_message",
            session_id="s1",
            role="user",
            turn_index=1,
        )
    )
    await memory.save(
        MemoryEntry(
            content="middle history",
            type="chat_message",
            session_id="s1",
            role="assistant",
            turn_index=2,
        )
    )
    await memory.save(
        MemoryEntry(
            content="newest history",
            type="chat_message",
            session_id="s1",
            role="user",
            turn_index=3,
        )
    )
    await memory.save(
        MemoryEntry(content="current question memory", type="note", session_id="s1")
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


def test_estimate_tokens_is_conservative_and_deterministic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


@pytest.mark.asyncio
async def test_progressive_context_drops_anchor_before_current_user_input():
    from multiclaw.agent.context import ContextBuilder

    memory = InMemoryMemory()
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
async def test_build_with_report_matches_legacy_when_progressive_mode_disabled():
    from multiclaw.agent.context import ContextBuilder

    memory = InMemoryMemory()
    await memory.save(
        MemoryEntry(content="recent user", type="chat_message", session_id="s1", role="user", turn_index=1)
    )
    await memory.save(
        MemoryEntry(content="recent assistant", type="chat_message", session_id="s1", role="assistant", turn_index=2)
    )
    await memory.save(
        MemoryEntry(content="relevant note", type="note", session_id="s1")
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
async def test_progressive_context_drops_oversized_skill_and_keeps_newest_chat_that_fits():
    from multiclaw.agent.context import ContextBuilder

    memory = InMemoryMemory()
    await memory.save(
        MemoryEntry(content="tiny newest chat", type="chat_message", session_id="s1", role="user", turn_index=1)
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
