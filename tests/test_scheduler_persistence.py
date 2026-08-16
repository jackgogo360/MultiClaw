from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from pydantic import BaseModel
from sqlalchemy import select, text, update

from multiclaw.cli import alembic_config
from multiclaw.config import DatabaseSettings, Settings
from multiclaw.events import EventBus
from multiclaw.events import EventRouter
from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
from multiclaw.governance.models import PermissionDecision
from multiclaw.storage import Database
from multiclaw.storage.schema import agent_runs, approval_requests, execution_checkpoints, tool_executions
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.tools import CoreToolScheduler, ToolBuilder, ToolExecutionResult, ToolInvocation, ToolStatus
from multiclaw.tools import ToolRegistry
from multiclaw.tools.read_file import ReadFileToolBuilder
from multiclaw.mcp.tool_adapter import MCPToolBuilder, _extract_text as mcp_extract_text
from multiclaw.mcp.types import ToolCallResult
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    CheckpointPhase,
    ExecutionStatus,
    LeaseConflictError,
    RecoveryStrategy,
    RunLeaseHandle,
    RunStatus,
    StaleFenceError,
    VersionConflictError,
)
from multiclaw.workflow import recovery as recovery_module
from multiclaw.workflow.continuation import WorkflowContinuationService
from multiclaw.agent.multiclaw import MultiClawAgent
from multiclaw.llm import LLMResponse
from multiclaw.planner import Planner
from multiclaw.runtime.factory import _DatabaseBackedMemory
from multiclaw.runtime.models import TenantRuntime
from multiclaw.skills import SkillManager

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'scheduler-persistence.db'}"


async def _upgrade_database(url: str) -> Database:
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=url), "head")
    driver = "mysql" if url.startswith("mysql+aiomysql://") else "sqlite"
    return Database.create(DatabaseSettings(driver=driver, url=url))


@pytest.fixture(params=("sqlite", "mysql"))
async def workflow_database(request, tmp_path):
    if request.param == "mysql":
        url = _ORIGINAL_TEST_MYSQL_URL or os.getenv("MULTICLAW_TEST_MYSQL_URL")
        if not url:
            pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")
    else:
        url = _sqlite_url(tmp_path)

    database = await _upgrade_database(url)
    try:
        yield database
    finally:
        await database.dispose()


async def _seed_user(database: Database, email: str) -> tuple[str, str]:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(email)
        assert user.default_workspace_id is not None
        return user.id, user.default_workspace_id


async def _create_run_context(
    database: Database,
    *,
    email_suffix: str,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
) -> TenantContext:
    if tenant_id is None or workspace_id is None:
        tenant_id, workspace_id = await _seed_user(
            database,
            f"scheduler-persistence{email_suffix}@example.com",
        )
    context = TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)
    async with TenantUnitOfWork(database, context) as uow:
        session = await uow.sessions.create(title="Scheduler Persistence")
    return context.for_run(session.id, str(uuid4()))


def _coordinator(database: Database) -> WorkflowCoordinator:
    return WorkflowCoordinator(database, settings=Settings(_config_file="/nonexistent"))


def _scheduler(database: Database) -> CoreToolScheduler:
    settings = Settings(_config_file="/nonexistent")
    return CoreToolScheduler(
        permission_checker=PermissionChecker(guarded_tools={"guarded_mutation"}),
        execution_guard=ExecutionGuard(timeout=1.0),
        audit_logger=InMemoryAuditLogger(),
        event_bus=EventBus(),
        database=database,
        settings=settings,
    )


async def _count_nonterminal_executions(database: Database, context: TenantContext) -> int:
    async with database.connect() as conn:
        count = await conn.scalar(
            select(text("COUNT(*)")).select_from(tool_executions).where(
                tool_executions.c.tenant_id == context.tenant_id,
                tool_executions.c.workspace_id == context.workspace_id,
                tool_executions.c.session_id == context.session_id,
                tool_executions.c.run_id == context.run_id,
                tool_executions.c.execution_status.not_in(
                    (
                        ExecutionStatus.SUCCEEDED.value,
                        ExecutionStatus.FAILED_TERMINAL.value,
                        ExecutionStatus.UNCERTAIN.value,
                        ExecutionStatus.BLOCKED_INCOMPATIBLE.value,
                        ExecutionStatus.BLOCKED_CORRUPT.value,
                    )
                ),
            )
        )
    return int(count or 0)


async def _approval_and_execution_counts(database: Database, context: TenantContext) -> tuple[int, int]:
    async with database.connect() as conn:
        approval_count = await conn.scalar(
            select(text("COUNT(*)")).select_from(approval_requests).where(
                approval_requests.c.tenant_id == context.tenant_id,
                approval_requests.c.workspace_id == context.workspace_id,
                approval_requests.c.session_id == context.session_id,
                approval_requests.c.run_id == context.run_id,
            )
        )
        execution_count = await conn.scalar(
            select(text("COUNT(*)")).select_from(tool_executions).where(
                tool_executions.c.tenant_id == context.tenant_id,
                tool_executions.c.workspace_id == context.workspace_id,
                tool_executions.c.session_id == context.session_id,
                tool_executions.c.run_id == context.run_id,
            )
        )
    return int(approval_count or 0), int(execution_count or 0)


async def _latest_execution_row(database: Database, context: TenantContext) -> dict[str, object]:
    async with database.connect() as conn:
        result = await conn.execute(
            select(
                tool_executions.c.execution_id,
                tool_executions.c.approval_id,
                tool_executions.c.tool_call_id,
                tool_executions.c.tool_name,
                tool_executions.c.execution_status,
                tool_executions.c.recovery_strategy,
                tool_executions.c.external_request_id,
                tool_executions.c.result_ref,
                tool_executions.c.result_digest,
                tool_executions.c.input_payload_json,
                tool_executions.c.input_hash,
                tool_executions.c.idempotency_key,
                tool_executions.c.version,
            )
            .where(
                tool_executions.c.tenant_id == context.tenant_id,
                tool_executions.c.workspace_id == context.workspace_id,
                tool_executions.c.session_id == context.session_id,
                tool_executions.c.run_id == context.run_id,
            )
            .order_by(tool_executions.c.created_at.desc(), tool_executions.c.execution_id.desc())
            .limit(1)
        )
        row = result.mappings().one()
    return dict(row)


async def _checkpoint_phases(database: Database, context: TenantContext) -> list[str]:
    async with database.connect() as conn:
        result = await conn.execute(
            select(execution_checkpoints.c.phase)
            .where(
                execution_checkpoints.c.tenant_id == context.tenant_id,
                execution_checkpoints.c.workspace_id == context.workspace_id,
                execution_checkpoints.c.session_id == context.session_id,
                execution_checkpoints.c.run_id == context.run_id,
            )
            .order_by(execution_checkpoints.c.checkpoint_seq.asc())
        )
        rows = result.scalars().all()
    return [str(row) for row in rows]


