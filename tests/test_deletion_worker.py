from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import MethodType
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import delete, insert, select, update

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.observability import current_metrics
from multiclaw.storage import Database
from multiclaw.storage.schema import (
    agent_runs,
    approval_requests,
    audit_logs,
    chat_sessions,
    deletion_jobs,
    execution_checkpoints,
    memory_entries,
    tool_executions,
    user_secrets,
    users,
    verification_codes,
    workspaces,
)
from multiclaw.storage.uow import AuthUnitOfWork, DeletionUnitOfWork
from multiclaw.tenancy import TenantContext, WorkspaceResolver
from multiclaw.workflow.models import ApprovalStatus, ExecutionStatus, RecoveryStrategy, RunStatus

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'deletion-worker.db'}"


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


async def _seed_scheduled_deletion(database: Database, tenant_id: str) -> str:
    from multiclaw.deletion.service import DeletionService

    service = DeletionService(
        database=database,
        runtime_pool=_TrackingRuntimePool(),
        settings=Settings(_config_file="/nonexistent", deletion={"retention_days": 0}),
    )
    scheduled = await service.request_account_deletion(tenant_id)
    return scheduled.job_id


async def _db_now_ms(database: Database) -> int:
    async with database.connect() as conn:
        return int((await conn.execute(select(database.dialect.db_now_ms()))).scalar_one())


async def _expire_job_lease(database: Database, tenant_id: str) -> None:
    async with database.write_transaction() as conn:
        await conn.execute(
            update(deletion_jobs)
            .where(deletion_jobs.c.tenant_id == tenant_id)
            .values(lease_expires_at=database.dialect.db_now_ms() - 1)
        )


async def _seed_workspace(database: Database, tenant_id: str, workspace_id: str) -> None:
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(workspaces).values(
                id=workspace_id,
                tenant_id=tenant_id,
                slug=f"workspace-{workspace_id[:8]}",
                name="Workspace",
                status="active",
                created_at=1,
                updated_at=1,
            )
        )


