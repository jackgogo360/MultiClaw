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
    column,
    func,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR
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
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "id"),
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


agent_plans = Table(
    "agent_plans",
    metadata,
    Column("id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=False),
    Column("source_message_id", UUID_CHAR, nullable=False),
    Column("trigger_mode", String(16), nullable=False),
    Column("status", String(32), nullable=False),
    Column("current_version", Integer, nullable=False),
    Column("approved_version", Integer, nullable=True),
    Column("version", BIGINT, nullable=False, server_default="1"),
    Column("created_at", BIGINT, nullable=False),
    Column("updated_at", BIGINT, nullable=False),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "id"),
    CheckConstraint(
        "trigger_mode IN ('automatic', 'explicit')",
        name=conv("ck_agent_plans_trigger_mode_valid"),
    ),
    CheckConstraint(
        "status IN ('awaiting_approval', 'approved', 'rejected', 'archived')",
        name=conv("ck_agent_plans_status_valid"),
    ),
    CheckConstraint(
        "current_version >= 1",
        name=conv("ck_agent_plans_current_version_positive"),
    ),
    CheckConstraint(
        "approved_version IS NULL OR approved_version <= current_version",
        name=conv("ck_agent_plans_approved_version_valid"),
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id"],
        ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
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
)


agent_plan_versions = Table(
    "agent_plan_versions",
    metadata,
    Column("tenant_id", UUID_CHAR, primary_key=True),
    Column("workspace_id", UUID_CHAR, primary_key=True),
    Column("session_id", UUID_CHAR, primary_key=True),
    Column("plan_id", UUID_CHAR, primary_key=True),
    Column("plan_version", Integer, primary_key=True),
    Column("objective", PAYLOAD_TEXT, nullable=False),
    Column("constraints_json", PAYLOAD_TEXT, nullable=False),
    Column("generation_reason", PAYLOAD_TEXT, nullable=False),
    Column("parent_version", Integer, nullable=True),
    Column("revision_feedback", PAYLOAD_TEXT, nullable=True),
    Column("schema_version", Integer, nullable=False),
    Column("content_digest", CHAR(64), nullable=False),
    Column("created_at", BIGINT, nullable=False),
    CheckConstraint(
        "plan_version >= 1",
        name=conv("ck_agent_plan_versions_plan_version_positive"),
    ),
    CheckConstraint(
        "schema_version >= 1",
        name=conv("ck_agent_plan_versions_schema_version_positive"),
    ),
    CheckConstraint(
        "length(content_digest) = 64",
        name=conv("ck_agent_plan_versions_content_digest_valid"),
    ),
    CheckConstraint(
        "parent_version IS NULL OR parent_version < plan_version",
        name=conv("ck_agent_plan_versions_parent_version_valid"),
    ),
    ForeignKeyConstraint(
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
    ForeignKeyConstraint(
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
)


agent_plan_steps = Table(
    "agent_plan_steps",
    metadata,
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=False),
    Column("plan_id", UUID_CHAR, nullable=False),
    Column("plan_version", Integer, nullable=False),
    Column("step_id", UUID_CHAR, primary_key=True),
    Column("logical_step_key", String(64), nullable=False),
    Column("supersedes_step_id", UUID_CHAR, nullable=True),
    Column("ordinal", Integer, nullable=False),
    Column("title", String(200), nullable=False),
    Column("description", PAYLOAD_TEXT, nullable=False),
    Column("expected_outcome", PAYLOAD_TEXT, nullable=False),
    Column("assigned_agent_profile_id", UUID_CHAR, nullable=True),
    Column("max_attempts", Integer, nullable=False),
    Column("definition_digest", CHAR(64), nullable=False),
    UniqueConstraint(
        "tenant_id",
        "workspace_id",
        "session_id",
        "plan_id",
        "plan_version",
        "step_id",
        name="uq_agent_plan_steps_scope_version_step",
    ),
    UniqueConstraint(
        "tenant_id",
        "workspace_id",
        "session_id",
        "plan_id",
        "plan_version",
        "logical_step_key",
        name="uq_agent_plan_steps_scope_version_logical_key",
    ),
    UniqueConstraint(
        "tenant_id",
        "workspace_id",
        "session_id",
        "plan_id",
        "step_id",
        name="uq_agent_plan_steps_scope_plan_step",
    ),
    CheckConstraint(
        "ordinal >= 1 AND ordinal <= 20",
        name=conv("ck_agent_plan_steps_ordinal_valid"),
    ),
    CheckConstraint(
        "max_attempts >= 1 AND max_attempts <= 20",
        name=conv("ck_agent_plan_steps_max_attempts_valid"),
    ),
    CheckConstraint(
        "length(definition_digest) = 64",
        name=conv("ck_agent_plan_steps_definition_digest_valid"),
    ),
    ForeignKeyConstraint(
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
    ForeignKeyConstraint(
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
)


agent_plan_step_dependencies = Table(
    "agent_plan_step_dependencies",
    metadata,
    Column("tenant_id", UUID_CHAR, primary_key=True),
    Column("workspace_id", UUID_CHAR, primary_key=True),
    Column("session_id", UUID_CHAR, primary_key=True),
    Column("plan_id", UUID_CHAR, primary_key=True),
    Column("plan_version", Integer, primary_key=True),
    Column("step_id", UUID_CHAR, primary_key=True),
    Column("depends_on_step_id", UUID_CHAR, primary_key=True),
    CheckConstraint(
        "step_id <> depends_on_step_id",
        name=conv("ck_agent_plan_step_dependencies_distinct_steps"),
    ),
    ForeignKeyConstraint(
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
    ForeignKeyConstraint(
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
)


agent_plan_decisions = Table(
    "agent_plan_decisions",
    metadata,
    Column("tenant_id", UUID_CHAR, primary_key=True),
    Column("workspace_id", UUID_CHAR, primary_key=True),
    Column("session_id", UUID_CHAR, primary_key=True),
    Column("plan_id", UUID_CHAR, primary_key=True),
    Column(
        "decision_id",
        String(128).with_variant(
            VARCHAR(128, collation="utf8mb4_bin"),
            "mysql",
        ),
        primary_key=True,
    ),
    Column("plan_version", Integer, nullable=False),
    Column("expected_plan_cas_version", BIGINT, nullable=False),
    Column("action", String(16), nullable=False),
    Column("feedback", PAYLOAD_TEXT, nullable=True),
    Column("decided_by", UUID_CHAR, nullable=False),
    Column("resulting_plan_version", Integer, nullable=True),
    Column("created_at", BIGINT, nullable=False),
    CheckConstraint(
        "plan_version >= 1",
        name=conv("ck_agent_plan_decisions_plan_version_positive"),
    ),
    CheckConstraint(
        "expected_plan_cas_version >= 1",
        name=conv("ck_agent_plan_decisions_expected_cas_positive"),
    ),
    CheckConstraint(
        "action IN ('approve', 'reject', 'revise')",
        name=conv("ck_agent_plan_decisions_action_valid"),
    ),
    CheckConstraint(
        func.char_length(column("decision_id")).between(1, 128),
        name=conv("ck_agent_plan_decisions_decision_id_length"),
    ),
    ForeignKeyConstraint(
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
    ForeignKeyConstraint(
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
)


agent_runs = Table(
    "agent_runs",
    metadata,
    Column("run_id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=False),
    Column("plan_id", UUID_CHAR, nullable=True),
    Column("initial_plan_version", Integer, nullable=True),
    Column("active_plan_version", Integer, nullable=True),
    Column("cancel_requested_at", BIGINT, nullable=True),
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
    UniqueConstraint(
        "tenant_id",
        "workspace_id",
        "session_id",
        "plan_id",
        "run_id",
        name="uq_agent_runs_scope_plan_run",
    ),
    CheckConstraint(
        (
            "run_status IN ("
            "'running', 'awaiting_user', 'resuming', 'completed', 'failed_terminal', "
            "'blocked_incompatible', 'blocked_corrupt', 'cancelled'"
            ")"
        ),
        name=conv("ck_agent_runs_agent_runs_run_status_valid"),
    ),
    CheckConstraint(
        (
            "(plan_id IS NULL AND initial_plan_version IS NULL AND active_plan_version IS NULL) OR "
            "(plan_id IS NOT NULL AND initial_plan_version IS NOT NULL "
            "AND active_plan_version IS NOT NULL)"
        ),
        name=conv("ck_agent_runs_plan_binding_complete"),
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id"],
        ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        [
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "initial_plan_version",
        ],
        [
            "agent_plan_versions.tenant_id",
            "agent_plan_versions.workspace_id",
            "agent_plan_versions.session_id",
            "agent_plan_versions.plan_id",
            "agent_plan_versions.plan_version",
        ],
        name="fk_agent_runs_initial_plan_agent_plan_versions",
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        [
            "tenant_id",
            "workspace_id",
            "session_id",
            "plan_id",
            "active_plan_version",
        ],
        [
            "agent_plan_versions.tenant_id",
            "agent_plan_versions.workspace_id",
            "agent_plan_versions.session_id",
            "agent_plan_versions.plan_id",
            "agent_plan_versions.plan_version",
        ],
        name="fk_agent_runs_active_plan_agent_plan_versions",
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)


agent_plan_step_runs = Table(
    "agent_plan_step_runs",
    metadata,
    Column("tenant_id", UUID_CHAR, primary_key=True),
    Column("workspace_id", UUID_CHAR, primary_key=True),
    Column("session_id", UUID_CHAR, primary_key=True),
    Column("plan_id", UUID_CHAR, primary_key=True),
    Column("plan_version", Integer, primary_key=True),
    Column("step_id", UUID_CHAR, primary_key=True),
    Column("step_run_id", UUID_CHAR, primary_key=True),
    Column("run_id", UUID_CHAR, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("result_summary", PAYLOAD_TEXT, nullable=True),
    Column("result_ref", String(128), nullable=True),
    Column("result_digest", CHAR(64), nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("error_detail_redacted", PAYLOAD_TEXT, nullable=True),
    Column("reused_from_step_run_id", UUID_CHAR, nullable=True),
    Column("version", BIGINT, nullable=False, server_default="1"),
    Column("started_at", BIGINT, nullable=False),
    Column("finished_at", BIGINT, nullable=True),
    UniqueConstraint(
        "tenant_id",
        "workspace_id",
        "session_id",
        "run_id",
        "step_id",
        "attempt",
        name="uq_agent_plan_step_runs_scope_run_step_attempt",
    ),
    UniqueConstraint(
        "tenant_id",
        "workspace_id",
        "session_id",
        "plan_id",
        "run_id",
        "step_run_id",
        name="uq_agent_plan_step_runs_scope_plan_run_step_run",
    ),
    CheckConstraint(
        (
            "status IN ('pending', 'running', 'succeeded', 'failed_retryable', "
            "'failed_terminal', 'cancelled')"
        ),
        name=conv("ck_agent_plan_step_runs_status_valid"),
    ),
    CheckConstraint(
        "attempt >= 1",
        name=conv("ck_agent_plan_step_runs_attempt_positive"),
    ),
    CheckConstraint(
        "version >= 1",
        name=conv("ck_agent_plan_step_runs_version_positive"),
    ),
    CheckConstraint(
        "result_digest IS NULL OR length(result_digest) = 64",
        name=conv("ck_agent_plan_step_runs_result_digest_valid"),
    ),
    CheckConstraint(
        "finished_at IS NULL OR finished_at >= started_at",
        name=conv("ck_agent_plan_step_runs_finished_at_valid"),
    ),
    ForeignKeyConstraint(
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
    ForeignKeyConstraint(
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
    ForeignKeyConstraint(
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
Index(
    "ix_agent_plans_scope_created_at",
    agent_plans.c.tenant_id,
    agent_plans.c.workspace_id,
    agent_plans.c.session_id,
    agent_plans.c.created_at,
)
Index(
    "ix_agent_plan_steps_scope_version_ordinal",
    agent_plan_steps.c.tenant_id,
    agent_plan_steps.c.workspace_id,
    agent_plan_steps.c.session_id,
    agent_plan_steps.c.plan_id,
    agent_plan_steps.c.plan_version,
    agent_plan_steps.c.ordinal,
)
Index(
    "ix_agent_plan_decisions_scope_created_at",
    agent_plan_decisions.c.tenant_id,
    agent_plan_decisions.c.workspace_id,
    agent_plan_decisions.c.session_id,
    agent_plan_decisions.c.plan_id,
    agent_plan_decisions.c.created_at,
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
    "agent_plan_decisions",
    "agent_plan_step_dependencies",
    "agent_plan_step_runs",
    "agent_plan_steps",
    "agent_plan_versions",
    "agent_plans",
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
