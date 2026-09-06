from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select, update

from alembic import command
from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.events import EventBus
from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
from multiclaw.memory import MemoryEntry
from multiclaw.planner.execution import PlanExecutionCoordinator
from multiclaw.planner.models import (
    MaterializeInitialPlan,
    PlanDecisionAction,
    PlanDecisionRequest,
    PlanDraft,
    PlanDraftStep,
    PlanStepCompletion,
    PlanStepRunStatus,
    PlanTriggerMode,
)
from multiclaw.planner.service import PlanningService
from multiclaw.storage import Database
from multiclaw.storage.schema import (
    agent_plan_step_runs,
    agent_plans,
    agent_runs,
    approval_requests,
    execution_checkpoints,
    tool_executions,
)
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.tools import CoreToolScheduler, ToolRegistry
from multiclaw.tools.base import (
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolStatus,
)
from multiclaw.workflow.models import (
    ApprovalStatus,
    CheckpointPhase,
    ExecutionStatus,
    InvalidTransitionError,
    RecoveryAction,
    RecoveryOutcome,
    RecoveryStrategy,
    RunLeaseHandle,
    RunStatus,
)
from multiclaw.workflow.recovery import (
    RecoveryService,
    RuntimeRecoveryContinuationService,
    WorkflowRecoveryWorker,
)


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
        if key in self.effects:
            self.duplicate_count += 1
        else:
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
            await coordinator.execute_to_boundary(
                context=context,
                run_lease_handle=RunLeaseHandle(outcome.lease),
                runner=runner,
                resume_running_attempt=(
                    outcome.plan_recovery_context is not None
                    and outcome.plan_recovery_context.running_step is not None
                ),
            )

        expected_invocations = {
            "after_plan_commit_before_sse": 0,
            "after_decision_commit_before_resume": 1,
            "after_step_run_before_dispatch": 1,
            "after_tool_completion_before_step_result": 2,
            "after_step_success_before_next_selection": 1,
        }[window]
        assert runner.invocations == expected_invocations
        assert runner.duplicate_count == (1 if expected_invocations == 2 else 0)
        if expected_invocations == 0:
            assert runner.effects == {}
        else:
            assert len(runner.effects) == 1
            assert set(runner.effects.values()) == {1}
            assert await _step_attempt_count(database, context) == 1
            assert await _execution_count(database, context) == 0
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corruption", "terminal_status"),
    (
        ("plan_binding", RunStatus.BLOCKED_CORRUPT),
        ("checkpoint_schema", RunStatus.BLOCKED_INCOMPATIBLE),
    ),
)
async def test_worker_terminalizes_plan_only_recovery_failures_once(
    tmp_path: Path,
    corruption: str,
    terminal_status: RunStatus,
):
    database = await _database(tmp_path)
    try:
        settings, context, service, _result = await _materialized(database)
        checkpoint = await service.workflow.get_latest_checkpoint(context)
        assert checkpoint is not None
        async with database.write_transaction() as conn:
            if corruption == "plan_binding":
                await conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.run_id == context.run_id)
                    .values(
                        plan_id=None,
                        initial_plan_version=None,
                        active_plan_version=None,
                    )
                )
            else:
                await conn.execute(
                    update(execution_checkpoints)
                    .where(execution_checkpoints.c.checkpoint_id == checkpoint.checkpoint_id)
                    .values(schema_version=2)
                )
        await _expire(database, context)
        runtime = _BlockedPlanRuntime(database, settings)
        pool = _BlockedPlanRuntimePool(runtime)
        worker = WorkflowRecoveryWorker(
            database=database,
            settings=settings,
            runtime_pool=pool,
        )

        await worker.run_once()

        run = await service.workflow.get_run(context)
        assert run is not None
        assert run.status is terminal_status
        assert await _terminal_checkpoint_count(database, context) == 1
        assert runtime.agent.generic_recovery_calls == 0
        assert runtime.begin_calls == runtime.closed_leases == pool.acquire_calls == 1

        await worker.run_once()

        assert await _terminal_checkpoint_count(database, context) == 1
        assert runtime.begin_calls == runtime.closed_leases == pool.acquire_calls == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_worker_cannot_terminalize_new_plan_lease(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        settings, context, service, _result = await _materialized(database)
        stale_lease = await service.workflow.resume_waiting_run(context, "runtime-old")
        await _expire(database, context)
        current_lease = await service.workflow.acquire_run(context, "runtime-new")
        runtime = _BlockedPlanRuntime(database, settings)
        worker = WorkflowRecoveryWorker(
            database=database,
            settings=settings,
            runtime_pool=_BlockedPlanRuntimePool(runtime),
        )

        with pytest.raises(InvalidTransitionError):
            await worker._consume_outcome(
                runtime=runtime,
                context=context,
                run_lease_handle=RunLeaseHandle(stale_lease),
                outcome=RecoveryOutcome(status=RunStatus.BLOCKED_CORRUPT),
            )

        run = await service.workflow.get_run(context)
        assert run is not None
        assert run.status is RunStatus.RESUMING
        assert run.lease_owner == current_lease.lease_owner
        assert await _terminal_checkpoint_count(database, context) == 0
    finally:
        await database.dispose()


class _ObservedToolParams(BaseModel):
    idempotency_key: str


class _ObservedToolInvocation(ToolInvocation[_ObservedToolParams]):
    def __init__(
        self,
        params: _ObservedToolParams,
        effect_counts: dict[str, int],
        *,
        result: ToolExecutionResult | None = None,
        cancel_after_effect: bool = False,
    ) -> None:
        super().__init__(name="observed_plan_effect", params=params)
        self._effect_counts = effect_counts
        self._result = result
        self._cancel_after_effect = cancel_after_effect

    async def execute(self) -> ToolExecutionResult:
        self._effect_counts[self.params.idempotency_key] = (
            self._effect_counts.get(self.params.idempotency_key, 0) + 1
        )
        if self._cancel_after_effect:
            raise asyncio.CancelledError
        if self._result is not None:
            return self._result
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content="durable external result",
            external_request_id="external-observed-1",
        )


