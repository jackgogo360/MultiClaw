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


class _FailOnce:
    def __init__(self, label: str, *, fail_first: bool = False) -> None:
        self.label = label
        self.fail_first = fail_first
        self.calls = 0

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError(f"{self.label} failed")


class RetryCloseManager(_FailOnce):
    def stop(self) -> None:
        self._maybe_fail()


class RetryCloseSkillManager(_FailOnce):
    def close(self) -> None:
        self._maybe_fail()


class RetryCloseRegistry(_FailOnce):
    def clear(self) -> None:
        self._maybe_fail()


class RetryCloseEventRouter(_FailOnce):
    def clear(self) -> None:
        self._maybe_fail()


class RetryCloseSecret(_FailOnce):
    def close(self) -> None:
        self._maybe_fail()


class RetryCloseController(_FailOnce):
    def close(self) -> None:
        self._maybe_fail()


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
    from multiclaw.runtime.pool import RuntimePool, _TenantLockEntry

    pool = RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=2,
        idle_ttl_ms=5_000,
        clock=clock,
    )

    tenant_lock = asyncio.Lock()
    pool._tenant_locks[contexts.a.tenant_id] = _TenantLockEntry(lock=tenant_lock, users=0)
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
    from multiclaw.runtime.pool import RuntimeCapacityError, RuntimePool, _TenantLockEntry

    pool = RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=1,
        idle_ttl_ms=5_000,
        clock=clock,
    )
    existing = await pool.acquire(contexts.a)
    clock.advance(pool.idle_ttl_ms + 1)

    tenant_lock = asyncio.Lock()
    pool._tenant_locks[contexts.a.tenant_id] = _TenantLockEntry(lock=tenant_lock, users=0)
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


def test_begin_run_rejects_unavailable_runtime(clock):
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
        clock=clock,
    )

    runtime.mark_unavailable()

    with pytest.raises(RuntimeError, match="runtime is unavailable"):
        runtime.begin_run()


@pytest.mark.asyncio
async def test_runtime_execution_lease_refreshes_last_used_from_injected_clock(clock):
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
        clock=clock,
    )

    clock.advance(500)
    run = runtime.begin_run()
    assert runtime.last_used_at_ms == clock.now_ms()

    clock.advance(750)
    run.close()

    assert runtime.last_used_at_ms == clock.now_ms()
    assert runtime.can_evict(runtime.last_used_at_ms + 5_000, 5_000) is False


@pytest.mark.asyncio
async def test_tenant_runtime_close_retries_only_failed_phases(clock):
    runtime = _build_runtime_types()(
        tenant_id="tenant-a",
        runtime_instance_id="runtime-1",
        workspace_root=Path("/tmp/runtime-tests/tenant-a/workspace-a"),
        agent=SimpleNamespace(),
        event_bus=EventBus(),
        event_router=RetryCloseEventRouter("event-router"),
        scheduler=SimpleNamespace(),
        registry=RetryCloseRegistry("registry", fail_first=True),
        skill_manager=RetryCloseSkillManager("skill-manager"),
        mcp_manager=RetryCloseManager("mcp-manager", fail_first=True),
        sandbox_controller=RetryCloseController("sandbox-controller", fail_first=True),
        sandbox_readiness=None,
        last_used_at_ms=clock.now_ms(),
        secret_handles=[
            RetryCloseSecret("secret-ok"),
            RetryCloseSecret("secret-retry", fail_first=True),
        ],
        clock=clock,
    )

    with pytest.raises(RuntimeError, match="mcp-manager failed") as error:
        await runtime.close()

    assert runtime.mcp_manager.calls == 1
    assert runtime.skill_manager.calls == 1
    assert runtime.registry.calls == 1
    assert runtime.event_router.calls == 1
    assert runtime.sandbox_controller.calls == 1
    assert len(runtime.secret_handles) == 1
    assert error.value.__notes__
    assert any("registry failed" in note for note in error.value.__notes__)
    assert any("secret-retry failed" in note for note in error.value.__notes__)
    assert any("sandbox-controller failed" in note for note in error.value.__notes__)

    await runtime.close()
    await runtime.close()

    assert runtime.mcp_manager.calls == 2
    assert runtime.skill_manager.calls == 1
    assert runtime.registry.calls == 2
    assert runtime.event_router.calls == 1
    assert runtime.sandbox_controller.calls == 2
    assert len(runtime.secret_handles) == 0


