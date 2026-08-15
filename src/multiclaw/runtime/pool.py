from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
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


class RuntimeUnavailableError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("runtime is temporarily unavailable")
        self.retry_after_seconds = retry_after_seconds


class _SystemClock:
    def now_ms(self) -> int:
        import time

        return int(time.time() * 1000)


@dataclass(slots=True)
class _TenantLockEntry:
    lock: asyncio.Lock
    users: int = 0


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
        self._tenant_locks: dict[str, _TenantLockEntry] = {}
        self._tenant_lock_table_lock = asyncio.Lock()
        self._capacity_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False

    def _retry_after_seconds(self) -> int:
        return max(1, math.ceil(self.idle_ttl_ms / 1000))

    async def acquire(self, context: TenantContext) -> TenantRuntime:
        if self._closed:
            raise RuntimeError("runtime pool is closed")

        async with self._tenant_lock(context.tenant_id):
            runtime = self._runtimes.get(context.tenant_id)
            if runtime is not None:
                if self._closed:
                    raise RuntimeError("runtime pool is closed")
                if not getattr(runtime, "is_available", True):
                    raise RuntimeUnavailableError(self._retry_after_seconds())
                self._touch_runtime(runtime)
                return runtime

            async with self._capacity_lock:
                if self._closed:
                    raise RuntimeError("runtime pool is closed")
                await self._ensure_capacity()
                runtime = self._runtimes.get(context.tenant_id)
                if runtime is None:
                    runtime = await self._factory.create(context)
                    if self._closed:
                        self._mark_runtime_unavailable(runtime)
                        await runtime.close()
                        raise RuntimeError("runtime pool is closed")
                    self._runtimes[context.tenant_id] = runtime
                if not getattr(runtime, "is_available", True):
                    raise RuntimeUnavailableError(self._retry_after_seconds())
                self._touch_runtime(runtime)
                return runtime

    async def peek(self, tenant_id: str) -> TenantRuntime | None:
        return self._runtimes.get(tenant_id)

    async def revoke(self, tenant_id: str) -> None:
        async with self._tenant_lock(tenant_id):
            runtime = self._runtimes.get(tenant_id)
            if runtime is None:
                return
            self._mark_runtime_unavailable(runtime)
            await runtime.close()
            if self._runtimes.get(tenant_id) is runtime:
                self._runtimes.pop(tenant_id, None)

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
        async with self._tenant_lock(tenant_id):
            current = self._runtimes.get(tenant_id)
            if current is not expected_runtime:
                return False
            if not current.can_evict(timestamp, self.idle_ttl_ms):
                return False
            self._mark_runtime_unavailable(current)
            try:
                await current.close()
            except Exception:
                return False
            if self._runtimes.get(tenant_id) is current:
                self._runtimes.pop(tenant_id, None)
            return True

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed and not self._runtimes:
                return
            self._closed = True
            async with self._capacity_lock:
                runtimes = list(self._runtimes.items())

            primary: BaseException | None = None
            for tenant_id, runtime in runtimes:
                try:
                    async with self._tenant_lock(tenant_id):
                        current = self._runtimes.get(tenant_id)
                        if current is not runtime:
                            continue
                        self._mark_runtime_unavailable(current)
                        await current.close()
                        if self._runtimes.get(tenant_id) is current:
                            self._runtimes.pop(tenant_id, None)
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
        raise RuntimeCapacityError(retry_after_seconds=self._retry_after_seconds())

    @asynccontextmanager
    async def _tenant_lock(self, tenant_id: str):
        async with self._tenant_lock_table_lock:
            entry = self._tenant_locks.get(tenant_id)
            if entry is None:
                entry = _TenantLockEntry(lock=asyncio.Lock())
                self._tenant_locks[tenant_id] = entry
            entry.users += 1

        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._tenant_lock_table_lock:
                entry.users -= 1
                current = self._tenant_locks.get(tenant_id)
                if current is entry and entry.users == 0:
                    self._tenant_locks.pop(tenant_id, None)

    @staticmethod
    def _mark_runtime_unavailable(runtime: TenantRuntime) -> None:
        marker = getattr(runtime, "mark_unavailable", None)
        if marker is not None:
            marker()

    def _touch_runtime(self, runtime: TenantRuntime) -> None:
        touch = getattr(runtime, "touch", None)
        if touch is not None:
            touch(self._clock.now_ms())
        else:
            runtime.last_used_at_ms = self._clock.now_ms()