class _ObservedToolBuilder(ToolBuilder[_ObservedToolParams]):
    parameters_schema = _ObservedToolParams
    name = "observed_plan_effect"
    description = "A deterministic durable test effect."
    recovery_strategy = RecoveryStrategy.IDEMPOTENT_RETRY
    idempotency_key_field = "idempotency_key"

    def __init__(
        self,
        effect_counts: dict[str, int],
        *,
        result: ToolExecutionResult | None = None,
        cancel_after_effect: bool = False,
    ) -> None:
        self._effect_counts = effect_counts
        self._result = result
        self._cancel_after_effect = cancel_after_effect

    def validate(self, params: dict[str, object]) -> _ObservedToolParams:
        return _ObservedToolParams.model_validate(params)

    def build(self, params: _ObservedToolParams) -> ToolInvocation[_ObservedToolParams]:
        return _ObservedToolInvocation(
            params,
            self._effect_counts,
            result=self._result,
            cancel_after_effect=self._cancel_after_effect,
        )


class _ApprovedPlanToolBuilder(_ObservedToolBuilder):
    name = "approved_plan_effect"
    recovery_strategy = RecoveryStrategy.MANUAL_UNCERTAIN
    idempotency_key_field = None

    def build(self, params: _ObservedToolParams) -> ToolInvocation[_ObservedToolParams]:
        invocation = super().build(params)
        invocation.name = self.name
        return invocation


class _ManualPlanToolBuilder(_ObservedToolBuilder):
    name = "manual_plan_effect"
    recovery_strategy = RecoveryStrategy.MANUAL_UNCERTAIN
    idempotency_key_field = None


class _ErroringApprovedPlanToolBuilder(_ObservedToolBuilder):
    name = "approved_plan_effect"


class _ObservedSummaryRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def completion(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["tools"] is None
        messages = kwargs["messages"]
        assert isinstance(messages, list) and len(messages) == 2
        assert messages[0]["role"] == "system"
        summary_input = json.loads(messages[1]["content"])
        assert set(summary_input) == {"objective", "step_summaries"}
        assert isinstance(summary_input["objective"], str)
        assert isinstance(summary_input["step_summaries"], list)
        assert len(summary_input["step_summaries"]) == 1
        assert set(summary_input["step_summaries"][0]) == {
            "logical_step_key",
            "summary",
        }
        return SimpleNamespace(content="Observed durable Plan summary.")


class _ObservedPlanAgent:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.registry = ToolRegistry()
        self.skill_manager = SimpleNamespace(active_skills=())
        self.router = _ObservedSummaryRouter()
        self.plan_calls: list[tuple[object, object]] = []
        self.generic_recovery_calls = 0

    async def run_plan_step(
        self,
        request,
        *,
        recovered_tool_result=None,
        recovered_tool_input_json=None,
        **_kwargs,
    ) -> PlanStepCompletion:
        self.plan_calls.append((recovered_tool_result, recovered_tool_input_json))
        assert recovered_tool_result is not None
        assert recovered_tool_input_json == '{"idempotency_key":"observed-key"}'
        return PlanStepCompletion(status="succeeded", summary="observed", evidence=[])

    async def resume_recovery(self, **_kwargs):
        self.generic_recovery_calls += 1
        raise AssertionError("Plan-bound observed results must not use generic recovery")


class _ObservedRuntimeLease:
    def __init__(self, runtime: _ObservedRuntime) -> None:
        self._runtime = runtime

    def close(self) -> None:
        self._runtime.closed_leases += 1


class _ObservedRuntime:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        scheduler: CoreToolScheduler,
        builder: _ObservedToolBuilder,
    ) -> None:
        self.runtime_instance_id = "observed-recovery-runtime"
        self.agent = _ObservedPlanAgent(database, settings)
        self.scheduler = scheduler
        self.registry = ToolRegistry()
        self.registry.register(builder)
        self.recovery_continuation = RuntimeRecoveryContinuationService()
        self.plan_execution = PlanExecutionCoordinator(database, settings=settings)
        self.begin_calls = 0
        self.closed_leases = 0

    def begin_run(self) -> _ObservedRuntimeLease:
        self.begin_calls += 1
        return _ObservedRuntimeLease(self)


