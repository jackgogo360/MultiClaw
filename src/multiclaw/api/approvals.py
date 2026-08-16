from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from multiclaw.api.dependencies import tenant_context
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import ApprovalRecord, InvalidTransitionError, VersionConflictError
from multiclaw.workflow.models import ApprovalRecord

router = APIRouter(prefix="/api")


class ApprovalDecisionRequest(BaseModel):
    approval_id: str
    approved: bool
    version: int


class ApprovalDecisionBody(BaseModel):
    approved: bool
    version: int


class ApprovalResponse(BaseModel):
    approval_id: str
    status: str
    version: int
    expires_at: int
    resolved_at: int | None = None

    @classmethod
    def from_record(cls, record: ApprovalRecord) -> "ApprovalResponse":
        return cls(
            approval_id=record.approval_id,
            status=record.status.value,
            version=record.version,
            expires_at=record.expires_at,
            resolved_at=record.resolved_at,
        )


@router.get("/approvals/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    approval_id: str,
    request: Request,
    context: TenantContext = Depends(tenant_context),
):
    record = await _lookup_scoped_approval(request, context, approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return ApprovalResponse.from_record(record)


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalResponse)
async def decide_approval(
    approval_id: str,
    body: ApprovalDecisionBody,
    request: Request,
    context: TenantContext = Depends(tenant_context),
):
    record = await _lookup_scoped_approval(request, context, approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="approval not found")
    coordinator = WorkflowCoordinator(request.app.state.database, settings=request.app.state.settings)
    try:
        resolved = await coordinator.decide_approval(
            context=record.context,
            approval_id=approval_id,
            approved=body.approved,
            version=body.version,
        )
    except InvalidTransitionError as error:
        if str(error) == "approval expired":
            raise HTTPException(status_code=410, detail="approval expired") from error
        raise HTTPException(status_code=409, detail="approval already resolved") from error
    except VersionConflictError as error:
        message = str(error)
        if message == "approval record not found":
            raise HTTPException(status_code=404, detail="approval not found") from error
        raise HTTPException(status_code=409, detail=message) from error
    return ApprovalResponse.from_record(resolved)


@router.post("/approve", include_in_schema=False)
async def approve_alias(
    req: ApprovalDecisionRequest,
    request: Request,
    context: TenantContext = Depends(tenant_context),
):
    return await decide_approval(
        req.approval_id,
        ApprovalDecisionBody(approved=req.approved, version=req.version),
        request,
        context,
    )


async def _lookup_scoped_approval(
    request: Request,
    context: TenantContext,
    approval_id: str,
) -> ApprovalRecord | None:
    async with request.app.state.database.connect() as conn:
        repository = _repository(request, conn)
        return await repository.get_workspace_approval(
            context.tenant_id,
            context.workspace_id,
            approval_id,
        )


def _repository(request: Request, conn: AsyncConnection) -> WorkflowRepository:
    return WorkflowRepository(
        conn,
        request.app.state.database.dialect,
        request.app.state.settings.workflow.heartbeat_ms,
        request.app.state.settings.workflow.lease_ttl_ms,
    )
