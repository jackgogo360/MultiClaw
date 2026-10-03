import asyncio
import pytest
from test_collaboration_store import scope


async def test_spawn_returns_durable_handle_before_executor_finishes(scope, tmp_path):
    from multiclaw.collaboration.service import CollaborationService

    database, context = scope
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(job, parent_context):
        assert parent_context == context
        entered.set()
        await release.wait()
        return {"status": "completed", "summary": "isolated result"}

    service = CollaborationService(database, executor=execute, max_workers=2)
    job = await service.spawn(context, {"goal": "inspect"})
    assert job["status"] == "queued"
    await service.tick()
    await entered.wait()
    assert (await service.get_job(context, job["job_id"]))["status"] == "running"
    release.set()
    await service.wait(context, job["job_id"], timeout=2)
    assert (await service.get_job(context, job["job_id"]))["result"]["summary"] == "isolated result"
    await service.close()


async def test_cancel_job_stops_owned_execution(scope):
    from multiclaw.collaboration.service import CollaborationService

    database, context = scope
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def execute(job, parent_context):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    service = CollaborationService(database, executor=execute)
    job = await service.spawn(context, {"goal": "inspect"})
    await service.tick()
    await started.wait()
    await service.cancel(context, job["job_id"])
    assert cancelled.is_set()
    assert (await service.get_job(context, job["job_id"]))["status"] == "cancelled"
    await service.close()


async def test_shutdown_preserves_interrupted_jobs_for_restart(scope):
    from multiclaw.collaboration.service import CollaborationService

    database, context = scope
    started = asyncio.Event()

    async def execute(job, parent_context):
        started.set()
        await asyncio.sleep(30)

    service = CollaborationService(database, executor=execute)
    job = await service.spawn(context, {"goal": "inspect"})
    await service.tick()
    await started.wait()
    await service.close()
    assert (await service.get_job(context, job["job_id"]))["status"] == "interrupted"

    async def resumed(job, parent_context):
        assert job["attempt"] == 2
        return {"status": "completed", "summary": "resumed"}

    restart = CollaborationService(database, executor=resumed)
    await restart.tick()
    assert (await restart.wait(context, job["job_id"], timeout=2))["status"] == "completed"
    await restart.close()


async def test_parent_cancel_propagates_when_dispatcher_is_full(scope):
    from multiclaw.collaboration.service import CollaborationService
    from multiclaw.workflow.coordinator import WorkflowCoordinator

    database, context = scope
    parent = context.for_run(context.session_id, "11111111-1111-4111-8111-111111111111")
    await WorkflowCoordinator(database).start_run_with_checkpoint(parent, "parent")
    started, stopped = asyncio.Event(), asyncio.Event()

    async def execute(job, parent_context):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    service = CollaborationService(database, executor=execute, max_workers=1)
    job = await service.spawn(context, {"goal": "inspect"}, parent_run_id=parent.run_id)
    await service.tick()
    await started.wait()
    await WorkflowCoordinator(database).request_cancellation(parent)
    await service.tick()
    assert stopped.is_set()
    assert (await service.get_job(context, job["job_id"]))["status"] == "cancelled"
    await service.close()


async def test_team_assignments_obey_configured_session_job_limit(scope):
    from multiclaw.collaboration.service import CollaborationService

    database, context = scope

    async def execute(job, parent):
        return {"status": "completed", "summary": "done"}

    service = CollaborationService(database, executor=execute, max_jobs=1)
    team = await service.create_team(context, {"objective": "inspect", "members": [
        {"name": "lead", "role": "leader"}, {"name": "peer"},
    ]})
    async with service.store(context, write=True) as repository:
        await repository.create_task(team["team_id"], "task", "inspect")
    await service.tick()
    assert len(await service.list_jobs(context)) == 1
    await service.close()


async def test_cancellation_callback_failure_still_stops_local_worker(scope):
    from multiclaw.collaboration.service import CollaborationService

    database, context = scope
    started, stopped = asyncio.Event(), asyncio.Event()

    class Executor:
        async def __call__(self, job, parent):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                stopped.set()

        async def cancel(self, job, parent):
            raise RuntimeError("temporary persistence failure")

    service = CollaborationService(database, executor=Executor())
    job = await service.spawn(context, {"goal": "inspect"})
    await service.tick()
    await started.wait()
    with pytest.raises(RuntimeError):
        await service.cancel(context, job["job_id"])
    assert stopped.is_set()
    await service.close()


async def test_dispatch_cursor_advances_past_a_full_page_of_waiting_jobs(scope):
    from uuid import uuid4
    from sqlalchemy import insert, select
    from multiclaw.collaboration.service import CollaborationService
    from multiclaw.storage.schema import agent_jobs
    from multiclaw.storage.uow import TenantUnitOfWork

    database, context = scope
    entered = asyncio.Event()

    class Executor:
        async def ready(self, job, parent):
            return job["status"] == "queued"

        async def __call__(self, job, parent):
            entered.set()
            return {"status": "completed", "summary": "new job"}

    service = CollaborationService(database, executor=Executor(), max_workers=1)
    first = await service.spawn(context, {"goal": "old waiting job"})
    async with database.write_transaction() as connection:
        original = dict((await connection.execute(select(agent_jobs).where(agent_jobs.c.job_id == first["job_id"]))).mappings().first())
        await connection.execute(__import__("sqlalchemy").delete(agent_jobs).where(agent_jobs.c.job_id == first["job_id"]))
        await connection.execute(insert(agent_jobs), [{**original, "job_id": str(uuid4()),
            "status": "awaiting_user", "created_at": 1, "updated_at": 1} for _ in range(100)])
    async with TenantUnitOfWork(database, context) as uow:
        another = await uow.sessions.create()
    parent = context.for_session(another.id)
    newest = await service.spawn(parent, {"goal": "new ready job"})
    await service.tick()
    assert not entered.is_set()
    await service.tick()
    assert (await service.wait(parent, newest["job_id"], timeout=2))["status"] == "completed"
    await service.close()


async def test_cancel_before_claim_cannot_be_resurrected(scope):
    from multiclaw.collaboration.service import CollaborationService

    database, context = scope
    ran = asyncio.Event()

    async def executor(job, parent):
        ran.set()
        return {"status": "completed"}

    service = CollaborationService(database, executor=executor)
    job = await service.spawn(context, {"goal": "inspect"})
    original = service.get_job
    intercept = True

    async def concurrent_cancel(parent, job_id):
        nonlocal intercept
        if intercept:
            intercept = False
            await service.cancel(parent, job_id)
        return await original(parent, job_id)

    service.get_job = concurrent_cancel
    await service.tick()
    await asyncio.sleep(0.02)
    assert not ran.is_set()
    assert (await original(context, job["job_id"]))["status"] == "cancelled"
    await service.close()
