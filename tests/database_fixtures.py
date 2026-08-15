import os

import pytest

from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database


_ORIGINAL_TEST_MYSQL_URL = os.getenv("MULTICLAW_TEST_MYSQL_URL")


@pytest.fixture(params=("sqlite", "mysql"))
async def database(request, tmp_path):
    if request.param == "mysql":
        url = _ORIGINAL_TEST_MYSQL_URL or os.getenv("MULTICLAW_TEST_MYSQL_URL")
        if not url:
            pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'contract.db'}"

    db = Database.create(DatabaseSettings(driver=request.param, url=url))
    try:
        yield db
    finally:
        await db.dispose()
