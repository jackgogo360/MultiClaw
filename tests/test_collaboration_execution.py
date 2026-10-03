from types import SimpleNamespace
import pytest
import asyncio
from test_collaboration_store import scope
from multiclaw.config import Settings
from multiclaw.workflow.coordinator import WorkflowCoordinator


async def test_executor_creates_independent_session_run_and_result(scope, tmp_path):
    from multiclaw.collaboration.execution import AgentJobExecutor
    from multiclaw.collaboration.service import CollaborationService

    database, parent = scope
    settings = Settings(_config_file="/nonexistent")
    calls = []

    class Agent:
        async def handle_message_stream(self, message, **kwargs):
            calls.append((message, kwargs["context"]))
            yield {"type": "done", "content": "verified result", "data": {}}

    async def close():
        pass

    class Factory:
        workspace_resolver = SimpleNamespace(resolve=lambda context: tmp_path)

        async def create_member(self, context, **kwargs):
            return SimpleNamespace(agent=Agent(), event_router=None, close=close, runtime_instance_id="child-test")

    executor = AgentJobExecutor(database, settings=settings, runtime_factory=Factory())
    service = CollaborationService(database, executor=executor)
    executor.service = service
    job = await service.spawn(parent, {"goal": "inspect"})
    await service.tick()
    completed = await service.wait(parent, job["job_id"], timeout=3)
    assert completed["status"] == "completed"
    assert completed["child_session_id"] != parent.session_id
    assert completed["child_run_id"]
    child = calls[0][1]
    assert child.tenant_id == parent.tenant_id and child.workspace_id == parent.workspace_id
    assert child.session_id == completed["child_session_id"]
    assert (await WorkflowCoordinator(database, settings=settings).get_run(child)).status.value == "completed"
    assert completed["result"]["summary"] == "verified result"
    await executor.steer(completed, parent, "new durable instruction")
    from multiclaw.storage.repositories.sessions import SessionRepository
    async with database.connect() as connection:
        messages = await SessionRepository(connection, child, database.dialect).get_messages(child.session_id)
        assert any(message["role"] == "user" and message["content"] == "new durable instruction" for message in messages)
    await service.close()
    async with database.write_transaction() as connection:
        sessions = SessionRepository(connection, parent, database.dialect)
        assert [session.id for session in await sessions.list()] == [parent.session_id]
        await sessions.delete(parent.session_id)
        assert await sessions.get(completed["child_session_id"]) is None


async def test_restart_resumes_owned_run_with_heartbeat_during_long_continuation(scope, tmp_path):
    from sqlalchemy import update
    from multiclaw.collaboration.execution import AgentJobExecutor
    from multiclaw.collaboration.service import CollaborationService
    from multiclaw.storage.schema import agent_runs
    from multiclaw.workflow.continuation import ContinuationOutcome, ContinuationState
    from multiclaw.workflow.recovery import RuntimeRecoveryContinuationService

    database, parent = scope
    settings = Settings(_config_file="/nonexistent", workflow={"heartbeat_ms": 1000, "lease_ttl_ms": 5000})
    started = asyncio.Event()
    counts = {"initial": 0, "resumed": 0}

    class Agent:
        async def handle_message_stream(self, message, **kwargs):
            counts["initial"] += 1
            started.set()
            await asyncio.sleep(30)
            yield {"type": "done", "content": "initial", "data": {}}

        async def resume_recovery(self, **kwargs):
            counts["resumed"] += 1
            await asyncio.sleep(5.5)
            return ContinuationOutcome(state=ContinuationState.COMPLETED, assistant_content="resumed")

    async def close():
        pass

    class Factory:
        workspace_resolver = SimpleNamespace(resolve=lambda context: tmp_path)

        async def create_member(self, context, **kwargs):
            agent = Agent()
            agent.database, agent.settings = database, settings
            return SimpleNamespace(agent=agent, event_router=None, close=close,
                recovery_continuation=RuntimeRecoveryContinuationService(), runtime_instance_id="owned-child")

    executor = AgentJobExecutor(database, settings=settings, runtime_factory=Factory())
    service = CollaborationService(database, executor=executor)
    executor.service = service
    job = await service.spawn(parent, {"goal": "inspect"})
    await service.tick()
    await started.wait()
    await service.close()
    interrupted = await service.get_job(parent, job["job_id"])
    assert interrupted["status"] == "interrupted"
    async with database.write_transaction() as connection:
        await connection.execute(update(agent_runs).where(agent_runs.c.run_id == interrupted["child_run_id"]).values(lease_expires_at=1))
    resumed_executor = AgentJobExecutor(database, settings=settings, runtime_factory=Factory())
    restarted = CollaborationService(database, executor=resumed_executor)
    resumed_executor.service = restarted
    await restarted.tick()
    result = await restarted.wait(parent, job["job_id"], timeout=10)
    assert result["status"] == "completed"
    assert counts == {"initial": 1, "resumed": 1}
    await restarted.close()


async def test_member_runtime_has_fresh_components_and_cannot_approve_external_writes(scope, tmp_path):
    from sandbox_fakes import ReadyRecordingSandboxController
    from multiclaw.runtime.factory import RuntimeFactory

    database, context = scope
    settings = Settings(_config_file="/nonexistent")
    factory = RuntimeFactory(settings=settings, database=database,
        workspace_resolver=SimpleNamespace(resolve=lambda scope, **kwargs: tmp_path),
        sandbox_controller_factory=lambda workspace_root, event_bus: ReadyRecordingSandboxController(workspace_root=workspace_root))
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    reader = await factory.create_member(context, workspace_root=one, profile="reader", model=None, instructions="research")
    writer = await factory.create_member(context, workspace_root=two, profile="writer", model=None, instructions="implement")
    try:
        assert reader.agent is not writer.agent and reader.registry is not writer.registry
        assert reader.registry.get("write_file") is None
        assert writer.registry.get("write_file") is not None
        decision = await writer.scheduler.permission_checker.check("write_file", {"file_path": str(tmp_path / "source.txt")}, two)
        assert not decision.allow and not decision.requires_approval
    finally:
        await reader.close()
        await writer.close()


