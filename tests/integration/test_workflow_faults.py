from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import insert, update

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.storage import Database
from multiclaw.storage.schema import agent_runs, approval_requests, tool_executions
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    ApprovalStatus,
    CheckpointPhase,
    ExecutionStatus,
    RecoveryStrategy,
    RunStatus,
)
from multiclaw.workflow.recovery import RecoveryAction, RecoveryService

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'workflow-faults.db'}"


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


async def _create_run_context(database: Database, suffix: str = "") -> TenantContext:
    tenant_id, workspace_id = await _seed_user(database, f"workflow-faults{suffix}@example.com")
    context = TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)
    async with TenantUnitOfWork(database, context) as uow:
        session = await uow.sessions.create(title="Workflow Faults")
    return context.for_run(session.id, str(uuid4()))


def _coordinator(database: Database, settings: Settings | None = None) -> WorkflowCoordinator:
    return WorkflowCoordinator(database, settings=settings or Settings(_config_file="/nonexistent"))


async def _expire_run_lease_with_db_clock(database: Database, context: TenantContext) -> None:
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


async def _seed_approval(
    database: Database,
    context: TenantContext,
    *,
    tool_call_id: str,
    status: ApprovalStatus = ApprovalStatus.AWAITING_USER,
) -> tuple[str, int]:
    approval_id = str(uuid4())
    resolved_at = None if status is ApprovalStatus.AWAITING_USER else database.dialect.db_now_ms()
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(approval_requests).values(
                approval_id=approval_id,
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                run_id=context.run_id,
                tool_call_id=tool_call_id,
                approval_status=status.value,
                requested_at=database.dialect.db_now_ms(),
                resolved_at=resolved_at,
                expires_at=database.dialect.db_now_ms() + 60_000,
                version=1,
            )
        )
    return approval_id, 1


async def _seed_execution(
    database: Database,
    context: TenantContext,
    *,
    tool_call_id: str,
    status: ExecutionStatus,
    recovery_strategy: RecoveryStrategy,
    idempotency_key: str | None = None,
    external_request_id: str | None = None,
    result_ref: str | None = None,
    result_digest: str | None = None,
) -> tuple[str, int]:
    execution_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(tool_executions).values(
                execution_id=execution_id,
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                run_id=context.run_id,
                approval_id=None,
                tool_call_id=tool_call_id,
                tool_name="echo",
                tool_kind="builtin",
                execution_status=status.value,
                recovery_strategy=recovery_strategy.value,
                idempotency_key=idempotency_key,
                input_payload_json='{"input":"value"}',
                input_hash="1" * 64,
                external_request_id=external_request_id,
                result_ref=result_ref,
                result_digest=result_digest,
                schema_version=1,
                version=1,
                created_at=database.dialect.db_now_ms(),
                updated_at=database.dialect.db_now_ms(),
                finished_at=None,
            )
        )
    return execution_id, 1


def _input_ref(execution_id: str) -> str:
    return f"tool_execution:{execution_id}:input_payload_json"


async def _build_before_approval_event(
    database: Database,
    coordinator: WorkflowCoordinator,
    lease,
    context: TenantContext,
):
    lease = await coordinator.transition_run(lease, RunStatus.AWAITING_USER)
    approval_id, _ = await _seed_approval(database, context, tool_call_id="tool-approval-awaiting")
    await coordinator.checkpoint(
        lease,
        CheckpointPhase.AWAITING_APPROVAL,
        {
            "run_id": context.run_id,
            "approval_id": approval_id,
            "tool_call_id": "tool-approval-awaiting",
            "approval_expires_at_ms": lease.lease_expires_at,
            "resume_cursor": "cursor-awaiting-approval",
            "cursor": "cursor-awaiting-approval",
        },
    )


async def _build_after_approval_cas(
    database: Database,
    coordinator: WorkflowCoordinator,
    lease,
    context: TenantContext,
):
    lease = await coordinator.transition_run(lease, RunStatus.AWAITING_USER)
    approval_id, version = await _seed_approval(database, context, tool_call_id="tool-approval-approved")
    await coordinator.checkpoint(
        lease,
        CheckpointPhase.AWAITING_APPROVAL,
        {
            "run_id": context.run_id,
            "approval_id": approval_id,
            "tool_call_id": "tool-approval-approved",
            "approval_expires_at_ms": lease.lease_expires_at,
            "resume_cursor": "cursor-approval-approved",
            "cursor": "cursor-approval-approved",
        },
    )
    await coordinator.decide_approval(context, approval_id, approved=True, version=version)


