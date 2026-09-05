"""durable plan engine

Revision ID: 20260905_0002
Revises: 20260815_0001
Create Date: 2026-09-05 00:02:00.000000
"""

from __future__ import annotations

from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "20260905_0002"
down_revision = "20260815_0001"
branch_labels = None
depends_on = None


def _table_kwargs() -> dict[str, Any]:
    if op.get_context().dialect.name == "mysql":
        return {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        }
    return {}


def _payload_type():
    return sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    state = "ON" if enabled else "OFF"
    op.get_bind().exec_driver_sql(f"PRAGMA foreign_keys={state}")


def _sqlite_foreign_keys_enabled() -> bool:
    return bool(op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar_one())


def _recover_sqlite_batch_table(table_name: str) -> None:
    temporary_name = f"_alembic_tmp_{table_name}"
    connection = op.get_bind()
    table_names = set(sa.inspect(connection).get_table_names())
    original_exists = table_name in table_names
    temporary_exists = temporary_name in table_names

    if not temporary_exists:
        return
    if not original_exists:
        op.rename_table(temporary_name, table_name)
        return

    preparer = connection.dialect.identifier_preparer
    original_count = connection.exec_driver_sql(
        f"SELECT count(*) FROM {preparer.quote(table_name)}"
    ).scalar_one()
    temporary_count = connection.exec_driver_sql(
        f"SELECT count(*) FROM {preparer.quote(temporary_name)}"
    ).scalar_one()
    if temporary_count > original_count:
        op.drop_table(table_name)
        op.rename_table(temporary_name, table_name)
    else:
        op.drop_table(temporary_name)


def _recover_sqlite_batch_tables() -> None:
    _recover_sqlite_batch_table("memory_entries")
    _recover_sqlite_batch_table("agent_runs")


def _add_memory_session_unique() -> None:
    constraint_name = "uq_memory_entries_tenant_id_workspace_id_session_id_id"
    unique_columns = ["tenant_id", "workspace_id", "session_id", "id"]
    if not op.get_context().as_sql:
        existing_uniques = sa.inspect(op.get_bind()).get_unique_constraints("memory_entries")
        if any(unique["column_names"] == unique_columns for unique in existing_uniques):
            return

    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table("memory_entries", recreate="always") as batch_op:
            batch_op.create_unique_constraint(
                constraint_name,
                unique_columns,
            )
        return

    op.create_unique_constraint(
        constraint_name,
        "memory_entries",
        unique_columns,
    )