class _ObservedRuntimePool:
    def __init__(self, runtime: _ObservedRuntime) -> None:
        self._runtime = runtime
        self.acquire_calls = 0

    async def acquire(self, _context: TenantContext) -> _ObservedRuntime:
        self.acquire_calls += 1
        return self._runtime


class _FinalSummaryAgent:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.plan_step_calls = 0

    async def run_plan_step(self, *_args, **_kwargs):
        self.plan_step_calls += 1
        raise AssertionError("final-summary recovery must not execute a Plan step")


class _FinalSummaryRuntime:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.agent = _FinalSummaryAgent(database, settings)
        self.plan_execution = PlanExecutionCoordinator(database, settings=settings)


class _BlockedPlanRuntimeLease:
    def __init__(self, runtime) -> None:
        self._runtime = runtime

    def close(self) -> None:
        self._runtime.closed_leases += 1


class _BlockedPlanAgent:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.generic_recovery_calls = 0

    async def resume_recovery(self, **_kwargs) -> None:
        self.generic_recovery_calls += 1
        raise AssertionError("corrupt Plan recovery must not invoke generic continuation")


class _BlockedPlanRuntime:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.runtime_instance_id = "blocked-plan-runtime"
        self.agent = _BlockedPlanAgent(database, settings)
        self.begin_calls = 0
        self.closed_leases = 0

    def begin_run(self) -> _BlockedPlanRuntimeLease:
        self.begin_calls += 1
        return _BlockedPlanRuntimeLease(self)


class _BlockedPlanRuntimePool:
    def __init__(self, runtime: _BlockedPlanRuntime) -> None:
        self._runtime = runtime
        self.acquire_calls = 0

    async def acquire(self, _context: TenantContext) -> _BlockedPlanRuntime:
        self.acquire_calls += 1
        return self._runtime


def _durable_scheduler(database: Database, settings: Settings) -> CoreToolScheduler:
    return CoreToolScheduler(
        permission_checker=PermissionChecker(),
        execution_guard=ExecutionGuard(timeout=1.0),
        audit_logger=InMemoryAuditLogger(),
        event_bus=EventBus(),
        database=database,
        settings=settings,
    )


def _approval_scheduler(database: Database, settings: Settings) -> CoreToolScheduler:
    return CoreToolScheduler(
        permission_checker=PermissionChecker(guarded_tools={"approved_plan_effect"}),
        execution_guard=ExecutionGuard(timeout=1.0),
        audit_logger=InMemoryAuditLogger(),
        event_bus=EventBus(),
        database=database,
        settings=settings,
    )


async def _observed_result_boundary(database: Database):
    settings, context, service, result = await _materialized(database)
    decision = await _approve(service, context, result)
    assert decision.lease is not None
    plan_execution = PlanExecutionCoordinator(database, settings=settings)
    started = await plan_execution.start_next_attempt(
        context=context,
        lease=decision.lease,
    )
    assert started is not None
    effects: dict[str, int] = {}
    builder = _ObservedToolBuilder(effects)
    scheduler = _durable_scheduler(database, settings)
    observed = await scheduler.run(
        builder,
        {"idempotency_key": "observed-key"},
        context=context,
        call_id="observed-call",
        run_lease_handle=RunLeaseHandle(decision.lease),
    )
    assert observed.status is ToolStatus.SUCCESS
    checkpoint = await service.workflow.get_latest_checkpoint(context)
    assert checkpoint is not None
    assert checkpoint.phase == "execution_result_observed"
    return settings, context, service, effects, builder, scheduler, checkpoint


