import asyncio
import os
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError

from alembic import command
from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, PlanningSettings
from multiclaw.memory import MemoryEntry
from multiclaw.planner import (
    PlanDecisionAction,
    PlanDecisionIdempotencyError,
    PlanDecisionRequest,
    PlanDraft,
    PlanDraftStep,
    PlanStatus,
    PlanStepRunStatus,
    PlanSummary,
    PlanTriggerMode,
    PlanValidationError,
    PlanVersionConflictError,
)
from multiclaw.storage import Database
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.repositories.plans import PlanRepository
from multiclaw.storage.schema import (
    agent_plan_decisions,
    agent_plan_step_dependencies,
    agent_plan_step_runs,
    agent_plan_steps,
    agent_plan_versions,
    agent_plans,
    agent_runs,
)
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow import RunStatus

_ORIGINAL_TEST_MYSQL_URL = os.getenv("MULTICLAW_TEST_MYSQL_URL")


def plan_draft(objective: str = "Deliver the change") -> PlanDraft:
    return PlanDraft(
        objective=objective,
        constraints=["No new dependency"],
        generation_reason="The work crosses durable boundaries.",
        steps=[
            PlanDraftStep(
                logical_step_key="inspect",
                title="Inspect",
                description="Inspect current behavior.",
                expected_outcome="Relevant interfaces are identified.",
                depends_on=[],
                max_attempts=2,
            ),
            PlanDraftStep(
                logical_step_key="verify",
                title="Verify",
                description="Verify the resulting behavior.",
                expected_outcome="Focused checks pass.",
                depends_on=["inspect"],
                max_attempts=2,
            ),
        ],
    )


def agent_run_row(
    context: TenantContext,
    *,
    plan_id: str,
    run_id: str,
    status: RunStatus,
    created_at: int,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "tenant_id": context.tenant_id,
        "workspace_id": context.workspace_id,
        "session_id": context.session_id,
        "plan_id": plan_id,
        "initial_plan_version": 1,
        "active_plan_version": 1,
        "cancel_requested_at": None,
        "run_status": status.value,
        "runtime_instance_id": "runtime-a",
        "lease_owner": "runtime-a",
        "fencing_token": 1,
        "lease_expires_at": 10_000,
        "heartbeat_at": 1,
        "schema_version": 1,
        "version": 1,
        "created_at": created_at,
        "updated_at": created_at,
        "finished_at": None,
    }


def test_plan_summary_is_frozen():
    summary = PlanSummary(
        plan_id=str(uuid4()),
        session_id=str(uuid4()),
        status=PlanStatus.APPROVED,
        current_version=2,
        approved_version=1,
        aggregate_version=3,
        latest_run_id=str(uuid4()),
        latest_run_status=RunStatus.RUNNING,
    )

    with pytest.raises(FrozenInstanceError):
        summary.current_version = 3


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'plan-repository.db'}"


async def _upgrade_database(database_url: str) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")


async def _seed_scope(database: Database, *, slug: str) -> TenantContext:
    tenant_id = str(uuid4())
    workspace_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users (
                    id, email, auth_epoch, default_workspace_id, status,
                    purge_after, created_at, updated_at, disabled_at, purge_requested_at
                ) VALUES (
                    :tenant_id, :email, 0, NULL, 'active',
                    NULL, 1, 1, NULL, NULL
                )
                """
            ),
            {"tenant_id": tenant_id, "email": f"{slug}@example.com"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                VALUES (:workspace_id, :tenant_id, :slug, :name, 'active', 1, 1)
                """
            ),
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "slug": slug,
                "name": slug.title(),
            },
        )
        await conn.execute(
            text(
                """
                UPDATE users SET default_workspace_id = :workspace_id
                WHERE id = :tenant_id
                """
            ),
            {"tenant_id": tenant_id, "workspace_id": workspace_id},
        )
    return TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)


