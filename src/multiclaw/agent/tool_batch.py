from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from multiclaw.agent.models import Observation, ObservationType
from multiclaw.tenancy import TenantContext
from multiclaw.tools.base import ToolExecutionResult, ToolStatus
from multiclaw.tools.registry import ToolRegistry
from multiclaw.tools.scheduler import CoreToolScheduler
from multiclaw.workflow.models import RunLeaseHandle


@dataclass(frozen=True, slots=True)
class ToolCallSpec:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    call_id: str
    name: str
    observation: Observation
    result: ToolExecutionResult


class ToolBatchExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        scheduler: CoreToolScheduler,
        max_concurrency: int,
        enabled: bool = True,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.registry = registry
        self.scheduler = scheduler
        self.max_concurrency = max_concurrency
        self.enabled = enabled

    async def execute(
        self,
        calls: Sequence[ToolCallSpec],
        *,
        context: TenantContext | None = None,
        run_lease_handle: RunLeaseHandle | None = None,
    ) -> list[ToolCallOutcome]:
        outcomes: list[ToolCallOutcome] = []
        for call in calls:
            outcome = await self._execute_one(
                call,
                context=context,
                run_lease_handle=run_lease_handle,
            )
            outcomes.append(outcome)
            if outcome.result.status is ToolStatus.AWAITING_APPROVAL:
                break
        return outcomes

    async def _execute_one(
        self,
        call: ToolCallSpec,
        *,
        context: TenantContext | None,
        run_lease_handle: RunLeaseHandle | None,
    ) -> ToolCallOutcome:
        builder = self.registry.get(call.name)
        if builder is None:
            return ToolCallOutcome(
                call_id=call.call_id,
                name=call.name,
                observation=Observation(
                    type=ObservationType.ERROR,
                    content=f"unknown tool: {call.name}",
                ),
                result=ToolExecutionResult(
                    status=ToolStatus.ERROR,
                    content=f"unknown tool: {call.name}",
                ),
            )

        result = await self.scheduler.run(
            builder,
            call.arguments,
            context=context,
            call_id=call.call_id,
            run_lease_handle=run_lease_handle,
        )
        return ToolCallOutcome(
            call_id=call.call_id,
            name=call.name,
            observation=Observation(
                type=ObservationType.TOOL_RESULT,
                content=result.content,
                data=result.data,
            ),
            result=result,
        )
