"""Scoped collaboration tables, defined after the workflow schema."""
from sqlalchemy import BigInteger, CheckConstraint, Column, ForeignKeyConstraint, Index, Integer, String, Table, UniqueConstraint


def define_tables(metadata, uuid_type, payload_type):
    def scope():
        return [Column("tenant_id", uuid_type, nullable=False),
                Column("workspace_id", uuid_type, nullable=False),
                Column("session_id", uuid_type, nullable=False)]

    def session_fk():
        return ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id"],
            ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
            ondelete="RESTRICT", onupdate="RESTRICT")

    def team_fk():
        return ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "team_id"],
            ["agent_teams.tenant_id", "agent_teams.workspace_id", "agent_teams.session_id", "agent_teams.team_id"],
            ondelete="RESTRICT", onupdate="RESTRICT")

    def member_fk(column):
        return ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "session_id", "team_id", column],
            ["agent_team_members.tenant_id", "agent_team_members.workspace_id", "agent_team_members.session_id", "agent_team_members.team_id", "agent_team_members.member_id"], ondelete="RESTRICT")

    teams = Table("agent_teams", metadata,
        Column("team_id", uuid_type, primary_key=True), *scope(),
        Column("objective", payload_type, nullable=False),
        Column("status", String(24), nullable=False),
        Column("config_json", payload_type, nullable=False),
        Column("version", BigInteger, nullable=False),
        Column("created_at", BigInteger, nullable=False),
        Column("updated_at", BigInteger, nullable=False),
        UniqueConstraint("tenant_id", "workspace_id", "session_id", "team_id"), session_fk(),
        CheckConstraint("status IN ('active','completed','failed','cancelled')", name="team_status"),
        CheckConstraint("version >= 1", name="team_version"))
    members = Table("agent_team_members", metadata,
        Column("member_id", uuid_type, primary_key=True), *scope(),
        Column("team_id", uuid_type, nullable=False),
        Column("name", String(80), nullable=False), Column("role", String(24), nullable=False),
        Column("config_json", payload_type, nullable=False),
        UniqueConstraint("tenant_id", "workspace_id", "session_id", "team_id", "member_id"),
        UniqueConstraint("team_id", "name"), team_fk(),
        CheckConstraint("role IN ('leader','member')", name="member_role"))
    jobs = Table("agent_jobs", metadata,
        Column("job_id", uuid_type, primary_key=True), *scope(),
        Column("parent_run_id", uuid_type, nullable=True),
        Column("child_session_id", uuid_type, nullable=True), Column("child_run_id", uuid_type, nullable=True),
        Column("team_id", uuid_type, nullable=True), Column("member_id", uuid_type, nullable=True),
        Column("task_id", uuid_type, nullable=True), Column("status", String(24), nullable=False),
        Column("budget_tokens", BigInteger, nullable=False),
        Column("request_json", payload_type, nullable=False), Column("result_json", payload_type, nullable=False),
        Column("version", BigInteger, nullable=False), Column("attempt", Integer, nullable=False),
        Column("created_at", BigInteger, nullable=False), Column("updated_at", BigInteger, nullable=False),
        UniqueConstraint("tenant_id", "workspace_id", "session_id", "job_id"), session_fk(),
        ForeignKeyConstraint(["tenant_id", "workspace_id", "child_session_id"],
                             ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "workspace_id", "session_id", "parent_run_id"],
                             ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "workspace_id", "child_session_id", "child_run_id"],
                             ["agent_runs.tenant_id", "agent_runs.workspace_id", "agent_runs.session_id", "agent_runs.run_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "workspace_id", "session_id", "team_id", "task_id"],
                             ["agent_team_tasks.tenant_id", "agent_team_tasks.workspace_id", "agent_team_tasks.session_id", "agent_team_tasks.team_id", "agent_team_tasks.task_id"], ondelete="RESTRICT"),
        team_fk(), member_fk("member_id"),
        CheckConstraint("status IN ('queued','running','awaiting_user','completed','failed','cancelled','interrupted')", name="job_status"),
        CheckConstraint("budget_tokens > 0 AND version >= 1 AND attempt >= 0", name="job_limits"),
        CheckConstraint("(child_session_id IS NULL AND child_run_id IS NULL) OR (child_session_id IS NOT NULL AND child_run_id IS NOT NULL)", name="job_child_binding"))
    tasks = Table("agent_team_tasks", metadata,
        Column("task_id", uuid_type, primary_key=True), *scope(), Column("team_id", uuid_type, nullable=False),
        Column("title", String(160), nullable=False), Column("objective", payload_type, nullable=False),
        Column("depends_json", payload_type, nullable=False), Column("owner_member_id", uuid_type, nullable=True),
        Column("job_id", uuid_type, nullable=True), Column("status", String(24), nullable=False),
        Column("result", payload_type, nullable=False), Column("version", BigInteger, nullable=False),
        Column("created_at", BigInteger, nullable=False), Column("updated_at", BigInteger, nullable=False),
        UniqueConstraint("tenant_id", "workspace_id", "session_id", "team_id", "task_id"), team_fk(), member_fk("owner_member_id"),
        CheckConstraint("status IN ('pending','running','completed','failed','cancelled')", name="task_status"),
        CheckConstraint("version >= 1 AND (status <> 'running' OR owner_member_id IS NOT NULL)", name="task_owner"))
    messages = Table("agent_team_messages", metadata,
        Column("message_id", uuid_type, primary_key=True), *scope(), Column("team_id", uuid_type, nullable=False),
        Column("sender_id", uuid_type, nullable=True), Column("recipient_id", uuid_type, nullable=True),
        Column("content", payload_type, nullable=False), Column("created_at", BigInteger, nullable=False),
        team_fk(), member_fk("sender_id"), member_fk("recipient_id"))
    Index("ix_agent_jobs_dispatch", jobs.c.status, jobs.c.created_at, jobs.c.job_id)
    Index("ix_agent_jobs_parent", jobs.c.tenant_id, jobs.c.workspace_id, jobs.c.session_id, jobs.c.parent_run_id)
    Index("ix_agent_jobs_child", jobs.c.tenant_id, jobs.c.workspace_id, jobs.c.child_session_id, jobs.c.child_run_id)
    Index("ix_agent_teams_dispatch", teams.c.status, teams.c.created_at, teams.c.team_id)
    return {table.name: table for table in (teams, members, tasks, jobs, messages)}