async def _approved_plan_tool_boundary(
    database: Database,
    *,
    approval_status: ApprovalStatus = ApprovalStatus.APPROVED,
    tool_result: ToolExecutionResult | None = None,
    builder_type: type[_ObservedToolBuilder] = _ApprovedPlanToolBuilder,
):
    settings, context, service, materialized = await _materialized(database)
    decision = await _approve(service, context, materialized)
    assert decision.lease is not None
    running_lease = await service.workflow.transition_run(
        decision.lease,
        RunStatus.RUNNING,
    )
    plan_execution = PlanExecutionCoordinator(database, settings=settings)
    started = await plan_execution.start_next_attempt(
        context=context,
        lease=running_lease,
    )
    assert started is not None
    effects: dict[str, int] = {}
    builder = builder_type(effects, result=tool_result)
    scheduler = _approval_scheduler(database, settings)
    approval = await scheduler.run(
        builder,
        {"idempotency_key": "observed-key"},
        context=context,
        call_id="approved-call",
        run_lease_handle=RunLeaseHandle(running_lease),
    )
    assert approval.status is ToolStatus.AWAITING_APPROVAL
    approval_id = str(approval.data["approval_id"])
    if approval_status is ApprovalStatus.APPROVED:
        await service.workflow.decide_approval(
            context,
            approval_id,
            approved=True,
            version=1,
        )
    elif approval_status is ApprovalStatus.REJECTED:
        await service.workflow.decide_approval(
            context,
            approval_id,
            approved=False,
            version=1,
        )
    else:
        assert approval_status is ApprovalStatus.EXPIRED
        async with database.write_transaction() as conn:
            await conn.execute(
                update(approval_requests)
                .where(approval_requests.c.approval_id == approval_id)
                .values(expires_at=database.dialect.db_now_ms() - 1)
            )
        with pytest.raises(InvalidTransitionError, match="approval expired"):
            await service.workflow.decide_approval(
                context,
                approval_id,
                approved=True,
                version=1,
            )
    return settings, context, service, effects, builder, scheduler


async def _manual_uncertain_plan_boundary(database: Database):
    settings, context, service, result = await _materialized(database)
    decision = await _approve(service, context, result)
    assert decision.lease is not None
    running_lease = await service.workflow.transition_run(
        decision.lease,
        RunStatus.RUNNING,
    )
    plan_execution = PlanExecutionCoordinator(database, settings=settings)
    started = await plan_execution.start_next_attempt(
        context=context,
        lease=running_lease,
    )
    assert started is not None
    effects: dict[str, int] = {}
    builder = _ManualPlanToolBuilder(effects, cancel_after_effect=True)
    scheduler = _durable_scheduler(database, settings)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run(
            builder,
            {"idempotency_key": "observed-key"},
            context=context,
            call_id="manual-call",
            run_lease_handle=RunLeaseHandle(running_lease),
        )
    return settings, context, service, effects, builder, scheduler


async def _final_summary_boundary(
    database: Database,
    *,
    run_status: RunStatus,
):
    settings, context, service, result = await _materialized(database)
    decision = await _approve(service, context, result)
    assert decision.lease is not None
    execution = PlanExecutionCoordinator(database, settings=settings)
    started = await execution.start_next_attempt(context=context, lease=decision.lease)
    assert started is not None
    async with database.write_transaction() as conn:
        await conn.execute(
            update(agent_plan_step_runs)
            .where(agent_plan_step_runs.c.step_run_id == started.step_run.step_run_id)
            .values(
                status=PlanStepRunStatus.SUCCEEDED.value,
                finished_at=database.dialect.db_now_ms(),
            )
        )
    await service.workflow.checkpoint(
        decision.lease,
        CheckpointPhase.PLAN_STEP_READY,
        {
            "run_id": context.run_id,
            "plan_id": result.plan.plan_id,
            "plan_version": 1,
            "plan_digest": result.plan.current.content_digest,
            "step_id": started.step.step_id,
            "step_run_id": started.step_run.step_run_id,
            "attempt": started.step_run.attempt,
            "execution_cursor": "final_summary",
            "cursor": "final_summary",
        },
    )
    if run_status is RunStatus.AWAITING_USER:
        running_lease = await service.workflow.transition_run(
            decision.lease,
            RunStatus.RUNNING,
        )
        await service.workflow.transition_run(running_lease, RunStatus.AWAITING_USER)
    elif run_status is RunStatus.RUNNING:
        await service.workflow.transition_run(decision.lease, RunStatus.RUNNING)
    return settings, context, service


