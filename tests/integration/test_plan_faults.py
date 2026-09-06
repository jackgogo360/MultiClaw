from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import update

from alembic import command
from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.memory import MemoryEntry
from multiclaw.planner.execution import PlanExecutionCoordinator
from multiclaw.planner.models import (
    MaterializeInitialPlan,
    PlanDecisionAction,
    PlanDecisionRequest,
    PlanDraft,
    PlanDraftStep,
    PlanStepCompletion,
    PlanTriggerMode,
)
from multiclaw.planner.service import PlanningService
from multiclaw.storage import Database
from multiclaw.storage.schema import agent_runs, execution_checkpoints
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.tools import ToolRegistry
from multiclaw.workflow.models import RecoveryAction, RunLeaseHandle, RunStatus
from multiclaw.workflow.recovery import RecoveryService


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'plan-faults.db'}"


async def _database(tmp_path: Path) -> Database:
    url = _sqlite_url(tmp_path)
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=url), "head")
    return Database.create(DatabaseSettings(driver="sqlite", url=url))


async def _context(database: Database) -> TenantContext:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(
            f"plan-faults-{uuid4()}@example.com"
        )
        assert user.default_workspace_id is not None
    root = TenantContext(tenant_id=user.id, workspace_id=user.default_workspace_id)
    async with TenantUnitOfWork(database, root) as uow:
        session = await uow.sessions.create(title="Plan recovery faults")
    return root.for_run(session.id, str(uuid4()))


async def _expire(database: Database, context: TenantContext) -> None:
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


async def _replace_checkpoint_payload(
    database: Database,
    checkpoint_id: str,
    payload: dict[str, object],
) -> None:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    async with database.write_transaction() as conn:
        await conn.execute(
            update(execution_checkpoints)
            .where(execution_checkpoints.c.checkpoint_id == checkpoint_id)
            .values(
                payload_json=encoded,
                payload_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            )
        )


@dataclass
class _IdempotentRunner:
    fail_once: bool = False
    effects: dict[str, int] = field(default_factory=dict)
    duplicate_count: int = 0
    invocations: int = 0
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    skill_manager: object = field(
        default_factory=lambda: SimpleNamespace(active_skills=())
    )

    async def run_plan_step(self, request, **_kwargs):
        self.invocations += 1
        key = f"{request.plan.plan_id}:{request.step.step_id}:{request.step_run.attempt}"
        if key not in self.effects:
            self.effects[key] = 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("injected crash after the external effect")
        return PlanStepCompletion(status="succeeded", summary="done", evidence=[])


async def _materialized(database: Database):
    settings = Settings(_config_file="/nonexistent")
    context = await _context(database)
    service = PlanningService(database, settings=settings)
    async with TenantUnitOfWork(database, context) as uow:
        source = await uow.memory.save(
            MemoryEntry(
                content="Create a durable Plan.",
                type="chat_message",
                role="user",
                session_id=context.session_id,
            )
        )
    result = await service.materialize_initial(
        MaterializeInitialPlan(
            context=context,
            runtime_instance_id="runtime-a",
            source_message_id=source.id,
            assistant_turn_index=1,
            trigger_mode=PlanTriggerMode.EXPLICIT,
            draft=PlanDraft(
                objective="Recover exactly once",
                constraints=[],
                generation_reason="fault test",
                steps=[
                    PlanDraftStep(
                        logical_step_key="execute",
                        title="Execute",
                        description="Perform the idempotent external operation.",
                        expected_outcome="one durable result",
                        max_attempts=1,
                        depends_on=[],
                    )
                ],
            ),
        )
    )
    return settings, context, service, result


