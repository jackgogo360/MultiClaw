import asyncio
from collections.abc import Awaitable, Callable
from inspect import iscoroutinefunction
from typing import TypeVar

T = TypeVar("T")


class ExecutionTimeoutError(asyncio.TimeoutError):
    pass


class ExecutionGuard:
    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    async def run(self, operation: Callable[[], T | Awaitable[T]]) -> T:
        if iscoroutinefunction(operation):
            coro = operation()
        else:
            coro = asyncio.to_thread(operation)

        try:
            return await asyncio.wait_for(coro, timeout=self._timeout)
        except asyncio.TimeoutError:
            raise ExecutionTimeoutError(
                f"Operation timed out after {self._timeout}s"
            )
