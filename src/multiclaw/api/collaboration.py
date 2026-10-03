"""Authenticated collaboration handles, teams and explicit change acceptance."""
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from multiclaw.api.dependencies import tenant_context
from multiclaw.collaboration.models import AgentRequest, TeamRequest
from multiclaw.collaboration.repository import CollaborationConflict, CollaborationNotFound
from multiclaw.collaboration.tools import TeamTask, public_job
from multiclaw.security.redaction import redact
from multiclaw.storage.repositories.sessions import SessionRepository


router = APIRouter(prefix="/api")


class Scoped(BaseModel):
    session_id: str = Field(min_length=36, max_length=36)


class AgentPost(AgentRequest):
    session_id: str = Field(min_length=36, max_length=36)


class TeamPost(TeamRequest):
    session_id: str = Field(min_length=36, max_length=36)


class TaskPost(TeamTask):
    session_id: str = Field(min_length=36, max_length=36)


class MessagePost(Scoped):
    message: str = Field(min_length=1, max_length=8000)


class TeamMessagePost(Scoped):
    content: str = Field(min_length=1, max_length=8000)
    recipient_id: str | None = None


class AcceptPost(Scoped):
    digest: str = Field(min_length=64, max_length=64)


def _service(request):
    service = getattr(request.app.state, "collaboration", None)
    if service is None or not request.app.state.settings.collaboration.enabled:
        raise HTTPException(503, "Collaboration is disabled")
    return service


async def _scope(request, context, session_id):
    async with request.app.state.database.connect() as connection:
        session = await SessionRepository(connection, context, request.app.state.database.dialect).get(session_id)
    if session is None or session.metadata.get("kind") == "agent_job":
        raise HTTPException(404, "session not found")
    if request.method != "GET" and session.status.value == "archived":
        raise HTTPException(409, "session is archived")
    service = getattr(request.app.state, "collaboration", None)
    if request.method != "GET" and service is not None and service.session_blocked(context.for_session(session_id)):
        raise HTTPException(409, "session is being deleted")
    return context.for_session(session_id)


def _failure(error):
    if isinstance(error, CollaborationNotFound):
        return HTTPException(404, "collaboration resource not found")
    if isinstance(error, CollaborationConflict):
        return HTTPException(409, str(error))
    if str(error) == "writer assignments require a clean Git repository at project_path":
        return HTTPException(422, str(error))
    if str(error).startswith("Batch changes overlap"):
        return HTTPException(409, "Team changes overlap; resolve file ownership before acceptance")
    return HTTPException(422, "Collaboration request is invalid or changes cannot be accepted")


@router.get("/collaboration")
async def overview(session_id: str, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, session_id)
    if not request.app.state.settings.collaboration.enabled:
        return {"enabled": False, "jobs": [], "teams": []}
    service = _service(request)
    return {"enabled": True, "jobs": [public_job(job) for job in await service.list_jobs(context)],
            "teams": redact(await service.list_teams(context))}


@router.post("/agents")
async def spawn(body: AgentPost, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, body.session_id)
    try:
        return public_job(await _service(request).spawn(context, body.model_dump(exclude={"session_id"})))
    except (CollaborationNotFound, CollaborationConflict, ValueError) as error:
        raise _failure(error) from None


@router.get("/agents/{job_id}")
async def get_job(job_id: str, session_id: str, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, session_id)
    try:
        return public_job(await _service(request).get_job(context, job_id))
    except CollaborationNotFound as error:
        raise _failure(error) from None


@router.post("/agents/{job_id}/cancel")
async def cancel(job_id: str, body: Scoped, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, body.session_id)
    try:
        return public_job(await _service(request).cancel(context, job_id))
    except (CollaborationNotFound, CollaborationConflict) as error:
        raise _failure(error) from None


@router.post("/agents/{job_id}/steer")
async def steer(job_id: str, body: MessagePost, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, body.session_id)
    try:
        return public_job(await _service(request).steer(context, job_id, body.message))
    except (CollaborationNotFound, CollaborationConflict, ValueError) as error:
        raise _failure(error) from None


@router.get("/agents/{job_id}/transcript")
async def transcript(job_id: str, session_id: str, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, session_id)
    try:
        job = await _service(request).get_job(context, job_id)
    except CollaborationNotFound as error:
        raise _failure(error) from None
    if not job["child_session_id"]:
        return {"messages": [], "approvals": [], "progress": []}
    child = context.for_session(job["child_session_id"])
    async with request.app.state.database.connect() as connection:
        repository = SessionRepository(connection, child, request.app.state.database.dialect)
        approvals = await repository.list_pending_approvals(child.session_id)
        from multiclaw.storage.repositories.memory import MemoryRepository
        events = await MemoryRepository(connection, child, request.app.state.database.dialect).recent(100, entry_type="run_event")
        progress = []
        for event in reversed(events):
            for line in event.content.splitlines():
                if not line.startswith("data: "):
                    continue
                try:
                    part = json.loads(line[6:])
                except ValueError:
                    continue
                if part.get("type") == "data-agent-progress":
                    item = part.get("data", {})
                    if item.get("type") == "token":
                        progress.append({"type": "token", "content": str(item.get("content", ""))[:2000]})
                    elif item.get("type") == "tool_call":
                        progress.append({"type": "tool_call", "name": str(item.get("name", ""))[:128]})
        return {"messages": await repository.get_messages(child.session_id, limit=200),
                "progress": redact(progress[-100:]),
                "approvals": redact([{
                    "approval_id": approval["approval_id"], "version": approval["version"],
                    "status": approval["approval_status"], "tool_name": approval["tool_name"],
                    "tool_call_id": approval["tool_call_id"],
                    "tool_input": json.loads(approval["input_payload_json"]),
                } for approval in approvals])}


