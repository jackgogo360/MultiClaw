import pytest

from multiclaw.memory.sqlite import SqliteMemory
from multiclaw.session.sqlite import SqliteSessionStore


@pytest.mark.asyncio
async def test_session_store_uses_wal_and_busy_timeout(tmp_path):
    store = SqliteSessionStore(str(tmp_path / "app.db"))
    await store.initialize()

    try:
        assert store._db is not None

        cursor = await store._db.execute("PRAGMA journal_mode")
        journal_mode = await cursor.fetchone()
        cursor = await store._db.execute("PRAGMA busy_timeout")
        busy_timeout = await cursor.fetchone()

        assert journal_mode[0].lower() == "wal"
        assert busy_timeout[0] >= 30000
    finally:
        if store._db is not None:
            await store._db.close()


@pytest.mark.asyncio
async def test_sqlite_memory_uses_wal_and_busy_timeout_for_file_db(tmp_path):
    memory = SqliteMemory(str(tmp_path / "memory.db"))
    await memory.initialize()

    try:
        assert memory._db is not None

        cursor = await memory._db.execute("PRAGMA journal_mode")
        journal_mode = await cursor.fetchone()
        cursor = await memory._db.execute("PRAGMA busy_timeout")
        busy_timeout = await cursor.fetchone()

        assert journal_mode[0].lower() == "wal"
        assert busy_timeout[0] >= 30000
    finally:
        await memory.close()
