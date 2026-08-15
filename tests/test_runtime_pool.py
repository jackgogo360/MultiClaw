import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiclaw.events import EventBus
from multiclaw.tenancy import TenantContext


class FakeClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self._now_ms = now_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, delta_ms: int) -> None:
        self._now_ms += delta_ms


class FakeSkillManager:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FakeRegistry:
    def __init__(self) -> None:
        self.clear_calls = 0

    def clear(self) -> None:
        self.clear_calls += 1


class FakeMcpManager:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class FakeEventRouter:
    def clear(self) -> None:
        return None


@dataclass
class RuntimeContexts:
    a: TenantContext
    b: TenantContext


@pytest.fixture
def contexts() -> RuntimeContexts:
    return RuntimeContexts(
        a=TenantContext("tenant-a", "workspace-a"),
        b=TenantContext("tenant-b", "workspace-b"),
    )


@pytest.fixture
def clock():
    return FakeClock()


def _build_runtime_types():
    from multiclaw.runtime.models import TenantRuntime

    return TenantRuntime


class FakeRuntimeFactory:
    def __init__(self, clock: FakeClock, *, create_delay: float = 0.0) -> None:
        self.clock = clock
        self.create_delay = create_delay
        self.create_calls: list[str] = []

    async def create(self, context: TenantContext):
        TenantRuntime = _build_runtime_types()
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        self.create_calls.append(context.tenant_id)
        workspace_root = Path("/tmp/runtime-tests") / context.tenant_id / context.workspace_id
        return TenantRuntime(
            tenant_id=context.tenant_id,
            runtime_instance_id=f"runtime-{len(self.create_calls)}",
            workspace_root=workspace_root,
            agent=SimpleNamespace(),
            event_bus=EventBus(),
            event_router=FakeEventRouter(),
            scheduler=SimpleNamespace(),
            registry=FakeRegistry(),
            skill_manager=FakeSkillManager(),
            mcp_manager=FakeMcpManager(),
            sandbox_controller=None,
            sandbox_readiness=None,
            last_used_at_ms=self.clock.now_ms(),
        )


@pytest.fixture
def pool(clock):
    from multiclaw.runtime.pool import RuntimePool

    return RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=2,
        idle_ttl_ms=5_000,
        clock=clock,
    )


@pytest.fixture
def pool_at_capacity(clock):
    from multiclaw.runtime.pool import RuntimePool

    return RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=1,
        idle_ttl_ms=5_000,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_same_tenant_reuses_runtime_and_different_tenants_never_share_state(pool, contexts):
    a1, a2 = await asyncio.gather(
        pool.acquire(contexts.a),
        pool.acquire(contexts.a),
    )
    b = await pool.acquire(contexts.b)

    assert a1 is a2
    assert a1 is not b
    assert a1.agent is not b.agent
    assert a1.event_bus is not b.event_bus
    assert a1.event_router is not b.event_router
    assert a1.scheduler is not b.scheduler
    assert a1.registry is not b.registry
    assert a1.skill_manager is not b.skill_manager
    assert a1.mcp_manager is not b.mcp_manager
    assert a1.workspace_root != b.workspace_root


@pytest.mark.asyncio
async def test_same_tenant_concurrent_acquire_uses_single_create_lock(contexts, clock):
    from multiclaw.runtime.pool import RuntimePool

    factory = FakeRuntimeFactory(clock, create_delay=0.01)
    pool = RuntimePool(
        factory=factory,
        max_resident_tenants=2,
        idle_ttl_ms=5_000,
        clock=clock,
    )

    first, second = await asyncio.gather(
        pool.acquire(contexts.a),
        pool.acquire(contexts.a),
    )

    assert first is second
    assert factory.create_calls == ["tenant-a"]


