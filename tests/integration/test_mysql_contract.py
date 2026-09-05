import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.runtime.migration import MigrationContext
from sqlalchemy import exc as sa_exc
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from alembic import command
from multiclaw.cli import alembic_config, check_revision_is_head
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.tenancy import TenantContext

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL

PLAN_TABLES = {
    "agent_plans",
    "agent_plan_versions",
    "agent_plan_steps",
    "agent_plan_step_dependencies",
    "agent_plan_step_runs",
    "agent_plan_decisions",
}


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
            user_foreign_keys = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_foreign_keys("users")
            )
            tool_execution_foreign_keys = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_foreign_keys("tool_executions")
            )
            checkpoint_foreign_keys = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_foreign_keys("execution_checkpoints")
            )
            agent_run_columns = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns("agent_runs")
            )
            agent_run_uniques = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_unique_constraints("agent_runs")
            )
            step_run_foreign_keys = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_foreign_keys("agent_plan_step_runs")
            )
            engines = await conn.execute(
                text(
                    """
                    SELECT table_name, engine
                    FROM information_schema.tables
                    WHERE table_schema = DATABASE()
                    """
                )
            )
            column_types = await conn.execute(
                text(
                    """
                    SELECT table_name, column_name, column_type
                    FROM information_schema.columns
                    WHERE table_schema = DATABASE()
                    AND (
                        (table_name = 'tool_executions' AND column_name = 'input_payload_json')
                        OR (table_name = 'execution_checkpoints' AND column_name = 'payload_json')
                        OR (table_name = 'user_secrets' AND column_name = 'nonce')
                        OR (table_name = 'agent_plan_versions' AND column_name IN (
                            'objective', 'constraints_json', 'generation_reason', 'revision_feedback'
                        ))
                        OR (table_name = 'agent_plan_steps' AND column_name IN (
                            'description', 'expected_outcome'
                        ))
                        OR (table_name = 'agent_plan_decisions' AND column_name = 'feedback')
                        OR (table_name = 'agent_plan_decisions' AND column_name = 'decision_id')
                        OR (table_name = 'agent_plan_step_runs' AND column_name IN (
                            'result_summary', 'result_ref', 'error_detail_redacted'
                        ))
                    )
                    """
                )
            )
            check_constraints = await conn.execute(
                text(
                    """
                    SELECT tc.constraint_name, cc.check_clause
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.check_constraints AS cc
                      ON cc.constraint_schema = tc.constraint_schema
                     AND cc.constraint_name = tc.constraint_name
                    WHERE tc.table_schema = DATABASE()
                    AND tc.constraint_type = 'CHECK'
                    """
                )
            )

        assert revision == "20260905_0002"
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
        } | PLAN_TABLES
        assert "alembic_version" in table_names
        assert PLAN_TABLES <= table_names
        assert {column["name"] for column in agent_run_columns} >= {
            "plan_id",
            "initial_plan_version",
            "active_plan_version",
            "cancel_requested_at",
        }
        assert any(
            unique["name"] == "uq_agent_runs_scope_plan_run"
            and unique["column_names"]
            == ["tenant_id", "workspace_id", "session_id", "plan_id", "run_id"]
            for unique in agent_run_uniques
        )
        assert any(
            foreign_key["constrained_columns"]
            == ["tenant_id", "workspace_id", "session_id", "plan_id", "run_id"]
            and foreign_key["referred_table"] == "agent_runs"
            and foreign_key["referred_columns"]
            == ["tenant_id", "workspace_id", "session_id", "plan_id", "run_id"]
            for foreign_key in step_run_foreign_keys
        )
        assert {row[1].lower() for row in engines.fetchall()} == {"innodb"}
        reflected_column_types = {
            (row[0], row[1]): row[2].lower()
            for row in column_types.fetchall()
        }
        assert reflected_column_types[("tool_executions", "input_payload_json")] == "mediumtext"
        assert reflected_column_types[("execution_checkpoints", "payload_json")] == "mediumtext"
        assert reflected_column_types[("user_secrets", "nonce")] in {"binary(12)", "varbinary(12)"}
        assert reflected_column_types[("agent_plan_decisions", "decision_id")] == "varchar(128)"
        assert reflected_column_types[("agent_plan_step_runs", "result_ref")] == "varchar(128)"
        assert not any(
            "result_ref" in foreign_key["constrained_columns"]
            for foreign_key in step_run_foreign_keys
        )
        assert {
            reflected_column_types[(table_name, column_name)]
            for table_name, column_name in {
                ("agent_plan_versions", "objective"),
                ("agent_plan_versions", "constraints_json"),
                ("agent_plan_versions", "generation_reason"),
                ("agent_plan_versions", "revision_feedback"),
                ("agent_plan_steps", "description"),
                ("agent_plan_steps", "expected_outcome"),
                ("agent_plan_decisions", "feedback"),
                ("agent_plan_step_runs", "result_summary"),
                ("agent_plan_step_runs", "error_detail_redacted"),
            }
        } == {"mediumtext"}
        assert any(
            fk["constrained_columns"] == ["id", "default_workspace_id"]
            and fk["referred_table"] == "workspaces"
            and fk["referred_columns"] == ["tenant_id", "id"]
            for fk in user_foreign_keys
        )
        assert any(
            fk["constrained_columns"] == ["tenant_id", "workspace_id", "session_id", "run_id", "approval_id"]
            and fk["referred_table"] == "approval_requests"
            for fk in tool_execution_foreign_keys
        )
        assert any(
            fk["constrained_columns"] == ["tenant_id", "workspace_id", "session_id", "run_id", "execution_id"]
            and fk["referred_table"] == "tool_executions"
            for fk in checkpoint_foreign_keys
        )
        reflected_checks = dict(check_constraints.fetchall())
        assert set(reflected_checks) >= {
            "ck_users_users_status_valid",
            "ck_tool_executions_tool_executions_status_valid",
            "ck_tool_executions_tool_executions_recovery_strategy_valid",
            "ck_user_secrets_user_secrets_algorithm_fixed",
            "ck_agent_plans_status_valid",
            "ck_agent_plan_decisions_action_valid",
            "ck_agent_plan_decisions_decision_id_length",
            "ck_agent_plan_step_runs_status_valid",
        }
        for status in ("awaiting_approval", "approved", "rejected", "archived"):
            assert f"'{status}'" in reflected_checks["ck_agent_plans_status_valid"]
        for action in ("approve", "reject", "revise"):
            assert f"'{action}'" in reflected_checks["ck_agent_plan_decisions_action_valid"]
        assert "128" in reflected_checks["ck_agent_plan_decisions_decision_id_length"]
        for status in (
            "pending",
            "running",
            "succeeded",
            "failed_retryable",
            "failed_terminal",
            "cancelled",
        ):
            assert f"'{status}'" in reflected_checks["ck_agent_plan_step_runs_status_valid"]

        async with database.write_transaction() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO users (
                        id, email, auth_epoch, default_workspace_id, status,
                        purge_after, created_at, updated_at, disabled_at, purge_requested_at
                    ) VALUES (
                        :tenant_id, :email, 0, NULL, 'active', NULL, 1, 1, NULL, NULL
                    )
                    """
                ),
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "email": "tenant@example.com",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                    VALUES
                    (:workspace_main, :tenant_id, 'main', 'Main', 'active', 1, 1),
                    (:workspace_other, :tenant_id, 'other', 'Other', 'active', 1, 1)
                    """
                ),
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "workspace_main": "00000000-0000-0000-0000-000000000101",
                    "workspace_other": "00000000-0000-0000-0000-000000000102",
                },
            )
            await conn.execute(
                text(
                    """
                    UPDATE users
                    SET default_workspace_id = :workspace_main
                    WHERE id = :tenant_id
                    """
                ),
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "workspace_main": "00000000-0000-0000-0000-000000000101",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO chat_sessions (
                        id, tenant_id, workspace_id, title, status, created_at, updated_at, last_message_at, metadata_json
                    ) VALUES (
                        :session_id, :tenant_id, :workspace_main, 'Thread', 'active', 1, 1, NULL, '{}'
                    )
                    """
                ),
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "workspace_main": "00000000-0000-0000-0000-000000000101",
                    "session_id": "00000000-0000-0000-0000-000000000201",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO agent_runs (
                        run_id, tenant_id, workspace_id, session_id, run_status, runtime_instance_id,
                        lease_owner, fencing_token, lease_expires_at, heartbeat_at, schema_version,
                        version, created_at, updated_at, finished_at
                    ) VALUES (
                        :run_id, :tenant_id, :workspace_main, :session_id, 'running', NULL, NULL,
                        0, NULL, NULL, 1, 1, 1, 1, NULL
                    )
                    """
                ),
                {
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "workspace_main": "00000000-0000-0000-0000-000000000101",
                    "session_id": "00000000-0000-0000-0000-000000000201",
                    "run_id": "00000000-0000-0000-0000-000000000301",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO user_secrets (
                        id, tenant_id, workspace_id, provider_kind, provider_name, secret_name,
                        key_provider_name, format_version, algorithm, key_version, nonce, ciphertext,
                        created_at, updated_at, rotated_at
                    ) VALUES (
                        :secret_id, :tenant_id, NULL, 'api', 'openai', 'primary',
                        'deployment-keyring', 1, 'AES-256-GCM', 1, :nonce, :ciphertext, 1, 1, NULL
                    )
                    """
                ),
                {
                    "secret_id": "00000000-0000-0000-0000-000000000401",
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "nonce": b"123456789012",
                    "ciphertext": b"ciphertext-with-tag",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO tool_executions (
                        execution_id, tenant_id, workspace_id, session_id, run_id, approval_id,
                        tool_call_id, tool_name, tool_kind, execution_status, recovery_strategy,
                        idempotency_key, input_payload_json, input_hash, external_request_id,
                        result_ref, result_digest, schema_version, version, created_at, updated_at, finished_at
                    ) VALUES (
                        :execution_id, :tenant_id, :workspace_main, :session_id, :run_id, NULL,
                        'tool-call-1', 'shell', 'builtin', 'executing', 'idempotent_retry',
                        NULL, '{}', :input_hash, NULL, NULL, NULL, 1, 1, 1, 1, NULL
                    )
                    """
                ),
                {
                    "execution_id": "00000000-0000-0000-0000-000000000501",
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "workspace_main": "00000000-0000-0000-0000-000000000101",
                    "session_id": "00000000-0000-0000-0000-000000000201",
                    "run_id": "00000000-0000-0000-0000-000000000301",
                    "input_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO execution_checkpoints (
                        checkpoint_id, tenant_id, workspace_id, session_id, run_id, approval_id,
                        execution_id, phase, checkpoint_seq, payload_json, payload_hash, schema_version, created_at
                    ) VALUES (
                        :checkpoint_id, :tenant_id, :workspace_main, :session_id, :run_id, NULL,
                        :execution_id, 'tool_running', 1, '{}', :payload_hash, 1, 1
                    )
                    """
                ),
                {
                    "checkpoint_id": "00000000-0000-0000-0000-000000000601",
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                    "workspace_main": "00000000-0000-0000-0000-000000000101",
                    "session_id": "00000000-0000-0000-0000-000000000201",
                    "run_id": "00000000-0000-0000-0000-000000000301",
                    "execution_id": "00000000-0000-0000-0000-000000000501",
                    "payload_hash": "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd",
                },
            )

        with pytest.raises(sa_exc.IntegrityError):
            async with database.write_transaction() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO tool_executions (
                            execution_id, tenant_id, workspace_id, session_id, run_id, approval_id,
                            tool_call_id, tool_name, tool_kind, execution_status, recovery_strategy,
                            idempotency_key, input_payload_json, input_hash, external_request_id,
                            result_ref, result_digest, schema_version, version, created_at, updated_at, finished_at
                        ) VALUES (
                            :execution_id, :tenant_id, :workspace_other, :session_id, :run_id, NULL,
                            'tool-call-2', 'shell', 'builtin', 'executing', 'idempotent_retry',
                            NULL, '{}', :input_hash, NULL, NULL, NULL, 1, 1, 1, 1, NULL
                        )
                        """
                    ),
                    {
                        "execution_id": "00000000-0000-0000-0000-000000000502",
                        "tenant_id": "00000000-0000-0000-0000-000000000001",
                        "workspace_other": "00000000-0000-0000-0000-000000000102",
                        "session_id": "00000000-0000-0000-0000-000000000201",
                        "run_id": "00000000-0000-0000-0000-000000000301",
                        "input_hash": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
                    },
                )

        with pytest.raises(sa_exc.IntegrityError):
            async with database.write_transaction() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO execution_checkpoints (
                            checkpoint_id, tenant_id, workspace_id, session_id, run_id, approval_id,
                            execution_id, phase, checkpoint_seq, payload_json, payload_hash, schema_version, created_at
                        ) VALUES (
                            :checkpoint_id, :tenant_id, :workspace_other, :session_id, :run_id, NULL,
                            :execution_id, 'tool_running', 2, '{}', :payload_hash, 1, 1
                        )
                        """
                    ),
                    {
                        "checkpoint_id": "00000000-0000-0000-0000-000000000602",
                        "tenant_id": "00000000-0000-0000-0000-000000000001",
                        "workspace_other": "00000000-0000-0000-0000-000000000102",
                        "session_id": "00000000-0000-0000-0000-000000000201",
                        "run_id": "00000000-0000-0000-0000-000000000301",
                        "execution_id": "00000000-0000-0000-0000-000000000501",
                        "payload_hash": "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
                    },
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_mysql_lock_run_blocks_with_for_update(isolated_mysql_database_url):
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=isolated_mysql_database_url), "head")

    database = Database.create(DatabaseSettings(driver="mysql", url=isolated_mysql_database_url))
    context = TenantContext(
        tenant_id="00000000-0000-0000-0000-000000000001",
        workspace_id="00000000-0000-0000-0000-000000000101",
        session_id="00000000-0000-0000-0000-000000000201",
        run_id="00000000-0000-0000-0000-000000000301",
    )

    try:
        async with database.write_transaction() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO users (
                        id, email, auth_epoch, default_workspace_id, status,
                        purge_after, created_at, updated_at, disabled_at, purge_requested_at
                    ) VALUES (
                        :tenant_id, :email, 0, NULL, 'active', NULL, 1, 1, NULL, NULL
                    )
                    """
                ),
                {"tenant_id": context.tenant_id, "email": "lease-lock@example.com"},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                    VALUES (:workspace_id, :tenant_id, 'default', 'Default', 'active', 1, 1)
                    """
                ),
                {"tenant_id": context.tenant_id, "workspace_id": context.workspace_id},
            )
            await conn.execute(
                text(
                    """
                    UPDATE users
                    SET default_workspace_id = :workspace_id
                    WHERE id = :tenant_id
                    """
                ),
                {"tenant_id": context.tenant_id, "workspace_id": context.workspace_id},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO chat_sessions (
                        id, tenant_id, workspace_id, title, status, created_at, updated_at, last_message_at, metadata_json
                    ) VALUES (
                        :session_id, :tenant_id, :workspace_id, 'Lease Lock', 'active', 1, 1, NULL, '{}'
                    )
                    """
                ),
                {
                    "tenant_id": context.tenant_id,
                    "workspace_id": context.workspace_id,
                    "session_id": context.session_id,
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO agent_runs (
                        run_id, tenant_id, workspace_id, session_id, run_status, runtime_instance_id,
                        lease_owner, fencing_token, lease_expires_at, heartbeat_at, schema_version,
                        version, created_at, updated_at, finished_at
                    ) VALUES (
                        :run_id, :tenant_id, :workspace_id, :session_id, 'running', 'runtime-1',
                        'runtime-1', 1, 9999999999999, 1, 1, 1, 1, 1, NULL
                    )
                    """
                ),
                {
                    "tenant_id": context.tenant_id,
                    "workspace_id": context.workspace_id,
                    "session_id": context.session_id,
                    "run_id": context.run_id,
                },
            )

        conn1 = await database.engine.connect()
        tx1 = await database.dialect.begin_write(conn1)
        conn2 = await database.engine.connect()
        tx2 = await database.dialect.begin_write(conn2)
        try:
            await database.dialect.lock_run(conn1, context)

            blocked = asyncio.create_task(database.dialect.lock_run(conn2, context))
            await asyncio.sleep(0.2)
            assert blocked.done() is False

            await tx1.commit()
            await asyncio.wait_for(blocked, timeout=3)
        finally:
            if tx1.is_active:
                await tx1.rollback()
            if tx2.is_active:
                await tx2.rollback()
            await conn1.close()
            await conn2.close()
    finally:
        await database.dispose()