class PersistedParams(BaseModel):
    label: str
    delay: float = 0.0
    idempotency_key: str | None = None


class PersistedInvocation(ToolInvocation[PersistedParams]):
    def __init__(self, name: str, params: PersistedParams, runner) -> None:
        super().__init__(name=name, params=params)
        self._runner = runner

    async def execute(self) -> ToolExecutionResult:
        return await self._runner(self.params)


class ReportingInvocation(PersistedInvocation):
    async def execute(self) -> ToolExecutionResult:
        recorder = getattr(self, "progress_recorder", None)
        assert recorder is not None
        await recorder.record_external_request_id("request-early")
        raise RuntimeError("crash after reporting request id")


class PersistedToolBuilder(ToolBuilder[PersistedParams]):
    parameters_schema = PersistedParams

    def __init__(
        self,
        *,
        name: str,
        runner,
        recovery_strategy: RecoveryStrategy,
        idempotency_key_field: str | None = None,
    ) -> None:
        self.name = name
        self.description = f"persisted {name}"
        self._runner = runner
        self.recovery_strategy = recovery_strategy
        self.idempotency_key_field = idempotency_key_field

    def validate(self, params: dict) -> PersistedParams:
        return PersistedParams(**params)

    def build(self, params: PersistedParams) -> ToolInvocation[PersistedParams]:
        return PersistedInvocation(self.name, params, self._runner)


class ReportingToolBuilder(PersistedToolBuilder):
    def build(self, params: PersistedParams) -> ToolInvocation[PersistedParams]:
        return ReportingInvocation(self.name, params, self._runner)


class _TrackingRuntimeLease:
    def __init__(self, owner: "_TrackingRuntime") -> None:
        self.owner = owner
        self.closed = False
        self.owner.begin_calls += 1

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.owner.closed_calls += 1


class _TrackingContinuation:
    def __init__(self) -> None:
        self.calls: list[tuple[TenantContext, object]] = []

    async def resume(
        self,
        *,
        runtime,
        context: TenantContext,
        run_lease_handle,
        recovery_outcome,
        recovered_tool_result=None,
        recovered_tool_input_json=None,
    ) -> None:
        del runtime, run_lease_handle, recovered_tool_result, recovered_tool_input_json
        self.calls.append((context, recovery_outcome))


class _TrackingRuntime:
    def __init__(self, *, runtime_instance_id: str, scheduler: CoreToolScheduler, builders: list[ToolBuilder]) -> None:
        from multiclaw.tools import ToolRegistry

        self.runtime_instance_id = runtime_instance_id
        self.scheduler = scheduler
        self.registry = ToolRegistry()
        for builder in builders:
            self.registry.register(builder)
        self.begin_calls = 0
        self.closed_calls = 0
        self.recovery_continuation = _TrackingContinuation()

    def begin_run(self) -> _TrackingRuntimeLease:
        return _TrackingRuntimeLease(self)


class _TrackingRuntimePool:
    def __init__(self, runtime: _TrackingRuntime) -> None:
        self.runtime = runtime
        self.acquired: list[TenantContext] = []

    async def acquire(self, context: TenantContext):
        self.acquired.append(context)
        return self.runtime


class _CompletionRouter:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def completion(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _real_runtime(
    *,
    database: Database,
    context: TenantContext,
    router,
    builders: list[ToolBuilder],
) -> TenantRuntime:
    from multiclaw.config import Settings

    settings = Settings(_config_file="/nonexistent")
    registry = ToolRegistry()
    for builder in builders:
        registry.register(builder)
    scheduler = _scheduler(database)
    scheduler.event_router = EventRouter()
    agent = MultiClawAgent(
        settings=settings,
        router=router,
        registry=registry,
        scheduler=scheduler,
        memory=_DatabaseBackedMemory(database),
        planner=Planner(),
        event_bus=EventBus(),
        event_router=EventRouter(),
        skill_manager=SkillManager(project_root=Path.cwd(), max_active=3),
    )
    agent.database = database
    runtime = TenantRuntime(
        tenant_id=context.tenant_id,
        runtime_instance_id="runtime-real",
        workspace_root=Path.cwd(),
        agent=agent,
        event_bus=agent.event_bus,
        event_router=agent.event_router,
        scheduler=scheduler,
        registry=registry,
        skill_manager=agent.skill_manager,
        mcp_manager=None,
        sandbox_controller=None,
        sandbox_readiness=None,
        last_used_at_ms=0,
        recovery_continuation=recovery_module.RuntimeRecoveryContinuationService(),
    )
    return runtime


async def _expire_run_lease(database: Database, context: TenantContext) -> None:
    async with database.write_transaction() as conn:
        await conn.execute(
            update(agent_runs)
            .where(
                agent_runs.c.tenant_id == context.tenant_id,
                agent_runs.c.workspace_id == context.workspace_id,
                agent_runs.c.session_id == context.session_id,
                agent_runs.c.run_id == context.run_id,
            )
            .values(lease_expires_at=database.dialect.db_now_ms() - 1)
        )


async def _seed_dispatch_state(
    database: Database,
    *,
    context: TenantContext,
    lease=None,
    tool_name: str,
    recovery_strategy: RecoveryStrategy,
    raw_params: dict[str, object],
    tool_call_id: str,
    idempotency_key: str | None = None,
    external_request_id: str | None = None,
) -> str:
    scheduler = _scheduler(database)
    coordinator = _coordinator(database)
    if lease is None:
        lease = await coordinator.start_run_with_checkpoint(context, "seed-runtime")
    canonical = scheduler._canonicalize_input(raw_params)
    execution = await coordinator.create_execution(
        lease,
        execution_id=str(uuid4()),
        approval_id=None,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_kind="native",
        recovery_strategy=recovery_strategy,
        idempotency_key=idempotency_key,
        input_payload_json=canonical.payload_json,
        input_hash=canonical.payload_hash,
    )
    assert execution is not None
    await coordinator.transition_execution(
        lease,
        execution.execution_id,
        expected_status=ExecutionStatus.NOT_STARTED,
        expected_version=execution.version,
        target=ExecutionStatus.EXECUTING,
    )
    if external_request_id is not None:
        await coordinator.record_external_request_id(
            lease,
            execution.execution_id,
            expected_status=ExecutionStatus.EXECUTING,
            expected_version=execution.version + 1,
            external_request_id=external_request_id,
        )
        execution_version = execution.version + 2
    else:
        execution_version = execution.version + 1
    await coordinator.checkpoint(
        lease,
        CheckpointPhase.EXECUTION_DISPATCHING,
        {
            "run_id": context.run_id,
            "execution_id": execution.execution_id,
            "tool_call_id": tool_call_id,
            "recovery_strategy": recovery_strategy.value,
            "input_hash": canonical.payload_hash,
            "input_ref": f"tool_execution:{execution.execution_id}:input_payload_json",
            "idempotency_key": idempotency_key,
            "dispatch_cursor": f"dispatch:{context.run_id}:{tool_call_id}",
            "cursor": f"dispatch:{context.run_id}:{tool_call_id}",
        },
        execution_id=execution.execution_id,
        execution_expected_status=ExecutionStatus.EXECUTING,
        execution_expected_version=execution_version,
    )
    await _expire_run_lease(database, context)
    return execution.execution_id


@pytest.mark.asyncio
async def test_same_run_scheduler_calls_use_single_live_execution_slot(workflow_database: Database):
    active = 0
    max_active = 0
    release = asyncio.Event()

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.wait_for(release.wait(), timeout=0.2)
            return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)
        finally:
            active -= 1

    context = await _create_run_context(workflow_database, email_suffix="-same-run")
    lease = await _coordinator(workflow_database).start_run_with_checkpoint(context, "runtime-1")
    lease_handle = RunLeaseHandle(lease)
    scheduler = _scheduler(workflow_database)
    builder = PersistedToolBuilder(
        name="same_run_probe",
        runner=runner,
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
    )

    first = asyncio.create_task(
        scheduler.run(
            builder,
            {"label": "first"},
            context=context,
            call_id="call-1",
            run_lease_handle=lease_handle,
        )
    )
    await asyncio.sleep(0.02)
    second = asyncio.create_task(
        scheduler.run(
            builder,
            {"label": "second"},
            context=context,
            call_id="call-2",
            run_lease_handle=lease_handle,
        )
    )
    await asyncio.sleep(0.02)
    release.set()

    results = await asyncio.gather(first, second, return_exceptions=True)

    scheduler_module = __import__("multiclaw.tools.scheduler", fromlist=["ExecutionConflictError"])
    assert max_active == 1
    assert any(isinstance(item, ToolExecutionResult) for item in results)
    assert sum(
        isinstance(item, getattr(scheduler_module, "ExecutionConflictError", RuntimeError))
        for item in results
    ) == 1
    assert await _count_nonterminal_executions(workflow_database, context) == 0