@pytest.mark.asyncio
async def test_pool_evicts_only_safe_idle_runtime(pool, contexts, clock):
    idle = await pool.acquire(contexts.a)
    active = await pool.acquire(contexts.b)
    active.active_executing_run_count = 1

    clock.advance(pool.idle_ttl_ms + 1)

    assert await pool.evict_idle(clock.now_ms()) == 1
    assert await pool.peek(contexts.a.tenant_id) is None
    assert await pool.peek(contexts.b.tenant_id) is active


@pytest.mark.asyncio
async def test_pool_evicts_awaiting_user_runtime_when_no_tool_is_executing(pool, contexts, clock):
    runtime = await pool.acquire(contexts.a)
    runtime.active_run_count = 1
    runtime.awaiting_user_run_count = 1
    runtime.active_tool_execution_count = 0

    clock.advance(pool.idle_ttl_ms + 1)

    assert await pool.evict_idle(clock.now_ms()) == 1
    assert await pool.peek(contexts.a.tenant_id) is None


@pytest.mark.asyncio
async def test_pool_returns_capacity_error_when_no_runtime_is_evictable(pool_at_capacity, contexts):
    from multiclaw.runtime.pool import RuntimeCapacityError

    resident = await pool_at_capacity.acquire(contexts.a)
    resident.active_executing_run_count = 1

    with pytest.raises(RuntimeCapacityError) as error:
        await pool_at_capacity.acquire(contexts.b)

    assert error.value.retry_after_seconds >= 1


@pytest.mark.asyncio
async def test_cross_tenant_concurrent_acquire_never_overruns_capacity(contexts, clock):
    from multiclaw.runtime.pool import RuntimeCapacityError, RuntimePool

    pool = RuntimePool(
        factory=FakeRuntimeFactory(clock, create_delay=0.01),
        max_resident_tenants=1,
        idle_ttl_ms=5_000,
        clock=clock,
    )

    first, second = await asyncio.gather(
        pool.acquire(contexts.a),
        pool.acquire(contexts.b),
        return_exceptions=True,
    )

    outcomes = [first, second]
    errors = [result for result in outcomes if isinstance(result, RuntimeCapacityError)]
    runtimes = [result for result in outcomes if not isinstance(result, Exception)]

    assert len(errors) == 1
    assert len(runtimes) == 1
    assert len(pool._runtimes) == 1


@pytest.mark.asyncio
async def test_pool_revoke_closes_and_removes_runtime(pool, contexts):
    runtime = await pool.acquire(contexts.a)

    await pool.revoke(contexts.a.tenant_id)

    assert await pool.peek(contexts.a.tenant_id) is None
    assert runtime.mcp_manager.stop_calls == 1
    assert runtime.skill_manager.close_calls == 1
    assert runtime.registry.clear_calls == 1


@pytest.mark.asyncio
async def test_pool_close_is_idempotent(pool, contexts):
    runtime = await pool.acquire(contexts.a)

    await pool.close()
    await pool.close()

    assert runtime.mcp_manager.stop_calls == 1
    assert runtime.skill_manager.close_calls == 1
    assert runtime.registry.clear_calls == 1


@pytest.mark.asyncio
async def test_pool_close_attempts_all_runtimes_and_preserves_primary_failure(clock):
    from multiclaw.runtime.pool import RuntimePool

    class ClosingRuntime:
        def __init__(self, exc: RuntimeError | None = None) -> None:
            self.calls = 0
            self.exc = exc

        async def close(self) -> None:
            self.calls += 1
            if self.exc is not None:
                raise self.exc

    pool = RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=2,
        idle_ttl_ms=5_000,
        clock=clock,
    )
    first = ClosingRuntime(RuntimeError("first close failed"))
    second = ClosingRuntime(RuntimeError("second close failed"))
    pool._runtimes = {"tenant-a": first, "tenant-b": second}

    with pytest.raises(RuntimeError, match="first close failed") as error:
        await pool.close()

    assert first.calls == 1
    assert second.calls == 1
    assert error.value.__notes__
    assert any("tenant-b" in note and "second close failed" in note for note in error.value.__notes__)
