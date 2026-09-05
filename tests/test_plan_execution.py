from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, text, update

from alembic import command
from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.memory import MemoryEntry
from multiclaw.planner.execution import PlanExecutionCoordinator, choose_ready_step
from multiclaw.planner.models import (
    MaterializeInitialPlan,
    PlanAttemptLimitError,
    PlanDecisionAction,
    PlanDecisionRequest,
    PlanDraft,
    PlanDraftStep,
    PlanExecutionBlocked,
    PlanStepAlreadyRunningError,
    PlanStepResultDocument,
    PlanStepRunRecord,
    PlanStepRunStatus,
    PlanTriggerMode,
)
from multiclaw.planner.service import PlanningService
from multiclaw.storage import Database
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.schema import (
    agent_plan_step_runs,
    agent_plans,
    execution_checkpoints,
    users,
)
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import CheckpointPhase, RunLease, StaleFenceError


_MYSQL_URL = os.getenv("MULTICLAW_TEST_MYSQL_URL")


def _settings(*, max_step_attempts: int = 2) -> Settings:
    return Settings(
        _config_file="/nonexistent",
        planning={"max_step_attempts": max_step_attempts},
    )


def _draft(
    keys: tuple[str, ...] = ("lint", "test"),
    *,
    chain: bool = False,
    max_attempts: int = 2,
) -> PlanDraft:
    return PlanDraft(
        objective="Deliver the durable Plan execution boundary",
        constraints=["Keep execution serial"],
        generation_reason="The approved Plan is ready to execute.",
        steps=[
            PlanDraftStep(
                logical_step_key=key,
                title=key.title(),
                description=f"Execute {key}.",
                expected_outcome=f"{key.title()} succeeds.",
                depends_on=[keys[index - 1]] if chain and index else [],
                max_attempts=max_attempts,
            )
            for index, key in enumerate(keys)
        ],
    )


def _lease_from_run(run) -> RunLease:
    assert run.context.run_id is not None
    assert run.lease_owner is not None
    assert run.lease_expires_at is not None
    return RunLease(
        context=run.context,
        lease_owner=run.lease_owner,
        fencing_token=run.fencing_token,
        version=run.version,
        lease_expires_at=run.lease_expires_at,
    )


async def _upgrade_database(database_url: str) -> None:
    await asyncio.to_thread(
        command.upgrade,
        alembic_config(database_url=database_url),
        "head",
    )


async def _seed_root_context(database: Database, slug: str) -> TenantContext:
    tenant_id = str(uuid4())
    workspace_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            text(
            """
            INSERT INTO users (
                id, email, auth_epoch, default_workspace_id, status,
                purge_after, created_at, updated_at, disabled_at, purge_requested_at
            ) VALUES (:tenant_id, :email, 0, NULL, 'active', NULL, 1, 1, NULL, NULL)
            """
            ),
            {"tenant_id": tenant_id, "email": f"{slug}-{tenant_id}@example.com"},
        )
        await conn.execute(
            text(
            """
            INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
            VALUES (:workspace_id, :tenant_id, :slug, :name, 'active', 1, 1)
            """
            ),
            {
                "workspace_id": workspace_id,
                "tenant_id": tenant_id,
                "slug": f"{slug}-{tenant_id}",
                "name": slug.title(),
            },
        )
        await conn.execute(
            update(users)
            .where(users.c.id == tenant_id)
            .values(default_workspace_id=workspace_id)
        )
    return TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)


@pytest.fixture
async def execution_database(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'plan-execution.db'}"
    await _upgrade_database(database_url)
    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        yield database
    finally:
        await database.dispose()