async def _execution_count(database: Database, context: TenantContext) -> int:
    async with database.connect() as conn:
        result = await conn.scalar(
            select(func.count())
            .select_from(tool_executions)
            .where(tool_executions.c.run_id == context.run_id)
        )
    return int(result or 0)


async def _step_attempt_count(database: Database, context: TenantContext) -> int:
    async with database.connect() as conn:
        result = await conn.scalar(
            select(func.count())
            .select_from(agent_plan_step_runs)
            .where(agent_plan_step_runs.c.run_id == context.run_id)
        )
    return int(result or 0)


async def _terminal_checkpoint_count(database: Database, context: TenantContext) -> int:
    async with database.connect() as conn:
        result = await conn.scalar(
            select(func.count())
            .select_from(execution_checkpoints)
            .where(
                execution_checkpoints.c.run_id == context.run_id,
                execution_checkpoints.c.phase == CheckpointPhase.RUN_TERMINAL.value,
            )
        )
    return int(result or 0)


async def _final_summary_count(database: Database, context: TenantContext) -> int:
    async with TenantUnitOfWork(database, context) as uow:
        messages = await uow.memory.recent(limit=100, entry_type="chat_message")
    return sum(
        message.role == "assistant"
        and message.metadata.get("kind") == "plan_final_summary"
        and message.metadata.get("run_id") == context.run_id
        for message in messages
    )


async def _execution_status(database: Database, context: TenantContext) -> ExecutionStatus:
    async with database.connect() as conn:
        status = await conn.scalar(
            select(tool_executions.c.execution_status).where(
                tool_executions.c.run_id == context.run_id
            )
        )
    assert status is not None
    return ExecutionStatus(status)


async def _assert_terminal_plan_recovery(
    *,
    database: Database,
    context: TenantContext,
    service: PlanningService,
    worker: WorkflowRecoveryWorker,
    runtime: _ObservedRuntime,
    pool: _ObservedRuntimePool,
    effects: dict[str, int],
    expected_effects: dict[str, int],
    expected_execution_status: ExecutionStatus,
    expected_result_content: str,
) -> None:
    await worker.run_once()

    run = await service.workflow.get_run(context)
    assert run is not None
    assert run.status is RunStatus.COMPLETED
    assert effects == expected_effects
    assert await _execution_status(database, context) is expected_execution_status
    assert runtime.agent.generic_recovery_calls == 0
    assert len(runtime.agent.plan_calls) == 1
    assert len(runtime.agent.router.calls) == 1
    assert await _final_summary_count(database, context) == 1
    recovered_result, _ = runtime.agent.plan_calls[0]
    assert recovered_result is not None
    assert recovered_result.content == expected_result_content
    assert runtime.begin_calls == runtime.closed_leases == pool.acquire_calls == 1
    async with TenantUnitOfWork(database, context) as uow:
        assert await uow.plans.running_step_attempts(run_id=str(context.run_id)) == ()

    await worker.run_once()

    assert effects == expected_effects
    assert len(runtime.agent.plan_calls) == 1
    assert len(runtime.agent.router.calls) == 1
    assert await _final_summary_count(database, context) == 1
    assert runtime.begin_calls == runtime.closed_leases == pool.acquire_calls == 1