async def _approve(service, context, result):
    return await service.decide(
        PlanDecisionRequest(
            decision_id=str(uuid4()),
            plan_id=result.plan.plan_id,
            plan_version=1,
            expected_version=result.plan.aggregate_version,
            action=PlanDecisionAction.APPROVE,
        ),
        decided_by=context.tenant_id,
        runtime_instance_id="runtime-a",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "window",
    (
        "after_plan_commit_before_sse",
        "after_decision_commit_before_resume",
        "after_step_run_before_dispatch",
        "after_tool_completion_before_step_result",
        "after_step_success_before_next_selection",
    ),
)
async def test_plan_crash_windows_preserve_idempotent_external_effects(
    tmp_path: Path,
    window: str,
):
    database = await _database(tmp_path)
    try:
        settings, context, service, result = await _materialized(database)
        runner = _IdempotentRunner()
        coordinator = PlanExecutionCoordinator(database, settings=settings)

        if window == "after_plan_commit_before_sse":
            # GET convergence is durable and does not need EventRouter replay.
            async with TenantUnitOfWork(database, context) as uow:
                fetched = await uow.plans.get(result.plan.plan_id)
            assert fetched is not None
            assert fetched.current.content_digest == result.plan.current.content_digest
            await _expire(database, context)
            outcome = await RecoveryService(database).recover(context, "runtime-b")
            assert outcome.action is RecoveryAction.AWAIT_PLAN_DECISION
            assert outcome.lease is None
        else:
            decision = await _approve(service, context, result)
            assert decision.lease is not None
            lease = decision.lease
            if window == "after_step_run_before_dispatch":
                await coordinator.start_next_attempt(context=context, lease=lease)
            elif window == "after_tool_completion_before_step_result":
                await coordinator.start_next_attempt(context=context, lease=lease)
                runner.fail_once = True
                with pytest.raises(RuntimeError, match="injected crash"):
                    await coordinator.execute_to_boundary(
                        context=context,
                        run_lease_handle=RunLeaseHandle(lease),
                        runner=runner,
                        resume_running_attempt=True,
                    )
            elif window == "after_step_success_before_next_selection":
                await coordinator.execute_to_boundary(
                    context=context,
                    run_lease_handle=RunLeaseHandle(lease),
                    runner=runner,
                )

            await _expire(database, context)
            outcome = await RecoveryService(database).recover(context, "runtime-b")
            assert outcome.action is RecoveryAction.RESUME_PLAN_STEP
            assert outcome.lease is not None
            if window != "after_step_success_before_next_selection":
                await coordinator.execute_to_boundary(
                    context=context,
                    run_lease_handle=RunLeaseHandle(outcome.lease),
                    runner=runner,
                    resume_running_attempt=(
                        outcome.plan_recovery_context is not None
                        and outcome.plan_recovery_context.running_step is not None
                    ),
                )

        assert all(count == 1 for count in runner.effects.values())
        assert runner.duplicate_count == 0
        run = await service.workflow.get_run(context)
        assert run is not None
        assert run.status not in {RunStatus.BLOCKED_CORRUPT, RunStatus.BLOCKED_INCOMPATIBLE}
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ("missing_plan", "foreign_scope", "digest_mismatch", "missing_step"),
)
async def test_corrupt_plan_recovery_fails_closed_before_effects(
    tmp_path: Path,
    corruption: str,
):
    database = await _database(tmp_path)
    try:
        settings, context, service, result = await _materialized(database)
        coordinator = PlanExecutionCoordinator(database, settings=settings)
        if corruption == "missing_step":
            decision = await _approve(service, context, result)
            assert decision.lease is not None
            await coordinator.start_next_attempt(context=context, lease=decision.lease)
        checkpoint = await service.workflow.get_latest_checkpoint(context)
        assert checkpoint is not None
        payload = json.loads(checkpoint.payload_json)
        if corruption == "missing_plan":
            async with database.write_transaction() as conn:
                await conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.run_id == context.run_id)
                    .values(
                        plan_id=None,
                        initial_plan_version=None,
                        active_plan_version=None,
                    )
                )
        elif corruption == "foreign_scope":
            payload["plan_id"] = str(uuid4())
            await _replace_checkpoint_payload(database, checkpoint.checkpoint_id, payload)
        elif corruption == "digest_mismatch":
            payload["plan_digest"] = "f" * 64
            await _replace_checkpoint_payload(database, checkpoint.checkpoint_id, payload)
        else:
            payload["step_id"] = str(uuid4())
            await _replace_checkpoint_payload(database, checkpoint.checkpoint_id, payload)
        await _expire(database, context)

        outcome = await RecoveryService(database).recover(context, "runtime-b")

        assert outcome.status in {RunStatus.BLOCKED_CORRUPT, RunStatus.BLOCKED_INCOMPATIBLE}
        assert outcome.executions_started == 0
    finally:
        await database.dispose()