@pytest.mark.asyncio
async def test_different_runs_can_overlap_under_same_tenant(workflow_database: Database):
    active = 0
    max_active = 0
    both_started = asyncio.Event()
    started = 0

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        nonlocal active, max_active, started
        active += 1
        started += 1
        max_active = max(max_active, active)
        if started == 2:
            both_started.set()
        try:
            await asyncio.wait_for(both_started.wait(), timeout=0.2)
            await asyncio.sleep(params.delay)
            return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)
        finally:
            active -= 1

    tenant_id, workspace_id = await _seed_user(
        workflow_database,
        "scheduler-persistence-cross-run@example.com",
    )
    first_context = await _create_run_context(
        workflow_database,
        email_suffix="-cross-run-1",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    second_context = await _create_run_context(
        workflow_database,
        email_suffix="-cross-run-2",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    coordinator = _coordinator(workflow_database)
    first_lease = await coordinator.start_run_with_checkpoint(first_context, "runtime-1")
    second_lease = await coordinator.start_run_with_checkpoint(second_context, "runtime-2")
    scheduler = _scheduler(workflow_database)
    builder = PersistedToolBuilder(
        name="cross_run_probe",
        runner=runner,
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
    )

    results = await asyncio.gather(
        scheduler.run(
            builder,
            {"label": "first", "delay": 0.01},
            context=first_context,
            call_id="call-1",
            run_lease_handle=RunLeaseHandle(first_lease),
        ),
        scheduler.run(
            builder,
            {"label": "second", "delay": 0.01},
            context=second_context,
            call_id="call-2",
            run_lease_handle=RunLeaseHandle(second_lease),
        ),
    )

    assert max_active == 2
    assert [result.content for result in results] == ["first", "second"]


@pytest.mark.asyncio
async def test_approval_api_persists_not_started_plan_and_worker_executes_after_run_once(
    workflow_database: Database,
):
    import multiclaw.server as server

    scheduler = _scheduler(workflow_database)
    context = await _create_run_context(workflow_database, email_suffix="-approval-api")
    lease = await _coordinator(workflow_database).start_run_with_checkpoint(context, "runtime-approval")
    call_count = 0

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        nonlocal call_count
        call_count += 1
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)

    result = await scheduler.run(
        PersistedToolBuilder(
            name="guarded_mutation",
            runner=runner,
            recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
        ),
        {"label": "danger"},
        context=context,
        call_id="call-approval",
        run_lease_handle=RunLeaseHandle(lease),
    )
    assert result.status == ToolStatus.AWAITING_APPROVAL
    approval_id = result.data["approval_id"]

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                database=workflow_database,
                settings=Settings(_config_file="/nonexistent"),
            )
        )
    )
    response = await server.approve(
        server.ApprovalDecisionRequest(
            approval_id=approval_id,
            approved=True,
            version=1,
        ),
        request,
        context,
    )

    approval_count, execution_count = await _approval_and_execution_counts(workflow_database, context)
    execution_row = await _latest_execution_row(workflow_database, context)
    assert getattr(response, "status_code", 200) == 200
    assert approval_count == 1
    assert execution_count == 1
    assert execution_row["approval_id"] == approval_id
    assert execution_row["execution_status"] == ExecutionStatus.NOT_STARTED.value
    assert call_count == 0

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-recovered",
        scheduler=_scheduler(workflow_database),
        builders=[
            PersistedToolBuilder(
                name="guarded_mutation",
                runner=runner,
                recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
            )
        ],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    execution_row = await _latest_execution_row(workflow_database, context)
    assert call_count == 1
    assert execution_row["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert runtime.begin_calls == 1
    assert runtime.closed_calls == 1


@pytest.mark.asyncio
async def test_worker_invokes_runtime_continuation_for_resume_model(
    workflow_database: Database,
):
    context = await _create_run_context(workflow_database, email_suffix="-resume-model")
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-resume")
    await _expire_run_lease(workflow_database, context)

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-resume-new",
        scheduler=_scheduler(workflow_database),
        builders=[],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    assert runtime.recovery_continuation.calls
    resumed_context, outcome = runtime.recovery_continuation.calls[0]
    assert resumed_context == context
    assert outcome.action is not None
    assert outcome.action.value == "resume_model"
    assert runtime.begin_calls == 1
    assert runtime.closed_calls == 1


@pytest.mark.asyncio
async def test_worker_replays_read_only_dispatch_from_persisted_input(
    workflow_database: Database,
):
    call_count = 0

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        nonlocal call_count
        call_count += 1
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=f"replayed:{params.label}",
        )

    context = await _create_run_context(workflow_database, email_suffix="-replay")
    await _seed_dispatch_state(
        workflow_database,
        context=context,
        tool_name="read_only_probe",
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        raw_params={"label": "replay-me", "delay": 0.0, "idempotency_key": None},
        tool_call_id="call-replay",
    )

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-replay",
        scheduler=_scheduler(workflow_database),
        builders=[
            PersistedToolBuilder(
                name="read_only_probe",
                runner=runner,
                recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
            )
        ],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    execution_row = await _latest_execution_row(workflow_database, context)
    assert call_count == 1
    assert execution_row["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert runtime.closed_calls == 1


@pytest.mark.asyncio
async def test_worker_retries_idempotent_dispatch_with_same_key(
    workflow_database: Database,
):
    call_keys: list[str | None] = []

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        call_keys.append(params.idempotency_key)
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=f"retried:{params.label}",
        )

    context = await _create_run_context(workflow_database, email_suffix="-retry")
    await _seed_dispatch_state(
        workflow_database,
        context=context,
        tool_name="idempotent_probe",
        recovery_strategy=RecoveryStrategy.IDEMPOTENT_RETRY,
        raw_params={"label": "retry-me", "delay": 0.0, "idempotency_key": "idem-123"},
        tool_call_id="call-retry",
        idempotency_key="idem-123",
    )

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-retry",
        scheduler=_scheduler(workflow_database),
        builders=[
            PersistedToolBuilder(
                name="idempotent_probe",
                runner=runner,
                recovery_strategy=RecoveryStrategy.IDEMPOTENT_RETRY,
                idempotency_key_field="idempotency_key",
            )
        ],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    execution_row = await _latest_execution_row(workflow_database, context)
    assert call_keys == ["idem-123"]
    assert execution_row["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert execution_row["idempotency_key"] == "idem-123"


@pytest.mark.asyncio
async def test_worker_marks_manual_uncertain_without_recalling_tool(
    workflow_database: Database,
):
    call_count = 0

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        nonlocal call_count
        call_count += 1
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=params.label,
        )

    context = await _create_run_context(workflow_database, email_suffix="-manual-uncertain")
    await _seed_dispatch_state(
        workflow_database,
        context=context,
        tool_name="manual_uncertain_probe",
        recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
        raw_params={"label": "once", "delay": 0.0, "idempotency_key": None},
        tool_call_id="call-uncertain",
        external_request_id="request-123",
    )

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-manual",
        scheduler=_scheduler(workflow_database),
        builders=[
            PersistedToolBuilder(
                name="manual_uncertain_probe",
                runner=runner,
                recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
            )
        ],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    execution_row = await _latest_execution_row(workflow_database, context)
    assert call_count == 0
    assert execution_row["execution_status"] == ExecutionStatus.UNCERTAIN.value
    assert execution_row["external_request_id"] == "request-123"