@dataclass(slots=True)
class ExecutionFixture:
    database: Database
    settings: Settings
    root_context: TenantContext
    source_message_id: str
    coordinator: PlanExecutionCoordinator
    service: PlanningService
    context: TenantContext | None = None
    plan_id: str | None = None
    aggregate_version: int | None = None
    current_lease: RunLease | None = None

    @property
    def lease(self) -> RunLease:
        assert self.current_lease is not None
        return self.current_lease

    def new_coordinator(self) -> PlanExecutionCoordinator:
        return PlanExecutionCoordinator(self.database, settings=self.settings)

    async def materialize(self, draft: PlanDraft) -> None:
        assert self.root_context.session_id is not None
        context = self.root_context.for_run(self.root_context.session_id, str(uuid4()))
        materialized = await self.service.materialize_initial(
            MaterializeInitialPlan(
                context=context,
                runtime_instance_id="runtime-a",
                source_message_id=self.source_message_id,
                assistant_turn_index=2,
                trigger_mode=PlanTriggerMode.EXPLICIT,
                draft=draft,
            )
        )
        self.context = context
        self.plan_id = materialized.plan.plan_id
        self.aggregate_version = materialized.plan.aggregate_version
        self.current_lease = _lease_from_run(materialized.run)

    async def approve(self) -> None:
        if self.plan_id is None:
            await self.materialize(_draft())
        assert self.plan_id is not None
        assert self.aggregate_version is not None
        result = await self.service.decide(
            PlanDecisionRequest(
                decision_id=str(uuid4()),
                plan_id=self.plan_id,
                plan_version=1,
                expected_version=self.aggregate_version,
                action=PlanDecisionAction.APPROVE,
            ),
            decided_by=self.root_context.tenant_id,
            runtime_instance_id="runtime-a",
        )
        assert result.lease is not None
        self.current_lease = result.lease
        self.aggregate_version = result.snapshot.aggregate_version

    async def approve_with_two_roots(self) -> None:
        await self.materialize(_draft())
        await self.approve()

    async def approve_chain(self, keys: list[str]) -> None:
        await self.materialize(_draft(tuple(keys), chain=True))
        await self.approve()

    async def prepare_version_mutation(self, mutation: str) -> None:
        await self.materialize(_draft())
        if mutation == "unapproved":
            return
        await self.approve()
        if mutation in {"current_ahead", "active_behind"}:
            assert self.context is not None
            assert self.plan_id is not None
            assert self.aggregate_version is not None
            async with TenantUnitOfWork(
                self.database,
                self.context,
                planning_settings=self.settings.planning,
            ) as uow:
                revised = await uow.plans.for_context(self.context).append_version(
                    plan_id=self.plan_id,
                    expected_version=self.aggregate_version,
                    draft=_draft(("lint", "test", "package")),
                    parent_version=1,
                    revision_feedback="Exercise the version gate.",
                    supersedes={},
                )
                self.aggregate_version = revised.aggregate_version
                if mutation == "active_behind":
                    await uow.conn.execute(
                        update(agent_plans)
                        .where(
                            agent_plans.c.tenant_id == self.context.tenant_id,
                            agent_plans.c.workspace_id == self.context.workspace_id,
                            agent_plans.c.session_id == self.context.session_id,
                            agent_plans.c.id == self.plan_id,
                        )
                        .values(status="approved", approved_version=2)
                    )

    async def finish(
        self,
        step_run: PlanStepRunRecord,
        status: PlanStepRunStatus,
        *,
        digest: str = "a" * 64,
    ) -> None:
        assert self.context is not None
        result_ref = None
        result_digest = None
        result_summary = None
        if status is PlanStepRunStatus.SUCCEEDED:
            async with TenantUnitOfWork(self.database, self.context) as uow:
                snapshot = await uow.plans.for_context(self.context).get(step_run.plan_id)
                assert snapshot is not None
                step = next(
                    item
                    for item in snapshot.current.steps
                    if item.step_id == step_run.step_id
                )
                document = PlanStepResultDocument(
                    plan_id=step_run.plan_id,
                    plan_version=step_run.plan_version,
                    run_id=step_run.run_id,
                    step_id=step_run.step_id,
                    step_run_id=step_run.step_run_id,
                    attempt=step_run.attempt,
                    status="succeeded",
                    summary=f"{step.logical_step_key} complete",
                    evidence=["durable evidence"],
                    definition_digest=step.definition_digest,
                    dependency_result_digests={},
                    tool_catalog_digest=digest,
                    policy_digest="b" * 64,
                    skill_set_digest="c" * 64,
                )
                entry = await uow.memory.save(
                    MemoryEntry(
                        content=json.dumps(
                            document.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        type="plan_step_result",
                        role="assistant",
                        session_id=self.context.session_id,
                        metadata={
                            "schema_version": 1,
                            "plan_id": step_run.plan_id,
                            "step_run_id": step_run.step_run_id,
                        },
                    )
                )
                result_ref = f"memory:{entry.id}"
                result_digest = document.digest()
                result_summary = document.summary
        async with self.database.write_transaction() as conn:
            await conn.execute(
                update(agent_plan_step_runs)
                .where(
                    agent_plan_step_runs.c.tenant_id == self.context.tenant_id,
                    agent_plan_step_runs.c.workspace_id == self.context.workspace_id,
                    agent_plan_step_runs.c.session_id == self.context.session_id,
                    agent_plan_step_runs.c.run_id == step_run.run_id,
                    agent_plan_step_runs.c.step_run_id == step_run.step_run_id,
                )
                .values(
                    status=status.value,
                    result_summary=result_summary,
                    result_ref=result_ref,
                    result_digest=result_digest,
                    error_code=(
                        "terminal" if status is PlanStepRunStatus.FAILED_TERMINAL else None
                    ),
                    error_detail_redacted=(
                        "dependency failed"
                        if status is PlanStepRunStatus.FAILED_TERMINAL
                        else None
                    ),
                    version=step_run.version + 1,
                    finished_at=step_run.started_at + 1,
                )
            )

    async def succeed(self, step_run: PlanStepRunRecord, digest: str) -> None:
        await self.finish(step_run, PlanStepRunStatus.SUCCEEDED, digest=digest)

    async def approve_and_succeed_all(self) -> None:
        await self.approve_with_two_roots()
        while True:
            started = await self.coordinator.start_next_attempt(
                context=self._context(),
                lease=self.lease,
            )
            if started is None:
                return
            await self.succeed(started.step_run, digest="d" * 64)

    async def capture_then_rotate_lease(self) -> RunLease:
        await self.approve_with_two_roots()
        stale = self.lease
        self.current_lease = await self.service.workflow.heartbeat(stale)
        return stale

    async def exhaust_current_step_attempts(self) -> None:
        assert self.context is not None
        assert self.plan_id is not None
        async with TenantUnitOfWork(self.database, self.context) as uow:
            snapshot = await uow.plans.for_context(self.context).get(self.plan_id)
        assert snapshot is not None
        step = snapshot.current.steps[0]
        async with self.database.write_transaction() as conn:
            for attempt in range(1, step.max_attempts + 1):
                await conn.execute(
                    insert(agent_plan_step_runs).values(
                        tenant_id=self.context.tenant_id,
                        workspace_id=self.context.workspace_id,
                        session_id=self.context.session_id,
                        plan_id=self.plan_id,
                        plan_version=snapshot.current_version,
                        step_id=step.step_id,
                        step_run_id=str(uuid4()),
                        run_id=self.context.run_id,
                        attempt=attempt,
                        status=PlanStepRunStatus.FAILED_RETRYABLE.value,
                        result_summary=None,
                        result_ref=None,
                        result_digest=None,
                        error_code="retry",
                        error_detail_redacted="try again",
                        reused_from_step_run_id=None,
                        version=1,
                        started_at=attempt,
                        finished_at=attempt,
                    )
                )

    async def count_step_runs(self) -> int:
        return await self.count_status(None)

    async def count_status(self, status: PlanStepRunStatus | None) -> int:
        assert self.context is not None
        statement = (
            select(func.count())
            .select_from(agent_plan_step_runs)
            .where(
                agent_plan_step_runs.c.tenant_id == self.context.tenant_id,
                agent_plan_step_runs.c.workspace_id == self.context.workspace_id,
                agent_plan_step_runs.c.session_id == self.context.session_id,
                agent_plan_step_runs.c.run_id == self.context.run_id,
            )
        )
        if status is not None:
            statement = statement.where(agent_plan_step_runs.c.status == status.value)
        async with self.database.connect() as conn:
            return int((await conn.execute(statement)).scalar_one())

    def _context(self) -> TenantContext:
        assert self.context is not None
        return self.context


@pytest.fixture
async def execution_fixture(execution_database: Database) -> ExecutionFixture:
    settings = _settings()
    root = await _seed_root_context(execution_database, "execution")
    async with TenantUnitOfWork(execution_database, root) as uow:
        session = await uow.sessions.create("Source")
        source_context = root.for_session(session.id)
        assert uow.conn is not None
        source = await MemoryRepository(
            uow.conn,
            source_context,
            execution_database.dialect,
        ).save(
            MemoryEntry(
                content="Deliver the Plan",
                type="chat_message",
                role="user",
                turn_index=1,
            )
        )
    workflow = WorkflowCoordinator(execution_database, settings=settings)
    return ExecutionFixture(
        database=execution_database,
        settings=settings,
        root_context=source_context,
        source_message_id=source.id,
        coordinator=PlanExecutionCoordinator(execution_database, settings=settings),
        service=PlanningService(
            execution_database,
            settings=settings,
            workflow=workflow,
        ),
    )


def test_selector_uses_persisted_ordinal_then_step_id_as_tie_breaker():
    plan = _draft()
    # This direct pure-function test is supplied with records in reverse ID order;
    # ordinal remains authoritative and step_id only breaks corrupt/equal ordinals.
    from multiclaw.planner.validation import validate_plan_draft
    from multiclaw.planner.models import PlanStepRecord, PlanVersionRecord

    validated = validate_plan_draft(plan, max_steps=20, max_depth=10, max_attempts=20)
    steps = tuple(
        PlanStepRecord(
            step_id=step_id,
            logical_step_key=step.logical_step_key,
            supersedes_step_id=None,
            ordinal=step.ordinal,
            title=step.title,
            description=step.description,
            expected_outcome=step.expected_outcome,
            assigned_agent_profile_id=None,
            max_attempts=step.max_attempts,
            definition_digest="a" * 64,
        )
        for step, step_id in zip(validated.steps, ("z" * 36, "a" * 36), strict=True)
    )
    version = PlanVersionRecord(
        plan_id=str(uuid4()),
        plan_version=1,
        objective=validated.objective,
        constraints=validated.constraints,
        generation_reason=validated.generation_reason,
        parent_version=None,
        revision_feedback=None,
        schema_version=1,
        content_digest="b" * 64,
        created_at=1,
        steps=tuple(reversed(steps)),
        dependencies={},
    )

    assert choose_ready_step(version, {}) is steps[0]


@pytest.mark.asyncio
async def test_selector_uses_stable_topological_ordinal(execution_fixture):
    await execution_fixture.approve()
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    assert first.step.logical_step_key == "lint"

    await execution_fixture.succeed(first.step_run, digest="a" * 64)
    second = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert second is not None
    assert second.step.logical_step_key == "test"


@pytest.mark.parametrize("mutation", ("unapproved", "current_ahead", "active_behind"))
@pytest.mark.asyncio
async def test_unapproved_or_stale_active_version_dispatches_no_step(
    execution_fixture,
    mutation,
):
    await execution_fixture.prepare_version_mutation(mutation)
    with pytest.raises(PlanExecutionBlocked, match="approved current active version"):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )
    assert await execution_fixture.count_step_runs() == 0


