"""Parent delegation and member communication tools with server-bound identity."""
import asyncio
import json

from pydantic import BaseModel, Field

from multiclaw.collaboration.models import AgentRequest, TeamRequest
from multiclaw.collaboration.repository import CollaborationConflict
from multiclaw.security.redaction import redact
from multiclaw.tools.base import ToolBuilder, ToolExecutionResult, ToolInvocation, ToolStatus
from multiclaw.workflow.models import RecoveryStrategy


def public_job(job):
    result = {**job, "result": {key: value for key, value in job["result"].items() if key != "workspace"}}
    return redact(result)


class JobId(BaseModel):
    job_id: str = Field(min_length=36, max_length=36)


class JobMessage(JobId):
    message: str = Field(min_length=1, max_length=8000)


class JobWait(JobId):
    timeout: float = Field(default=20, gt=0, le=25)


class TeamId(BaseModel):
    team_id: str = Field(min_length=36, max_length=36)


class TeamMessage(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    recipient_id: str | None = None


class TeamTask(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=12000)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    assigned_member_id: str | None = None


class Empty(BaseModel):
    pass


class _Invocation(ToolInvocation):
    def __init__(self, name, params, callback):
        super().__init__(name, params)
        self.callback = callback

    async def execute(self):
        try:
            result = await self.callback(self.params)
        except asyncio.CancelledError:
            raise
        except Exception:
            return ToolExecutionResult(status=ToolStatus.ERROR, content="Collaboration action could not be completed")
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=json.dumps(redact(result), ensure_ascii=False), data=redact(result) if isinstance(result, dict) else {"items": redact(result)})


class CollaborationTool(ToolBuilder):
    def __init__(self, name, description, parameters_schema, callback, *, read_only=False):
        self.name, self.description, self.parameters_schema = name, description, parameters_schema
        self.callback, self.read_only = callback, read_only
        self.recovery_strategy = RecoveryStrategy.READ_ONLY_REPLAY if read_only else RecoveryStrategy.MANUAL_UNCERTAIN

    def validate(self, params):
        return self.parameters_schema.model_validate(params)

    def build(self, params):
        return _Invocation(self.name, params, self.callback)


def _parent_context():
    from multiclaw.runtime.inference import current_inference_budget
    from multiclaw.runtime.run_control import current_run_control
    budget, control = current_inference_budget(), current_run_control.get()
    if budget is None or budget.context.run_id is None:
        raise CollaborationConflict("an active parent Run is required")
    if control is not None and control.context != budget.context:
        raise CollaborationConflict("parent context mismatch")
    return budget.context


def register_parent_tools(registry, service):
    async def spawn(params):
        parent = _parent_context()
        return public_job(await service.spawn(parent.for_session(parent.session_id), params.model_dump(), parent_run_id=parent.run_id))

    async def status(params):
        parent = _parent_context()
        return public_job(await service.get_job(parent.for_session(parent.session_id), params.job_id))

    async def message(params):
        parent = _parent_context()
        return public_job(await service.steer(parent.for_session(parent.session_id), params.job_id, params.message))

    async def cancel(params):
        parent = _parent_context()
        return public_job(await service.cancel(parent.for_session(parent.session_id), params.job_id))

    async def wait(params):
        parent = _parent_context()
        context = parent.for_session(parent.session_id)
        try:
            return public_job(await service.wait(context, params.job_id, timeout=params.timeout))
        except TimeoutError:
            return public_job(await service.get_job(context, params.job_id))

    async def create_team(params):
        parent = _parent_context()
        return await service.create_team(parent.for_session(parent.session_id), params.model_dump())

    async def team_status(params):
        parent = _parent_context()
        return await service.get_team(parent.for_session(parent.session_id), params.team_id)

    for name, description, schema, callback, read in (
        ("spawn_agent", "Start an independent durable assignment; returns a handle immediately. Writers use isolated Git worktrees.", AgentRequest, spawn, False),
        ("agent_status", "Read an assignment status, usage and final report.", JobId, status, True),
        ("agent_message", "Send a follow-up instruction to an active child assignment.", JobMessage, message, False),
        ("agent_cancel", "Cancel an owned assignment.", JobId, cancel, False),
        ("agent_wait", "Wait briefly for a child report; returns current status on timeout.", JobWait, wait, True),
        ("create_team", "Create a durable team with a leader, shared tasks and direct member messaging.", TeamRequest, create_team, False),
        ("team_status", "Inspect a team's members and shared task board.", TeamId, team_status, True),
    ):
        registry.register(CollaborationTool(name, description, schema, callback, read_only=read))


def register_member_tools(registry, service, parent_context, job):
    team_id, member_id = job["team_id"], job["member_id"]

    async def board(params):
        return await service.get_team(parent_context, team_id)

    async def inbox(params):
        async with service.store(parent_context) as repository:
            return await repository.messages(team_id, member_id)

    async def message(params):
        async with service.store(parent_context, write=True) as repository:
            return await repository.send_message(team_id, member_id, params.recipient_id, params.content)

    async def task(params):
        async with service.store(parent_context, write=True) as repository:
            member = await repository.require_member(team_id, member_id)
            if member["role"] != "leader":
                raise CollaborationConflict("only the leader can assign tasks")
            return await repository.create_task(team_id, params.title, params.objective,
                depends_on=params.depends_on, assigned_member_id=params.assigned_member_id)

    async def changes(params):
        child = await service.get_job(parent_context, params.job_id)
        if child["team_id"] != team_id or child["status"] != "completed" or not child["result"].get("workspace"):
            raise CollaborationConflict("completed writer in this team required")
        return await service.executor.manager(parent_context).diff(child["result"]["workspace"])

    registry.register(CollaborationTool("team_board", "Read your team members, task IDs, dependencies and reports.", Empty, board, read_only=True))
    registry.register(CollaborationTool("team_inbox", "Read direct and broadcast messages addressed to you.", Empty, inbox, read_only=True))
    registry.register(CollaborationTool("team_message", "Send a direct message or broadcast within your team.", TeamMessage, message))
    # The callback verifies leader identity again; arguments cannot override it.
    registry.register(CollaborationTool("team_create_task", "Leader: create and optionally assign a shared task.", TeamTask, task))
    registry.register(CollaborationTool("team_changes", "Review a completed member's isolated changes by job ID from the board.", JobId, changes, read_only=True))
