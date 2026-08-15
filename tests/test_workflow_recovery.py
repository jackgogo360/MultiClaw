from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import insert, select, text, update

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.storage import Database
from multiclaw.storage.schema import execution_checkpoints
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import CheckpointPhase, CorruptCheckpointError, RunStatus, StaleFenceError
from multiclaw.workflow.recovery import RecoveryService, validate_phase_payload

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'workflow-recovery.db'}"


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
    tenant_id, workspace_id = await _seed_user(database, f"workflow-recovery{suffix}@example.com")
    context = TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)
    async with TenantUnitOfWork(database, context) as uow:
        session = await uow.sessions.create(title="Workflow Recovery")
    return context.for_run(session.id, str(uuid4()))


def _coordinator(database: Database, settings: Settings | None = None) -> WorkflowCoordinator:
    return WorkflowCoordinator(database, settings=settings or Settings(_config_file="/nonexistent"))


async def _expire_run_lease_with_db_clock(database: Database, context: TenantContext) -> None:
    from multiclaw.storage.schema import agent_runs

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


async def _checkpoint_count(database: Database, context: TenantContext) -> int:
    async with database.connect() as conn:
        count = await conn.scalar(
            select(text("COUNT(*)")).select_from(execution_checkpoints).where(
                execution_checkpoints.c.tenant_id == context.tenant_id,
                execution_checkpoints.c.workspace_id == context.workspace_id,
                execution_checkpoints.c.session_id == context.session_id,
                execution_checkpoints.c.run_id == context.run_id,
            )
        )
    return int(count or 0)


async def _corrupt_payload_without_hash_update(
    database: Database,
    checkpoint_id: str,
    *,
    replacement_payload: dict[str, object],
) -> None:
    payload_json = json.dumps(
        replacement_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    async with database.write_transaction() as conn:
        await conn.execute(
            update(execution_checkpoints)
            .where(execution_checkpoints.c.checkpoint_id == checkpoint_id)
            .values(payload_json=payload_json)
        )


@pytest.mark.asyncio
async def test_checkpoint_hash_mismatch_blocks_run(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-hash")
    lease = await _coordinator(workflow_database).start_run(run_context, "runtime-1")

    checkpoint = await _coordinator(workflow_database).checkpoint(
        lease,
        CheckpointPhase.MODEL_OUTPUT_COMMITTED,
        {
            "run_id": run_context.run_id,
            "message_id": "msg-1",
            "output_digest": "a" * 64,
            "model_cursor": "cursor-1",
            "cursor": "cursor-1",
        },
    )

    await _corrupt_payload_without_hash_update(
        workflow_database,
        checkpoint.checkpoint_id,
        replacement_payload={
            "schema_version": 1,
            "run_id": run_context.run_id,
            "message_id": "msg-corrupt",
            "output_digest": "b" * 64,
            "model_cursor": "cursor-1",
            "next_step": "tool_plan_or_terminal",
            "cursor": "cursor-1",
        },
    )
    await _expire_run_lease_with_db_clock(workflow_database, run_context)

    outcome = await RecoveryService(workflow_database).recover(run_context, "runtime-2")

    assert outcome.status == RunStatus.BLOCKED_CORRUPT
    assert outcome.executions_started == 0


@pytest.mark.asyncio
async def test_old_fence_cannot_write_run_only_checkpoint(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-stale-fence")
    expired_lease = await _coordinator(workflow_database).start_run(run_context, "runtime-1")
    await _expire_run_lease_with_db_clock(workflow_database, run_context)
    await _coordinator(workflow_database).acquire_run(run_context, "runtime-2")

    with pytest.raises(StaleFenceError):
        await _coordinator(workflow_database).checkpoint(
            expired_lease,
            CheckpointPhase.MODEL_OUTPUT_COMMITTED,
            {
                "run_id": run_context.run_id,
                "message_id": "msg-1",
                "output_digest": "c" * 64,
                "model_cursor": "cursor-2",
                "cursor": "cursor-2",
            },
        )

    assert await _checkpoint_count(workflow_database, run_context) == 0


@pytest.mark.asyncio
async def test_unsupported_checkpoint_schema_blocks_incompatible(workflow_database: Database):
    run_context = await _create_run_context(workflow_database, suffix="-incompatible")
    lease = await _coordinator(workflow_database).start_run(run_context, "runtime-1")
    payload = {
        "schema_version": 2,
        "run_id": run_context.run_id,
        "message_id": "msg-1",
        "output_digest": "d" * 64,
        "model_cursor": "cursor-3",
        "next_step": "tool_plan_or_terminal",
        "cursor": "cursor-3",
    }
    payload_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    async with workflow_database.write_transaction() as conn:
        await conn.execute(
            insert(execution_checkpoints).values(
                checkpoint_id=str(uuid4()),
                tenant_id=run_context.tenant_id,
                workspace_id=run_context.workspace_id,
                session_id=run_context.session_id,
                run_id=run_context.run_id,
                approval_id=None,
                execution_id=None,
                phase=CheckpointPhase.MODEL_OUTPUT_COMMITTED.value,
                checkpoint_seq=1,
                payload_json=payload_json,
                payload_hash=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                schema_version=2,
                created_at=workflow_database.dialect.db_now_ms(),
            )
        )
    await _expire_run_lease_with_db_clock(workflow_database, run_context)

    outcome = await RecoveryService(workflow_database).recover(run_context, "runtime-2")

    assert outcome.status == RunStatus.BLOCKED_INCOMPATIBLE
    assert outcome.executions_started == 0


def test_checkpoint_timestamp_fields_reject_string_coercion() -> None:
    with pytest.raises(CorruptCheckpointError):
        validate_phase_payload(
            CheckpointPhase.RUN_STARTED,
            {
                "tenant_id": "00000000-0000-0000-0000-000000000001",
                "workspace_id": "00000000-0000-0000-0000-000000000002",
                "session_id": "00000000-0000-0000-0000-000000000003",
                "run_id": "00000000-0000-0000-0000-000000000004",
                "started_at_ms": "123",
                "model_cursor": "cursor-1",
                "cursor": "cursor-1",
            },
        )

    with pytest.raises(CorruptCheckpointError):
        validate_phase_payload(
            CheckpointPhase.RUN_TERMINAL,
            {
                "run_id": "00000000-0000-0000-0000-000000000004",
                "terminal_status": RunStatus.COMPLETED.value,
                "finished_at_ms": "456",
                "final_digest": "a" * 64,
            },
        )