@pytest.mark.asyncio
async def test_concurrent_ready_nodes_still_create_one_running_attempt(execution_fixture):
    await execution_fixture.approve_with_two_roots()

    async def start():
        try:
            return await execution_fixture.new_coordinator().start_next_attempt(
                context=execution_fixture._context(),
                lease=execution_fixture.lease,
            )
        except (StaleFenceError, PlanStepAlreadyRunningError):
            return None

    outcomes = await asyncio.gather(start(), start())
    started = [item for item in outcomes if item is not None]
    assert len(started) == 1
    assert await execution_fixture.count_status(PlanStepRunStatus.RUNNING) == 1


@pytest.mark.asyncio
async def test_dependent_step_is_not_ready_until_dependency_succeeds(execution_fixture):
    await execution_fixture.approve_chain(["inspect", "verify"])
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    assert first.step.logical_step_key == "inspect"

    await execution_fixture.finish(first.step_run, PlanStepRunStatus.FAILED_TERMINAL)
    with pytest.raises(PlanExecutionBlocked, match="failed dependency"):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )


@pytest.mark.asyncio
async def test_selector_returns_only_succeeded_dependency_documents(execution_fixture):
    await execution_fixture.approve_chain(["inspect", "verify"])
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    await execution_fixture.succeed(first.step_run, digest="a" * 64)

    ready = await execution_fixture.coordinator.select_next(
        context=execution_fixture._context(),
    )
    assert ready is not None
    assert ready.step.logical_step_key == "verify"
    assert [item.step_id for item in ready.dependency_results] == [first.step.step_id]
    assert all(item.status == "succeeded" for item in ready.dependency_results)


