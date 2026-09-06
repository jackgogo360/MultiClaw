import asyncio
import os
import sqlite3
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError

from alembic import command
from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, PlanningSettings, Settings
from multiclaw.memory import MemoryEntry
from multiclaw.planner import (
    PlanDecisionAction,
    PlanDecisionIdempotencyError,
    PlanDecisionRequest,
    PlanDraft,
    PlanDraftStep,
    PlanNotFoundError,
    PlanStatus,
    PlanStepRunStatus,
    PlanSummary,
    PlanTriggerMode,
    PlanValidationError,
    PlanVersionConflictError,
)
from multiclaw.planner.generator import PlanGenerator
from multiclaw.planner.models import (
    PlanExecutionBlocked,
    PlanRevisionContext,
    PlanRevisionLimitError,
    PlanStepResultDocument,
)
from multiclaw.planner.service import MaterializeInitialPlan, PlanningService
from multiclaw.planner.validation import sanitize_plan_text
from multiclaw.storage import Database
from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.repositories.plans import PlanRepository
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.storage.schema import (
    agent_plan_decisions,
    agent_plan_step_dependencies,
    agent_plan_step_runs,
    agent_plan_steps,
    agent_plan_versions,
    agent_plans,
    agent_runs,
    execution_checkpoints,
    memory_entries,
)
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow import RunStatus
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import CheckpointPhase, RunLease, StaleFenceError

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


def planning_settings(*, max_revisions: int = 5) -> Settings:
    return Settings(
        _config_file="/nonexistent",
        planning={"max_revisions": max_revisions},
    )


@dataclass(frozen=True, slots=True)
class SeededSourceMessage:
    context: TenantContext
    message_id: str

    @property
    def session_id(self) -> str:
        assert self.context.session_id is not None
        return self.context.session_id


class FakePlanGenerator:
    def __init__(self, next_draft: PlanDraft | None = None) -> None:
        self.next_draft = next_draft or plan_draft()
        self.calls: list[tuple[str, object | None]] = []
        self.failure: BaseException | None = None
        self.on_generate: Any = None

    async def generate(self, objective: str, revision=None, **_limits):
        self.calls.append((objective, revision))
        if self.failure is not None:
            raise self.failure
        if self.on_generate is not None:
            callback = self.on_generate
            self.on_generate = None
            await callback()
        return self.next_draft


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


@pytest.fixture
async def seeded_source_message(
    plan_database: Database,
    plan_contexts: dict[str, TenantContext],
) -> SeededSourceMessage:
    root = plan_contexts["primary"]
    async with TenantUnitOfWork(plan_database, root) as uow:
        session = await uow.sessions.create("Atomic plan")
        context = root.for_session(session.id)
        assert uow.conn is not None
        message = await MemoryRepository(
            uow.conn,
            context,
            plan_database.dialect,
        ).save(
            MemoryEntry(
                content="Deliver the change",
                type="chat_message",
                role="user",
                turn_index=1,
            )
        )
    return SeededSourceMessage(context=context, message_id=message.id)


@dataclass(slots=True)
class SeededWaitingPlan:
    database: Database
    context: TenantContext
    plan_id: str
    aggregate_version: int
    generator: FakePlanGenerator
    workflow: WorkflowCoordinator
    service: PlanningService

    @property
    def tenant_id(self) -> str:
        return self.context.tenant_id

    async def load_plan(self):
        async with TenantUnitOfWork(self.database, self.context) as uow:
            snapshot = await uow.plans.for_context(self.context).get(self.plan_id)
        assert snapshot is not None
        return snapshot

    async def load_run(self):
        run = await self.workflow.get_run(self.context)
        assert run is not None
        return run


@pytest.fixture
async def seeded_waiting_plan(
    plan_database: Database,
    seeded_source_message: SeededSourceMessage,
) -> SeededWaitingPlan:
    settings = planning_settings()
    generator = FakePlanGenerator()
    workflow = WorkflowCoordinator(plan_database, settings=settings)
    service = PlanningService(
        plan_database,
        settings=settings,
        generator=cast(PlanGenerator, generator),
        workflow=workflow,
    )
    context = seeded_source_message.context.for_run(
        seeded_source_message.session_id,
        str(uuid4()),
    )
    materialized = await service.materialize_initial(
        MaterializeInitialPlan(
            context=context,
            runtime_instance_id="runtime-1",
            source_message_id=seeded_source_message.message_id,
            assistant_turn_index=2,
            trigger_mode=PlanTriggerMode.EXPLICIT,
            draft=plan_draft(),
        )
    )
    return SeededWaitingPlan(
        database=plan_database,
        context=context,
        plan_id=materialized.plan.plan_id,
        aggregate_version=materialized.plan.aggregate_version,
        generator=generator,
        workflow=workflow,
        service=service,
    )


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


class ReusedAttemptIntegrityFailureConnection:
    def __init__(self, connection, error: IntegrityError) -> None:
        self.connection = connection
        self.error = error
        self.insert_failed = False

    async def begin_nested(self):
        return await self.connection.begin_nested()

    async def execute(self, statement, *args, **kwargs):
        if self.insert_failed:
            raise AssertionError("replay query attempted after non-duplicate error")
        if (
            getattr(statement, "is_insert", False)
            and statement.table is agent_plan_step_runs
        ):
            self.insert_failed = True
            raise self.error
        return await self.connection.execute(statement, *args, **kwargs)


class SQLiteConstraintError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.sqlite_errorcode = code
        self.message = message

    def __str__(self) -> str:
        return self.message


class MySQLConstraintError(Exception):
    def __init__(self, errno: int, message: str) -> None:
        self.args = (errno, message)


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


def _duplicate_classifier_repository(dialect) -> PlanRepository:
    context = TenantContext(
        tenant_id=str(uuid4()),
        workspace_id=str(uuid4()),
        session_id=str(uuid4()),
        run_id=str(uuid4()),
    )
    return PlanRepository(  # type: ignore[arg-type]
        None,
        dialect,
        context,
        PlanningSettings(),
    )


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    (
        (
            sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            (
                "UNIQUE constraint failed: agent_plan_step_runs.tenant_id, "
                "agent_plan_step_runs.workspace_id, agent_plan_step_runs.session_id, "
                "agent_plan_step_runs.run_id, agent_plan_step_runs.step_id, "
                "agent_plan_step_runs.attempt"
            ),
            True,
        ),
        (
            sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
            (
                "UNIQUE constraint failed: agent_plan_step_runs.tenant_id, "
                "agent_plan_step_runs.workspace_id, agent_plan_step_runs.session_id, "
                "agent_plan_step_runs.run_id, agent_plan_step_runs.step_id, "
                "agent_plan_step_runs.attempt"
            ),
            True,
        ),
        (sqlite3.SQLITE_CONSTRAINT_CHECK, "CHECK constraint failed", False),
        (sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY, "FOREIGN KEY constraint failed", False),
        (sqlite3.SQLITE_CONSTRAINT_NOTNULL, "NOT NULL constraint failed", False),
        (
            sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            (
                "UNIQUE constraint failed: agent_plan_step_runs.tenant_id, "
                "agent_plan_step_runs.workspace_id, agent_plan_step_runs.session_id, "
                "agent_plan_step_runs.plan_id, agent_plan_step_runs.run_id, "
                "agent_plan_step_runs.step_run_id"
            ),
            False,
        ),
    ),
)
def test_reused_step_attempt_duplicate_classifier_scopes_sqlite_constraint(
    code: int,
    message: str,
    expected: bool,
) -> None:
    error = IntegrityError("INSERT", {}, SQLiteConstraintError(code, message))

    assert (
        _duplicate_classifier_repository(SQLiteDialect())._is_step_attempt_duplicate(
            error
        )
        is expected
    )


