import asyncio
from collections.abc import Iterable, Mapping

import pytest
from sqlalchemy import BigInteger, CheckConstraint, inspect, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from alembic import command
from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.schema import metadata

PLAN_TABLES = {
    "agent_plans",
    "agent_plan_versions",
    "agent_plan_steps",
    "agent_plan_step_dependencies",
    "agent_plan_step_runs",
    "agent_plan_decisions",
}

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
} | PLAN_TABLES

EXPECTED_PRIMARY_UUID_COLUMNS = {
    ("users", "id"),
    ("workspaces", "id"),
    ("chat_sessions", "id"),
    ("memory_entries", "id"),
    ("agent_plans", "id"),
    ("agent_plan_steps", "step_id"),
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
    ("agent_plans", "id"): 36,
    ("agent_plans", "tenant_id"): 36,
    ("agent_plans", "workspace_id"): 36,
    ("agent_plans", "session_id"): 36,
    ("agent_plans", "source_message_id"): 36,
    ("agent_plans", "trigger_mode"): 16,
    ("agent_plans", "status"): 32,
    ("agent_plan_versions", "tenant_id"): 36,
    ("agent_plan_versions", "workspace_id"): 36,
    ("agent_plan_versions", "session_id"): 36,
    ("agent_plan_versions", "plan_id"): 36,
    ("agent_plan_versions", "content_digest"): 64,
    ("agent_plan_steps", "tenant_id"): 36,
    ("agent_plan_steps", "workspace_id"): 36,
    ("agent_plan_steps", "session_id"): 36,
    ("agent_plan_steps", "plan_id"): 36,
    ("agent_plan_steps", "step_id"): 36,
    ("agent_plan_steps", "logical_step_key"): 64,
    ("agent_plan_steps", "supersedes_step_id"): 36,
    ("agent_plan_steps", "title"): 200,
    ("agent_plan_steps", "assigned_agent_profile_id"): 36,
    ("agent_plan_steps", "definition_digest"): 64,
    ("agent_plan_step_dependencies", "tenant_id"): 36,
    ("agent_plan_step_dependencies", "workspace_id"): 36,
    ("agent_plan_step_dependencies", "session_id"): 36,
    ("agent_plan_step_dependencies", "plan_id"): 36,
    ("agent_plan_step_dependencies", "step_id"): 36,
    ("agent_plan_step_dependencies", "depends_on_step_id"): 36,
    ("agent_plan_decisions", "tenant_id"): 36,
    ("agent_plan_decisions", "workspace_id"): 36,
    ("agent_plan_decisions", "session_id"): 36,
    ("agent_plan_decisions", "plan_id"): 36,
    ("agent_plan_decisions", "decision_id"): 36,
    ("agent_plan_decisions", "action"): 16,
    ("agent_plan_decisions", "decided_by"): 36,
    ("agent_runs", "tenant_id"): 36,
    ("agent_runs", "workspace_id"): 36,
    ("agent_runs", "session_id"): 36,
    ("agent_runs", "run_status"): 32,
    ("agent_runs", "runtime_instance_id"): 128,
    ("agent_runs", "lease_owner"): 128,
    ("agent_runs", "plan_id"): 36,
    ("agent_plan_step_runs", "tenant_id"): 36,
    ("agent_plan_step_runs", "workspace_id"): 36,
    ("agent_plan_step_runs", "session_id"): 36,
    ("agent_plan_step_runs", "plan_id"): 36,
    ("agent_plan_step_runs", "step_id"): 36,
    ("agent_plan_step_runs", "step_run_id"): 36,
    ("agent_plan_step_runs", "run_id"): 36,
    ("agent_plan_step_runs", "status"): 32,
    ("agent_plan_step_runs", "result_ref"): 36,
    ("agent_plan_step_runs", "result_digest"): 64,
    ("agent_plan_step_runs", "error_code"): 64,
    ("agent_plan_step_runs", "reused_from_step_run_id"): 36,
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
    ("agent_plans", "version"),
    ("agent_plans", "created_at"),
    ("agent_plans", "updated_at"),
    ("agent_plan_versions", "created_at"),
    ("agent_plan_decisions", "expected_plan_cas_version"),
    ("agent_plan_decisions", "created_at"),
    ("agent_runs", "fencing_token"),
    ("agent_runs", "lease_expires_at"),
    ("agent_runs", "heartbeat_at"),
    ("agent_runs", "version"),
    ("agent_runs", "created_at"),
    ("agent_runs", "updated_at"),
    ("agent_runs", "finished_at"),
    ("agent_runs", "cancel_requested_at"),
    ("agent_plan_step_runs", "version"),
    ("agent_plan_step_runs", "started_at"),
    ("agent_plan_step_runs", "finished_at"),
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
    ("agent_plan_versions", "objective"),
    ("agent_plan_versions", "constraints_json"),
    ("agent_plan_versions", "generation_reason"),
    ("agent_plan_versions", "revision_feedback"),
    ("agent_plan_steps", "description"),
    ("agent_plan_steps", "expected_outcome"),
    ("agent_plan_decisions", "feedback"),
    ("agent_plan_step_runs", "result_summary"),
    ("agent_plan_step_runs", "error_detail_redacted"),
    ("tool_executions", "input_payload_json"),
    ("execution_checkpoints", "payload_json"),
}