@pytest.mark.asyncio
async def test_all_succeeded_returns_no_next_step(execution_fixture):
    await execution_fixture.approve_and_succeed_all()
    assert (
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )
        is None
    )


@pytest.mark.asyncio
async def test_stale_lease_and_exhausted_attempt_budget_create_no_attempt(execution_fixture):
    stale = await execution_fixture.capture_then_rotate_lease()
    with pytest.raises(StaleFenceError):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=stale,
        )

    await execution_fixture.exhaust_current_step_attempts()
    before = await execution_fixture.count_step_runs()
    with pytest.raises(PlanAttemptLimitError):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )
    assert await execution_fixture.count_step_runs() == before


@pytest.mark.asyncio
async def test_attempt_and_plan_step_ready_checkpoint_commit_together(execution_fixture):
    await execution_fixture.approve()
    before_checkpoints = 0
    async with execution_fixture.database.connect() as conn:
        before_checkpoints = int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(execution_checkpoints)
                    .where(
                        execution_checkpoints.c.tenant_id
                        == execution_fixture._context().tenant_id,
                        execution_checkpoints.c.workspace_id
                        == execution_fixture._context().workspace_id,
                        execution_checkpoints.c.session_id
                        == execution_fixture._context().session_id,
                        execution_checkpoints.c.run_id
                        == execution_fixture._context().run_id,
                    )
                )
            ).scalar_one()
        )

    started = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert started is not None

    async with execution_fixture.database.connect() as conn:
        checkpoint = (
            await conn.execute(
                select(execution_checkpoints)
                .where(
                    execution_checkpoints.c.tenant_id
                    == execution_fixture._context().tenant_id,
                    execution_checkpoints.c.workspace_id
                    == execution_fixture._context().workspace_id,
                    execution_checkpoints.c.session_id
                    == execution_fixture._context().session_id,
                    execution_checkpoints.c.run_id == execution_fixture._context().run_id,
                )
                .order_by(execution_checkpoints.c.checkpoint_seq.desc())
                .limit(1)
            )
        ).mappings().one()
        count = int(
            (
                await conn.execute(
                    select(func.count())
                    .select_from(execution_checkpoints)
                    .where(
                        execution_checkpoints.c.tenant_id
                        == execution_fixture._context().tenant_id,
                        execution_checkpoints.c.workspace_id
                        == execution_fixture._context().workspace_id,
                        execution_checkpoints.c.session_id
                        == execution_fixture._context().session_id,
                        execution_checkpoints.c.run_id
                        == execution_fixture._context().run_id,
                    )
                )
            ).scalar_one()
        )
    payload = json.loads(str(checkpoint["payload_json"]))
    assert count == before_checkpoints + 1
    assert checkpoint["phase"] == CheckpointPhase.PLAN_STEP_READY.value
    assert payload["step_run_id"] == started.step_run.step_run_id
    assert payload["execution_cursor"] == "dispatch_step"


