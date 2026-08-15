import asyncio
from collections.abc import Iterable

import pytest
from alembic import command
from sqlalchemy import BigInteger, CheckConstraint, inspect, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import IntegrityError

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.schema import metadata


EXPECTED_TABLES = {
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

EXPECTED_PRIMARY_UUID_COLUMNS = {
    ("users", "id"),
    ("workspaces", "id"),
    ("chat_sessions", "id"),
    ("memory_entries", "id"),
    ("agent_runs", "run_id"),
    ("approval_requests", "approval_id"),
    ("tool_executions", "execution_id"),
    ("execution_checkpoints", "checkpoint_id"),
    ("user_secrets", "id"),
    ("audit_logs", "audit_id"),
    ("deletion_jobs", "job_id"),
    ("verification_codes", "id"),
}

EXPECTED_STRING_LENGTHS = {
    ("users", "email"): 320,
    ("users", "status"): 32,
    ("users", "default_workspace_id"): 36,
    ("workspaces", "tenant_id"): 36,
    ("workspaces", "slug"): 64,
    ("workspaces", "name"): 255,
    ("workspaces", "status"): 32,
    ("chat_sessions", "tenant_id"): 36,
    ("chat_sessions", "workspace_id"): 36,
    ("chat_sessions", "title"): 255,
    ("chat_sessions", "status"): 32,
    ("memory_entries", "tenant_id"): 36,
    ("memory_entries", "workspace_id"): 36,
    ("memory_entries", "session_id"): 36,
    ("memory_entries", "type"): 64,
    ("memory_entries", "role"): 32,
    ("agent_runs", "tenant_id"): 36,
    ("agent_runs", "workspace_id"): 36,
    ("agent_runs", "session_id"): 36,
    ("agent_runs", "run_status"): 32,
    ("agent_runs", "runtime_instance_id"): 128,
    ("agent_runs", "lease_owner"): 128,
    ("approval_requests", "tenant_id"): 36,
    ("approval_requests", "workspace_id"): 36,
    ("approval_requests", "session_id"): 36,
    ("approval_requests", "run_id"): 36,
    ("approval_requests", "tool_call_id"): 128,
    ("approval_requests", "approval_status"): 32,
    ("tool_executions", "tenant_id"): 36,
    ("tool_executions", "workspace_id"): 36,
    ("tool_executions", "session_id"): 36,
    ("tool_executions", "run_id"): 36,
    ("tool_executions", "approval_id"): 36,
    ("tool_executions", "tool_call_id"): 128,
    ("tool_executions", "tool_name"): 128,
    ("tool_executions", "tool_kind"): 64,
    ("tool_executions", "execution_status"): 32,
    ("tool_executions", "recovery_strategy"): 32,
    ("tool_executions", "idempotency_key"): 128,
    ("tool_executions", "input_hash"): 64,
    ("tool_executions", "external_request_id"): 255,
    ("tool_executions", "result_ref"): 255,
    ("tool_executions", "result_digest"): 64,
    ("execution_checkpoints", "tenant_id"): 36,
    ("execution_checkpoints", "workspace_id"): 36,
    ("execution_checkpoints", "session_id"): 36,
    ("execution_checkpoints", "run_id"): 36,
    ("execution_checkpoints", "approval_id"): 36,
    ("execution_checkpoints", "execution_id"): 36,
    ("execution_checkpoints", "phase"): 64,
    ("execution_checkpoints", "payload_hash"): 64,
    ("user_secrets", "tenant_id"): 36,
    ("user_secrets", "workspace_id"): 36,
    ("user_secrets", "provider_kind"): 32,
    ("user_secrets", "provider_name"): 128,
    ("user_secrets", "secret_name"): 128,
    ("user_secrets", "key_provider_name"): 128,
    ("user_secrets", "algorithm"): 32,
    ("audit_logs", "tenant_id"): 36,
    ("audit_logs", "workspace_id"): 36,
    ("audit_logs", "session_id"): 36,
    ("audit_logs", "run_id"): 36,
    ("audit_logs", "approval_id"): 36,
    ("audit_logs", "execution_id"): 36,
    ("audit_logs", "event_type"): 64,
    ("audit_logs", "status"): 32,
    ("audit_logs", "tool_name"): 128,
    ("deletion_jobs", "tenant_id"): 36,
    ("deletion_jobs", "status"): 32,
    ("deletion_jobs", "worker_id"): 128,
    ("verification_codes", "email"): 320,
    ("verification_codes", "code_digest"): 128,
    ("verification_codes", "purpose"): 32,
}

EXPECTED_BIGINT_COLUMNS = {
    ("users", "auth_epoch"),
    ("users", "created_at"),
    ("users", "updated_at"),
    ("users", "disabled_at"),
    ("users", "purge_requested_at"),
    ("users", "purge_after"),
    ("workspaces", "created_at"),
    ("workspaces", "updated_at"),
    ("chat_sessions", "created_at"),
    ("chat_sessions", "updated_at"),
    ("chat_sessions", "last_message_at"),
    ("memory_entries", "created_at"),
    ("agent_runs", "fencing_token"),
    ("agent_runs", "lease_expires_at"),
    ("agent_runs", "heartbeat_at"),
    ("agent_runs", "version"),
    ("agent_runs", "created_at"),
    ("agent_runs", "updated_at"),
    ("agent_runs", "finished_at"),
    ("approval_requests", "requested_at"),
    ("approval_requests", "resolved_at"),
    ("approval_requests", "expires_at"),
    ("approval_requests", "version"),
    ("tool_executions", "version"),
    ("tool_executions", "created_at"),
    ("tool_executions", "updated_at"),
    ("tool_executions", "finished_at"),
    ("execution_checkpoints", "checkpoint_seq"),
    ("execution_checkpoints", "created_at"),
    ("user_secrets", "created_at"),
    ("user_secrets", "updated_at"),
    ("user_secrets", "rotated_at"),
    ("audit_logs", "created_at"),
    ("deletion_jobs", "purge_after"),
    ("deletion_jobs", "requested_at"),
    ("deletion_jobs", "started_at"),
    ("deletion_jobs", "lease_expires_at"),
    ("deletion_jobs", "heartbeat_at"),
    ("deletion_jobs", "fencing_token"),
    ("deletion_jobs", "version"),
    ("verification_codes", "expires_at"),
    ("verification_codes", "used_at"),
    ("verification_codes", "created_at"),
}

PAYLOAD_COLUMNS = {
    ("chat_sessions", "metadata_json"),
    ("memory_entries", "content"),
    ("memory_entries", "metadata_json"),
    ("tool_executions", "input_payload_json"),
    ("execution_checkpoints", "payload_json"),
}


def _constraint_names(constraints: Iterable[CheckConstraint]) -> set[str]:
    return {constraint.name for constraint in constraints if constraint.name}


def _sqlite_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}"


