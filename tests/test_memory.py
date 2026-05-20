import pytest


@pytest.mark.asyncio
async def test_in_memory_memory_save_and_query():
    from multiclaw.memory import InMemoryMemory, MemoryEntry

    memory = InMemoryMemory()
    await memory.save(MemoryEntry(content="remember alpha", type="note"))
    await memory.save(MemoryEntry(content="remember beta", type="note"))

    results = await memory.query("alpha", top_k=5)

    assert len(results) == 1
    assert results[0].content == "remember alpha"


@pytest.mark.asyncio
async def test_in_memory_memory_forget_removes_entry():
    from multiclaw.memory import InMemoryMemory, MemoryEntry

    memory = InMemoryMemory()
    entry = await memory.save(MemoryEntry(content="remember alpha", type="note"))

    await memory.forget(entry.id)
    results = await memory.query("alpha", top_k=5)

    assert results == []


def test_memory_package_exports():
    from multiclaw import memory
    from multiclaw.memory import InMemoryMemory, MemoryEntry, MemoryProtocol

    assert memory.InMemoryMemory is InMemoryMemory
    assert memory.MemoryEntry is MemoryEntry
    assert memory.MemoryProtocol is MemoryProtocol


@pytest.mark.asyncio
async def test_in_memory_query_ranks_keyword_overlap():
    from multiclaw.memory import InMemoryMemory, MemoryEntry

    memory = InMemoryMemory()
    await memory.save(MemoryEntry(content="alpha only", type="note"))
    await memory.save(MemoryEntry(content="alpha beta gamma", type="note"))

    results = await memory.query("alpha beta", top_k=2)

    assert [entry.content for entry in results] == [
        "alpha beta gamma",
        "alpha only",
    ]


@pytest.mark.asyncio
async def test_in_memory_recent_returns_newest_first_with_filters():
    from multiclaw.memory import InMemoryMemory, MemoryEntry

    memory = InMemoryMemory()
    await memory.save(MemoryEntry(content="tenant a old", type="note", tenant_id="a"))
    await memory.save(MemoryEntry(content="tenant b", type="note", tenant_id="b"))
    await memory.save(MemoryEntry(content="tenant a new", type="tool_result", tenant_id="a"))

    results = await memory.recent(limit=2, tenant_id="a")

    assert [entry.content for entry in results] == ["tenant a new", "tenant a old"]


@pytest.mark.asyncio
async def test_in_memory_context_keeps_recent_entries_within_character_budget():
    from multiclaw.memory import InMemoryMemory, MemoryEntry

    memory = InMemoryMemory()
    await memory.save(MemoryEntry(content="old entry is too long", type="note"))
    await memory.save(MemoryEntry(content="middle", type="note"))
    await memory.save(MemoryEntry(content="new", type="note"))

    results = await memory.context(max_chars=12, limit=3)

    assert [entry.content for entry in results] == ["middle", "new"]


@pytest.mark.asyncio
async def test_sqlite_memory_persists_entries_across_instances(tmp_path):
    from multiclaw.memory import MemoryEntry, SqliteMemory

    db_path = tmp_path / "memory.db"
    first = SqliteMemory(str(db_path))
    await first.initialize()
    saved = await first.save(
        MemoryEntry(content="persistent alpha beta", type="note", tenant_id="tenant-1")
    )
    await first.close()

    second = SqliteMemory(str(db_path))
    await second.initialize()
    results = await second.query("alpha", top_k=5, tenant_id="tenant-1")
    await second.close()

    assert [entry.id for entry in results] == [saved.id]
    assert results[0].content == "persistent alpha beta"


@pytest.mark.asyncio
async def test_sqlite_memory_creates_parent_directory(tmp_path):
    from multiclaw.memory import MemoryEntry, SqliteMemory

    db_path = tmp_path / "nested" / "memory.db"
    memory = SqliteMemory(str(db_path))
    await memory.save(MemoryEntry(content="created parent directory", type="note"))
    await memory.close()

    assert db_path.exists()


@pytest.mark.asyncio
async def test_in_memory_filters_by_session_id():
    from multiclaw.memory import InMemoryMemory, MemoryEntry

    memory = InMemoryMemory()
    await memory.save(
        MemoryEntry(content="first user", type="chat_message", session_id="s1", role="user", turn_index=1)
    )
    await memory.save(
        MemoryEntry(content="other session", type="chat_message", session_id="s2", role="user", turn_index=1)
    )
    await memory.save(
        MemoryEntry(content="first assistant", type="chat_message", session_id="s1", role="assistant", turn_index=2)
    )

    results = await memory.recent(limit=3, session_id="s1", entry_type="chat_message")

    assert [entry.content for entry in results] == ["first assistant", "first user"]


@pytest.mark.asyncio
async def test_in_memory_query_filters_by_session_id():
    from multiclaw.memory import InMemoryMemory, MemoryEntry

    memory = InMemoryMemory()
    await memory.save(MemoryEntry(content="legacy alpha", type="note"))
    await memory.save(MemoryEntry(content="session alpha", type="note", session_id="s1"))

    results = await memory.query("alpha", top_k=5, session_id="s1")

    assert [entry.content for entry in results] == ["session alpha"]


@pytest.mark.asyncio
async def test_sqlite_memory_session_scoped_recent(tmp_path):
    from multiclaw.memory import MemoryEntry, SqliteMemory

    memory = SqliteMemory(str(tmp_path / "memory.db"))
    await memory.save(
        MemoryEntry(content="first user", type="chat_message", session_id="s1", role="user", turn_index=1)
    )
    await memory.save(
        MemoryEntry(content="other session", type="chat_message", session_id="s2", role="user", turn_index=1)
    )
    await memory.save(
        MemoryEntry(content="first assistant", type="chat_message", session_id="s1", role="assistant", turn_index=2)
    )

    results = await memory.recent(limit=3, session_id="s1", entry_type="chat_message")

    assert [entry.content for entry in results] == ["first assistant", "first user"]


@pytest.mark.asyncio
async def test_sqlite_memory_query_filters_by_session_id(tmp_path):
    from multiclaw.memory import MemoryEntry, SqliteMemory

    memory = SqliteMemory(str(tmp_path / "memory.db"))
    await memory.save(MemoryEntry(content="legacy alpha", type="note"))
    await memory.save(MemoryEntry(content="session alpha", type="note", session_id="s1"))

    results = await memory.query("alpha", top_k=5, session_id="s1")

    assert [entry.content for entry in results] == ["session alpha"]
