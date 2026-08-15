from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from fastapi import HTTPException
from sqlalchemy import insert, select, text, update
from starlette.requests import Request

from multiclaw.api.dependencies import tenant_context
from multiclaw.auth.models import UserRecord
from multiclaw.cli import alembic_config
from multiclaw.config import DatabaseSettings, Settings
from multiclaw.storage import Database
from multiclaw.storage.schema import (
    agent_runs,
    approval_requests,
    execution_checkpoints,
    tool_executions,
)
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    ApprovalRecord,
    ApprovalStatus,
    CheckpointPhase,
    ExecutionStatus,
    InvalidTransitionError,
    LeaseConflictError,
    RunLease,
    RunStatus,
    StaleFenceError,
    TenantRunQuotaError,
    VersionConflictError,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'workflow-state.db'}"


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
    tenant_id, workspace_id = await _seed_user(database, f"workflow{suffix}@example.com")
    context = TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)
    async with TenantUnitOfWork(database, context) as uow:
        session = await uow.sessions.create(title="Workflow Session")
    return context.for_run(session.id, str(uuid4()))


def _coordinator(database: Database, settings: Settings | None = None) -> WorkflowCoordinator:
    return WorkflowCoordinator(database, settings=settings or Settings(_config_file="/nonexistent"))


async def _expire_lease_with_db_clock(database: Database, context: TenantContext) -> None:
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


async def _seed_execution(
    database: Database,
    lease: RunLease,
    *,
    status: ExecutionStatus | str,
    approval_id: str | None = None,
) -> str:
    execution_id = str(uuid4())
    execution_status = status.value if isinstance(status, ExecutionStatus) else status
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(tool_executions).values(
                execution_id=execution_id,
                tenant_id=lease.context.tenant_id,
                workspace_id=lease.context.workspace_id,
                session_id=lease.context.session_id,
                run_id=lease.context.run_id,
                approval_id=approval_id,
                tool_call_id=f"call-{execution_id}",
                tool_name="echo",
                tool_kind="builtin",
                execution_status=execution_status,
                recovery_strategy="idempotent_retry",
                idempotency_key=None,
                input_payload_json="{}",
                input_hash="0" * 64,
                external_request_id=None,
                result_ref=None,
                result_digest=None,
                schema_version=1,
                version=1,
                created_at=database.dialect.db_now_ms(),
                updated_at=database.dialect.db_now_ms(),
                finished_at=None,
            )
        )
    return execution_id


async def _fetch_execution_state(
    database: Database,
    lease: RunLease,
    execution_id: str,
) -> tuple[str, int]:
    async with database.connect() as conn:
        result = await conn.execute(
            select(
                tool_executions.c.execution_status,
                tool_executions.c.version,
            ).where(
                tool_executions.c.tenant_id == lease.context.tenant_id,
                tool_executions.c.workspace_id == lease.context.workspace_id,
                tool_executions.c.session_id == lease.context.session_id,
                tool_executions.c.run_id == lease.context.run_id,
                tool_executions.c.execution_id == execution_id,
            )
        )
        row = result.mappings().one()
    return str(row["execution_status"]), int(row["version"])


async def _checkpoint_rows(database: Database, context: TenantContext) -> list[dict[str, object]]:
    async with database.connect() as conn:
        result = await conn.execute(
            select(
                execution_checkpoints.c.phase,
                execution_checkpoints.c.checkpoint_seq,
                execution_checkpoints.c.payload_json,
            )
            .where(
                execution_checkpoints.c.tenant_id == context.tenant_id,
                execution_checkpoints.c.workspace_id == context.workspace_id,
                execution_checkpoints.c.session_id == context.session_id,
                execution_checkpoints.c.run_id == context.run_id,
            )
            .order_by(execution_checkpoints.c.checkpoint_seq.asc())
        )
        rows = result.mappings().all()
    return [
        {
            "phase": str(row["phase"]),
            "checkpoint_seq": int(row["checkpoint_seq"]),
            "payload": json.loads(str(row["payload_json"])),
        }
        for row in rows
    ]