@pytest.mark.parametrize(
    ("errno", "message", "expected"),
    (
        (
            1062,
            "Duplicate entry 'x' for key 'uq_agent_plan_step_runs_scope_run_step_attempt'",
            True,
        ),
        (
            1062,
            "Duplicate entry 'x' for key `db`.`uq_agent_plan_step_runs_scope_run_step_attempt`",
            True,
        ),
        (1452, "Cannot add or update a child row", False),
        (1062, "Duplicate entry 'x' for key 'PRIMARY'", False),
        (1062, "Duplicate entry 'x' for key 'other_unique_key'", False),
    ),
)
def test_reused_step_attempt_duplicate_classifier_scopes_mysql_constraint(
    errno: int,
    message: str,
    expected: bool,
) -> None:
    error = IntegrityError("INSERT", {}, MySQLConstraintError(errno, message))

    assert (
        _duplicate_classifier_repository(MySQLDialect())._is_step_attempt_duplicate(
            error
        )
        is expected
    )


@pytest.fixture
async def seeded_plan(
    plan_database: Database,
    plan_contexts: dict[str, TenantContext],
) -> SeededPlan:
    root = plan_contexts["primary"]
    async with TenantUnitOfWork(plan_database, root) as uow:
        session = await uow.sessions.create("Seeded plan")
        context = root.for_session(session.id)
        assert uow.conn is not None
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


async def count_all_rows(database: Database, table) -> int:
    async with database.connect() as conn:
        return int(
            (await conn.execute(select(func.count()).select_from(table))).scalar_one()
        )


async def load_message(database: Database, message_id: str) -> MemoryEntry:
    async with database.connect() as conn:
        row = (
            await conn.execute(
                select(memory_entries).where(memory_entries.c.id == message_id)
            )
        ).mappings().one()
    return MemoryEntry.from_row(dict(row))


def materialize_request(source: SeededSourceMessage) -> MaterializeInitialPlan:
    return MaterializeInitialPlan(
        context=source.context.for_run(source.session_id, str(uuid4())),
        runtime_instance_id="runtime-1",
        source_message_id=source.message_id,
        assistant_turn_index=2,
        trigger_mode=PlanTriggerMode.EXPLICIT,
        draft=plan_draft(),
    )


def approve_waiting_request(
    seeded: SeededWaitingPlan,
    decision_id: str,
) -> PlanDecisionRequest:
    return PlanDecisionRequest(
        decision_id=decision_id,
        plan_id=seeded.plan_id,
        plan_version=1,
        expected_version=seeded.aggregate_version,
        action=PlanDecisionAction.APPROVE,
        feedback=None,
    )


@pytest.mark.asyncio
async def test_initial_materialization_is_one_transaction(
    plan_database: Database,
    seeded_source_message: SeededSourceMessage,
):
    service = PlanningService(plan_database, settings=planning_settings())
    request = materialize_request(seeded_source_message)

    materialized = await service.materialize_initial(request)
    run = await WorkflowCoordinator(
        plan_database,
        settings=planning_settings(),
    ).get_run(request.context)
    checkpoint = await WorkflowCoordinator(
        plan_database,
        settings=planning_settings(),
    ).get_latest_checkpoint(request.context)
    message = await load_message(plan_database, materialized.reference_message_id)

    assert run is not None
    assert run.status is RunStatus.AWAITING_USER
    assert (run.plan_id, run.initial_plan_version, run.active_plan_version) == (
        materialized.plan.plan_id,
        1,
        1,
    )
    assert checkpoint is not None
    assert checkpoint.phase == CheckpointPhase.PLAN_AWAITING_APPROVAL.value
    assert message.content == ""
    assert message.role == "assistant"
    assert message.turn_index == 2
    assert message.metadata["parts"] == [
        {
            "type": "data-plan-created",
            "data": materialized.reference.model_dump(mode="json"),
        }
    ]
    assert materialized.run == run
    assert materialized.event.data == materialized.reference.model_dump(mode="json")


@pytest.mark.asyncio
async def test_materialization_plan_create_failure_leaves_no_partial_rows(
    plan_database: Database,
    seeded_source_message: SeededSourceMessage,
    monkeypatch,
):
    service = PlanningService(plan_database, settings=planning_settings())
    message_count = await count_all_rows(plan_database, memory_entries)
    original_create = PlanRepository.create
    plan_write_reached = False

    async def fail_after_plan_create(repository, *args, **kwargs):
        nonlocal plan_write_reached
        await original_create(repository, *args, **kwargs)
        plan_write_reached = True
        raise RuntimeError("plan create failure")

    monkeypatch.setattr(PlanRepository, "create", fail_after_plan_create)

    with pytest.raises(RuntimeError, match="plan create failure"):
        await service.materialize_initial(materialize_request(seeded_source_message))

    assert plan_write_reached is True
    assert await count_all_rows(plan_database, agent_plans) == 0
    assert await count_all_rows(plan_database, agent_plan_versions) == 0
    assert await count_all_rows(plan_database, agent_plan_steps) == 0
    assert await count_all_rows(plan_database, agent_plan_step_dependencies) == 0
    assert await count_all_rows(plan_database, agent_runs) == 0
    assert await count_all_rows(plan_database, execution_checkpoints) == 0
    assert await count_all_rows(plan_database, memory_entries) == message_count


@pytest.mark.asyncio
async def test_materialization_run_creation_failure_rolls_back_plan_rows(
    plan_database: Database,
    seeded_source_message: SeededSourceMessage,
    monkeypatch,
):
    service = PlanningService(plan_database, settings=planning_settings())
    message_count = await count_all_rows(plan_database, memory_entries)
    original_create_run = WorkflowRepository._create_run
    run_write_reached = False

    async def fail_after_run_create(repository, *args, **kwargs):
        nonlocal run_write_reached
        await original_create_run(repository, *args, **kwargs)
        run_write_reached = True
        raise RuntimeError("run creation failure")

    monkeypatch.setattr(WorkflowRepository, "_create_run", fail_after_run_create)

    with pytest.raises(RuntimeError, match="run creation failure"):
        await service.materialize_initial(materialize_request(seeded_source_message))

    assert run_write_reached is True
    assert await count_all_rows(plan_database, agent_plans) == 0
    assert await count_all_rows(plan_database, agent_plan_versions) == 0
    assert await count_all_rows(plan_database, agent_plan_steps) == 0
    assert await count_all_rows(plan_database, agent_plan_step_dependencies) == 0
    assert await count_all_rows(plan_database, agent_runs) == 0
    assert await count_all_rows(plan_database, execution_checkpoints) == 0
    assert await count_all_rows(plan_database, memory_entries) == message_count


