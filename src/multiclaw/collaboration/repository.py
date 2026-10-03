"""Tenant/session scoped durable jobs, task claims and team messages."""
from __future__ import annotations

import json
import time
from uuid import uuid4

from sqlalchemy import func, insert, select, update

from multiclaw.collaboration.models import AgentRequest, TeamRequest
from multiclaw.storage.schema import agent_jobs, agent_team_members, agent_team_messages, agent_team_tasks, agent_teams


class CollaborationNotFound(LookupError):
    pass


class CollaborationConflict(RuntimeError):
    pass


def _decode(row):
    if row is None:
        return None
    value = dict(row)
    for name in list(value):
        if name.endswith("_json"):
            value[name.removesuffix("_json")] = json.loads(value.pop(name))
    return value


class CollaborationRepository:
    def __init__(self, connection, context, dialect):
        if context.session_id is None:
            raise ValueError("collaboration requires a session")
        self.connection, self.context, self.dialect = connection, context, dialect

    def _scope(self, table):
        return (table.c.tenant_id == self.context.tenant_id,
                table.c.workspace_id == self.context.workspace_id,
                table.c.session_id == self.context.session_id)

    def _values(self):
        return {"tenant_id": self.context.tenant_id, "workspace_id": self.context.workspace_id,
                "session_id": self.context.session_id}

    async def _get(self, table, key, value):
        row = (await self.connection.execute(select(table).where(*self._scope(table), table.c[key] == value))).mappings().first()
        return _decode(row)

    async def _list(self, table, *filters):
        query = select(table).where(*self._scope(table), *filters)
        if "created_at" in table.c:
            query = query.order_by(table.c.created_at, list(table.primary_key)[0])
        rows = (await self.connection.execute(query.limit(500))).mappings()
        return [_decode(row) for row in rows]

    async def create_job(self, request: dict, *, parent_run_id=None, team_id=None, member_id=None, task_id=None,
                         budget_tokens=20000, parent_limit=250000, team_limit=250000, max_jobs=100):
        request = AgentRequest.model_validate(request).model_dump()
        await self._lock_session()
        if len(await self.list_jobs()) >= max_jobs:
            raise CollaborationConflict("session assignment limit reached")
        if team_id:
            await self._lock_team(team_id)
            await self.require_team(team_id)
            if member_id:
                await self.require_member(team_id, member_id)
            reserved = await self.connection.scalar(select(func.coalesce(func.sum(agent_jobs.c.budget_tokens), 0)).where(
                *self._scope(agent_jobs), agent_jobs.c.team_id == team_id))
            if int(reserved) + budget_tokens + (budget_tokens if task_id else 0) > team_limit:
                raise CollaborationConflict("team token allocation exhausted")
        if parent_run_id:
            from multiclaw.storage.schema import agent_runs, memory_entries
            parent = (await self.connection.execute(select(agent_runs.c.run_id).where(
                agent_runs.c.tenant_id == self.context.tenant_id, agent_runs.c.workspace_id == self.context.workspace_id,
                agent_runs.c.session_id == self.context.session_id, agent_runs.c.run_id == parent_run_id,
            ).with_for_update())).scalar_one_or_none()
            if parent is None:
                raise CollaborationNotFound("parent Run not found")
            reserved = await self.connection.scalar(select(func.coalesce(func.sum(agent_jobs.c.budget_tokens), 0)).where(
                *self._scope(agent_jobs), agent_jobs.c.parent_run_id == parent_run_id))
            rows = (await self.connection.execute(select(memory_entries.c.metadata_json).where(
                memory_entries.c.tenant_id == self.context.tenant_id,
                memory_entries.c.workspace_id == self.context.workspace_id,
                memory_entries.c.session_id == self.context.session_id,
                memory_entries.c.type == "run_usage",
            ))).scalars()
            used = sum(int(data.get("total_tokens", 0)) for data in (json.loads(row) for row in rows)
                       if data.get("run_id") == parent_run_id)
            if int(reserved) + used + budget_tokens > parent_limit:
                raise CollaborationConflict("parent token allocation exhausted")
        now = int(time.time() * 1000)
        job_id = str(uuid4())
        await self.connection.execute(insert(agent_jobs).values(
            **self._values(), job_id=job_id, parent_run_id=parent_run_id,
            child_session_id=None, child_run_id=None, team_id=team_id, member_id=member_id,
            task_id=task_id, status="queued", request_json=json.dumps(request), result_json="{}",
            budget_tokens=budget_tokens,
            version=1, attempt=0, created_at=now, updated_at=now))
        return await self.get_job(job_id)

    async def get_job(self, job_id, *, for_update=False):
        query = select(agent_jobs).where(*self._scope(agent_jobs), agent_jobs.c.job_id == job_id)
        if for_update:
            query = query.with_for_update()
        return _decode((await self.connection.execute(query)).mappings().first())

    async def list_jobs(self, *, team_id=None):
        return await self._list(agent_jobs, *( [agent_jobs.c.team_id == team_id] if team_id else []))

    async def update_job(self, job_id, expected_version, **changes):
        permitted = {"status", "result", "request", "child_session_id", "child_run_id", "attempt"}
        if not set(changes).issubset(permitted):
            raise ValueError("unsupported job update")
        for name in ("result", "request"):
            if name in changes:
                changes[name + "_json"] = json.dumps(changes.pop(name))
        result = await self.connection.execute(update(agent_jobs).where(
            *self._scope(agent_jobs), agent_jobs.c.job_id == job_id, agent_jobs.c.version == expected_version
        ).values(**changes, version=agent_jobs.c.version + 1, updated_at=int(time.time() * 1000)))
        if result.rowcount != 1:
            raise CollaborationConflict("job version conflict")
        return await self.get_job(job_id)

    async def create_team(self, objective, members, *, project_path="."):
        request = TeamRequest(objective=objective, members=members, project_path=project_path)
        await self._lock_session()
        if len(await self.list_teams()) >= 20:
            raise CollaborationConflict("session team limit reached")
        team_id, now = str(uuid4()), int(time.time() * 1000)
        await self.connection.execute(insert(agent_teams).values(
            **self._values(), team_id=team_id, objective=objective, status="active",
            config_json=json.dumps({"project_path": project_path}), version=1, created_at=now, updated_at=now))
        for member in request.members:
            await self.connection.execute(insert(agent_team_members).values(
                **self._values(), team_id=team_id, member_id=str(uuid4()), name=member.name,
                role=member.role, config_json=json.dumps(member.model_dump())))
        return await self.get_team(team_id)

    async def get_team(self, team_id):
        team = await self._get(agent_teams, "team_id", team_id)
        if team is not None:
            team["summary"] = team["config"].get("summary", "")
            team["members"] = await self._list(agent_team_members, agent_team_members.c.team_id == team_id)
            team["tasks"] = await self._list(agent_team_tasks, agent_team_tasks.c.team_id == team_id)
        return team

    async def list_teams(self):
        return await self._list(agent_teams)

    async def update_team(self, team_id, expected_version, *, status=None, config=None):
        values = {"version": agent_teams.c.version + 1, "updated_at": int(time.time() * 1000)}
        if status is not None:
            if status not in {"active", "completed", "failed", "cancelled"}:
                raise ValueError("invalid team status")
            values["status"] = status
        if config is not None:
            values["config_json"] = json.dumps(config)
        result = await self.connection.execute(update(agent_teams).where(
            *self._scope(agent_teams), agent_teams.c.team_id == team_id,
            agent_teams.c.version == expected_version,
        ).values(**values))
        if result.rowcount != 1:
            raise CollaborationConflict("team version changed")
        return await self.get_team(team_id)

    async def require_team(self, team_id):
        team = await self.get_team(team_id)
        if team is None:
            raise CollaborationNotFound("team not found")
        return team

    async def require_member(self, team_id, member_id):
        team = await self.require_team(team_id)
        member = next((item for item in team["members"] if item["member_id"] == member_id), None)
        if member is None:
            raise CollaborationNotFound("member not found")
        return member

    async def create_task(self, team_id, title, objective, *, depends_on=None, assigned_member_id=None):
        await self._lock_session()
        await self._lock_team(team_id)
        team = await self.require_team(team_id)
        if team["status"] != "active":
            raise CollaborationConflict("team is not active")
        if not title.strip() or len(title) > 160 or not objective.strip() or len(objective) > 12000:
            raise ValueError("invalid task title or objective")
        depends_on = list(dict.fromkeys(depends_on or []))
        if len(depends_on) > 20:
            raise ValueError("too many dependencies")
        known = {task["task_id"] for task in team["tasks"]}
        if not set(depends_on).issubset(known):
            raise CollaborationNotFound("dependency not found")
        if len(team["tasks"]) >= 100:
            raise CollaborationConflict("team task limit reached")
        if assigned_member_id:
            await self.require_member(team_id, assigned_member_id)
        task_id, now = str(uuid4()), int(time.time() * 1000)
        await self.connection.execute(insert(agent_team_tasks).values(
            **self._values(), team_id=team_id, task_id=task_id, title=title, objective=objective,
            depends_json=json.dumps(depends_on), owner_member_id=assigned_member_id,
            job_id=None, status="pending", result="", version=1, created_at=now, updated_at=now))
        return await self._get(agent_team_tasks, "task_id", task_id)

    async def claim_task(self, team_id, task_id, member_id, expected_version):
        await self._lock_session()
        await self._lock_team(team_id)
        team = await self.require_team(team_id)
        await self.require_member(team_id, member_id)
        task = next((task for task in team["tasks"] if task["task_id"] == task_id), None)
        if task is None:
            raise CollaborationNotFound("task not found")
        completed = {item["task_id"] for item in team["tasks"] if item["status"] == "completed"}
        if team["status"] != "active" or not set(task["depends"]).issubset(completed):
            raise CollaborationConflict("task dependencies are not completed")
        if task["owner_member_id"] not in (None, member_id):
            raise CollaborationConflict("task assigned to another member")
        result = await self.connection.execute(update(agent_team_tasks).where(
            *self._scope(agent_team_tasks), agent_team_tasks.c.team_id == team_id,
            agent_team_tasks.c.task_id == task_id, agent_team_tasks.c.status == "pending",
            agent_team_tasks.c.version == expected_version,
        ).values(status="running", owner_member_id=member_id, version=agent_team_tasks.c.version + 1,
                 updated_at=int(time.time() * 1000)))
        if result.rowcount != 1:
            raise CollaborationConflict("task already claimed or version changed")
        return await self._get(agent_team_tasks, "task_id", task_id)

    async def finish_task(self, team_id, task_id, member_id, expected_version, status, result):
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid terminal task status")
        updated = await self.connection.execute(update(agent_team_tasks).where(
            *self._scope(agent_team_tasks), agent_team_tasks.c.team_id == team_id,
            agent_team_tasks.c.task_id == task_id, agent_team_tasks.c.owner_member_id == member_id,
            agent_team_tasks.c.version == expected_version, agent_team_tasks.c.status == "running",
        ).values(status=status, result=result[:16000], version=agent_team_tasks.c.version + 1,
                 updated_at=int(time.time() * 1000)))
        if updated.rowcount != 1:
            raise CollaborationConflict("task ownership or version changed")
        return await self._get(agent_team_tasks, "task_id", task_id)

    async def bind_task_job(self, task_id, job_id):
        await self.connection.execute(update(agent_team_tasks).where(
            *self._scope(agent_team_tasks), agent_team_tasks.c.task_id == task_id,
            agent_team_tasks.c.status == "running", agent_team_tasks.c.job_id.is_(None),
        ).values(job_id=job_id))

    async def send_message(self, team_id, sender_id, recipient_id, content):
        await self._lock_team(team_id)
        team = await self.require_team(team_id)
        if team["status"] != "active":
            raise CollaborationConflict("team is not active")
        if sender_id:
            await self.require_member(team_id, sender_id)
        if recipient_id:
            await self.require_member(team_id, recipient_id)
        if not content.strip() or len(content) > 8000:
            raise ValueError("invalid team message")
        if len(await self._list(agent_team_messages, agent_team_messages.c.team_id == team_id)) >= 500:
            raise CollaborationConflict("team message limit reached")
        message_id = str(uuid4())
        await self.connection.execute(insert(agent_team_messages).values(
            **self._values(), team_id=team_id, message_id=message_id, sender_id=sender_id,
            recipient_id=recipient_id, content=content, created_at=int(time.time() * 1000)))
        return await self._get(agent_team_messages, "message_id", message_id)

    async def messages(self, team_id, member_id=None):
        await self.require_team(team_id)
        messages = await self._list(agent_team_messages, agent_team_messages.c.team_id == team_id)
        if member_id:
            await self.require_member(team_id, member_id)
            messages = [item for item in messages if item["recipient_id"] in (None, member_id) or item["sender_id"] == member_id]
        return messages

    async def cancel_team(self, team_id):
        await self.require_team(team_id)
        await self.connection.execute(update(agent_teams).where(
            *self._scope(agent_teams), agent_teams.c.team_id == team_id
        ).values(status="cancelled", version=agent_teams.c.version + 1, updated_at=int(time.time() * 1000)))
        await self.connection.execute(update(agent_team_tasks).where(
            *self._scope(agent_team_tasks), agent_team_tasks.c.team_id == team_id,
            agent_team_tasks.c.status == "pending"
        ).values(status="cancelled", version=agent_team_tasks.c.version + 1))
        return await self.get_team(team_id)

    async def _lock_team(self, team_id):
        await self.connection.execute(select(agent_teams.c.team_id).where(
            *self._scope(agent_teams), agent_teams.c.team_id == team_id,
        ).with_for_update())

    async def _lock_session(self):
        from multiclaw.storage.schema import chat_sessions
        await self.connection.execute(select(chat_sessions.c.id).where(
            chat_sessions.c.tenant_id == self.context.tenant_id,
            chat_sessions.c.workspace_id == self.context.workspace_id,
            chat_sessions.c.id == self.context.session_id,
        ).with_for_update())
