"""Independent worker Runs with isolated runtime state and durable recovery."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select, update

from multiclaw.collaboration.repository import CollaborationRepository
from multiclaw.collaboration.workspaces import GitWorkspaceManager
from multiclaw.events import EventScope
from multiclaw.memory import MemoryEntry
from multiclaw.runtime.inference import inference_scope
from multiclaw.runtime.run_control import RunControl, current_run_control
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.repositories.sessions import SessionRepository
from multiclaw.storage.schema import approval_requests, chat_sessions
from multiclaw.stream import DataStreamEncoder
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.continuation import WorkflowContinuationService
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import RunLeaseHandle, RunStatus, TenantRunQuotaError
from multiclaw.workflow.recovery import RecoveryService, WorkflowRecoveryWorker


class AgentJobExecutor:
    def __init__(self, database, *, settings, runtime_factory, background_runs=None):
        self.database, self.settings, self.factory = database, settings, runtime_factory
        self.background_runs = background_runs
        self.service = None
        self.shutting_down = False
        self.controls = {}
        self.workspaces = {}

    def _child(self, job, context):
        return context.for_run(job["child_session_id"], job["child_run_id"])

    async def ready(self, job, context):
        if not job["child_run_id"]:
            return True
        child = self._child(job, context)
        coordinator = WorkflowCoordinator(self.database, settings=self.settings)
        run = await coordinator.get_run(child)
        if run is None or run.status.value in {"completed", "failed_terminal", "cancelled", "blocked_corrupt", "blocked_incompatible"}:
            return True
        if run.status is RunStatus.AWAITING_USER:
            checkpoint = await coordinator.get_latest_checkpoint(child)
            if checkpoint is None or checkpoint.approval_id is None:
                return False
            async with self.database.connect() as connection:
                resolved = (await connection.execute(select(approval_requests.c.approval_id).where(
                    approval_requests.c.tenant_id == child.tenant_id,
                    approval_requests.c.workspace_id == child.workspace_id,
                    approval_requests.c.session_id == child.session_id,
                    approval_requests.c.run_id == child.run_id,
                    approval_requests.c.approval_id == checkpoint.approval_id,
                    approval_requests.c.approval_status.in_(("approved", "rejected", "expired")),
                ).limit(1))).scalar_one_or_none()
            return resolved is not None
        return not run.lease_expires_at or run.lease_expires_at <= int(time.time() * 1000)

    async def cancel(self, job, context):
        control = self.controls.get(job["job_id"])
        if control is not None:
            control.cancelled = True
        if job["child_run_id"]:
            coordinator = WorkflowCoordinator(self.database, settings=self.settings)
            child = self._child(job, context)
            run = await coordinator.request_cancellation(child)
            if control is None and run.status.value not in {"completed", "cancelled", "failed_terminal", "blocked_corrupt", "blocked_incompatible"}:
                from multiclaw.workflow.models import LeaseConflictError
                try:
                    lease = await (coordinator.resume_waiting_run(child, "collaboration-cancel")
                                   if run.status is RunStatus.AWAITING_USER
                                   else coordinator.acquire_run(child, "collaboration-cancel"))
                    await coordinator.finish_run_with_checkpoint(lease, RunStatus.CANCELLED)
                except LeaseConflictError:
                    pass

    async def steer(self, job, context, message):
        async with self.service.store(context, write=True) as repository:
            current = await repository.get_job(job["job_id"], for_update=True)
            if not current["child_session_id"]:
                request = current["request"]
                request["context"] = (request["context"] + "\n" + message)[-16000:]
                await repository.update_job(job["job_id"], current["version"], request=request)
                return
            child = self._child(current, context)
            memory = MemoryRepository(repository.connection, child, self.database.dialect)
            recent = await memory.recent(1, entry_type="chat_message")
            await memory.save(MemoryEntry(content=message, type="chat_message", role="user",
                session_id=child.session_id, turn_index=(recent[0].turn_index + 1 if recent else 1),
                metadata={"kind": "agent_steering"}))
        control = self.controls.get(job["job_id"])
        if control is not None:
            control.steering.append(message)

    def manager(self, context):
        root = self.factory.workspace_resolver.resolve(context).resolve()
        storage = root.parent / ".agent-workspaces"
        key = str(storage)
        if key not in self.workspaces:
            self.workspaces[key] = GitWorkspaceManager(storage)
        return self.workspaces[key]

    async def validate_request(self, context, request):
        root = self.factory.workspace_resolver.resolve(context).resolve()
        project = (root / request.get("project_path", ".")).resolve(strict=True)
        if not project.is_relative_to(root) or not project.is_dir():
            raise ValueError("project is outside tenant workspace")
        model = request.get("model")
        if model and model not in {self.settings.llm.default_model, *self.settings.llm.model_providers}:
            raise ValueError("member model is not configured")
        if request.get("profile") == "writer":
            manager = self.manager(context)
            try:
                await asyncio.to_thread(manager._project, project)
                await asyncio.to_thread(manager._clean, project)
            except ValueError:
                raise ValueError("writer assignments require a clean Git repository at project_path") from None

    async def _registered_workspace(self, job, context, project):
        """Join threaded Git creation and its checkpoint before delivering cancel."""
        cancelled = False
        creation = asyncio.create_task(self.manager(context).create(project, job["job_id"]))
        while True:
            try:
                workspace = await asyncio.shield(creation)
                break
            except asyncio.CancelledError:
                if creation.cancelled():
                    raise
                cancelled = True

        async def persist():
            async with self.service.store(context, write=True) as repository:
                current = await repository.get_job(job["job_id"], for_update=True)
                return await repository.update_job(job["job_id"], current["version"],
                    result={**current["result"], "workspace": workspace})

        checkpoint = asyncio.create_task(persist())
        while True:
            try:
                saved = await asyncio.shield(checkpoint)
                break
            except asyncio.CancelledError:
                if checkpoint.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return workspace, saved

    async def __call__(self, job, context):
        request = job["request"]
        root = self.factory.workspace_resolver.resolve(context).resolve()
        project = (root / request["project_path"]).resolve(strict=True)
        if not project.is_relative_to(root) or not project.is_dir():
            raise ValueError("project is outside tenant workspace")
        workspace = job["result"].get("workspace")
        if request["profile"] == "writer":
            if workspace is None:
                workspace, job = await self._registered_workspace(job, context, project)
            project = Path(workspace["workspace_path"])
        runtime = await self.factory.create_member(context, workspace_root=project,
            profile=request["profile"], model=request.get("model"), instructions=request["instructions"],
            max_tokens=job["budget_tokens"])
        child, handle = None, None
        subscription = None
        stop = asyncio.Event()
        heartbeat = None
        control_token = None
        summary, failed = "", False
        try:
            if not job["child_run_id"]:
                async with self.database.write_transaction() as connection:
                    repository = CollaborationRepository(connection, context, self.database.dialect)
                    current = await repository.get_job(job["job_id"], for_update=True)
                    request = current["request"]
                    session = await SessionRepository(connection, context, self.database.dialect).create("Agent: " + request["goal"][:100])
                    await connection.execute(update(chat_sessions).where(chat_sessions.c.id == session.id,
                        chat_sessions.c.tenant_id == context.tenant_id).values(metadata_json=json.dumps({"kind": "agent_job", "job_id": job["job_id"]})))
                    child = context.for_run(session.id, str(uuid4()))
                    coordinator = WorkflowCoordinator(self.database, settings=self.settings, connection=connection)
                    try:
                        lease = await coordinator.start_run_with_checkpoint(child, runtime.runtime_instance_id)
                    except TenantRunQuotaError:
                        raise
                    message = request["goal"] + ("\nAssigned context:\n" + request["context"] if request["context"] else "")
                    await MemoryRepository(connection, child, self.database.dialect).save(MemoryEntry(
                        content=message, type="chat_message", role="user", session_id=child.session_id, turn_index=1))
                    current = await repository.get_job(job["job_id"])
                    job = await repository.update_job(job["job_id"], current["version"],
                        child_session_id=child.session_id, child_run_id=child.run_id)
                handle = RunLeaseHandle(lease)
            else:
                child = self._child(job, context)
                run = await WorkflowCoordinator(self.database, settings=self.settings).get_run(child)
                if run and run.status.value in {"completed", "cancelled", "failed_terminal", "blocked_corrupt", "blocked_incompatible"}:
                    return await self._result(job, child, run.status)
            limit_seconds = min(self.settings.runtime.max_run_seconds, self.settings.collaboration.max_job_seconds)
            control = RunControl(child, deadline=time.monotonic() + limit_seconds)
            self.controls[job["job_id"]] = control
            control_token = current_run_control.set(control)
            if job["team_id"]:
                from multiclaw.collaboration.tools import register_member_tools
                register_member_tools(runtime.registry, self.service, context, job)

            async def event(event):
                if self.background_runs is not None:
                    await self.background_runs.record_event(child, DataStreamEncoder.scoped_event(event))

            if runtime.event_router is not None:
                subscription = runtime.event_router.subscribe(EventScope.from_context(child), event)

            async def beat():
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), self.settings.workflow.heartbeat_ms / 1000)
                    except TimeoutError:
                        if handle is not None:
                            await handle.refresh(lambda lease: WorkflowCoordinator(self.database, settings=self.settings).heartbeat(lease))

            heartbeat = asyncio.create_task(beat())
            actor_settings = getattr(runtime.agent, "settings", self.settings)
            with inference_scope(child, settings=actor_settings, database=self.database, workspace_root=project) as budget:
                async with asyncio.timeout(limit_seconds):
                    if handle is not None:
                        message = request["goal"] + ("\nAssigned context:\n" + request["context"] if request["context"] else "")
                        async for item in runtime.agent.handle_message_stream(message, context=child,
                            run_lease_handle=handle, workflow_continuation=WorkflowContinuationService(self.database, settings=self.settings),
                            persisted_user_turn_index=1):
                            if item["type"] == "done":
                                summary = item.get("content", "")
                            if item["type"] == "error":
                                failed = True
                            if self.background_runs is not None:
                                await self.background_runs.record_event(child, DataStreamEncoder.data_part("data-agent-progress", item, transient=True))
                        current = await WorkflowCoordinator(self.database, settings=self.settings).get_run(child)
                        if current.status is not RunStatus.AWAITING_USER:
                            await handle.refresh(lambda lease: WorkflowCoordinator(self.database, settings=self.settings).finish_run_with_checkpoint(
                                lease, RunStatus.FAILED_TERMINAL if failed else RunStatus.COMPLETED))
                    else:
                        class OwnedPool:
                            async def acquire(self, ignored):
                                return runtime
                        worker = WorkflowRecoveryWorker(database=self.database, settings=self.settings, runtime_pool=OwnedPool())
                        await worker.resume_owned_run(child)
                await budget.load()
            run = await WorkflowCoordinator(self.database, settings=self.settings).get_run(child)
            result = await self._result(job, child, run.status)
            result["summary"] = summary or result["summary"]
            result["usage"] = budget.payload()
            return result
        except TenantRunQuotaError:
            return {"status": "interrupted", "summary": "Waiting for tenant execution capacity"}
        except asyncio.CancelledError:
            if child is not None and not self.shutting_down:
                await WorkflowCoordinator(self.database, settings=self.settings).request_cancellation(child)
                if handle is not None:
                    await handle.refresh(lambda lease: WorkflowCoordinator(self.database, settings=self.settings).finish_run_with_checkpoint(lease, RunStatus.CANCELLED))
            raise
        except Exception:
            if child is not None and handle is None:
                return {"status": "interrupted", "summary": "Worker recovery will retry after lease validation"}
            if handle is not None:
                try:
                    await handle.refresh(lambda lease: WorkflowCoordinator(self.database, settings=self.settings).finish_run_with_checkpoint(
                        lease, RunStatus.FAILED_TERMINAL))
                except Exception:
                    # Retain uncertain or fenced workflow state for recovery rather
                    # than falsely marking externally visible work complete.
                    pass
            raise
        finally:
            stop.set()
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if subscription is not None:
                subscription.close()
            self.controls.pop(job["job_id"], None)
            if control_token is not None:
                current_run_control.reset(control_token)
            await runtime.close()

    async def _result(self, job, child, status):
        async with self.database.connect() as connection:
            messages = await SessionRepository(connection, child, self.database.dialect).get_messages(child.session_id, limit=100)
        summary = next((item["content"] for item in reversed(messages) if item["role"] == "assistant"), "")
        result_status = {"completed": "completed", "awaiting_user": "awaiting_user", "cancelled": "cancelled"}.get(status.value, "failed")
        return {"status": result_status, "summary": summary[:16000]}