def test_core_metadata_matches_schema_contract():
    assert set(metadata.tables) == EXPECTED_TABLES

    for table_name, column_name in EXPECTED_PRIMARY_UUID_COLUMNS:
        column = metadata.tables[table_name].c[column_name]
        assert column.primary_key is True
        assert getattr(column.type, "length", None) == 36

    for table_name, column_name, in EXPECTED_BIGINT_COLUMNS:
        column = metadata.tables[table_name].c[column_name]
        assert isinstance(column.type, BigInteger)

    for table_name, column_name, expected_length in (
        (table, column, length) for (table, column), length in EXPECTED_STRING_LENGTHS.items()
    ):
        column = metadata.tables[table_name].c[column_name]
        assert getattr(column.type, "length", None) == expected_length

    mysql_dialect = mysql.dialect()
    sqlite_dialect = sqlite.dialect()
    for table_name, column_name in PAYLOAD_COLUMNS:
        column = metadata.tables[table_name].c[column_name]
        assert column.type.compile(dialect=sqlite_dialect).upper() == "TEXT"
        assert column.type.compile(dialect=mysql_dialect).upper() == "MEDIUMTEXT"

    for table in metadata.tables.values():
        for foreign_key in table.foreign_key_constraints:
            assert foreign_key.ondelete in {"RESTRICT", "NO ACTION", None}
            assert foreign_key.onupdate in {"RESTRICT", "NO ACTION", None}

    assert any(
        fk.column_keys == ["id", "default_workspace_id"]
        and [element.column.name for element in fk.elements] == ["tenant_id", "id"]
        for fk in metadata.tables["users"].foreign_key_constraints
    )

    expected_check_names = {
        "ck_users_users_status_valid",
        "ck_workspaces_workspaces_status_valid",
        "ck_agent_runs_agent_runs_run_status_valid",
        "ck_approval_requests_approval_requests_status_valid",
        "ck_tool_executions_tool_executions_status_valid",
        "ck_tool_executions_tool_executions_recovery_strategy_valid",
        "ck_user_secrets_user_secrets_key_provider_name_fixed",
        "ck_user_secrets_user_secrets_format_version_fixed",
        "ck_user_secrets_user_secrets_algorithm_fixed",
        "ck_deletion_jobs_deletion_jobs_status_valid",
        "ck_verification_codes_verification_codes_purpose_valid",
    }
    actual_check_names = set()
    for table in metadata.tables.values():
        actual_check_names |= _constraint_names(
            constraint for constraint in table.constraints if isinstance(constraint, CheckConstraint)
        )
    assert expected_check_names <= actual_check_names