async def _build_before_tool_call(
    database: Database,
    coordinator: WorkflowCoordinator,
    lease,
    context: TenantContext,
):
    lease = await coordinator.transition_run(lease, RunStatus.AWAITING_USER)
    approval_id, version = await _seed_approval(database, context, tool_call_id="tool-before-call")
    await coordinator.checkpoint(
        lease,
        CheckpointPhase.AWAITING_APPROVAL,
        {
            "run_id": context.run_id,
            "approval_id": approval_id,
            "tool_call_id": "tool-before-call",
            "approval_expires_at_ms": lease.lease_expires_at,
            "resume_cursor": "cursor-before-tool-call",
            "cursor": "cursor-before-tool-call",
        },
    )
    await coordinator.decide_approval(context, approval_id, approved=True, version=version)
    await coordinator.transition_run(lease, RunStatus.RESUMING)


async def _build_read_only_dispatch(
    database: Database,
    coordinator: WorkflowCoordinator,
    lease,
    context: TenantContext,
):
    execution_id, version = await _seed_execution(
        database,
        context,
        tool_call_id="tool-read-only",
        status=ExecutionStatus.EXECUTING,
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
    )
    await coordinator.checkpoint(
        lease,
        CheckpointPhase.EXECUTION_DISPATCHING,
        {
            "run_id": context.run_id,
            "execution_id": execution_id,
            "tool_call_id": "tool-read-only",
            "recovery_strategy": RecoveryStrategy.READ_ONLY_REPLAY.value,
            "input_hash": "1" * 64,
            "input_ref": _input_ref(execution_id),
            "dispatch_cursor": "cursor-read-only",
            "cursor": "cursor-read-only",
        },
        execution_id=execution_id,
        execution_expected_status=ExecutionStatus.EXECUTING,
        execution_expected_version=version,
    )


async def _build_idempotent_dispatch(
    database: Database,
    coordinator: WorkflowCoordinator,
    lease,
    context: TenantContext,
):
    execution_id, version = await _seed_execution(
        database,
        context,
        tool_call_id="tool-idempotent",
        status=ExecutionStatus.EXECUTING,
        recovery_strategy=RecoveryStrategy.IDEMPOTENT_RETRY,
        idempotency_key="idem-1",
    )
    await coordinator.checkpoint(
        lease,
        CheckpointPhase.EXECUTION_DISPATCHING,
        {
            "run_id": context.run_id,
            "execution_id": execution_id,
            "tool_call_id": "tool-idempotent",
            "recovery_strategy": RecoveryStrategy.IDEMPOTENT_RETRY.value,
            "input_hash": "1" * 64,
            "input_ref": _input_ref(execution_id),
            "idempotency_key": "idem-1",
            "dispatch_cursor": "cursor-idempotent",
            "cursor": "cursor-idempotent",
        },
        execution_id=execution_id,
        execution_expected_status=ExecutionStatus.EXECUTING,
        execution_expected_version=version,
    )


async def _build_manual_dispatch(
    database: Database,
    coordinator: WorkflowCoordinator,
    lease,
    context: TenantContext,
):
    execution_id, version = await _seed_execution(
        database,
        context,
        tool_call_id="tool-manual",
        status=ExecutionStatus.EXECUTING,
        recovery_strategy=RecoveryStrategy.MANUAL_UNCERTAIN,
        external_request_id="request-1",
    )
    await coordinator.checkpoint(
        lease,
        CheckpointPhase.EXECUTION_DISPATCHING,
        {
            "run_id": context.run_id,
            "execution_id": execution_id,
            "tool_call_id": "tool-manual",
            "recovery_strategy": RecoveryStrategy.MANUAL_UNCERTAIN.value,
            "input_hash": "1" * 64,
            "input_ref": _input_ref(execution_id),
            "dispatch_cursor": "cursor-manual",
            "cursor": "cursor-manual",
        },
        execution_id=execution_id,
        execution_expected_status=ExecutionStatus.EXECUTING,
        execution_expected_version=version,
    )


