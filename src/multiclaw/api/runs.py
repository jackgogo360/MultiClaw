"""Authenticated, session-scoped durable Plan run routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
from multiclaw.runtime.background import background_manager, background_response
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
    await background_manager(request).cancel(run_context)
    current = await WorkflowCoordinator(request.app.state.database, settings=request.app.state.settings).get_run(run_context)
    if current is not None:
        persisted = current

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

    async with background_manager(request).session_admission(run_context):
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

        return await background_response(request, run_context, stream(), runtime_lease=runtime_lease, run_lease_handle=handle)


@router.get('/sessions/{session_id}/runs', response_model=list[RunResponse])
async def list_session_runs(
    session_id: str, request: Request,
    context: TenantContext = Depends(tenant_context),
):
    from sqlalchemy import select
    from multiclaw.storage.schema import agent_runs

    async for uow, session_context in _scoped_uow(request, context, session_id):
        rows = await uow.conn.execute(select(agent_runs.c.run_id).where(
            agent_runs.c.tenant_id == context.tenant_id,
            agent_runs.c.workspace_id == context.workspace_id,
            agent_runs.c.session_id == session_id,
        ).order_by(agent_runs.c.created_at.desc()).limit(100))
        responses = []
        for run_id in rows.scalars():
            run = await uow.workflow.get_run(session_context.for_run(session_id, str(run_id)))
            if run is not None:
                responses.append(await build_run_response(uow, run))
        return responses
    raise AssertionError('scoped UoW did not yield')


@router.get('/runs/{run_id}/events')
async def reconnect_run(
    run_id: str, session_id: str, request: Request, cursor: int = 0,
    context: TenantContext = Depends(tenant_context),
):
    async for uow, session_context in _scoped_uow(request, context, session_id):
        run_context = session_context.for_run(session_id, run_id)
        if await uow.workflow.get_run(run_context) is None:
            raise _not_found()
    last_event = request.headers.get('last-event-id')
    if last_event:
        try:
            cursor = max(cursor, int(last_event))
        except ValueError:
            raise HTTPException(422, 'invalid event cursor') from None
    if cursor < 0:
        raise HTTPException(422, 'invalid event cursor')
    return StreamingResponse(background_manager(request).events(run_context, cursor),
        media_type='text/event-stream', headers={'X-Vercel-AI-Data-Stream': 'v1'})


class SteeringRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=100000)


@router.post('/runs/{run_id}/steer')
async def steer_run(
    run_id: str, body: SteeringRequest, request: Request,
    context: TenantContext = Depends(tenant_context),
):
    async for uow, session_context in _scoped_uow(request, context, body.session_id):
        run_context = session_context.for_run(body.session_id, run_id)
        if await uow.workflow.get_run(run_context) is None:
            raise _not_found()
    await background_manager(request).steer(run_context, body.message)
    return {'run_id': run_id, 'status': 'accepted'}


@router.get('/runs/{run_id}/usage')
async def get_run_usage(
    run_id: str, session_id: str, request: Request,
    context: TenantContext = Depends(tenant_context),
):
    async for uow, session_context in _scoped_uow(request, context, session_id):
        if await uow.workflow.get_run(session_context.for_run(session_id, run_id)) is None:
            raise _not_found()
        from uuid import NAMESPACE_URL, uuid5
        usage_id = str(uuid5(NAMESPACE_URL, 'multiclaw:run-usage:' + ':'.join((context.tenant_id, context.workspace_id, run_id))))
        entry = await uow.memory.get(usage_id, session_id)
        if entry is not None and entry.type == 'run_usage' and entry.metadata.get('run_id') == run_id:
            return {key: value for key, value in entry.metadata.items() if key not in {'task_context'}}
        settings = request.app.state.settings.runtime
        return {'run_id': run_id, 'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
            'model_calls': 0, 'estimated': False, 'estimated_cost': None, 'cost_complete': False,
            'limits': {'max_run_tokens': settings.max_run_tokens,
                'max_run_seconds': settings.max_run_seconds,
                'tenant_daily_token_limit': settings.tenant_daily_token_limit}}
    raise AssertionError('scoped UoW did not yield')


class QueueRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100000)


@router.post('/sessions/{session_id}/queue')
async def queue_message(
    session_id: str, body: QueueRequest, request: Request,
    context: TenantContext = Depends(tenant_context),
):
    async for uow, session_context in _scoped_uow(request, context, session_id):
        session = await uow.sessions.get(session_id)
        if session.status.value == 'archived':
            raise HTTPException(409, 'session is archived')
    manager = background_manager(request)

    async def setup():
        from multiclaw.api.chat import ChatRequest, _chat
        from multiclaw.storage.uow import TenantUnitOfWork
        async with TenantUnitOfWork(request.app.state.database, context,
                planning_settings=request.app.state.settings.planning,
                workflow_settings=request.app.state.settings.workflow) as queued_uow:
            user = await queued_uow.users.get_current()
            workspace = await queued_uow.workspaces.get_current()
            if user.status != 'active' or workspace.status != 'active':
                return
            response = await _chat(ChatRequest(session_id=session_id, message=body.message),
                request, context, queued_uow)
    async def execute():
        response = await manager.run_setup(session_context, setup)
        # Consume the HTTP view only to await this run before starting the next.
        # Cancellation of this queue worker cannot cancel the detached producer.
        async for _chunk in response.body_iterator:
            pass
        run_id = response.headers.get('x-run-id')
        if not run_id:
            return {'run_id': None, 'status': 'completed'}
        from multiclaw.workflow.models import RunStatus
        run = await WorkflowCoordinator(request.app.state.database, settings=request.app.state.settings).get_run(context.for_run(session_id, run_id))
        status = 'completed'
        if run is not None:
            if run.status is RunStatus.AWAITING_USER:
                status = 'awaiting_user'
            elif run.status is RunStatus.CANCELLED:
                status = 'cancelled'
            elif run.status is not RunStatus.COMPLETED:
                status = 'failed'
        return {'run_id': run_id, 'status': status}

    queue_id = await manager.queue_message(session_context, execute, message=body.message)
    return {'queue_id': queue_id, 'session_id': session_id, 'status': 'queued'}


@router.get('/sessions/{session_id}/queue')
async def list_queued_messages(session_id: str, request: Request,
        context: TenantContext = Depends(tenant_context)):
    async for _uow, session_context in _scoped_uow(request, context, session_id):
        pass
    return await background_manager(request).list_queue(session_context)
