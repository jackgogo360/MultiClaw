"""Authenticated, session-scoped Plan control-plane routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import asdict
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from multiclaw.api.dependencies import tenant_context
from multiclaw.planner.models import (
    PlanDecisionAction,
    PlanDecisionBody,
    PlanDecisionIdempotencyError,
    PlanDecisionRequest,
    PlanDecisionResponse,
    PlanNotFoundError,
    PlanResponse,
    PlanStepAttemptResponse,
    PlanStepResponse,
    PlanVersionConflictError,
    PlanVersionResponse,
    RunResponse,
    RunSummaryResponse,
    SessionScopedRequest,
)
from multiclaw.planner.service import PlanningService
from multiclaw.planner.validation import sanitize_plan_text
from multiclaw.runtime.pool import RuntimeUnavailableError
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.stream import DataStreamEncoder
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import RunLeaseHandle, RunStatus, TenantRunQuotaError

router = APIRouter(prefix="/api")
_NOT_FOUND = "resource not found"
_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED_TERMINAL,
        RunStatus.CANCELLED,
        RunStatus.BLOCKED_CORRUPT,
        RunStatus.BLOCKED_INCOMPATIBLE,
    }
)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_NOT_FOUND)


async def _require_session(uow: TenantUnitOfWork, session_id: str) -> None:
    if await uow.sessions.get(session_id) is None:
        raise _not_found()


def _step_response(step, dependency_ids: tuple[str, ...]) -> PlanStepResponse:
    return PlanStepResponse(
        step_id=step.step_id,
        logical_step_key=step.logical_step_key,
        supersedes_step_id=step.supersedes_step_id,
        ordinal=step.ordinal,
        title=step.title,
        description=step.description,
        expected_outcome=step.expected_outcome,
        assigned_agent_profile_id=step.assigned_agent_profile_id,
        max_attempts=step.max_attempts,
        definition_digest=step.definition_digest,
        dependency_ids=dependency_ids,
    )


def _attempt_response(attempt) -> PlanStepAttemptResponse:
    return PlanStepAttemptResponse(
        step_run_id=attempt.step_run_id,
        plan_version=attempt.plan_version,
        step_id=attempt.step_id,
        attempt=attempt.attempt,
        status=attempt.status,
        result_summary=attempt.result_summary,
        result_digest=attempt.result_digest,
        error_code=attempt.error_code,
        error_detail_redacted=attempt.error_detail_redacted,
        reused_from_step_run_id=attempt.reused_from_step_run_id,
        version=attempt.version,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )


async def _summary_available(uow: TenantUnitOfWork, run_id: str) -> bool:
    return await uow.memory.has_final_summary(run_id)


async def build_run_response(
    uow: TenantUnitOfWork,
    run,
) -> RunResponse:
    attempts: tuple[PlanStepAttemptResponse, ...] = ()
    if run.plan_id is not None and run.active_plan_version is not None:
        latest = await uow.plans.latest_step_attempts(
            plan_id=run.plan_id,
            plan_version=run.active_plan_version,
            run_id=str(run.context.run_id),
        )
        attempts = tuple(
            _attempt_response(item)
            for item in sorted(latest.values(), key=lambda item: (item.step_id, item.attempt))
        )
    return RunResponse(
        run_id=str(run.context.run_id),
        session_id=str(run.context.session_id),
        plan_id=run.plan_id,
        initial_plan_version=run.initial_plan_version,
        active_plan_version=run.active_plan_version,
        status=run.status,
        cancel_requested_at=run.cancel_requested_at,
        version=run.version,
        created_at=run.created_at,
        updated_at=run.updated_at,
        finished_at=run.finished_at,
        final_summary_available=await _summary_available(uow, str(run.context.run_id)),
        latest_attempts=attempts,
    )


async def build_plan_response(
    uow: TenantUnitOfWork,
    plan,
) -> PlanResponse:
    runs = await uow.workflow.list_plan_runs(plan.context, plan.plan_id)
    run_summaries = []
    for run in runs:
        run_summaries.append(
            RunSummaryResponse(
                run_id=str(run.context.run_id),
                status=run.status,
                initial_plan_version=run.initial_plan_version,
                active_plan_version=run.active_plan_version,
                cancel_requested_at=run.cancel_requested_at,
                final_summary_available=await _summary_available(
                    uow, str(run.context.run_id)
                ),
            )
        )
    latest_attempts: tuple[PlanStepAttemptResponse, ...] = ()
    if runs and runs[0].active_plan_version is not None:
        latest = await uow.plans.latest_step_attempts(
            plan_id=plan.plan_id,
            plan_version=runs[0].active_plan_version,
            run_id=str(runs[0].context.run_id),
        )
        latest_attempts = tuple(
            _attempt_response(item)
            for item in sorted(latest.values(), key=lambda item: (item.step_id, item.attempt))
        )
    return PlanResponse(
        plan_id=plan.plan_id,
        session_id=str(plan.context.session_id),
        source_message_id=plan.source_message_id,
        trigger_mode=plan.trigger_mode,
        status=plan.status,
        current_version=plan.current_version,
        approved_version=plan.approved_version,
        aggregate_version=plan.aggregate_version,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        versions=tuple(
            PlanVersionResponse(
                plan_version=version.plan_version,
                objective=version.objective,
                constraints=version.constraints,
                generation_reason=version.generation_reason,
                parent_version=version.parent_version,
                revision_feedback=version.revision_feedback,
                schema_version=version.schema_version,
                content_digest=version.content_digest,
                created_at=version.created_at,
                steps=tuple(
                    _step_response(
                        step, version.dependencies.get(step.step_id, ())
                    )
                    for step in version.steps
                ),
            )
            for version in plan.versions
        ),
        decisions=tuple(
            PlanDecisionResponse(**asdict(decision)) for decision in plan.decisions
        ),
        runs=tuple(run_summaries),
        latest_attempts=latest_attempts,
    )


async def _load_scoped_plan(
    uow: TenantUnitOfWork,
    session_context: TenantContext,
    plan_id: str,
):
    plan = await uow.plans.for_context(session_context).get(plan_id)
    if plan is None:
        raise _not_found()
    return plan


async def _terminalize_stream_error(
    request: Request,
    handle: RunLeaseHandle,
) -> None:
    """Do not leave a live mutation stream holding a durable run lease."""
    with suppress(Exception):
        await handle.refresh(
            lambda lease: WorkflowCoordinator(
                request.app.state.database, settings=request.app.state.settings
            ).finish_run_with_checkpoint(lease, RunStatus.FAILED_TERMINAL)
        )


async def _terminalize_cancelled_stream(
    request: Request,
    handle: RunLeaseHandle,
) -> None:
    """Finish durable cleanup before propagating an SSE cancellation."""
    cleanup_task = asyncio.create_task(_terminalize_stream_error(request, handle))
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
    await cleanup_task


async def _decision_conflict_response(
    request: Request,
    context: TenantContext,
    session_id: str,
    plan_id: str,
) -> JSONResponse:
    async for uow, session_context in _scoped_uow(request, context, session_id):
        latest = await build_plan_response(
            uow,
            await _load_scoped_plan(uow, session_context, plan_id),
        )
        return JSONResponse(
            {
                "detail": {
                    "code": "plan_decision_conflict",
                    "latest": latest.model_dump(mode="json"),
                }
            },
            status_code=409,
        )
    raise AssertionError("scoped UoW did not yield")


def _decision_replay_response(
    body: PlanDecisionBody,
    replay,
    run,
) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        encoder = DataStreamEncoder()
        yield encoder.run_metadata(body.session_id, str(run.context.run_id))
        if replay.decision.action is PlanDecisionAction.REVISE:
            yield encoder.plan_revised(
                {
                    "plan_id": replay.snapshot.plan_id,
                    "current_version": replay.snapshot.current_version,
                }
            )
        yield encoder.plan_decision(
            {
                "plan_id": replay.snapshot.plan_id,
                "decision": PlanDecisionResponse(
                    **asdict(replay.decision)
                ).model_dump(mode="json"),
                "idempotent_replay": True,
            }
        )
        yield encoder.finish("stop")

    return StreamingResponse(stream(), media_type="text/event-stream")


async def _scoped_uow(
    request: Request,
    context: TenantContext,
    session_id: str,
) -> AsyncIterator[tuple[TenantUnitOfWork, TenantContext]]:
    session_context = context.for_session(session_id)
    async with TenantUnitOfWork(
        request.app.state.database,
        session_context,
        planning_settings=request.app.state.settings.planning,
        workflow_settings=request.app.state.settings.workflow,
    ) as uow:
        await _require_session(uow, session_id)
        yield uow, session_context


@router.get("/sessions/{session_id}/plans", response_model=list[PlanResponse])
async def list_session_plans(
    session_id: str,
    request: Request,
    context: TenantContext = Depends(tenant_context),  # noqa: B008
):
    async for uow, session_context in _scoped_uow(request, context, session_id):
        summaries = await uow.plans.for_context(session_context).list_for_session()
        plans = []
        for summary in summaries:
            plan = await uow.plans.for_context(session_context).get(summary.plan_id)
            if plan is not None:
                plans.append(await build_plan_response(uow, plan))
        return plans
    raise AssertionError("scoped UoW did not yield")


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def get_plan(
    plan_id: str,
    session_id: str,
    request: Request,
    context: TenantContext = Depends(tenant_context),  # noqa: B008
):
    async for uow, session_context in _scoped_uow(request, context, session_id):
        plan = await _load_scoped_plan(uow, session_context, plan_id)
        return await build_plan_response(uow, plan)
    raise AssertionError("scoped UoW did not yield")


@router.post("/plans/{plan_id}/decision")
async def decide_plan(
    plan_id: str,
    body: PlanDecisionBody,
    request: Request,
    context: TenantContext = Depends(tenant_context),  # noqa: B008
):
    decision_request = PlanDecisionRequest(
        decision_id=body.decision_id,
        plan_id=plan_id,
        plan_version=body.plan_version,
        expected_version=body.expected_version,
        action=body.action,
        feedback=body.feedback,
    )
    if decision_request.feedback is not None:
        decision_request = decision_request.model_copy(
            update={"feedback": sanitize_plan_text(decision_request.feedback)}
        )
    try:
        async for uow, session_context in _scoped_uow(request, context, body.session_id):
            await _load_scoped_plan(uow, session_context, plan_id)
            replay = await uow.plans.for_context(session_context).replay_decision(
                decision_request,
                decided_by=context.tenant_id,
            )
            if replay is not None:
                run = await uow.workflow.get_plan_run(session_context, plan_id)
                if run is None:
                    raise PlanNotFoundError("Plan not found")
                return _decision_replay_response(body, replay, run)
    except PlanNotFoundError as error:
        raise _not_found() from error
    except PlanDecisionIdempotencyError:
        return await _decision_conflict_response(
            request, context, body.session_id, plan_id
        )

    runtime = await request.app.state.runtime_pool.acquire(session_context)
    coordinator = getattr(runtime, "plan_execution", None)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="runtime temporarily unavailable")
    service: PlanningService | None = getattr(coordinator, "planning_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="runtime temporarily unavailable")
    try:
        result = await service.decide(
            decision_request,
            decided_by=context.tenant_id,
            runtime_instance_id=runtime.runtime_instance_id,
        )
    except PlanNotFoundError as error:
        raise _not_found() from error
    except PlanDecisionIdempotencyError:
        return await _decision_conflict_response(
            request, context, body.session_id, plan_id
        )
    except PlanVersionConflictError as error:
        async for uow, _ in _scoped_uow(request, context, body.session_id):
            latest = await build_plan_response(uow, error.latest)
        return JSONResponse(
            {
                "detail": {
                    "code": "plan_version_conflict",
                    "latest": latest.model_dump(mode="json"),
                }
            },
            status_code=409,
        )

    runtime_lease = None
    handle = None
    if result.lease is not None and not result.idempotent_replay:
        try:
            runtime_lease = runtime.begin_run()
        except RuntimeError as error:
            with suppress(Exception):
                await WorkflowCoordinator(
                    request.app.state.database, settings=request.app.state.settings
                ).finish_run_with_checkpoint(result.lease, RunStatus.CANCELLED)
            raise RuntimeUnavailableError(
                request.app.state.runtime_pool.idle_ttl_ms // 1000 or 1
            ) from error
        handle = RunLeaseHandle(result.lease)

    async def stream() -> AsyncIterator[str]:
        encoder = DataStreamEncoder()
        try:
            yield encoder.run_metadata(body.session_id, str(result.run.context.run_id))
            if result.decision.action is PlanDecisionAction.REVISE:
                yield encoder.plan_revised(
                    {
                        "plan_id": result.snapshot.plan_id,
                        "current_version": result.snapshot.current_version,
                    }
                )
            yield encoder.plan_decision(
                {
                    "plan_id": result.snapshot.plan_id,
                    "decision": PlanDecisionResponse(
                        **asdict(result.decision)
                    ).model_dump(mode="json"),
                    "idempotent_replay": result.idempotent_replay,
                }
            )
            if handle is not None:
                outcome = await coordinator.execute_with_final_summary(
                    context=result.run.context,
                    run_lease_handle=handle,
                    runner=runtime.agent,
                )
                async for uow, _ in _scoped_uow(request, context, body.session_id):
                    persisted = await uow.workflow.get_run(result.run.context)
                    if persisted is not None:
                        response = await build_run_response(uow, persisted)
                        for attempt in response.latest_attempts:
                            yield encoder.plan_step_status(attempt.model_dump(mode="json"))
                if outcome.state == "awaiting_user":
                    yield encoder.run_status(
                        {"run_id": str(result.run.context.run_id), "status": "awaiting_user"}
                    )
            yield encoder.finish("stop")
        except asyncio.CancelledError:
            if handle is not None:
                await _terminalize_cancelled_stream(request, handle)
            raise
        except Exception:  # noqa: BLE001 - SSE must terminalize any internal failure.
            if handle is not None:
                await _terminalize_stream_error(request, handle)
            yield encoder.error("Plan execution failed")
            yield encoder.finish("error")
        finally:
            if runtime_lease is not None:
                runtime_lease.close()

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/plans/{plan_id}/runs")
async def rerun_plan(
    plan_id: str,
    body: SessionScopedRequest,
    request: Request,
    context: TenantContext = Depends(tenant_context),  # noqa: B008
):
    session_context = context.for_session(body.session_id)
    async for uow, checked_context in _scoped_uow(request, context, body.session_id):
        await _load_scoped_plan(uow, checked_context, plan_id)
    runtime = await request.app.state.runtime_pool.acquire(session_context)
    run_context = session_context.for_run(body.session_id, str(uuid4()))
    try:
        async with TenantUnitOfWork(
            request.app.state.database,
            session_context,
            planning_settings=request.app.state.settings.planning,
            workflow_settings=request.app.state.settings.workflow,
        ) as uow:
            await _require_session(uow, body.session_id)
            plan = await uow.plans.lock_aggregate(plan_id)
            if (
                plan.status.value != "approved"
                or plan.approved_version is None
                or plan.approved_version != plan.current_version
            ):
                raise HTTPException(status_code=409, detail="Plan is not approved")
            runs = await uow.workflow.list_plan_runs(
                session_context, plan_id, for_update=True
            )
            if any(run.status not in _TERMINAL_RUN_STATUSES for run in runs):
                raise HTTPException(status_code=409, detail="Plan already has an active run")
            workflow = WorkflowCoordinator(
                request.app.state.database,
                settings=request.app.state.settings,
                connection=uow.conn,
            )
            # Existing Coordinator owns quota, lease and RUN_STARTED durability.
            lease = await workflow.start_approved_plan_run_with_checkpoint(
                run_context,
                runtime.runtime_instance_id,
                plan_id=plan.plan_id,
                plan_version=plan.current_version,
                plan_digest=plan.current.content_digest,
            )
    except PlanNotFoundError as error:
        raise _not_found() from error
    except TenantRunQuotaError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error

    try:
        runtime_lease = runtime.begin_run()
    except RuntimeError as error:
        with suppress(Exception):
            await WorkflowCoordinator(
                request.app.state.database, settings=request.app.state.settings
            ).finish_run_with_checkpoint(lease, RunStatus.CANCELLED)
        raise RuntimeUnavailableError(
            request.app.state.runtime_pool.idle_ttl_ms // 1000 or 1
        ) from error
    handle = RunLeaseHandle(lease)

    async def stream() -> AsyncIterator[str]:
        encoder = DataStreamEncoder()
        try:
            yield encoder.run_metadata(body.session_id, str(run_context.run_id))
            attempts = await coordinator_execute_and_emit(
                request=request,
                context=context,
                session_id=body.session_id,
                runtime=runtime,
                run_context=run_context,
                handle=handle,
            )
            for attempt in attempts:
                yield encoder.plan_step_status(attempt)
            yield encoder.finish("stop")
        except asyncio.CancelledError:
            await _terminalize_cancelled_stream(request, handle)
            raise
        except Exception:  # noqa: BLE001 - SSE must terminalize any internal failure.
            await _terminalize_stream_error(request, handle)
            yield encoder.error("Plan execution failed")
            yield encoder.finish("error")
        finally:
            runtime_lease.close()

    return StreamingResponse(stream(), media_type="text/event-stream")


async def coordinator_execute_and_emit(
    *,
    request: Request,
    context: TenantContext,
    session_id: str,
    runtime,
    run_context: TenantContext,
    handle: RunLeaseHandle,
) -> tuple[dict[str, object], ...]:
    coordinator = runtime.plan_execution
    await coordinator.execute_with_final_summary(
        context=run_context,
        run_lease_handle=handle,
        runner=runtime.agent,
    )
    async for uow, _ in _scoped_uow(request, context, session_id):
        persisted = await uow.workflow.get_run(run_context)
        if persisted is None:
            return ()
        response = await build_run_response(uow, persisted)
        return tuple(
            attempt.model_dump(mode="json") for attempt in response.latest_attempts
        )
    return ()