@pytest.mark.asyncio
async def test_pool_reclaims_tenant_lock_entries_after_rejected_tenants(clock):
    from multiclaw.runtime.pool import RuntimeCapacityError, RuntimePool

    pool = RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=1,
        idle_ttl_ms=5_000,
        clock=clock,
    )
    resident = await pool.acquire(TenantContext("tenant-a", "workspace-a"))
    run = resident.begin_run()

    try:
        for index in range(50):
            with pytest.raises(RuntimeCapacityError):
                await pool.acquire(TenantContext(f"tenant-{index+1}", "workspace-a"))
    finally:
        run.close()

    assert len(pool._tenant_locks) == 0


@pytest.mark.asyncio
async def test_revoke_keeps_failed_runtime_resident_and_retryable(clock, contexts):
    from multiclaw.runtime.pool import RuntimePool, RuntimeUnavailableError

    runtime = _build_runtime_types()(
        tenant_id=contexts.a.tenant_id,
        runtime_instance_id="runtime-1",
        workspace_root=Path("/tmp/runtime-tests/tenant-a/workspace-a"),
        agent=SimpleNamespace(),
        event_bus=EventBus(),
        event_router=RetryCloseEventRouter("event-router"),
        scheduler=SimpleNamespace(),
        registry=RetryCloseRegistry("registry"),
        skill_manager=RetryCloseSkillManager("skill-manager"),
        mcp_manager=RetryCloseManager("mcp-manager", fail_first=True),
        sandbox_controller=RetryCloseController("sandbox-controller"),
        sandbox_readiness=None,
        last_used_at_ms=clock.now_ms(),
        clock=clock,
    )
    pool = RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=2,
        idle_ttl_ms=5_000,
        clock=clock,
    )
    pool._runtimes[contexts.a.tenant_id] = runtime

    with pytest.raises(RuntimeError, match="mcp-manager failed"):
        await pool.revoke(contexts.a.tenant_id)

    assert await pool.peek(contexts.a.tenant_id) is runtime
    with pytest.raises(RuntimeUnavailableError):
        await pool.acquire(contexts.a)

    await pool.revoke(contexts.a.tenant_id)

    assert await pool.peek(contexts.a.tenant_id) is None
    assert runtime.mcp_manager.calls == 2


@pytest.mark.asyncio
async def test_pool_close_retries_failed_runtimes_without_reclosing_successes(clock):
    from multiclaw.runtime.pool import RuntimePool

    success = _build_runtime_types()(
        tenant_id="tenant-a",
        runtime_instance_id="runtime-a",
        workspace_root=Path("/tmp/runtime-tests/tenant-a/workspace-a"),
        agent=SimpleNamespace(),
        event_bus=EventBus(),
        event_router=RetryCloseEventRouter("event-router-a"),
        scheduler=SimpleNamespace(),
        registry=RetryCloseRegistry("registry-a"),
        skill_manager=RetryCloseSkillManager("skill-manager-a"),
        mcp_manager=RetryCloseManager("mcp-manager-a"),
        sandbox_controller=RetryCloseController("sandbox-controller-a"),
        sandbox_readiness=None,
        last_used_at_ms=clock.now_ms(),
        clock=clock,
    )
    retry = _build_runtime_types()(
        tenant_id="tenant-b",
        runtime_instance_id="runtime-b",
        workspace_root=Path("/tmp/runtime-tests/tenant-b/workspace-a"),
        agent=SimpleNamespace(),
        event_bus=EventBus(),
        event_router=RetryCloseEventRouter("event-router-b"),
        scheduler=SimpleNamespace(),
        registry=RetryCloseRegistry("registry-b"),
        skill_manager=RetryCloseSkillManager("skill-manager-b"),
        mcp_manager=RetryCloseManager("mcp-manager-b", fail_first=True),
        sandbox_controller=RetryCloseController("sandbox-controller-b"),
        sandbox_readiness=None,
        last_used_at_ms=clock.now_ms(),
        clock=clock,
    )
    pool = RuntimePool(
        factory=FakeRuntimeFactory(clock),
        max_resident_tenants=2,
        idle_ttl_ms=5_000,
        clock=clock,
    )
    pool._runtimes = {"tenant-a": success, "tenant-b": retry}

    with pytest.raises(RuntimeError, match="mcp-manager-b failed"):
        await pool.close()

    assert await pool.peek("tenant-a") is None
    assert await pool.peek("tenant-b") is retry
    assert success.mcp_manager.calls == 1
    assert retry.mcp_manager.calls == 1

    await pool.close()

    assert await pool.peek("tenant-b") is None
    assert success.mcp_manager.calls == 1
    assert retry.mcp_manager.calls == 2


def test_threading_event_sanity():
    event = threading.Event()
    assert event.is_set() is False
