from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import insert, select, update

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.storage import Database
from multiclaw.storage.schema import (
    agent_runs,
    approval_requests,
    chat_sessions,
    deletion_jobs,
    tool_executions,
    users,
)
from multiclaw.storage.repositories.deletions import next_claimable_tenant_id
from multiclaw.storage.uow import AuthUnitOfWork, DeletionUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    ApprovalStatus,
    ExecutionStatus,
    RecoveryStrategy,
    RunStatus,
    TERMINAL_RUN_STATUSES,
)

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'deletion-service.db'}"


async def _upgrade_database(url: str) -> Database:
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=url), "head")
    driver = "mysql" if url.startswith("mysql+aiomysql://") else "sqlite"
    return Database.create(DatabaseSettings(driver=driver, url=url))


@pytest.fixture(params=("sqlite", "mysql"))
async def deletion_database(request: pytest.FixtureRequest, tmp_path: Path):
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


class _TrackingRuntimePool:
    def __init__(self) -> None:
        self.revoked: list[str] = []

    async def revoke(self, tenant_id: str) -> None:
        self.revoked.append(tenant_id)


async def _seed_user(database: Database, email: str) -> tuple[str, str]:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(email)
        assert user.default_workspace_id is not None
        return user.id, user.default_workspace_id


async def _create_run_context(database: Database, *, tenant_id: str, workspace_id: str) -> TenantContext:
    context = TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)
    async with TenantUnitOfWork(database, context) as uow:
        session = await uow.sessions.create(title="Deletion workflow session")
    return context.for_run(session.id, str(uuid4()))


async def _db_now_ms(database: Database) -> int:
    async with database.connect() as conn:
        return int((await conn.execute(select(database.dialect.db_now_ms()))).scalar_one())


async def _user_and_job(
    database: Database,
    tenant_id: str,
) -> tuple[dict[str, object], dict[str, object] | None]:
    async with database.connect() as conn:
        user_row = (
            await conn.execute(
                select(
                    users.c.id,
                    users.c.status,
                    users.c.auth_epoch,
                    users.c.purge_requested_at,
                    users.c.purge_after,
                ).where(users.c.id == tenant_id)
            )
        ).mappings().one()
        job_row = (
            await conn.execute(
                select(
                    deletion_jobs.c.job_id,
                    deletion_jobs.c.tenant_id,
                    deletion_jobs.c.status,
                    deletion_jobs.c.requested_at,
                    deletion_jobs.c.purge_after,
                    deletion_jobs.c.worker_id,
                    deletion_jobs.c.lease_expires_at,
                    deletion_jobs.c.version,
                    deletion_jobs.c.fencing_token,
                    deletion_jobs.c.attempt_count,
                ).where(deletion_jobs.c.tenant_id == tenant_id)
            )
        ).mappings().first()
    return dict(user_row), None if job_row is None else dict(job_row)


async def _run_and_approval_state(
    database: Database,
    *,
    tenant_id: str,
    run_id: str,
    approval_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    async with database.connect() as conn:
        run_row = (
            await conn.execute(
                select(
                    agent_runs.c.run_status,
                    agent_runs.c.lease_expires_at,
                    agent_runs.c.finished_at,
                ).where(
                    agent_runs.c.tenant_id == tenant_id,
                    agent_runs.c.run_id == run_id,
                )
            )
        ).mappings().one()
        approval_row = (
            await conn.execute(
                select(
                    approval_requests.c.approval_status,
                    approval_requests.c.resolved_at,
                    approval_requests.c.version,
                ).where(
                    approval_requests.c.tenant_id == tenant_id,
                    approval_requests.c.approval_id == approval_id,
                )
            )
        ).mappings().one()
    return dict(run_row), dict(approval_row)


async def _seed_awaiting_user_run_with_approval(
    database: Database,
    *,
    tenant_id: str,
    workspace_id: str,
    lease_expires_at: int,
) -> tuple[str, str]:
    run_id = str(uuid4())
    session_id = str(uuid4())
    approval_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(chat_sessions).values(
                id=session_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                title="Awaiting approval",
                status="active",
                created_at=1,
                updated_at=1,
                last_message_at=None,
                metadata_json="{}",
            )
        )
        await conn.execute(
            insert(agent_runs).values(
                run_id=run_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_status=RunStatus.AWAITING_USER.value,
                runtime_instance_id="runtime-awaiting",
                lease_owner="runtime-awaiting",
                fencing_token=1,
                lease_expires_at=lease_expires_at,
                heartbeat_at=1,
                schema_version=1,
                version=1,
                created_at=1,
                updated_at=1,
                finished_at=None,
            )
        )
        await conn.execute(
            insert(approval_requests).values(
                approval_id=approval_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_id=run_id,
                tool_call_id="tool-awaiting",
                approval_status=ApprovalStatus.AWAITING_USER.value,
                requested_at=1,
                resolved_at=None,
                expires_at=lease_expires_at + 60_000,
                version=1,
            )
        )
    return run_id, approval_id


