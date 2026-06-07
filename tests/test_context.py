import pytest
from datetime import datetime, timezone

from multiclaw.memory import InMemoryMemory, MemoryEntry


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
