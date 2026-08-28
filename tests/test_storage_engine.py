from pathlib import Path
import sys
import time

import pytest
from sqlalchemy import select, text
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database_fixtures import database


@pytest.mark.asyncio
async def test_db_now_ms_is_epoch_milliseconds_and_monotonic(database):
    async with database.connect() as conn:
        first = await conn.scalar(select(database.dialect.db_now_ms()))
        second = await conn.scalar(select(database.dialect.db_now_ms()))

    assert isinstance(first, int)
    assert isinstance(second, int)
    assert first <= second
    assert abs(first - int(time.time() * 1000)) < 10_000


@pytest.mark.asyncio
async def test_uow_rollback_is_atomic(database):
    async with database.write_transaction() as conn:
        await conn.execute(text("CREATE TABLE atomic_probe (id INTEGER PRIMARY KEY)"))

    with pytest.raises(RuntimeError):
        async with database.write_transaction() as conn:
            await conn.execute(text("INSERT INTO atomic_probe (id) VALUES (1)"))
            raise RuntimeError("rollback")

    async with database.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM atomic_probe")) == 0


@pytest.mark.asyncio
async def test_file_sqlite_creates_missing_parent_directories(tmp_path):
    database_path = tmp_path / "nested" / "deeper" / "engine.db"
    database = Database.create(
        DatabaseSettings(
            driver="sqlite",
            url=f"sqlite+aiosqlite:///{database_path}",
        )
    )

    try:
        async with database.connect() as conn:
            assert await conn.scalar(select(database.dialect.db_now_ms())) is not None

        async with database.write_transaction() as conn:
            await conn.execute(text("CREATE TABLE nested_probe (id INTEGER PRIMARY KEY)"))
    finally:
        await database.dispose()

    assert database_path.parent.exists()
    assert database_path.exists()