@pytest.mark.asyncio
async def test_sqlite_baseline_enforces_scoped_foreign_keys_and_has_clean_foreign_key_check(tmp_path):
    database_url = _sqlite_url(tmp_path)
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")

    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        async with database.write_transaction() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO users (
                        id, email, auth_epoch, default_workspace_id, status,
                        purge_after, created_at, updated_at, disabled_at, purge_requested_at
                    )
                    VALUES (
                        'tenant-000000000000000000000000000001',
                        'tenant@example.com',
                        0,
                        NULL,
                        'active',
                        NULL,
                        1,
                        1,
                        NULL,
                        NULL
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                    VALUES
                        ('workspace-0000000000000000000000000001', 'tenant-000000000000000000000000000001', 'main', 'Main', 'active', 1, 1),
                        ('workspace-0000000000000000000000000002', 'tenant-000000000000000000000000000001', 'other', 'Other', 'active', 1, 1)
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    UPDATE users
                    SET default_workspace_id = 'workspace-0000000000000000000000000001'
                    WHERE id = 'tenant-000000000000000000000000000001'
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO chat_sessions (
                        id, tenant_id, workspace_id, title, status, created_at, updated_at, last_message_at, metadata_json
                    )
                    VALUES (
                        'session-000000000000000000000000000001',
                        'tenant-000000000000000000000000000001',
                        'workspace-0000000000000000000000000001',
                        'Thread',
                        'active',
                        1,
                        1,
                        NULL,
                        '{}'
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO agent_runs (
                        run_id, tenant_id, workspace_id, session_id, run_status, runtime_instance_id,
                        lease_owner, fencing_token, lease_expires_at, heartbeat_at, schema_version,
                        version, created_at, updated_at, finished_at
                    )
                    VALUES (
                        'run-00000000000000000000000000000001',
                        'tenant-000000000000000000000000000001',
                        'workspace-0000000000000000000000000001',
                        'session-000000000000000000000000000001',
                        'running',
                        NULL,
                        NULL,
                        0,
                        NULL,
                        NULL,
                        1,
                        1,
                        1,
                        1,
                        NULL
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO tool_executions (
                        execution_id, tenant_id, workspace_id, session_id, run_id, approval_id,
                        tool_call_id, tool_name, tool_kind, execution_status, recovery_strategy,
                        idempotency_key, input_payload_json, input_hash, external_request_id,
                        result_ref, result_digest, schema_version, version, created_at, updated_at, finished_at
                    )
                    VALUES (
                        'execution-00000000000000000000000001',
                        'tenant-000000000000000000000000000001',
                        'workspace-0000000000000000000000000001',
                        'session-000000000000000000000000000001',
                        'run-00000000000000000000000000000001',
                        NULL,
                        'tool-call-1',
                        'shell',
                        'builtin',
                        'executing',
                        'idempotent_retry',
                        NULL,
                        '{}',
                        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
                        NULL,
                        NULL,
                        NULL,
                        1,
                        1,
                        1,
                        1,
                        NULL
                    )
                    """
                )
            )

        with pytest.raises(IntegrityError):
            async with database.write_transaction() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO tool_executions (
                            execution_id, tenant_id, workspace_id, session_id, run_id, approval_id,
                            tool_call_id, tool_name, tool_kind, execution_status, recovery_strategy,
                            idempotency_key, input_payload_json, input_hash, external_request_id,
                            result_ref, result_digest, schema_version, version, created_at, updated_at, finished_at
                        )
                        VALUES (
                            'execution-00000000000000000000000002',
                            'tenant-000000000000000000000000000001',
                            'workspace-0000000000000000000000000002',
                            'session-000000000000000000000000000001',
                            'run-00000000000000000000000000000001',
                            NULL,
                            'tool-call-2',
                            'shell',
                            'builtin',
                            'executing',
                            'idempotent_retry',
                            NULL,
                            '{}',
                            'abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd',
                            NULL,
                            NULL,
                            NULL,
                            1,
                            1,
                            1,
                            1,
                            NULL
                        )
                        """
                    )
                )

        async with database.connect() as conn:
            violations = await conn.execute(text("PRAGMA foreign_key_check"))
            assert violations.fetchall() == []
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_sqlite_baseline_introspection_exposes_expected_constraints(tmp_path):
    database_url = _sqlite_url(tmp_path)
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")

    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        async with database.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
            tool_execution_uniques = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_unique_constraints("tool_executions")
            )
            user_foreign_keys = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_foreign_keys("users")
            )
            tool_execution_indexes = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_indexes("tool_executions")
            )
            create_statements = await conn.execute(
                text(
                    """
                    SELECT sql
                    FROM sqlite_master
                    WHERE type = 'table'
                    AND name IN ('users', 'tool_executions')
                    ORDER BY name
                    """
                )
            )
            ddl = "\n".join(row[0] for row in create_statements.fetchall())

        assert tables - {"alembic_version"} == EXPECTED_TABLES
        assert "alembic_version" in tables
        assert {
            constraint["name"] for constraint in tool_execution_uniques
        } >= {
            "uq_tool_executions_tenant_id_execution_id",
            "uq_tool_executions_tenant_id_workspace_id_session_id_run_id_execution_id",
            "uq_tool_executions_tenant_id_workspace_id_session_id_run_id_tool_call_id",
        }
        assert any(
            fk["constrained_columns"] == ["id", "default_workspace_id"]
            and fk["referred_columns"] == ["tenant_id", "id"]
            and fk["referred_table"] == "workspaces"
            for fk in user_foreign_keys
        )
        assert {
            index["name"] for index in tool_execution_indexes
        } >= {
            "ix_tool_executions_tenant_id_workspace_id_session_id_run_id",
            "ix_tool_executions_tenant_id_workspace_id_session_id_run_id_approval_id",
        }
        assert "CONSTRAINT fk_users_id_default_workspace_id_workspaces" in ddl
        assert "CONSTRAINT fk_tool_executions_tenant_id_workspace_id_session_id_run_id_agent_runs" in ddl
    finally:
        await database.dispose()
