import asyncio
from pathlib import Path
import os
import sys
from uuid import uuid4

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy import exc as sa_exc, inspect, text
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
            user_foreign_keys = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_foreign_keys("users")
            )
            tool_execution_foreign_keys = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_foreign_keys("tool_executions")
            )
            checkpoint_foreign_keys = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_foreign_keys("execution_checkpoints")
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
                    )
                    """
                )
            )
            check_constraints = await conn.execute(
                text(
                    """
                    SELECT constraint_name
                    FROM information_schema.table_constraints
                    WHERE table_schema = DATABASE()
                    AND constraint_type = 'CHECK'
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
        reflected_column_types = {
            (row[0], row[1]): row[2].lower()
            for row in column_types.fetchall()
        }
        assert reflected_column_types[("tool_executions", "input_payload_json")] == "mediumtext"
        assert reflected_column_types[("execution_checkpoints", "payload_json")] == "mediumtext"
        assert reflected_column_types[("user_secrets", "nonce")] in {"binary(12)", "varbinary(12)"}
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
        assert {
            row[0] for row in check_constraints.fetchall()
        } >= {
            "ck_users_users_status_valid",
            "ck_tool_executions_tool_executions_status_valid",
            "ck_tool_executions_tool_executions_recovery_strategy_valid",
            "ck_user_secrets_user_secrets_algorithm_fixed",
        }

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