@pytest.mark.asyncio
async def test_materialization_checkpoint_insert_failure_rolls_back_plan_and_run(
    plan_database: Database,
    seeded_source_message: SeededSourceMessage,
    monkeypatch,
):
    service = PlanningService(plan_database, settings=planning_settings())
    message_count = await count_all_rows(plan_database, memory_entries)
    original_insert_checkpoint = WorkflowRepository._insert_checkpoint
    checkpoint_write_reached = False

    async def fail_after_checkpoint_insert(repository, *args, **kwargs):
        nonlocal checkpoint_write_reached
        await original_insert_checkpoint(repository, *args, **kwargs)
        checkpoint_write_reached = True
        raise RuntimeError("approval checkpoint insertion failure")

    monkeypatch.setattr(
        WorkflowRepository,
        "_insert_checkpoint",
        fail_after_checkpoint_insert,
    )

    with pytest.raises(RuntimeError, match="approval checkpoint insertion failure"):
        await service.materialize_initial(materialize_request(seeded_source_message))

    assert checkpoint_write_reached is True
    assert await count_all_rows(plan_database, agent_plans) == 0
    assert await count_all_rows(plan_database, agent_plan_versions) == 0
    assert await count_all_rows(plan_database, agent_plan_steps) == 0
    assert await count_all_rows(plan_database, agent_plan_step_dependencies) == 0
    assert await count_all_rows(plan_database, agent_runs) == 0
    assert await count_all_rows(plan_database, execution_checkpoints) == 0
    assert await count_all_rows(plan_database, memory_entries) == message_count


@pytest.mark.asyncio
async def test_materialization_reference_insert_failure_rolls_back_every_write(
    plan_database: Database,
    seeded_source_message: SeededSourceMessage,
    monkeypatch,
):
    service = PlanningService(plan_database, settings=planning_settings())
    message_count = await count_all_rows(plan_database, memory_entries)
    original_persist_reference = service._persist_reference
    reference_write_reached = False

    async def fail_after_reference_insert(*args, **kwargs):
        nonlocal reference_write_reached
        await original_persist_reference(*args, **kwargs)
        reference_write_reached = True
        raise RuntimeError("reference failure")

    monkeypatch.setattr(service, "_persist_reference", fail_after_reference_insert)

    with pytest.raises(RuntimeError, match="reference failure"):
        await service.materialize_initial(materialize_request(seeded_source_message))

    assert reference_write_reached is True
    assert await count_all_rows(plan_database, agent_plans) == 0
    assert await count_all_rows(plan_database, agent_plan_versions) == 0
    assert await count_all_rows(plan_database, agent_plan_steps) == 0
    assert await count_all_rows(plan_database, agent_plan_step_dependencies) == 0
    assert await count_all_rows(plan_database, agent_runs) == 0
    assert await count_all_rows(plan_database, execution_checkpoints) == 0
    assert await count_all_rows(plan_database, memory_entries) == message_count


@pytest.mark.asyncio
async def test_revision_keeps_active_version_and_old_rows_immutable(
    seeded_waiting_plan: SeededWaitingPlan,
):
    before = await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    )
    revised = plan_draft()
    revised.steps[1].description = "Verify SQLite and MySQL."
    seeded_waiting_plan.generator.next_draft = revised

    result = await seeded_waiting_plan.service.decide(
        PlanDecisionRequest(
            decision_id="revise-1",
            plan_id=seeded_waiting_plan.plan_id,
            plan_version=1,
            expected_version=seeded_waiting_plan.aggregate_version,
            action=PlanDecisionAction.REVISE,
            feedback="Verify both databases",
        ),
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-2",
    )
    run = await seeded_waiting_plan.load_run()
    after = await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    )

    assert result.snapshot.current_version == 2
    assert result.snapshot.status is PlanStatus.AWAITING_APPROVAL
    assert result.decision.resulting_plan_version == 2
    assert result.lease is None
    assert run.active_plan_version == 1
    assert run.status is RunStatus.AWAITING_USER
    assert before == after


@pytest.mark.asyncio
async def test_revision_generation_failure_writes_no_decision_or_version(
    seeded_waiting_plan: SeededWaitingPlan,
):
    seeded_waiting_plan.generator.failure = RuntimeError("generation failure")

    with pytest.raises(RuntimeError, match="generation failure"):
        await seeded_waiting_plan.service.decide(
            PlanDecisionRequest(
                decision_id="revise-generation-failure",
                plan_id=seeded_waiting_plan.plan_id,
                plan_version=1,
                expected_version=seeded_waiting_plan.aggregate_version,
                action=PlanDecisionAction.REVISE,
                feedback="Revise safely",
            ),
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-2",
        )

    plan = await seeded_waiting_plan.load_plan()
    assert plan.current_version == 1
    assert plan.decisions == ()
    assert await count_decisions(
        seeded_waiting_plan.database,
        seeded_waiting_plan.plan_id,
    ) == 0


@pytest.mark.asyncio
async def test_revision_checkpoint_failure_rolls_back_decision_and_version(
    seeded_waiting_plan: SeededWaitingPlan,
    monkeypatch,
):
    before_plan = await seeded_waiting_plan.load_plan()
    before_run = await seeded_waiting_plan.load_run()
    before_rows = await count_plan_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
    )
    before_version = await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    )
    before_checkpoints = await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    )
    original_insert_checkpoint = WorkflowRepository._insert_checkpoint
    checkpoint_write_reached = False

    async def fail_after_checkpoint_insert(repository, *args, **kwargs):
        nonlocal checkpoint_write_reached
        await original_insert_checkpoint(repository, *args, **kwargs)
        assert kwargs["phase"] == CheckpointPhase.PLAN_AWAITING_APPROVAL.value
        checkpoint_write_reached = True
        raise RuntimeError("revision checkpoint insertion failure")

    monkeypatch.setattr(
        WorkflowRepository,
        "_insert_checkpoint",
        fail_after_checkpoint_insert,
    )

    with pytest.raises(RuntimeError, match="revision checkpoint insertion failure"):
        await seeded_waiting_plan.service.decide(
            PlanDecisionRequest(
                decision_id="revise-checkpoint-failure",
                plan_id=seeded_waiting_plan.plan_id,
                plan_version=1,
                expected_version=seeded_waiting_plan.aggregate_version,
                action=PlanDecisionAction.REVISE,
                feedback="Revise atomically",
            ),
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-2",
        )

    assert checkpoint_write_reached is True
    assert await seeded_waiting_plan.load_plan() == before_plan
    assert await seeded_waiting_plan.load_run() == before_run
    assert await count_plan_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
    ) == before_rows
    assert await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    ) == before_version
    assert await count_decisions(
        seeded_waiting_plan.database,
        seeded_waiting_plan.plan_id,
    ) == 0
    assert await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    ) == before_checkpoints