async def _seed_blocking_run(
    database: Database,
    *,
    tenant_id: str,
    workspace_id: str,
    lease_expires_at: int,
) -> tuple[str, str]:
    session_id = str(uuid4())
    run_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(chat_sessions).values(
                id=session_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                title="blocking",
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
                run_status=RunStatus.RUNNING.value,
                runtime_instance_id="runtime-blocking",
                lease_owner="runtime-blocking",
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
    return session_id, run_id


async def _seed_purge_fixture(
    database: Database,
    *,
    tenant_id: str,
    workspace_id: str,
    email: str,
) -> tuple[str, str, str]:
    session_id = str(uuid4())
    run_id = str(uuid4())
    approval_id = str(uuid4())
    execution_id = str(uuid4())
    now_ms = await _db_now_ms(database)
    async with database.write_transaction() as conn:
        await conn.execute(
            insert(chat_sessions).values(
                id=session_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                title="fixture",
                status="active",
                created_at=now_ms,
                updated_at=now_ms,
                last_message_at=None,
                metadata_json="{}",
            )
        )
        await conn.execute(
            insert(memory_entries).values(
                id=str(uuid4()),
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                content="memory",
                type="message",
                role="assistant",
                turn_index=0,
                created_at=now_ms,
                metadata_json="{}",
            )
        )
        await conn.execute(
            insert(agent_runs).values(
                run_id=run_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_status=RunStatus.CANCELLED.value,
                runtime_instance_id=None,
                lease_owner=None,
                fencing_token=1,
                lease_expires_at=None,
                heartbeat_at=None,
                schema_version=1,
                version=1,
                created_at=now_ms,
                updated_at=now_ms,
                finished_at=now_ms,
            )
        )
        await conn.execute(
            insert(approval_requests).values(
                approval_id=approval_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_id=run_id,
                tool_call_id="tool-call",
                approval_status=ApprovalStatus.APPROVED.value,
                requested_at=now_ms,
                resolved_at=now_ms,
                expires_at=now_ms + 60_000,
                version=1,
            )
        )
        await conn.execute(
            insert(tool_executions).values(
                execution_id=execution_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_id=run_id,
                approval_id=approval_id,
                tool_call_id="tool-call",
                tool_name="read_file",
                tool_kind="builtin",
                execution_status=ExecutionStatus.SUCCEEDED.value,
                recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY.value,
                idempotency_key=None,
                input_payload_json="{}",
                input_hash="0" * 64,
                external_request_id=None,
                result_ref=None,
                result_digest=None,
                schema_version=1,
                version=1,
                created_at=now_ms,
                updated_at=now_ms,
                finished_at=now_ms,
            )
        )
        await conn.execute(
            insert(execution_checkpoints).values(
                checkpoint_id=str(uuid4()),
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_id=run_id,
                approval_id=approval_id,
                execution_id=execution_id,
                phase="tool_finished",
                checkpoint_seq=1,
                payload_json="{}",
                payload_hash="1" * 64,
                schema_version=1,
                created_at=now_ms,
            )
        )
        await conn.execute(
            insert(user_secrets).values(
                id=str(uuid4()),
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                provider_kind="api",
                provider_name="fixture",
                secret_name="token",
                key_provider_name="deployment-keyring",
                format_version=1,
                algorithm="AES-256-GCM",
                key_version=1,
                nonce=b"0123456789ab",
                ciphertext=b"ciphertext",
                created_at=now_ms,
                updated_at=now_ms,
                rotated_at=None,
            )
        )
        await conn.execute(
            insert(audit_logs).values(
                audit_id=str(uuid4()),
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                run_id=run_id,
                approval_id=approval_id,
                execution_id=execution_id,
                event_type="tool.finished",
                status="success",
                tool_name="read_file",
                detail_redacted="{}",
                created_at=now_ms,
            )
        )
        await conn.execute(
            insert(verification_codes).values(
                id=str(uuid4()),
                email=email,
                code_digest="digest-current",
                purpose="deletion_recovery",
                expires_at=now_ms + 60_000,
                used_at=None,
                created_at=now_ms,
            )
        )
        await conn.execute(
            insert(verification_codes).values(
                id=str(uuid4()),
                email=email,
                code_digest="digest-expired",
                purpose="login",
                expires_at=now_ms - 1,
                used_at=None,
                created_at=now_ms,
            )
        )
        await conn.execute(
            insert(verification_codes).values(
                id=str(uuid4()),
                email="other@example.com",
                code_digest="digest-other",
                purpose="login",
                expires_at=now_ms + 60_000,
                used_at=None,
                created_at=now_ms,
            )
        )
    return session_id, run_id, approval_id


class _ConnectionProbe:
    def __init__(self, connection, hook) -> None:
        self._connection = connection
        self._hook = hook

    async def execute(self, statement, *args, **kwargs):
        return await self._hook(self._connection, statement, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


async def _job_and_user_state(database: Database, tenant_id: str) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    async with database.connect() as conn:
        job_row = (
            await conn.execute(
                select(
                    deletion_jobs.c.job_id,
                    deletion_jobs.c.status,
                    deletion_jobs.c.worker_id,
                    deletion_jobs.c.started_at,
                    deletion_jobs.c.heartbeat_at,
                    deletion_jobs.c.lease_expires_at,
                    deletion_jobs.c.fencing_token,
                    deletion_jobs.c.version,
                    deletion_jobs.c.attempt_count,
                ).where(deletion_jobs.c.tenant_id == tenant_id)
            )
        ).mappings().first()
        user_row = (
            await conn.execute(
                select(users.c.id, users.c.status).where(users.c.id == tenant_id)
            )
        ).mappings().first()
    return None if job_row is None else dict(job_row), None if user_row is None else dict(user_row)


@pytest.mark.asyncio
async def test_worker_claims_due_job_and_purges_workspace_and_database(
    deletion_database: Database,
    tmp_path: Path,
) -> None:
    from multiclaw.deletion.worker import DeletionWorker

    tenant_id, workspace_id = await _seed_user(deletion_database, "purge@example.com")
    await _seed_scheduled_deletion(deletion_database, tenant_id)

    resolver = WorkspaceResolver(tmp_path)
    workspace = resolver.resolve(TenantContext(tenant_id, workspace_id), create=True)
    (workspace / "notes.txt").write_text("delete me\n", encoding="utf-8")

    runtime_pool = _TrackingRuntimePool()
    worker = DeletionWorker(
        database=deletion_database,
        runtime_pool=runtime_pool,
        workspace_resolver=resolver,
        settings=Settings(_config_file="/nonexistent", deletion={"retention_days": 0}),
        worker_id="worker-a",
    )

    claimed = await worker.purge_due_jobs(batch_size=10)
    job_row, user_row = await _job_and_user_state(deletion_database, tenant_id)

    assert claimed == 1
    assert job_row is None
    assert user_row is None
    assert not workspace.exists()
    assert runtime_pool.revoked == [tenant_id]


@pytest.mark.asyncio
async def test_worker_leaves_database_intact_when_workspace_delete_fails(
    deletion_database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.deletion.worker import DeletionWorker

    tenant_id, workspace_id = await _seed_user(deletion_database, "fs-failure@example.com")
    await _seed_scheduled_deletion(deletion_database, tenant_id)

    resolver = WorkspaceResolver(tmp_path)
    workspace = resolver.resolve(TenantContext(tenant_id, workspace_id), create=True)
    (workspace / "notes.txt").write_text("keep me\n", encoding="utf-8")

    worker = DeletionWorker(
        database=deletion_database,
        runtime_pool=_TrackingRuntimePool(),
        workspace_resolver=resolver,
        settings=Settings(_config_file="/nonexistent", deletion={"retention_days": 0}),
        worker_id="worker-failure",
    )

    def fail_unlink_tree(_path: Path) -> None:
        raise OSError("disk error")

    monkeypatch.setattr(worker, "_remove_workspace_tree", fail_unlink_tree)
    current_metrics().clear()

    claimed = await worker.purge_due_jobs(batch_size=10)
    job_row, user_row = await _job_and_user_state(deletion_database, tenant_id)

    assert claimed == 0
    assert workspace.exists()
    assert user_row is not None
    assert user_row["status"] == "pending_purge"
    assert job_row is not None
    assert job_row["status"] == "running"
    assert any(metric_name == "multiclaw_purge_retry_total" for metric_name, _labels in current_metrics().counters)


@pytest.mark.asyncio
async def test_worker_rejects_symlink_escape_before_purge(
    deletion_database: Database,
    tmp_path: Path,
) -> None:
    from multiclaw.deletion.worker import DeletionWorker

    tenant_id, workspace_id = await _seed_user(deletion_database, "symlink@example.com")
    await _seed_scheduled_deletion(deletion_database, tenant_id)

    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / tenant_id).symlink_to(outside, target_is_directory=True)

    worker = DeletionWorker(
        database=deletion_database,
        runtime_pool=_TrackingRuntimePool(),
        workspace_resolver=WorkspaceResolver(root),
        settings=Settings(_config_file="/nonexistent", deletion={"retention_days": 0}),
        worker_id="worker-symlink",
    )

    claimed = await worker.purge_due_jobs(batch_size=10)
    job_row, user_row = await _job_and_user_state(deletion_database, tenant_id)

    assert claimed == 0
    assert user_row is not None
    assert job_row is not None
    assert job_row["status"] == "running"


@pytest.mark.asyncio
async def test_worker_leaves_workspace_and_rows_intact_when_active_runs_still_exist(
    deletion_database: Database,
    tmp_path: Path,
) -> None:
    from multiclaw.deletion.worker import DeletionWorker

    tenant_id, workspace_id = await _seed_user(deletion_database, "active-run@example.com")
    await _seed_scheduled_deletion(deletion_database, tenant_id)
    now_ms = await _db_now_ms(deletion_database)
    await _seed_blocking_run(
        deletion_database,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        lease_expires_at=now_ms + 60_000,
    )

    resolver = WorkspaceResolver(tmp_path)
    workspace = resolver.resolve(TenantContext(tenant_id, workspace_id), create=True)
    worker = DeletionWorker(
        database=deletion_database,
        runtime_pool=_TrackingRuntimePool(),
        workspace_resolver=resolver,
        settings=Settings(_config_file="/nonexistent", deletion={"retention_days": 0}),
        worker_id="worker-active-run",
    )

    claimed = await worker.purge_due_jobs(batch_size=10)
    job_row, user_row = await _job_and_user_state(deletion_database, tenant_id)

    assert claimed == 0
    assert workspace.exists()
    assert user_row is not None
    assert user_row["status"] == "pending_purge"
    assert job_row is not None
    assert job_row["status"] == "running"


@pytest.mark.asyncio
async def test_worker_retries_successfully_after_first_attempt_deletes_files_then_crashes(
    deletion_database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.deletion.worker import DeletionWorker

    tenant_id, workspace_id = await _seed_user(deletion_database, "retry-files@example.com")
    await _seed_scheduled_deletion(deletion_database, tenant_id)

    resolver = WorkspaceResolver(tmp_path)
    workspace = resolver.resolve(TenantContext(tenant_id, workspace_id), create=True)
    (workspace / "notes.txt").write_text("retry me\n", encoding="utf-8")

    worker = DeletionWorker(
        database=deletion_database,
        runtime_pool=_TrackingRuntimePool(),
        workspace_resolver=resolver,
        settings=Settings(_config_file="/nonexistent", deletion={"retention_days": 0}),
        worker_id="worker-retry-files",
    )

    original_remove = worker._remove_workspace_tree
    call_count = 0

    def remove_then_fail(path: Path) -> None:
        nonlocal call_count
        call_count += 1
        original_remove(path)
        raise OSError("boom")

    monkeypatch.setattr(worker, "_remove_workspace_tree", remove_then_fail)
    first = await worker.purge_due_jobs(batch_size=10)
    await _expire_job_lease(deletion_database, tenant_id)
    monkeypatch.setattr(worker, "_remove_workspace_tree", original_remove)
    second = await worker.purge_due_jobs(batch_size=10)
    job_row, user_row = await _job_and_user_state(deletion_database, tenant_id)

    assert first == 0
    assert second == 1
    assert call_count == 1
    assert not workspace.exists()
    assert job_row is None
    assert user_row is None


@pytest.mark.asyncio
async def test_worker_batch_reports_counts_refreshes_lease_and_supports_cancellable_loop(
    deletion_database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.deletion.worker import DeletionWorker
    from multiclaw.storage.repositories.deletions import DeletionJobRepository

    tenant_id, workspace_id = await _seed_user(deletion_database, "batch-heartbeat@example.com")
    second_workspace_id = str(uuid4())
    await _seed_workspace(deletion_database, tenant_id, second_workspace_id)
    await _seed_scheduled_deletion(deletion_database, tenant_id)

    resolver = WorkspaceResolver(tmp_path)
    resolver.resolve(TenantContext(tenant_id, workspace_id), create=True)
    resolver.resolve(TenantContext(tenant_id, second_workspace_id), create=True)

    heartbeat_calls: list[tuple[str, int, int]] = []
    original_heartbeat = DeletionJobRepository.heartbeat

    async def spy_heartbeat(self, tenant_id: str, *, worker_id: str, fencing_token: int, version: int, lease_ttl_ms: int):
        heartbeat_calls.append((worker_id, fencing_token, version))
        return await original_heartbeat(
            self,
            tenant_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
            version=version,
            lease_ttl_ms=lease_ttl_ms,
        )

    monkeypatch.setattr(DeletionJobRepository, "heartbeat", spy_heartbeat)

    worker = DeletionWorker(
        database=deletion_database,
        runtime_pool=_TrackingRuntimePool(),
        workspace_resolver=resolver,
        settings=Settings(_config_file="/nonexistent", deletion={"retention_days": 0}),
        worker_id="worker-batch",
    )

    batch = await worker.run_batch(batch_size=10)

    stop_event = asyncio.Event()
    poll_calls: list[int] = []

    async def fake_run_batch(self, *, batch_size: int = 10):
        poll_calls.append(batch_size)
        stop_event.set()
        return batch

    monkeypatch.setattr(worker, "run_batch", MethodType(fake_run_batch, worker))
    await worker.run_until_stopped(stop_event=stop_event, batch_size=3, interval_seconds=60.0)

    assert batch.claimed == 1
    assert batch.completed == 1
    assert batch.failed == 0
    assert len(heartbeat_calls) >= 2
    assert poll_calls == [3]


@pytest.mark.asyncio
async def test_purge_account_uses_restricted_fk_order_and_retains_other_tenant_rows(
    deletion_database: Database,
) -> None:
    tenant_id, workspace_id = await _seed_user(deletion_database, "purge-order@example.com")
    other_tenant_id, _other_workspace_id = await _seed_user(deletion_database, "other-tenant@example.com")
    await _seed_purge_fixture(
        deletion_database,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        email="purge-order@example.com",
    )
    await _seed_scheduled_deletion(deletion_database, tenant_id)

    async with DeletionUnitOfWork(deletion_database, tenant_id) as uow:
        claimed = await uow.deletions.claim_due(worker_id="worker-order", lease_ttl_ms=5_000)
    assert claimed is not None

    seen_tables: list[str] = []

    async with DeletionUnitOfWork(deletion_database, tenant_id) as uow:
        async def spy_execute(connection, statement, *args, **kwargs):
            visit_name = getattr(statement, "__visit_name__", "")
            table_name = getattr(getattr(statement, "table", None), "name", None)
            if visit_name in {"update", "delete"} and table_name is not None:
                seen_tables.append(table_name)
            return await connection.execute(statement, *args, **kwargs)

        uow.deletions._conn = _ConnectionProbe(uow.conn, spy_execute)
        purged = await uow.deletions.purge_account(
            tenant_id,
            worker_id="worker-order",
            fencing_token=claimed.fencing_token,
            version=claimed.version,
        )
        assert purged is True

    async with deletion_database.connect() as conn:
        remaining_user = await conn.scalar(select(users.c.id).where(users.c.id == tenant_id))
        remaining_job = await conn.scalar(select(deletion_jobs.c.job_id).where(deletion_jobs.c.tenant_id == tenant_id))
        other_user = await conn.scalar(select(users.c.id).where(users.c.id == other_tenant_id))
        remaining_codes = [
            tuple(row)
            for row in (
                await conn.execute(
                    select(verification_codes.c.email, verification_codes.c.code_digest).order_by(
                        verification_codes.c.email.asc(),
                        verification_codes.c.code_digest.asc(),
                    )
                )
            ).all()
        ]

    assert seen_tables == [
        "users",
        "execution_checkpoints",
        "audit_logs",
        "tool_executions",
        "approval_requests",
        "agent_runs",
        "memory_entries",
        "chat_sessions",
        "user_secrets",
        "workspaces",
        "verification_codes",
        "deletion_jobs",
        "users",
    ]
    assert remaining_user is None
    assert remaining_job is None
    assert other_user == other_tenant_id
    assert remaining_codes == [
        ("other@example.com", "digest-other"),
        ("purge-order@example.com", "digest-expired"),
    ]


@pytest.mark.asyncio
async def test_purge_account_rolls_back_mid_transaction_and_preserves_rows(
    deletion_database: Database,
) -> None:
    tenant_id, workspace_id = await _seed_user(deletion_database, "rollback@example.com")
    await _seed_purge_fixture(
        deletion_database,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        email="rollback@example.com",
    )
    await _seed_scheduled_deletion(deletion_database, tenant_id)

    async with DeletionUnitOfWork(deletion_database, tenant_id) as uow:
        claimed = await uow.deletions.claim_due(worker_id="worker-rollback", lease_ttl_ms=5_000)
    assert claimed is not None

    with pytest.raises(RuntimeError, match="rollback purge"):
        async with DeletionUnitOfWork(deletion_database, tenant_id) as uow:
            async def fail_on_memory_delete(connection, statement, *args, **kwargs):
                if (
                    getattr(statement, "__visit_name__", "") == "delete"
                    and getattr(getattr(statement, "table", None), "name", None) == "memory_entries"
                ):
                    raise RuntimeError("rollback purge")
                return await connection.execute(statement, *args, **kwargs)

            uow.deletions._conn = _ConnectionProbe(uow.conn, fail_on_memory_delete)
            await uow.deletions.purge_account(
                tenant_id,
                worker_id="worker-rollback",
                fencing_token=claimed.fencing_token,
                version=claimed.version,
            )

    async with deletion_database.connect() as conn:
        user_row = (
            await conn.execute(
                select(users.c.status, users.c.default_workspace_id).where(users.c.id == tenant_id)
            )
        ).mappings().one()
        session_count = len(
            (
                await conn.execute(select(chat_sessions.c.id).where(chat_sessions.c.tenant_id == tenant_id))
            ).all()
        )
        memory_count = len(
            (
                await conn.execute(select(memory_entries.c.id).where(memory_entries.c.tenant_id == tenant_id))
            ).all()
        )
        job_row = (
            await conn.execute(
                select(deletion_jobs.c.status).where(deletion_jobs.c.tenant_id == tenant_id)
            )
        ).mappings().one()

    assert user_row["status"] == "pending_purge"
    assert user_row["default_workspace_id"] == workspace_id
    assert session_count == 1
    assert memory_count == 1
    assert job_row["status"] == "running"


@pytest.mark.asyncio
async def test_purge_account_treats_missing_user_and_job_as_already_completed(
    deletion_database: Database,
) -> None:
    tenant_id, _workspace_id = await _seed_user(deletion_database, "already-gone@example.com")
    await _seed_scheduled_deletion(deletion_database, tenant_id)

    async with DeletionUnitOfWork(deletion_database, tenant_id) as uow:
        claimed = await uow.deletions.claim_due(worker_id="worker-complete", lease_ttl_ms=5_000)
    assert claimed is not None

    async with deletion_database.write_transaction() as conn:
        await conn.execute(delete(deletion_jobs).where(deletion_jobs.c.tenant_id == tenant_id))
        await conn.execute(
            update(users).where(users.c.id == tenant_id).values(default_workspace_id=None)
        )
        await conn.execute(delete(workspaces).where(workspaces.c.tenant_id == tenant_id))
        await conn.execute(delete(users).where(users.c.id == tenant_id))

    async with DeletionUnitOfWork(deletion_database, tenant_id) as uow:
        assert (
            await uow.deletions.purge_account(
                tenant_id,
                worker_id="worker-complete",
                fencing_token=claimed.fencing_token,
                version=claimed.version,
            )
            is True
        )