async def _seed_blocking_run(
    database: Database,
    *,
    tenant_id: str,
    workspace_id: str,
    run_status: str,
    lease_expires_at: int | None,
) -> str:
    run_id = str(uuid4())
    session_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(chat_sessions).values(
                id=session_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                title="Blocking run",
                status="active",
                created_at=1,
                updated_at=1,
                last_message_at=None,
                metadata_json="{}",
            )
        )
        await conn.execute(
            insert(agent_runs).values(
                run_id=run_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_status=run_status,
                runtime_instance_id="runtime-blocking",
                lease_owner="runtime-blocking",
                fencing_token=1,
                lease_expires_at=lease_expires_at,
                heartbeat_at=1,
                schema_version=1,
                version=1,
                created_at=1,
                updated_at=1,
                finished_at=None if run_status != RunStatus.COMPLETED.value else 1,
            )
        )
    return run_id


async def _seed_executing_tool(
    database: Database,
    *,
    tenant_id: str,
    workspace_id: str,
) -> str:
    run_id = str(uuid4())
    session_id = str(uuid4())
    execution_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(chat_sessions).values(
                id=session_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                title="Executing tool",
                status="active",
                created_at=1,
                updated_at=1,
                last_message_at=None,
                metadata_json="{}",
            )
        )
        await conn.execute(
            insert(agent_runs).values(
                run_id=run_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_status=RunStatus.COMPLETED.value,
                runtime_instance_id="runtime-tool",
                lease_owner=None,
                fencing_token=0,
                lease_expires_at=None,
                heartbeat_at=None,
                schema_version=1,
                version=1,
                created_at=1,
                updated_at=1,
                finished_at=1,
            )
        )
        await conn.execute(
            insert(tool_executions).values(
                execution_id=execution_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_id=run_id,
                approval_id=None,
                tool_call_id="tool-executing",
                tool_name="shell",
                tool_kind="shell",
                execution_status=ExecutionStatus.EXECUTING.value,
                recovery_strategy=RecoveryStrategy.IDEMPOTENT_RETRY.value,
                idempotency_key="idem-1",
                input_payload_json="{}",
                input_hash="a" * 64,
                external_request_id=None,
                result_ref=None,
                result_digest=None,
                schema_version=1,
                version=1,
                created_at=1,
                updated_at=1,
                finished_at=None,
            )
        )
    return execution_id


async def _set_job_purge_after(database: Database, tenant_id: str, purge_after: int) -> None:
    async with database.write_transaction() as conn:
        await conn.execute(
            update(deletion_jobs)
            .where(deletion_jobs.c.tenant_id == tenant_id)
            .values(purge_after=purge_after)
        )
        await conn.execute(
            update(users)
            .where(users.c.id == tenant_id)
            .values(purge_after=purge_after)
        )


def _service(database: Database, runtime_pool: _TrackingRuntimePool | None = None, *, retention_days: int = 7):
    from multiclaw.deletion.service import DeletionService

    return DeletionService(
        database=database,
        runtime_pool=runtime_pool,
        settings=Settings(_config_file="/nonexistent", deletion={"retention_days": retention_days}),
    )


