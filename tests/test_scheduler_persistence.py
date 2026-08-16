from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from pydantic import BaseModel
from sqlalchemy import select, text

from multiclaw.cli import alembic_config
from multiclaw.config import DatabaseSettings, Settings
from multiclaw.events import EventBus
from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
from multiclaw.storage import Database
from multiclaw.storage.schema import approval_requests, execution_checkpoints, tool_executions
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.tools import CoreToolScheduler, ToolBuilder, ToolExecutionResult, ToolInvocation, ToolStatus
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import ExecutionStatus, RecoveryStrategy, RunLeaseHandle
from multiclaw.workflow import recovery as recovery_module

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
                tool_executions.c.execution_status,
                tool_executions.c.recovery_strategy,
                tool_executions.c.external_request_id,
                tool_executions.c.result_ref,
                tool_executions.c.result_digest,
                tool_executions.c.input_payload_json,
                tool_executions.c.input_hash,
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
async def test_approval_decision_survives_runtime_revoke_without_executing_tool(workflow_database: Database):
    import multiclaw.server as server

    scheduler = _scheduler(workflow_database)
    context = await _create_run_context(workflow_database, email_suffix="-approval-api")
    lease = await _coordinator(workflow_database).start_run_with_checkpoint(context, "runtime-approval")

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        raise AssertionError("approval API must not execute tools")

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
    assert getattr(response, "status_code", 200) == 200
    assert approval_count == 1
    assert execution_count == 0


@pytest.mark.asyncio
async def test_terminal_execution_recovery_metadata_survives_worker_noop(
    workflow_database: Database,
):
    call_count = 0

    async def runner(params: PersistedParams) -> ToolExecutionResult:
        nonlocal call_count
        call_count += 1
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=params.label,
            external_request_id="request-123",
            result_ref="workspace://results/tool.json",
            result_digest="a" * 64,
        )

    context = await _create_run_context(workflow_database, email_suffix="-manual-uncertain")
    lease = await _coordinator(workflow_database).start_run_with_checkpoint(context, "runtime-manual")
    scheduler = _scheduler(workflow_database)
    builder = PersistedToolBuilder(
        name="manual_uncertain_probe",
        runner=runner,
        recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
    )

    result = await scheduler.run(
        builder,
        {"label": "once"},
        context=context,
        call_id="call-uncertain",
        run_lease_handle=RunLeaseHandle(lease),
    )
    assert result.status == ToolStatus.SUCCESS

    phases = await _checkpoint_phases(workflow_database, context)
    assert "execution_dispatching" in phases
    row = await _latest_execution_row(workflow_database, context)
    assert row["external_request_id"] == "request-123"
    assert row["result_ref"] == "workspace://results/tool.json"
    assert row["result_digest"] == "a" * 64

    worker_cls = getattr(recovery_module, "WorkflowRecoveryWorker", None)
    assert worker_cls is not None
    worker = worker_cls(
        database=workflow_database,
        settings=Settings(_config_file="/nonexistent"),
        runtime_pool=SimpleNamespace(acquire=lambda context: None),
    )
    await worker.run_once()

    assert call_count == 1
    latest = await _latest_execution_row(workflow_database, context)
    assert latest["execution_status"] == ExecutionStatus.SUCCEEDED.value
