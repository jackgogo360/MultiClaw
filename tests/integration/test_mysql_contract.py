import asyncio
from pathlib import Path
import os
import sys
from uuid import uuid4

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from multiclaw.cli import alembic_config, check_revision_is_head
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL


def _parse_mysql_version(version: str) -> tuple[int, int, int]:
    numeric = version.split("-", 1)[0]
    major, minor, patch = numeric.split(".")[:3]
    return int(major), int(minor), int(patch)


@pytest.fixture
def mysql_database_url():
    url = _ORIGINAL_TEST_MYSQL_URL or os.getenv("MULTICLAW_TEST_MYSQL_URL")
    if not url:
        pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")
    return url


@pytest.fixture
async def mysql_database(mysql_database_url):
    database = Database.create(DatabaseSettings(driver="mysql", url=mysql_database_url))
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def isolated_mysql_database_url(mysql_database_url):
    admin_database = Database.create(DatabaseSettings(driver="mysql", url=mysql_database_url))
    schema_name = f"multiclaw_task3_{uuid4().hex[:12]}"
    temporary_url = make_url(mysql_database_url).set(database=schema_name).render_as_string(hide_password=False)

    try:
        async with admin_database.write_transaction() as conn:
            await conn.execute(text(f"CREATE DATABASE `{schema_name}` CHARACTER SET utf8mb4"))
        yield temporary_url
    finally:
        async with admin_database.write_transaction() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS `{schema_name}`"))
        await admin_database.dispose()


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


@pytest.mark.asyncio
async def test_mysql_baseline_schema_contract(isolated_mysql_database_url):
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=isolated_mysql_database_url), "head")

    assert await check_revision_is_head(database_url=isolated_mysql_database_url) is True

    database = Database.create(DatabaseSettings(driver="mysql", url=isolated_mysql_database_url))
    try:
        async with database.connect() as conn:
            revision = await conn.run_sync(
                lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
            )
            table_names = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
            engines = await conn.execute(
                text(
                    """
                    SELECT table_name, engine
                    FROM information_schema.tables
                    WHERE table_schema = DATABASE()
                    """
                )
            )

        assert revision == "20260815_0001"
        assert table_names - {"alembic_version"} == {
            "agent_runs",
            "approval_requests",
            "audit_logs",
            "chat_sessions",
            "deletion_jobs",
            "execution_checkpoints",
            "memory_entries",
            "tool_executions",
            "user_secrets",
            "users",
            "verification_codes",
            "workspaces",
        }
        assert "alembic_version" in table_names
        assert {row[1].lower() for row in engines.fetchall()} == {"innodb"}
    finally:
        await database.dispose()
