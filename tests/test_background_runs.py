from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.storage import Database
from multiclaw.storage.schema import metadata
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext


@pytest.fixture
async def background_scope(tmp_path):
    database = Database.create(DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}"))
    async with database.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(f"{uuid4()}@example.com")
    context = TenantContext(user.id, user.default_workspace_id)
    async with TenantUnitOfWork(database, context) as uow:
        session = await uow.sessions.create()
    yield database, context.for_run(session.id, str(uuid4()))
    await database.dispose()


async def test_disconnected_consumer_does_not_cancel_producer_and_events_replay(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'))
    finish = asyncio.Event()
    completed = asyncio.Event()

    async def producer():
        yield 'data: {"type":"text-delta","delta":"hello"}\n\n'
        await finish.wait()
        completed.set()
        yield 'data: {"type":"finish"}\n\n'

    await manager.start(context, producer())
    consumer = manager.events(context)
    assert 'hello' in await anext(consumer)
    await consumer.aclose()
    finish.set()
    await asyncio.wait_for(completed.wait(), 2)
    await manager.wait(context)
    replay = [chunk async for chunk in manager.events(context)]
    assert len(replay) == 2
    assert 'hello' in replay[0]
    assert not manager.active(context)
    await manager.close()


async def test_event_journal_is_scoped_redacted_and_bounded(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'), max_events=2)

    async def producer():
        for index in range(4):
            yield f'data: {{"type":"data-test","data":{{"api_key":"secret-{index}","index":{index}}}}}\n\n'

    await manager.start(context, producer())
    await manager.wait(context)
    replay = [chunk async for chunk in manager.events(context)]
    assert len(replay) == 2
    assert 'Replay history expired' in replay[0]
    async with TenantUnitOfWork(database, context) as uow:
        stored = await uow.memory.recent(100, entry_type='run_event')
    assert len(stored) == 2
    assert all('secret-' not in entry.content and '[REDACTED]' in entry.content for entry in stored)
    assert [chunk async for chunk in manager.events(context.for_run(context.session_id, str(uuid4())))] == []
    assert [chunk async for chunk in manager.events(TenantContext(str(uuid4()), context.workspace_id, context.session_id, context.run_id))] == []
    await manager.close()


async def test_explicit_cancel_runs_producer_cleanup_and_scoped_steering(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager
    from multiclaw.runtime.run_control import collect_steering

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'))
    started = asyncio.Event()
    receive = asyncio.Event()
    cleaned = asyncio.Event()
    steering = []

    async def producer():
        try:
            started.set()
            await receive.wait()
            steering.extend(await collect_steering(context))
            await asyncio.Event().wait()
            yield ''
        finally:
            cleaned.set()

    await manager.start(context, producer())
    await started.wait()
    await manager.steer(context, 'change direction')
    receive.set()
    await asyncio.sleep(0.02)
    assert steering == ['change direction']
    async with TenantUnitOfWork(database, context) as uow:
        saved = await uow.memory.recent(10, entry_type='chat_message')
    assert saved[0].content == 'change direction'
    assert not await manager.cancel(context.for_run(context.session_id, str(uuid4())))
    assert await manager.cancel(context)
    assert cleaned.is_set()
    await manager.close()


async def test_fifo_queue_waits_for_active_run_and_enforces_bound(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager
    from fastapi import HTTPException

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'))
    manager.max_queue = 2
    gate = asyncio.Event()
    order = []
    finished = asyncio.Event()

    async def producer():
        await gate.wait()
        yield 'data: [DONE]\n\n'

    async def first():
        order.append(1)

    async def second():
        order.append(2)
        finished.set()

    await manager.start(context, producer())
    manager.enqueue(context, first)
    manager.enqueue(context, second)
    with pytest.raises(HTTPException) as error:
        manager.enqueue(context, first)
    assert error.value.status_code == 429
    assert order == []
    gate.set()
    await asyncio.wait_for(finished.wait(), 2)
    assert order == [1, 2]
    await manager.close()


async def test_shutdown_cancels_and_awaits_cleanup_before_return(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'))
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def producer():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield ''
        finally:
            await asyncio.sleep(0.01)
            cleaned.set()

    await manager.start(context, producer())
    await entered.wait()
    await manager.close()
    assert cleaned.is_set()
    assert not manager._producers


def test_background_control_routes_are_registered():
    from multiclaw.server import app

    routes = {route.path for route in app.routes}
    assert '/api/sessions/{session_id}/runs' in routes
    assert '/api/runs/{run_id}/events' in routes
    assert '/api/runs/{run_id}/steer' in routes
    assert '/api/runs/{run_id}/usage' in routes
    assert '/api/sessions/{session_id}/queue' in routes


async def test_queue_status_is_persisted_and_failed_requests_are_visible(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'))
    failed = asyncio.Event()

    async def operation():
        failed.set()
        raise RuntimeError('api_key=never-public')

    queue_id = await manager.queue_message(context, operation, message='next task')
    await failed.wait()
    while manager._queue_workers:
        await asyncio.sleep(0.01)
    rows = await manager.list_queue(context)
    assert rows[0]['queue_id'] == queue_id
    assert rows[0]['status'] == 'failed'
    assert 'never-public' not in rows[0]['error']
    async with TenantUnitOfWork(database, context) as uow:
        assert await uow.memory.recent(100) == []
    await manager.close()


async def test_time_limit_interrupts_hung_producer_and_publishes_reason(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'))
    manager.max_seconds = 0.02
    cleaned = asyncio.Event()

    async def producer():
        try:
            await asyncio.Event().wait()
            yield ''
        finally:
            cleaned.set()

    await manager.start(context, producer())
    await asyncio.wait_for(manager.wait(context), 2)
    assert cleaned.is_set()
    replay = ''.join([chunk async for chunk in manager.events(context)])
    assert 'Run time budget exceeded' in replay
    await manager.close()


async def test_setup_survives_cancelled_http_waiter_and_revoke_awaits_cleanup(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'))
    started = asyncio.Event()
    gate = asyncio.Event()
    cleaned = asyncio.Event()

    async def setup():
        manager.bind_setup_run(context)
        try:
            started.set()
            await gate.wait()
            return 'ready'
        finally:
            cleaned.set()

    waiter = asyncio.create_task(manager.run_setup(context, setup))
    await started.wait()
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert manager.active_session(context)
    assert not cleaned.is_set()
    await manager.revoke(context.tenant_id)
    assert cleaned.is_set()
    assert not manager._setups
    await manager.close()


async def test_admission_defers_queue_without_failing_queued_request(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'))
    completed = asyncio.Event()
    manager.reserve_session(context)
    async def operation():
        completed.set()
    queue_id = await manager.queue_message(context, operation, message='later')
    await asyncio.sleep(0.02)
    assert not completed.is_set()
    assert (await manager.list_queue(context))[0]['status'] == 'queued'
    manager.release_session(context)
    await asyncio.wait_for(completed.wait(), 2)
    await manager.close()


async def test_truncated_replay_returns_explicit_error_instead_of_orphan_deltas(background_scope):
    from multiclaw.runtime.background import BackgroundRunManager

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'), max_events=2)

    async def producer():
        yield 'data: {"type":"text-start","id":"part"}\n\n'
        yield 'data: {"type":"text-delta","id":"part","delta":"one"}\n\n'
        yield 'data: {"type":"text-delta","id":"part","delta":"two"}\n\n'
        yield 'data: {"type":"text-end","id":"part"}\n\n'

    await manager.start(context, producer())
    await manager.wait(context)
    replay = ''.join([chunk async for chunk in manager.events(context)])
    assert 'Replay history expired' in replay
    assert 'text-delta' not in replay
    current = ''.join([chunk async for chunk in manager.events(context, cursor=3)])
    assert 'text-end' in current
    assert 'Replay history expired' not in current
    await manager.close()


async def test_reconnect_control_api_denies_foreign_scope_and_lists_direct_runs(background_scope):
    import httpx
    from fastapi import FastAPI
    from multiclaw.api.dependencies import tenant_context
    from multiclaw.api.runs import router
    from multiclaw.runtime.background import BackgroundRunManager
    from multiclaw.workflow.coordinator import WorkflowCoordinator
    from multiclaw.workflow.models import RunStatus

    database, context = background_scope
    settings = Settings(_config_file='/nonexistent')
    manager = BackgroundRunManager(database, settings)
    workflow = WorkflowCoordinator(database, settings=settings)
    lease = await workflow.start_run_with_checkpoint(context, 'test-runtime')
    await workflow.finish_run_with_checkpoint(lease, RunStatus.COMPLETED)

    async def producer():
        yield 'data: {"type":"finish"}\n\n'

    await manager.start(context, producer())
    await manager.wait(context)
    app = FastAPI()
    app.include_router(router)
    app.state.database = database
    app.state.settings = settings
    app.state.background_runs = manager
    app.dependency_overrides[tenant_context] = lambda: TenantContext(context.tenant_id, context.workspace_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        params = {'session_id': context.session_id}
        replay = await client.get(f'/api/runs/{context.run_id}/events', params=params)
        assert replay.status_code == 200
        assert 'finish' in replay.text
        listed = await client.get(f'/api/sessions/{context.session_id}/runs')
        assert listed.status_code == 200
        assert listed.json()[0]['run_id'] == context.run_id
        usage = await client.get(f'/api/runs/{context.run_id}/usage', params=params)
        assert usage.status_code == 200
        assert usage.json()['limits']['max_run_tokens'] == settings.runtime.max_run_tokens
        app.dependency_overrides[tenant_context] = lambda: TenantContext(str(uuid4()), context.workspace_id)
        for path in (f'/api/runs/{context.run_id}/events', f'/api/runs/{context.run_id}/usage',
                f'/api/sessions/{context.session_id}/runs', f'/api/sessions/{context.session_id}/queue'):
            assert (await client.get(path, params=params)).status_code == 404
        assert (await client.post(f'/api/runs/{context.run_id}/steer',
            json={'session_id': context.session_id, 'message': 'foreign'})).status_code == 404
        assert (await client.post(f'/api/sessions/{context.session_id}/queue',
            json={'message': 'foreign'})).status_code == 404
    await manager.close()


async def test_concurrent_steering_enforces_queue_bound(background_scope):
    from fastapi import HTTPException
    from multiclaw.runtime.background import BackgroundRunManager

    database, context = background_scope
    manager = BackgroundRunManager(database, Settings(_config_file='/nonexistent'))
    manager.max_queue = 2
    started = asyncio.Event()

    async def producer():
        started.set()
        await asyncio.Event().wait()
        yield ''

    await manager.start(context, producer())
    await started.wait()
    results = await asyncio.gather(*(manager.steer(context, f'message {index}') for index in range(5)),
        return_exceptions=True)
    assert sum(result is None for result in results) == 2
    assert all(result is None or isinstance(result, HTTPException) and result.status_code == 429 for result in results)
    await manager.close()


async def test_recovery_defers_reserved_session_before_chat_setup_exists(background_scope):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from multiclaw.runtime.background import BackgroundRunManager
    from multiclaw.workflow.recovery import WorkflowRecoveryWorker, _RecoveryCandidate

    database, context = background_scope
    settings = Settings(_config_file='/nonexistent')
    manager = BackgroundRunManager(database, settings)
    pool = SimpleNamespace(acquire=AsyncMock(side_effect=AssertionError('reserved session must not execute recovery')))
    worker = WorkflowRecoveryWorker(database=database, settings=settings, runtime_pool=pool,
        background_runs=manager)
    manager.reserve_session(context)
    try:
        await worker._process_candidate(_RecoveryCandidate(context=context, awaiting_resolution=False))
        assert not manager.active(context)
        assert not manager._setups
        pool.acquire.assert_not_awaited()
    finally:
        manager.release_session(context)
        await manager.close()
