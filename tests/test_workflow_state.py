from __future__ import annotations

import asyncio
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
from multiclaw.storage.schema import agent_runs, approval_requests, tool_executions
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    ApprovalRecord,
    ApprovalStatus,
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

