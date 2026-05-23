import pytest


@pytest.mark.asyncio
async def test_delete_removes_session_and_messages(tmp_path):
    import aiosqlite
    from multiclaw.session import SqliteSessionStore

    db_path = str(tmp_path / "test.db")
    store = SqliteSessionStore(db_path)

    # Create session
    created = await store.create(title="Test")

    # Manually insert a chat message into memory_entries
    db = await store._ensure_db()
    await db.execute(
        "INSERT INTO memory_entries (id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("msg-1", "Hello", "chat_message", "", created.id, "user", 0, "2025-01-01T00:00:00", "{}"),
    )
    await db.commit()

    # Delete the session
    await store.delete(created.id)

    # Session should be gone
    assert await store.get(created.id) is None

    # Message should be gone
    cursor = await db.execute("SELECT COUNT(*) FROM memory_entries WHERE session_id = ?", (created.id,))
    count = (await cursor.fetchone())[0]
    assert count == 0


@pytest.mark.asyncio
async def test_delete_missing_session_does_not_raise(tmp_path):
    from multiclaw.session import SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "test.db"))
    # Should not raise
    await store.delete("nonexistent")


@pytest.mark.asyncio
async def test_get_messages_returns_recent_user_assistant_only(tmp_path):
    import aiosqlite
    from multiclaw.session import SqliteSessionStore

    db_path = str(tmp_path / "test.db")
    store = SqliteSessionStore(db_path)
    created = await store.create(title="Test")

    db = await store._ensure_db()
    # Insert chat messages
    entries = [
        ("u1", "Hello", "user", 1),
        ("a1", "Hi there", "assistant", 2),
        ("u2", "What is Python?", "user", 3),
        ("a2", "Python is a language", "assistant", 4),
    ]
    for eid, content, role, turn in entries:
        await db.execute(
            "INSERT INTO memory_entries (id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata) "
            "VALUES (?, ?, 'chat_message', '', ?, ?, ?, '2025-01-01T00:00:00', '{}')",
            (eid, content, created.id, role, turn),
        )
    # Insert a tool message — should NOT appear in results
    await db.execute(
        "INSERT INTO memory_entries (id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata) "
        "VALUES (?, ?, 'chat_message', '', ?, 'tool', ?, '2025-01-01T00:00:00', '{}')",
        ("t1", "tool output", created.id, 5),
    )
    await db.commit()

    messages = await store.get_messages(created.id, limit=50)

    assert len(messages) == 4
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert "tool" not in roles


@pytest.mark.asyncio
async def test_get_messages_respects_limit(tmp_path):
    import aiosqlite
    from multiclaw.session import SqliteSessionStore

    db_path = str(tmp_path / "test.db")
    store = SqliteSessionStore(db_path)
    created = await store.create(title="Test")

    db = await store._ensure_db()
    for i in range(10):
        await db.execute(
            "INSERT INTO memory_entries (id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata) "
            "VALUES (?, ?, 'chat_message', '', ?, 'user', ?, '2025-01-01T00:00:00', '{}')",
            (f"msg-{i}", f"Message {i}", created.id, i),
        )
    await db.commit()

    messages = await store.get_messages(created.id, limit=3)

    assert len(messages) == 3
    # Most recent 3 (chronological order: reversed from DESC query)
    assert messages[0]["content"] == "Message 7"
    assert messages[1]["content"] == "Message 8"
    assert messages[2]["content"] == "Message 9"


@pytest.mark.asyncio
async def test_get_messages_empty_session(tmp_path):
    from multiclaw.session import SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "test.db"))
    created = await store.create(title="Empty")

    messages = await store.get_messages(created.id)

    assert messages == []
