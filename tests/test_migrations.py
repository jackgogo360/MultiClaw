import asyncio
import io
import logging
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

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


async def _seed_legacy_baseline(database: Database) -> None:
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
                INSERT INTO memory_entries (
                    id, tenant_id, workspace_id, session_id, content, type, role,
                    turn_index, created_at, metadata_json
                ) VALUES (
                    'message', 'tenant', 'workspace', 'session', 'legacy message',
                    'message', 'user', 1, 1, '{}'
                )
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
        await _seed_legacy_baseline(database)
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


@pytest.mark.parametrize("collision_table", ("agent_plans", "agent_plan_step_runs"))
@pytest.mark.asyncio
async def test_failed_sqlite_upgrade_restores_state_and_can_retry(
    tmp_path,
    collision_table,
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / f'retry-{collision_table}.db'}"
    config = alembic_config(database_url=database_url)
    await asyncio.to_thread(command.upgrade, config, "20260815_0001")

    baseline = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        await _seed_legacy_baseline(baseline)
        async with baseline.write_transaction() as conn:
            original_foreign_keys = await conn.scalar(text("PRAGMA foreign_keys"))
            await conn.execute(
                text(f"CREATE TABLE {collision_table} (collision INTEGER PRIMARY KEY)")
            )
    finally:
        await baseline.dispose()

    with pytest.raises(OperationalError, match=f"table {collision_table} already exists"):
        await asyncio.to_thread(command.upgrade, config, "head")

    failed = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        assert await _current_revision(failed) == "20260815_0001"
        async with failed.connect() as conn:
            failed_foreign_keys = await conn.scalar(text("PRAGMA foreign_keys"))
            failed_tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            failed_memory_uniques = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_unique_constraints("memory_entries")
            )
            failed_agent_run_columns = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns("agent_runs")
            )
            failed_plan_indexes = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            """
                            SELECT name
                            FROM sqlite_master
                            WHERE type = 'index'
                            AND name IN (
                                'ix_agent_plans_scope_created_at',
                                'ix_agent_plan_steps_scope_version_ordinal',
                                'ix_agent_plan_decisions_scope_created_at'
                            )
                            """
                        )
                    )
                ).all()
            }
            failed_counts = {
                table_name: await conn.scalar(text(f"SELECT count(*) FROM {table_name}"))
                for table_name in ("memory_entries", "agent_runs", "approval_requests")
            }
            failed_violations = (await conn.execute(text("PRAGMA foreign_key_check"))).all()

        assert failed_foreign_keys == original_foreign_keys
        assert not any(table_name.startswith("_alembic_tmp_") for table_name in failed_tables)
        assert failed_counts == {
            "memory_entries": 1,
            "agent_runs": 1,
            "approval_requests": 1,
        }
        assert not any(
            unique["column_names"] == ["tenant_id", "workspace_id", "session_id", "id"]
            for unique in failed_memory_uniques
        )
        assert {
            "plan_id",
            "initial_plan_version",
            "active_plan_version",
            "cancel_requested_at",
        }.isdisjoint(column["name"] for column in failed_agent_run_columns)
        assert (PLAN_TABLES - {collision_table}).isdisjoint(failed_tables)
        assert failed_plan_indexes == set()
        assert failed_violations == []

        async with failed.write_transaction() as conn:
            await conn.execute(text(f"DROP TABLE {collision_table}"))
    finally:
        await failed.dispose()

    await asyncio.to_thread(command.upgrade, config, "head")

    migrated = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        assert await _current_revision(migrated) == "20260905_0002"
        async with migrated.connect() as conn:
            final_foreign_keys = await conn.scalar(text("PRAGMA foreign_keys"))
            final_tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            final_memory_uniques = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_unique_constraints("memory_entries")
            )
            final_counts = {
                table_name: await conn.scalar(text(f"SELECT count(*) FROM {table_name}"))
                for table_name in ("memory_entries", "agent_runs", "approval_requests")
            }
            final_diffs = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(
                        sync_conn,
                        opts={
                            "target_metadata": metadata,
                            "include_object": (
                                lambda obj, name, type_, reflected, compare_to: not (
                                    type_ == "table"
                                    and reflected
                                    and name == "alembic_version"
                                )
                            ),
                        },
                    ),
                    metadata,
                )
            )
            violations = (await conn.execute(text("PRAGMA foreign_key_check"))).all()
    finally:
        await migrated.dispose()

    assert final_foreign_keys == original_foreign_keys
    assert not any(table_name.startswith("_alembic_tmp_") for table_name in final_tables)
    assert final_counts == failed_counts
    assert PLAN_TABLES <= final_tables
    assert any(
        unique["column_names"] == ["tenant_id", "workspace_id", "session_id", "id"]
        for unique in final_memory_uniques
    )
    assert final_diffs == []
    assert violations == []


def test_durable_plan_migration_rejects_downgrade(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'forward-only.db'}"
    config = alembic_config(database_url=database_url)
    command.upgrade(config, "head")

    with pytest.raises(RuntimeError, match="forward-only"):
        command.downgrade(config, "20260815_0001")


def test_mysql_offline_upgrade_renders_durable_plan_contract():
    config = alembic_config(
        database_url="mysql+aiomysql://user:pass@localhost/multiclaw"
    )
    output = io.StringIO()
    config.output_buffer = output

    command.upgrade(config, "head", sql=True)

    ddl = output.getvalue()
    assert "decision_id VARCHAR(128)" in ddl
    assert "result_ref VARCHAR(128)" in ddl
    assert "fk_agent_plan_step_runs_run_agent_runs" in ddl
    assert "fk_agent_plan_step_runs_result_memory_entries" not in ddl
    assert "ix_agent_plan_step_runs_scope_run_step_attempt" not in ddl


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