def _constraint_names(constraints: Iterable[CheckConstraint]) -> set[str]:
    return {str(constraint.name) for constraint in constraints if constraint.name}


def _sqlite_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}"


@pytest.fixture
async def database(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'plan-constraints.db'}"
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")
    instance = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest.fixture
async def seeded_scopes(database):
    primary = {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "workspace_id": "00000000-0000-0000-0000-000000000101",
        "session_id": "00000000-0000-0000-0000-000000000201",
        "source_message_id": "00000000-0000-0000-0000-000000000301",
        "plan_id": "00000000-0000-0000-0000-000000000401",
        "version_one_step_id": "00000000-0000-0000-0000-000000000501",
        "version_two_step_id": "00000000-0000-0000-0000-000000000502",
        "run_id": "00000000-0000-0000-0000-000000000601",
    }
    foreign = {
        **primary,
        "session_id": "00000000-0000-0000-0000-000000000202",
    }
    digest = "a" * 64

    async with database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users (
                    id, email, auth_epoch, default_workspace_id, status, purge_after,
                    created_at, updated_at, disabled_at, purge_requested_at
                ) VALUES (:tenant_id, 'plans@example.com', 0, NULL, 'active', NULL, 1, 1, NULL, NULL)
                """
            ),
            primary,
        )
        await conn.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                VALUES (:workspace_id, :tenant_id, 'plans', 'Plans', 'active', 1, 1)
                """
            ),
            primary,
        )
        await conn.execute(
            text(
                """
                INSERT INTO chat_sessions (
                    id, tenant_id, workspace_id, title, status, created_at, updated_at,
                    last_message_at, metadata_json
                ) VALUES
                    (:primary_session_id, :tenant_id, :workspace_id, 'Primary', 'active', 1, 1, NULL, '{}'),
                    (:foreign_session_id, :tenant_id, :workspace_id, 'Foreign', 'active', 1, 1, NULL, '{}')
                """
            ),
            {
                **primary,
                "primary_session_id": primary["session_id"],
                "foreign_session_id": foreign["session_id"],
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO memory_entries (
                    id, tenant_id, workspace_id, session_id, content, type, role,
                    turn_index, created_at, metadata_json
                ) VALUES (
                    :source_message_id, :tenant_id, :workspace_id, :session_id,
                    'make a plan', 'message', 'user', 1, 1, '{}'
                )
                """
            ),
            primary,
        )
        await conn.execute(
            text(
                """
                INSERT INTO agent_plans (
                    id, tenant_id, workspace_id, session_id, source_message_id,
                    trigger_mode, status, current_version, approved_version,
                    version, created_at, updated_at
                ) VALUES (
                    :plan_id, :tenant_id, :workspace_id, :session_id, :source_message_id,
                    'explicit', 'awaiting_approval', 2, NULL, 1, 1, 1
                )
                """
            ),
            primary,
        )
        await conn.execute(
            text(
                """
                INSERT INTO agent_plan_versions (
                    tenant_id, workspace_id, session_id, plan_id, plan_version,
                    objective, constraints_json, generation_reason, parent_version,
                    revision_feedback, schema_version, content_digest, created_at
                ) VALUES
                    (:tenant_id, :workspace_id, :session_id, :plan_id, 1,
                     'Initial', '{}', 'initial', NULL, NULL, 1, :digest, 1),
                    (:tenant_id, :workspace_id, :session_id, :plan_id, 2,
                     'Revised', '{}', 'revision', 1, 'clarify', 1, :digest, 2)
                """
            ),
            {**primary, "digest": digest},
        )
        await conn.execute(
            text(
                """
                INSERT INTO agent_runs (
                    run_id, tenant_id, workspace_id, session_id, plan_id,
                    initial_plan_version, active_plan_version, run_status,
                    fencing_token, schema_version, version, created_at, updated_at
                ) VALUES (
                    :run_id, :tenant_id, :workspace_id, :session_id, :plan_id,
                    1, 2, 'running', 0, 1, 1, 1, 1
                )
                """
            ),
            primary,
        )
        await conn.execute(
            text(
                """
                INSERT INTO agent_plan_steps (
                    tenant_id, workspace_id, session_id, plan_id, plan_version,
                    step_id, logical_step_key, supersedes_step_id, ordinal, title,
                    description, expected_outcome, assigned_agent_profile_id,
                    max_attempts, definition_digest
                ) VALUES
                    (:tenant_id, :workspace_id, :session_id, :plan_id, 1,
                     :version_one_step_id, 'build', NULL, 1, 'Build',
                     'Build it', 'Built', NULL, 3, :digest),
                    (:tenant_id, :workspace_id, :session_id, :plan_id, 2,
                     :version_two_step_id, 'build', :version_one_step_id, 1, 'Build better',
                     'Build it better', 'Built better', NULL, 3, :digest)
                """
            ),
            {**primary, "digest": digest},
        )

    return primary, foreign


async def insert_cross_session_plan_step(
    conn: AsyncConnection,
    primary: dict[str, str],
    foreign: dict[str, str],
) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO agent_plan_steps (
                tenant_id, workspace_id, session_id, plan_id, plan_version,
                step_id, logical_step_key, supersedes_step_id, ordinal, title,
                description, expected_outcome, assigned_agent_profile_id,
                max_attempts, definition_digest
            ) VALUES (
                :tenant_id, :workspace_id, :foreign_session_id, :plan_id, 1,
                '00000000-0000-0000-0000-000000000503', 'foreign', NULL, 2,
                'Foreign', 'Wrong session', 'Rejected', NULL, 1, :digest
            )
            """
        ),
        {**primary, "foreign_session_id": foreign["session_id"], "digest": "b" * 64},
    )


