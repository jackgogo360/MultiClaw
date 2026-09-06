"""Authenticated, session-scoped durable Plan run routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from multiclaw.api.dependencies import tenant_context
from multiclaw.api.plans import (
    _not_found,
    _scoped_uow,
    _terminalize_cancelled_stream,
    _terminalize_stream_error,
    build_run_response,
)
from multiclaw.planner.models import (
    PlanExecutionBlocked,
    RunResponse,
    SessionScopedRequest,
)
from multiclaw.runtime.pool import RuntimeUnavailableError
from multiclaw.stream import DataStreamEncoder
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import RunLeaseHandle, RunStatus, StaleFenceError

router = APIRouter(prefix="/api")


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    session_id: str,
    request: Request,
    context: TenantContext = Depends(tenant_context),  # noqa: B008
):
    async for uow, session_context in _scoped_uow(request, context, session_id):
        run = await uow.workflow.get_run(session_context.for_run(session_id, run_id))
        if run is None:
            raise _not_found()
        return await build_run_response(uow, run)
    raise AssertionError("scoped UoW did not yield")


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    body: SessionScopedRequest,
    request: Request,
    context: TenantContext = Depends(tenant_context),  # noqa: B008
):
    async for uow, session_context in _scoped_uow(request, context, body.session_id):
        run_context = session_context.for_run(body.session_id, run_id)
        if await uow.workflow.get_run(run_context) is None:
            raise _not_found()

    persisted = await WorkflowCoordinator(
        request.app.state.database, settings=request.app.state.settings
    ).request_cancellation(run_context)

    async def stream() -> AsyncIterator[str]:
        encoder = DataStreamEncoder()
        yield encoder.run_metadata(body.session_id, run_id)
        yield encoder.run_status(
            {
                "run_id": run_id,
                "status": persisted.status.value,
                "cancel_requested_at": persisted.cancel_requested_at,
            }
        )
        yield encoder.finish("stop")

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/runs/{run_id}/summary/retry")
async def retry_final_summary(
    run_id: str,
    body: SessionScopedRequest,
    request: Request,
    context: TenantContext = Depends(tenant_context),  # noqa: B008
):
    async for uow, session_context in _scoped_uow(request, context, body.session_id):
        run_context = session_context.for_run(body.session_id, run_id)
        if await uow.workflow.get_run(run_context) is None:
            raise _not_found()

    runtime = await request.app.state.runtime_pool.acquire(run_context)
    coordinator = getattr(runtime, "plan_execution", None)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="runtime temporarily unavailable")
    try:
        lease = await coordinator.resume_waiting_summary_run(
            context=run_context,
            runtime_instance_id=runtime.runtime_instance_id,
        )
    except (PlanExecutionBlocked, StaleFenceError) as error:
        raise HTTPException(status_code=409, detail="summary retry is unavailable") from error

    try:
        runtime_lease = runtime.begin_run()
    except RuntimeError as error:
        await WorkflowCoordinator(
            request.app.state.database, settings=request.app.state.settings
        ).transition_run(lease, RunStatus.AWAITING_USER)
        raise RuntimeUnavailableError(
            request.app.state.runtime_pool.idle_ttl_ms // 1000 or 1
        ) from error
    handle = RunLeaseHandle(lease)

    async def stream() -> AsyncIterator[str]:
        encoder = DataStreamEncoder()
        try:
            yield encoder.run_metadata(body.session_id, run_id)
            outcome = await coordinator.complete_final_summary(
                context=run_context,
                run_lease_handle=handle,
                runner=runtime.agent,
            )
            yield encoder.run_status(
                {"run_id": run_id, "status": outcome.run.status.value}
            )
            yield encoder.finish("stop")
        except asyncio.CancelledError:
            await _terminalize_cancelled_stream(request, handle)
            raise
        except Exception:  # noqa: BLE001 - SSE must terminalize any internal failure.
            await _terminalize_stream_error(request, handle)
            yield encoder.error("Plan summary retry failed")
            yield encoder.finish("error")
        finally:
            runtime_lease.close()

    return StreamingResponse(stream(), media_type="text/event-stream")