@pytest.mark.asyncio
async def test_revision_finish_failure_rolls_back_version_fence_and_checkpoint(
    seeded_waiting_plan: SeededWaitingPlan,
    monkeypatch,
):
    before_plan = await seeded_waiting_plan.load_plan()
    before_run = await seeded_waiting_plan.load_run()
    before_rows = await count_plan_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
    )
    before_version = await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    )
    before_checkpoints = await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    )
    original_finish_revision = PlanRepository.finish_revision_decision
    finish_write_reached = False

    async def fail_after_finish_revision(repository, *args, **kwargs):
        nonlocal finish_write_reached
        await original_finish_revision(repository, *args, **kwargs)
        finish_write_reached = True
        raise RuntimeError("revision decision finalization failure")

    monkeypatch.setattr(
        PlanRepository,
        "finish_revision_decision",
        fail_after_finish_revision,
    )

    with pytest.raises(RuntimeError, match="revision decision finalization failure"):
        await seeded_waiting_plan.service.decide(
            PlanDecisionRequest(
                decision_id="revise-finish-failure",
                plan_id=seeded_waiting_plan.plan_id,
                plan_version=1,
                expected_version=seeded_waiting_plan.aggregate_version,
                action=PlanDecisionAction.REVISE,
                feedback="Finalize atomically",
            ),
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-2",
        )

    assert finish_write_reached is True
    assert await seeded_waiting_plan.load_plan() == before_plan
    assert await seeded_waiting_plan.load_run() == before_run
    assert await count_plan_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
    ) == before_rows
    assert await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    ) == before_version
    assert await count_decisions(
        seeded_waiting_plan.database,
        seeded_waiting_plan.plan_id,
    ) == 0
    assert await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    ) == before_checkpoints


@pytest.mark.asyncio
async def test_revision_quota_failure_persists_nothing(
    seeded_waiting_plan: SeededWaitingPlan,
):
    service = PlanningService(
        seeded_waiting_plan.database,
        settings=planning_settings(max_revisions=0),
        generator=cast(PlanGenerator, seeded_waiting_plan.generator),
        workflow=seeded_waiting_plan.workflow,
    )

    with pytest.raises(PlanRevisionLimitError):
        await service.decide(
            PlanDecisionRequest(
                decision_id="revise-over-quota",
                plan_id=seeded_waiting_plan.plan_id,
                plan_version=1,
                expected_version=seeded_waiting_plan.aggregate_version,
                action=PlanDecisionAction.REVISE,
                feedback="One revision too many",
            ),
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-2",
        )

    plan = await seeded_waiting_plan.load_plan()
    assert plan.current_version == 1
    assert plan.decisions == ()


@pytest.mark.asyncio
async def test_completed_revision_replay_skips_generation_and_new_checkpoint(
    seeded_waiting_plan: SeededWaitingPlan,
):
    request = PlanDecisionRequest(
        decision_id="revise-replay",
        plan_id=seeded_waiting_plan.plan_id,
        plan_version=1,
        expected_version=seeded_waiting_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="Revise safely",
    )
    first = await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-2",
    )
    checkpoints_before = await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    )
    seeded_waiting_plan.generator.calls.clear()

    replay = await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-3",
    )

    assert replay.idempotent_replay is True
    assert replay.decision == first.decision
    assert replay.snapshot == first.snapshot
    assert replay.lease is None
    assert replay.events == ()
    assert seeded_waiting_plan.generator.calls == []
    assert await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    ) == checkpoints_before


@pytest.mark.asyncio
async def test_revision_race_replays_winner_without_appending_again(
    seeded_waiting_plan: SeededWaitingPlan,
):
    request = PlanDecisionRequest(
        decision_id="revise-race-replay",
        plan_id=seeded_waiting_plan.plan_id,
        plan_version=1,
        expected_version=seeded_waiting_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="Revise once",
    )
    winning_service = PlanningService(
        seeded_waiting_plan.database,
        settings=planning_settings(),
        generator=cast(PlanGenerator, FakePlanGenerator()),
        workflow=seeded_waiting_plan.workflow,
    )

    async def commit_winner():
        await winning_service.decide(
            request,
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-winner",
        )

    seeded_waiting_plan.generator.on_generate = commit_winner

    replay = await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-loser",
    )
    plan = await seeded_waiting_plan.load_plan()

    assert replay.idempotent_replay is True
    assert replay.lease is None
    assert replay.events == ()
    assert plan.current_version == 2
    assert len(plan.versions) == 2
    assert len(plan.decisions) == 1


@pytest.mark.asyncio
async def test_revision_locked_claim_replays_winner_before_quota_or_append(
    seeded_waiting_plan: SeededWaitingPlan,
    monkeypatch,
):
    request = PlanDecisionRequest(
        decision_id="revise-locked-winner",
        plan_id=seeded_waiting_plan.plan_id,
        plan_version=1,
        expected_version=seeded_waiting_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="Revise exactly once",
    )
    winning_service = PlanningService(
        seeded_waiting_plan.database,
        settings=planning_settings(),
        generator=cast(PlanGenerator, FakePlanGenerator()),
        workflow=seeded_waiting_plan.workflow,
    )
    losing_service = PlanningService(
        seeded_waiting_plan.database,
        settings=planning_settings(max_revisions=1),
        generator=cast(PlanGenerator, seeded_waiting_plan.generator),
        workflow=seeded_waiting_plan.workflow,
    )
    original_replay = PlanRepository.replay_decision
    original_begin = PlanRepository.begin_revision_decision
    original_append = PlanRepository.append_version
    original_fence = WorkflowCoordinator.fence_waiting_plan_run
    original_finish = PlanRepository.finish_revision_decision
    winner_committed = False
    stale_write_read_forced = False
    locked_claim_observed_winner = False

    async def stale_once_after_winner(repository, *args, **kwargs):
        nonlocal stale_write_read_forced
        replay = await original_replay(repository, *args, **kwargs)
        if (
            winner_committed
            and replay is not None
            and not stale_write_read_forced
            and not locked_claim_observed_winner
        ):
            stale_write_read_forced = True
            return None
        return replay

    async def observe_locked_claim(repository, *args, **kwargs):
        nonlocal locked_claim_observed_winner
        decision = await original_begin(repository, *args, **kwargs)
        if winner_committed and decision.resulting_plan_version is not None:
            locked_claim_observed_winner = True
        return decision

    async def forbid_append_after_winner(repository, *args, **kwargs):
        if winner_committed:
            raise AssertionError("losing revision appended after observing the winner")
        return await original_append(repository, *args, **kwargs)

    async def forbid_fence_after_winner(coordinator, *args, **kwargs):
        if winner_committed:
            raise AssertionError("losing revision fenced after observing the winner")
        return await original_fence(coordinator, *args, **kwargs)

    async def forbid_finish_after_winner(repository, *args, **kwargs):
        if winner_committed:
            raise AssertionError("losing revision finished after observing the winner")
        return await original_finish(repository, *args, **kwargs)

    monkeypatch.setattr(PlanRepository, "replay_decision", stale_once_after_winner)
    monkeypatch.setattr(
        PlanRepository,
        "begin_revision_decision",
        observe_locked_claim,
    )
    monkeypatch.setattr(PlanRepository, "append_version", forbid_append_after_winner)
    monkeypatch.setattr(
        WorkflowCoordinator,
        "fence_waiting_plan_run",
        forbid_fence_after_winner,
    )
    monkeypatch.setattr(
        PlanRepository,
        "finish_revision_decision",
        forbid_finish_after_winner,
    )

    async def commit_winner():
        nonlocal winner_committed
        await winning_service.decide(
            request,
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-winner",
        )
        winner_committed = True

    seeded_waiting_plan.generator.on_generate = commit_winner

    replay = await losing_service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-loser",
    )
    plan = await seeded_waiting_plan.load_plan()

    assert locked_claim_observed_winner is True
    assert replay.idempotent_replay is True
    assert replay.snapshot.current_version == 2
    assert replay.lease is None
    assert replay.events == ()
    assert len(seeded_waiting_plan.generator.calls) == 1
    assert len(plan.versions) == 2
    assert len(plan.decisions) == 1


