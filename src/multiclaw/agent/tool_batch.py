from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Sequence

from multiclaw.agent.models import Observation, ObservationType
from multiclaw.tenancy import TenantContext
from multiclaw.tools.registry import ToolRegistry
from multiclaw.tools.scheduler import CoreToolScheduler


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
    ) -> list[ToolCallOutcome]:
        outcomes: list[ToolCallOutcome | None] = [None] * len(calls)
        index = 0

        while index < len(calls):
            if self.enabled and await self._is_concurrency_eligible(calls[index]):
                run_end = index + 1
                while run_end < len(calls) and await self._is_concurrency_eligible(calls[run_end]):
                    run_end += 1
                await self._execute_concurrent_run(
                    calls[index:run_end],
                    outcomes,
                    start=index,
                    context=context,
                )
                index = run_end
                continue

            outcomes[index] = await self._execute_one(calls[index], context=context)
            index += 1

        return [outcome for outcome in outcomes if outcome is not None]

    async def _is_concurrency_eligible(self, call: ToolCallSpec) -> bool:
        builder = self.registry.get(call.name)
        if builder is None:
            return False
        return await self.scheduler.can_run_concurrently(builder, call.arguments)

    async def _execute_concurrent_run(
        self,
        calls: Sequence[ToolCallSpec],
        outcomes: list[ToolCallOutcome | None],
        *,
        start: int,
        context: TenantContext | None,
    ) -> None:
        semaphore = asyncio.Semaphore(self.max_concurrency)
        tasks = [
            asyncio.create_task(
                self._execute_with_semaphore(semaphore, offset, call, context=context)
            )
            for offset, call in enumerate(calls, start=start)
        ]
        try:
            results = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        for index, outcome in results:
            outcomes[index] = outcome

    async def _execute_with_semaphore(
        self,
        semaphore: asyncio.Semaphore,
        index: int,
        call: ToolCallSpec,
        *,
        context: TenantContext | None,
    ) -> tuple[int, ToolCallOutcome]:
        async with semaphore:
            return index, await self._execute_one(call, context=context)

    async def _execute_one(
        self,
        call: ToolCallSpec,
        *,
        context: TenantContext | None,
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
            )

        result = await self.scheduler.run(
            builder,
            call.arguments,
            context=context,
            call_id=call.call_id,
        )
        return ToolCallOutcome(
            call_id=call.call_id,
            name=call.name,
            observation=Observation(
                type=ObservationType.TOOL_RESULT,
                content=result.content,
                data=result.data,
            ),
        )
