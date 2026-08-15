import pytest
from sqlalchemy import text

from multiclaw.config.settings import DatabaseSettings
from multiclaw.memory.sqlite import SqliteMemory
from multiclaw.session.sqlite import SqliteSessionStore
from multiclaw.storage import Database


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


@pytest.mark.asyncio
async def test_storage_engine_sqlite_sets_foreign_keys_busy_timeout_and_wal(tmp_path):
    database = Database.create(
        DatabaseSettings(
            driver="sqlite",
            url=f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}",
            sqlite_busy_timeout_ms=4321,
        )
    )

    try:
        async with database.connect() as conn:
            foreign_keys = await conn.scalar(text("PRAGMA foreign_keys"))
            busy_timeout = await conn.scalar(text("PRAGMA busy_timeout"))
            journal_mode = await conn.scalar(text("PRAGMA journal_mode"))

        assert foreign_keys == 1
        assert busy_timeout == 4321
        assert journal_mode is not None
        assert journal_mode.lower() == "wal"
    finally:
        await database.dispose()