@pytest.mark.asyncio
async def test_mismatched_revision_decision_id_is_rejected_before_generation(
    seeded_waiting_plan: SeededWaitingPlan,
):
    request = PlanDecisionRequest(
        decision_id="revise-mismatch",
        plan_id=seeded_waiting_plan.plan_id,
        plan_version=1,
        expected_version=seeded_waiting_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback="First feedback",
    )
    await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-2",
    )
    seeded_waiting_plan.generator.calls.clear()

    with pytest.raises(PlanDecisionIdempotencyError):
        await seeded_waiting_plan.service.decide(
            request.model_copy(update={"feedback": "Different feedback"}),
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-3",
        )

    assert seeded_waiting_plan.generator.calls == []


@pytest.mark.asyncio
async def test_plan_run_approval_rolls_back_after_run_cas_succeeds(
    seeded_waiting_plan: SeededWaitingPlan,
    monkeypatch,
):
    before_plan = await seeded_waiting_plan.load_plan()
    before_run = await seeded_waiting_plan.load_run()
    before_version = await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    )
    before_checkpoints = await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    )
    original_resume = WorkflowRepository._resume_waiting_plan_run
    run_cas_reached = False

    async def fail_after_run_cas(repository, *args, **kwargs):
        nonlocal run_cas_reached
        lease = await original_resume(repository, *args, **kwargs)
        assert lease is not None
        run_cas_reached = True
        raise RuntimeError("approval post-CAS failure")

    monkeypatch.setattr(
        WorkflowRepository,
        "_resume_waiting_plan_run",
        fail_after_run_cas,
    )

    with pytest.raises(RuntimeError, match="approval post-CAS failure"):
        await seeded_waiting_plan.service.decide(
            approve_waiting_request(seeded_waiting_plan, "approve-rollback"),
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-2",
        )

    assert run_cas_reached is True
    assert await seeded_waiting_plan.load_plan() == before_plan
    assert await seeded_waiting_plan.load_run() == before_run
    assert await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    ) == before_version
    assert await count_decisions(
        seeded_waiting_plan.database,
        seeded_waiting_plan.plan_id,
    ) == 0
    assert await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    ) == before_checkpoints


@pytest.mark.asyncio
async def test_approval_replay_never_constructs_a_second_lease(
    seeded_waiting_plan: SeededWaitingPlan,
):
    request = approve_waiting_request(seeded_waiting_plan, "approve-replay")
    first = await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-2",
    )
    run_after_first = await seeded_waiting_plan.load_run()
    replay = await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-3",
    )
    run_after_replay = await seeded_waiting_plan.load_run()

    assert first.lease is not None
    assert replay.idempotent_replay is True
    assert replay.lease is None
    assert replay.events == ()
    assert run_after_first.status is RunStatus.RESUMING
    assert run_after_replay == run_after_first


@pytest.mark.asyncio
async def test_plan_run_reject_and_cancel_commit_together(
    seeded_waiting_plan: SeededWaitingPlan,
):
    result = await seeded_waiting_plan.service.decide(
        PlanDecisionRequest(
            decision_id="reject-1",
            plan_id=seeded_waiting_plan.plan_id,
            plan_version=1,
            expected_version=seeded_waiting_plan.aggregate_version,
            action=PlanDecisionAction.REJECT,
            feedback=None,
        ),
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-2",
    )

    assert result.snapshot.status is PlanStatus.REJECTED
    assert result.run.status is RunStatus.CANCELLED
    assert result.run.finished_at is not None
    assert result.lease is None


@pytest.mark.asyncio
async def test_plan_run_reject_checkpoint_failure_rolls_back_cancel_cas(
    seeded_waiting_plan: SeededWaitingPlan,
    monkeypatch,
):
    before_plan = await seeded_waiting_plan.load_plan()
    before_run = await seeded_waiting_plan.load_run()
    before_version = await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    )
    before_checkpoints = await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    )
    original_insert_checkpoint = WorkflowRepository._insert_checkpoint
    terminal_checkpoint_write_reached = False

    async def fail_after_terminal_checkpoint(repository, *args, **kwargs):
        nonlocal terminal_checkpoint_write_reached
        await original_insert_checkpoint(repository, *args, **kwargs)
        assert kwargs["phase"] == CheckpointPhase.RUN_TERMINAL.value
        terminal_checkpoint_write_reached = True
        raise RuntimeError("terminal checkpoint insertion failure")

    monkeypatch.setattr(
        WorkflowRepository,
        "_insert_checkpoint",
        fail_after_terminal_checkpoint,
    )

    with pytest.raises(RuntimeError, match="terminal checkpoint insertion failure"):
        await seeded_waiting_plan.service.decide(
            PlanDecisionRequest(
                decision_id="reject-rollback",
                plan_id=seeded_waiting_plan.plan_id,
                plan_version=1,
                expected_version=seeded_waiting_plan.aggregate_version,
                action=PlanDecisionAction.REJECT,
                feedback=None,
            ),
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-2",
        )

    assert terminal_checkpoint_write_reached is True
    assert await seeded_waiting_plan.load_plan() == before_plan
    assert await seeded_waiting_plan.load_run() == before_run
    assert await dump_plan_version_rows(
        seeded_waiting_plan.database,
        seeded_waiting_plan.context,
        seeded_waiting_plan.plan_id,
        1,
    ) == before_version
    assert await count_decisions(
        seeded_waiting_plan.database,
        seeded_waiting_plan.plan_id,
    ) == 0
    assert await count_all_rows(
        seeded_waiting_plan.database,
        execution_checkpoints,
    ) == before_checkpoints


