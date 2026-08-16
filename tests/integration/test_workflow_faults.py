from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import and_, func, insert, select, update

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.storage import Database
from multiclaw.storage.schema import agent_runs, approval_requests, execution_checkpoints, tool_executions
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    ApprovalStatus,
    CheckpointPhase,
    ExecutionStatus,
    InvalidTransitionError,
    RecoveryStrategy,
    RunStatus,
    StaleFenceError,
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


async def _measure_workflow_fault_release_gate(database: Database) -> dict[str, int]:
    terminal_execution_statuses = {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED_TERMINAL,
        ExecutionStatus.UNCERTAIN,
        ExecutionStatus.BLOCKED_INCOMPATIBLE,
        ExecutionStatus.BLOCKED_CORRUPT,
    }

    async def _execution_row_count(context: TenantContext) -> int:
        async with database.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(tool_executions)
                .where(
                    tool_executions.c.tenant_id == context.tenant_id,
                    tool_executions.c.workspace_id == context.workspace_id,
                    tool_executions.c.session_id == context.session_id,
                    tool_executions.c.run_id == context.run_id,
                )
            )
        return int(result.scalar_one())

    async def _completed_active_join_count(context: TenantContext) -> int:
        nonterminal_statuses = [
            status.value
            for status in ExecutionStatus
            if status not in terminal_execution_statuses
        ]
        async with database.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(
                    agent_runs.join(
                        tool_executions,
                        and_(
                            tool_executions.c.tenant_id == agent_runs.c.tenant_id,
                            tool_executions.c.workspace_id == agent_runs.c.workspace_id,
                            tool_executions.c.session_id == agent_runs.c.session_id,
                            tool_executions.c.run_id == agent_runs.c.run_id,
                        ),
                    )
                )
                .where(
                    agent_runs.c.tenant_id == context.tenant_id,
                    agent_runs.c.workspace_id == context.workspace_id,
                    agent_runs.c.session_id == context.session_id,
                    agent_runs.c.run_id == context.run_id,
                    agent_runs.c.run_status == RunStatus.COMPLETED.value,
                    tool_executions.c.execution_status.in_(nonterminal_statuses),
                )
            )
        return int(result.scalar_one())

    metrics: dict[str, int] = {}

    stale_context = await _create_run_context(database, suffix=f"-gate-stale-{uuid4().hex[:8]}")
    stale_coordinator = _coordinator(database)
    stale_lease = await stale_coordinator.start_run_with_checkpoint(stale_context, "runtime-stale-old")

    await _expire_run_lease_with_db_clock(database, stale_context)
    stale_outcome = await RecoveryService(database).recover(stale_context, "runtime-stale-new")

    assert stale_outcome.action == RecoveryAction.RESUME_MODEL
    assert stale_outcome.lease is not None
    assert stale_outcome.lease.lease_owner == "runtime-stale-new"

    stale_fence_writes = 0
    try:
        await stale_coordinator.transition_run(stale_lease, RunStatus.AWAITING_USER)
    except (InvalidTransitionError, StaleFenceError):
        pass
    else:
        stale_fence_writes += 1

    stale_execution = await stale_coordinator.create_execution(
        stale_lease,
        execution_id=str(uuid4()),
        approval_id=None,
        tool_call_id=f"tool-stale-{uuid4().hex[:8]}",
        tool_name="echo",
        tool_kind="builtin",
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        idempotency_key=None,
        input_payload_json='{"input":"value"}',
        input_hash="5" * 64,
        status=ExecutionStatus.NOT_STARTED,
    )
    if stale_execution is not None:
        stale_fence_writes += 1

    try:
        await stale_coordinator.checkpoint(
            stale_lease,
            CheckpointPhase.MODEL_OUTPUT_COMMITTED,
            {
                "run_id": stale_context.run_id,
                "message_id": "msg-stale-fence",
                "output_digest": "6" * 64,
                "model_cursor": "cursor-stale-fence",
                "cursor": "cursor-stale-fence",
            },
        )
    except StaleFenceError:
        pass
    else:
        stale_fence_writes += 1

    metrics["stale_fence_writes"] = stale_fence_writes

    manual_context = await _create_run_context(database, suffix=f"-gate-manual-{uuid4().hex[:8]}")
    manual_coordinator = _coordinator(database)
    manual_lease = await manual_coordinator.start_run(manual_context, "runtime-manual-old")
    await _build_manual_dispatch(database, manual_coordinator, manual_lease, manual_context)
    manual_checkpoint = await manual_coordinator.get_latest_checkpoint(manual_context)
    assert manual_checkpoint is not None
    assert manual_checkpoint.execution_id is not None
    manual_execution_before = await manual_coordinator.get_execution_recovery(
        manual_context, manual_checkpoint.execution_id
    )
    assert manual_execution_before is not None

    await _expire_run_lease_with_db_clock(database, manual_context)
    manual_outcome = await RecoveryService(database).recover(manual_context, "runtime-manual-new")
    manual_execution_after = await manual_coordinator.get_execution_recovery(
        manual_context, manual_checkpoint.execution_id
    )
    assert manual_execution_after is not None

    assert manual_outcome.action == RecoveryAction.MARK_MANUAL_UNCERTAIN
    assert manual_outcome.executions_started == 0
    assert manual_execution_after.status is ExecutionStatus.EXECUTING
    metrics["automatic_non_idempotent_retries"] = (
        manual_outcome.executions_started
        + max(0, await _execution_row_count(manual_context) - 1)
        + int(manual_execution_after.version != manual_execution_before.version)
    )

    corrupt_context = await _create_run_context(database, suffix=f"-gate-corrupt-{uuid4().hex[:8]}")
    corrupt_coordinator = _coordinator(database)
    corrupt_lease = await corrupt_coordinator.start_run(corrupt_context, "runtime-corrupt-old")
    await _build_read_only_dispatch(database, corrupt_coordinator, corrupt_lease, corrupt_context)
    corrupt_checkpoint = await corrupt_coordinator.get_latest_checkpoint(corrupt_context)
    assert corrupt_checkpoint is not None
    assert corrupt_checkpoint.execution_id is not None
    corrupt_execution_before = await corrupt_coordinator.get_execution_recovery(
        corrupt_context, corrupt_checkpoint.execution_id
    )
    assert corrupt_execution_before is not None

    async with database.write_transaction() as conn:
        await conn.execute(
            update(execution_checkpoints)
            .where(
                execution_checkpoints.c.tenant_id == corrupt_context.tenant_id,
                execution_checkpoints.c.workspace_id == corrupt_context.workspace_id,
                execution_checkpoints.c.session_id == corrupt_context.session_id,
                execution_checkpoints.c.run_id == corrupt_context.run_id,
                execution_checkpoints.c.checkpoint_id == corrupt_checkpoint.checkpoint_id,
            )
            .values(payload_hash="f" * 64)
        )

    await _expire_run_lease_with_db_clock(database, corrupt_context)
    corrupt_outcome = await RecoveryService(database).recover(corrupt_context, "runtime-corrupt-new")
    corrupt_execution_after = await corrupt_coordinator.get_execution_recovery(
        corrupt_context, corrupt_checkpoint.execution_id
    )
    assert corrupt_execution_after is not None

    assert corrupt_outcome.status is RunStatus.BLOCKED_CORRUPT
    assert corrupt_outcome.lease is None
    assert "hash mismatch" in corrupt_outcome.reason
    metrics["corrupt_checkpoint_tool_starts"] = (
        corrupt_outcome.executions_started
        + max(0, await _execution_row_count(corrupt_context) - 1)
        + int(corrupt_execution_after.version != corrupt_execution_before.version)
    )

    completed_context = await _create_run_context(database, suffix=f"-gate-complete-{uuid4().hex[:8]}")
    completed_coordinator = _coordinator(database)
    completed_lease = await completed_coordinator.start_run(completed_context, "runtime-complete")
    created_execution = await completed_coordinator.create_execution(
        completed_lease,
        execution_id=str(uuid4()),
        approval_id=None,
        tool_call_id=f"tool-complete-{uuid4().hex[:8]}",
        tool_name="echo",
        tool_kind="builtin",
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        idempotency_key=None,
        input_payload_json='{"input":"value"}',
        input_hash="7" * 64,
        status=ExecutionStatus.EXECUTING,
    )
    assert created_execution is not None

    completed_successes = 0
    try:
        await completed_coordinator.finish_run(completed_lease, RunStatus.COMPLETED)
    except InvalidTransitionError as error:
        assert "nonterminal" in str(error)
    else:
        completed_successes += 1

    metrics["completed_runs_with_active_execution"] = (
        completed_successes + await _completed_active_join_count(completed_context)
    )

    return metrics


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


@pytest.mark.asyncio
async def test_workflow_fault_release_gate_reports_zero_success_regressions(
    workflow_database: Database,
):
    metrics = await _measure_workflow_fault_release_gate(workflow_database)

    assert metrics == {
        "stale_fence_writes": 0,
        "automatic_non_idempotent_retries": 0,
        "corrupt_checkpoint_tool_starts": 0,
        "completed_runs_with_active_execution": 0,
    }
