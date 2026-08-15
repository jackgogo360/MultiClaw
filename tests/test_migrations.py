import asyncio
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic import command
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.schema import metadata
from multiclaw.cli import alembic_config, check_revision_is_head, main


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
    database_path = tmp_path / "check.db"

    assert await check_revision_is_head(database_url=database_url) is False
    assert database_path.exists() is False

    config = alembic_config(database_url=database_url)
    await asyncio.to_thread(command.upgrade, config, "head")

    assert await check_revision_is_head(database_url=database_url) is True


def test_alembic_config_targets_repo_baseline_script_location(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'config.db'}"

    config = alembic_config(database_url=database_url)
    script_location = Path(config.get_main_option("script_location")).resolve()

    assert config.get_main_option("sqlalchemy.url") == database_url
    assert script_location == Path(__file__).resolve().parents[1] / "alembic"


def test_cli_current_missing_sqlite_file_returns_nonzero_without_creating_database(monkeypatch, tmp_path):
    database_path = tmp_path / "missing" / "current.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"

    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", database_url)

    assert main(["db", "current"]) == 1
    assert database_path.parent.exists() is False
    assert database_path.exists() is False


@pytest.mark.asyncio
async def test_upgrade_to_head_has_no_metadata_diff_against_core_schema(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'compare.db'}"
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")

    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        async with database.connect() as conn:
            diffs = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(
                        sync_conn,
                        opts={
                            "target_metadata": metadata,
                            "include_object": (
                                lambda obj, name, type_, reflected, compare_to: not (
                                    type_ == "table" and reflected and name == "alembic_version"
                                )
                            ),
                        },
                    ),
                    metadata,
                )
            )
    finally:
        await database.dispose()

    assert diffs == []
