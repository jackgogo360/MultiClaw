from pathlib import Path
import os
import sys

import pytest
from sqlalchemy import text

from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL


def _parse_mysql_version(version: str) -> tuple[int, int, int]:
    numeric = version.split("-", 1)[0]
    major, minor, patch = numeric.split(".")[:3]
    return int(major), int(minor), int(patch)


@pytest.fixture
async def mysql_database():
    url = _ORIGINAL_TEST_MYSQL_URL or os.getenv("MULTICLAW_TEST_MYSQL_URL")
    if not url:
        pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")

    database = Database.create(DatabaseSettings(driver="mysql", url=url))
    try:
        yield database
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_mysql_engine_contract(mysql_database):
    async with mysql_database.connect() as conn:
        version = await conn.scalar(text("SELECT @@version"))
        storage_engine = await conn.scalar(text("SELECT @@default_storage_engine"))
        isolation = await conn.scalar(text("SELECT @@session.transaction_isolation"))
        timezone = await conn.scalar(text("SELECT @@session.time_zone"))

    assert isinstance(version, str)
    assert _parse_mysql_version(version) >= (8, 0, 36)
    assert storage_engine is not None
    assert storage_engine.lower() == "innodb"
    assert isolation is not None
    assert isolation.upper() == "READ-COMMITTED"
    assert timezone == "+00:00"