async def test_writable_actor_approval_resume_and_explicit_acceptance(scope, tmp_path):
    import subprocess
    from sandbox_fakes import ReadyRecordingSandboxController
    from multiclaw.collaboration.execution import AgentJobExecutor
    from multiclaw.collaboration.service import CollaborationService
    from multiclaw.llm import LLMResponse
    from multiclaw.runtime.factory import RuntimeFactory
    from multiclaw.runtime.inference import InferenceRouter
    from multiclaw.storage.repositories.sessions import SessionRepository

    database, parent = scope
    project = tmp_path / "project"
    project.mkdir()
    for arguments in (("init",), ("config", "user.email", "test@example.com"),
                      ("config", "user.name", "Test"), ("commit", "--allow-empty", "-m", "base")):
        subprocess.run(["git", "-C", str(project), *arguments], check=True, capture_output=True)
    settings = Settings(_config_file="/nonexistent", mcp={"enabled": False}, skill={"enabled": False})
    factory = RuntimeFactory(settings=settings, database=database,
        workspace_resolver=SimpleNamespace(resolve=lambda scope, **kwargs: project),
        sandbox_controller_factory=lambda workspace_root, event_bus: ReadyRecordingSandboxController(workspace_root=workspace_root))
    responses = []

    class Router:
        supports_output_limit = True

        async def stream_completion(self, **kwargs):
            yield {"type": "tool_calls", "calls": [{"id": "write-once", "name": "write_file",
                "arguments": {"file_path": "result.txt", "content": "isolated result\n"}}]}

        async def completion(self, **kwargs):
            responses.append(kwargs)
            return LLMResponse(content="Created result.txt with verified evidence")

    original = factory.create_member

    async def create_member(context, **kwargs):
        runtime = await original(context, **kwargs)
        runtime.agent.router = InferenceRouter(Router(), settings=runtime.agent.settings, workspace_root=runtime.workspace_root)
        return runtime

    factory.create_member = create_member
    executor = AgentJobExecutor(database, settings=settings, runtime_factory=factory)
    service = CollaborationService(database, executor=executor)
    executor.service = service
    job = await service.spawn(parent, {"goal": "write result", "profile": "writer"})
    await service.tick()
    waiting = await service.wait(parent, job["job_id"], timeout=5)
    assert waiting["status"] == "awaiting_user"
    assert not (project / "result.txt").exists()
    child = parent.for_run(waiting["child_session_id"], waiting["child_run_id"])
    async with database.connect() as connection:
        approvals = await SessionRepository(connection, child, database.dialect).list_pending_approvals(child.session_id)
    assert len(approvals) == 1
    await WorkflowCoordinator(database, settings=settings).decide_approval(child,
        approvals[0]["approval_id"], approved=True, version=approvals[0]["version"])
    await service.tick()
    done = await service.wait(parent, job["job_id"], timeout=5)
    assert done["status"] == "completed"
    assert len(responses) == 1
    assert not (project / "result.txt").exists()
    workspace = done["result"]["workspace"]
    changes = await executor.manager(parent).diff(workspace)
    await executor.manager(parent).accept(workspace, changes["digest"])
    assert (project / "result.txt").read_text() == "isolated result\n"
    await executor.manager(parent).discard(workspace)
    assert (project / "result.txt").exists()
    await service.close()


async def test_cancel_during_workspace_creation_waits_for_durable_registration(scope, tmp_path):
    import subprocess
    from multiclaw.collaboration.execution import AgentJobExecutor
    from multiclaw.collaboration.service import CollaborationService
    from multiclaw.collaboration.workspaces import GitWorkspaceManager

    database, parent = scope
    project = tmp_path / "project"
    project.mkdir()
    for arguments in (("init",), ("config", "user.email", "test@example.com"),
                      ("config", "user.name", "Test"), ("commit", "--allow-empty", "-m", "base")):
        subprocess.run(["git", "-C", str(project), *arguments], check=True, capture_output=True)
    entered, release = asyncio.Event(), asyncio.Event()

    class Manager(GitWorkspaceManager):
        async def create(self, project_root, job_id):
            entered.set()
            await release.wait()
            return await super().create(project_root, job_id)

    class Factory:
        workspace_resolver = SimpleNamespace(resolve=lambda scope: project)

        async def create_member(self, *args, **kwargs):
            raise AssertionError("cancelled assignment must not start a model")

    executor = AgentJobExecutor(database, settings=Settings(_config_file="/nonexistent"), runtime_factory=Factory())
    manager = Manager(tmp_path / "workspaces")
    executor.manager = lambda context: manager
    service = CollaborationService(database, executor=executor)
    executor.service = service
    job = await service.spawn(parent, {"goal": "write", "profile": "writer"})
    await service.tick()
    await entered.wait()
    cancelled = asyncio.create_task(service.cancel(parent, job["job_id"]))
    await asyncio.sleep(0.03)
    assert not cancelled.done()
    release.set()
    await cancelled
    saved = await service.get_job(parent, job["job_id"])
    assert saved["status"] == "cancelled" and saved["result"].get("workspace")
    await service.discard_session_artifacts(parent)
    assert not (manager.storage_root / job["job_id"]).exists()
    await service.close()
