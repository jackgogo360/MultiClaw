from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from multiclaw.api.dependencies import tenant_uow
from multiclaw.auth.middleware import require_recent_auth
from multiclaw.observability import increment_metric, record_trace_event
from multiclaw.storage.uow import TenantUnitOfWork


router = APIRouter(prefix="/api")


class SessionCreateRequest(BaseModel):
    title: str = "New Chat"


class SessionRenameRequest(BaseModel):
    title: str


@router.get("/sessions")
async def list_sessions(
    include_archived: bool = False,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    sessions = await uow.sessions.list(include_archived=include_archived)
    return [session.model_dump(mode="json") for session in sessions]


@router.post("/sessions")
async def create_session(
    req: SessionCreateRequest,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.create(title=req.title)
    return session.model_dump(mode="json")


@router.patch("/sessions/{session_id}")
async def rename_session(
    session_id: str,
    req: SessionRenameRequest,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    renamed = await uow.sessions.rename(session_id, req.title)
    assert renamed is not None
    return renamed.model_dump(mode="json")


@router.post("/sessions/{session_id}/archive")
async def archive_session(
    session_id: str,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    archived = await uow.sessions.archive(session_id)
    assert archived is not None
    return archived.model_dump(mode="json")


@router.post("/sessions/{session_id}/restore")
async def restore_session(
    session_id: str,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    restored = await uow.sessions.restore(session_id)
    assert restored is not None
    return restored.model_dump(mode="json")


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    _recent_user=Depends(require_recent_auth),
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    del _recent_user
    session = await uow.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    await uow.sessions.delete(session_id)
    return {"ok": True}


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = 50,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.get(session_id)
    if session is None:
        increment_metric(
            "multiclaw_scope_fk_rejections_total",
            labels={"backend": "unknown", "operation": "session_messages", "status": "error", "error_class": "scope_fk_rejection"},
        )
        record_trace_event("scope_fk_rejection", attributes={"operation": "session_messages"})
        raise HTTPException(status_code=404, detail="session not found")
    return await uow.sessions.get_messages(session_id, limit)
