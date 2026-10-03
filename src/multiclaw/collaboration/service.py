"""Durable collaboration admission, dispatch and controls."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from sqlalchemy import and_, or_, select, update

from multiclaw.collaboration.models import AgentRequest, TeamRequest
from multiclaw.collaboration.repository import CollaborationConflict, CollaborationNotFound, CollaborationRepository
from multiclaw.storage.schema import agent_jobs, agent_runs, agent_teams, agent_team_tasks, chat_sessions, users
from multiclaw.tenancy import TenantContext


TERMINAL_JOBS = {"completed", "failed", "cancelled"}


class CollaborationService:
    def __init__(self, database, *, executor, max_workers=3, max_jobs=100):
        self.database, self.executor = database, executor
        self.max_workers, self.max_jobs = max_workers, max_jobs
        self._tasks: dict[str, asyncio.Task] = {}
        self._closed = False
        self._tick_lock = asyncio.Lock()
        self._revoked = set()
        self._job_cursor = None
        self._team_cursor = None
        self._blocked_sessions = set()

    @staticmethod
    def _session_key(context):
        return context.tenant_id, context.workspace_id, context.session_id

    def session_blocked(self, context):
        return self._session_key(context) in self._blocked_sessions

    async def _admission(self, context):
        if self._closed:
            raise CollaborationConflict("collaboration is shutting down")
        if context.tenant_id in self._revoked:
            async with self.database.connect() as connection:
                active = await connection.scalar(select(users.c.status).where(users.c.id == context.tenant_id))
            if active != "active":
                raise CollaborationConflict("tenant is unavailable")
            self._revoked.discard(context.tenant_id)
            self._blocked_sessions = {key for key in self._blocked_sessions if key[0] != context.tenant_id}
        if self.session_blocked(context):
            raise CollaborationConflict("session is being deleted")

    def _allocation(self):
        settings = getattr(self.executor, "settings", None)
        return {"budget_tokens": settings.collaboration.max_job_tokens if settings else 20000,
                "parent_limit": settings.runtime.max_run_tokens if settings else 250000,
                "team_limit": settings.collaboration.max_team_tokens if settings else 250000,
                "max_jobs": self.max_jobs}

    @asynccontextmanager
    async def store(self, context, *, write=False):
        manager = self.database.write_transaction() if write else self.database.connect()
        async with manager as connection:
            yield CollaborationRepository(connection, context, self.database.dialect)

    async def spawn(self, context, request, *, parent_run_id=None, team_id=None, member_id=None, task_id=None):
        await self._admission(context)
        AgentRequest.model_validate(request)
        validator = getattr(self.executor, "validate_request", None)
        if validator is not None:
            await validator(context, request)
        async with self.store(context, write=True) as repository:
            session = (await repository.connection.execute(select(chat_sessions.c.id).where(
                chat_sessions.c.tenant_id == context.tenant_id,
                chat_sessions.c.workspace_id == context.workspace_id,
                chat_sessions.c.id == context.session_id,
            ))).scalar_one_or_none()
            if session is None:
                raise CollaborationNotFound("session not found")
            if len(await repository.list_jobs()) >= self.max_jobs:
                raise CollaborationConflict("session child task limit reached")
            return await repository.create_job(request, parent_run_id=parent_run_id,
                                               team_id=team_id, member_id=member_id, task_id=task_id, **self._allocation())

    async def get_job(self, context, job_id):
        async with self.store(context) as repository:
            job = await repository.get_job(job_id)
        if job is None:
            raise CollaborationNotFound("job not found")
        return job

    async def list_jobs(self, context):
        async with self.store(context) as repository:
            return await repository.list_jobs()

    async def cancel(self, context, job_id):
        job = await self.get_job(context, job_id)
        if job["status"] in TERMINAL_JOBS:
            if job["status"] == "cancelled" and hasattr(self.executor, "cancel"):
                await self.executor.cancel(job, context)
            return job
        async with self.store(context, write=True) as repository:
            job = await repository.update_job(job_id, job["version"], status="cancelled")
        cancel = getattr(self.executor, "cancel", None)
        task = self._tasks.get(job_id)
        try:
            if cancel is not None:
                await cancel(job, context)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return await self.get_job(context, job_id)

    async def wait(self, context, job_id, *, timeout=25):
        async with asyncio.timeout(max(0.01, min(timeout, 30))):
            try:
                while True:
                    job = await self.get_job(context, job_id)
                    if job["status"] in TERMINAL_JOBS or job["status"] == "awaiting_user":
                        return job
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                raise

    async def steer(self, context, job_id, message):
        if not message.strip() or len(message) > 8000:
            raise ValueError("invalid agent message")
        job = await self.get_job(context, job_id)
        if job["status"] in TERMINAL_JOBS:
            raise CollaborationConflict("agent assignment already finished")
        handler = getattr(self.executor, "steer", None)
        if handler is not None:
            await handler(job, context, message)
        else:
            raise CollaborationConflict("agent control unavailable")
        return await self.get_job(context, job_id)

    async def create_team(self, context, request):
        parsed = TeamRequest.model_validate(request)
        await self._admission(context)
        validator = getattr(self.executor, "validate_request", None)
        if validator is not None:
            for member in parsed.members:
                await validator(context, {"goal": parsed.objective, "profile": member.profile,
                    "project_path": parsed.project_path, "model": member.model})
        async with self.store(context, write=True) as repository:
            team = await repository.create_team(parsed.objective,
                [member.model_dump() for member in parsed.members], project_path=parsed.project_path)
            leader = next(member for member in team["members"] if member["role"] == "leader")
            config = leader["config"]
            await repository.create_job({
                "goal": parsed.objective,
                "context": "You lead this team. Inspect the shared board and create independent tasks with team_create_task. Assign members by ID. Review their reports before synthesizing a final answer.",
                "profile": config["profile"], "model": config.get("model"),
                "project_path": parsed.project_path, "instructions": config.get("instructions", ""),
            }, team_id=team["team_id"], member_id=leader["member_id"], **self._allocation())
            return team

    async def get_team(self, context, team_id):
        async with self.store(context) as repository:
            return await repository.require_team(team_id)

    async def list_teams(self, context):
        async with self.store(context) as repository:
            return await repository.list_teams()

    async def cancel_team(self, context, team_id):
        async with self.store(context, write=True) as repository:
            team = await repository.cancel_team(team_id)
            jobs = await repository.list_jobs(team_id=team_id)
        for job in jobs:
            if job["status"] not in TERMINAL_JOBS:
                await self.cancel(context, job["job_id"])
        return team

    async def tick(self):
        if self._closed:
            return
        async with self._tick_lock:
            await self._propagate_cancellation()
            await self._schedule_team_tasks()
            async with self.database.connect() as connection:
                query = select(
                    agent_jobs.c.job_id, agent_jobs.c.tenant_id, agent_jobs.c.workspace_id,
                    agent_jobs.c.session_id, agent_jobs.c.created_at,
                ).join(users,
                    agent_jobs.c.tenant_id == users.c.id).where(
                    users.c.status == "active", agent_jobs.c.status.in_(("queued", "interrupted", "running", "awaiting_user")),
                )
                if self._job_cursor:
                    created, job_id = self._job_cursor
                    query = query.where(or_(agent_jobs.c.created_at > created,
                        and_(agent_jobs.c.created_at == created, agent_jobs.c.job_id > job_id)))
                rows = (await connection.execute(query.order_by(agent_jobs.c.created_at, agent_jobs.c.job_id).limit(100))).mappings().all()
            self._job_cursor = (rows[-1]["created_at"], rows[-1]["job_id"]) if len(rows) == 100 else None
            for row in rows:
                job_id = row["job_id"]
                if job_id in self._tasks:
                    continue
                if len(self._tasks) >= self.max_workers:
                    continue
                context = TenantContext(row["tenant_id"], row["workspace_id"], row["session_id"])
                if self.session_blocked(context) or context.tenant_id in self._revoked:
                    continue
                job = await self.get_job(context, job_id)
                if job["status"] not in {"queued", "running", "awaiting_user", "interrupted"}:
                    continue
                ready = getattr(self.executor, "ready", None)
                if ready is not None and not await ready(job, context):
                    continue
                try:
                    async with self.store(context, write=True) as repository:
                        current = await repository.get_job(job_id, for_update=True)
                        if current is None or current["status"] not in {"queued", "running", "awaiting_user", "interrupted"}:
                            continue
                        job = await repository.update_job(job_id, job["version"], status="running", attempt=job["attempt"] + 1)
                except CollaborationConflict:
                    continue
                task = asyncio.create_task(self._run(job, context))
                self._tasks[job_id] = task

    async def _propagate_cancellation(self):
        async with self.database.connect() as connection:
            rows = (await connection.execute(select(agent_jobs.c.job_id, agent_jobs.c.tenant_id,
                agent_jobs.c.workspace_id, agent_jobs.c.session_id).join(agent_runs, and_(
                    agent_runs.c.tenant_id == agent_jobs.c.tenant_id,
                    agent_runs.c.workspace_id == agent_jobs.c.workspace_id,
                    agent_runs.c.session_id == agent_jobs.c.session_id,
                    agent_runs.c.run_id == agent_jobs.c.parent_run_id,
                )).where(agent_jobs.c.status.not_in(tuple(TERMINAL_JOBS)),
                    or_(agent_runs.c.cancel_requested_at.is_not(None), agent_runs.c.run_status == "cancelled")
                ).limit(100))).mappings().all()
        for row in rows:
            await self.cancel(TenantContext(row["tenant_id"], row["workspace_id"], row["session_id"]), row["job_id"])
        if hasattr(self.executor, "cancel"):
            async with self.database.connect() as connection:
                rows = (await connection.execute(select(agent_jobs).join(agent_runs, and_(
                    agent_runs.c.tenant_id == agent_jobs.c.tenant_id,
                    agent_runs.c.workspace_id == agent_jobs.c.workspace_id,
                    agent_runs.c.session_id == agent_jobs.c.child_session_id,
                    agent_runs.c.run_id == agent_jobs.c.child_run_id,
                )).where(agent_jobs.c.status == "cancelled", agent_runs.c.run_status.in_(("running", "awaiting_user", "resuming"))).limit(100))).mappings().all()
            for row in rows:
                context = TenantContext(row["tenant_id"], row["workspace_id"], row["session_id"])
                await self.executor.cancel(await self.get_job(context, row["job_id"]), context)

    async def _run(self, job, context):
        try:
            result = await self.executor(job, context)
            status = result.get("status", "completed")
            if status not in TERMINAL_JOBS | {"awaiting_user", "interrupted"}:
                raise ValueError("invalid executor status")
        except asyncio.CancelledError:
            status, result = ("interrupted" if self._closed else "cancelled"), {}
        except Exception:
            status = "interrupted" if job["child_run_id"] else "failed"
            result = {"summary": "Agent assignment failed; recovery state retained" if status == "interrupted" else "Agent assignment failed"}
        try:
            async with self.store(context, write=True) as repository:
                current = await repository.get_job(job["job_id"])
                if current and current["status"] != "cancelled":
                    await repository.update_job(current["job_id"], current["version"], status=status,
                                                result={**current["result"], **result})
                if job["task_id"] and status in TERMINAL_JOBS:
                    team = await repository.require_team(job["team_id"])
                    task = next(item for item in team["tasks"] if item["task_id"] == job["task_id"])
                    if task["status"] == "running" and task["job_id"] == job["job_id"]:
                        await repository.finish_task(job["team_id"], task["task_id"], job["member_id"],
                            task["version"], "completed" if status == "completed" else status,
                            result.get("summary", ""))
        finally:
            self._tasks.pop(job["job_id"], None)

    async def _schedule_team_tasks(self):
        async with self.database.connect() as connection:
            query = select(agent_teams).join(users,
                agent_teams.c.tenant_id == users.c.id).where(
                users.c.status == "active", agent_teams.c.status == "active"
            )
            if self._team_cursor:
                created, team_id = self._team_cursor
                query = query.where(or_(agent_teams.c.created_at > created,
                    and_(agent_teams.c.created_at == created, agent_teams.c.team_id > team_id)))
            teams = (await connection.execute(query.order_by(agent_teams.c.created_at, agent_teams.c.team_id).limit(100))).mappings().all()
        self._team_cursor = (teams[-1]["created_at"], teams[-1]["team_id"]) if len(teams) == 100 else None
        for row in teams:
            context = TenantContext(row["tenant_id"], row["workspace_id"], row["session_id"])
            if self.session_blocked(context) or context.tenant_id in self._revoked:
                continue
            async with self.store(context, write=True) as repository:
                team = await repository.require_team(row["team_id"])
                jobs = await repository.list_jobs(team_id=team["team_id"])
                busy = {job["member_id"] for job in jobs if job["status"] not in TERMINAL_JOBS}
                completed = {task["task_id"] for task in team["tasks"] if task["status"] == "completed"}
                for task in team["tasks"]:
                    if task["status"] != "pending" or not set(task["depends"]).issubset(completed):
                        continue
                    members = [member for member in team["members"] if member["member_id"] not in busy
                               and (task["owner_member_id"] == member["member_id"] or
                                    (task["owner_member_id"] is None and member["role"] != "leader"))]
                    if not members:
                        continue
                    member = members[0]
                    try:
                        await repository.claim_task(team["team_id"], task["task_id"], member["member_id"], task["version"])
                    except CollaborationConflict:
                        continue
                    config = member["config"]
                    try:
                        job = await repository.create_job({
                        "goal": task["objective"], "context": json.dumps({"team_objective": team["objective"],
                            "dependencies": [item for item in team["tasks"] if item["task_id"] in task["depends"]]})[:16000],
                        "profile": config["profile"], "model": config.get("model"),
                        "project_path": team["config"]["project_path"], "instructions": config.get("instructions", ""),
                        }, team_id=team["team_id"], member_id=member["member_id"], task_id=task["task_id"], **self._allocation())
                    except CollaborationConflict:
                        current = await repository._get(agent_team_tasks, "task_id", task["task_id"])
                        await repository.finish_task(team["team_id"], task["task_id"], member["member_id"], current["version"], "failed", "Team allocation limit reached")
                        await repository.update_team(team["team_id"], team["version"], status="failed")
                        break
                    await repository.bind_task_job(task["task_id"], job["job_id"])
                    busy.add(member["member_id"])
                if not busy and jobs and all(task["status"] in {"completed", "failed", "cancelled"} for task in team["tasks"]):
                    final_id = team["config"].get("final_job_id")
                    if not team["tasks"] or final_id:
                        status = "completed" if all(job["status"] == "completed" for job in jobs) else "failed"
                        report = next((job["result"].get("summary", "") for job in reversed(jobs)
                                       if job["job_id"] == final_id or not team["tasks"]), "")
                        await repository.update_team(team["team_id"], team["version"], status=status,
                            config={**team["config"], "summary": report})
                    else:
                        leader = next(member for member in team["members"] if member["role"] == "leader")
                        final = await repository.create_job({
                            "goal": ("Produce the final team report for: " + team["objective"])[:12000],
                            "context": json.dumps({"tasks": team["tasks"], "reports": [job["result"].get("summary", "") for job in jobs]})[:16000],
                            "profile": "reader", "model": leader["config"].get("model"),
                            "project_path": team["config"]["project_path"],
                            "instructions": "Synthesize results and unresolved issues. Do not create further tasks in this final report.",
                        }, team_id=team["team_id"], member_id=leader["member_id"], **self._allocation())
                        await repository.update_team(team["team_id"], team["version"],
                            config={**team["config"], "final_job_id": final["job_id"]})

    async def close(self):
        async with self._tick_lock:
            self._closed = True
            if hasattr(self.executor, "shutting_down"):
                self.executor.shutting_down = True
            tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel_session(self, context):
        self._blocked_sessions.add(self._session_key(context))
        async with self._tick_lock:
            for team in await self.list_teams(context):
                if team["status"] == "active":
                    await self.cancel_team(context, team["team_id"])
            for job in await self.list_jobs(context):
                if job["status"] not in TERMINAL_JOBS:
                    await self.cancel(context, job["job_id"])

    def release_deleted_session(self, context):
        self._blocked_sessions.discard(self._session_key(context))

    async def discard_session_artifacts(self, context):
        for job in await self.list_jobs(context):
            workspace = job["result"].get("workspace")
            if workspace is not None:
                await self.executor.manager(context).discard(workspace)

    async def revoke(self, tenant_id):
        self._revoked.add(tenant_id)
        async with self.database.connect() as connection:
            rows = (await connection.execute(select(agent_jobs.c.workspace_id, agent_jobs.c.session_id).where(
                agent_jobs.c.tenant_id == tenant_id,
            ).distinct())).all()
        for workspace_id, session_id in rows:
            await self.cancel_session(TenantContext(tenant_id, workspace_id, session_id))