async def _seed_workspace(
    database: Database,
    *,
    tenant_id: str,
    slug: str,
) -> TenantContext:
    workspace_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                VALUES (:workspace_id, :tenant_id, :slug, :name, 'active', 1, 1)
                """
            ),
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "slug": slug,
                "name": slug.title(),
            },
        )
    return TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)


@pytest.fixture
async def plan_database(tmp_path: Path):
    database_url = _sqlite_url(tmp_path)
    await _upgrade_database(database_url)
    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def optional_mysql_plan_database():
    if not _ORIGINAL_TEST_MYSQL_URL:
        pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")
    database = Database.create(
        DatabaseSettings(driver="mysql", url=_ORIGINAL_TEST_MYSQL_URL)
    )
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def plan_contexts(plan_database: Database) -> dict[str, TenantContext]:
    primary = await _seed_scope(plan_database, slug="plan-primary")
    return {
        "primary": primary,
        "sibling": await _seed_workspace(
            plan_database,
            tenant_id=primary.tenant_id,
            slug="plan-sibling",
        ),
        "secondary": await _seed_scope(plan_database, slug="plan-secondary"),
    }


@dataclass(frozen=True, slots=True)
class SeededPlan:
    context: TenantContext
    plan_id: str
    aggregate_version: int
    step_ids: dict[str, str]
    foreign_contexts: tuple[TenantContext, ...]

    @property
    def tenant_id(self) -> str:
        return self.context.tenant_id


class FailingSavepoint:
    is_active = True

    def __init__(self, rollback_error: BaseException) -> None:
        self.rollback_error = rollback_error

    async def commit(self) -> None:
        self.is_active = False

    async def rollback(self) -> None:
        raise self.rollback_error


class FailingCreateConnection:
    def __init__(self, primary_error: BaseException, savepoint: FailingSavepoint) -> None:
        self.primary_error = primary_error
        self.savepoint = savepoint

    async def begin_nested(self) -> FailingSavepoint:
        return self.savepoint

    async def execute(self, *_args, **_kwargs):
        raise self.primary_error


class FixedNowDialect:
    def db_now_ms(self) -> int:
        return 1


class ZeroRowcountResult:
    rowcount = 0


class LateCasFailureConnection:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def begin_nested(self):
        return await self.connection.begin_nested()

    async def execute(self, statement, *args, **kwargs):
        if getattr(statement, "is_update", False) and statement.table is agent_plans:
            return ZeroRowcountResult()
        return await self.connection.execute(statement, *args, **kwargs)


class DecisionIntegrityFailureConnection:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def begin_nested(self):
        return await self.connection.begin_nested()

    async def execute(self, statement, *args, **kwargs):
        if (
            getattr(statement, "is_insert", False)
            and statement.table is agent_plan_decisions
        ):
            raise IntegrityError(
                "INSERT INTO agent_plan_decisions",
                {},
                RuntimeError("injected non-duplicate integrity failure"),
            )
        return await self.connection.execute(statement, *args, **kwargs)


class FailingDecisionRollbackSavepoint:
    is_active = True

    def __init__(self, connection, rollback_error: BaseException) -> None:
        self.connection = connection
        self.rollback_error = rollback_error

    async def commit(self) -> None:
        self.is_active = False

    async def rollback(self) -> None:
        self.connection.rollback_failed = True
        raise self.rollback_error


class DuplicateDecisionRollbackFailureConnection:
    def __init__(self, connection, primary_error: IntegrityError) -> None:
        self.connection = connection
        self.primary_error = primary_error
        self.rollback_failed = False

    async def begin_nested(self):
        return FailingDecisionRollbackSavepoint(
            self,
            RuntimeError("injected rollback failure"),
        )

    async def execute(self, statement, *args, **kwargs):
        if self.rollback_failed:
            raise AssertionError("database queried after savepoint rollback failure")
        if (
            getattr(statement, "is_insert", False)
            and statement.table is agent_plan_decisions
        ):
            raise self.primary_error
        return await self.connection.execute(statement, *args, **kwargs)


class CountingConnection:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.execute_calls = 0

    async def execute(self, statement, *args, **kwargs):
        self.execute_calls += 1
        return await self.connection.execute(statement, *args, **kwargs)


class StatementRecordingConnection:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.statements: list[object] = []

    async def begin_nested(self):
        return await self.connection.begin_nested()

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        return await self.connection.execute(statement, *args, **kwargs)


@pytest.fixture
async def seeded_plan(
    plan_database: Database,
    plan_contexts: dict[str, TenantContext],
) -> SeededPlan:
    root = plan_contexts["primary"]
    async with TenantUnitOfWork(plan_database, root) as uow:
        session = await uow.sessions.create("Seeded plan")
        context = root.for_session(session.id)
        source = await MemoryRepository(uow.conn, context, plan_database.dialect).save(
            MemoryEntry(content="Deliver the change", type="chat_message", role="user", turn_index=1)
        )
        snapshot = await uow.plans.for_context(context).create(
            plan_id=str(uuid4()),
            source_message_id=source.id,
            trigger_mode=PlanTriggerMode.EXPLICIT,
            draft=plan_draft(),
        )

    sibling = plan_contexts["sibling"]
    secondary = plan_contexts["secondary"]
    return SeededPlan(
        context=context,
        plan_id=snapshot.plan_id,
        aggregate_version=snapshot.aggregate_version,
        step_ids={step.logical_step_key: step.step_id for step in snapshot.current.steps},
        foreign_contexts=(
            replace(context, workspace_id=sibling.workspace_id),
            replace(
                context,
                tenant_id=secondary.tenant_id,
                workspace_id=secondary.workspace_id,
            ),
            replace(context, session_id=str(uuid4())),
        ),
    )


@pytest.fixture
async def seeded_revised_plan(plan_database: Database, seeded_plan: SeededPlan) -> SeededPlan:
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        snapshot = await uow.plans.for_context(seeded_plan.context).append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=seeded_plan.aggregate_version,
            draft=plan_draft("Deliver the revised change"),
            parent_version=1,
            revision_feedback="Add the revision",
            supersedes=seeded_plan.step_ids,
        )
    return replace(
        seeded_plan,
        aggregate_version=snapshot.aggregate_version,
        step_ids={step.logical_step_key: step.step_id for step in snapshot.current.steps},
    )


async def dump_plan_version_rows(
    database: Database,
    context: TenantContext,
    plan_id: str,
    plan_version: int,
) -> tuple[tuple[dict[str, object], ...], ...]:
    filters = (
        agent_plan_versions.c.tenant_id == context.tenant_id,
        agent_plan_versions.c.workspace_id == context.workspace_id,
        agent_plan_versions.c.session_id == context.session_id,
        agent_plan_versions.c.plan_id == plan_id,
        agent_plan_versions.c.plan_version == plan_version,
    )
    async with database.connect() as conn:
        version_rows = tuple(
            dict(row)
            for row in (
                await conn.execute(select(agent_plan_versions).where(*filters))
            ).mappings()
        )
        step_rows = tuple(
            dict(row)
            for row in (
                await conn.execute(
                    select(agent_plan_steps)
                    .where(
                        agent_plan_steps.c.tenant_id == context.tenant_id,
                        agent_plan_steps.c.workspace_id == context.workspace_id,
                        agent_plan_steps.c.session_id == context.session_id,
                        agent_plan_steps.c.plan_id == plan_id,
                        agent_plan_steps.c.plan_version == plan_version,
                    )
                    .order_by(agent_plan_steps.c.ordinal)
                )
            ).mappings()
        )
        dependency_rows = tuple(
            dict(row)
            for row in (
                await conn.execute(
                    select(agent_plan_step_dependencies)
                    .where(
                        agent_plan_step_dependencies.c.tenant_id == context.tenant_id,
                        agent_plan_step_dependencies.c.workspace_id == context.workspace_id,
                        agent_plan_step_dependencies.c.session_id == context.session_id,
                        agent_plan_step_dependencies.c.plan_id == plan_id,
                        agent_plan_step_dependencies.c.plan_version == plan_version,
                    )
                    .order_by(
                        agent_plan_step_dependencies.c.step_id,
                        agent_plan_step_dependencies.c.depends_on_step_id,
                    )
                )
            ).mappings()
        )
    return version_rows, step_rows, dependency_rows


async def count_plan_rows(
    database: Database,
    context: TenantContext,
    plan_id: str,
) -> tuple[int, int, int, int]:
    tables = (
        agent_plans,
        agent_plan_versions,
        agent_plan_steps,
        agent_plan_step_dependencies,
    )
    async with database.connect() as conn:
        counts: list[int] = []
        for table in tables:
            result = await conn.execute(
                select(func.count())
                .select_from(table)
                .where(
                    table.c.tenant_id == context.tenant_id,
                    table.c.workspace_id == context.workspace_id,
                    table.c.session_id == context.session_id,
                    (table.c.id if table is agent_plans else table.c.plan_id) == plan_id,
                )
            )
            counts.append(int(result.scalar_one()))
        return counts[0], counts[1], counts[2], counts[3]


async def count_decisions(database: Database, plan_id: str) -> int:
    async with database.connect() as conn:
        result = await conn.execute(
            select(func.count())
            .select_from(agent_plan_decisions)
            .where(agent_plan_decisions.c.plan_id == plan_id)
        )
        return int(result.scalar_one())


async def insert_legacy_decision(
    database: Database,
    seeded_plan: SeededPlan,
    *,
    decision_id: str,
    plan_version: int,
    expected_version: int,
) -> None:
    async with database.write_transaction() as connection:
        await connection.execute(
            insert(agent_plan_decisions).values(
                tenant_id=seeded_plan.context.tenant_id,
                workspace_id=seeded_plan.context.workspace_id,
                session_id=seeded_plan.context.session_id,
                plan_id=seeded_plan.plan_id,
                decision_id=decision_id,
                plan_version=plan_version,
                expected_plan_cas_version=expected_version,
                action=PlanDecisionAction.REJECT.value,
                feedback=None,
                decided_by=seeded_plan.tenant_id,
                resulting_plan_version=None,
                created_at=1,
            )
        )


def approve_request(seeded_plan: SeededPlan, decision_id: str) -> PlanDecisionRequest:
    return PlanDecisionRequest(
        decision_id=decision_id,
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.APPROVE,
        feedback=None,
    )


@pytest.mark.asyncio
async def test_same_decision_id_returns_original_result(plan_database, seeded_plan):
    request = approve_request(seeded_plan, "decision-retry")
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        repo = uow.plans.for_context(seeded_plan.context)
        first = await repo.record_decision(request, decided_by=seeded_plan.tenant_id)
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        persisted_first = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        latest_before_replay = await uow.plans.for_context(
            seeded_plan.context
        ).append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=first.snapshot.aggregate_version,
            draft=plan_draft("A later immutable version"),
            parent_version=first.snapshot.current_version,
            revision_feedback="This happened after the approval response",
            supersedes=seeded_plan.step_ids,
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        second = await uow.plans.for_context(seeded_plan.context).record_decision(
            request,
            decided_by=seeded_plan.tenant_id,
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        latest_after_replay = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )

    assert first.idempotent_replay is False
    assert first.snapshot == persisted_first
    assert second.idempotent_replay is True
    assert second.snapshot == first.snapshot
    assert second.decision == first.decision
    assert second.snapshot.status is PlanStatus.APPROVED
    assert latest_after_replay == latest_before_replay
    assert await count_decisions(plan_database, seeded_plan.plan_id) == 1


@pytest.mark.asyncio
async def test_rejected_decision_replay_preserves_prior_approval(
    plan_database,
    seeded_plan,
    monkeypatch,
):
    monkeypatch.setattr(plan_database.dialect, "db_now_ms", lambda: 1)
    approval = approve_request(seeded_plan, "z-decision-approved-v1")
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        approved = await uow.plans.for_context(seeded_plan.context).record_decision(
            approval,
            decided_by=seeded_plan.tenant_id,
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        awaiting_rejection = await uow.plans.for_context(
            seeded_plan.context
        ).append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=approved.snapshot.aggregate_version,
            draft=plan_draft("A version that will be rejected"),
            parent_version=approved.snapshot.current_version,
            revision_feedback="Review a second version",
            supersedes=seeded_plan.step_ids,
        )

    request = PlanDecisionRequest(
        decision_id="y-decision-rejected-v2",
        plan_id=seeded_plan.plan_id,
        plan_version=awaiting_rejection.current_version,
        expected_version=awaiting_rejection.aggregate_version,
        action=PlanDecisionAction.REJECT,
        feedback=None,
    )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        first = await uow.plans.for_context(seeded_plan.context).record_decision(
            request,
            decided_by=seeded_plan.tenant_id,
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        persisted_first = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        awaiting_later_approval = await uow.plans.for_context(
            seeded_plan.context
        ).append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=first.snapshot.aggregate_version,
            draft=plan_draft("A version after the rejection"),
            parent_version=first.snapshot.current_version,
            revision_feedback="Continue after rejection",
            supersedes={
                step.logical_step_key: step.step_id
                for step in first.snapshot.current.steps
            },
        )
    later_approval = PlanDecisionRequest(
        decision_id="a-decision-approved-v3",
        plan_id=seeded_plan.plan_id,
        plan_version=awaiting_later_approval.current_version,
        expected_version=awaiting_later_approval.aggregate_version,
        action=PlanDecisionAction.APPROVE,
        feedback=None,
    )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        latest_before_replay = await uow.plans.for_context(
            seeded_plan.context
        ).record_decision(
            later_approval,
            decided_by=seeded_plan.tenant_id,
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        replay = await uow.plans.for_context(seeded_plan.context).record_decision(
            request,
            decided_by=seeded_plan.tenant_id,
        )

    assert replay.idempotent_replay is True
    assert first.snapshot == persisted_first
    assert replay.snapshot == first.snapshot
    assert replay.snapshot.status is PlanStatus.REJECTED
    assert replay.snapshot.current_version == 2
    assert replay.snapshot.approved_version == 1
    assert [version.plan_version for version in replay.snapshot.versions] == [1, 2]
    assert {decision.decision_id for decision in replay.snapshot.decisions} == {
        approval.decision_id,
        request.decision_id,
    }
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        latest_after_replay = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )
    assert latest_after_replay == latest_before_replay.snapshot


@pytest.mark.asyncio
async def test_reused_decision_id_with_different_input_is_rejected(
    plan_database,
    seeded_plan,
):
    request = approve_request(seeded_plan, "decision-reused")
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        await uow.plans.for_context(seeded_plan.context).record_decision(
            request,
            decided_by=seeded_plan.tenant_id,
        )

    conflicts = (
        (request.model_copy(update={"plan_version": 2}), seeded_plan.tenant_id),
        (request.model_copy(update={"expected_version": 2}), seeded_plan.tenant_id),
        (
            request.model_copy(update={"action": PlanDecisionAction.REJECT}),
            seeded_plan.tenant_id,
        ),
        (request.model_copy(update={"feedback": "different"}), seeded_plan.tenant_id),
        (request, str(uuid4())),
    )
    for changed, decided_by in conflicts:
        with pytest.raises(PlanDecisionIdempotencyError):
            async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
                await uow.plans.for_context(seeded_plan.context).record_decision(
                    changed,
                    decided_by=decided_by,
                )

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 1


@pytest.mark.asyncio
async def test_decision_losing_late_cas_rolls_back_insert(plan_database, seeded_plan):
    request = approve_request(seeded_plan, "decision-late-cas")
    with pytest.raises(PlanVersionConflictError) as raised:
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = PlanRepository(
                LateCasFailureConnection(uow.conn),  # type: ignore[arg-type]
                plan_database.dialect,
                seeded_plan.context,
                PlanningSettings(),
            )
            await repository.record_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 0
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        snapshot = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )
    assert snapshot is not None
    assert raised.value.latest == snapshot
    assert raised.value.latest.decisions == ()
    assert snapshot.status is PlanStatus.AWAITING_APPROVAL
    assert snapshot.aggregate_version == seeded_plan.aggregate_version


@pytest.mark.asyncio
async def test_nonduplicate_decision_integrity_error_is_not_a_replay(
    plan_database,
    seeded_plan,
):
    request = approve_request(seeded_plan, "decision-integrity")
    with pytest.raises(IntegrityError, match="non-duplicate integrity failure"):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = PlanRepository(
                DecisionIntegrityFailureConnection(uow.conn),  # type: ignore[arg-type]
                plan_database.dialect,
                seeded_plan.context,
                PlanningSettings(),
            )
            await repository.record_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    (PlanDecisionAction.APPROVE, PlanDecisionAction.REVISE),
)
async def test_duplicate_decision_rollback_failure_rethrows_primary_without_query(
    action,
    plan_database,
    seeded_plan,
    monkeypatch,
):
    primary = IntegrityError(
        "INSERT INTO agent_plan_decisions",
        {},
        RuntimeError("injected duplicate decision"),
    )
    request = PlanDecisionRequest(
        decision_id=f"rollback-failure-{action.value}",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=action,
        feedback="Revise safely" if action is PlanDecisionAction.REVISE else None,
    )
    monkeypatch.setattr(
        PlanRepository,
        "_is_decision_id_duplicate",
        lambda _self, _error: True,
    )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        repository = PlanRepository(
            DuplicateDecisionRollbackFailureConnection(  # type: ignore[arg-type]
                uow.conn,
                primary,
            ),
            plan_database.dialect,
            seeded_plan.context,
            PlanningSettings(),
        )
        with pytest.raises(IntegrityError) as raised:
            if action is PlanDecisionAction.REVISE:
                await repository.begin_revision_decision(
                    request,
                    decided_by=seeded_plan.tenant_id,
                )
            else:
                await repository.record_decision(
                    request,
                    decided_by=seeded_plan.tenant_id,
                )

    assert raised.value is primary
    assert getattr(primary, "__notes__", []) == [
        "savepoint rollback cleanup failed: RuntimeError: injected rollback failure"
    ]


@pytest.mark.asyncio
async def test_stale_decision_returns_latest_snapshot(plan_database, seeded_revised_plan):
    stale = PlanDecisionRequest(
        decision_id="stale",
        plan_id=seeded_revised_plan.plan_id,
        plan_version=1,
        expected_version=1,
        action=PlanDecisionAction.REJECT,
        feedback=None,
    )
    with pytest.raises(PlanVersionConflictError) as raised:
        async with TenantUnitOfWork(plan_database, seeded_revised_plan.context) as uow:
            await uow.plans.for_context(seeded_revised_plan.context).record_decision(
                stale,
                decided_by=seeded_revised_plan.tenant_id,
            )

    assert raised.value.latest.current_version == 2
    assert await count_decisions(plan_database, seeded_revised_plan.plan_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    (
        PlanDecisionAction.APPROVE,
        PlanDecisionAction.REJECT,
        PlanDecisionAction.REVISE,
    ),
)
async def test_stale_different_decision_key_returns_durable_latest(
    action,
    plan_database,
    seeded_plan,
):
    winner_request = approve_request(seeded_plan, "decision-winner")
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        winner = await uow.plans.for_context(seeded_plan.context).record_decision(
            winner_request,
            decided_by=seeded_plan.tenant_id,
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        durable_latest = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )
    assert durable_latest == winner.snapshot

    stale = PlanDecisionRequest(
        decision_id=f"stale-{action.value}",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=action,
        feedback="Stale revision" if action is PlanDecisionAction.REVISE else None,
    )
    with pytest.raises(PlanVersionConflictError) as raised:
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = uow.plans.for_context(seeded_plan.context)
            if action is PlanDecisionAction.REVISE:
                await repository.begin_revision_decision(
                    stale,
                    decided_by=seeded_plan.tenant_id,
                )
            else:
                await repository.record_decision(
                    stale,
                    decided_by=seeded_plan.tenant_id,
                )

    assert raised.value.latest == durable_latest
    assert await count_decisions(plan_database, seeded_plan.plan_id) == 1
    assert await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    ) == (1, 1, 2, 1)


@pytest.mark.asyncio
async def test_ten_concurrent_mixed_decisions_have_one_winner(plan_database, seeded_plan):
    actions = [PlanDecisionAction.APPROVE, PlanDecisionAction.REJECT] * 5

    async def decide(index: int, action: PlanDecisionAction):
        request = PlanDecisionRequest(
            decision_id=f"decision-{index}",
            plan_id=seeded_plan.plan_id,
            plan_version=1,
            expected_version=seeded_plan.aggregate_version,
            action=action,
            feedback=None,
        )
        try:
            async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
                return await uow.plans.for_context(seeded_plan.context).record_decision(
                    request,
                    decided_by=seeded_plan.tenant_id,
                )
        except PlanVersionConflictError as error:
            return error

    outcomes = await asyncio.gather(
        *(decide(i, action) for i, action in enumerate(actions))
    )
    winners = [item for item in outcomes if not isinstance(item, PlanVersionConflictError)]
    assert len(winners) == 1
    assert await count_decisions(plan_database, seeded_plan.plan_id) == 1


@pytest.mark.asyncio
async def test_ten_concurrent_identical_decisions_have_one_write_and_nine_replays(
    plan_database,
    seeded_plan,
):
    request = approve_request(seeded_plan, "decision-concurrent-retry")

    async def decide():
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            return await uow.plans.for_context(seeded_plan.context).record_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )

    outcomes = await asyncio.gather(*(decide() for _ in range(10)))

    assert sum(not outcome.idempotent_replay for outcome in outcomes) == 1
    assert sum(outcome.idempotent_replay for outcome in outcomes) == 9
    assert len({outcome.decision for outcome in outcomes}) == 1
    assert await count_decisions(plan_database, seeded_plan.plan_id) == 1


def test_revision_requires_nonblank_feedback(seeded_plan):
    with pytest.raises(ValidationError, match="revision feedback is required"):
        PlanDecisionRequest(
            decision_id="revise-empty",
            plan_id=seeded_plan.plan_id,
            plan_version=1,
            expected_version=seeded_plan.aggregate_version,
            action=PlanDecisionAction.REVISE,
            feedback="   ",
        )


@pytest.mark.asyncio
async def test_revision_records_result_and_preserves_parent_rows(plan_database, seeded_plan):
    before = await dump_plan_version_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
        1,
    )
    request = PlanDecisionRequest(
        decision_id="revise-1",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="Cover both databases",
    )
    revised = plan_draft("Deliver with database parity")

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        repo = uow.plans.for_context(seeded_plan.context)
        await repo.begin_revision_decision(request, decided_by=seeded_plan.tenant_id)
        snapshot = await repo.append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=seeded_plan.aggregate_version,
            draft=revised,
            parent_version=1,
            revision_feedback=request.feedback,
            supersedes=seeded_plan.step_ids,
        )
        decision = await repo.finish_revision_decision(
            plan_id=seeded_plan.plan_id,
            decision_id=request.decision_id,
            resulting_plan_version=snapshot.current_version,
        )

    assert (
        await dump_plan_version_rows(
            plan_database,
            seeded_plan.context,
            seeded_plan.plan_id,
            1,
        )
        == before
    )
    assert snapshot.current_version == 2
    assert decision.resulting_plan_version == 2


@pytest.mark.asyncio
async def test_completed_revision_decision_replays_original_snapshot(
    plan_database,
    seeded_plan,
    monkeypatch,
):
    monkeypatch.setattr(plan_database.dialect, "db_now_ms", lambda: 1)
    approval = approve_request(seeded_plan, "z-decision-approved-before-revision")
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        approved = await uow.plans.for_context(seeded_plan.context).record_decision(
            approval,
            decided_by=seeded_plan.tenant_id,
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        awaiting_revision = await uow.plans.for_context(
            seeded_plan.context
        ).append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=approved.snapshot.aggregate_version,
            draft=plan_draft("Version two needs revision"),
            parent_version=approved.snapshot.current_version,
            revision_feedback="Prepare version two",
            supersedes=seeded_plan.step_ids,
        )

    request = PlanDecisionRequest(
        decision_id="y-decision-revise-v2",
        plan_id=seeded_plan.plan_id,
        plan_version=awaiting_revision.current_version,
        expected_version=awaiting_revision.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="Produce version three",
    )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        repository = uow.plans.for_context(seeded_plan.context)
        await repository.begin_revision_decision(
            request,
            decided_by=seeded_plan.tenant_id,
        )
        revised = await repository.append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=request.expected_version,
            draft=plan_draft("Revised version three"),
            parent_version=request.plan_version,
            revision_feedback=request.feedback,
            supersedes={
                step.logical_step_key: step.step_id
                for step in awaiting_revision.current.steps
            },
        )
        await repository.finish_revision_decision(
            plan_id=seeded_plan.plan_id,
            decision_id=request.decision_id,
            resulting_plan_version=revised.current_version,
        )
        first = await repository.record_decision(
            request,
            decided_by=seeded_plan.tenant_id,
        )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        persisted_first = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        awaiting_later_approval = await uow.plans.for_context(
            seeded_plan.context
        ).append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=first.snapshot.aggregate_version,
            draft=plan_draft("Version four after the completed revision"),
            parent_version=first.snapshot.current_version,
            revision_feedback="Continue after version three",
            supersedes={
                step.logical_step_key: step.step_id
                for step in first.snapshot.current.steps
            },
        )
    later_approval = PlanDecisionRequest(
        decision_id="a-decision-approved-v4",
        plan_id=seeded_plan.plan_id,
        plan_version=awaiting_later_approval.current_version,
        expected_version=awaiting_later_approval.aggregate_version,
        action=PlanDecisionAction.APPROVE,
        feedback=None,
    )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        latest_before_replay = await uow.plans.for_context(
            seeded_plan.context
        ).record_decision(
            later_approval,
            decided_by=seeded_plan.tenant_id,
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        replay = await uow.plans.for_context(seeded_plan.context).record_decision(
            request,
            decided_by=seeded_plan.tenant_id,
        )

    assert replay.idempotent_replay is True
    assert first.snapshot == persisted_first
    assert replay.snapshot == first.snapshot
    assert replay.snapshot.status is PlanStatus.AWAITING_APPROVAL
    assert replay.snapshot.current_version == 3
    assert replay.snapshot.approved_version == 1
    assert replay.snapshot.aggregate_version == request.expected_version + 1
    assert [version.plan_version for version in replay.snapshot.versions] == [1, 2, 3]
    assert replay.decision.resulting_plan_version == 3
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        latest_after_replay = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )
    assert latest_after_replay == latest_before_replay.snapshot


@pytest.mark.asyncio
async def test_revision_claim_is_unique_per_expected_aggregate_version(
    plan_database,
    seeded_plan,
    monkeypatch,
):
    monkeypatch.setattr(plan_database.dialect, "db_now_ms", lambda: 1)
    target = PlanDecisionRequest(
        decision_id="z-revision-target",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="The intended revision",
    )
    later = target.model_copy(
        update={
            "decision_id": "a-later-revision",
            "feedback": "A conflicting revision",
        }
    )
    approval = target.model_copy(
        update={
            "decision_id": "approve-during-revision",
            "action": PlanDecisionAction.APPROVE,
            "feedback": None,
        }
    )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        first_repository = uow.plans.for_context(seeded_plan.context)
        target_record = await first_repository.begin_revision_decision(
            target,
            decided_by=seeded_plan.tenant_id,
        )
        repeated = await uow.plans.for_context(
            seeded_plan.context
        ).begin_revision_decision(
            target,
            decided_by=seeded_plan.tenant_id,
        )
        assert repeated == target_record

        with pytest.raises(PlanDecisionIdempotencyError):
            await uow.plans.for_context(
                seeded_plan.context
            ).begin_revision_decision(
                target,
                decided_by=str(uuid4()),
            )
        with pytest.raises(PlanDecisionIdempotencyError, match="decision claim"):
            await uow.plans.for_context(
                seeded_plan.context
            ).begin_revision_decision(
                later,
                decided_by=seeded_plan.tenant_id,
            )
        with pytest.raises(PlanDecisionIdempotencyError, match="decision claim"):
            await uow.plans.for_context(seeded_plan.context).record_decision(
                approval,
                decided_by=seeded_plan.tenant_id,
            )

        revised = await first_repository.append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=target.expected_version,
            draft=plan_draft("The intended revised version"),
            parent_version=target.plan_version,
            revision_feedback=target.feedback,
            supersedes=seeded_plan.step_ids,
        )
        intervening_approval = PlanDecisionRequest(
            decision_id="approve-after-revision-append",
            plan_id=seeded_plan.plan_id,
            plan_version=revised.current_version,
            expected_version=revised.aggregate_version,
            action=PlanDecisionAction.APPROVE,
            feedback=None,
        )
        with pytest.raises(PlanDecisionIdempotencyError, match="decision claim"):
            await first_repository.record_decision(
                intervening_approval,
                decided_by=seeded_plan.tenant_id,
            )
        finished = await uow.plans.for_context(
            seeded_plan.context
        ).finish_revision_decision(
            plan_id=seeded_plan.plan_id,
            decision_id=target.decision_id,
            resulting_plan_version=revised.current_version,
        )
        repeated_finish = await first_repository.finish_revision_decision(
            plan_id=seeded_plan.plan_id,
            decision_id=target.decision_id,
            resulting_plan_version=revised.current_version,
        )

    assert repeated_finish == finished
    assert await count_decisions(plan_database, seeded_plan.plan_id) == 1


@pytest.mark.asyncio
async def test_historical_replay_rejects_ambiguous_same_cas_decisions(
    plan_database,
    seeded_plan,
    monkeypatch,
):
    monkeypatch.setattr(plan_database.dialect, "db_now_ms", lambda: 1)
    request = approve_request(seeded_plan, "z-original-decision")
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        await uow.plans.for_context(seeded_plan.context).record_decision(
            request,
            decided_by=seeded_plan.tenant_id,
        )
    async with plan_database.write_transaction() as connection:
        await connection.execute(
            insert(agent_plan_decisions).values(
                tenant_id=seeded_plan.context.tenant_id,
                workspace_id=seeded_plan.context.workspace_id,
                session_id=seeded_plan.context.session_id,
                plan_id=seeded_plan.plan_id,
                decision_id="a-ambiguous-decision",
                plan_version=1,
                expected_plan_cas_version=seeded_plan.aggregate_version,
                action=PlanDecisionAction.REJECT.value,
                feedback=None,
                decided_by=seeded_plan.tenant_id,
                resulting_plan_version=None,
                created_at=1,
            )
        )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        with pytest.raises(PlanDecisionIdempotencyError, match="ambiguous"):
            await uow.plans.for_context(seeded_plan.context).record_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )


@pytest.mark.asyncio
async def test_historical_replay_rejects_ambiguous_prior_cas_decisions(
    plan_database,
    seeded_plan,
):
    first_request = approve_request(seeded_plan, "approved-before-ambiguous-history")
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        first = await uow.plans.for_context(seeded_plan.context).record_decision(
            first_request,
            decided_by=seeded_plan.tenant_id,
        )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        awaiting_approval = await uow.plans.for_context(
            seeded_plan.context
        ).append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=first.snapshot.aggregate_version,
            draft=plan_draft("Version after the first approval"),
            parent_version=first.snapshot.current_version,
            revision_feedback="Continue after version one",
            supersedes=seeded_plan.step_ids,
        )
    replayed_request = PlanDecisionRequest(
        decision_id="approved-after-ambiguous-history",
        plan_id=seeded_plan.plan_id,
        plan_version=awaiting_approval.current_version,
        expected_version=awaiting_approval.aggregate_version,
        action=PlanDecisionAction.APPROVE,
        feedback=None,
    )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        await uow.plans.for_context(seeded_plan.context).record_decision(
            replayed_request,
            decided_by=seeded_plan.tenant_id,
        )
    async with plan_database.write_transaction() as connection:
        await connection.execute(
            insert(agent_plan_decisions).values(
                tenant_id=seeded_plan.context.tenant_id,
                workspace_id=seeded_plan.context.workspace_id,
                session_id=seeded_plan.context.session_id,
                plan_id=seeded_plan.plan_id,
                decision_id="ambiguous-prior-decision",
                plan_version=1,
                expected_plan_cas_version=seeded_plan.aggregate_version,
                action=PlanDecisionAction.REJECT.value,
                feedback=None,
                decided_by=seeded_plan.tenant_id,
                resulting_plan_version=None,
                created_at=1,
            )
        )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        with pytest.raises(PlanDecisionIdempotencyError, match="ambiguous"):
            await uow.plans.for_context(seeded_plan.context).record_decision(
                replayed_request,
                decided_by=seeded_plan.tenant_id,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous_boundary", ("target", "prior"))
@pytest.mark.parametrize(
    "action",
    (
        PlanDecisionAction.APPROVE,
        PlanDecisionAction.REJECT,
        PlanDecisionAction.REVISE,
    ),
)
async def test_new_decision_claim_rejects_ambiguous_history(
    ambiguous_boundary,
    action,
    plan_database,
    seeded_plan,
):
    if ambiguous_boundary == "prior":
        first_request = approve_request(seeded_plan, "first-history-decision")
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            first = await uow.plans.for_context(
                seeded_plan.context
            ).record_decision(
                first_request,
                decided_by=seeded_plan.tenant_id,
            )
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            current = await uow.plans.for_context(
                seeded_plan.context
            ).append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=first.snapshot.aggregate_version,
                draft=plan_draft("Version after ambiguous history"),
                parent_version=first.snapshot.current_version,
                revision_feedback="Continue after version one",
                supersedes=seeded_plan.step_ids,
            )
        await insert_legacy_decision(
            plan_database,
            seeded_plan,
            decision_id="ambiguous-prior-history",
            plan_version=1,
            expected_version=seeded_plan.aggregate_version,
        )
    else:
        await insert_legacy_decision(
            plan_database,
            seeded_plan,
            decision_id="ambiguous-target-history-a",
            plan_version=1,
            expected_version=seeded_plan.aggregate_version,
        )
        await insert_legacy_decision(
            plan_database,
            seeded_plan,
            decision_id="ambiguous-target-history-b",
            plan_version=1,
            expected_version=seeded_plan.aggregate_version,
        )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        current = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )
    assert current is not None

    decision_count = await count_decisions(plan_database, seeded_plan.plan_id)
    plan_counts = await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    )
    request = PlanDecisionRequest(
        decision_id=f"new-{ambiguous_boundary}-{action.value}",
        plan_id=seeded_plan.plan_id,
        plan_version=current.current_version,
        expected_version=current.aggregate_version,
        action=action,
        feedback="Produce an exact revision" if action is PlanDecisionAction.REVISE else None,
    )

    with pytest.raises(PlanDecisionIdempotencyError, match="ambiguous"):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = uow.plans.for_context(seeded_plan.context)
            if action is PlanDecisionAction.REVISE:
                await repository.begin_revision_decision(
                    request,
                    decided_by=seeded_plan.tenant_id,
                )
                revised = await repository.append_version(
                    plan_id=seeded_plan.plan_id,
                    expected_version=request.expected_version,
                    draft=plan_draft("Revision that must not persist"),
                    parent_version=request.plan_version,
                    revision_feedback=request.feedback,
                    supersedes={
                        step.logical_step_key: step.step_id
                        for step in current.current.steps
                    },
                )
                await repository.finish_revision_decision(
                    plan_id=seeded_plan.plan_id,
                    decision_id=request.decision_id,
                    resulting_plan_version=revised.current_version,
                )
            else:
                await repository.record_decision(
                    request,
                    decided_by=seeded_plan.tenant_id,
                )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        after = await uow.plans.for_context(seeded_plan.context).get(
            seeded_plan.plan_id
        )
    assert after == current
    assert await count_decisions(plan_database, seeded_plan.plan_id) == decision_count
    assert await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    ) == plan_counts


@pytest.mark.asyncio
async def test_revision_finish_keeps_claim_pending_when_history_becomes_ambiguous(
    plan_database,
    seeded_plan,
):
    request = PlanDecisionRequest(
        decision_id="revision-before-ambiguous-history",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="Finish must revalidate history",
    )

    with pytest.raises(RuntimeError, match="unfinished plan revision"):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = uow.plans.for_context(seeded_plan.context)
            await repository.begin_revision_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )
            revised = await repository.append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=request.expected_version,
                draft=plan_draft("Revision before ambiguous history"),
                parent_version=request.plan_version,
                revision_feedback=request.feedback,
                supersedes=seeded_plan.step_ids,
            )
            assert uow.conn is not None
            await uow.conn.execute(
                insert(agent_plan_decisions).values(
                    tenant_id=seeded_plan.context.tenant_id,
                    workspace_id=seeded_plan.context.workspace_id,
                    session_id=seeded_plan.context.session_id,
                    plan_id=seeded_plan.plan_id,
                    decision_id="late-ambiguous-history",
                    plan_version=request.plan_version,
                    expected_plan_cas_version=request.expected_version,
                    action=PlanDecisionAction.REJECT.value,
                    feedback=None,
                    decided_by=seeded_plan.tenant_id,
                    resulting_plan_version=None,
                    created_at=1,
                )
            )
            with pytest.raises(PlanDecisionIdempotencyError, match="ambiguous"):
                await repository.finish_revision_decision(
                    plan_id=seeded_plan.plan_id,
                    decision_id=request.decision_id,
                    resulting_plan_version=revised.current_version,
                )

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 0
    assert await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    ) == (1, 1, 2, 1)


@pytest.mark.asyncio
async def test_revision_cannot_finish_without_appending_the_next_version(
    plan_database,
    seeded_plan,
):
    request = PlanDecisionRequest(
        decision_id="revise-without-version",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="This must produce version two",
    )

    with pytest.raises(PlanVersionConflictError):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = uow.plans.for_context(seeded_plan.context)
            await repository.begin_revision_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )
            await repository.finish_revision_decision(
                plan_id=seeded_plan.plan_id,
                decision_id=request.decision_id,
                resulting_plan_version=1,
            )

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 0


@pytest.mark.asyncio
async def test_revision_validation_failure_rolls_back_decision_intent(
    plan_database,
    seeded_plan,
):
    request = PlanDecisionRequest(
        decision_id="revise-invalid",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="Use an invalid generated draft",
    )
    invalid = plan_draft("Invalid revision")
    invalid.steps[1].depends_on = ["missing"]

    with pytest.raises(PlanValidationError, match="missing dependency"):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = uow.plans.for_context(seeded_plan.context)
            await repository.begin_revision_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )
            await repository.append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=seeded_plan.aggregate_version,
                draft=invalid,
                parent_version=1,
                revision_feedback=request.feedback,
                supersedes=seeded_plan.step_ids,
            )

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 0
    assert await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    ) == (1, 1, 2, 1)


@pytest.mark.asyncio
async def test_caught_revision_failure_cannot_commit_pending_intent(
    plan_database,
    seeded_plan,
):
    request = PlanDecisionRequest(
        decision_id="revise-caught-invalid",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="This invalid append is caught by the caller",
    )
    invalid = plan_draft("Invalid caught revision")
    invalid.steps[1].depends_on = ["missing"]

    with pytest.raises(RuntimeError, match="unfinished plan revision"):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = uow.plans.for_context(seeded_plan.context)
            await repository.begin_revision_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )
            with pytest.raises(PlanValidationError, match="missing dependency"):
                await repository.append_version(
                    plan_id=seeded_plan.plan_id,
                    expected_version=seeded_plan.aggregate_version,
                    draft=invalid,
                    parent_version=1,
                    revision_feedback=request.feedback,
                    supersedes=seeded_plan.step_ids,
                )

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 0
    assert await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    ) == (1, 1, 2, 1)


@pytest.mark.asyncio
async def test_explicit_commit_rejects_pending_revision_intent(
    plan_database,
    seeded_plan,
):
    request = PlanDecisionRequest(
        decision_id="revise-explicit-commit",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="Explicit commit must fail closed",
    )

    with pytest.raises(RuntimeError, match="unfinished plan revision"):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            await uow.plans.for_context(seeded_plan.context).begin_revision_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )
            await uow.commit()

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_result",
    ("intervening-approval", "wrong-parent", "wrong-feedback", "wrong-result"),
)
async def test_revision_finish_requires_exact_intent_result(
    invalid_result,
    plan_database,
    seeded_plan,
):
    request = PlanDecisionRequest(
        decision_id=f"revise-{invalid_result}",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="The exact revision feedback",
    )

    with pytest.raises(RuntimeError, match="unfinished plan revision"):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = uow.plans.for_context(seeded_plan.context)
            await repository.begin_revision_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )
            revised = await repository.append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=request.expected_version,
                draft=plan_draft("Revision with a mismatched result"),
                parent_version=request.plan_version,
                revision_feedback=request.feedback,
                supersedes=seeded_plan.step_ids,
            )
            assert uow.conn is not None
            if invalid_result == "intervening-approval":
                await uow.conn.execute(
                    update(agent_plans)
                    .where(agent_plans.c.id == seeded_plan.plan_id)
                    .values(
                        status=PlanStatus.APPROVED.value,
                        approved_version=revised.current_version,
                        version=revised.aggregate_version + 1,
                    )
                )
            elif invalid_result == "wrong-parent":
                await uow.conn.execute(
                    update(agent_plan_versions)
                    .where(
                        agent_plan_versions.c.plan_id == seeded_plan.plan_id,
                        agent_plan_versions.c.plan_version == revised.current_version,
                    )
                    .values(parent_version=None)
                )
            elif invalid_result == "wrong-feedback":
                await uow.conn.execute(
                    update(agent_plan_versions)
                    .where(
                        agent_plan_versions.c.plan_id == seeded_plan.plan_id,
                        agent_plan_versions.c.plan_version == revised.current_version,
                    )
                    .values(revision_feedback="different feedback")
                )

            resulting_version = (
                revised.current_version + 1
                if invalid_result == "wrong-result"
                else revised.current_version
            )
            with pytest.raises(PlanVersionConflictError):
                await repository.finish_revision_decision(
                    plan_id=seeded_plan.plan_id,
                    decision_id=request.decision_id,
                    resulting_plan_version=resulting_version,
                )

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 0
    assert await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    ) == (1, 1, 2, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feedback_column", "expected_error"),
    (
        ("version", PlanVersionConflictError),
        ("decision", PlanDecisionIdempotencyError),
    ),
)
@pytest.mark.parametrize(
    "stored_feedback",
    ("caféExact", "CafeExact", "CaféExact "),
)
async def test_revision_finish_requires_python_exact_feedback(
    feedback_column,
    expected_error,
    stored_feedback,
    plan_database,
    seeded_plan,
):
    request = PlanDecisionRequest(
        decision_id=f"exact-feedback-{feedback_column}",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="CaféExact",
    )

    with pytest.raises(expected_error):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            repository = uow.plans.for_context(seeded_plan.context)
            await repository.begin_revision_decision(
                request,
                decided_by=seeded_plan.tenant_id,
            )
            revised = await repository.append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=request.expected_version,
                draft=plan_draft("Revision with inexact stored feedback"),
                parent_version=request.plan_version,
                revision_feedback=request.feedback,
                supersedes=seeded_plan.step_ids,
            )
            assert uow.conn is not None
            if feedback_column == "version":
                await uow.conn.execute(
                    update(agent_plan_versions)
                    .where(
                        agent_plan_versions.c.tenant_id
                        == seeded_plan.context.tenant_id,
                        agent_plan_versions.c.workspace_id
                        == seeded_plan.context.workspace_id,
                        agent_plan_versions.c.session_id
                        == seeded_plan.context.session_id,
                        agent_plan_versions.c.plan_id == seeded_plan.plan_id,
                        agent_plan_versions.c.plan_version == revised.current_version,
                    )
                    .values(revision_feedback=stored_feedback)
                )
            else:
                await uow.conn.execute(
                    update(agent_plan_decisions)
                    .where(
                        agent_plan_decisions.c.tenant_id
                        == seeded_plan.context.tenant_id,
                        agent_plan_decisions.c.workspace_id
                        == seeded_plan.context.workspace_id,
                        agent_plan_decisions.c.session_id
                        == seeded_plan.context.session_id,
                        agent_plan_decisions.c.plan_id == seeded_plan.plan_id,
                        agent_plan_decisions.c.decision_id == request.decision_id,
                    )
                    .values(feedback=stored_feedback)
                )
            await repository.finish_revision_decision(
                plan_id=seeded_plan.plan_id,
                decision_id=request.decision_id,
                resulting_plan_version=revised.current_version,
            )

    assert await count_decisions(plan_database, seeded_plan.plan_id) == 0
    assert await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    ) == (1, 1, 2, 1)


@pytest.mark.asyncio
async def test_revision_finish_mysql_sql_does_not_use_collated_feedback_equality(
    plan_database,
    seeded_plan,
):
    request = PlanDecisionRequest(
        decision_id="mysql-exact-feedback-contract",
        plan_id=seeded_plan.plan_id,
        plan_version=1,
        expected_version=seeded_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="CaféExact",
    )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        recording = StatementRecordingConnection(uow.conn)
        repository = PlanRepository(  # type: ignore[arg-type]
            recording,
            plan_database.dialect,
            seeded_plan.context,
            PlanningSettings(),
        )
        await repository.begin_revision_decision(
            request,
            decided_by=seeded_plan.tenant_id,
        )
        revised = await repository.append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=request.expected_version,
            draft=plan_draft("Compile exact feedback SQL"),
            parent_version=request.plan_version,
            revision_feedback=request.feedback,
            supersedes=seeded_plan.step_ids,
        )
        await repository.finish_revision_decision(
            plan_id=seeded_plan.plan_id,
            decision_id=request.decision_id,
            resulting_plan_version=revised.current_version,
        )

    compiled = tuple(
        str(statement.compile(dialect=mysql.dialect()))
        for statement in recording.statements
        if hasattr(statement, "compile")
    )
    version_lock = next(
        sql
        for sql in compiled
        if sql.startswith("SELECT agent_plan_versions") and "FOR UPDATE" in sql
    )
    decision_update = next(
        sql for sql in compiled if sql.startswith("UPDATE agent_plan_decisions")
    )
    assert "agent_plan_versions.revision_feedback =" not in version_lock
    assert "agent_plan_decisions.feedback =" not in decision_update


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_feedback",
    ("caféExact", "CafeExact", "CaféExact "),
)
async def test_mysql_revision_finish_requires_python_exact_feedback(
    stored_feedback,
    optional_mysql_plan_database,
):
    database = optional_mysql_plan_database
    root = await _seed_scope(database, slug=f"plan-mysql-{uuid4().hex[:8]}")
    async with TenantUnitOfWork(database, root) as uow:
        session = await uow.sessions.create("MySQL exact feedback")
        context = root.for_session(session.id)
        source = await MemoryRepository(uow.conn, context, database.dialect).save(
            MemoryEntry(
                content="Verify exact feedback",
                type="chat_message",
                role="user",
                turn_index=1,
            )
        )
        initial = await uow.plans.for_context(context).create(
            plan_id=str(uuid4()),
            source_message_id=source.id,
            trigger_mode=PlanTriggerMode.EXPLICIT,
            draft=plan_draft(),
        )
    request = PlanDecisionRequest(
        decision_id="mysql-feedback-revision",
        plan_id=initial.plan_id,
        plan_version=initial.current_version,
        expected_version=initial.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="CaféExact",
    )

    with pytest.raises(PlanVersionConflictError):
        async with TenantUnitOfWork(database, context) as uow:
            repository = uow.plans.for_context(context)
            await repository.begin_revision_decision(
                request,
                decided_by=context.tenant_id,
            )
            revised = await repository.append_version(
                plan_id=initial.plan_id,
                expected_version=request.expected_version,
                draft=plan_draft("MySQL exact feedback revision"),
                parent_version=request.plan_version,
                revision_feedback=request.feedback,
                supersedes={
                    step.logical_step_key: step.step_id
                    for step in initial.current.steps
                },
            )
            assert uow.conn is not None
            await uow.conn.execute(
                update(agent_plan_versions)
                .where(
                    agent_plan_versions.c.tenant_id == context.tenant_id,
                    agent_plan_versions.c.workspace_id == context.workspace_id,
                    agent_plan_versions.c.session_id == context.session_id,
                    agent_plan_versions.c.plan_id == initial.plan_id,
                    agent_plan_versions.c.plan_version == revised.current_version,
                )
                .values(revision_feedback=stored_feedback)
            )
            await repository.finish_revision_decision(
                plan_id=initial.plan_id,
                decision_id=request.decision_id,
                resulting_plan_version=revised.current_version,
            )

    assert await count_decisions(database, initial.plan_id) == 0


@pytest.mark.asyncio
async def test_create_plan_materializes_one_immutable_version(plan_database, plan_contexts):
    context = plan_contexts["primary"]
    async with TenantUnitOfWork(plan_database, context) as uow:
        session = await uow.sessions.create("Plan")
        scoped = context.for_session(session.id)
        source = await MemoryRepository(uow.conn, scoped, plan_database.dialect).save(
            MemoryEntry(content="Deliver the change", type="chat_message", role="user", turn_index=1)
        )
        snapshot = await uow.plans.for_context(scoped).create(
            plan_id=str(uuid4()),
            source_message_id=source.id,
            trigger_mode=PlanTriggerMode.EXPLICIT,
            draft=plan_draft(),
        )

        assert uow.plans.connection is uow.conn

    assert snapshot.status is PlanStatus.AWAITING_APPROVAL
    assert snapshot.current_version == 1
    assert snapshot.approved_version is None
    assert [step.logical_step_key for step in snapshot.current.steps] == ["inspect", "verify"]
    assert snapshot.current.dependencies == {
        snapshot.current.steps[1].step_id: (snapshot.current.steps[0].step_id,)
    }
    assert len(snapshot.current.content_digest) == 64


@pytest.mark.asyncio
async def test_create_plan_hydrates_multiple_dependencies_in_canonical_order(
    plan_database,
    plan_contexts,
):
    context = plan_contexts["primary"]
    draft = plan_draft()
    draft.steps.insert(
        1,
        PlanDraftStep(
            logical_step_key="lint",
            title="Lint",
            description="Lint the implementation.",
            expected_outcome="Static checks pass.",
            depends_on=[],
            max_attempts=2,
        ),
    )
    draft.steps[2].depends_on = ["lint", "inspect"]

    async with TenantUnitOfWork(plan_database, context) as uow:
        session = await uow.sessions.create("Canonical dependencies")
        scoped = context.for_session(session.id)
        source = await MemoryRepository(uow.conn, scoped, plan_database.dialect).save(
            MemoryEntry(
                content="Deliver with linting",
                type="chat_message",
                role="user",
                turn_index=1,
            )
        )
        snapshot = await uow.plans.for_context(scoped).create(
            plan_id=str(uuid4()),
            source_message_id=source.id,
            trigger_mode=PlanTriggerMode.EXPLICIT,
            draft=draft,
        )

    step_ids = {
        step.logical_step_key: step.step_id for step in snapshot.current.steps
    }
    assert snapshot.current.dependencies[step_ids["verify"]] == (
        step_ids["inspect"],
        step_ids["lint"],
    )


@pytest.mark.asyncio
async def test_hydrated_plan_dependencies_are_deeply_immutable(plan_database, seeded_plan):
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        snapshot = await uow.plans.for_context(seeded_plan.context).get(seeded_plan.plan_id)

    assert snapshot is not None
    assert snapshot.current is snapshot.versions[0]
    dependent_step_id = snapshot.current.steps[1].step_id
    with pytest.raises(TypeError):
        snapshot.current.dependencies[dependent_step_id] = ()  # type: ignore[index]


@pytest.mark.asyncio
async def test_create_plan_rolls_back_all_rows_when_step_materialization_fails(
    plan_database,
    plan_contexts,
    monkeypatch,
):
    root = plan_contexts["primary"]
    plan_id = str(uuid4())
    duplicate_step_id = uuid4()
    monkeypatch.setattr(
        "multiclaw.storage.repositories.plans.uuid4",
        lambda: duplicate_step_id,
    )

    async with TenantUnitOfWork(plan_database, root) as uow:
        session = await uow.sessions.create("Atomic plan")
        context = root.for_session(session.id)
        source = await MemoryRepository(uow.conn, context, plan_database.dialect).save(
            MemoryEntry(content="Deliver atomically", type="chat_message", role="user", turn_index=1)
        )
        with pytest.raises(IntegrityError):
            await uow.plans.for_context(context).create(
                plan_id=plan_id,
                source_message_id=source.id,
                trigger_mode=PlanTriggerMode.EXPLICIT,
                draft=plan_draft(),
            )

    assert await count_plan_rows(plan_database, context, plan_id) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_create_plan_preserves_primary_error_when_savepoint_rollback_fails():
    primary = RuntimeError("insert failed")
    rollback_error = RuntimeError("rollback failed")
    connection = FailingCreateConnection(primary, FailingSavepoint(rollback_error))
    context = TenantContext(
        tenant_id=str(uuid4()),
        workspace_id=str(uuid4()),
        session_id=str(uuid4()),
    )
    repository = PlanRepository(
        connection,  # type: ignore[arg-type]
        FixedNowDialect(),  # type: ignore[arg-type]
        context,
        PlanningSettings(),
    )

    with pytest.raises(RuntimeError) as raised:
        await repository.create(
            plan_id=str(uuid4()),
            source_message_id=str(uuid4()),
            trigger_mode=PlanTriggerMode.EXPLICIT,
            draft=plan_draft(),
        )

    assert raised.value is primary
    assert getattr(primary, "__notes__", []) == [
        "savepoint rollback cleanup failed: RuntimeError: rollback failed"
    ]


@pytest.mark.asyncio
async def test_plan_lookup_hides_foreign_tenant_workspace_and_session(plan_database, seeded_plan):
    for foreign in seeded_plan.foreign_contexts:
        async with TenantUnitOfWork(plan_database, foreign) as uow:
            assert await uow.plans.for_context(foreign).get(seeded_plan.plan_id) is None
            assert await uow.plans.for_context(foreign).list_for_session() == []


@pytest.mark.asyncio
async def test_list_for_session_uses_one_query_without_hydrating_history(
    plan_database,
    seeded_plan,
    monkeypatch,
):
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        repository = uow.plans.for_context(seeded_plan.context)
        snapshot = await repository.get(seeded_plan.plan_id)
        assert snapshot is not None
        for plan_version in range(2, 7):
            snapshot = await repository.append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=snapshot.aggregate_version,
                draft=plan_draft(f"Historical revision {plan_version}"),
                parent_version=snapshot.current_version,
                revision_feedback=f"Revision {plan_version}",
                supersedes={
                    step.logical_step_key: step.step_id for step in snapshot.current.steps
                },
            )

    def fail_if_hydrated(*_args, **_kwargs):
        raise AssertionError("list_for_session must not hydrate PlanSnapshot history")

    monkeypatch.setattr(PlanRepository, "_hydrate_version", fail_if_hydrated)
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        connection = CountingConnection(uow.conn)
        repository = PlanRepository(
            connection,  # type: ignore[arg-type]
            plan_database.dialect,
            seeded_plan.context,
            PlanningSettings(),
        )
        summaries = await repository.list_for_session()

    assert connection.execute_calls == 1
    assert len(summaries) == 1
    assert isinstance(summaries[0], PlanSummary)
    assert summaries[0].current_version == 6
    assert summaries[0].latest_run_id is None
    assert summaries[0].latest_run_status is None


@pytest.mark.asyncio
async def test_list_for_session_selects_latest_run_deterministically(
    plan_database,
    seeded_plan,
):
    lower_run_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    higher_run_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        await uow.conn.execute(
            insert(agent_runs),
            [
                agent_run_row(
                    seeded_plan.context,
                    plan_id=seeded_plan.plan_id,
                    run_id=lower_run_id,
                    status=RunStatus.COMPLETED,
                    created_at=20,
                ),
                agent_run_row(
                    seeded_plan.context,
                    plan_id=seeded_plan.plan_id,
                    run_id=higher_run_id,
                    status=RunStatus.RUNNING,
                    created_at=20,
                ),
            ],
        )

        summaries = await uow.plans.for_context(seeded_plan.context).list_for_session()

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.plan_id == seeded_plan.plan_id
    assert summary.session_id == seeded_plan.context.session_id
    assert summary.status is PlanStatus.AWAITING_APPROVAL
    assert summary.current_version == 1
    assert summary.approved_version is None
    assert summary.aggregate_version == seeded_plan.aggregate_version
    assert summary.latest_run_id == higher_run_id
    assert summary.latest_run_status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_list_for_session_orders_equal_timestamps_by_plan_id_desc(
    plan_database,
    seeded_plan,
):
    extra_plan_ids = (
        "11111111-1111-1111-1111-111111111111",
        "99999999-9999-9999-9999-999999999999",
    )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        repository = uow.plans.for_context(seeded_plan.context)
        existing = await repository.get(seeded_plan.plan_id)
        assert existing is not None
        for plan_id in extra_plan_ids:
            await repository.create(
                plan_id=plan_id,
                source_message_id=existing.source_message_id,
                trigger_mode=PlanTriggerMode.EXPLICIT,
                draft=plan_draft(f"Plan {plan_id}"),
            )
        await uow.conn.execute(
            update(agent_plans)
            .where(
                agent_plans.c.tenant_id == seeded_plan.context.tenant_id,
                agent_plans.c.workspace_id == seeded_plan.context.workspace_id,
                agent_plans.c.session_id == seeded_plan.context.session_id,
            )
            .values(created_at=100)
        )

        summaries = await repository.list_for_session()

    expected_ids = sorted((seeded_plan.plan_id, *extra_plan_ids), reverse=True)
    assert [summary.plan_id for summary in summaries] == expected_ids


@pytest.mark.asyncio
async def test_append_version_never_updates_old_rows(plan_database, seeded_plan):
    before = await dump_plan_version_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
        1,
    )
    revised = plan_draft("Deliver the change with extra verification")
    revised.steps[1].description = "Verify both database backends."

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        snapshot = await uow.plans.for_context(seeded_plan.context).append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=seeded_plan.aggregate_version,
            draft=revised,
            parent_version=1,
            revision_feedback="Cover both databases",
            supersedes={
                "inspect": seeded_plan.step_ids["inspect"],
                "verify": seeded_plan.step_ids["verify"],
            },
        )

    after = await dump_plan_version_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
        1,
    )
    assert after == before
    assert snapshot.current_version == 2
    assert snapshot.current.parent_version == 1
    assert snapshot.current.steps[1].supersedes_step_id == seeded_plan.step_ids["verify"]


@pytest.mark.asyncio
async def test_append_version_preserves_last_approved_version(plan_database, seeded_plan):
    approved_aggregate_version = seeded_plan.aggregate_version + 1
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        await uow.conn.execute(
            update(agent_plans)
            .where(
                agent_plans.c.tenant_id == seeded_plan.context.tenant_id,
                agent_plans.c.workspace_id == seeded_plan.context.workspace_id,
                agent_plans.c.session_id == seeded_plan.context.session_id,
                agent_plans.c.id == seeded_plan.plan_id,
            )
            .values(
                status=PlanStatus.APPROVED.value,
                approved_version=1,
                version=approved_aggregate_version,
            )
        )

        snapshot = await uow.plans.for_context(seeded_plan.context).append_version(
            plan_id=seeded_plan.plan_id,
            expected_version=approved_aggregate_version,
            draft=plan_draft("Revise the approved plan"),
            parent_version=1,
            revision_feedback="Add a revision",
            supersedes={
                "inspect": seeded_plan.step_ids["inspect"],
                "verify": seeded_plan.step_ids["verify"],
            },
        )

    assert snapshot.status is PlanStatus.AWAITING_APPROVAL
    assert snapshot.current_version == 2
    assert snapshot.approved_version == 1


@pytest.mark.asyncio
async def test_append_version_rejects_stale_expected_version_without_writes(
    plan_database,
    seeded_plan,
):
    before = await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    )
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        with pytest.raises(ValueError, match="version conflict"):
            await uow.plans.for_context(seeded_plan.context).append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=seeded_plan.aggregate_version + 1,
                draft=plan_draft("Stale revision"),
                parent_version=1,
                revision_feedback="This writer is stale",
                supersedes={},
            )

    after = await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    )
    assert after == before


@pytest.mark.asyncio
async def test_append_version_rolls_back_new_rows_when_late_cas_loses(
    plan_database,
    seeded_plan,
):
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        repository = PlanRepository(
            LateCasFailureConnection(uow.conn),  # type: ignore[arg-type]
            plan_database.dialect,
            seeded_plan.context,
            PlanningSettings(),
        )
        with pytest.raises(ValueError, match="version conflict"):
            await repository.append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=seeded_plan.aggregate_version,
                draft=plan_draft("Losing revision"),
                parent_version=1,
                revision_feedback="Lose after child inserts",
                supersedes={
                    "inspect": seeded_plan.step_ids["inspect"],
                    "verify": seeded_plan.step_ids["verify"],
                },
            )

        snapshot = await uow.plans.for_context(seeded_plan.context).get(seeded_plan.plan_id)
        assert snapshot is not None
        assert snapshot.current_version == 1
        assert [version.plan_version for version in snapshot.versions] == [1]

    assert await count_plan_rows(
        plan_database,
        seeded_plan.context,
        seeded_plan.plan_id,
    ) == (1, 1, 2, 1)


@pytest.mark.asyncio
async def test_concurrent_sqlite_appends_have_one_winner_and_a_contiguous_chain(
    plan_database,
    seeded_plan,
):
    async def append_revision(label: str):
        async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
            return await uow.plans.for_context(seeded_plan.context).append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=seeded_plan.aggregate_version,
                draft=plan_draft(f"Concurrent revision {label}"),
                parent_version=1,
                revision_feedback=f"Writer {label}",
                supersedes={
                    "inspect": seeded_plan.step_ids["inspect"],
                    "verify": seeded_plan.step_ids["verify"],
                },
            )

    outcomes = await asyncio.gather(
        append_revision("a"),
        append_revision("b"),
        return_exceptions=True,
    )

    winners = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], ValueError)
    assert "version conflict" in str(losers[0])

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        snapshot = await uow.plans.for_context(seeded_plan.context).get(seeded_plan.plan_id)
    assert snapshot is not None
    assert snapshot.current_version == 2
    assert snapshot.aggregate_version == seeded_plan.aggregate_version + 1
    assert [version.plan_version for version in snapshot.versions] == [1, 2]
    assert snapshot.versions[1].parent_version == 1


@pytest.mark.asyncio
async def test_root_plan_repository_rejects_direct_data_calls(plan_database, plan_contexts):
    async with TenantUnitOfWork(plan_database, plan_contexts["primary"]) as uow:
        with pytest.raises(ValueError, match="PlanRepository requires session scope"):
            await uow.plans.get(str(uuid4()))
        with pytest.raises(ValueError, match="PlanRepository requires session scope"):
            await uow.plans.list_for_session()
        with pytest.raises(ValueError, match="PlanRepository requires session scope"):
            uow.plans.for_context(plan_contexts["primary"])


@pytest.mark.asyncio
async def test_append_version_rejects_supersedes_from_another_plan(plan_database, seeded_plan):
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        source = await MemoryRepository(
            uow.conn,
            seeded_plan.context,
            plan_database.dialect,
        ).save(
            MemoryEntry(content="Another plan", type="chat_message", role="user", turn_index=2)
        )
        other = await uow.plans.for_context(seeded_plan.context).create(
            plan_id=str(uuid4()),
            source_message_id=source.id,
            trigger_mode=PlanTriggerMode.EXPLICIT,
            draft=plan_draft("Another objective"),
        )
        with pytest.raises(ValueError, match="supersedes_step_id"):
            await uow.plans.for_context(seeded_plan.context).append_version(
                plan_id=seeded_plan.plan_id,
                expected_version=seeded_plan.aggregate_version,
                draft=plan_draft("Revised objective"),
                parent_version=1,
                revision_feedback="Reuse the wrong step",
                supersedes={"inspect": other.current.steps[0].step_id},
            )


@pytest.mark.asyncio
async def test_get_rejects_non_list_constraints_as_corrupt(plan_database, seeded_plan):
    async with plan_database.write_transaction() as conn:
        await conn.execute(
            update(agent_plan_versions)
            .where(
                agent_plan_versions.c.tenant_id == seeded_plan.context.tenant_id,
                agent_plan_versions.c.workspace_id == seeded_plan.context.workspace_id,
                agent_plan_versions.c.session_id == seeded_plan.context.session_id,
                agent_plan_versions.c.plan_id == seeded_plan.plan_id,
                agent_plan_versions.c.plan_version == 1,
            )
            .values(constraints_json='{"not": "a list"}')
        )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        with pytest.raises(ValueError, match="constraints_json"):
            await uow.plans.for_context(seeded_plan.context).get(seeded_plan.plan_id)


@pytest.mark.asyncio
async def test_get_rejects_plan_with_missing_step_rows(plan_database, seeded_plan):
    async with plan_database.write_transaction() as conn:
        await conn.execute(
            delete(agent_plan_step_dependencies).where(
                agent_plan_step_dependencies.c.tenant_id == seeded_plan.context.tenant_id,
                agent_plan_step_dependencies.c.workspace_id == seeded_plan.context.workspace_id,
                agent_plan_step_dependencies.c.session_id == seeded_plan.context.session_id,
                agent_plan_step_dependencies.c.plan_id == seeded_plan.plan_id,
                agent_plan_step_dependencies.c.plan_version == 1,
            )
        )
        await conn.execute(
            delete(agent_plan_steps).where(
                agent_plan_steps.c.tenant_id == seeded_plan.context.tenant_id,
                agent_plan_steps.c.workspace_id == seeded_plan.context.workspace_id,
                agent_plan_steps.c.session_id == seeded_plan.context.session_id,
                agent_plan_steps.c.plan_id == seeded_plan.plan_id,
                agent_plan_steps.c.plan_version == 1,
                agent_plan_steps.c.step_id == seeded_plan.step_ids["verify"],
            )
        )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        with pytest.raises(ValueError, match="corrupt"):
            await uow.plans.for_context(seeded_plan.context).get(seeded_plan.plan_id)


@pytest.mark.asyncio
async def test_get_rejects_plan_with_missing_dependency_rows(plan_database, seeded_plan):
    async with plan_database.write_transaction() as conn:
        await conn.execute(
            delete(agent_plan_step_dependencies).where(
                agent_plan_step_dependencies.c.tenant_id == seeded_plan.context.tenant_id,
                agent_plan_step_dependencies.c.workspace_id == seeded_plan.context.workspace_id,
                agent_plan_step_dependencies.c.session_id == seeded_plan.context.session_id,
                agent_plan_step_dependencies.c.plan_id == seeded_plan.plan_id,
                agent_plan_step_dependencies.c.plan_version == 1,
            )
        )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        with pytest.raises(ValueError, match="corrupt"):
            await uow.plans.for_context(seeded_plan.context).get(seeded_plan.plan_id)


@pytest.mark.asyncio
async def test_get_rejects_plan_with_mismatched_step_digest(plan_database, seeded_plan):
    async with plan_database.write_transaction() as conn:
        await conn.execute(
            update(agent_plan_steps)
            .where(
                agent_plan_steps.c.tenant_id == seeded_plan.context.tenant_id,
                agent_plan_steps.c.workspace_id == seeded_plan.context.workspace_id,
                agent_plan_steps.c.session_id == seeded_plan.context.session_id,
                agent_plan_steps.c.plan_id == seeded_plan.plan_id,
                agent_plan_steps.c.plan_version == 1,
                agent_plan_steps.c.step_id == seeded_plan.step_ids["inspect"],
            )
            .values(definition_digest="f" * 64)
        )

    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        with pytest.raises(ValueError, match="definition_digest"):
            await uow.plans.for_context(seeded_plan.context).get(seeded_plan.plan_id)


@pytest.mark.asyncio
async def test_latest_step_attempts_returns_only_latest_rows_for_the_scoped_run(
    plan_database,
    seeded_plan,
):
    run_id = str(uuid4())
    inspect_step_id = seeded_plan.step_ids["inspect"]
    verify_step_id = seeded_plan.step_ids["verify"]
    async with TenantUnitOfWork(plan_database, seeded_plan.context) as uow:
        scope = {
            "tenant_id": seeded_plan.context.tenant_id,
            "workspace_id": seeded_plan.context.workspace_id,
            "session_id": seeded_plan.context.session_id,
        }
        await uow.conn.execute(
            insert(agent_runs).values(
                run_id=run_id,
                **scope,
                plan_id=seeded_plan.plan_id,
                initial_plan_version=1,
                active_plan_version=1,
                cancel_requested_at=None,
                run_status="running",
                runtime_instance_id="runtime-a",
                lease_owner="runtime-a",
                fencing_token=1,
                lease_expires_at=10_000,
                heartbeat_at=1,
                schema_version=1,
                version=1,
                created_at=1,
                updated_at=1,
                finished_at=None,
            )
        )
        await uow.conn.execute(
            insert(agent_plan_step_runs),
            [
                {
                    **scope,
                    "plan_id": seeded_plan.plan_id,
                    "plan_version": 1,
                    "step_id": inspect_step_id,
                    "step_run_id": str(uuid4()),
                    "run_id": run_id,
                    "attempt": 1,
                    "status": PlanStepRunStatus.FAILED_RETRYABLE.value,
                    "result_summary": None,
                    "result_ref": None,
                    "result_digest": None,
                    "error_code": "retry",
                    "error_detail_redacted": "retry safely",
                    "reused_from_step_run_id": None,
                    "version": 1,
                    "started_at": 2,
                    "finished_at": 3,
                },
                {
                    **scope,
                    "plan_id": seeded_plan.plan_id,
                    "plan_version": 1,
                    "step_id": inspect_step_id,
                    "step_run_id": str(uuid4()),
                    "run_id": run_id,
                    "attempt": 2,
                    "status": PlanStepRunStatus.SUCCEEDED.value,
                    "result_summary": "inspected",
                    "result_ref": "memory://inspect",
                    "result_digest": "a" * 64,
                    "error_code": None,
                    "error_detail_redacted": None,
                    "reused_from_step_run_id": None,
                    "version": 2,
                    "started_at": 4,
                    "finished_at": 5,
                },
                {
                    **scope,
                    "plan_id": seeded_plan.plan_id,
                    "plan_version": 1,
                    "step_id": verify_step_id,
                    "step_run_id": str(uuid4()),
                    "run_id": run_id,
                    "attempt": 1,
                    "status": PlanStepRunStatus.RUNNING.value,
                    "result_summary": None,
                    "result_ref": None,
                    "result_digest": None,
                    "error_code": None,
                    "error_detail_redacted": None,
                    "reused_from_step_run_id": None,
                    "version": 1,
                    "started_at": 6,
                    "finished_at": None,
                },
            ],
        )

        latest = await uow.plans.for_context(seeded_plan.context).latest_step_attempts(
            plan_id=seeded_plan.plan_id,
            plan_version=1,
            run_id=run_id,
        )

    assert latest[inspect_step_id].attempt == 2
    assert latest[inspect_step_id].status is PlanStepRunStatus.SUCCEEDED
    assert latest[verify_step_id].attempt == 1
    assert latest[verify_step_id].status is PlanStepRunStatus.RUNNING