@pytest.mark.asyncio
async def test_real_observed_tool_result_resumes_plan_without_redispatch(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        settings, context, _service, effects, builder, scheduler, _ = await _observed_result_boundary(database)
        runtime = _ObservedRuntime(
            database=database,
            settings=settings,
            scheduler=scheduler,
            builder=builder,
        )
        await _expire(database, context)

        await WorkflowRecoveryWorker(
            database=database,
            settings=settings,
            runtime_pool=_ObservedRuntimePool(runtime),
        ).run_once()

        assert effects == {"observed-key": 1}
        assert runtime.agent.generic_recovery_calls == 0
        assert len(runtime.agent.plan_calls) == 1
        recovered_result, recovered_input = runtime.agent.plan_calls[0]
        assert recovered_result is not None
        assert recovered_result.result_ref.startswith("memory://")
        assert recovered_result.content == "durable external result"
        assert recovered_input == '{"idempotency_key":"observed-key"}'
        assert await _execution_count(database, context) == 1
        assert await _step_attempt_count(database, context) == 1
        async with TenantUnitOfWork(database, context) as uow:
            attempts = await uow.plans.running_step_attempts(run_id=str(context.run_id))
        assert attempts == ()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_approved_plan_tool_resolution_resumes_plan_without_generic_recovery(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        settings, context, service, effects, builder, scheduler = await _approved_plan_tool_boundary(database)
        runtime = _ObservedRuntime(
            database=database,
            settings=settings,
            scheduler=scheduler,
            builder=builder,
        )
        worker = WorkflowRecoveryWorker(
            database=database,
            settings=settings,
            runtime_pool=_ObservedRuntimePool(runtime),
        )

        await worker.run_once()

        run = await service.workflow.get_run(context)
        assert run is not None
        assert effects == {"observed-key": 1}
        assert runtime.agent.generic_recovery_calls == 0
        assert len(runtime.agent.plan_calls) == 1
        assert len(runtime.agent.router.calls) == 1
        assert run.status is RunStatus.COMPLETED
        assert await _final_summary_count(database, context) == 1
        assert await _execution_count(database, context) == 1
        assert await _execution_status(database, context) is ExecutionStatus.SUCCEEDED
        assert await _step_attempt_count(database, context) == 1
        recovered_result, _ = runtime.agent.plan_calls[0]
        assert recovered_result is not None
        assert recovered_result.content == "durable external result"
        async with TenantUnitOfWork(database, context) as uow:
            attempts = await uow.plans.running_step_attempts(run_id=str(context.run_id))
        assert attempts == ()

        await worker.run_once()

        assert effects == {"observed-key": 1}
        assert len(runtime.agent.plan_calls) == 1
        assert len(runtime.agent.router.calls) == 1
        assert await _final_summary_count(database, context) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_status", "expected_result_content"),
    (
        (ApprovalStatus.REJECTED, "rejected by user"),
        (ApprovalStatus.EXPIRED, "approval expired"),
    ),
)
async def test_terminal_plan_approval_resolution_resumes_plan_without_generic_recovery(
    tmp_path: Path,
    approval_status: ApprovalStatus,
    expected_result_content: str,
):
    database = await _database(tmp_path)
    try:
        settings, context, service, effects, builder, scheduler = await _approved_plan_tool_boundary(
            database,
            approval_status=approval_status,
        )
        runtime = _ObservedRuntime(
            database=database,
            settings=settings,
            scheduler=scheduler,
            builder=builder,
        )
        pool = _ObservedRuntimePool(runtime)
        worker = WorkflowRecoveryWorker(
            database=database,
            settings=settings,
            runtime_pool=pool,
        )

        await _assert_terminal_plan_recovery(
            database=database,
            context=context,
            service=service,
            worker=worker,
            runtime=runtime,
            pool=pool,
            effects=effects,
            expected_effects={},
            expected_execution_status=ExecutionStatus.BLOCKED_INCOMPATIBLE,
            expected_result_content=expected_result_content,
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_erroring_approved_plan_tool_resumes_plan_without_generic_recovery(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        settings, context, service, effects, builder, scheduler = await _approved_plan_tool_boundary(
            database,
            tool_result=ToolExecutionResult(status=ToolStatus.ERROR, content="approved tool failed"),
            builder_type=_ErroringApprovedPlanToolBuilder,
        )
        runtime = _ObservedRuntime(
            database=database,
            settings=settings,
            scheduler=scheduler,
            builder=builder,
        )
        pool = _ObservedRuntimePool(runtime)
        worker = WorkflowRecoveryWorker(
            database=database,
            settings=settings,
            runtime_pool=pool,
        )

        await _assert_terminal_plan_recovery(
            database=database,
            context=context,
            service=service,
            worker=worker,
            runtime=runtime,
            pool=pool,
            effects=effects,
            expected_effects={"observed-key": 1},
            expected_execution_status=ExecutionStatus.FAILED_TERMINAL,
            expected_result_content="approved tool failed",
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_manual_uncertain_plan_recovery_resumes_plan_without_redispatch(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        settings, context, service, effects, builder, scheduler = await _manual_uncertain_plan_boundary(
            database
        )
        runtime = _ObservedRuntime(
            database=database,
            settings=settings,
            scheduler=scheduler,
            builder=builder,
        )
        pool = _ObservedRuntimePool(runtime)
        await _expire(database, context)
        worker = WorkflowRecoveryWorker(
            database=database,
            settings=settings,
            runtime_pool=pool,
        )

        await _assert_terminal_plan_recovery(
            database=database,
            context=context,
            service=service,
            worker=worker,
            runtime=runtime,
            pool=pool,
            effects=effects,
            expected_effects={"observed-key": 1},
            expected_execution_status=ExecutionStatus.UNCERTAIN,
            expected_result_content="tool execution failed",
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_missing_observed_tool_result_blocks_plan_before_redispatch(tmp_path: Path):
    database = await _database(tmp_path)
    try:
        settings, context, service, effects, builder, scheduler, checkpoint = await _observed_result_boundary(database)
        payload = json.loads(checkpoint.payload_json)
        payload["result_ref"] = "memory://missing-observed-result"
        await _replace_checkpoint_payload(database, checkpoint.checkpoint_id, payload)
        async with database.write_transaction() as conn:
            await conn.execute(
                update(tool_executions)
                .where(tool_executions.c.run_id == context.run_id)
                .values(result_ref="memory://missing-observed-result")
            )
        runtime = _ObservedRuntime(
            database=database,
            settings=settings,
            scheduler=scheduler,
            builder=builder,
        )
        await _expire(database, context)

        await WorkflowRecoveryWorker(
            database=database,
            settings=settings,
            runtime_pool=_ObservedRuntimePool(runtime),
        ).run_once()

        run = await service.workflow.get_run(context)
        assert run is not None
        assert run.status is RunStatus.BLOCKED_CORRUPT
        assert effects == {"observed-key": 1}
        assert runtime.agent.plan_calls == []
        assert runtime.agent.generic_recovery_calls == 0
        assert await _execution_count(database, context) == 1
        assert await _step_attempt_count(database, context) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("input_payload_json", "input_hash"))
async def test_tampered_observed_tool_input_blocks_plan_before_continuation(
    tmp_path: Path,
    field: str,
):
    database = await _database(tmp_path)
    try:
        settings, context, service, effects, builder, scheduler, _ = await _observed_result_boundary(database)
        tampered_value = (
            '{"idempotency_key":"tampered-key"}'
            if field == "input_payload_json"
            else "f" * 64
        )
        async with database.write_transaction() as conn:
            await conn.execute(
                update(tool_executions)
                .where(tool_executions.c.run_id == context.run_id)
                .values({field: tampered_value})
            )
        runtime = _ObservedRuntime(
            database=database,
            settings=settings,
            scheduler=scheduler,
            builder=builder,
        )
        await _expire(database, context)

        await WorkflowRecoveryWorker(
            database=database,
            settings=settings,
            runtime_pool=_ObservedRuntimePool(runtime),
        ).run_once()

        run = await service.workflow.get_run(context)
        assert run is not None
        assert run.status is RunStatus.BLOCKED_CORRUPT
        assert effects == {"observed-key": 1}
        assert runtime.agent.plan_calls == []
        assert runtime.agent.generic_recovery_calls == 0
        assert await _execution_count(database, context) == 1
        assert await _step_attempt_count(database, context) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_final_summary_checkpoint_awaits_user_without_recovery_lease(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        _settings, context, service = await _final_summary_boundary(
            database,
            run_status=RunStatus.AWAITING_USER,
        )

        outcome = await RecoveryService(database).recover(context, "runtime-b")

        run = await service.workflow.get_run(context)
        assert run is not None
        assert outcome.action is RecoveryAction.AWAIT_USER
        assert outcome.lease is None
        assert run.status is RunStatus.AWAITING_USER
        assert await _execution_count(database, context) == 0
        assert await _step_attempt_count(database, context) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", (RunStatus.RUNNING, RunStatus.RESUMING))
async def test_final_summary_checkpoint_waits_without_step_or_tool_continuation(
    tmp_path: Path,
    run_status: RunStatus,
):
    database = await _database(tmp_path)
    try:
        settings, context, service = await _final_summary_boundary(
            database,
            run_status=run_status,
        )
        runtime = _FinalSummaryRuntime(database, settings)
        await _expire(database, context)

        outcome = await RecoveryService(database).recover(context, "runtime-b")

        assert outcome.action is RecoveryAction.RESUME_PLAN_STEP
        assert outcome.lease is not None
        await RuntimeRecoveryContinuationService().resume(
            runtime=runtime,
            context=context,
            run_lease_handle=RunLeaseHandle(outcome.lease),
            recovery_outcome=outcome,
        )

        run = await service.workflow.get_run(context)
        assert run is not None
        assert run.status is RunStatus.AWAITING_USER
        assert runtime.agent.plan_step_calls == 0
        assert await _execution_count(database, context) == 0
        assert await _step_attempt_count(database, context) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_replan_checkpoint_with_committed_next_version_awaits_decision(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        settings, context, service, result = await _materialized(database)
        decision = await _approve(service, context, result)
        assert decision.lease is not None
        execution = PlanExecutionCoordinator(database, settings=settings)
        started = await execution.start_next_attempt(context=context, lease=decision.lease)
        assert started is not None
        async with database.write_transaction() as conn:
            await conn.execute(
                update(agent_plan_step_runs)
                .where(agent_plan_step_runs.c.step_run_id == started.step_run.step_run_id)
                .values(status=PlanStepRunStatus.FAILED_TERMINAL.value)
            )
        await service.workflow.checkpoint(
            decision.lease,
            CheckpointPhase.PLAN_REPLAN_REQUIRED,
            {
                "run_id": context.run_id,
                "plan_id": result.plan.plan_id,
                "plan_version": 1,
                "plan_digest": result.plan.current.content_digest,
                "failed_step_run_id": started.step_run.step_run_id,
                "failure_digest": "a" * 64,
                "revision_cursor": "generate_revision",
                "cursor": "generate_revision",
            },
        )
        async with TenantUnitOfWork(
            database,
            context,
            planning_settings=settings.planning,
            workflow_settings=settings.workflow,
        ) as uow:
            revised = await uow.plans.append_version(
                plan_id=result.plan.plan_id,
                expected_version=decision.snapshot.aggregate_version,
                draft=PlanDraft(
                    objective="Recover exactly once",
                    constraints=[],
                    generation_reason="durable replay",
                    steps=[
                        PlanDraftStep(
                            logical_step_key="execute",
                            title="Execute again",
                            description="Revised step.",
                            expected_outcome="one durable result",
                            max_attempts=1,
                            depends_on=[],
                        )
                    ],
                ),
                parent_version=1,
                revision_feedback=f"failure:{started.step_run.step_run_id}",
                supersedes={"execute": started.step.step_id},
            )
        running_lease = await service.workflow.transition_run(
            decision.lease, RunStatus.RUNNING
        )
        await service.workflow.transition_run(running_lease, RunStatus.AWAITING_USER)
        await _expire(database, context)

        outcome = await RecoveryService(database).recover(context, "runtime-b")

        assert revised.current_version == 2
        assert outcome.action is RecoveryAction.AWAIT_PLAN_DECISION
        assert outcome.lease is None
        assert await _execution_count(database, context) == 0
        async with TenantUnitOfWork(database, context) as uow:
            persisted = await uow.plans.get(result.plan.plan_id)
        assert persisted is not None
        assert persisted.current_version == 2
        assert len(persisted.versions) == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_replan_checkpoint_rejects_next_version_without_prior_approval_binding(
    tmp_path: Path,
):
    database = await _database(tmp_path)
    try:
        settings, context, service, result = await _materialized(database)
        decision = await _approve(service, context, result)
        assert decision.lease is not None
        execution = PlanExecutionCoordinator(database, settings=settings)
        started = await execution.start_next_attempt(context=context, lease=decision.lease)
        assert started is not None
        async with database.write_transaction() as conn:
            await conn.execute(
                update(agent_plan_step_runs)
                .where(agent_plan_step_runs.c.step_run_id == started.step_run.step_run_id)
                .values(status=PlanStepRunStatus.FAILED_TERMINAL.value)
            )
        await service.workflow.checkpoint(
            decision.lease,
            CheckpointPhase.PLAN_REPLAN_REQUIRED,
            {
                "run_id": context.run_id,
                "plan_id": result.plan.plan_id,
                "plan_version": 1,
                "plan_digest": result.plan.current.content_digest,
                "failed_step_run_id": started.step_run.step_run_id,
                "failure_digest": "a" * 64,
                "revision_cursor": "generate_revision",
                "cursor": "generate_revision",
            },
        )
        async with TenantUnitOfWork(
            database,
            context,
            planning_settings=settings.planning,
            workflow_settings=settings.workflow,
        ) as uow:
            await uow.plans.append_version(
                plan_id=result.plan.plan_id,
                expected_version=decision.snapshot.aggregate_version,
                draft=PlanDraft(
                    objective="Recover exactly once",
                    constraints=[],
                    generation_reason="durable replay",
                    steps=[
                        PlanDraftStep(
                            logical_step_key="execute",
                            title="Execute again",
                            description="Revised step.",
                            expected_outcome="one durable result",
                            max_attempts=1,
                            depends_on=[],
                        )
                    ],
                ),
                parent_version=1,
                revision_feedback=f"failure:{started.step_run.step_run_id}",
                supersedes={"execute": started.step.step_id},
            )
            await uow.conn.execute(
                update(agent_plans)
                .where(agent_plans.c.id == result.plan.plan_id)
                .values(approved_version=None)
            )
        running_lease = await service.workflow.transition_run(
            decision.lease, RunStatus.RUNNING
        )
        await service.workflow.transition_run(running_lease, RunStatus.AWAITING_USER)
        await _expire(database, context)

        outcome = await RecoveryService(database).recover(context, "runtime-b")

        assert outcome.status is RunStatus.BLOCKED_CORRUPT
        assert outcome.lease is None
        assert await _execution_count(database, context) == 0
    finally:
        await database.dispose()
