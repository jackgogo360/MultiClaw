from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Iterable
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from multiclaw.api.dependencies import tenant_context, tenant_uow
from multiclaw.events import EventScope, ScopedEvent
from multiclaw.memory import MemoryEntry
from multiclaw.observability import increment_metric, record_trace_event
from multiclaw.observability import observability_scope
from multiclaw.runtime.pool import RuntimeUnavailableError
from multiclaw.security.redaction import public_error_message, redact
from multiclaw.session import SessionStatus
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.stream import DataStreamEncoder
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.continuation import WorkflowContinuationService
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    RecoveryAction,
    RecoveryOutcome,
    RunLease,
    RunLeaseHandle,
    RunStatus,
    StaleFenceError,
    TenantRunQuotaError,
)
from multiclaw.workflow.recovery import RecoveryService


logger = logging.getLogger("multiclaw")
router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    message: str | None = None
    session_id: str | None = None
    id: str | None = None
    messages: list[dict[str, Any]] | None = None


def encode_session_metadata(session_payload: dict[str, Any]) -> str:
    return DataStreamEncoder.data_part(
        "data-session",
        session_payload,
        transient=True,
    )


def encode_run_metadata(session_id: str, run_id: str) -> str:
    return DataStreamEncoder.run_metadata(session_id, run_id)


def encode_scoped_event(event: ScopedEvent) -> str:
    safe_event = event.model_copy(update={"data": redact(event.data)})
    return DataStreamEncoder.scoped_event(safe_event)


def build_workflow_coordinator(database, settings, *, connection=None) -> WorkflowCoordinator:
    return WorkflowCoordinator(database, settings=settings, connection=connection)


def build_workflow_recovery_service(database, settings) -> RecoveryService:
    return RecoveryService(database, settings=settings)


def build_workflow_continuation_service(database, settings) -> WorkflowContinuationService:
    return WorkflowContinuationService(database, settings=settings)


def stream_accepts_run_lease(handler) -> bool:
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return False

    return _accepts_keyword(signature, "run_lease")


def _accepts_keyword(signature: inspect.Signature, keyword: str) -> bool:
    if keyword in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


async def iterate_message_stream(
    handler,
    user_input: str,
    *,
    context,
    run_lease: RunLease,
    run_lease_handle: RunLeaseHandle,
    workflow_recovery=None,
    workflow_continuation=None,
    persisted_user_turn_index: int | None = None,
):
    signature = inspect.signature(handler)
    kwargs = {"context": context}
    if _accepts_keyword(signature, "run_lease"):
        kwargs["run_lease"] = run_lease
    if _accepts_keyword(signature, "run_lease_handle"):
        kwargs["run_lease_handle"] = run_lease_handle
    if _accepts_keyword(signature, "workflow_recovery") and workflow_recovery is not None:
        kwargs["workflow_recovery"] = workflow_recovery
    if _accepts_keyword(signature, "workflow_continuation") and workflow_continuation is not None:
        kwargs["workflow_continuation"] = workflow_continuation
    if _accepts_keyword(signature, "persisted_user_turn_index") and persisted_user_turn_index is not None:
        kwargs["persisted_user_turn_index"] = persisted_user_turn_index
    async for item in handler(user_input, **kwargs):
        yield item


