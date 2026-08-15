from __future__ import annotations

import asyncio
import math
from typing import Protocol

from multiclaw.runtime.models import RuntimeClock, TenantRuntime
from multiclaw.tenancy import TenantContext


class RuntimeFactoryProtocol(Protocol):
    async def create(self, context: TenantContext) -> TenantRuntime: ...


class RuntimeCapacityError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("runtime pool is at capacity")
        self.retry_after_seconds = retry_after_seconds


class _SystemClock:
    def now_ms(self) -> int:
        import time

        return int(time.time() * 1000)


class RuntimePool:
    def __init__(
        self,
        *,
        factory: RuntimeFactoryProtocol,
        max_resident_tenants: int,
        idle_ttl_ms: int,
        clock: RuntimeClock | None = None,
    ) -> None:
        self._factory = factory
        self.max_resident_tenants = max_resident_tenants
        self.idle_ttl_ms = idle_ttl_ms
        self._clock = clock or _SystemClock()
        self._runtimes: dict[str, TenantRuntime] = {}
        self._create_locks: dict[str, asyncio.Lock] = {}
        self._capacity_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False

    async def acquire(self, context: TenantContext) -> TenantRuntime:
        if self._closed:
            raise RuntimeError("runtime pool is closed")

        lock = self._create_locks.setdefault(context.tenant_id, asyncio.Lock())
        async with lock:
            runtime = self._runtimes.get(context.tenant_id)
            if runtime is not None:
                if self._closed:
                    raise RuntimeError("runtime pool is closed")
                runtime.last_used_at_ms = self._clock.now_ms()
                return runtime

            async with self._capacity_lock:
                if self._closed:
                    raise RuntimeError("runtime pool is closed")
                runtime = self._runtimes.get(context.tenant_id)
                if runtime is None:
                    await self._ensure_capacity()
                    runtime = await self._factory.create(context)
                    if self._closed:
                        await runtime.close()
                        raise RuntimeError("runtime pool is closed")
                    self._runtimes[context.tenant_id] = runtime
                runtime.last_used_at_ms = self._clock.now_ms()
                return runtime

    async def peek(self, tenant_id: str) -> TenantRuntime | None:
        return self._runtimes.get(tenant_id)

    async def revoke(self, tenant_id: str) -> None:
        lock = self._create_locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            runtime = self._runtimes.pop(tenant_id, None)
            if runtime is not None:
                await runtime.close()

    async def evict_idle(self, now_ms: int | None = None) -> int:
        timestamp = self._clock.now_ms() if now_ms is None else now_ms
        evicted = 0
        for tenant_id, runtime in list(self._runtimes.items()):
            if await self._evict_if_safe(tenant_id, runtime, timestamp):
                evicted += 1
        return evicted

    async def _evict_if_safe(
        self,
        tenant_id: str,
        expected_runtime: TenantRuntime,
        timestamp: int,
    ) -> bool:
        lock = self._create_locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            current = self._runtimes.get(tenant_id)
            if current is not expected_runtime:
                return False
            if not current.can_evict(timestamp, self.idle_ttl_ms):
                return False
            runtime = self._runtimes.pop(tenant_id, None)
        if runtime is None:
            return False
        await runtime.close()
        return True

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            async with self._capacity_lock:
                runtimes = list(self._runtimes.items())
                self._runtimes.clear()
        primary: BaseException | None = None

        for tenant_id, runtime in runtimes:
            try:
                await runtime.close()
            except BaseException as error:
                if primary is None:
                    primary = error
                else:
                    primary.add_note(
                        f"runtime[{tenant_id}].close failed: {type(error).__name__}: {error}"
                    )
        if primary is not None:
            raise primary

    async def _ensure_capacity(self) -> None:
        if len(self._runtimes) < self.max_resident_tenants:
            return
        await self.evict_idle(self._clock.now_ms())
        if len(self._runtimes) < self.max_resident_tenants:
            return
        raise RuntimeCapacityError(
            retry_after_seconds=max(1, math.ceil(self.idle_ttl_ms / 1000))
        )
