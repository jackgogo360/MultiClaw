import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.cli import alembic_config, check_revision_is_head


EXPECTED_BASELINE_TABLES = {
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


async def _current_revision(database: Database) -> str | None:
    async with database.connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
        )

@pytest.mark.asyncio
async def test_upgrade_to_head_creates_exact_baseline_table_set_and_revision(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'baseline.db'}"
    config = alembic_config(database_url=database_url)

    await asyncio.to_thread(command.upgrade, config, "head")

    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        assert ScriptDirectory.from_config(config).get_current_head() == "20260815_0001"
        assert await _current_revision(database) == "20260815_0001"

        async with database.connect() as conn:
            table_names = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        assert table_names - {"alembic_version"} == EXPECTED_BASELINE_TABLES
        assert "alembic_version" in table_names
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_check_revision_is_head_reports_false_before_upgrade_and_true_at_head(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'check.db'}"

    assert await check_revision_is_head(database_url=database_url) is False

    config = alembic_config(database_url=database_url)
    await asyncio.to_thread(command.upgrade, config, "head")

    assert await check_revision_is_head(database_url=database_url) is True


def test_alembic_config_targets_repo_baseline_script_location(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'config.db'}"

    config = alembic_config(database_url=database_url)
    script_location = Path(config.get_main_option("script_location")).resolve()

    assert config.get_main_option("sqlalchemy.url") == database_url
    assert script_location == Path(__file__).resolve().parents[1] / "alembic"
