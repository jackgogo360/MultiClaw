from sqlalchemy import (
    BIGINT,
    CHAR,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.sql.naming import conv


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

PAYLOAD_TEXT = Text().with_variant(MEDIUMTEXT(), "mysql")
UUID_CHAR = CHAR(36)


users = Table(
    "users",
    metadata,
    Column("id", UUID_CHAR, primary_key=True),
    Column("email", String(320), nullable=False),
    Column("auth_epoch", BIGINT, nullable=False, server_default="0"),
    Column("default_workspace_id", UUID_CHAR, nullable=True),
    Column("status", String(32), nullable=False),
    Column("purge_after", BIGINT, nullable=True),
    Column("created_at", BIGINT, nullable=False),
    Column("updated_at", BIGINT, nullable=False),
    Column("disabled_at", BIGINT, nullable=True),
    Column("purge_requested_at", BIGINT, nullable=True),
    UniqueConstraint("email"),
    UniqueConstraint("id", "default_workspace_id"),
    CheckConstraint(
        "status IN ('active', 'disabled', 'pending_purge')",
        name=conv("ck_users_users_status_valid"),
    ),
    ForeignKeyConstraint(
        ["id", "default_workspace_id"],
        ["workspaces.tenant_id", "workspaces.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


workspaces = Table(
    "workspaces",
    metadata,
    Column("id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("slug", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", BIGINT, nullable=False),
    Column("updated_at", BIGINT, nullable=False),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "slug"),
    CheckConstraint(
        "status IN ('active', 'disabled', 'pending_purge')",
        name=conv("ck_workspaces_workspaces_status_valid"),
    ),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["users.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


chat_sessions = Table(
    "chat_sessions",
    metadata,
    Column("id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("title", String(255), nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", BIGINT, nullable=False),
    Column("updated_at", BIGINT, nullable=False),
    Column("last_message_at", BIGINT, nullable=True),
    Column("metadata_json", PAYLOAD_TEXT, nullable=False),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "workspace_id", "id"),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id"],
        ["workspaces.tenant_id", "workspaces.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


memory_entries = Table(
    "memory_entries",
    metadata,
    Column("id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=True),
    Column("content", PAYLOAD_TEXT, nullable=False),
    Column("type", String(64), nullable=False),
    Column("role", String(32), nullable=False),
    Column("turn_index", Integer, nullable=False),
    Column("created_at", BIGINT, nullable=False),
    Column("metadata_json", PAYLOAD_TEXT, nullable=False),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "workspace_id", "id"),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id"],
        ["workspaces.tenant_id", "workspaces.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id"],
        ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


agent_runs = Table(
    "agent_runs",
    metadata,
    Column("run_id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=False),
    Column("run_status", String(32), nullable=False),
    Column("runtime_instance_id", String(128), nullable=True),
    Column("lease_owner", String(128), nullable=True),
    Column("fencing_token", BIGINT, nullable=False, server_default="0"),
    Column("lease_expires_at", BIGINT, nullable=True),
    Column("heartbeat_at", BIGINT, nullable=True),
    Column("schema_version", Integer, nullable=False, server_default="1"),
    Column("version", BIGINT, nullable=False, server_default="1"),
    Column("created_at", BIGINT, nullable=False),
    Column("updated_at", BIGINT, nullable=False),
    Column("finished_at", BIGINT, nullable=True),
    UniqueConstraint("tenant_id", "run_id"),
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "run_id"),
    CheckConstraint(
        (
            "run_status IN ("
            "'running', 'awaiting_user', 'resuming', 'completed', 'failed_terminal', "
            "'blocked_incompatible', 'blocked_corrupt', 'cancelled'"
            ")"
        ),
        name=conv("ck_agent_runs_agent_runs_run_status_valid"),
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id"],
        ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


approval_requests = Table(
    "approval_requests",
    metadata,
    Column("approval_id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=False),
    Column("run_id", UUID_CHAR, nullable=False),
    Column("tool_call_id", String(128), nullable=False),
    Column("approval_status", String(32), nullable=False),
    Column("requested_at", BIGINT, nullable=False),
    Column("resolved_at", BIGINT, nullable=True),
    Column("expires_at", BIGINT, nullable=False),
    Column("version", BIGINT, nullable=False),
    UniqueConstraint("tenant_id", "approval_id"),
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "run_id", "approval_id"),
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "run_id", "tool_call_id"),
    CheckConstraint(
        "approval_status IN ('awaiting_user', 'approved', 'rejected', 'expired')",
        name=conv("ck_approval_requests_approval_requests_status_valid"),
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "run_id"],
        ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


tool_executions = Table(
    "tool_executions",
    metadata,
    Column("execution_id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=False),
    Column("run_id", UUID_CHAR, nullable=False),
    Column("approval_id", UUID_CHAR, nullable=True),
    Column("tool_call_id", String(128), nullable=False),
    Column("tool_name", String(128), nullable=False),
    Column("tool_kind", String(64), nullable=False),
    Column("execution_status", String(32), nullable=False),
    Column("recovery_strategy", String(32), nullable=False),
    Column("idempotency_key", String(128), nullable=True),
    Column("input_payload_json", PAYLOAD_TEXT, nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("external_request_id", String(255), nullable=True),
    Column("result_ref", String(255), nullable=True),
    Column("result_digest", String(64), nullable=True),
    Column("schema_version", Integer, nullable=False),
    Column("version", BIGINT, nullable=False),
    Column("created_at", BIGINT, nullable=False),
    Column("updated_at", BIGINT, nullable=False),
    Column("finished_at", BIGINT, nullable=True),
    UniqueConstraint("tenant_id", "execution_id"),
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "run_id", "execution_id"),
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "run_id", "tool_call_id"),
    CheckConstraint(
        (
            "execution_status IN ("
            "'not_started', 'replaying', 'executing', 'succeeded', 'failed_retryable', "
            "'failed_terminal', 'uncertain', 'blocked_incompatible', 'blocked_corrupt'"
            ")"
        ),
        name=conv("ck_tool_executions_tool_executions_status_valid"),
    ),
    CheckConstraint(
        "recovery_strategy IN ('read_only_replay', 'idempotent_retry', 'manual_uncertain')",
        name=conv("ck_tool_executions_tool_executions_recovery_strategy_valid"),
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "run_id"],
        ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "run_id", "approval_id"],
        [
            "approval_requests.tenant_id",
            "approval_requests.workspace_id",
            "approval_requests.session_id",
            "approval_requests.run_id",
            "approval_requests.approval_id",
        ],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


execution_checkpoints = Table(
    "execution_checkpoints",
    metadata,
    Column("checkpoint_id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=False),
    Column("run_id", UUID_CHAR, nullable=False),
    Column("approval_id", UUID_CHAR, nullable=True),
    Column("execution_id", UUID_CHAR, nullable=True),
    Column("phase", String(64), nullable=False),
    Column("checkpoint_seq", BIGINT, nullable=False),
    Column("payload_json", PAYLOAD_TEXT, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("created_at", BIGINT, nullable=False),
    UniqueConstraint("tenant_id", "checkpoint_id"),
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "run_id", "checkpoint_seq"),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "run_id"],
        ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "run_id", "approval_id"],
        [
            "approval_requests.tenant_id",
            "approval_requests.workspace_id",
            "approval_requests.session_id",
            "approval_requests.run_id",
            "approval_requests.approval_id",
        ],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "run_id", "execution_id"],
        [
            "tool_executions.tenant_id",
            "tool_executions.workspace_id",
            "tool_executions.session_id",
            "tool_executions.run_id",
            "tool_executions.execution_id",
        ],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


user_secrets = Table(
    "user_secrets",
    metadata,
    Column("id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=True),
    Column("provider_kind", String(32), nullable=False),
    Column("provider_name", String(128), nullable=False),
    Column("secret_name", String(128), nullable=False),
    Column("key_provider_name", String(128), nullable=False),
    Column("format_version", Integer, nullable=False),
    Column("algorithm", String(32), nullable=False),
    Column("key_version", Integer, nullable=False),
    Column("nonce", LargeBinary(12), nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("created_at", BIGINT, nullable=False),
    Column("updated_at", BIGINT, nullable=False),
    Column("rotated_at", BIGINT, nullable=True),
    UniqueConstraint("tenant_id", "provider_kind", "provider_name", "secret_name"),
    UniqueConstraint("key_provider_name", "key_version", "nonce"),
    CheckConstraint(
        "key_provider_name = 'deployment-keyring'",
        name=conv("ck_user_secrets_user_secrets_key_provider_name_fixed"),
    ),
    CheckConstraint(
        "format_version = 1",
        name=conv("ck_user_secrets_user_secrets_format_version_fixed"),
    ),
    CheckConstraint(
        "algorithm = 'AES-256-GCM'",
        name=conv("ck_user_secrets_user_secrets_algorithm_fixed"),
    ),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["users.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id"],
        ["workspaces.tenant_id", "workspaces.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


audit_logs = Table(
    "audit_logs",
    metadata,
    Column("audit_id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=True),
    Column("run_id", UUID_CHAR, nullable=True),
    Column("approval_id", UUID_CHAR, nullable=True),
    Column("execution_id", UUID_CHAR, nullable=True),
    Column("event_type", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("tool_name", String(128), nullable=True),
    Column("detail_redacted", Text, nullable=False),
    Column("created_at", BIGINT, nullable=False),
    UniqueConstraint("tenant_id", "audit_id"),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id"],
        ["workspaces.tenant_id", "workspaces.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id"],
        ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "run_id"],
        ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "run_id", "approval_id"],
        [
            "approval_requests.tenant_id",
            "approval_requests.workspace_id",
            "approval_requests.session_id",
            "approval_requests.run_id",
            "approval_requests.approval_id",
        ],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "run_id", "execution_id"],
        [
            "tool_executions.tenant_id",
            "tool_executions.workspace_id",
            "tool_executions.session_id",
            "tool_executions.run_id",
            "tool_executions.execution_id",
        ],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


deletion_jobs = Table(
    "deletion_jobs",
    metadata,
    Column("job_id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("status", String(32), nullable=False),
    Column("purge_after", BIGINT, nullable=False),
    Column("requested_at", BIGINT, nullable=False),
    Column("started_at", BIGINT, nullable=True),
    Column("worker_id", String(128), nullable=True),
    Column("lease_expires_at", BIGINT, nullable=True),
    Column("heartbeat_at", BIGINT, nullable=True),
    Column("fencing_token", BIGINT, nullable=False, server_default="0"),
    Column("version", BIGINT, nullable=False, server_default="0"),
    Column("attempt_count", Integer, nullable=False),
    Column("last_error", Text, nullable=True),
    UniqueConstraint("tenant_id", "job_id"),
    CheckConstraint(
        "status IN ('scheduled', 'running')",
        name=conv("ck_deletion_jobs_deletion_jobs_status_valid"),
    ),
    ForeignKeyConstraint(
        ["tenant_id"],
        ["users.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


verification_codes = Table(
    "verification_codes",
    metadata,
    Column("id", UUID_CHAR, primary_key=True),
    Column("email", String(320), nullable=False),
    Column("code_digest", String(128), nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("expires_at", BIGINT, nullable=False),
    Column("used_at", BIGINT, nullable=True),
    Column("created_at", BIGINT, nullable=False),
    CheckConstraint(
        "purpose IN ('login', 'deletion_recovery')",
        name=conv("ck_verification_codes_verification_codes_purpose_valid"),
    ),
)


Index("ix_workspaces_tenant_id", workspaces.c.tenant_id)
Index("ix_chat_sessions_tenant_id_workspace_id", chat_sessions.c.tenant_id, chat_sessions.c.workspace_id)
Index(
    "ix_memory_entries_tenant_id_workspace_id_session_id",
    memory_entries.c.tenant_id,
    memory_entries.c.workspace_id,
    memory_entries.c.session_id,
)
Index("ix_agent_runs_tenant_id_workspace_id_session_id", agent_runs.c.tenant_id, agent_runs.c.workspace_id, agent_runs.c.session_id)
Index(
    "ix_approval_requests_tenant_id_workspace_id_session_id_run_id",
    approval_requests.c.tenant_id,
    approval_requests.c.workspace_id,
    approval_requests.c.session_id,
    approval_requests.c.run_id,
)
Index(
    "ix_tool_executions_tenant_id_workspace_id_session_id_run_id",
    tool_executions.c.tenant_id,
    tool_executions.c.workspace_id,
    tool_executions.c.session_id,
    tool_executions.c.run_id,
)
Index(
    "ix_tool_executions_tenant_id_workspace_id_session_id_run_id_approval_id",
    tool_executions.c.tenant_id,
    tool_executions.c.workspace_id,
    tool_executions.c.session_id,
    tool_executions.c.run_id,
    tool_executions.c.approval_id,
)
Index(
    "ix_execution_checkpoints_tenant_id_workspace_id_session_id_run_id",
    execution_checkpoints.c.tenant_id,
    execution_checkpoints.c.workspace_id,
    execution_checkpoints.c.session_id,
    execution_checkpoints.c.run_id,
)
Index("ix_user_secrets_tenant_id_workspace_id", user_secrets.c.tenant_id, user_secrets.c.workspace_id)
Index(
    "ix_audit_logs_tenant_id_workspace_id_session_id_run_id",
    audit_logs.c.tenant_id,
    audit_logs.c.workspace_id,
    audit_logs.c.session_id,
    audit_logs.c.run_id,
)
Index("ix_deletion_jobs_tenant_id", deletion_jobs.c.tenant_id)
Index("ix_verification_codes_email_purpose_expires_at", verification_codes.c.email, verification_codes.c.purpose, verification_codes.c.expires_at)


__all__ = [
    "NAMING_CONVENTION",
    "agent_runs",
    "approval_requests",
    "audit_logs",
    "chat_sessions",
    "deletion_jobs",
    "execution_checkpoints",
    "memory_entries",
    "metadata",
    "tool_executions",
    "user_secrets",
    "users",
    "verification_codes",
    "workspaces",
]