@router.get("/agents/{job_id}/changes")
async def changes(job_id: str, session_id: str, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, session_id)
    try:
        service = _service(request)
        job = await service.get_job(context, job_id)
        workspace = job["result"].get("workspace")
        if workspace is None or job["status"] != "completed":
            raise CollaborationConflict("completed writable assignment required")
        return await service.executor.manager(context).diff(workspace)
    except (CollaborationNotFound, CollaborationConflict, ValueError) as error:
        raise _failure(error) from None


@router.post("/agents/{job_id}/accept")
async def accept(job_id: str, body: AcceptPost, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, body.session_id)
    try:
        service = _service(request)
        job = await service.get_job(context, job_id)
        if job["team_id"]:
            raise CollaborationConflict("accept member changes through the completed Team review")
        if job["result"].get("accepted_digest") == body.digest:
            return {"accepted": True, "digest": body.digest}
        workspace = job["result"].get("workspace")
        if workspace is None or job["status"] != "completed":
            raise CollaborationConflict("completed writable assignment required")
        result = await service.executor.manager(context).accept(workspace, body.digest)
        async with service.store(context, write=True) as repository:
            current = await repository.get_job(job_id)
            await repository.update_job(job_id, current["version"], result={**current["result"], "accepted_digest": body.digest})
        return result
    except (CollaborationNotFound, CollaborationConflict, ValueError) as error:
        raise _failure(error) from None


@router.post("/teams")
async def create_team(body: TeamPost, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, body.session_id)
    try:
        return redact(await _service(request).create_team(context, body.model_dump(exclude={"session_id"})))
    except (CollaborationNotFound, CollaborationConflict, ValueError) as error:
        raise _failure(error) from None


@router.get("/teams/{team_id}")
async def get_team(team_id: str, session_id: str, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, session_id)
    try:
        return redact(await _service(request).get_team(context, team_id))
    except CollaborationNotFound as error:
        raise _failure(error) from None


@router.post("/teams/{team_id}/cancel")
async def cancel_team(team_id: str, body: Scoped, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, body.session_id)
    try:
        return redact(await _service(request).cancel_team(context, team_id))
    except (CollaborationNotFound, CollaborationConflict) as error:
        raise _failure(error) from None


@router.post("/teams/{team_id}/tasks")
async def task_create(team_id: str, body: TaskPost, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, body.session_id)
    try:
        async with _service(request).store(context, write=True) as repository:
            return redact(await repository.create_task(team_id, body.title, body.objective,
                depends_on=body.depends_on, assigned_member_id=body.assigned_member_id))
    except (CollaborationNotFound, CollaborationConflict, ValueError) as error:
        raise _failure(error) from None


@router.get("/teams/{team_id}/messages")
async def messages(team_id: str, session_id: str, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, session_id)
    try:
        async with _service(request).store(context) as repository:
            return redact(await repository.messages(team_id))
    except CollaborationNotFound as error:
        raise _failure(error) from None


@router.post("/teams/{team_id}/messages")
async def message_send(team_id: str, body: TeamMessagePost, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, body.session_id)
    try:
        async with _service(request).store(context, write=True) as repository:
            return redact(await repository.send_message(team_id, None, body.recipient_id, body.content))
    except (CollaborationNotFound, CollaborationConflict, ValueError) as error:
        raise _failure(error) from None


async def _team_workspaces(service, context, team_id):
    team = await service.get_team(context, team_id)
    if team["status"] != "completed":
        raise CollaborationConflict("completed Team required for change acceptance")
    async with service.store(context) as repository:
        jobs = await repository.list_jobs(team_id=team_id)
    workspaces = [job["result"]["workspace"] for job in jobs if job["status"] == "completed" and job["result"].get("workspace")]
    if not workspaces:
        raise CollaborationConflict("this Team has no writable changes")
    return team, workspaces


@router.get("/teams/{team_id}/changes")
async def team_changes(team_id: str, session_id: str, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, session_id)
    try:
        service = _service(request)
        _, workspaces = await _team_workspaces(service, context, team_id)
        return await service.executor.manager(context).diff_batch(workspaces)
    except (CollaborationNotFound, CollaborationConflict, ValueError) as error:
        raise _failure(error) from None


@router.post("/teams/{team_id}/accept")
async def team_accept(team_id: str, body: AcceptPost, request: Request, context=Depends(tenant_context)):
    context = await _scope(request, context, body.session_id)
    try:
        service = _service(request)
        team, workspaces = await _team_workspaces(service, context, team_id)
        if team["config"].get("accepted_digest") == body.digest:
            return {"accepted": True, "digest": body.digest}
        result = await service.executor.manager(context).accept_batch(workspaces, body.digest)
        async with service.store(context, write=True) as repository:
            current = await repository.require_team(team_id)
            await repository.update_team(team_id, current["version"], config={**current["config"], "accepted_digest": body.digest})
        return result
    except (CollaborationNotFound, CollaborationConflict, ValueError) as error:
        raise _failure(error) from None
