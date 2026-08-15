from pathlib import Path
import sys
import time

import pytest
from sqlalchemy import select, text

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