@pytest.mark.asyncio
async def test_external_request_id_progress_survives_crash_before_terminal_persist(
    workflow_database: Database,
    monkeypatch,
):
    scheduler = _scheduler(workflow_database)
    context = await _create_run_context(workflow_database, email_suffix="-progress-crash")
    lease = await _coordinator(workflow_database).start_run_with_checkpoint(context, "runtime-progress")

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        del params
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content="unreachable",
        )

    async def crash_persist(*args, **kwargs):
        raise RuntimeError("crash after progress callback")

    monkeypatch.setattr(scheduler, "_persist_execution_result", crash_persist)

    result = await scheduler.run(
        ReportingToolBuilder(
            name="progress_probe",
            runner=runner,
            recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
        ),
        {"label": "progress"},
        context=context,
        call_id="call-progress",
        run_lease_handle=RunLeaseHandle(lease),
    )
    assert result.status == ToolStatus.ERROR

    execution_row = await _latest_execution_row(workflow_database, context)
    assert execution_row["external_request_id"] == "request-early"
    assert execution_row["execution_status"] == ExecutionStatus.EXECUTING.value
    await _expire_run_lease(workflow_database, context)

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-progress-recovery",
        scheduler=_scheduler(workflow_database),
        builders=[
            ReportingToolBuilder(
                name="progress_probe",
                runner=runner,
                recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
            )
        ],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    execution_row = await _latest_execution_row(workflow_database, context)
    assert execution_row["external_request_id"] == "request-early"
    assert execution_row["execution_status"] == ExecutionStatus.UNCERTAIN.value


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hash_mismatch", "missing_builder", "strategy_mismatch"])
async def test_worker_blocks_fail_closed_for_invalid_replay_inputs(
    workflow_database: Database,
    mode: str,
):
    call_count = 0

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        nonlocal call_count
        call_count += 1
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)

    context = await _create_run_context(workflow_database, email_suffix=f"-blocked-{mode}")
    execution_id = await _seed_dispatch_state(
        workflow_database,
        context=context,
        tool_name="blocked_probe",
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        raw_params={"label": "blocked", "delay": 0.0, "idempotency_key": None},
        tool_call_id=f"call-{mode}",
    )

    if mode == "hash_mismatch":
        async with workflow_database.write_transaction() as conn:
            await conn.execute(
                update(tool_executions)
                .where(tool_executions.c.execution_id == execution_id)
                .values(input_hash="f" * 64)
            )

    builder_strategy = (
        RecoveryStrategy.MANUAL_UNCERTAIN if mode == "strategy_mismatch" else RecoveryStrategy.READ_ONLY_REPLAY
    )
    builders: list[ToolBuilder] = []
    if mode != "missing_builder":
        builders.append(
            PersistedToolBuilder(
                name="blocked_probe",
                runner=runner,
                recovery_strategy=builder_strategy,
            )
        )

    runtime = _TrackingRuntime(
        runtime_instance_id=f"runtime-{mode}",
        scheduler=_scheduler(workflow_database),
        builders=builders,
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    row = await _latest_execution_row(workflow_database, context)
    assert call_count == 0
    assert row["execution_status"] in {
        ExecutionStatus.BLOCKED_CORRUPT.value,
        ExecutionStatus.BLOCKED_INCOMPATIBLE.value,
    }


@pytest.mark.asyncio
async def test_worker_candidate_failure_isolated_and_runtime_released(
    workflow_database: Database,
):
    failing_context = await _create_run_context(workflow_database, email_suffix="-fail-isolated")
    good_context = await _create_run_context(workflow_database, email_suffix="-good-isolated")
    failing_coordinator = _coordinator(workflow_database)
    await failing_coordinator.start_run_with_checkpoint(failing_context, "runtime-failing")
    await _expire_run_lease(workflow_database, failing_context)
    await _seed_dispatch_state(
        workflow_database,
        context=good_context,
        tool_name="good_probe",
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        raw_params={"label": "good", "delay": 0.0, "idempotency_key": None},
        tool_call_id="call-good",
    )

    async def good_runner(params: PersistedParams) -> ToolExecutionResult:
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)

    class _ExplodingContinuation:
        async def resume(self, **kwargs) -> None:
            del kwargs
            raise RuntimeError("boom")

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-isolated",
        scheduler=_scheduler(workflow_database),
        builders=[
            PersistedToolBuilder(
                name="good_probe",
                runner=good_runner,
                recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
            )
        ],
    )
    runtime.recovery_continuation = _ExplodingContinuation()
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )

    await worker.run_once()

    failing_run = await failing_coordinator.get_run(failing_context)
    good_execution = await _latest_execution_row(workflow_database, good_context)
    assert failing_run is not None
    assert good_execution["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert runtime.begin_calls == 2
    assert runtime.closed_calls == 2


@pytest.mark.asyncio
async def test_worker_does_not_revive_terminal_run(
    workflow_database: Database,
):
    context = await _create_run_context(workflow_database, email_suffix="-terminal-noop")
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-terminal")
    await coordinator.finish_run_with_checkpoint(lease, RunStatus.CANCELLED)
    await _expire_run_lease(workflow_database, context)

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-terminal-recovery",
        scheduler=_scheduler(workflow_database),
        builders=[],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    run = await coordinator.get_run(context)
    assert run is not None
    assert run.status is RunStatus.CANCELLED
    assert runtime.begin_calls == 0
    assert runtime.closed_calls == 0


@pytest.mark.asyncio
async def test_resume_model_uses_real_agent_router_and_completes_run(
    workflow_database: Database,
):
    context = await _create_run_context(workflow_database, email_suffix="-real-resume")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        await uow.memory.save(
            __import__("multiclaw.memory", fromlist=["MemoryEntry"]).MemoryEntry(
                content="resume me",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-started")
    await _expire_run_lease(workflow_database, context)

    router = _CompletionRouter(
        [LLMResponse(content="recovered assistant", tool_calls=[], reasoning_content="")]
    )
    runtime = _real_runtime(database=workflow_database, context=context, router=router, builders=[])
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    assert len(router.calls) == 1
    messages = router.calls[0]["messages"]
    assert [message for message in messages if message["role"] == "user"] == [
        {"role": "user", "content": "resume me"}
    ]
    async with TenantUnitOfWork(workflow_database, context) as uow:
        recent = await uow.memory.recent(limit=5, entry_type="chat_message")
    assert [entry.content for entry in reversed(recent)] == ["resume me", "recovered assistant"]
    run = await coordinator.get_run(context)
    assert run is not None
    assert run.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_approved_tool_post_result_continues_model_with_persisted_result(
    workflow_database: Database,
):
    context = await _create_run_context(workflow_database, email_suffix="-approved-continue")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        await uow.memory.save(
            __import__("multiclaw.memory", fromlist=["MemoryEntry"]).MemoryEntry(
                content="run approved tool",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-approved")
    scheduler = _scheduler(workflow_database)

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=f"tool:{params.label}")

    result = await scheduler.run(
        PersistedToolBuilder(
            name="guarded_mutation",
            runner=runner,
            recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
        ),
        {"label": "payload"},
        context=context,
        call_id="call-approved",
        run_lease_handle=RunLeaseHandle(lease),
    )
    approval_id = result.data["approval_id"]
    await _coordinator(workflow_database).decide_approval(
        context,
        approval_id,
        approved=True,
        version=result.data["version"],
    )

    router = _CompletionRouter(
        [LLMResponse(content="continued after tool", tool_calls=[], reasoning_content="")]
    )
    runtime = _real_runtime(
        database=workflow_database,
        context=context,
        router=router,
        builders=[
            PersistedToolBuilder(
                name="guarded_mutation",
                runner=runner,
                recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
            )
        ],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    assert len(router.calls) == 1
    messages = router.calls[0]["messages"]
    tool_result_message = next(message for message in messages if message["role"] == "tool")
    assert tool_result_message["content"] == "tool:payload"
    assistant_tool_call = next(message for message in messages if message.get("tool_calls"))
    assert assistant_tool_call["tool_calls"][0]["id"] == "call-approved"
    run = await coordinator.get_run(context)
    assert run is not None
    assert run.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_approved_tool_continuation_stops_at_new_awaiting_user_boundary(
    workflow_database: Database,
):
    context = await _create_run_context(workflow_database, email_suffix="-approved-awaiting")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        await uow.memory.save(
            __import__("multiclaw.memory", fromlist=["MemoryEntry"]).MemoryEntry(
                content="resume into approval",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-approved-awaiting")
    scheduler = _scheduler(workflow_database)
    executed = 0

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        nonlocal executed
        executed += 1
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=f"tool:{params.label}")

    result = await scheduler.run(
        PersistedToolBuilder(
            name="guarded_mutation",
            runner=runner,
            recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
        ),
        {"label": "payload"},
        context=context,
        call_id="call-approved-awaiting",
        run_lease_handle=RunLeaseHandle(lease),
    )
    await coordinator.decide_approval(
        context,
        result.data["approval_id"],
        approved=True,
        version=result.data["version"],
    )

    router = _CompletionRouter(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call-followup",
                        "name": "guarded_mutation",
                        "arguments": {"label": "followup"},
                    }
                ],
                reasoning_content="",
            )
        ]
    )
    runtime = _real_runtime(
        database=workflow_database,
        context=context,
        router=router,
        builders=[
            PersistedToolBuilder(
                name="guarded_mutation",
                runner=runner,
                recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
            )
        ],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    run = await coordinator.get_run(context)
    assert run is not None
    assert run.status is RunStatus.AWAITING_USER
    approvals, executions = await _approval_and_execution_counts(workflow_database, context)
    assert approvals == 2
    assert executions == 2
    latest = await _latest_execution_row(workflow_database, context)
    assert latest["tool_call_id"] == "call-followup"
    assert latest["execution_status"] == ExecutionStatus.NOT_STARTED.value
    phases = await _checkpoint_phases(workflow_database, context)
    assert "run_terminal" not in phases
    assert executed == 1


@pytest.mark.asyncio
async def test_resume_model_router_failure_marks_failed_terminal(
    workflow_database: Database,
):
    context = await _create_run_context(workflow_database, email_suffix="-resume-fail")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        await uow.memory.save(
            __import__("multiclaw.memory", fromlist=["MemoryEntry"]).MemoryEntry(
                content="resume and fail",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
    coordinator = _coordinator(workflow_database)
    await coordinator.start_run_with_checkpoint(context, "runtime-resume-fail")
    await _expire_run_lease(workflow_database, context)

    router = _CompletionRouter([RuntimeError("router failed")])
    runtime = _real_runtime(database=workflow_database, context=context, router=router, builders=[])
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    run = await coordinator.get_run(context)
    assert run is not None
    assert run.status is RunStatus.FAILED_TERMINAL


@pytest.mark.asyncio
async def test_resume_model_cancellation_does_not_false_terminalize(
    workflow_database: Database,
):
    context = await _create_run_context(workflow_database, email_suffix="-resume-cancel")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        await uow.memory.save(
            __import__("multiclaw.memory", fromlist=["MemoryEntry"]).MemoryEntry(
                content="resume and cancel",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
    coordinator = _coordinator(workflow_database)
    await coordinator.start_run_with_checkpoint(context, "runtime-resume-cancel")
    await _expire_run_lease(workflow_database, context)

    router = _CompletionRouter([asyncio.CancelledError()])
    runtime = _real_runtime(database=workflow_database, context=context, router=router, builders=[])
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run_once()

    run = await coordinator.get_run(context)
    assert run is not None
    assert run.status in {RunStatus.RUNNING, RunStatus.RESUMING}
    phases = await _checkpoint_phases(workflow_database, context)
    assert "run_terminal" not in phases


@pytest.mark.asyncio
async def test_real_mcp_adapter_reports_external_id_before_crash_and_recovery_marks_uncertain(
    workflow_database: Database,
    monkeypatch,
):
    context = await _create_run_context(workflow_database, email_suffix="-mcp-early-id")
    lease = await _coordinator(workflow_database).start_run_with_checkpoint(context, "runtime-mcp")

    class Manager:
        def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> ToolCallResult:
            del server_name, tool_name, arguments
            return ToolCallResult(
                content=[{"type": "text", "text": "hello"}],
                external_request_id="mcp-request-123",
            )

    builder = MCPToolBuilder(
        name="mcp__demo__tool",
        server_name="demo",
        original_name="tool",
        description="demo",
        input_schema={"properties": {"label": {"type": "string"}}},
        manager=Manager(),
        read_only=False,
    )
    monkeypatch.setattr("multiclaw.mcp.tool_adapter._extract_text", lambda content: (_ for _ in ()).throw(RuntimeError("after-report crash")))

    result = await _scheduler(workflow_database).run(
        builder,
        {"label": "x"},
        context=context,
        call_id="call-mcp",
        run_lease_handle=RunLeaseHandle(lease),
    )
    assert result.status is ToolStatus.ERROR

    row = await _latest_execution_row(workflow_database, context)
    assert row["external_request_id"] == "mcp-request-123"
    assert row["execution_status"] == ExecutionStatus.UNCERTAIN.value


@pytest.mark.asyncio
async def test_recovery_external_read_uses_current_approved_roots(
    workflow_database: Database,
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside text\n", encoding="utf-8")
    context = await _create_run_context(workflow_database, email_suffix="-external-read")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        from multiclaw.memory import MemoryEntry

        await uow.memory.save(
            MemoryEntry(
                content="read outside",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-external-read")
    scheduler = _scheduler(workflow_database)
    builder = ReadFileToolBuilder(str(workspace))
    result = await scheduler.run(
        builder,
        {"file_path": str(outside)},
        context=context,
        call_id="call-read-outside",
        run_lease_handle=RunLeaseHandle(lease),
    )
    await coordinator.decide_approval(
        context,
        result.data["approval_id"],
        approved=True,
        version=result.data["version"],
    )

    router = _CompletionRouter([LLMResponse(content="read complete", tool_calls=[], reasoning_content="")])
    runtime = _real_runtime(database=workflow_database, context=context, router=router, builders=[ReadFileToolBuilder(str(workspace))])
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    row = await _latest_execution_row(workflow_database, context)
    assert row["execution_status"] == ExecutionStatus.SUCCEEDED.value
    loaded = await WorkflowContinuationService(workflow_database, settings=Settings(_config_file="/nonexistent")).load_tool_result(
        context=context,
        result_ref=str(row["result_ref"]),
        expected_digest=str(row["result_digest"]),
        expected_execution_id=str(row["execution_id"]),
        expected_tool_call_id=str(row["tool_call_id"]),
        expected_tool_name=str(row["tool_name"]),
    )
    assert "outside text" in loaded.content


@pytest.mark.asyncio
async def test_recovery_policy_change_denies_external_read_fails_closed(
    workflow_database: Database,
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside text\n", encoding="utf-8")
    context = await _create_run_context(workflow_database, email_suffix="-external-deny")
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-external-deny")
    result = await _scheduler(workflow_database).run(
        ReadFileToolBuilder(str(workspace)),
        {"file_path": str(outside)},
        context=context,
        call_id="call-read-deny",
        run_lease_handle=RunLeaseHandle(lease),
    )
    await coordinator.decide_approval(
        context,
        result.data["approval_id"],
        approved=True,
        version=result.data["version"],
    )

    router = _CompletionRouter([LLMResponse(content="should not run", tool_calls=[], reasoning_content="")])
    runtime = _real_runtime(database=workflow_database, context=context, router=router, builders=[ReadFileToolBuilder(str(workspace))])

    async def deny(*args, **kwargs):
        del args, kwargs
        return PermissionDecision(allow=False, requires_approval=False, reason="policy_changed")

    monkeypatch.setattr(runtime.scheduler.permission_checker, "check", deny)
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    row = await _latest_execution_row(workflow_database, context)
    assert row["execution_status"] == ExecutionStatus.BLOCKED_INCOMPATIBLE.value
    assert router.calls == []


@pytest.mark.asyncio
async def test_rejected_approval_is_candidate_immediately_and_resumes_without_execution(
    workflow_database: Database,
):
    context = await _create_run_context(workflow_database, email_suffix="-reject-now")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        from multiclaw.memory import MemoryEntry

        await uow.memory.save(
            MemoryEntry(
                content="reject this",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-reject")
    executed = 0

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        nonlocal executed
        executed += 1
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)

    result = await _scheduler(workflow_database).run(
        PersistedToolBuilder(
            name="guarded_mutation",
            runner=runner,
            recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
        ),
        {"label": "danger"},
        context=context,
        call_id="call-reject",
        run_lease_handle=RunLeaseHandle(lease),
    )
    await coordinator.decide_approval(
        context,
        result.data["approval_id"],
        approved=False,
        version=result.data["version"],
    )

    router = _CompletionRouter([LLMResponse(content="rejection handled", tool_calls=[], reasoning_content="")])
    runtime = _real_runtime(
        database=workflow_database,
        context=context,
        router=router,
        builders=[
            PersistedToolBuilder(
                name="guarded_mutation",
                runner=runner,
                recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
            )
        ],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    run = await coordinator.get_run(context)
    assert run is not None
    assert run.status is RunStatus.COMPLETED
    assert executed == 0
    assert len(router.calls) == 1


@pytest.mark.asyncio
async def test_unresolved_awaiting_user_approval_is_not_candidate(
    workflow_database: Database,
):
    async def runner(params: PersistedParams) -> ToolExecutionResult:
        raise AssertionError("must not run")

    context = await _create_run_context(workflow_database, email_suffix="-awaiting-not-candidate")
    lease = await _coordinator(workflow_database).start_run_with_checkpoint(context, "runtime-awaiting")
    result = await _scheduler(workflow_database).run(
        PersistedToolBuilder(
            name="guarded_mutation",
            runner=runner,
            recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
        ),
        {"label": "danger"},
        context=context,
        call_id="call-awaiting",
        run_lease_handle=RunLeaseHandle(lease),
    )
    assert result.status is ToolStatus.AWAITING_APPROVAL

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-awaiting",
        scheduler=_scheduler(workflow_database),
        builders=[],
    )
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()
    assert runtime.begin_calls == 0


@pytest.mark.asyncio
async def test_real_mcp_adapter_stale_fence_during_early_id_propagates(
    workflow_database: Database,
    monkeypatch,
):
    import multiclaw.tools.scheduler as scheduler_module

    context = await _create_run_context(workflow_database, email_suffix="-mcp-stale")
    lease = await _coordinator(workflow_database).start_run_with_checkpoint(context, "runtime-mcp-stale")

    class Manager:
        def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> ToolCallResult:
            del server_name, tool_name, arguments
            return ToolCallResult(
                content=[{"type": "text", "text": "hello"}],
                external_request_id="mcp-stale-123",
            )

    builder = MCPToolBuilder(
        name="mcp__demo__tool",
        server_name="demo",
        original_name="tool",
        description="demo",
        input_schema={"properties": {"label": {"type": "string"}}},
        manager=Manager(),
        read_only=False,
    )

    original_record = scheduler_module._ExecutionProgressRecorder.record_external_request_id

    async def stale_then_record(self, external_request_id: str):
        await _expire_run_lease(workflow_database, context)
        await _coordinator(workflow_database).acquire_run(context, "takeover-owner")
        return await original_record(self, external_request_id)

    monkeypatch.setattr(
        scheduler_module._ExecutionProgressRecorder,
        "record_external_request_id",
        stale_then_record,
    )

    with pytest.raises((StaleFenceError, VersionConflictError, LeaseConflictError)):
        await _scheduler(workflow_database).run(
            builder,
            {"label": "x"},
            context=context,
            call_id="call-mcp-stale",
            run_lease_handle=RunLeaseHandle(lease),
        )

    row = await _latest_execution_row(workflow_database, context)
    assert row["execution_status"] == ExecutionStatus.EXECUTING.value
    assert row["result_ref"] is None
    phases = await _checkpoint_phases(workflow_database, context)
    assert "execution_result_observed" not in phases


@pytest.mark.asyncio
async def test_memory_tool_result_loader_rejects_type_confusion_and_noncanonical_refs(
    workflow_database: Database,
):
    from multiclaw.memory import MemoryEntry

    context = await _create_run_context(workflow_database, email_suffix="-memory-loader")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        chat = await uow.memory.save(
            MemoryEntry(
                content="same digest payload",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
        tool = await uow.memory.save(
            MemoryEntry(
                content="tool secret payload",
                type="tool_result",
                role="tool",
                session_id=context.session_id,
                metadata={
                    "tool_call_id": "call-1",
                    "tool_name": "demo_tool",
                    "execution_id": "exec-1",
                    "result_status": "succeeded",
                },
            )
        )
    service = WorkflowContinuationService(workflow_database, settings=Settings(_config_file="/nonexistent"))
    digest = __import__("hashlib").sha256("same digest payload".encode("utf-8")).hexdigest()
    with pytest.raises(ValueError):
        await service.load_tool_result(
            context=context,
            result_ref=f"memory://{chat.id}",
            expected_digest=digest,
            expected_execution_id="exec-1",
            expected_tool_call_id="call-1",
            expected_tool_name="demo_tool",
        )
    for ref in ("memory://", f"memory://{tool.id}?q=1", f"memory:///{tool.id}", "file://abc"):
        with pytest.raises(ValueError):
            await service.load_tool_result(
                context=context,
                result_ref=ref,
                expected_digest=digest,
            )


@pytest.mark.asyncio
async def test_generic_memory_history_and_query_exclude_tool_results(
    workflow_database: Database,
):
    from multiclaw.memory import MemoryEntry

    context = await _create_run_context(workflow_database, email_suffix="-memory-filter")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        await uow.memory.save(
            MemoryEntry(
                content="hello user",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
        await uow.memory.save(MemoryEntry(content="workspace note", type="note"))
        await uow.memory.save(
            MemoryEntry(
                content="tool secret payload",
                type="tool_result",
                role="tool",
                session_id=context.session_id,
                metadata={
                    "tool_call_id": "call-1",
                    "tool_name": "demo_tool",
                    "execution_id": "exec-1",
                    "result_status": "succeeded",
                },
            )
        )
        recent = await uow.memory.recent(limit=10)
        scoped = await uow.memory.context(max_chars=10_000, limit=10)
        queried = await uow.memory.query("tool secret payload", top_k=10)
        explicit = await uow.memory.recent(limit=10, entry_type="tool_result")

    assert all(entry.type != "tool_result" for entry in recent)
    assert all(entry.type != "tool_result" for entry in scoped)
    assert queried == []
    assert [entry.type for entry in explicit] == ["tool_result"]


@pytest.mark.asyncio
async def test_approval_binding_mismatch_blocks_mutated_external_read(
    workflow_database: Database,
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside1 = tmp_path / "outside1.txt"
    outside2 = tmp_path / "outside2.txt"
    outside1.write_text("outside1\n", encoding="utf-8")
    outside2.write_text("outside2\n", encoding="utf-8")
    context = await _create_run_context(workflow_database, email_suffix="-binding-mutate")
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-binding")
    result = await _scheduler(workflow_database).run(
        ReadFileToolBuilder(str(workspace)),
        {"file_path": str(outside1)},
        context=context,
        call_id="call-binding",
        run_lease_handle=RunLeaseHandle(lease),
    )
    await coordinator.decide_approval(
        context,
        result.data["approval_id"],
        approved=True,
        version=result.data["version"],
    )
    scheduler = _scheduler(workflow_database)
    mutated = scheduler._canonicalize_input({"file_path": str(outside2), "offset": 1, "limit": 2000})
    async with workflow_database.write_transaction() as conn:
        await conn.execute(
            update(tool_executions)
            .where(
                tool_executions.c.tenant_id == context.tenant_id,
                tool_executions.c.workspace_id == context.workspace_id,
                tool_executions.c.session_id == context.session_id,
                tool_executions.c.run_id == context.run_id,
            )
            .values(
                input_payload_json=mutated.payload_json,
                input_hash=mutated.payload_hash,
            )
        )

    router = _CompletionRouter([LLMResponse(content="must not run", tool_calls=[], reasoning_content="")])
    runtime = _real_runtime(database=workflow_database, context=context, router=router, builders=[ReadFileToolBuilder(str(workspace))])
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    row = await _latest_execution_row(workflow_database, context)
    assert row["execution_status"] == ExecutionStatus.BLOCKED_CORRUPT.value
    assert router.calls == []


@pytest.mark.asyncio
async def test_memory_tool_result_loader_rejects_foreign_session_lookup(
    workflow_database: Database,
):
    from multiclaw.memory import MemoryEntry

    tenant_id, workspace_id = await _seed_user(workflow_database, "foreign-tool-result@example.com")
    owner = await _create_run_context(
        workflow_database,
        email_suffix="-foreign-owner",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    other = await _create_run_context(
        workflow_database,
        email_suffix="-foreign-other",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    async with TenantUnitOfWork(workflow_database, owner) as uow:
        entry = await uow.memory.save(
            MemoryEntry(
                content="tool secret payload",
                type="tool_result",
                role="tool",
                session_id=owner.session_id,
                metadata={
                    "tool_call_id": "call-1",
                    "tool_name": "demo_tool",
                    "execution_id": "exec-1",
                    "result_status": "succeeded",
                },
            )
        )
    service = WorkflowContinuationService(workflow_database, settings=Settings(_config_file="/nonexistent"))
    digest = hashlib.sha256("tool secret payload".encode("utf-8")).hexdigest()
    with pytest.raises(ValueError):
        await service.load_tool_result(
            context=other,
            result_ref=f"memory://{entry.id}",
            expected_digest=digest,
            expected_execution_id="exec-1",
            expected_tool_call_id="call-1",
            expected_tool_name="demo_tool",
        )


@pytest.mark.asyncio
async def test_recovery_invalid_approved_input_blocks_from_not_started(
    workflow_database: Database,
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside text\n", encoding="utf-8")
    context = await _create_run_context(workflow_database, email_suffix="-invalid-approved-input")
    async with TenantUnitOfWork(workflow_database, context) as uow:
        from multiclaw.memory import MemoryEntry

        await uow.memory.save(
            MemoryEntry(
                content="read outside",
                type="chat_message",
                role="user",
                session_id=context.session_id,
                turn_index=1,
            )
        )
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(context, "runtime-invalid-approved-input")
    scheduler = _scheduler(workflow_database)
    invalid_payload = scheduler._canonicalize_input(
        {"file_path": str(outside), "offset": 0, "limit": 2000}
    )
    rebound_approval_id = scheduler._approval_binding_id(
        context=context,
        tool_call_id="call-invalid-approved-input",
        tool_name="read_file",
        tool_kind="native",
        input_hash=invalid_payload.payload_hash,
        approved_roots=[str(outside.resolve())],
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        idempotency_key=None,
    )
    approval = await coordinator.create_approval(
        lease,
        approval_id=rebound_approval_id,
        tool_call_id="call-invalid-approved-input",
        expires_at=workflow_database.dialect.db_now_ms() + 120_000,
    )
    await coordinator.decide_approval(
        context,
        approval.approval_id,
        approved=True,
        version=approval.version,
    )
    execution = await coordinator.create_execution(
        lease,
        execution_id=str(uuid4()),
        approval_id=approval.approval_id,
        tool_call_id="call-invalid-approved-input",
        tool_name="read_file",
        tool_kind="native",
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        idempotency_key=None,
        input_payload_json=invalid_payload.payload_json,
        input_hash=invalid_payload.payload_hash,
        status=ExecutionStatus.NOT_STARTED,
    )
    assert execution is not None
    lease = await coordinator.transition_run(lease, RunStatus.AWAITING_USER)
    await coordinator.checkpoint(
        lease,
        CheckpointPhase.AWAITING_APPROVAL,
        {
            "run_id": context.run_id,
            "approval_id": approval.approval_id,
            "tool_call_id": "call-invalid-approved-input",
            "approval_expires_at_ms": approval.expires_at,
            "resume_cursor": f"approval:{context.run_id}:call-invalid-approved-input",
            "cursor": f"approval:{context.run_id}:call-invalid-approved-input",
        },
        approval_id=approval.approval_id,
    )

    runtime = _real_runtime(
        database=workflow_database,
        context=context,
        router=_CompletionRouter([]),
        builders=[ReadFileToolBuilder(str(workspace))],
    )
    await _expire_run_lease(workflow_database, context)
    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    row = await _latest_execution_row(workflow_database, context)
    run = await coordinator.get_run(context)
    assert row["execution_status"] == ExecutionStatus.BLOCKED_CORRUPT.value
    assert run is not None
    assert run.status is not RunStatus.RESUMING
    assert runtime.active_run_count == 0
    assert runtime.active_executing_run_count == 0
    assert runtime.active_tool_execution_count == 0


@pytest.mark.asyncio
async def test_recovery_failure_after_prepare_uses_refreshed_executing_state(
    workflow_database: Database,
):
    context = await _create_run_context(workflow_database, email_suffix="-prepare-failure")
    await _seed_dispatch_state(
        workflow_database,
        context=context,
        tool_name="read_only_probe",
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        raw_params={"label": "replay-me", "delay": 0.0, "idempotency_key": None},
        tool_call_id="call-prepare-failure",
    )

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        return ToolExecutionResult(status=ToolStatus.SUCCESS, content=params.label)

    runtime = _TrackingRuntime(
        runtime_instance_id="runtime-prepare-failure",
        scheduler=_scheduler(workflow_database),
        builders=[
            PersistedToolBuilder(
                name="read_only_probe",
                runner=runner,
                recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
            )
        ],
    )

    original_prepare = runtime.scheduler._prepare_recovery_execution

    async def fail_after_prepare(*args, **kwargs):
        prepared = await original_prepare(*args, **kwargs)
        raise RuntimeError("fail after prepare")

    runtime.scheduler._prepare_recovery_execution = fail_after_prepare

    worker = recovery_module.WorkflowRecoveryWorker(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=_TrackingRuntimePool(runtime),
    )
    await worker.run_once()

    row = await _latest_execution_row(workflow_database, context)
    assert row["execution_status"] == ExecutionStatus.FAILED_TERMINAL.value
    assert runtime.closed_calls == 1