@pytest.mark.asyncio
async def test_request_marks_user_pending_uses_db_clock_cancels_waiting_work_and_revokes_after_commit(
    deletion_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, workspace_id = await _seed_user(deletion_database, "delete-me@example.com")
    run_id, approval_id = await _seed_awaiting_user_run_with_approval(
        deletion_database,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        lease_expires_at=1,
    )
    runtime_pool = _TrackingRuntimePool()
    service = _service(deletion_database, runtime_pool, retention_days=7)

    before_db_now = await _db_now_ms(deletion_database)
    monkeypatch.setattr(time, "time", lambda: 12.0)
    scheduled = await service.request(tenant_id, retention_days=7)
    monkeypatch.setattr(time, "time", lambda: 48_000_000.0)
    after_db_now = await _db_now_ms(deletion_database)

    user_row, job_row = await _user_and_job(deletion_database, tenant_id)
    run_row, approval_row = await _run_and_approval_state(
        deletion_database,
        tenant_id=tenant_id,
        run_id=run_id,
        approval_id=approval_id,
    )

    assert job_row is not None
    assert scheduled.status == "scheduled"
    assert scheduled.job_id == job_row["job_id"]
    assert scheduled.requested_at == job_row["requested_at"]
    assert scheduled.purge_after == job_row["purge_after"]
    assert job_row["status"] == "scheduled"
    assert user_row["status"] == "pending_purge"
    assert user_row["purge_requested_at"] == job_row["requested_at"]
    assert user_row["purge_after"] == job_row["purge_after"]
    assert int(user_row["auth_epoch"]) == 1
    assert int(job_row["purge_after"]) - int(job_row["requested_at"]) == 7 * 86_400_000
    assert before_db_now <= int(job_row["requested_at"]) <= after_db_now
    assert run_row["run_status"] == RunStatus.CANCELLED.value
    assert run_row["lease_expires_at"] is None
    assert run_row["finished_at"] is not None
    assert approval_row["approval_status"] == ApprovalStatus.EXPIRED.value
    assert approval_row["resolved_at"] is not None
    assert approval_row["version"] == 2
    assert runtime_pool.revoked == [tenant_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed_blocker", "description"),
    [
        ("active_run", "running run"),
        ("active_tool", "executing tool"),
        ("valid_lease", "non-expired lease"),
    ],
)
async def test_request_rejects_active_runs_tools_or_valid_leases_without_mutating_state(
    deletion_database: Database,
    seed_blocker: str,
    description: str,
) -> None:
    from multiclaw.deletion.service import ActiveRunsError

    tenant_id, workspace_id = await _seed_user(deletion_database, f"{seed_blocker}@example.com")
    now_ms = await _db_now_ms(deletion_database)
    if seed_blocker == "active_run":
        await _seed_blocking_run(
            deletion_database,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            run_status=RunStatus.RUNNING.value,
            lease_expires_at=now_ms + 30_000,
        )
    elif seed_blocker == "active_tool":
        await _seed_executing_tool(
            deletion_database,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
    else:
        await _seed_blocking_run(
            deletion_database,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            run_status=RunStatus.AWAITING_USER.value,
            lease_expires_at=now_ms + 30_000,
        )

    runtime_pool = _TrackingRuntimePool()
    service = _service(deletion_database, runtime_pool, retention_days=7)

    with pytest.raises(ActiveRunsError, match="active runs"):
        await service.request(tenant_id, retention_days=7)

    user_row, job_row = await _user_and_job(deletion_database, tenant_id)
    assert user_row["status"] == "active", description
    assert user_row["auth_epoch"] == 0, description
    assert user_row["purge_requested_at"] is None, description
    assert user_row["purge_after"] is None, description
    assert job_row is None, description
    assert runtime_pool.revoked == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    sorted(TERMINAL_RUN_STATUSES, key=lambda status: status.value),
)
async def test_request_allows_terminal_run_with_unexpired_lease(
    deletion_database: Database,
    terminal_status: RunStatus,
) -> None:
    tenant_id, workspace_id = await _seed_user(
        deletion_database,
        f"terminal-{terminal_status.value}@example.com",
    )
    run_context = await _create_run_context(
        deletion_database,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    coordinator = WorkflowCoordinator(deletion_database, settings=Settings(_config_file="/nonexistent"))
    lease = await coordinator.start_run_with_checkpoint(run_context, f"runtime-{terminal_status.value}")
    await coordinator.finish_run_with_checkpoint(lease, terminal_status)

    async with deletion_database.connect() as conn:
        run_row = (
            await conn.execute(
                select(
                    agent_runs.c.run_status,
                    agent_runs.c.lease_expires_at,
                    agent_runs.c.finished_at,
                ).where(
                    agent_runs.c.tenant_id == tenant_id,
                    agent_runs.c.run_id == run_context.run_id,
                )
            )
        ).mappings().one()

    assert run_row["run_status"] == terminal_status.value
    assert run_row["finished_at"] is not None
    assert run_row["lease_expires_at"] is not None
    assert int(run_row["lease_expires_at"]) > await _db_now_ms(deletion_database)

    runtime_pool = _TrackingRuntimePool()
    service = _service(deletion_database, runtime_pool, retention_days=7)

    scheduled = await service.request(tenant_id, retention_days=7)
    user_row, job_row = await _user_and_job(deletion_database, tenant_id)

    assert scheduled.status == "scheduled"
    assert job_row is not None
    assert user_row["status"] == "pending_purge"
    assert runtime_pool.revoked == [tenant_id]


@pytest.mark.asyncio
async def test_request_is_idempotent_and_retention_zero_stays_scheduled_but_due(
    deletion_database: Database,
) -> None:
    tenant_id, _workspace_id = await _seed_user(deletion_database, "duplicate@example.com")
    runtime_pool = _TrackingRuntimePool()
    service = _service(deletion_database, runtime_pool, retention_days=0)

    first = await service.request(tenant_id, retention_days=0)
    second = await service.request(tenant_id, retention_days=0)

    user_row, job_row = await _user_and_job(deletion_database, tenant_id)
    async with deletion_database.connect() as conn:
        due_tenant_id = await next_claimable_tenant_id(conn, deletion_database.dialect)

    assert job_row is not None
    assert first.job_id == second.job_id
    assert first.requested_at == second.requested_at
    assert first.purge_after == second.purge_after
    assert job_row["status"] == "scheduled"
    assert user_row["status"] == "pending_purge"
    assert user_row["auth_epoch"] == 1
    assert first.purge_after == first.requested_at
    assert due_tenant_id == tenant_id
    assert runtime_pool.revoked == [tenant_id]


@pytest.mark.asyncio
async def test_recover_succeeds_only_strictly_before_boundary_and_rejects_running_jobs(
    deletion_database: Database,
) -> None:
    from multiclaw.deletion.service import RecoveryWindowClosedError

    tenant_id, _workspace_id = await _seed_user(deletion_database, "recover-me@example.com")
    runtime_pool = _TrackingRuntimePool()
    service = _service(deletion_database, runtime_pool, retention_days=1)

    scheduled = await service.request(tenant_id, retention_days=1)
    await service.recover(tenant_id, scheduled.job_id)

    user_row, job_row = await _user_and_job(deletion_database, tenant_id)
    assert job_row is None
    assert user_row["status"] == "active"
    assert user_row["purge_requested_at"] is None
    assert user_row["purge_after"] is None
    assert user_row["auth_epoch"] == 2
    assert runtime_pool.revoked == [tenant_id, tenant_id]

    rescheduled = await service.request(tenant_id, retention_days=1)
    await _set_job_purge_after(deletion_database, tenant_id, await _db_now_ms(deletion_database))
    with pytest.raises(RecoveryWindowClosedError, match="recovery window"):
        await service.recover(tenant_id, rescheduled.job_id)

    expired_cutoff = await _db_now_ms(deletion_database) - 1
    await _set_job_purge_after(deletion_database, tenant_id, expired_cutoff)
    with pytest.raises(RecoveryWindowClosedError, match="recovery window"):
        await service.recover(tenant_id, rescheduled.job_id)

    async with DeletionUnitOfWork(deletion_database, tenant_id) as uow:
        claimed = await uow.deletions.claim_due(worker_id="worker-running", lease_ttl_ms=5_000)
    assert claimed is not None
    with pytest.raises(RecoveryWindowClosedError, match="scheduled"):
        await service.recover(tenant_id, claimed.job_id)


@pytest.mark.asyncio
async def test_recover_and_claim_race_allows_exactly_one_winner(
    deletion_database: Database,
) -> None:
    from multiclaw.deletion.service import RecoveryWindowClosedError

    tenant_id, _workspace_id = await _seed_user(deletion_database, "race@example.com")
    service = _service(deletion_database, _TrackingRuntimePool(), retention_days=0)
    scheduled = await service.request(tenant_id, retention_days=0)

    async def recover() -> str:
        try:
            await service.recover(tenant_id, scheduled.job_id)
        except RecoveryWindowClosedError:
            return "recover-lost"
        return "recovered"

    async def claim() -> str:
        async with DeletionUnitOfWork(deletion_database, tenant_id) as uow:
            claimed = await uow.deletions.claim_due(worker_id="worker-race", lease_ttl_ms=5_000)
        return "claimed" if claimed is not None else "claim-lost"

    recover_result, claim_result = await asyncio.gather(recover(), claim())
    winners = {recover_result, claim_result} & {"recovered", "claimed"}
    assert len(winners) == 1

    user_row, job_row = await _user_and_job(deletion_database, tenant_id)
    if recover_result == "recovered":
        assert claim_result == "claim-lost"
        assert user_row["status"] == "active"
        assert job_row is None
    else:
        assert recover_result == "recover-lost"
        assert user_row["status"] == "pending_purge"
        assert job_row is not None
        assert job_row["status"] == "running"
        assert job_row["worker_id"] == "worker-race"


@pytest.mark.asyncio
async def test_db_clock_not_wall_clock_controls_request_recover_and_claim_eligibility(
    deletion_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, _workspace_id = await _seed_user(deletion_database, "db-clock@example.com")
    runtime_pool = _TrackingRuntimePool()
    service = _service(deletion_database, runtime_pool, retention_days=0)

    monkeypatch.setattr(time, "time", lambda: -1_000_000.0)
    scheduled = await service.request(tenant_id, retention_days=0)
    monkeypatch.setattr(time, "time", lambda: 9_999_999_999.0)

    async with DeletionUnitOfWork(deletion_database, tenant_id) as uow:
        claimed = await uow.deletions.claim_due(worker_id="worker-db-clock", lease_ttl_ms=5_000)
    assert claimed is not None
    assert claimed.job_id == scheduled.job_id

    tenant_id2, _workspace_id2 = await _seed_user(deletion_database, "db-clock-recover@example.com")
    service2 = _service(deletion_database, runtime_pool, retention_days=1)
    monkeypatch.setattr(time, "time", lambda: -2_000_000.0)
    recoverable = await service2.request(tenant_id2, retention_days=1)
    monkeypatch.setattr(time, "time", lambda: 8_888_888_888.0)
    await service2.recover(tenant_id2, recoverable.job_id)

    user_row, job_row = await _user_and_job(deletion_database, tenant_id2)
    assert user_row["status"] == "active"
    assert job_row is None


@pytest.mark.asyncio
async def test_cross_tenant_job_cannot_be_recovered_or_read(
    deletion_database: Database,
) -> None:
    from multiclaw.deletion.service import RecoveryWindowClosedError

    tenant_a, _workspace_a = await _seed_user(deletion_database, "tenant-a@example.com")
    tenant_b, _workspace_b = await _seed_user(deletion_database, "tenant-b@example.com")
    service = _service(deletion_database, _TrackingRuntimePool(), retention_days=1)
    scheduled = await service.request(tenant_a, retention_days=1)

    async with DeletionUnitOfWork(deletion_database, tenant_b, read_only=True) as uow:
        foreign_job = await uow.deletions.get_by_job_id(scheduled.job_id)
    assert foreign_job is None

    with pytest.raises(RecoveryWindowClosedError):
        await service.recover(tenant_b, scheduled.job_id)

    user_a, job_a = await _user_and_job(deletion_database, tenant_a)
    user_b, job_b = await _user_and_job(deletion_database, tenant_b)
    assert user_a["status"] == "pending_purge"
    assert job_a is not None
    assert user_b["status"] == "active"
    assert job_b is None
