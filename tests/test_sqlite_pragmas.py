import pytest
from sqlalchemy import text

from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database


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
            synchronous = await conn.scalar(text("PRAGMA synchronous"))

        assert foreign_keys == 1
        assert busy_timeout == 4321
        assert journal_mode is not None
        assert journal_mode.lower() == "wal"
        assert synchronous == 1
    finally:
        await database.dispose()