@pytest.mark.parametrize(
    ("feedback", "expected_feedback"),
    (
        ("Keep the validation wording unchanged", "Keep the validation wording unchanged"),
        (
            "Use access_token=supersecretvalue in the fixture",
            "Use [REDACTED] in the fixture",
        ),
    ),
)
@pytest.mark.asyncio
async def test_revision_feedback_is_safe_before_model_storage_and_return(
    seeded_waiting_plan: SeededWaitingPlan,
    feedback: str,
    expected_feedback: str,
):
    request = PlanDecisionRequest(
        decision_id="revise-safe-feedback",
        plan_id=seeded_waiting_plan.plan_id,
        plan_version=1,
        expected_version=seeded_waiting_plan.aggregate_version,
        action=PlanDecisionAction.REVISE,
        feedback=feedback,
    )

    result = await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-2",
    )
    revision_context = seeded_waiting_plan.generator.calls[0][1]
    assert isinstance(revision_context, PlanRevisionContext)
    async with seeded_waiting_plan.database.connect() as connection:
        decision_feedback = (
            await connection.execute(
                select(agent_plan_decisions.c.feedback).where(
                    agent_plan_decisions.c.plan_id == seeded_waiting_plan.plan_id,
                    agent_plan_decisions.c.decision_id == request.decision_id,
                )
            )
        ).scalar_one()
        revision_feedback = (
            await connection.execute(
                select(agent_plan_versions.c.revision_feedback).where(
                    agent_plan_versions.c.plan_id == seeded_waiting_plan.plan_id,
                    agent_plan_versions.c.plan_version == 2,
                )
            )
        ).scalar_one()

    calls_before_replay = len(seeded_waiting_plan.generator.calls)
    replay = await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-3",
    )

    assert expected_feedback == sanitize_plan_text(feedback)
    assert revision_context.feedback == expected_feedback
    assert decision_feedback == expected_feedback
    assert revision_feedback == expected_feedback
    assert result.decision.feedback == expected_feedback
    assert result.snapshot.current.revision_feedback == expected_feedback
    assert result.snapshot.decisions[-1].feedback == expected_feedback
    assert len(result.events) == 1
    assert result.events[0].data["decision"]["feedback"] == expected_feedback
    assert replay.idempotent_replay is True
    assert replay.decision.feedback == expected_feedback
    assert replay.snapshot.decisions[-1].feedback == expected_feedback
    assert len(seeded_waiting_plan.generator.calls) == calls_before_replay
    assert "supersecretvalue" not in str(
        {
            "decision_row": decision_feedback,
            "version_row": revision_feedback,
            "revision_context": revision_context,
            "result": result,
            "replay": replay,
        }
    )


@pytest.mark.parametrize(
    "action",
    (PlanDecisionAction.APPROVE, PlanDecisionAction.REJECT),
)
@pytest.mark.asyncio
async def test_plan_run_non_revision_feedback_is_sanitized_at_service_boundary(
    seeded_waiting_plan: SeededWaitingPlan,
    action: PlanDecisionAction,
):
    feedback = "Authorization: Bearer supersecretvalue"
    expected_feedback = sanitize_plan_text(feedback)
    request = PlanDecisionRequest.model_construct(
        decision_id=f"{action.value}-safe-feedback",
        plan_id=seeded_waiting_plan.plan_id,
        plan_version=1,
        expected_version=seeded_waiting_plan.aggregate_version,
        action=action,
        feedback=feedback,
    )

    result = await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-2",
    )
    async with seeded_waiting_plan.database.connect() as connection:
        decision_feedback = (
            await connection.execute(
                select(agent_plan_decisions.c.feedback).where(
                    agent_plan_decisions.c.plan_id == seeded_waiting_plan.plan_id,
                    agent_plan_decisions.c.decision_id == request.decision_id,
                )
            )
        ).scalar_one()
    replay = await seeded_waiting_plan.service.decide(
        request,
        decided_by=seeded_waiting_plan.tenant_id,
        runtime_instance_id="runtime-3",
    )

    assert decision_feedback == expected_feedback
    assert result.decision.feedback == expected_feedback
    assert result.snapshot.decisions[-1].feedback == expected_feedback
    assert result.events[0].data["decision"]["feedback"] == expected_feedback
    assert replay.idempotent_replay is True
    assert replay.decision.feedback == expected_feedback
    assert replay.events == ()
    assert "supersecretvalue" not in str(
        {
            "decision_row": decision_feedback,
            "result": result,
            "replay": replay,
        }
    )


@pytest.mark.asyncio
async def test_decision_context_locator_hides_foreign_and_unknown_plans(
    seeded_waiting_plan: SeededWaitingPlan,
    plan_contexts: dict[str, TenantContext],
):
    foreign_tenant_id = plan_contexts["secondary"].tenant_id
    failures: list[tuple[type[BaseException], str]] = []

    for plan_id in (seeded_waiting_plan.plan_id, str(uuid4())):
        request = PlanDecisionRequest(
            decision_id=f"foreign-{plan_id}",
            plan_id=plan_id,
            plan_version=1,
            expected_version=1,
            action=PlanDecisionAction.REVISE,
            feedback="Do not disclose scope",
        )
        with pytest.raises(PlanNotFoundError) as exc_info:
            await seeded_waiting_plan.service.decide(
                request,
                decided_by=foreign_tenant_id,
                runtime_instance_id="runtime-foreign",
            )
        failures.append((type(exc_info.value), str(exc_info.value)))

    assert failures[0] == failures[1]
    assert seeded_waiting_plan.generator.calls == []


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


@pytest.mark.asyncio
async def test_create_reused_step_attempt_copies_immutable_source_fact(
    plan_database, seeded_revised_plan,
):
    context = seeded_revised_plan.context
    async with TenantUnitOfWork(plan_database, context) as uow:
        snapshot = await uow.plans.get(seeded_revised_plan.plan_id)
        assert snapshot is not None
        v1 = snapshot.versions[0]
        source_step = v1.steps[0]
        target_step = snapshot.current.steps[0]
        run_id = str(uuid4())
        await uow.conn.execute(update(agent_plans).where(agent_plans.c.id == snapshot.plan_id).values(status="approved", approved_version=2))
        await uow.conn.execute(insert(agent_runs).values(run_id=run_id, tenant_id=context.tenant_id, workspace_id=context.workspace_id, session_id=context.session_id, plan_id=snapshot.plan_id, initial_plan_version=1, active_plan_version=2, cancel_requested_at=None, run_status="running", runtime_instance_id="runtime-a", lease_owner="runtime-a", fencing_token=1, lease_expires_at=9_999_999_999_999, heartbeat_at=1, schema_version=1, version=1, created_at=1, updated_at=1, finished_at=None))
        source_id = str(uuid4())
        document = PlanStepResultDocument(plan_id=snapshot.plan_id, plan_version=1, run_id=run_id, step_id=source_step.step_id, step_run_id=source_id, attempt=1, status="succeeded", summary="source summary", evidence=["durable evidence"], definition_digest=source_step.definition_digest, dependency_result_digests={}, tool_catalog_digest="b" * 64, policy_digest="c" * 64, skill_set_digest="d" * 64)
        entry = await uow.memory.save(MemoryEntry(content=document.canonical_json(), type="plan_step_result", role="assistant", session_id=context.session_id))
        await uow.conn.execute(insert(agent_plan_step_runs).values(tenant_id=context.tenant_id, workspace_id=context.workspace_id, session_id=context.session_id, plan_id=snapshot.plan_id, plan_version=1, step_id=source_step.step_id, step_run_id=source_id, run_id=run_id, attempt=1, status="succeeded", result_summary=document.summary, result_ref=f"memory:{entry.id}", result_digest=document.digest(), error_code=None, error_detail_redacted=None, reused_from_step_run_id=None, version=2, started_at=1, finished_at=2))
        lease = RunLease(context=context.for_run(context.session_id, run_id), lease_owner="runtime-a", fencing_token=1, version=1, lease_expires_at=9_999_999_999_999)
        source_before = await uow.plans.step_run_by_id(run_id=run_id, step_run_id=source_id)
        entry_before = await uow.memory.get(entry.id, context.session_id)
        created = await uow.plans.create_reused_step_attempt(lease, plan_id=snapshot.plan_id, plan_version=2, step_id=target_step.step_id, source_step_run_id=source_id)
        source_after = await uow.plans.step_run_by_id(run_id=run_id, step_run_id=source_id)
        entry_after = await uow.memory.get(entry.id, context.session_id)
    assert source_before is not None and entry_before is not None
    assert source_after == source_before and entry_after == entry_before
    assert created.step_run_id != source_id
    assert (created.plan_version, created.step_id, created.status, created.reused_from_step_run_id) == (2, target_step.step_id, PlanStepRunStatus.SUCCEEDED, source_id)
    assert (created.result_ref, created.result_digest, created.result_summary) == (source_before.result_ref, source_before.result_digest, source_before.result_summary)
    assert created.attempt == 1 and created.version == 2 and created.finished_at is not None
    assert source_before.reused_from_step_run_id is None and source_before.version == 2
    assert entry_after.content == document.canonical_json()


