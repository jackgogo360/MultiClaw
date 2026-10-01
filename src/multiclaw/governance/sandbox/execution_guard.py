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

    async def run(self, operation: Callable[[], T | Awaitable[T]], *, timeout: float | None = None) -> T:
        if iscoroutinefunction(operation):
            coro = operation()
        else:
            coro = asyncio.to_thread(operation)

        effective_timeout = self._timeout if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")
        try:
            return await asyncio.wait_for(coro, timeout=effective_timeout)
        except asyncio.TimeoutError:
            raise ExecutionTimeoutError(
                f"Operation timed out after {effective_timeout}s"
            )