@pytest.mark.asyncio
async def test_attempt_rolls_back_when_checkpoint_insert_fails(execution_fixture, monkeypatch):
    await execution_fixture.approve()

    async def fail_checkpoint(*_args, **_kwargs):
        raise RuntimeError("injected checkpoint failure")

    monkeypatch.setattr(WorkflowCoordinator, "checkpoint", fail_checkpoint)
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )
    assert await execution_fixture.count_step_runs() == 0


@pytest.mark.skipif(not _MYSQL_URL, reason="MULTICLAW_TEST_MYSQL_URL is not configured")
@pytest.mark.asyncio
async def test_mysql_concurrent_attempt_contract():
    assert _MYSQL_URL is not None
    database = Database.create(DatabaseSettings(driver="mysql", url=_MYSQL_URL))
    try:
        root = await _seed_root_context(database, "mysql-execution")
        settings = _settings()
        async with TenantUnitOfWork(database, root) as uow:
            session = await uow.sessions.create("MySQL Plan execution")
            context = root.for_session(session.id)
            assert uow.conn is not None
            source = await MemoryRepository(uow.conn, context, database.dialect).save(
                MemoryEntry(
                    content="Deliver the Plan",
                    type="chat_message",
                    role="user",
                    turn_index=1,
                )
            )
        workflow = WorkflowCoordinator(database, settings=settings)
        service = PlanningService(database, settings=settings, workflow=workflow)
        fixture = ExecutionFixture(
            database=database,
            settings=settings,
            root_context=context,
            source_message_id=source.id,
            coordinator=PlanExecutionCoordinator(database, settings=settings),
            service=service,
        )
        await fixture.approve_with_two_roots()

        async def start():
            try:
                return await fixture.new_coordinator().start_next_attempt(
                    context=fixture._context(),
                    lease=fixture.lease,
                )
            except PlanStepAlreadyRunningError:
                return None

        assert len([item for item in await asyncio.gather(start(), start()) if item]) == 1
    finally:
        await database.dispose()