async def insert_cross_version_dependency(
    conn: AsyncConnection,
    primary: dict[str, str],
) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO agent_plan_step_dependencies (
                tenant_id, workspace_id, session_id, plan_id, plan_version,
                step_id, depends_on_step_id
            ) VALUES (
                :tenant_id, :workspace_id, :session_id, :plan_id, 2,
                :version_two_step_id, :version_one_step_id
            )
            """
        ),
        primary,
    )


async def assert_migrated_check_rejects(
    conn: AsyncConnection,
    *,
    case: str,
    statement: str,
    parameters: Mapping[str, object],
    constraint_name: str,
) -> None:
    savepoint = await conn.begin_nested()
    try:
        with pytest.raises(IntegrityError) as raised:
            await conn.execute(text(statement), parameters)
        assert constraint_name in str(raised.value.orig), case
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


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

    assert metadata.tables["tool_executions"].c.schema_version.server_default is None
    assert metadata.tables["execution_checkpoints"].c.schema_version.server_default is None
    assert metadata.tables["agent_runs"].c.schema_version.server_default is not None
    assert metadata.tables["agent_runs"].c.version.server_default is not None
    assert metadata.tables["agent_plans"].c.version.server_default is not None
    assert metadata.tables["agent_plan_step_runs"].c.version.server_default is not None

    assert list(metadata.tables["agent_plan_versions"].primary_key.columns.keys()) == [
        "tenant_id",
        "workspace_id",
        "session_id",
        "plan_id",
        "plan_version",
    ]

    step_foreign_keys = metadata.tables["agent_plan_steps"].foreign_key_constraints
    assert any(
        fk.column_keys
        == ["tenant_id", "workspace_id", "session_id", "plan_id", "plan_version"]
        and fk.referred_table.name == "agent_plan_versions"
        for fk in step_foreign_keys
    )
    dependency_foreign_keys = metadata.tables[
        "agent_plan_step_dependencies"
    ].foreign_key_constraints
    assert sum(
        fk.referred_table.name == "agent_plan_steps"
        and fk.column_keys[:5]
        == ["tenant_id", "workspace_id", "session_id", "plan_id", "plan_version"]
        for fk in dependency_foreign_keys
    ) == 2

    assert any(
        fk.column_keys == ["id", "default_workspace_id"]
        and [element.column.name for element in fk.elements] == ["tenant_id", "id"]
        for fk in metadata.tables["users"].foreign_key_constraints
    )

    expected_check_names = {
        "ck_users_users_status_valid",
        "ck_workspaces_workspaces_status_valid",
        "ck_agent_plans_trigger_mode_valid",
        "ck_agent_plans_status_valid",
        "ck_agent_plans_current_version_positive",
        "ck_agent_plans_approved_version_valid",
        "ck_agent_plan_versions_plan_version_positive",
        "ck_agent_plan_versions_schema_version_positive",
        "ck_agent_plan_versions_content_digest_valid",
        "ck_agent_plan_versions_parent_version_valid",
        "ck_agent_plan_steps_ordinal_valid",
        "ck_agent_plan_steps_max_attempts_valid",
        "ck_agent_plan_steps_definition_digest_valid",
        "ck_agent_plan_step_dependencies_distinct_steps",
        "ck_agent_plan_decisions_plan_version_positive",
        "ck_agent_plan_decisions_expected_cas_positive",
        "ck_agent_plan_decisions_action_valid",
        "ck_agent_runs_agent_runs_run_status_valid",
        "ck_agent_runs_plan_binding_complete",
        "ck_agent_plan_step_runs_status_valid",
        "ck_agent_plan_step_runs_attempt_positive",
        "ck_agent_plan_step_runs_version_positive",
        "ck_agent_plan_step_runs_result_digest_valid",
        "ck_agent_plan_step_runs_finished_at_valid",
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
async def test_plan_schema_rejects_cross_session_step_and_dependency(database, seeded_scopes):
    primary, foreign = seeded_scopes
    async with database.write_transaction() as conn:
        with pytest.raises(IntegrityError):
            await insert_cross_session_plan_step(conn, primary, foreign)
        with pytest.raises(IntegrityError):
            await insert_cross_version_dependency(conn, primary)


@pytest.mark.asyncio
async def test_migrated_sqlite_enforces_agent_plan_checks(database, seeded_scopes):
    primary, _foreign = seeded_scopes
    cases = (
        (
            "invalid trigger mode",
            "UPDATE agent_plans SET trigger_mode = 'manual' WHERE id = :plan_id",
            "ck_agent_plans_trigger_mode_valid",
        ),
        (
            "invalid status",
            "UPDATE agent_plans SET status = 'running' WHERE id = :plan_id",
            "ck_agent_plans_status_valid",
        ),
        (
            "non-positive current version",
            "UPDATE agent_plans SET current_version = 0 WHERE id = :plan_id",
            "ck_agent_plans_current_version_positive",
        ),
        (
            "approved version beyond current",
            "UPDATE agent_plans SET approved_version = 3 WHERE id = :plan_id",
            "ck_agent_plans_approved_version_valid",
        ),
    )

    async with database.write_transaction() as conn:
        for case, statement, constraint_name in cases:
            await assert_migrated_check_rejects(
                conn,
                case=case,
                statement=statement,
                parameters=primary,
                constraint_name=constraint_name,
            )


@pytest.mark.asyncio
async def test_migrated_sqlite_enforces_plan_version_checks(database, seeded_scopes):
    primary, _foreign = seeded_scopes
    digest = "c" * 64
    cases = (
        (
            "non-positive plan version",
            """
            INSERT INTO agent_plan_versions (
                tenant_id, workspace_id, session_id, plan_id, plan_version,
                objective, constraints_json, generation_reason, parent_version,
                revision_feedback, schema_version, content_digest, created_at
            ) VALUES (
                :tenant_id, :workspace_id, :session_id, :plan_id, 0,
                'Invalid', '{}', 'test', NULL, NULL, 1, :digest, 3
            )
            """,
            {**primary, "digest": digest},
            "ck_agent_plan_versions_plan_version_positive",
        ),
        (
            "non-positive schema version",
            """
            UPDATE agent_plan_versions SET schema_version = 0
            WHERE tenant_id = :tenant_id AND workspace_id = :workspace_id
              AND session_id = :session_id AND plan_id = :plan_id AND plan_version = 2
            """,
            primary,
            "ck_agent_plan_versions_schema_version_positive",
        ),
        (
            "parent is not earlier",
            """
            UPDATE agent_plan_versions SET parent_version = 2
            WHERE tenant_id = :tenant_id AND workspace_id = :workspace_id
              AND session_id = :session_id AND plan_id = :plan_id AND plan_version = 2
            """,
            primary,
            "ck_agent_plan_versions_parent_version_valid",
        ),
        (
            "invalid content digest length",
            """
            UPDATE agent_plan_versions SET content_digest = 'short'
            WHERE tenant_id = :tenant_id AND workspace_id = :workspace_id
              AND session_id = :session_id AND plan_id = :plan_id AND plan_version = 2
            """,
            primary,
            "ck_agent_plan_versions_content_digest_valid",
        ),
    )

    async with database.write_transaction() as conn:
        for case, statement, parameters, constraint_name in cases:
            await assert_migrated_check_rejects(
                conn,
                case=case,
                statement=statement,
                parameters=parameters,
                constraint_name=constraint_name,
            )


@pytest.mark.asyncio
async def test_migrated_sqlite_enforces_plan_step_checks(database, seeded_scopes):
    primary, _foreign = seeded_scopes
    cases = (
        ("ordinal below range", "ordinal", 0, "ck_agent_plan_steps_ordinal_valid"),
        ("ordinal above range", "ordinal", 21, "ck_agent_plan_steps_ordinal_valid"),
        ("attempts below range", "max_attempts", 0, "ck_agent_plan_steps_max_attempts_valid"),
        ("attempts above range", "max_attempts", 21, "ck_agent_plan_steps_max_attempts_valid"),
        (
            "invalid definition digest length",
            "definition_digest",
            "short",
            "ck_agent_plan_steps_definition_digest_valid",
        ),
    )

    async with database.write_transaction() as conn:
        for case, column_name, value, constraint_name in cases:
            await assert_migrated_check_rejects(
                conn,
                case=case,
                statement=(
                    f"UPDATE agent_plan_steps SET {column_name} = :invalid_value "
                    "WHERE step_id = :version_two_step_id"
                ),
                parameters={**primary, "invalid_value": value},
                constraint_name=constraint_name,
            )


@pytest.mark.asyncio
async def test_migrated_sqlite_rejects_self_dependency(database, seeded_scopes):
    primary, _foreign = seeded_scopes
    async with database.write_transaction() as conn:
        await assert_migrated_check_rejects(
            conn,
            case="step depends on itself",
            statement="""
                INSERT INTO agent_plan_step_dependencies (
                    tenant_id, workspace_id, session_id, plan_id, plan_version,
                    step_id, depends_on_step_id
                ) VALUES (
                    :tenant_id, :workspace_id, :session_id, :plan_id, 2,
                    :version_two_step_id, :version_two_step_id
                )
            """,
            parameters=primary,
            constraint_name="ck_agent_plan_step_dependencies_distinct_steps",
        )


@pytest.mark.asyncio
async def test_migrated_sqlite_enforces_plan_decision_checks(database, seeded_scopes):
    primary, _foreign = seeded_scopes
    statement = """
        INSERT INTO agent_plan_decisions (
            tenant_id, workspace_id, session_id, plan_id, decision_id,
            plan_version, expected_plan_cas_version, action, feedback,
            decided_by, resulting_plan_version, created_at
        ) VALUES (
            :tenant_id, :workspace_id, :session_id, :plan_id, :decision_id,
            :plan_version, :expected_plan_cas_version, :action, NULL,
            :tenant_id, NULL, 3
        )
    """
    cases = (
        (
            "invalid action",
            {"plan_version": 2, "expected_plan_cas_version": 1, "action": "archive"},
            "ck_agent_plan_decisions_action_valid",
        ),
        (
            "non-positive expected CAS version",
            {"plan_version": 2, "expected_plan_cas_version": 0, "action": "approve"},
            "ck_agent_plan_decisions_expected_cas_positive",
        ),
        (
            "non-positive plan version",
            {"plan_version": 0, "expected_plan_cas_version": 1, "action": "approve"},
            "ck_agent_plan_decisions_plan_version_positive",
        ),
    )

    async with database.write_transaction() as conn:
        for index, (case, values, constraint_name) in enumerate(cases, start=1):
            await assert_migrated_check_rejects(
                conn,
                case=case,
                statement=statement,
                parameters={
                    **primary,
                    **values,
                    "decision_id": f"00000000-0000-0000-0000-{index:012d}",
                },
                constraint_name=constraint_name,
            )


@pytest.mark.asyncio
async def test_migrated_sqlite_enforces_complete_run_plan_binding(database, seeded_scopes):
    primary, _foreign = seeded_scopes
    statement = """
        INSERT INTO agent_runs (
            run_id, tenant_id, workspace_id, session_id, plan_id,
            initial_plan_version, active_plan_version, run_status,
            fencing_token, schema_version, version, created_at, updated_at
        ) VALUES (
            :invalid_run_id, :tenant_id, :workspace_id, :session_id, :binding_plan_id,
            :initial_plan_version, :active_plan_version, 'running', 0, 1, 1, 2, 2
        )
    """
    partial_bindings = (
        (primary["plan_id"], None, None),
        (primary["plan_id"], 1, None),
        (primary["plan_id"], None, 2),
        (None, 1, 2),
    )

    async with database.write_transaction() as conn:
        for index, (plan_id, initial_version, active_version) in enumerate(
            partial_bindings,
            start=1,
        ):
            await assert_migrated_check_rejects(
                conn,
                case=f"partial run binding {index}",
                statement=statement,
                parameters={
                    **primary,
                    "invalid_run_id": f"00000000-0000-0000-0001-{index:012d}",
                    "binding_plan_id": plan_id,
                    "initial_plan_version": initial_version,
                    "active_plan_version": active_version,
                },
                constraint_name="ck_agent_runs_plan_binding_complete",
            )


@pytest.mark.asyncio
async def test_migrated_sqlite_enforces_plan_step_run_checks(database, seeded_scopes):
    primary, _foreign = seeded_scopes
    statement = """
        INSERT INTO agent_plan_step_runs (
            tenant_id, workspace_id, session_id, plan_id, plan_version,
            step_id, step_run_id, run_id, attempt, status, result_digest,
            version, started_at, finished_at
        ) VALUES (
            :tenant_id, :workspace_id, :session_id, :plan_id, 2,
            :version_two_step_id, :step_run_id, :run_id, :attempt, :status,
            :result_digest, :row_version, 10, :finished_at
        )
    """
    cases = (
        (
            "invalid status",
            {"attempt": 1, "status": "waiting", "result_digest": None, "row_version": 1, "finished_at": None},
            "ck_agent_plan_step_runs_status_valid",
        ),
        (
            "non-positive attempt",
            {"attempt": 0, "status": "pending", "result_digest": None, "row_version": 1, "finished_at": None},
            "ck_agent_plan_step_runs_attempt_positive",
        ),
        (
            "non-positive row version",
            {"attempt": 1, "status": "pending", "result_digest": None, "row_version": 0, "finished_at": None},
            "ck_agent_plan_step_runs_version_positive",
        ),
        (
            "invalid result digest length",
            {"attempt": 1, "status": "succeeded", "result_digest": "short", "row_version": 1, "finished_at": 10},
            "ck_agent_plan_step_runs_result_digest_valid",
        ),
        (
            "finish before start",
            {"attempt": 1, "status": "succeeded", "result_digest": "d" * 64, "row_version": 1, "finished_at": 9},
            "ck_agent_plan_step_runs_finished_at_valid",
        ),
    )

    async with database.write_transaction() as conn:
        for index, (case, values, constraint_name) in enumerate(cases, start=1):
            await assert_migrated_check_rejects(
                conn,
                case=case,
                statement=statement,
                parameters={
                    **primary,
                    **values,
                    "step_run_id": f"00000000-0000-0000-0002-{index:012d}",
                },
                constraint_name=constraint_name,
            )


@pytest.mark.asyncio
async def test_plan_schema_allows_legacy_direct_run(database, seeded_scopes):
    primary, _foreign = seeded_scopes
    async with database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO agent_runs (
                    run_id, tenant_id, workspace_id, session_id, run_status,
                    fencing_token, schema_version, version, created_at, updated_at
                ) VALUES (
                    '00000000-0000-0000-0000-000000000602', :tenant_id,
                    :workspace_id, :session_id, 'running', 0, 1, 1, 1, 1
                )
                """
            ),
            primary,
        )

    async with database.connect() as conn:
        binding = (
            await conn.execute(
                text(
                    """
                    SELECT plan_id, initial_plan_version, active_plan_version, cancel_requested_at
                    FROM agent_runs
                    WHERE run_id = '00000000-0000-0000-0000-000000000602'
                    """
                )
            )
        ).one()

    assert tuple(binding) == (None, None, None, None)


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
            tool_execution_columns = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns("tool_executions")
            )
            execution_checkpoint_columns = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns("execution_checkpoints")
            )
            agent_run_columns = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns("agent_runs")
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
        assert next(
            column for column in tool_execution_columns if column["name"] == "schema_version"
        )["default"] is None
        assert next(
            column for column in execution_checkpoint_columns if column["name"] == "schema_version"
        )["default"] is None
        assert next(
            column for column in agent_run_columns if column["name"] == "schema_version"
        )["default"] is not None
        assert next(
            column for column in agent_run_columns if column["name"] == "version"
        )["default"] is not None
        assert "CONSTRAINT fk_users_id_default_workspace_id_workspaces" in ddl
        assert "CONSTRAINT fk_tool_executions_tenant_id_workspace_id_session_id_run_id_agent_runs" in ddl
    finally:
        await database.dispose()