async def _build_result_observed(
    database: Database,
    coordinator: WorkflowCoordinator,
    lease,
    context: TenantContext,
):
    execution_id, version = await _seed_execution(
        database,
        context,
        tool_call_id="tool-result",
        status=ExecutionStatus.SUCCEEDED,
        recovery_strategy=RecoveryStrategy.IDEMPOTENT_RETRY,
        idempotency_key="idem-result",
        external_request_id="remote-result-1",
        result_ref="workspace://results/result.json",
        result_digest="3" * 64,
    )
    await coordinator.checkpoint(
        lease,
        CheckpointPhase.EXECUTION_RESULT_OBSERVED,
        {
            "run_id": context.run_id,
            "execution_id": execution_id,
            "result_status": ExecutionStatus.SUCCEEDED.value,
            "result_digest": "3" * 64,
            "result_ref": "workspace://results/result.json",
            "external_request_id": "remote-result-1",
            "resume_cursor": "cursor-result-observed",
            "cursor": "cursor-result-observed",
        },
        execution_id=execution_id,
        execution_expected_status=ExecutionStatus.SUCCEEDED,
        execution_expected_version=version,
    )


async def _build_terminal_checkpoint(
    database: Database,
    coordinator: WorkflowCoordinator,
    lease,
    context: TenantContext,
):
    finished = await coordinator.finish_run(lease, RunStatus.CANCELLED)
    await coordinator.checkpoint(
        finished,
        CheckpointPhase.RUN_TERMINAL,
        {
            "run_id": context.run_id,
            "terminal_status": RunStatus.CANCELLED.value,
            "finished_at_ms": finished.lease_expires_at,
            "final_digest": "4" * 64,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "builder", "expected_action"),
    [
        (
            "after_run_creation",
            lambda database, coordinator, lease, context: coordinator.checkpoint(
                lease,
                CheckpointPhase.RUN_STARTED,
                {
                    "tenant_id": context.tenant_id,
                    "workspace_id": context.workspace_id,
                    "session_id": context.session_id,
                    "run_id": context.run_id,
                    "started_at_ms": lease.lease_expires_at - 1,
                    "model_cursor": "cursor-run-started",
                    "cursor": "cursor-run-started",
                },
            ),
            RecoveryAction.RESUME_MODEL,
        ),
        (
            "after_model_output_commit",
            lambda _database, coordinator, lease, context: coordinator.checkpoint(
                lease,
                CheckpointPhase.MODEL_OUTPUT_COMMITTED,
                {
                    "run_id": context.run_id,
                    "message_id": "msg-1",
                    "output_digest": "2" * 64,
                    "model_cursor": "cursor-model-output",
                    "cursor": "cursor-model-output",
                },
            ),
            RecoveryAction.RESUME_MODEL,
        ),
        (
            "before_approval_event",
            _build_before_approval_event,
            RecoveryAction.AWAIT_USER,
        ),
        (
            "after_approval_cas",
            _build_after_approval_cas,
            RecoveryAction.RESUME_MODEL,
        ),
        (
            "before_tool_call",
            _build_before_tool_call,
            RecoveryAction.RESUME_MODEL,
        ),
        (
            "after_remote_side_effect_before_result_commit_read_only",
            _build_read_only_dispatch,
            RecoveryAction.REPLAY_READ_ONLY,
        ),
        (
            "after_remote_side_effect_before_result_commit_idempotent",
            _build_idempotent_dispatch,
            RecoveryAction.RETRY_IDEMPOTENT,
        ),
        (
            "after_remote_side_effect_before_result_commit_manual",
            _build_manual_dispatch,
            RecoveryAction.MARK_MANUAL_UNCERTAIN,
        ),
        (
            "after_result_commit_before_terminal_sse",
            _build_result_observed,
            RecoveryAction.RESUME_MODEL,
        ),
        (
            "after_db_purge_commit_before_worker_ack",
            _build_terminal_checkpoint,
            RecoveryAction.TERMINAL_NOOP,
        ),
    ],
)
async def test_recovery_classifies_fault_windows_deterministically(
    workflow_database: Database,
    scenario: str,
    builder,
    expected_action: RecoveryAction,
):
    del scenario
    run_context = await _create_run_context(workflow_database, suffix=f"-{uuid4().hex[:8]}")
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run(run_context, "runtime-1")

    await builder(workflow_database, coordinator, lease, run_context)
    await _expire_run_lease_with_db_clock(workflow_database, run_context)

    outcome = await RecoveryService(workflow_database).recover(run_context, "runtime-2")

    assert outcome.action == expected_action
    assert outcome.executions_started == 0