@dataclass(slots=True, frozen=True)
class SeededApproval:
    context: TenantContext
    approval_id: str
    version: int


async def _seed_approval(
    database: Database,
    *,
    expires_offset_ms: int = 60_000,
) -> SeededApproval:
    context = await _create_run_context(database, suffix="-approval")
    lease = await _coordinator(database).start_run(context, "runtime-approval")
    approval_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(approval_requests).values(
                approval_id=approval_id,
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                run_id=context.run_id,
                tool_call_id=f"call-{approval_id}",
                approval_status=ApprovalStatus.AWAITING_USER.value,
                requested_at=database.dialect.db_now_ms(),
                resolved_at=None,
                expires_at=database.dialect.db_now_ms() + expires_offset_ms,
                version=1,
            )
        )
    await _coordinator(database).heartbeat(lease)
    return SeededApproval(context=context, approval_id=approval_id, version=1)


async def _decide(
    database: Database,
    approval: SeededApproval,
    *,
    approved: bool,
    version: int,
) -> ApprovalRecord:
    return await _coordinator(database).decide_approval(
        approval.context,
        approval.approval_id,
        approved=approved,
        version=version,
    )


@pytest.mark.asyncio
async def test_stale_runtime_cannot_write_after_lease_takeover(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-stale")

    first = await _coordinator(workflow_database).start_run(run_context, "runtime-1")
    await _expire_lease_with_db_clock(workflow_database, run_context)

    second = await _coordinator(workflow_database).acquire_run(run_context, "runtime-2")

    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(StaleFenceError):
        await _coordinator(workflow_database).heartbeat(first)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    (
        RunStatus.COMPLETED,
        RunStatus.FAILED_TERMINAL,
        RunStatus.CANCELLED,
        RunStatus.BLOCKED_CORRUPT,
        RunStatus.BLOCKED_INCOMPATIBLE,
    ),
)
async def test_terminal_run_cannot_be_reacquired_after_lease_expiry(
    workflow_database: Database,
    terminal_status: RunStatus,
):
    run_context = await _create_run_context(
        workflow_database,
        suffix=f"-terminal-takeover-{terminal_status.value}",
    )

    lease = await _coordinator(workflow_database).start_run(run_context, "runtime-1")
    finished = await _coordinator(workflow_database).finish_run(lease, terminal_status)
    before = await _coordinator(workflow_database).get_run(run_context)
    assert before is not None

    await _expire_lease_with_db_clock(workflow_database, run_context)

    with pytest.raises(InvalidTransitionError):
        await _coordinator(workflow_database).acquire_run(run_context, "runtime-2")

    after = await _coordinator(workflow_database).get_run(run_context)
    assert after is not None
    assert after.lease_owner == before.lease_owner
    assert after.fencing_token == before.fencing_token
    assert after.version == before.version
    assert after.status is terminal_status