@router.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    context: TenantContext = Depends(tenant_context),
    uow: TenantUnitOfWork = Depends(tenant_uow, scope="function"),
):
    message = _resolve_chat_message(req)
    has_session_id = req.session_id is not None
    has_id_alias = req.id is not None
    requested_session_id = req.session_id if has_session_id else req.id

    if has_session_id or has_id_alias:
        if not requested_session_id:
            increment_metric(
                "multiclaw_scope_fk_rejections_total",
                labels={"backend": "unknown", "operation": "chat_session_lookup", "status": "error", "error_class": "scope_fk_rejection"},
            )
            raise HTTPException(status_code=404, detail="session not found")
        session = await uow.sessions.get(requested_session_id)
        if session is None:
            increment_metric(
                "multiclaw_scope_fk_rejections_total",
                labels={"backend": "unknown", "operation": "chat_session_lookup", "status": "error", "error_class": "scope_fk_rejection"},
            )
            raise HTTPException(status_code=404, detail="session not found")
        if session.status == SessionStatus.ARCHIVED:
            raise HTTPException(status_code=409, detail="session is archived")
    else:
        session = await uow.sessions.create()

    session = await uow.sessions.touch_message(session.id, message)
    assert session is not None
    run_id = str(uuid4())
    run_context = context.for_run(session.id, run_id)
    runtime = await request.app.state.runtime_pool.acquire(run_context)
    session_memory = MemoryRepository(
        uow.conn,
        context.for_session(session.id),
        request.app.state.database.dialect,
    )
    recent_messages = await session_memory.recent(limit=1, entry_type="chat_message")
    user_turn_index = (recent_messages[0].turn_index + 1) if recent_messages else 1
    await session_memory.save(
        MemoryEntry(
            content=message,
            type="chat_message",
            role="user",
            session_id=session.id,
            turn_index=user_turn_index,
        )
    )
    workflow = build_workflow_coordinator(
        request.app.state.database,
        request.app.state.settings,
        connection=uow.conn,
    )
    workflow_continuation = build_workflow_continuation_service(
        request.app.state.database,
        request.app.state.settings,
    )
    workflow_recovery = build_workflow_recovery_service(
        request.app.state.database,
        request.app.state.settings,
    )

    async def _cleanup_prestream_failure(
        primary: BaseException,
        *,
        workflow_lease,
        recovery_outcome: RecoveryOutcome | None,
    ) -> None:
        target = RunStatus.FAILED_TERMINAL
        if recovery_outcome is not None and recovery_outcome.status in {
            RunStatus.BLOCKED_CORRUPT,
            RunStatus.BLOCKED_INCOMPATIBLE,
        }:
            target = recovery_outcome.status

        coordinator = build_workflow_coordinator(
            request.app.state.database,
            request.app.state.settings,
        )
        try:
            await coordinator.finish_run_with_checkpoint(workflow_lease, target)
            return
        except Exception:
            primary.add_note("pre-stream terminal checkpoint cleanup failed")
        try:
            await coordinator.finish_run(workflow_lease, target)
        except Exception:
            primary.add_note("pre-stream terminal state cleanup failed")

    workflow_lease = None
    try:
        workflow_lease = await workflow.start_run_with_checkpoint(
            run_context,
            runtime.runtime_instance_id,
        )
        await uow.commit()
        live_recovery = await workflow_recovery.validate_live_run(run_context)
        if live_recovery.action is not RecoveryAction.RESUME_MODEL:
            raise RuntimeError("live workflow checkpoint validation failed")
    except TenantRunQuotaError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except Exception as error:
        if workflow_lease is not None:
            await _cleanup_prestream_failure(
                error,
                workflow_lease=workflow_lease,
                recovery_outcome=locals().get("live_recovery"),
            )
        raise

    try:
        run_lease = runtime.begin_run()
    except RuntimeError as error:
        if workflow_lease is not None:
            await build_workflow_coordinator(
                request.app.state.database,
                request.app.state.settings,
            ).finish_run_with_checkpoint(workflow_lease, RunStatus.CANCELLED)
        if str(error) == "runtime is unavailable":
            raise RuntimeUnavailableError(
                request.app.state.runtime_pool.idle_ttl_ms // 1000 or 1
            ) from error
        raise

    async def event_stream():
        async with observability_scope(
            metrics=getattr(request.app.state, "operational_metrics", None),
            trace_sink=getattr(request.app.state, "trace_sink", None),
        ):
            logger.info("SSE stream started session=%s run=%s", session.id, run_id)
            enc = DataStreamEncoder()
            text_part_id: str | None = None
            reasoning_part_id: str | None = None
            step_open = False
            pending_tool_results = 0
            subscription = None
            stream_task: asyncio.Task | None = None
            heartbeat_task: asyncio.Task | None = None
            assert workflow_lease is not None
            workflow_lease_handle = RunLeaseHandle(workflow_lease)
            terminal_persisted = False
            fence_lost = False

            async def persist_terminal(status: RunStatus) -> None:
                nonlocal terminal_persisted
                if terminal_persisted:
                    return
                await workflow_lease_handle.refresh(
                    lambda lease: build_workflow_coordinator(
                        request.app.state.database,
                        request.app.state.settings,
                    ).finish_run_with_checkpoint(lease, status)
                )
                terminal_persisted = True

            def close_text_part() -> list[str]:
                nonlocal text_part_id
                if text_part_id is None:
                    return []
                chunks = [enc.text_end(text_part_id)]
                text_part_id = None
                return chunks

            def close_reasoning_part() -> list[str]:
                nonlocal reasoning_part_id
                if reasoning_part_id is None:
                    return []
                chunks = [enc.reasoning_end(reasoning_part_id)]
                reasoning_part_id = None
                return chunks

            def close_open_parts() -> list[str]:
                return [*close_reasoning_part(), *close_text_part()]

            def open_step() -> list[str]:
                nonlocal step_open
                if step_open:
                    return []
                step_open = True
                return [enc.start_step()]

            def close_step() -> list[str]:
                nonlocal step_open
                if not step_open:
                    return []
                step_open = False
                return [enc.finish_step()]

            def drain_event_queue() -> list[str]:
                chunks: list[str] = []
                while not event_queue.empty():
                    evt = event_queue.get_nowait()
                    chunks.append(encode_scoped_event(evt))
                    if evt.event_type == "tool.awaiting_approval":
                        chunks.extend(open_step())
                        chunks.extend(close_open_parts())
                        tool_call_id = evt.data.get("call_id") or evt.data.get("request_id") or ""
                        chunks.append(
                            enc.tool_input_available(
                                tool_call_id,
                                evt.data.get("tool", ""),
                                redact(evt.data.get("params", {})),
                            )
                        )
                        chunks.append(
                            enc.tool_approval_request(
                                evt.data.get("request_id", ""),
                                tool_call_id,
                            )
                        )
                return chunks

            try:
                yield enc.start()
                yield encode_session_metadata(session.model_dump(mode="json"))
                yield encode_run_metadata(session.id, run_id)
                for chunk in open_step():
                    yield chunk

                token_queue: asyncio.Queue[dict] = asyncio.Queue()
                event_queue: asyncio.Queue[ScopedEvent] = asyncio.Queue()
                heartbeat_stop = asyncio.Event()

                async def collector(event: ScopedEvent):
                    await event_queue.put(event)

                subscription = runtime.event_router.subscribe(EventScope.from_context(run_context), collector)

                async def run_stream():
                    try:
                        async for item in iterate_message_stream(
                            runtime.agent.handle_message_stream,
                            message,
                            context=run_context,
                            run_lease=await workflow_lease_handle.current(),
                            run_lease_handle=workflow_lease_handle,
                            workflow_recovery=workflow_recovery,
                            workflow_continuation=workflow_continuation,
                            persisted_user_turn_index=user_turn_index,
                        ):
                            await token_queue.put(item)
                    except Exception as exc:
                        logger.error("stream error error_type=%s", type(exc).__name__)
                        await token_queue.put({"type": "error", "content": public_error_message(exc)})

                async def heartbeat_run_lease() -> None:
                    nonlocal fence_lost
                    interval_seconds = max(
                        0.001,
                        request.app.state.settings.workflow.heartbeat_ms / 1000,
                    )
                    while True:
                        try:
                            await asyncio.wait_for(heartbeat_stop.wait(), timeout=interval_seconds)
                            return
                        except asyncio.TimeoutError:
                            pass
                        if terminal_persisted:
                            continue
                        try:
                            await workflow_lease_handle.refresh(
                                lambda lease: build_workflow_coordinator(
                                    request.app.state.database,
                                    request.app.state.settings,
                                ).heartbeat(lease)
                            )
                        except StaleFenceError as exc:
                            fence_lost = True
                            increment_metric(
                                "multiclaw_stale_fence_total",
                                labels={"backend": "unknown", "operation": "chat_heartbeat", "status": "error", "error_class": "stale_fence"},
                            )
                            record_trace_event("stale_fence", attributes={"operation": "chat_heartbeat", "error": str(exc)})
                            logger.error("run lease heartbeat lost current fence")
                            if stream_task is not None:
                                stream_task.cancel()
                            await token_queue.put({"type": "error", "content": public_error_message(exc)})
                            return
                        except Exception as exc:
                            logger.error("run lease heartbeat failed error_type=%s", type(exc).__name__)
                            await token_queue.put({"type": "error", "content": public_error_message(exc)})
                            return

                stream_task = asyncio.create_task(run_stream())
                heartbeat_task = asyncio.create_task(heartbeat_run_lease())

                while True:
                    while True:
                        try:
                            item = token_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                        if item["type"] == "token":
                            for chunk in open_step():
                                yield chunk
                            for chunk in close_reasoning_part():
                                yield chunk
                            if text_part_id is None:
                                text_part_id = uuid4().hex
                                yield enc.text_start(text_part_id)
                            yield enc.text_delta(text_part_id, item["content"])
                        elif item["type"] == "done":
                            if fence_lost:
                                continue
                            await persist_terminal(RunStatus.COMPLETED)
                            for chunk in drain_event_queue():
                                yield chunk
                            for chunk in close_open_parts():
                                yield chunk
                            for chunk in close_step():
                                yield chunk
                            yield enc.finish("stop")
                            return
                        elif item["type"] == "error":
                            if not fence_lost:
                                await persist_terminal(RunStatus.FAILED_TERMINAL)
                            for chunk in drain_event_queue():
                                yield chunk
                            for chunk in close_open_parts():
                                yield chunk
                            for chunk in close_step():
                                yield chunk
                            yield enc.error(item["content"])
                            return
                        elif item["type"] == "tool_call":
                            for chunk in open_step():
                                yield chunk
                            for chunk in close_open_parts():
                                yield chunk
                            pending_tool_results += 1
                            run_lease.mark_tool_execution_started()
                            tool_call_id = item.get("call_id") or uuid4().hex
                            yield enc.tool_input_available(
                                tool_call_id,
                                item["name"],
                                redact(item.get("arguments", {})),
                            )
                        elif item["type"] == "tool_result":
                            for chunk in close_open_parts():
                                yield chunk
                            tool_call_id = item.get("call_id", "")
                            if item.get("is_error", False):
                                yield enc.tool_output_error(tool_call_id, public_error_message(RuntimeError(str(item.get("content", "")))))
                            else:
                                yield enc.tool_output_available(
                                    tool_call_id,
                                    {"content": item.get("content", "")},
                                )
                            if pending_tool_results > 0:
                                pending_tool_results -= 1
                                run_lease.mark_tool_execution_finished()
                            if pending_tool_results == 0:
                                for chunk in close_step():
                                    yield chunk
                        elif item["type"] == "reasoning":
                            for chunk in open_step():
                                yield chunk
                            for chunk in close_text_part():
                                yield chunk
                            if reasoning_part_id is None:
                                reasoning_part_id = uuid4().hex
                                yield enc.reasoning_start(reasoning_part_id)
                            yield enc.reasoning_delta(reasoning_part_id, item["content"])
                        else:
                            yield enc.data_part("data-state", {"item": redact(item)}, transient=True)

                    for chunk in drain_event_queue():
                        yield chunk
                    if stream_task.done():
                        exc = stream_task.exception()
                        if exc:
                            if not fence_lost:
                                await persist_terminal(RunStatus.FAILED_TERMINAL)
                            for chunk in drain_event_queue():
                                yield chunk
                            for chunk in close_open_parts():
                                yield chunk
                            for chunk in close_step():
                                yield chunk
                            yield enc.error(public_error_message(exc))
                        else:
                            for chunk in drain_event_queue():
                                yield chunk
                            for chunk in close_open_parts():
                                yield chunk
                            for chunk in close_step():
                                yield chunk
                            yield enc.finish("stop")
                        return

                    await asyncio.sleep(0.02)
            finally:
                if heartbeat_task is not None:
                    heartbeat_stop.set()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                if stream_task is not None:
                    stream_task.cancel()
                    await asyncio.gather(stream_task, return_exceptions=True)
                if subscription is not None:
                    subscription.close()
                if not terminal_persisted and not fence_lost:
                    try:
                        await persist_terminal(RunStatus.CANCELLED)
                    except Exception:
                        logger.error("failed to persist terminal run status")
                run_lease.close()
                logger.info("SSE stream ended session=%s run=%s", session.id, run_id)

    try:
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"X-Vercel-AI-Data-Stream": "v1"},
        )
    except BaseException:
        if workflow_lease is not None:
            try:
                await build_workflow_coordinator(
                    request.app.state.database,
                    request.app.state.settings,
                ).finish_run_with_checkpoint(workflow_lease, RunStatus.CANCELLED)
            except Exception:
                logger.error("failed to cancel run after streaming setup error")
        run_lease.close()
        raise


def _resolve_chat_message(req: ChatRequest) -> str:
    if req.message:
        return req.message

    message = _extract_latest_user_message(req.messages or [])
    if message:
        return message

    raise HTTPException(status_code=422, detail="No user message found in request")


def _extract_latest_user_message(messages: list[dict[str, Any]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _extract_message_text(message)
        if text:
            return text
    return None


def _extract_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, Iterable) and not isinstance(content, (str, bytes, dict)):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""