@pytest.mark.asyncio
async def test_reused_attempt_nonduplicate_integrity_error_is_not_replayed(
    plan_database,
    seeded_revised_plan,
):
    context = seeded_revised_plan.context
    async with TenantUnitOfWork(plan_database, context) as uow:
        snapshot = await uow.plans.get(seeded_revised_plan.plan_id)
        assert snapshot is not None
        source_step = snapshot.versions[0].steps[0]
        target_step = snapshot.current.steps[0]
        run_id = str(uuid4())
        await uow.conn.execute(
            update(agent_plans)
            .where(agent_plans.c.id == snapshot.plan_id)
            .values(status="approved", approved_version=2)
        )
        await uow.conn.execute(
            insert(agent_runs).values(
                run_id=run_id,
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                plan_id=snapshot.plan_id,
                initial_plan_version=1,
                active_plan_version=2,
                cancel_requested_at=None,
                run_status="running",
                runtime_instance_id="runtime-a",
                lease_owner="runtime-a",
                fencing_token=1,
                lease_expires_at=9_999_999_999_999,
                heartbeat_at=1,
                schema_version=1,
                version=1,
                created_at=1,
                updated_at=1,
                finished_at=None,
            )
        )
        source_id = str(uuid4())
        document = PlanStepResultDocument(
            plan_id=snapshot.plan_id,
            plan_version=1,
            run_id=run_id,
            step_id=source_step.step_id,
            step_run_id=source_id,
            attempt=1,
            status="succeeded",
            summary="source summary",
            evidence=[],
            definition_digest=source_step.definition_digest,
            dependency_result_digests={},
            tool_catalog_digest="b" * 64,
            policy_digest="c" * 64,
            skill_set_digest="d" * 64,
        )
        entry = await uow.memory.save(
            MemoryEntry(
                content=document.canonical_json(),
                type="plan_step_result",
                role="assistant",
                session_id=context.session_id,
            )
        )
        await uow.conn.execute(
            insert(agent_plan_step_runs).values(
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                plan_id=snapshot.plan_id,
                plan_version=1,
                step_id=source_step.step_id,
                step_run_id=source_id,
                run_id=run_id,
                attempt=1,
                status="succeeded",
                result_summary=document.summary,
                result_ref=f"memory:{entry.id}",
                result_digest=document.digest(),
                error_code=None,
                error_detail_redacted=None,
                reused_from_step_run_id=None,
                version=2,
                started_at=1,
                finished_at=2,
            )
        )
        lease = RunLease(
            context=context.for_run(context.session_id, run_id),
            lease_owner="runtime-a",
            fencing_token=1,
            version=1,
            lease_expires_at=9_999_999_999_999,
        )
        source_before = await uow.plans.step_run_by_id(
            run_id=run_id,
            step_run_id=source_id,
        )
        assert source_before is not None
        primary = IntegrityError(
            "INSERT INTO agent_plan_step_runs",
            {},
            RuntimeError("injected non-duplicate reused-attempt integrity failure"),
        )
        repository = PlanRepository(
            ReusedAttemptIntegrityFailureConnection(uow.conn, primary),  # type: ignore[arg-type]
            plan_database.dialect,
            context,
            PlanningSettings(),
        )
        with pytest.raises(IntegrityError) as raised:
            await repository.create_reused_step_attempt(
                lease,
                plan_id=snapshot.plan_id,
                plan_version=2,
                step_id=target_step.step_id,
                source_step_run_id=source_id,
            )

    async with TenantUnitOfWork(plan_database, context) as uow:
        source_after = await uow.plans.step_run_by_id(
            run_id=run_id,
            step_run_id=source_id,
        )
        target_attempts = await uow.plans.step_attempts(
            plan_id=snapshot.plan_id,
            plan_version=2,
            run_id=run_id,
            step_id=target_step.step_id,
        )
    assert raised.value is primary
    assert source_after == source_before
    assert target_attempts == ()


@pytest.mark.asyncio
async def test_create_reused_step_attempt_rejects_stale_lease_without_writes(
    plan_database, seeded_revised_plan,
):
    # The happy-path fixture above establishes the full source fact; stale fences
    # are rejected before any target-version fact can be inserted.
    context = seeded_revised_plan.context
    async with TenantUnitOfWork(plan_database, context) as uow:
        snapshot = await uow.plans.get(seeded_revised_plan.plan_id)
        assert snapshot is not None
        run_id = str(uuid4())
        await uow.conn.execute(update(agent_plans).where(agent_plans.c.id == snapshot.plan_id).values(status="approved", approved_version=2))
        await uow.conn.execute(insert(agent_runs).values(run_id=run_id, tenant_id=context.tenant_id, workspace_id=context.workspace_id, session_id=context.session_id, plan_id=snapshot.plan_id, initial_plan_version=1, active_plan_version=2, cancel_requested_at=None, run_status="running", runtime_instance_id="runtime-a", lease_owner="runtime-a", fencing_token=1, lease_expires_at=9_999_999_999_999, heartbeat_at=1, schema_version=1, version=1, created_at=1, updated_at=1, finished_at=None))
        target = snapshot.current.steps[0]
        before = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=2, run_id=run_id, step_id=target.step_id)
        stale = RunLease(context=context.for_run(context.session_id, run_id), lease_owner="runtime-a", fencing_token=2, version=1, lease_expires_at=9_999_999_999_999)
        with pytest.raises(StaleFenceError):
            await uow.plans.create_reused_step_attempt(stale, plan_id=snapshot.plan_id, plan_version=2, step_id=target.step_id, source_step_run_id=str(uuid4()))
        after = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=2, run_id=run_id, step_id=target.step_id)
    assert before == after == ()


