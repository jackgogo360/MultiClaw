import pytest

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
    assert messages[1] == {"role": "user", "content": "hello"}
    assert messages[2] == {"role": "assistant", "content": "hi there"}
    assert messages[3]["role"] == "system"
    assert "Relevant memory:" in messages[3]["content"]
    assert messages[4] == {"role": "user", "content": "alpha status?"}


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

    assert len([msg for msg in messages if msg["role"] == "system"]) == 1