@pytest.mark.asyncio
async def test_heartbeat_requires_current_fence_owner(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-heartbeat")

    lease = await _coordinator(workflow_database).start_run(run_context, "runtime-1")

    with pytest.raises(StaleFenceError):
        await _coordinator(workflow_database).heartbeat(
            RunLease(
                context=lease.context,
                lease_owner="runtime-2",
                fencing_token=lease.fencing_token,
                version=lease.version,
                lease_expires_at=lease.lease_expires_at,
            )
        )


@pytest.mark.asyncio
async def test_run_cannot_complete_with_nonterminal_execution(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-terminal")

    lease = await _coordinator(workflow_database).start_run(run_context, "runtime")
    await _seed_execution(workflow_database, lease, status=ExecutionStatus.EXECUTING)

    with pytest.raises(InvalidTransitionError):
        await _coordinator(workflow_database).finish_run(lease, RunStatus.COMPLETED)


@pytest.mark.asyncio
async def test_terminal_run_is_immutable(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-immutable")

    lease = await _coordinator(workflow_database).start_run(run_context, "runtime")
    finished = await _coordinator(workflow_database).finish_run(lease, RunStatus.COMPLETED)

    with pytest.raises(InvalidTransitionError):
        await _coordinator(workflow_database).finish_run(finished, RunStatus.CANCELLED)


@pytest.mark.asyncio
async def test_version_cas_allows_exactly_one_concurrent_approval_decision(workflow_database: Database):
    approval = await _seed_approval(workflow_database)

    results = await asyncio.gather(
        _decide(workflow_database, approval, approved=True, version=approval.version),
        _decide(workflow_database, approval, approved=False, version=approval.version),
        return_exceptions=True,
    )

    assert sum(isinstance(result, ApprovalRecord) for result in results) == 1
    assert sum(isinstance(result, VersionConflictError) for result in results) == 1


@pytest.mark.asyncio
async def test_expired_approval_cannot_be_decided(workflow_database: Database):
    approval = await _seed_approval(workflow_database, expires_offset_ms=-1)

    with pytest.raises(InvalidTransitionError):
        await _decide(workflow_database, approval, approved=True, version=approval.version)


@pytest.mark.asyncio
async def test_execution_without_approval_fk_can_start(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-optional-approval")
    lease = await _coordinator(workflow_database).start_run(run_context, "runtime")

    execution_id = await _seed_execution(
        workflow_database,
        lease,
        status=ExecutionStatus.NOT_STARTED,
        approval_id=None,
    )

    async with workflow_database.connect() as conn:
        persisted = await conn.execute(
            select(tool_executions.c.approval_id).where(
                tool_executions.c.tenant_id == run_context.tenant_id,
                tool_executions.c.workspace_id == run_context.workspace_id,
                tool_executions.c.session_id == run_context.session_id,
                tool_executions.c.run_id == run_context.run_id,
                tool_executions.c.execution_id == execution_id,
            )
        )

    assert persisted.scalar_one() is None


@pytest.mark.asyncio
async def test_execution_transition_rejects_illegal_source_target_pair(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-exec-illegal")
    lease = await _coordinator(workflow_database).start_run(run_context, "runtime")
    execution_id = await _seed_execution(workflow_database, lease, status=ExecutionStatus.EXECUTING)

    with pytest.raises(InvalidTransitionError):
        await _coordinator(workflow_database).transition_execution(
            lease,
            execution_id,
            expected_status=ExecutionStatus.EXECUTING,
            expected_version=1,
            target=ExecutionStatus.REPLAYING,
        )


@pytest.mark.asyncio
async def test_execution_transition_updates_under_current_lease(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-exec-green")
    lease = await _coordinator(workflow_database).start_run(run_context, "runtime")
    execution_id = await _seed_execution(workflow_database, lease, status=ExecutionStatus.NOT_STARTED)

    lease = await _coordinator(workflow_database).transition_execution(
        lease,
        execution_id,
        expected_status=ExecutionStatus.NOT_STARTED,
        expected_version=1,
        target=ExecutionStatus.REPLAYING,
    )
    assert await _fetch_execution_state(workflow_database, lease, execution_id) == (
        ExecutionStatus.REPLAYING.value,
        2,
    )

    lease = await _coordinator(workflow_database).transition_execution(
        lease,
        execution_id,
        expected_status=ExecutionStatus.REPLAYING,
        expected_version=2,
        target=ExecutionStatus.EXECUTING,
    )
    assert await _fetch_execution_state(workflow_database, lease, execution_id) == (
        ExecutionStatus.EXECUTING.value,
        3,
    )


@pytest.mark.asyncio
async def test_execution_transition_detects_version_conflict(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-exec-version")
    lease = await _coordinator(workflow_database).start_run(run_context, "runtime")
    execution_id = await _seed_execution(workflow_database, lease, status=ExecutionStatus.NOT_STARTED)

    with pytest.raises(VersionConflictError):
        await _coordinator(workflow_database).transition_execution(
            lease,
            execution_id,
            expected_status=ExecutionStatus.NOT_STARTED,
            expected_version=2,
            target=ExecutionStatus.REPLAYING,
        )


@pytest.mark.asyncio
async def test_stale_lease_blocks_execution_transition_after_takeover(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-exec-stale")
    first = await _coordinator(workflow_database).start_run(run_context, "runtime-1")
    execution_id = await _seed_execution(workflow_database, first, status=ExecutionStatus.NOT_STARTED)
    await _expire_lease_with_db_clock(workflow_database, run_context)
    current = await _coordinator(workflow_database).acquire_run(run_context, "runtime-2")

    with pytest.raises(StaleFenceError):
        await _coordinator(workflow_database).transition_execution(
            first,
            execution_id,
            expected_status=ExecutionStatus.NOT_STARTED,
            expected_version=1,
            target=ExecutionStatus.REPLAYING,
        )

    current = await _coordinator(workflow_database).transition_execution(
        current,
        execution_id,
        expected_status=ExecutionStatus.NOT_STARTED,
        expected_version=1,
        target=ExecutionStatus.REPLAYING,
    )
    assert current.version >= 2


@pytest.mark.asyncio
async def test_checkpoint_insert_requires_current_lease(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-checkpoint")
    first = await _coordinator(workflow_database).start_run(run_context, "runtime-1")
    await _expire_lease_with_db_clock(workflow_database, run_context)
    current = await _coordinator(workflow_database).acquire_run(run_context, "runtime-2")

    with pytest.raises(StaleFenceError):
        await _coordinator(workflow_database).write_checkpoint(
            first,
            checkpoint_id=str(uuid4()),
            checkpoint_seq=1,
            phase="run",
            payload_json="{}",
            payload_hash="1" * 64,
            schema_version=1,
        )

    await _coordinator(workflow_database).write_checkpoint(
        current,
        checkpoint_id=str(uuid4()),
        checkpoint_seq=1,
        phase="run",
        payload_json="{}",
        payload_hash="2" * 64,
        schema_version=1,
    )

    async with workflow_database.connect() as conn:
        count = await conn.scalar(
            select(text("COUNT(*)")).select_from(execution_checkpoints).where(
                execution_checkpoints.c.tenant_id == run_context.tenant_id,
                execution_checkpoints.c.workspace_id == run_context.workspace_id,
                execution_checkpoints.c.session_id == run_context.session_id,
                execution_checkpoints.c.run_id == run_context.run_id,
            )
        )
    assert count == 1


@pytest.mark.asyncio
async def test_start_run_with_checkpoint_rolls_back_when_checkpoint_insert_fails(
    workflow_database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    run_context = await _create_run_context(workflow_database, suffix="-start-rollback")
    coordinator = _coordinator(workflow_database)

    from multiclaw.storage.repositories.workflow import WorkflowRepository

    async def fail_insert(self, lease, **kwargs):
        del lease, kwargs
        raise RuntimeError("checkpoint insert failed")

    monkeypatch.setattr(WorkflowRepository, "_insert_checkpoint", fail_insert)

    with pytest.raises(RuntimeError, match="checkpoint insert failed"):
        await coordinator.start_run_with_checkpoint(run_context, "runtime")

    assert await coordinator.get_run(run_context) is None
    assert await _checkpoint_rows(workflow_database, run_context) == []


@pytest.mark.asyncio
async def test_finish_run_with_checkpoint_rolls_back_terminal_transition_when_checkpoint_insert_fails(
    workflow_database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    run_context = await _create_run_context(workflow_database, suffix="-finish-rollback")
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run_with_checkpoint(run_context, "runtime")

    from multiclaw.storage.repositories.workflow import WorkflowRepository

    original_insert = WorkflowRepository._insert_checkpoint

    async def fail_terminal_insert(self, lease, **kwargs):
        if kwargs.get("phase") == CheckpointPhase.RUN_TERMINAL.value:
            raise RuntimeError("terminal checkpoint insert failed")
        return await original_insert(self, lease, **kwargs)

    monkeypatch.setattr(WorkflowRepository, "_insert_checkpoint", fail_terminal_insert)

    with pytest.raises(RuntimeError, match="terminal checkpoint insert failed"):
        await coordinator.finish_run_with_checkpoint(lease, RunStatus.COMPLETED)

    record = await coordinator.get_run(run_context)
    assert record is not None
    assert record.status is RunStatus.RUNNING
    checkpoints = await _checkpoint_rows(workflow_database, run_context)
    assert [row["phase"] for row in checkpoints] == [CheckpointPhase.RUN_STARTED.value]


@pytest.mark.asyncio
async def test_execution_dispatching_checkpoint_rejects_invalid_phase_status(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-dispatch-invalid-status")
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run(run_context, "runtime")
    execution_id = await _seed_execution(workflow_database, lease, status=ExecutionStatus.NOT_STARTED)

    with pytest.raises(InvalidTransitionError):
        await coordinator.checkpoint(
            lease,
            CheckpointPhase.EXECUTION_DISPATCHING,
            {
                "run_id": run_context.run_id,
                "execution_id": execution_id,
                "tool_call_id": f"call-{execution_id}",
                "recovery_strategy": "idempotent_retry",
                "input_hash": "1" * 64,
                "input_ref": f"tool_execution:{execution_id}:input_payload_json",
                "idempotency_key": "idem-1",
                "dispatch_cursor": "cursor-dispatch-invalid",
                "cursor": "cursor-dispatch-invalid",
            },
            execution_id=execution_id,
            execution_expected_status=ExecutionStatus.NOT_STARTED,
            execution_expected_version=1,
        )


@pytest.mark.asyncio
async def test_execution_dispatching_checkpoint_accepts_allowed_status_and_detects_version_conflict(
    workflow_database: Database,
):
    run_context = await _create_run_context(workflow_database, suffix="-dispatch-valid")
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run(run_context, "runtime")
    execution_id = await _seed_execution(workflow_database, lease, status=ExecutionStatus.EXECUTING)

    await coordinator.checkpoint(
        lease,
        CheckpointPhase.EXECUTION_DISPATCHING,
        {
            "run_id": run_context.run_id,
            "execution_id": execution_id,
            "tool_call_id": f"call-{execution_id}",
            "recovery_strategy": "idempotent_retry",
            "input_hash": "1" * 64,
            "input_ref": f"tool_execution:{execution_id}:input_payload_json",
            "idempotency_key": "idem-1",
            "dispatch_cursor": "cursor-dispatch-valid",
            "cursor": "cursor-dispatch-valid",
        },
        execution_id=execution_id,
        execution_expected_status=ExecutionStatus.EXECUTING,
        execution_expected_version=1,
    )

    with pytest.raises(VersionConflictError):
        await coordinator.checkpoint(
            lease,
            CheckpointPhase.EXECUTION_DISPATCHING,
            {
                "run_id": run_context.run_id,
                "execution_id": execution_id,
                "tool_call_id": f"call-{execution_id}",
                "recovery_strategy": "idempotent_retry",
                "input_hash": "1" * 64,
                "input_ref": f"tool_execution:{execution_id}:input_payload_json",
                "idempotency_key": "idem-1",
                "dispatch_cursor": "cursor-dispatch-version",
                "cursor": "cursor-dispatch-version",
            },
            execution_id=execution_id,
            execution_expected_status=ExecutionStatus.EXECUTING,
            execution_expected_version=2,
        )


@pytest.mark.asyncio
async def test_execution_result_observed_checkpoint_rejects_invalid_phase_status(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-result-invalid-status")
    coordinator = _coordinator(workflow_database)
    lease = await coordinator.start_run(run_context, "runtime")
    execution_id = await _seed_execution(workflow_database, lease, status=ExecutionStatus.EXECUTING)

    with pytest.raises(InvalidTransitionError):
        await coordinator.checkpoint(
            lease,
            CheckpointPhase.EXECUTION_RESULT_OBSERVED,
            {
                "run_id": run_context.run_id,
                "execution_id": execution_id,
                "result_status": ExecutionStatus.EXECUTING.value,
                "result_digest": "2" * 64,
                "result_ref": "workspace://results/current.json",
                "resume_cursor": "cursor-result-invalid",
                "cursor": "cursor-result-invalid",
            },
            execution_id=execution_id,
            execution_expected_status=ExecutionStatus.EXECUTING,
            execution_expected_version=1,
        )


@pytest.mark.asyncio
async def test_execution_result_observed_checkpoint_accepts_terminal_status_and_respects_stale_fence(
    workflow_database: Database,
):
    run_context = await _create_run_context(workflow_database, suffix="-result-valid")
    coordinator = _coordinator(workflow_database)
    first = await coordinator.start_run(run_context, "runtime-1")
    execution_id = await _seed_execution(workflow_database, first, status=ExecutionStatus.SUCCEEDED)

    await _expire_lease_with_db_clock(workflow_database, run_context)
    current = await coordinator.acquire_run(run_context, "runtime-2")

    with pytest.raises(StaleFenceError):
        await coordinator.checkpoint(
            first,
            CheckpointPhase.EXECUTION_RESULT_OBSERVED,
            {
                "run_id": run_context.run_id,
                "execution_id": execution_id,
                "result_status": ExecutionStatus.SUCCEEDED.value,
                "result_digest": "3" * 64,
                "result_ref": "workspace://results/final.json",
                "resume_cursor": "cursor-result-stale",
                "cursor": "cursor-result-stale",
            },
            execution_id=execution_id,
            execution_expected_status=ExecutionStatus.SUCCEEDED,
            execution_expected_version=1,
        )

    await coordinator.checkpoint(
        current,
        CheckpointPhase.EXECUTION_RESULT_OBSERVED,
        {
            "run_id": run_context.run_id,
            "execution_id": execution_id,
            "result_status": ExecutionStatus.SUCCEEDED.value,
            "result_digest": "3" * 64,
            "result_ref": "workspace://results/final.json",
            "resume_cursor": "cursor-result-valid",
            "cursor": "cursor-result-valid",
        },
        execution_id=execution_id,
        execution_expected_status=ExecutionStatus.SUCCEEDED,
        execution_expected_version=1,
    )


@pytest.mark.asyncio
async def test_start_run_enforces_persisted_quota(workflow_database: Database):
    settings = Settings(_config_file="/nonexistent", runtime={"max_concurrent_runs_per_tenant": 2})
    tenant_id, workspace_id = await _seed_user(workflow_database, "quota@example.com")
    base = TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)
    contexts: list[TenantContext] = []
    async with TenantUnitOfWork(workflow_database, base) as uow:
        for index in range(3):
            session = await uow.sessions.create(title=f"Quota {index}")
            contexts.append(base.for_run(session.id, str(uuid4())))

    results = await asyncio.gather(
        _coordinator(workflow_database, settings).start_run(contexts[0], "runtime-a"),
        _coordinator(workflow_database, settings).start_run(contexts[1], "runtime-b"),
        _coordinator(workflow_database, settings).start_run(contexts[2], "runtime-c"),
        return_exceptions=True,
    )

    assert sum(isinstance(result, RunLease) for result in results) == 2
    assert sum(isinstance(result, TenantRunQuotaError) for result in results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shift_hours", (-24, 24))
async def test_lease_takeover_uses_db_clock_not_python_wall_time(
    workflow_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    shift_hours: int,
):
    run_context = await _create_run_context(workflow_database, suffix=f"-clock-{shift_hours}")
    shifted = time.time() + (shift_hours * 3600)
    monkeypatch.setattr(time, "time", lambda: shifted)

    first = await _coordinator(workflow_database).start_run(run_context, "runtime-1")
    await _expire_lease_with_db_clock(workflow_database, run_context)
    second = await _coordinator(workflow_database).acquire_run(run_context, "runtime-2")

    assert second.fencing_token == first.fencing_token + 1


@pytest.mark.asyncio
async def test_tenant_context_rejects_active_user_without_default_workspace():
    request = Request({"type": "http", "headers": []})
    user = UserRecord(
        id=str(uuid4()),
        email="orphan@example.com",
        status="active",
        default_workspace_id=None,
        auth_epoch=0,
        created_at=1,
        updated_at=1,
    )

    with pytest.raises(HTTPException, match="Account unavailable") as exc_info:
        await tenant_context(request, user)

    assert exc_info.value.status_code == 403
