from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.exc import NoResultFound

from multiclaw.auth.models import UserRecord
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext


async def current_user(request: Request) -> UserRecord:
    user = getattr(request.state, "authenticated_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


async def tenant_context(
    request: Request,
    user: UserRecord = Depends(current_user),
) -> TenantContext:
    if user.status != "active" or user.default_workspace_id is None:
        raise HTTPException(status_code=403, detail="Account unavailable")
    return TenantContext(
        tenant_id=user.id,
        workspace_id=user.default_workspace_id,
        request_started_at_ms=getattr(request.state, "request_started_at_ms", 0),
    )


async def tenant_uow(
    request: Request,
    context: TenantContext = Depends(tenant_context),
) -> AsyncIterator[TenantUnitOfWork]:
    async with TenantUnitOfWork(request.app.state.database, context) as uow:
        try:
            user = await uow.users.get_current()
            workspace = await uow.workspaces.get_current()
        except NoResultFound as exc:
            raise HTTPException(status_code=403, detail="Account unavailable") from exc

        if user.status != "active" or workspace.status != "active":
            raise HTTPException(status_code=403, detail="Account unavailable")
        yield uow
