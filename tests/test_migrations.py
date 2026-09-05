import asyncio
import logging
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from alembic import command
from multiclaw.cli import alembic_config, check_revision_is_head, main
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.schema import metadata

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

PLAN_TABLES = {
    "agent_plans",
    "agent_plan_versions",
    "agent_plan_steps",
    "agent_plan_step_dependencies",
    "agent_plan_step_runs",
    "agent_plan_decisions",
}


async def _current_revision(database: Database) -> str | None:
    async with database.connect() as conn:
        return await conn.run_sync(
            lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
        )


@pytest.mark.asyncio
async def test_upgrade_to_durable_plan_head_matches_metadata(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'plans.db'}"
    config = alembic_config(database_url=database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        assert ScriptDirectory.from_config(config).get_current_head() == "20260905_0002"
        assert await _current_revision(database) == "20260905_0002"
        async with database.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("agent_runs"))
            diffs = await conn.run_sync(
                lambda sync: compare_metadata(
                    MigrationContext.configure(sync, opts={"target_metadata": metadata}), metadata
                )
            )
    finally:
        await database.dispose()

    assert PLAN_TABLES <= tables
    assert tables - {"alembic_version"} == EXPECTED_BASELINE_TABLES | PLAN_TABLES
    assert {column["name"] for column in columns} >= {
        "plan_id",
        "initial_plan_version",
        "active_plan_version",
        "cancel_requested_at",
    }
    assert diffs == []


@pytest.mark.asyncio
async def test_check_revision_is_head_reports_false_before_upgrade_and_true_at_head(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'check.db'}"
    database_path = tmp_path / "check.db"

    assert await check_revision_is_head(database_url=database_url) is False
    assert database_path.exists() is False

    config = alembic_config(database_url=database_url)
    await asyncio.to_thread(command.upgrade, config, "head")

    assert await check_revision_is_head(database_url=database_url) is True


@pytest.mark.asyncio
async def test_upgrade_preserves_legacy_direct_run_with_existing_reference(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy-run.db'}"
    config = alembic_config(database_url=database_url)
    await asyncio.to_thread(command.upgrade, config, "20260815_0001")

    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        async with database.write_transaction() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO users (
                        id, email, auth_epoch, default_workspace_id, status, purge_after,
                        created_at, updated_at, disabled_at, purge_requested_at
                    ) VALUES ('tenant', 'legacy@example.com', 0, NULL, 'active', NULL, 1, 1, NULL, NULL)
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                    VALUES ('workspace', 'tenant', 'legacy', 'Legacy', 'active', 1, 1)
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO chat_sessions (
                        id, tenant_id, workspace_id, title, status, created_at, updated_at,
                        last_message_at, metadata_json
                    ) VALUES ('session', 'tenant', 'workspace', 'Legacy', 'active', 1, 1, NULL, '{}')
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO agent_runs (
                        run_id, tenant_id, workspace_id, session_id, run_status, fencing_token,
                        schema_version, version, created_at, updated_at
                    ) VALUES ('run', 'tenant', 'workspace', 'session', 'awaiting_user', 0, 1, 1, 1, 1)
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO approval_requests (
                        approval_id, tenant_id, workspace_id, session_id, run_id, tool_call_id,
                        approval_status, requested_at, expires_at, version
                    ) VALUES (
                        'approval', 'tenant', 'workspace', 'session', 'run', 'tool-call',
                        'awaiting_user', 1, 2, 1
                    )
                    """
                )
            )
    finally:
        await database.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")

    migrated = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        assert await _current_revision(migrated) == "20260905_0002"
        async with migrated.connect() as conn:
            binding = (
                await conn.execute(
                    text(
                        """
                        SELECT plan_id, initial_plan_version, active_plan_version
                        FROM agent_runs WHERE run_id = 'run'
                        """
                    )
                )
            ).one()
            approvals = await conn.scalar(text("SELECT count(*) FROM approval_requests"))
            violations = (await conn.execute(text("PRAGMA foreign_key_check"))).all()
    finally:
        await migrated.dispose()

    assert tuple(binding) == (None, None, None)
    assert approvals == 1
    assert violations == []


def test_durable_plan_migration_rejects_downgrade(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'forward-only.db'}"
    config = alembic_config(database_url=database_url)
    command.upgrade(config, "head")

    with pytest.raises(RuntimeError, match="forward-only"):
        command.downgrade(config, "20260815_0001")


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


@pytest.mark.asyncio
async def test_alembic_upgrade_does_not_disable_existing_module_loggers(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'logging.db'}"
    logger = logging.getLogger("multiclaw.mcp.transport.stdio")
    original_disabled = logger.disabled
    original_propagate = logger.propagate
    original_handlers = list(logger.handlers)

    logger.disabled = False
    logger.propagate = True

    try:
        await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")
        logger.addHandler(caplog.handler)
        with caplog.at_level("DEBUG"):
            logger.debug("sentinel debug message")

        assert logger.disabled is False
        assert "sentinel debug message" in caplog.text
    finally:
        logger.disabled = original_disabled
        logger.propagate = original_propagate
        logger.handlers[:] = original_handlers
