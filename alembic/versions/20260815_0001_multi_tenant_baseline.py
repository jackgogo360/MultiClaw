"""multi tenant baseline

Revision ID: 20260815_0001
Revises:
Create Date: 2026-08-15 00:01:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "20260815_0001"
down_revision = None
branch_labels = None
depends_on = None


def _table_kwargs() -> dict[str, str]:
    if op.get_context().dialect.name == "mysql":
        return {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        }
    return {}


def _payload_type():
    return sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def _add_users_default_workspace_fk() -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table("users", recreate="always") as batch_op:
            batch_op.create_foreign_key(
                "fk_users_id_default_workspace_id_workspaces",
                "workspaces",
                ["id", "default_workspace_id"],
                ["tenant_id", "id"],
                ondelete="RESTRICT",
                onupdate="RESTRICT",
            )
        return

    op.create_foreign_key(
        "fk_users_id_default_workspace_id_workspaces",
        "users",
        "workspaces",
        ["id", "default_workspace_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    )


def _create_indexes() -> None:
    op.create_index("ix_workspaces_tenant_id", "workspaces", ["tenant_id"], unique=False)
    op.create_index(
        "ix_chat_sessions_tenant_id_workspace_id",
        "chat_sessions",
        ["tenant_id", "workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_entries_tenant_id_workspace_id_session_id",
        "memory_entries",
        ["tenant_id", "workspace_id", "session_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_tenant_id_workspace_id_session_id",
        "agent_runs",
        ["tenant_id", "workspace_id", "session_id"],
        unique=False,
    )
    op.create_index(
        "ix_approval_requests_tenant_id_workspace_id_session_id_run_id",
        "approval_requests",
        ["tenant_id", "workspace_id", "session_id", "run_id"],
        unique=False,
    )
    op.create_index(
        "ix_tool_executions_tenant_id_workspace_id_session_id_run_id",
        "tool_executions",
        ["tenant_id", "workspace_id", "session_id", "run_id"],
        unique=False,
    )
    op.create_index(
        "ix_tool_executions_tenant_id_workspace_id_session_id_run_id_approval_id",
        "tool_executions",
        ["tenant_id", "workspace_id", "session_id", "run_id", "approval_id"],
        unique=False,
    )
    op.create_index(
        "ix_execution_checkpoints_tenant_id_workspace_id_session_id_run_id",
        "execution_checkpoints",
        ["tenant_id", "workspace_id", "session_id", "run_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_secrets_tenant_id_workspace_id",
        "user_secrets",
        ["tenant_id", "workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_tenant_id_workspace_id_session_id_run_id",
        "audit_logs",
        ["tenant_id", "workspace_id", "session_id", "run_id"],
        unique=False,
    )
    op.create_index("ix_deletion_jobs_tenant_id", "deletion_jobs", ["tenant_id"], unique=False)
    op.create_index(
        "ix_verification_codes_email_purpose_expires_at",
        "verification_codes",
        ["email", "purpose", "expires_at"],
        unique=False,
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("auth_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("default_workspace_id", sa.CHAR(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("purge_after", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("disabled_at", sa.BigInteger(), nullable=True),
        sa.Column("purge_requested_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'pending_purge')",
            name="ck_users_users_status_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("id", "default_workspace_id", name="uq_users_id_default_workspace_id"),
        **_table_kwargs(),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'pending_purge')",
            name="ck_workspaces_workspaces_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["users.id"],
            name="fk_workspaces_tenant_id_users",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_workspaces_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_workspaces_tenant_id_slug"),
        **_table_kwargs(),
    )

    _add_users_default_workspace_fk()

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("last_message_at", sa.BigInteger(), nullable=True),
        sa.Column("metadata_json", _payload_type(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_chat_sessions_tenant_id_workspace_id_workspaces",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_sessions"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_chat_sessions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "id",
            name="uq_chat_sessions_tenant_id_workspace_id_id",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "memory_entries",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=True),
        sa.Column("content", _payload_type(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", _payload_type(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_memory_entries_tenant_id_workspace_id_workspaces",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id"],
            ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
            name="fk_memory_entries_tenant_id_workspace_id_session_id_chat_sessions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_entries"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_memory_entries_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "id",
            name="uq_memory_entries_tenant_id_workspace_id_id",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("run_status", sa.String(length=32), nullable=False),
        sa.Column("runtime_instance_id", sa.String(length=128), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lease_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("heartbeat_at", sa.BigInteger(), nullable=True),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            (
                "run_status IN ("
                "'running', 'awaiting_user', 'resuming', 'completed', 'failed_terminal', "
                "'blocked_incompatible', 'blocked_corrupt', 'cancelled'"
                ")"
            ),
            name="ck_agent_runs_agent_runs_run_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id"],
            ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
            name="fk_agent_runs_tenant_id_workspace_id_session_id_chat_sessions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_agent_runs"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_agent_runs_tenant_id_run_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "run_id",
            name="uq_agent_runs_tenant_id_workspace_id_session_id_run_id",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "approval_requests",
        sa.Column("approval_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("run_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("approval_status", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.BigInteger(), nullable=False),
        sa.Column("resolved_at", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "approval_status IN ('awaiting_user', 'approved', 'rejected', 'expired')",
            name="ck_approval_requests_approval_requests_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"],
            name="fk_approval_requests_tenant_id_workspace_id_session_id_run_id_agent_runs",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("approval_id", name="pk_approval_requests"),
        sa.UniqueConstraint("tenant_id", "approval_id", name="uq_approval_requests_tenant_id_approval_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "run_id",
            "approval_id",
            name="uq_approval_requests_tenant_id_workspace_id_session_id_run_id_approval_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "run_id",
            "tool_call_id",
            name="uq_approval_requests_tenant_id_workspace_id_session_id_run_id_tool_call_id",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "tool_executions",
        sa.Column("execution_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("run_id", sa.CHAR(length=36), nullable=False),
        sa.Column("approval_id", sa.CHAR(length=36), nullable=True),
        sa.Column("tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("tool_kind", sa.String(length=64), nullable=False),
        sa.Column("execution_status", sa.String(length=32), nullable=False),
        sa.Column("recovery_strategy", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("input_payload_json", _payload_type(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("external_request_id", sa.String(length=255), nullable=True),
        sa.Column("result_ref", sa.String(length=255), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            (
                "execution_status IN ("
                "'not_started', 'replaying', 'executing', 'succeeded', 'failed_retryable', "
                "'failed_terminal', 'uncertain', 'blocked_incompatible', 'blocked_corrupt'"
                ")"
            ),
            name="ck_tool_executions_tool_executions_status_valid",
        ),
        sa.CheckConstraint(
            "recovery_strategy IN ('read_only_replay', 'idempotent_retry', 'manual_uncertain')",
            name="ck_tool_executions_tool_executions_recovery_strategy_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"],
            name="fk_tool_executions_tenant_id_workspace_id_session_id_run_id_agent_runs",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "run_id", "approval_id"],
            [
                "approval_requests.tenant_id",
                "approval_requests.workspace_id",
                "approval_requests.session_id",
                "approval_requests.run_id",
                "approval_requests.approval_id",
            ],
            name="fk_tool_executions_tenant_id_workspace_id_session_id_run_id_approval_id_approval_requests",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("execution_id", name="pk_tool_executions"),
        sa.UniqueConstraint("tenant_id", "execution_id", name="uq_tool_executions_tenant_id_execution_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "run_id",
            "execution_id",
            name="uq_tool_executions_tenant_id_workspace_id_session_id_run_id_execution_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "run_id",
            "tool_call_id",
            name="uq_tool_executions_tenant_id_workspace_id_session_id_run_id_tool_call_id",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "execution_checkpoints",
        sa.Column("checkpoint_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=False),
        sa.Column("run_id", sa.CHAR(length=36), nullable=False),
        sa.Column("approval_id", sa.CHAR(length=36), nullable=True),
        sa.Column("execution_id", sa.CHAR(length=36), nullable=True),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_seq", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", _payload_type(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"],
            name="fk_execution_checkpoints_tenant_id_workspace_id_session_id_run_id_agent_runs",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "run_id", "approval_id"],
            [
                "approval_requests.tenant_id",
                "approval_requests.workspace_id",
                "approval_requests.session_id",
                "approval_requests.run_id",
                "approval_requests.approval_id",
            ],
            name="fk_execution_checkpoints_tenant_id_workspace_id_session_id_run_id_approval_id_approval_requests",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "run_id", "execution_id"],
            [
                "tool_executions.tenant_id",
                "tool_executions.workspace_id",
                "tool_executions.session_id",
                "tool_executions.run_id",
                "tool_executions.execution_id",
            ],
            name="fk_execution_checkpoints_tenant_id_workspace_id_session_id_run_id_execution_id_tool_executions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("checkpoint_id", name="pk_execution_checkpoints"),
        sa.UniqueConstraint(
            "tenant_id",
            "checkpoint_id",
            name="uq_execution_checkpoints_tenant_id_checkpoint_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "session_id",
            "run_id",
            "checkpoint_seq",
            name="uq_execution_checkpoints_tenant_id_workspace_id_session_id_run_id_checkpoint_seq",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "user_secrets",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=True),
        sa.Column("provider_kind", sa.String(length=32), nullable=False),
        sa.Column("provider_name", sa.String(length=128), nullable=False),
        sa.Column("secret_name", sa.String(length=128), nullable=False),
        sa.Column("key_provider_name", sa.String(length=128), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("rotated_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "key_provider_name = 'deployment-keyring'",
            name="ck_user_secrets_user_secrets_key_provider_name_fixed",
        ),
        sa.CheckConstraint(
            "format_version = 1",
            name="ck_user_secrets_user_secrets_format_version_fixed",
        ),
        sa.CheckConstraint(
            "algorithm = 'AES-256-GCM'",
            name="ck_user_secrets_user_secrets_algorithm_fixed",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["users.id"],
            name="fk_user_secrets_tenant_id_users",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_user_secrets_tenant_id_workspace_id_workspaces",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_secrets"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider_kind",
            "provider_name",
            "secret_name",
            name="uq_user_secrets_tenant_id_provider_kind_provider_name_secret_name",
        ),
        sa.UniqueConstraint(
            "key_provider_name",
            "key_version",
            "nonce",
            name="uq_user_secrets_key_provider_name_key_version_nonce",
        ),
        **_table_kwargs(),
    )

    op.create_table(
        "audit_logs",
        sa.Column("audit_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("workspace_id", sa.CHAR(length=36), nullable=False),
        sa.Column("session_id", sa.CHAR(length=36), nullable=True),
        sa.Column("run_id", sa.CHAR(length=36), nullable=True),
        sa.Column("approval_id", sa.CHAR(length=36), nullable=True),
        sa.Column("execution_id", sa.CHAR(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("detail_redacted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspaces.tenant_id", "workspaces.id"],
            name="fk_audit_logs_tenant_id_workspace_id_workspaces",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id"],
            ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
            name="fk_audit_logs_tenant_id_workspace_id_session_id_chat_sessions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"],
            name="fk_audit_logs_tenant_id_workspace_id_session_id_run_id_agent_runs",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "run_id", "approval_id"],
            [
                "approval_requests.tenant_id",
                "approval_requests.workspace_id",
                "approval_requests.session_id",
                "approval_requests.run_id",
                "approval_requests.approval_id",
            ],
            name="fk_audit_logs_tenant_id_workspace_id_session_id_run_id_approval_id_approval_requests",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "run_id", "execution_id"],
            [
                "tool_executions.tenant_id",
                "tool_executions.workspace_id",
                "tool_executions.session_id",
                "tool_executions.run_id",
                "tool_executions.execution_id",
            ],
            name="fk_audit_logs_tenant_id_workspace_id_session_id_run_id_execution_id_tool_executions",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("audit_id", name="pk_audit_logs"),
        sa.UniqueConstraint("tenant_id", "audit_id", name="uq_audit_logs_tenant_id_audit_id"),
        **_table_kwargs(),
    )

    op.create_table(
        "deletion_jobs",
        sa.Column("job_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("purge_after", sa.BigInteger(), nullable=False),
        sa.Column("requested_at", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("heartbeat_at", sa.BigInteger(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('scheduled', 'running')",
            name="ck_deletion_jobs_deletion_jobs_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["users.id"],
            name="fk_deletion_jobs_tenant_id_users",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_id", name="pk_deletion_jobs"),
        sa.UniqueConstraint("tenant_id", "job_id", name="uq_deletion_jobs_tenant_id_job_id"),
        **_table_kwargs(),
    )

    op.create_table(
        "verification_codes",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("code_digest", sa.String(length=128), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("used_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('login', 'deletion_recovery')",
            name="ck_verification_codes_verification_codes_purpose_valid",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_verification_codes"),
        **_table_kwargs(),
    )

    _create_indexes()


def downgrade() -> None:
    raise RuntimeError("MultiClaw migrations are forward-only")
