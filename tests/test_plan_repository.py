import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import insert, select, text, update

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.memory import MemoryEntry
from multiclaw.planner import (
    PlanDraft,
    PlanDraftStep,
    PlanStatus,
    PlanStepRunStatus,
    PlanTriggerMode,
)
from multiclaw.storage import Database
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.schema import (
    agent_plan_step_dependencies,
    agent_plan_step_runs,
    agent_plan_steps,
    agent_plan_versions,
    agent_plans,
    agent_runs,
)
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext


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
async def test_plan_lookup_hides_foreign_tenant_workspace_and_session(plan_database, seeded_plan):
    for foreign in seeded_plan.foreign_contexts:
        async with TenantUnitOfWork(plan_database, foreign) as uow:
            assert await uow.plans.for_context(foreign).get(seeded_plan.plan_id) is None
            assert await uow.plans.for_context(foreign).list_for_session() == []


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
