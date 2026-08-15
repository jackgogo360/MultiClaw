import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import threading

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
    active_run = active.begin_run()

    clock.advance(pool.idle_ttl_ms + 1)

    try:
        assert await pool.evict_idle(clock.now_ms()) == 1
        assert await pool.peek(contexts.a.tenant_id) is None
        assert await pool.peek(contexts.b.tenant_id) is active
    finally:
        active_run.close()


@pytest.mark.asyncio
async def test_pool_does_not_evict_uncheckpointed_awaiting_user_runtime(pool, contexts, clock):
    runtime = await pool.acquire(contexts.a)
    run = runtime.begin_run()
    run.mark_awaiting_user(checkpoint_persisted=False)

    clock.advance(pool.idle_ttl_ms + 1)

    try:
        assert await pool.evict_idle(clock.now_ms()) == 0
        assert await pool.peek(contexts.a.tenant_id) is runtime
    finally:
        run.close()


@pytest.mark.asyncio
async def test_pool_evicts_checkpoint_safe_awaiting_user_runtime(pool, contexts, clock):
    runtime = await pool.acquire(contexts.a)
    run = runtime.begin_run()
    run.mark_awaiting_user(checkpoint_persisted=True)

    clock.advance(pool.idle_ttl_ms + 1)

    assert await pool.evict_idle(clock.now_ms()) == 1
    assert await pool.peek(contexts.a.tenant_id) is None


@pytest.mark.asyncio
async def test_pool_returns_capacity_error_when_no_runtime_is_evictable(pool_at_capacity, contexts):
    from multiclaw.runtime.pool import RuntimeCapacityError

    resident = await pool_at_capacity.acquire(contexts.a)
    run = resident.begin_run()

    try:
        with pytest.raises(RuntimeCapacityError) as error:
            await pool_at_capacity.acquire(contexts.b)
    finally:
        run.close()

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


@pytest.mark.asyncio
async def test_close_during_create_closes_new_runtime_and_acquire_fails(contexts, clock):
    from multiclaw.runtime.pool import RuntimePool

    created_runtime = None
    create_started = asyncio.Event()
    release_create = asyncio.Event()

    class BlockingFactory(FakeRuntimeFactory):
        async def create(self, context: TenantContext):
            nonlocal created_runtime
            create_started.set()
            await release_create.wait()
            created_runtime = await super().create(context)
            return created_runtime

    pool = RuntimePool(
        factory=BlockingFactory(clock),
        max_resident_tenants=1,
        idle_ttl_ms=5_000,
        clock=clock,
    )

    acquire_task = asyncio.create_task(pool.acquire(contexts.a))
    await create_started.wait()
    close_task = asyncio.create_task(pool.close())
    release_create.set()

    with pytest.raises(RuntimeError, match="runtime pool is closed"):
        await asyncio.wait_for(acquire_task, timeout=3)
    await asyncio.wait_for(close_task, timeout=3)

    assert created_runtime is not None
    assert created_runtime.mcp_manager.stop_calls == 1
    assert await pool.peek(contexts.a.tenant_id) is None


@pytest.mark.asyncio
async def test_acquire_waiting_on_tenant_lock_fails_after_close(contexts, clock):
    from multiclaw.runtime.pool import RuntimePool

    pool = RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=2,
        idle_ttl_ms=5_000,
        clock=clock,
    )

    tenant_lock = pool._create_locks.setdefault(contexts.a.tenant_id, asyncio.Lock())
    await tenant_lock.acquire()
    acquire_task = asyncio.create_task(pool.acquire(contexts.a))
    await asyncio.sleep(0)

    close_task = asyncio.create_task(pool.close())
    await asyncio.sleep(0)
    tenant_lock.release()

    with pytest.raises(RuntimeError, match="runtime pool is closed"):
        await asyncio.wait_for(acquire_task, timeout=3)
    await asyncio.wait_for(close_task, timeout=3)


@pytest.mark.asyncio
async def test_existing_runtime_fast_path_avoids_tenant_capacity_lock_inversion(contexts, clock):
    from multiclaw.runtime.pool import RuntimeCapacityError, RuntimePool

    pool = RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=1,
        idle_ttl_ms=5_000,
        clock=clock,
    )
    existing = await pool.acquire(contexts.a)
    clock.advance(pool.idle_ttl_ms + 1)

    tenant_lock = pool._create_locks.setdefault(contexts.a.tenant_id, asyncio.Lock())
    await tenant_lock.acquire()

    acquire_existing = asyncio.create_task(pool.acquire(contexts.a))
    await asyncio.sleep(0)
    acquire_other = asyncio.create_task(pool.acquire(contexts.b))
    await asyncio.sleep(0)

    tenant_lock.release()

    returned = await asyncio.wait_for(acquire_existing, timeout=3)
    assert returned is existing
    assert returned.last_used_at_ms == clock.now_ms()
    with pytest.raises(RuntimeCapacityError):
        await asyncio.wait_for(acquire_other, timeout=3)
    assert await pool.peek(contexts.a.tenant_id) is existing


@pytest.mark.asyncio
async def test_runtime_execution_lease_tracks_tool_and_checkpoint_state(clock):
    runtime = _build_runtime_types()(
        tenant_id="tenant-a",
        runtime_instance_id="runtime-1",
        workspace_root=Path("/tmp/runtime-tests/tenant-a/workspace-a"),
        agent=SimpleNamespace(),
        event_bus=EventBus(),
        event_router=FakeEventRouter(),
        scheduler=SimpleNamespace(),
        registry=FakeRegistry(),
        skill_manager=FakeSkillManager(),
        mcp_manager=FakeMcpManager(),
        sandbox_controller=None,
        sandbox_readiness=None,
        last_used_at_ms=clock.now_ms(),
    )

    run = runtime.begin_run()
    run.mark_tool_execution_started()
    run.mark_tool_execution_finished()
    run.mark_awaiting_user(checkpoint_persisted=True)

    assert runtime.active_run_count == 1
    assert runtime.awaiting_user_run_count == 1
    assert runtime.checkpointed_awaiting_user_run_count == 1
    assert runtime.active_executing_run_count == 0
    assert runtime.active_tool_execution_count == 0

    run.close()

    assert runtime.active_run_count == 0
    assert runtime.awaiting_user_run_count == 0
    assert runtime.checkpointed_awaiting_user_run_count == 0
    assert runtime.active_executing_run_count == 0


def test_threading_event_sanity():
    event = threading.Event()
    assert event.is_set() is False
