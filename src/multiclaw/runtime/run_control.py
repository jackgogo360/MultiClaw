"""Cooperative controls bound to a detached producer, inherited by its children."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field

from multiclaw.tenancy import TenantContext


@dataclass
class RunControl:
    context: TenantContext
    cancelled: bool = False
    deadline: float | None = None
    steering: deque[str] = field(default_factory=deque)


current_run_control: ContextVar[RunControl | None] = ContextVar('current_run_control', default=None)


def _control(context: TenantContext) -> RunControl | None:
    control = current_run_control.get()
    if control is None:
        return None
    expected = control.context
    if (expected.tenant_id, expected.workspace_id, expected.session_id, expected.run_id) != (
        context.tenant_id, context.workspace_id, context.session_id, context.run_id
    ):
        return None
    return control


async def check_cancel(context: TenantContext) -> None:
    control = _control(context)
    if control is not None and control.cancelled:
        raise asyncio.CancelledError


async def collect_steering(context: TenantContext) -> list[str]:
    await check_cancel(context)
    control = _control(context)
    if control is None:
        return []
    messages = list(control.steering)
    control.steering.clear()
    return messages


def cancellation_status(context: TenantContext):
    from multiclaw.workflow.models import RunStatus
    control = _control(context)
    if control is not None and not control.cancelled and control.deadline is not None and time.monotonic() >= control.deadline:
        return RunStatus.FAILED_TERMINAL
    return RunStatus.CANCELLED