@pytest.mark.parametrize("status", ["awaiting_user", "failed_terminal"])
@pytest.mark.asyncio
async def test_create_reused_step_attempt_rejects_non_executable_run_without_writes(
    plan_database, seeded_revised_plan, status,
):
    context = seeded_revised_plan.context
    async with TenantUnitOfWork(plan_database, context) as uow:
        snapshot = await uow.plans.get(seeded_revised_plan.plan_id)
        assert snapshot is not None
        run_id = str(uuid4())
        await uow.conn.execute(update(agent_plans).where(agent_plans.c.id == snapshot.plan_id).values(status="approved", approved_version=2))
        await uow.conn.execute(insert(agent_runs).values(run_id=run_id, tenant_id=context.tenant_id, workspace_id=context.workspace_id, session_id=context.session_id, plan_id=snapshot.plan_id, initial_plan_version=1, active_plan_version=2, cancel_requested_at=None, run_status=status, runtime_instance_id="runtime-a", lease_owner="runtime-a", fencing_token=1, lease_expires_at=9_999_999_999_999, heartbeat_at=1, schema_version=1, version=1, created_at=1, updated_at=1, finished_at=(None if status == "awaiting_user" else 2)))
        target = snapshot.current.steps[0]
        before = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=2, run_id=run_id, step_id=target.step_id)
        lease = RunLease(context=context.for_run(context.session_id, run_id), lease_owner="runtime-a", fencing_token=1, version=1, lease_expires_at=9_999_999_999_999)
        with pytest.raises(PlanExecutionBlocked):
            await uow.plans.create_reused_step_attempt(lease, plan_id=snapshot.plan_id, plan_version=2, step_id=target.step_id, source_step_run_id=str(uuid4()))
        after = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=2, run_id=run_id, step_id=target.step_id)
    assert before == after == ()


@pytest.mark.asyncio
async def test_create_reused_step_attempt_rejects_active_version_mismatch_before_source_lookup(
    plan_database, seeded_revised_plan,
):
    context = seeded_revised_plan.context
    async with TenantUnitOfWork(plan_database, context) as uow:
        snapshot = await uow.plans.get(seeded_revised_plan.plan_id)
        assert snapshot is not None
        run_id = str(uuid4())
        await uow.conn.execute(update(agent_plans).where(agent_plans.c.id == snapshot.plan_id).values(status="approved", approved_version=2))
        await uow.conn.execute(insert(agent_runs).values(run_id=run_id, tenant_id=context.tenant_id, workspace_id=context.workspace_id, session_id=context.session_id, plan_id=snapshot.plan_id, initial_plan_version=1, active_plan_version=1, cancel_requested_at=None, run_status="running", runtime_instance_id="runtime-a", lease_owner="runtime-a", fencing_token=1, lease_expires_at=9_999_999_999_999, heartbeat_at=1, schema_version=1, version=1, created_at=1, updated_at=1, finished_at=None))
        target = snapshot.current.steps[0]
        before = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=2, run_id=run_id, step_id=target.step_id)
        lease = RunLease(context=context.for_run(context.session_id, run_id), lease_owner="runtime-a", fencing_token=1, version=1, lease_expires_at=9_999_999_999_999)
        with pytest.raises(PlanExecutionBlocked, match="approved current active version"):
            await uow.plans.create_reused_step_attempt(lease, plan_id=snapshot.plan_id, plan_version=2, step_id=target.step_id, source_step_run_id=str(uuid4()))
        after = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=2, run_id=run_id, step_id=target.step_id)
    assert before == after == ()


@pytest.mark.asyncio
async def test_create_reused_step_attempt_rejects_requested_version_mismatch_without_writes(
    plan_database, seeded_revised_plan,
):
    context = seeded_revised_plan.context
    async with TenantUnitOfWork(plan_database, context) as uow:
        snapshot = await uow.plans.get(seeded_revised_plan.plan_id)
        assert snapshot is not None
        run_id = str(uuid4())
        await uow.conn.execute(update(agent_plans).where(agent_plans.c.id == snapshot.plan_id).values(status="approved", approved_version=2))
        await uow.conn.execute(insert(agent_runs).values(run_id=run_id, tenant_id=context.tenant_id, workspace_id=context.workspace_id, session_id=context.session_id, plan_id=snapshot.plan_id, initial_plan_version=1, active_plan_version=2, cancel_requested_at=None, run_status="running", runtime_instance_id="runtime-a", lease_owner="runtime-a", fencing_token=1, lease_expires_at=9_999_999_999_999, heartbeat_at=1, schema_version=1, version=1, created_at=1, updated_at=1, finished_at=None))
        v1_target, v2_target = snapshot.versions[0].steps[0], snapshot.current.steps[0]
        before_v1 = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=1, run_id=run_id, step_id=v1_target.step_id)
        before_v2 = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=2, run_id=run_id, step_id=v2_target.step_id)
        lease = RunLease(context=context.for_run(context.session_id, run_id), lease_owner="runtime-a", fencing_token=1, version=1, lease_expires_at=9_999_999_999_999)
        with pytest.raises(PlanExecutionBlocked, match="approved current active version"):
            await uow.plans.create_reused_step_attempt(lease, plan_id=snapshot.plan_id, plan_version=1, step_id=v1_target.step_id, source_step_run_id=str(uuid4()))
        after_v1 = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=1, run_id=run_id, step_id=v1_target.step_id)
        after_v2 = await uow.plans.step_attempts(plan_id=snapshot.plan_id, plan_version=2, run_id=run_id, step_id=v2_target.step_id)
    assert before_v1 == after_v1 == () and before_v2 == after_v2 == ()


@pytest.mark.asyncio
async def test_create_reused_step_attempt_rejects_source_step_as_v2_target_without_writes(
    plan_database, seeded_revised_plan,
):
    context = seeded_revised_plan.context
    async with TenantUnitOfWork(plan_database, context) as uow:
        snapshot = await uow.plans.get(seeded_revised_plan.plan_id)
        assert snapshot is not None
        run_id, source_id = str(uuid4()), str(uuid4())
        source_step = snapshot.versions[0].steps[0]
        await uow.conn.execute(update(agent_plans).where(agent_plans.c.id == snapshot.plan_id).values(status="approved", approved_version=2))
        await uow.conn.execute(insert(agent_runs).values(run_id=run_id, tenant_id=context.tenant_id, workspace_id=context.workspace_id, session_id=context.session_id, plan_id=snapshot.plan_id, initial_plan_version=1, active_plan_version=2, cancel_requested_at=None, run_status="running", runtime_instance_id="runtime-a", lease_owner="runtime-a", fencing_token=1, lease_expires_at=9_999_999_999_999, heartbeat_at=1, schema_version=1, version=1, created_at=1, updated_at=1, finished_at=None))
        await uow.conn.execute(insert(agent_plan_step_runs).values(tenant_id=context.tenant_id, workspace_id=context.workspace_id, session_id=context.session_id, plan_id=snapshot.plan_id, plan_version=1, step_id=source_step.step_id, step_run_id=source_id, run_id=run_id, attempt=1, status="succeeded", result_summary="source", result_ref="memory:source", result_digest="a" * 64, error_code=None, error_detail_redacted=None, reused_from_step_run_id=None, version=2, started_at=1, finished_at=2))
        lease = RunLease(context=context.for_run(context.session_id, run_id), lease_owner="runtime-a", fencing_token=1, version=1, lease_expires_at=9_999_999_999_999)
        source_before = await uow.plans.step_run_by_id(run_id=run_id, step_run_id=source_id)
        with pytest.raises(PlanExecutionBlocked, match="active version"):
            await uow.plans.create_reused_step_attempt(lease, plan_id=snapshot.plan_id, plan_version=2, step_id=source_step.step_id, source_step_run_id=source_id)
        source_after = await uow.plans.step_run_by_id(run_id=run_id, step_run_id=source_id)
        v2 = await uow.plans.latest_step_attempts(plan_id=snapshot.plan_id, plan_version=2, run_id=run_id)
    assert source_after == source_before and v2 == {}
