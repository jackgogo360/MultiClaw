from __future__ import annotations

import inspect
from typing import Any

from multiclaw.events import ScopedEvent
from multiclaw.stream import DataStreamEncoder
from multiclaw.workflow.continuation import WorkflowContinuationService
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.recovery import RecoveryService
from multiclaw.workflow.models import RunLease, RunLeaseHandle


def encode_session_metadata(session_payload: dict[str, Any]) -> str:
    return DataStreamEncoder.data_part(
        "data-session",
        session_payload,
        transient=True,
    )


def encode_run_metadata(session_id: str, run_id: str) -> str:
    return DataStreamEncoder.run_metadata(session_id, run_id)


def encode_scoped_event(event: ScopedEvent) -> str:
    return DataStreamEncoder.scoped_event(event)


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
    async for item in handler(user_input, **kwargs):
        yield item