def _add_agent_run_plan_binding() -> None:
    recreate: Literal["always", "auto"] = (
        "always" if op.get_context().dialect.name == "sqlite" else "auto"
    )
    with op.batch_alter_table("agent_runs", recreate=recreate) as batch_op:
        batch_op.add_column(sa.Column("plan_id", sa.CHAR(length=36), nullable=True))
        batch_op.add_column(sa.Column("initial_plan_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("active_plan_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("cancel_requested_at", sa.BigInteger(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_agent_runs_scope_plan_run",
            ["tenant_id", "workspace_id", "session_id", "plan_id", "run_id"],
        )
        batch_op.create_check_constraint(
            op.f("ck_agent_runs_plan_binding_complete"),
            (
                "(plan_id IS NULL AND initial_plan_version IS NULL AND active_plan_version IS NULL) OR "
                "(plan_id IS NOT NULL AND initial_plan_version IS NOT NULL "
                "AND active_plan_version IS NOT NULL)"
            ),
        )
        batch_op.create_foreign_key(
            "fk_agent_runs_initial_plan_agent_plan_versions",
            "agent_plan_versions",
            [
                "tenant_id",
                "workspace_id",
                "session_id",
                "plan_id",
                "initial_plan_version",
            ],
            ["tenant_id", "workspace_id", "session_id", "plan_id", "plan_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_agent_runs_active_plan_agent_plan_versions",
            "agent_plan_versions",
            [
                "tenant_id",
                "workspace_id",
                "session_id",
                "plan_id",
                "active_plan_version",
            ],
            ["tenant_id", "workspace_id", "session_id", "plan_id", "plan_version"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )


def _create_indexes() -> None:
    op.create_index(
        "ix_agent_plans_scope_created_at",
        "agent_plans",
        ["tenant_id", "workspace_id", "session_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_plan_steps_scope_version_ordinal",
        "agent_plan_steps",
        ["tenant_id", "workspace_id", "session_id", "plan_id", "plan_version", "ordinal"],
        unique=False,
    )
    op.create_index(
        "ix_agent_plan_decisions_scope_created_at",
        "agent_plan_decisions",
        ["tenant_id", "workspace_id", "session_id", "plan_id", "created_at"],
        unique=False,
    )
def _upgrade_schema() -> None:
    _add_memory_session_unique()

    op.create_table(
        "agent_plans",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("source_message_id", sa.CHAR(length=36), nullable=False),
        sa.Column("trigger_mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("approved_version", sa.Integer(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "trigger_mode IN ('automatic', 'explicit')",
            name=sa.schema.conv("ck_agent_plans_trigger_mode_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_approval', 'approved', 'rejected', 'archived')",
            name=sa.schema.conv("ck_agent_plans_status_valid"),
        ),
        sa.CheckConstraint(
            "current_version >= 1",
            name=sa.schema.conv("ck_agent_plans_current_version_positive"),
        ),
        sa.CheckConstraint(
            "approved_version IS NULL OR approved_version <= current_version",
            name=sa.schema.conv("ck_agent_plans_approved_version_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id"],
            ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
            name="fk_agent_plans_tenant_id_workspace_id_session_id_chat_sessions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "source_message_id"],
            [
                "memory_entries.tenant_id",
                "memory_entries.workspace_id",
                "memory_entries.session_id",
                "memory_entries.id",
            ],
            name="fk_agent_plans_source_message_memory_entries",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_plans"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_plans_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "id",
            name="uq_agent_plans_tenant_id_workspace_id_session_id_id",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "agent_plan_versions",
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("plan_id", sa.CHAR(length=36), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("objective", _payload_type(), nullable=False),
        sa.Column("constraints_json", _payload_type(), nullable=False),
        sa.Column("generation_reason", _payload_type(), nullable=False),
        sa.Column("parent_version", sa.Integer(), nullable=True),
        sa.Column("revision_feedback", _payload_type(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.CHAR(length=64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "plan_version >= 1",
            name=sa.schema.conv("ck_agent_plan_versions_plan_version_positive"),
        ),
        sa.CheckConstraint(
            "schema_version >= 1",
            name=sa.schema.conv("ck_agent_plan_versions_schema_version_positive"),
        ),
        sa.CheckConstraint(
            "length(content_digest) = 64",
            name=sa.schema.conv("ck_agent_plan_versions_content_digest_valid"),
        ),
        sa.CheckConstraint(
            "parent_version IS NULL OR parent_version < plan_version",
            name=sa.schema.conv("ck_agent_plan_versions_parent_version_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "plan_id"],
            [
                "agent_plans.tenant_id",
                "agent_plans.workspace_id",
                "agent_plans.session_id",
                "agent_plans.id",
            ],
            name="fk_agent_plan_versions_plan_agent_plans",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "plan_id", "parent_version"],
            [
                "agent_plan_versions.tenant_id",
                "agent_plan_versions.workspace_id",
                "agent_plan_versions.session_id",
                "agent_plan_versions.plan_id",
                "agent_plan_versions.plan_version",
            ],
            name="fk_agent_plan_versions_parent_agent_plan_versions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "plan_version",
            name="pk_agent_plan_versions",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "agent_plan_steps",
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("plan_id", sa.CHAR(length=36), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.CHAR(length=36), nullable=False),
        sa.Column("logical_step_key", sa.String(length=64), nullable=False),
        sa.Column("supersedes_step_id", sa.CHAR(length=36), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", _payload_type(), nullable=False),
        sa.Column("expected_outcome", _payload_type(), nullable=False),
        sa.Column("assigned_agent_profile_id", sa.CHAR(length=36), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("definition_digest", sa.CHAR(length=64), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 1 AND ordinal <= 20",
            name=sa.schema.conv("ck_agent_plan_steps_ordinal_valid"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 20",
            name=sa.schema.conv("ck_agent_plan_steps_max_attempts_valid"),
        ),
        sa.CheckConstraint(
            "length(definition_digest) = 64",
            name=sa.schema.conv("ck_agent_plan_steps_definition_digest_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "plan_id", "plan_version"],
            [
                "agent_plan_versions.tenant_id",
                "agent_plan_versions.workspace_id",
                "agent_plan_versions.session_id",
                "agent_plan_versions.plan_id",
                "agent_plan_versions.plan_version",
            ],
            name="fk_agent_plan_steps_version_agent_plan_versions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "plan_id", "supersedes_step_id"],
            [
                "agent_plan_steps.tenant_id",
                "agent_plan_steps.workspace_id",
                "agent_plan_steps.session_id",
                "agent_plan_steps.plan_id",
                "agent_plan_steps.step_id",
            ],
            name="fk_agent_plan_steps_supersedes_agent_plan_steps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("step_id", name="pk_agent_plan_steps"),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "plan_version",
            "step_id",
            name="uq_agent_plan_steps_scope_version_step",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "plan_version",
            "logical_step_key",
            name="uq_agent_plan_steps_scope_version_logical_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "step_id",
            name="uq_agent_plan_steps_scope_plan_step",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "agent_plan_step_dependencies",
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("plan_id", sa.CHAR(length=36), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.CHAR(length=36), nullable=False),
        sa.Column("depends_on_step_id", sa.CHAR(length=36), nullable=False),
        sa.CheckConstraint(
            "step_id <> depends_on_step_id",
            name=sa.schema.conv("ck_agent_plan_step_dependencies_distinct_steps"),
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "workspace_id",
                "session_id",
                "plan_id",
                "plan_version",
                "step_id",
            ],
            [
                "agent_plan_steps.tenant_id",
                "agent_plan_steps.workspace_id",
                "agent_plan_steps.session_id",
                "agent_plan_steps.plan_id",
                "agent_plan_steps.plan_version",
                "agent_plan_steps.step_id",
            ],
            name="fk_agent_plan_step_dependencies_step_agent_plan_steps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "workspace_id",
                "session_id",
                "plan_id",
                "plan_version",
                "depends_on_step_id",
            ],
            [
                "agent_plan_steps.tenant_id",
                "agent_plan_steps.workspace_id",
                "agent_plan_steps.session_id",
                "agent_plan_steps.plan_id",
                "agent_plan_steps.plan_version",
                "agent_plan_steps.step_id",
            ],
            name="fk_agent_plan_step_dependencies_source_agent_plan_steps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "plan_version",
            "step_id",
            "depends_on_step_id",
            name="pk_agent_plan_step_dependencies",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "agent_plan_decisions",
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("plan_id", sa.CHAR(length=36), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("expected_plan_cas_version", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("feedback", _payload_type(), nullable=True),
        sa.Column("decided_by", sa.CHAR(length=36), nullable=False),
        sa.Column("resulting_plan_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "plan_version >= 1",
            name=sa.schema.conv("ck_agent_plan_decisions_plan_version_positive"),
        ),
        sa.CheckConstraint(
            "expected_plan_cas_version >= 1",
            name=sa.schema.conv("ck_agent_plan_decisions_expected_cas_positive"),
        ),
        sa.CheckConstraint(
            "action IN ('approve', 'reject', 'revise')",
            name=sa.schema.conv("ck_agent_plan_decisions_action_valid"),
        ),
        sa.CheckConstraint(
            "length(decision_id) BETWEEN 1 AND 128",
            name=sa.schema.conv("ck_agent_plan_decisions_decision_id_length"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "plan_id", "plan_version"],
            [
                "agent_plan_versions.tenant_id",
                "agent_plan_versions.workspace_id",
                "agent_plan_versions.session_id",
                "agent_plan_versions.plan_id",
                "agent_plan_versions.plan_version",
            ],
            name="fk_agent_plan_decisions_version_agent_plan_versions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "workspace_id",
                "session_id",
                "plan_id",
                "resulting_plan_version",
            ],
            [
                "agent_plan_versions.tenant_id",
                "agent_plan_versions.workspace_id",
                "agent_plan_versions.session_id",
                "agent_plan_versions.plan_id",
                "agent_plan_versions.plan_version",
            ],
            name="fk_agent_plan_decisions_result_agent_plan_versions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "decision_id",
            name="pk_agent_plan_decisions",
        ),
        **_table_kwargs(),
    )

    _add_agent_run_plan_binding()

    op.create_table(
        "agent_plan_step_runs",
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("plan_id", sa.CHAR(length=36), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.CHAR(length=36), nullable=False),
        sa.Column("step_run_id", sa.CHAR(length=36), nullable=False),
        sa.Column("run_id", sa.CHAR(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_summary", _payload_type(), nullable=True),
        sa.Column("result_ref", sa.String(length=128), nullable=True),
        sa.Column("result_digest", sa.CHAR(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail_redacted", _payload_type(), nullable=True),
        sa.Column("reused_from_step_run_id", sa.CHAR(length=36), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            (
                "status IN ('pending', 'running', 'succeeded', 'failed_retryable', "
                "'failed_terminal', 'cancelled')"
            ),
            name=sa.schema.conv("ck_agent_plan_step_runs_status_valid"),
        ),
        sa.CheckConstraint(
            "attempt >= 1",
            name=sa.schema.conv("ck_agent_plan_step_runs_attempt_positive"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=sa.schema.conv("ck_agent_plan_step_runs_version_positive"),
        ),
        sa.CheckConstraint(
            "result_digest IS NULL OR length(result_digest) = 64",
            name=sa.schema.conv("ck_agent_plan_step_runs_result_digest_valid"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=sa.schema.conv("ck_agent_plan_step_runs_finished_at_valid"),
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "workspace_id",
                "session_id",
                "plan_id",
                "plan_version",
                "step_id",
            ],
            [
                "agent_plan_steps.tenant_id",
                "agent_plan_steps.workspace_id",
                "agent_plan_steps.session_id",
                "agent_plan_steps.plan_id",
                "agent_plan_steps.plan_version",
                "agent_plan_steps.step_id",
            ],
            name="fk_agent_plan_step_runs_step_agent_plan_steps",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "plan_id", "run_id"],
            [
                "agent_runs.tenant_id",
                "agent_runs.workspace_id",
                "agent_runs.session_id",
                "agent_runs.plan_id",
                "agent_runs.run_id",
            ],
            name="fk_agent_plan_step_runs_run_agent_runs",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "workspace_id",
                "session_id",
                "plan_id",
                "run_id",
                "reused_from_step_run_id",
            ],
            [
                "agent_plan_step_runs.tenant_id",
                "agent_plan_step_runs.workspace_id",
                "agent_plan_step_runs.session_id",
                "agent_plan_step_runs.plan_id",
                "agent_plan_step_runs.run_id",
                "agent_plan_step_runs.step_run_id",
            ],
            name="fk_agent_plan_step_runs_reuse_agent_plan_step_runs",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "plan_version",
            "step_id",
            "step_run_id",
            name="pk_agent_plan_step_runs",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "run_id",
            "step_id",
            "attempt",
            name="uq_agent_plan_step_runs_scope_run_step_attempt",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "run_id",
            "step_run_id",
            name="uq_agent_plan_step_runs_scope_plan_run_step_run",
        ),
        **_table_kwargs(),
    )

    _create_indexes()


def upgrade() -> None:
    if op.get_context().dialect.name != "sqlite":
        _upgrade_schema()
        return

    original_foreign_keys = _sqlite_foreign_keys_enabled()
    with op.get_context().autocommit_block():
        _set_sqlite_foreign_keys(enabled=False)
        try:
            _recover_sqlite_batch_tables()
            _upgrade_schema()
        except BaseException:
            _recover_sqlite_batch_tables()
            raise
        finally:
            _set_sqlite_foreign_keys(enabled=original_foreign_keys)


def downgrade() -> None:
    raise RuntimeError("MultiClaw migrations are forward-only")
