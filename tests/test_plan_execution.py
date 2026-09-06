from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import get_args
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects import mysql

from alembic import command
from multiclaw.agent.models import Observation, ObservationType
from multiclaw.agent.multiclaw import MultiClawAgent
from multiclaw.agent.tool_batch import ToolCallOutcome
from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.llm import LLMResponse, ToolCall
from multiclaw.memory import MemoryEntry
from multiclaw.planner.execution import (
    PlanExecutionCoordinator,
    can_reuse_result,
    choose_ready_step,
)
from multiclaw.planner.generator import PlanGenerationError
from multiclaw.planner.models import (
    MaterializeInitialPlan,
    PlanAttemptLimitError,
    PlanDecisionAction,
    PlanDecisionRequest,
    PlanDraft,
    PlanDraftStep,
    PlanExecutionBlocked,
    PlanExecutionOutcome,
    PlanStepAlreadyRunningError,
    PlanStepCompletion,
    PlanStepExecutionRequest,
    PlanStepRecord,
    PlanStepResultDocument,
    PlanStepRunRecord,
    PlanStepRunStatus,
    PlanTriggerMode,
)
from multiclaw.planner.service import FailureRevisionRequest, PlanningService
from multiclaw.security.redaction import redact
from multiclaw.skills import SkillManager
from multiclaw.skills.types import Skill, SkillMetadata
from multiclaw.storage import Database
from multiclaw.storage.dialect import MySQLDialect
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.repositories.plans import PlanRepository
from multiclaw.storage.schema import (
    agent_plan_step_runs,
    agent_plans,
    agent_runs,
    execution_checkpoints,
    memory_entries,
    users,
)
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext
from multiclaw.tools import ToolExecutionResult, ToolRegistry, ToolStatus
from multiclaw.workflow.continuation import (
    ContinuationOutcome,
    ContinuationState,
    PersistedToolResult,
    WorkflowContinuationService,
)
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    CheckpointPhase,
    RunLease,
    RunLeaseHandle,
    RunStatus,
    StaleFenceError,
)

_MYSQL_URL = os.getenv("MULTICLAW_TEST_MYSQL_URL")


class ScriptedPlanRouter:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def completion(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class ObservingFailureGenerator:
    def __init__(self, draft: PlanDraft, observe) -> None:
        self.draft = draft
        self.observe = observe
        self.calls: list[object] = []
        self.error: Exception | None = None

    async def generate(self, objective: str, *, revision, **_limits):
        self.calls.append(revision)
        await self.observe()
        if self.error is not None:
            raise self.error
        return self.draft


class ScriptedStepRunner:
    def __init__(self, completions: list[PlanStepCompletion]) -> None:
        self.completions = list(completions)
        self.calls: list[object] = []
        self.registry = ToolRegistry()
        self.skill_manager = SkillManager()

    async def run_plan_step(self, request, **_kwargs):
        self.calls.append(request)
        return self.completions.pop(0)


class HeartbeatStepRunner:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        delegate,
    ) -> None:
        self.workflow = WorkflowCoordinator(database, settings=settings)
        self.delegate = delegate
        self.registry = delegate.registry
        self.skill_manager = delegate.skill_manager

    async def run_plan_step(self, request, *, run_lease_handle, **kwargs):
        refreshed = await self.workflow.heartbeat(await run_lease_handle.current())
        await run_lease_handle.replace(refreshed)
        return await self.delegate.run_plan_step(
            request,
            run_lease_handle=run_lease_handle,
            **kwargs,
        )


class ApprovalStepRunner:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.registry = ToolRegistry()
        self.skill_manager = SkillManager()

    async def run_plan_step(self, request, *, run_lease_handle, **_kwargs):
        lease = await run_lease_handle.current()
        approval_id = str(uuid4())
        async with self.database.write_transaction() as conn:
            workflow = WorkflowCoordinator(
                self.database,
                settings=self.settings,
                connection=conn,
            )
            transitioned = await workflow.transition_run(
                lease,
                RunStatus.AWAITING_USER,
            )
            approval = await workflow.create_approval(
                transitioned,
                approval_id=approval_id,
                tool_call_id="approval-tool-1",
                expires_at=9_999_999_999_999,
            )
            await workflow.checkpoint(
                transitioned,
                CheckpointPhase.AWAITING_APPROVAL,
                {
                    "run_id": request.context.run_id,
                    "approval_id": approval.approval_id,
                    "tool_call_id": "approval-tool-1",
                    "approval_expires_at_ms": approval.expires_at,
                    "resume_cursor": "approval:resume",
                    "cursor": "approval:resume",
                },
                approval_id=approval.approval_id,
            )
        await run_lease_handle.replace(transitioned)
        return ContinuationOutcome(
            state=ContinuationState.AWAITING_USER,
            detail="tool awaiting approval",
        )


def _settings(*, max_step_attempts: int = 2) -> Settings:
    return Settings(
        _config_file="/nonexistent",
        planning={"max_step_attempts": max_step_attempts},
    )


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _activate_test_skill(manager: SkillManager) -> Skill:
    skill = Skill(
        "audit", SkillMetadata("audit"), body="Preserve durable evidence.", active=True
    )
    manager.skills[skill.name] = skill
    return skill


def _result_document_payload() -> dict[str, object]:
    return {
        "plan_id": str(uuid4()),
        "plan_version": 1,
        "run_id": str(uuid4()),
        "step_id": str(uuid4()),
        "step_run_id": str(uuid4()),
        "attempt": 1,
        "status": "succeeded",
        "summary": "Dependency completed.",
        "evidence": [],
        "definition_digest": "a" * 64,
        "dependency_result_digests": {},
        "tool_catalog_digest": "b" * 64,
        "policy_digest": "c" * 64,
        "skill_set_digest": "d" * 64,
    }


@pytest.mark.parametrize("missing", ("evidence", "dependency_result_digests"))
def test_result_document_requires_durable_collection_fields(missing: str):
    payload = _result_document_payload()
    del payload[missing]

    with pytest.raises(ValidationError) as raised:
        PlanStepResultDocument.model_validate(payload)

    assert any(error["loc"] == (missing,) for error in raised.value.errors())


def test_result_document_accepts_explicit_empty_collection_fields():
    document = PlanStepResultDocument.model_validate(_result_document_payload())

    assert document.evidence == []
    assert document.dependency_result_digests == {}


def test_plan_step_runner_protocol_has_the_durable_resume_signature():
    from multiclaw.planner.models import PlanStepRunner

    signature = inspect.signature(PlanStepRunner.run_plan_step)
    assert getattr(PlanStepRunner, "_is_protocol", False) is True
    assert list(signature.parameters) == [
        "self",
        "request",
        "run_lease_handle",
        "workflow_continuation",
        "recovered_tool_result",
        "recovered_tool_input_json",
    ]
    assert signature.parameters["request"].annotation == "PlanStepExecutionRequest"
    assert signature.parameters["run_lease_handle"].annotation == "RunLeaseHandle"
    assert signature.parameters["run_lease_handle"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["workflow_continuation"].annotation == (
        "WorkflowContinuationService"
    )
    assert signature.parameters["workflow_continuation"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert signature.parameters["recovered_tool_result"].annotation == (
        "PersistedToolResult | None"
    )
    assert signature.parameters["recovered_tool_result"].default is None
    assert signature.parameters["recovered_tool_input_json"].annotation == "str | None"
    assert signature.parameters["recovered_tool_input_json"].default is None
    assert signature.return_annotation == "PlanStepCompletion | ContinuationOutcome"
    agent_return = inspect.signature(MultiClawAgent.run_plan_step).return_annotation
    assert {annotation.__name__ for annotation in get_args(agent_return)} == set(
        signature.return_annotation.split(" | ")
    )


@pytest.mark.parametrize(
    "logical_key",
    ("Invalid.Key", "x" * 65),
    ids=("invalid", "overlong"),
)
def test_result_document_rejects_invalid_dependency_logical_keys(logical_key: str):
    payload = _result_document_payload()
    payload["dependency_result_digests"] = {logical_key: "a" * 64}

    with pytest.raises(ValidationError) as raised:
        PlanStepResultDocument.model_validate(payload)

    assert any(
        error["loc"] == ("dependency_result_digests", logical_key, "[key]")
        for error in raised.value.errors()
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


class StatementRecordingConnection:
    def __init__(self, connection, *, mysql_run_id: str | None = None) -> None:
        self.connection = connection
        self.statements: list[object] = []
        self.mysql_run_id = mysql_run_id

    async def begin_nested(self):
        return await self.connection.begin_nested()

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        if self.mysql_run_id is not None:
            mysql_sql = str(statement.compile(dialect=mysql.dialect())).lower()
            if "unix_timestamp" in mysql_sql:
                final_froms = (
                    statement.get_final_froms()
                    if getattr(statement, "is_select", False)
                    else []
                )
                value = self.mysql_run_id if agent_runs in final_froms else 1
                return ScalarResult(value)
        return await self.connection.execute(statement, *args, **kwargs)


class ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


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

    async def execute(
        self,
        runner: object,
        *,
        draft: PlanDraft | None = None,
    ) -> PlanExecutionOutcome:
        if draft is not None:
            await self.materialize(draft)
        await self.approve()
        return await self.coordinator.execute_to_boundary(
            context=self._context(),
            run_lease_handle=RunLeaseHandle(self.lease),
            runner=runner,
        )

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

    async def activate_second_version(self) -> None:
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
                draft=_draft(("package", "publish")),
                parent_version=1,
                revision_feedback="Activate a second execution version.",
                supersedes={},
            )
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
            await uow.conn.execute(
                update(agent_runs)
                .where(
                    agent_runs.c.tenant_id == self.context.tenant_id,
                    agent_runs.c.workspace_id == self.context.workspace_id,
                    agent_runs.c.session_id == self.context.session_id,
                    agent_runs.c.run_id == self.context.run_id,
                )
                .values(active_plan_version=2)
            )
            self.aggregate_version = revised.aggregate_version

    async def update_run(self, **values: object) -> None:
        context = self._context()
        async with self.database.write_transaction() as conn:
            await conn.execute(
                update(agent_runs)
                .where(
                    agent_runs.c.tenant_id == context.tenant_id,
                    agent_runs.c.workspace_id == context.workspace_id,
                    agent_runs.c.session_id == context.session_id,
                    agent_runs.c.run_id == context.run_id,
                )
                .values(**values)
            )

    async def update_step_run(
        self,
        step_run: PlanStepRunRecord,
        **values: object,
    ) -> None:
        context = self._context()
        async with self.database.write_transaction() as conn:
            await conn.execute(
                update(agent_plan_step_runs)
                .where(
                    agent_plan_step_runs.c.tenant_id == context.tenant_id,
                    agent_plan_step_runs.c.workspace_id == context.workspace_id,
                    agent_plan_step_runs.c.session_id == context.session_id,
                    agent_plan_step_runs.c.run_id == context.run_id,
                    agent_plan_step_runs.c.step_run_id == step_run.step_run_id,
                )
                .values(**values)
            )

    async def finish(
        self,
        step_run: PlanStepRunRecord,
        status: PlanStepRunStatus,
        *,
        digest: str = "a" * 64,
        dependency_result_digests: dict[str, str] | None = None,
        content_prefix: str = "",
        policy_digest: str = "b" * 64,
        skill_set_digest: str = "c" * 64,
    ) -> PlanStepResultDocument | None:
        assert self.context is not None
        document = None
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
                    dependency_result_digests=(
                        {}
                        if dependency_result_digests is None
                        else dependency_result_digests
                    ),
                    tool_catalog_digest=digest,
                    policy_digest=policy_digest,
                    skill_set_digest=skill_set_digest,
                )
                entry = await uow.memory.save(
                    MemoryEntry(
                        content=(
                            content_prefix
                            + json.dumps(
                                document.model_dump(mode="json"),
                                sort_keys=True,
                                separators=(",", ":"),
                            )
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
        return document

    async def succeed(
        self,
        step_run: PlanStepRunRecord,
        digest: str,
        *,
        dependency_result_digests: dict[str, str] | None = None,
        content_prefix: str = "",
        policy_digest: str = "b" * 64,
        skill_set_digest: str = "c" * 64,
    ) -> PlanStepResultDocument:
        document = await self.finish(
            step_run,
            PlanStepRunStatus.SUCCEEDED,
            digest=digest,
            dependency_result_digests=dependency_result_digests,
            content_prefix=content_prefix,
            policy_digest=policy_digest,
            skill_set_digest=skill_set_digest,
        )
        assert document is not None
        return document

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

    async def latest_attempt(self) -> PlanStepRunRecord:
        attempts = await self.all_attempts()
        assert attempts
        return attempts[-1]

    async def attempt_statuses(self) -> list[PlanStepRunStatus]:
        return [attempt.status for attempt in await self.all_attempts()]

    async def all_attempts(self) -> tuple[PlanStepRunRecord, ...]:
        context = self._context()
        assert self.plan_id is not None
        async with TenantUnitOfWork(
            self.database,
            context,
            planning_settings=self.settings.planning,
        ) as uow:
            snapshot = await uow.plans.get(self.plan_id)
            assert snapshot is not None
            return await uow.plans.step_attempts(
                plan_id=self.plan_id,
                plan_version=snapshot.current_version,
                run_id=str(context.run_id),
                step_id=snapshot.current.steps[0].step_id,
            )

    async def load_result(self, result_ref: str | None) -> PlanStepResultDocument:
        assert result_ref is not None
        entry_id = result_ref.removeprefix("memory:")
        context = self._context()
        async with TenantUnitOfWork(self.database, context) as uow:
            entry = await uow.memory.get(entry_id, context.session_id)
        assert entry is not None
        return PlanStepResultDocument.model_validate_json(entry.content)

    async def latest_checkpoint(self) -> dict[str, object]:
        checkpoints = await self.checkpoints()
        assert checkpoints
        return checkpoints[-1]

    async def checkpoints(self) -> tuple[dict[str, object], ...]:
        context = self._context()
        async with self.database.connect() as conn:
            rows = (
                await conn.execute(
                    select(execution_checkpoints)
                    .where(
                        execution_checkpoints.c.tenant_id == context.tenant_id,
                        execution_checkpoints.c.workspace_id == context.workspace_id,
                        execution_checkpoints.c.session_id == context.session_id,
                        execution_checkpoints.c.run_id == context.run_id,
                    )
                    .order_by(execution_checkpoints.c.checkpoint_seq)
                )
            ).mappings().all()
        return tuple(dict(row) for row in rows)

    async def count_results(self) -> int:
        context = self._context()
        async with self.database.connect() as conn:
            return int(
                (
                    await conn.execute(
                        select(func.count())
                        .select_from(memory_entries)
                        .where(
                            memory_entries.c.tenant_id == context.tenant_id,
                            memory_entries.c.workspace_id == context.workspace_id,
                            memory_entries.c.session_id == context.session_id,
                            memory_entries.c.type == "plan_step_result",
                        )
                    )
                ).scalar_one()
            )

    async def count_step_attempts(self, step_id: str) -> int:
        context = self._context()
        async with self.database.connect() as conn:
            return int(
                (
                    await conn.execute(
                        select(func.count())
                        .select_from(agent_plan_step_runs)
                        .where(
                            agent_plan_step_runs.c.tenant_id == context.tenant_id,
                            agent_plan_step_runs.c.workspace_id == context.workspace_id,
                            agent_plan_step_runs.c.session_id == context.session_id,
                            agent_plan_step_runs.c.run_id == context.run_id,
                            agent_plan_step_runs.c.step_id == step_id,
                        )
                    )
                ).scalar_one()
            )

    async def succeeded_result_ids(self, step_ids: list[str]) -> list[str]:
        context = self._context()
        async with self.database.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        agent_plan_step_runs.c.step_id,
                        agent_plan_step_runs.c.result_ref,
                    ).where(
                        agent_plan_step_runs.c.tenant_id == context.tenant_id,
                        agent_plan_step_runs.c.workspace_id == context.workspace_id,
                        agent_plan_step_runs.c.session_id == context.session_id,
                        agent_plan_step_runs.c.run_id == context.run_id,
                        agent_plan_step_runs.c.status
                        == PlanStepRunStatus.SUCCEEDED.value,
                    )
                )
            ).mappings().all()
        refs_by_step = {
            str(row["step_id"]): str(row["result_ref"]).removeprefix("memory:")
            for row in rows
        }
        return [refs_by_step[step_id] for step_id in step_ids]

    async def count_checkpoints(self) -> int:
        assert self.context is not None
        async with self.database.connect() as conn:
            return int(
                (
                    await conn.execute(
                        select(func.count())
                        .select_from(execution_checkpoints)
                        .where(
                            execution_checkpoints.c.tenant_id
                            == self.context.tenant_id,
                            execution_checkpoints.c.workspace_id
                            == self.context.workspace_id,
                            execution_checkpoints.c.session_id
                            == self.context.session_id,
                            execution_checkpoints.c.run_id == self.context.run_id,
                        )
                    )
                ).scalar_one()
            )

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


@dataclass(slots=True)
class PlanAgentHarness:
    fixture: ExecutionFixture
    router: ScriptedPlanRouter
    agent: MultiClawAgent
    request: PlanStepExecutionRequest

    async def run(
        self,
        *,
        recovered_tool_result: PersistedToolResult | None = None,
        recovered_tool_input_json: str | None = None,
    ):
        return await self.agent.run_plan_step(
            self.request,
            run_lease_handle=RunLeaseHandle(self.fixture.lease),
            workflow_continuation=WorkflowContinuationService(
                self.fixture.database,
                settings=self.fixture.settings,
            ),
            recovered_tool_result=recovered_tool_result,
            recovered_tool_input_json=recovered_tool_input_json,
        )


def _build_plan_agent(
    fixture: ExecutionFixture,
    responses: list[LLMResponse],
    *,
    outcomes: list[ToolCallOutcome] | None = None,
) -> tuple[MultiClawAgent, ScriptedPlanRouter]:
    router = ScriptedPlanRouter(responses)
    agent = MultiClawAgent.__new__(MultiClawAgent)
    agent.settings = fixture.settings
    agent.router = router
    agent.registry = ToolRegistry()
    agent.skill_manager = SkillManager()
    if outcomes is not None:
        agent._execute_tool_batch = AsyncMock(return_value=outcomes)
    return agent, router


async def _plan_agent_harness(
    fixture: ExecutionFixture,
    responses: list[LLMResponse],
    *,
    outcomes: list[ToolCallOutcome] | None = None,
) -> PlanAgentHarness:
    await fixture.approve()
    started = await fixture.coordinator.start_next_attempt(
        context=fixture._context(),
        lease=fixture.lease,
    )
    assert started is not None
    agent, router = _build_plan_agent(fixture, responses, outcomes=outcomes)
    request = PlanStepExecutionRequest(
        context=fixture._context(),
        lease=fixture.lease,
        plan=started.plan,
        step=started.step,
        step_run=started.step_run,
        dependency_results=started.dependency_results,
    )
    return PlanAgentHarness(fixture, router, agent, request)


def _completion_response(completion: PlanStepCompletion) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="complete-1",
                name="complete_plan_step",
                arguments=completion.model_dump(mode="json"),
            )
        ],
    )


@pytest.mark.asyncio
async def test_free_text_cannot_complete_a_plan_step(execution_fixture):
    harness = await _plan_agent_harness(
        execution_fixture,
        [
            LLMResponse(content="Done", tool_calls=[]),
            LLMResponse(content="Still done", tool_calls=[]),
        ],
    )
    active_skill = _activate_test_skill(harness.agent.skill_manager)

    completion = await harness.run()

    assert completion.status == "failed"
    assert completion.summary == "Step completion protocol was not satisfied"
    assert completion.evidence == []
    assert completion.retryable is False
    assert len(harness.router.calls) == (
        execution_fixture.settings.agent.reflection_max_attempts + 1
    )
    assert await execution_fixture.count_status(PlanStepRunStatus.SUCCEEDED) == 0
    assert harness.router.calls[0]["tools"][-1] == {
        "type": "function",
        "function": {
            "name": "complete_plan_step",
            "description": "Finish the current Plan step with structured evidence.",
            "parameters": PlanStepCompletion.model_json_schema(),
        },
    }
    first_messages = harness.router.calls[0]["messages"]
    assert first_messages[:2] == [
        {
            "role": "system",
            "content": execution_fixture.settings.agent.system_prompt,
        },
        {
            "role": "system",
            "content": "Execute exactly one Plan step and finish with complete_plan_step.",
        },
    ]
    assert json.loads(first_messages[2]["content"]) == {
        "attempt": 1,
        "constraints": ["Keep execution serial"],
        "dependencies": [],
        "objective": "Deliver the durable Plan execution boundary",
        "remaining_attempts": 1,
        "step": {
            "description": "Execute lint.",
            "expected_outcome": "Lint succeeds.",
            "logical_step_key": "lint",
            "title": "Lint",
        },
    }
    assert first_messages[3]["role"] == "system"
    assert first_messages[3]["content"] == active_skill.format_instructions()


@pytest.mark.asyncio
async def test_valid_completion_call_returns_structured_plan_step_completion(
    execution_fixture,
):
    expected = PlanStepCompletion(
        status="succeeded",
        summary="The schema contract passes.",
        evidence=["pytest tests/test_migrations.py: pass"],
        retryable=False,
    )
    harness = await _plan_agent_harness(
        execution_fixture,
        [_completion_response(expected)],
    )

    completion = await harness.run()

    assert completion == expected


@pytest.mark.asyncio
async def test_malformed_completion_uses_only_bounded_repairs(execution_fixture):
    malformed = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="complete-invalid",
                name="complete_plan_step",
                arguments={"status": "succeeded"},
            )
        ],
    )
    harness = await _plan_agent_harness(
        execution_fixture,
        [malformed, malformed],
    )

    completion = await harness.run()

    assert completion == PlanStepCompletion(
        status="failed",
        summary="Step completion protocol was not satisfied",
        evidence=[],
        retryable=False,
    )
    assert len(harness.router.calls) == (
        execution_fixture.settings.agent.reflection_max_attempts + 1
    )
    assert "valid" in harness.router.calls[1]["messages"][-1]["content"].lower()


@pytest.mark.asyncio
async def test_ordinary_tool_result_returns_to_step_completion(execution_fixture):
    expected = PlanStepCompletion(
        status="succeeded",
        summary="Inspected the file",
        evidence=["read_file result"],
    )
    outcomes = [
        ToolCallOutcome(
            call_id="read-1",
            name="read_file",
            observation=Observation(
                type=ObservationType.TOOL_RESULT,
                content="README contents",
            ),
            result=ToolExecutionResult(
                status=ToolStatus.SUCCESS,
                content="README contents",
            ),
        )
    ]
    harness = await _plan_agent_harness(
        execution_fixture,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="read-1",
                        name="read_file",
                        arguments={"path": "README.md"},
                    )
                ],
            ),
            _completion_response(expected),
        ],
        outcomes=outcomes,
    )

    completion = await harness.run()

    assert completion == expected
    harness.agent._execute_tool_batch.assert_awaited_once()
    second_messages = harness.router.calls[1]["messages"]
    assert second_messages[-1] == {
        "role": "tool",
        "tool_call_id": "read-1",
        "content": "README contents",
    }


def test_internal_completion_protocol_is_not_a_registered_tool():
    registry = ToolRegistry()

    assert registry.get("complete_plan_step") is None
    assert all(
        schema["function"]["name"] != "complete_plan_step"
        for schema in registry.to_openai_schemas()
    )


@pytest.mark.asyncio
async def test_completion_call_cannot_mix_with_ordinary_tools(execution_fixture):
    mixed = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(id="read-1", name="read_file", arguments={"path": "README.md"}),
            ToolCall(
                id="complete-1",
                name="complete_plan_step",
                arguments={
                    "status": "succeeded",
                    "summary": "Must not be accepted",
                    "evidence": [],
                    "retryable": False,
                },
            ),
        ],
    )
    harness = await _plan_agent_harness(
        execution_fixture,
        [mixed, mixed],
        outcomes=[],
    )

    completion = await harness.run()

    assert completion.status == "failed"
    harness.agent._execute_tool_batch.assert_not_awaited()
    assert len(harness.router.calls) == 2
    assert "exactly once" in harness.router.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_recovered_tool_result_returns_to_step_completion_without_redispatch(
    execution_fixture,
):
    expected = PlanStepCompletion(
        status="succeeded",
        summary="Recovered result inspected",
        evidence=["persisted tool result"],
    )
    harness = await _plan_agent_harness(
        execution_fixture,
        [_completion_response(expected)],
        outcomes=[],
    )
    recovered = PersistedToolResult(
        entry_id="result-1",
        result_ref="memory://result-1",
        result_digest="a" * 64,
        content="persisted README contents",
        tool_call_id="read-1",
        tool_name="read_file",
    )

    completion = await harness.run(
        recovered_tool_result=recovered,
        recovered_tool_input_json=json.dumps({"path": "README.md"}),
    )

    assert completion == expected
    harness.agent._execute_tool_batch.assert_not_awaited()
    first_messages = harness.router.calls[0]["messages"]
    assert first_messages[-2]["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": json.dumps({"path": "README.md"}, ensure_ascii=False),
    }
    assert first_messages[-1] == {
        "role": "tool",
        "tool_call_id": "read-1",
        "content": "persisted README contents",
    }


@pytest.mark.asyncio
async def test_tool_approval_stops_plan_step_without_completion(execution_fixture):
    outcomes = [
        ToolCallOutcome(
            call_id="write-1",
            name="write_file",
            observation=Observation(
                type=ObservationType.TOOL_RESULT,
                content="approval required",
            ),
            result=ToolExecutionResult(
                status=ToolStatus.AWAITING_APPROVAL,
                content="approval required",
            ),
        )
    ]
    harness = await _plan_agent_harness(
        execution_fixture,
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="write_file",
                        arguments={"path": "artifact.txt", "content": "result"},
                    )
                ],
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="complete-1",
                        name="complete_plan_step",
                        arguments={
                            "status": "succeeded",
                            "summary": "Must not be reached",
                            "evidence": [],
                            "retryable": False,
                        },
                    )
                ],
            ),
        ],
        outcomes=outcomes,
    )

    outcome = await harness.run()

    assert outcome == ContinuationOutcome(
        state=ContinuationState.AWAITING_USER,
        detail="tool awaiting approval",
    )
    assert len(harness.router.calls) == 1
    assert await execution_fixture.count_status(PlanStepRunStatus.RUNNING) == 1


@pytest.mark.asyncio
async def test_valid_completion_is_persisted_before_step_success(
    execution_fixture,
    monkeypatch,
):
    completion = PlanStepCompletion(
        status="succeeded",
        summary="The schema passes with api_key=sk_live_secret.",
        evidence=["Authorization: Bearer secret-token"],
    )
    runner = ScriptedStepRunner([completion])
    _activate_test_skill(runner.skill_manager)
    tool_schemas = [
        {"function": {"name": "inspect", "api_key": "sk_tool_secret"}}
    ]
    monkeypatch.setattr(runner.registry, "to_openai_schemas", lambda: tool_schemas)
    original_finish = PlanRepository.finish_step_attempt
    result_was_visible_before_finish = False

    async def observe_finish(repository, lease, **kwargs):
        nonlocal result_was_visible_before_finish
        result_ref = kwargs["result_ref"]
        entry = await MemoryRepository(
            repository.connection,
            execution_fixture._context(),
            execution_fixture.database.dialect,
        ).get(result_ref.removeprefix("memory:"), execution_fixture._context().session_id)
        result_was_visible_before_finish = entry is not None
        return await original_finish(repository, lease, **kwargs)

    monkeypatch.setattr(PlanRepository, "finish_step_attempt", observe_finish)

    outcome = await execution_fixture.execute(
        runner,
        draft=_draft(("lint",)),
    )
    attempt = await execution_fixture.latest_attempt()
    document = await execution_fixture.load_result(attempt.result_ref)

    assert outcome.state == "completed"
    assert result_was_visible_before_finish is True
    assert attempt.status is PlanStepRunStatus.SUCCEEDED
    assert attempt.result_digest == document.digest()
    assert document.definition_digest == runner.calls[0].step.definition_digest
    assert document.dependency_result_digests == {}
    assert document.summary == redact(completion.summary)
    assert document.evidence == redact(completion.evidence)
    assert document.tool_catalog_digest == _digest(redact(tool_schemas))
    assert document.policy_digest == _digest(
        redact(execution_fixture.settings.governance.model_dump(mode="json"))
    )
    assert document.skill_set_digest == _digest(["audit"])
    assert document.public_context() == {
        "step_id": document.step_id,
        "status": "succeeded",
        "summary": document.summary,
        "evidence": document.evidence,
        "result_digest": document.digest(),
    }
    checkpoint = await execution_fixture.latest_checkpoint()
    payload = json.loads(str(checkpoint["payload_json"]))
    assert checkpoint["phase"] == CheckpointPhase.PLAN_STEP_READY.value
    assert payload["execution_cursor"] == "select_next"


@pytest.mark.asyncio
async def test_success_finalization_uses_lease_refreshed_during_runner(
    execution_fixture,
):
    scripted = ScriptedStepRunner(
        [
            PlanStepCompletion(
                status="succeeded",
                summary="Heartbeat-safe success.",
                evidence=["runner refreshed the lease"],
            )
        ]
    )
    runner = HeartbeatStepRunner(
        execution_fixture.database,
        execution_fixture.settings,
        scripted,
    )

    outcome = await execution_fixture.execute(
        runner,
        draft=_draft(("lint",)),
    )

    attempt = await execution_fixture.latest_attempt()
    assert outcome.state == "completed"
    assert attempt.status is PlanStepRunStatus.SUCCEEDED
    assert attempt.result_ref is not None
    assert await execution_fixture.count_results() == 1
    assert (await execution_fixture.load_result(attempt.result_ref)).status == "succeeded"


@pytest.mark.asyncio
async def test_result_and_attempt_finish_roll_back_when_checkpoint_fails(
    execution_fixture,
    monkeypatch,
):
    runner = ScriptedStepRunner(
        [
            PlanStepCompletion(
                status="succeeded",
                summary="Must roll back",
                evidence=[],
            )
        ]
    )
    original_checkpoint = WorkflowCoordinator.checkpoint

    async def fail_select_next(workflow, lease, phase, payload, **kwargs):
        if (
            phase is CheckpointPhase.PLAN_STEP_READY
            and payload.get("execution_cursor") == "select_next"
        ):
            raise RuntimeError("injected completion checkpoint failure")
        return await original_checkpoint(
            workflow,
            lease,
            phase,
            payload,
            **kwargs,
        )

    monkeypatch.setattr(WorkflowCoordinator, "checkpoint", fail_select_next)

    with pytest.raises(RuntimeError, match="injected completion checkpoint failure"):
        await execution_fixture.execute(
            runner,
            draft=_draft(("lint",)),
        )

    attempt = await execution_fixture.latest_attempt()
    assert attempt.status is PlanStepRunStatus.RUNNING
    assert attempt.result_ref is None
    assert await execution_fixture.count_results() == 0


@pytest.mark.asyncio
async def test_retryable_completion_creates_only_bounded_attempts(execution_fixture):
    execution_fixture.settings.planning.max_step_attempts = 3
    await execution_fixture.materialize(_draft(("lint",), max_attempts=3))
    execution_fixture.settings.planning.max_step_attempts = 2
    runner = ScriptedStepRunner(
        [
            PlanStepCompletion(
                status="failed",
                summary="Transient one",
                evidence=[],
                retryable=True,
            ),
            PlanStepCompletion(
                status="failed",
                summary="Transient two",
                evidence=[],
                retryable=True,
            ),
            PlanStepCompletion(
                status="succeeded",
                summary="Late success",
                evidence=[],
            ),
        ]
    )

    outcome = await execution_fixture.execute(runner)

    assert outcome.state == "replan_required"
    assert await execution_fixture.attempt_statuses() == [
        PlanStepRunStatus.FAILED_RETRYABLE,
        PlanStepRunStatus.FAILED_TERMINAL,
    ]
    assert len(runner.calls) == 2
    attempts = await execution_fixture.all_attempts()
    for attempt in attempts:
        document = await execution_fixture.load_result(attempt.result_ref)
        assert document.status == "failed"
        assert document.summary == attempt.result_summary
        assert document.digest() == attempt.result_digest
        assert attempt.error_code == "plan_step_failed"
        assert attempt.error_detail_redacted == document.summary


@pytest.mark.asyncio
async def test_retryable_finalization_uses_lease_refreshed_during_runner(
    execution_fixture,
):
    scripted = ScriptedStepRunner(
        [
            PlanStepCompletion(
                status="failed",
                summary="Retry after heartbeat.",
                evidence=[],
                retryable=True,
            ),
            PlanStepCompletion(
                status="succeeded",
                summary="Second attempt succeeds.",
                evidence=["bounded retry"],
            ),
        ]
    )
    runner = HeartbeatStepRunner(
        execution_fixture.database,
        execution_fixture.settings,
        scripted,
    )

    outcome = await execution_fixture.execute(
        runner,
        draft=_draft(("lint",), max_attempts=2),
    )

    attempts = await execution_fixture.all_attempts()
    assert outcome.state == "completed"
    assert [attempt.status for attempt in attempts] == [
        PlanStepRunStatus.FAILED_RETRYABLE,
        PlanStepRunStatus.SUCCEEDED,
    ]
    assert [attempt.attempt for attempt in attempts] == [1, 2]
    assert len(scripted.calls) == 2
    assert await execution_fixture.count_results() == 2


@pytest.mark.asyncio
async def test_tool_approval_keeps_run_and_attempt_resumable(execution_fixture):
    outcome = await execution_fixture.execute(
        ApprovalStepRunner(
            execution_fixture.database,
            execution_fixture.settings,
        ),
        draft=_draft(("lint",)),
    )

    run = await execution_fixture.service.workflow.get_run(execution_fixture._context())
    attempt = await execution_fixture.latest_attempt()
    assert outcome.state == "awaiting_user"
    assert run is not None
    assert run.status is RunStatus.AWAITING_USER
    assert attempt.status is PlanStepRunStatus.RUNNING
    assert attempt.result_ref is None
    checkpoint = await execution_fixture.latest_checkpoint()
    assert checkpoint["phase"] == CheckpointPhase.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_recovered_approval_reuses_running_attempt_without_redispatch(
    execution_fixture,
):
    await execution_fixture.materialize(_draft(("lint",)))
    await execution_fixture.approve()
    lease_handle = RunLeaseHandle(execution_fixture.lease)

    first_outcome = await execution_fixture.coordinator.execute_to_boundary(
        context=execution_fixture._context(),
        run_lease_handle=lease_handle,
        runner=ApprovalStepRunner(
            execution_fixture.database,
            execution_fixture.settings,
        ),
    )
    original_attempt = await execution_fixture.latest_attempt()
    assert first_outcome.state == "awaiting_user"

    resumed = await execution_fixture.service.workflow.transition_run(
        await lease_handle.current(),
        RunStatus.RESUMING,
    )
    await lease_handle.replace(resumed)
    completion = PlanStepCompletion(
        status="succeeded",
        summary="Recovered approval result completed the step.",
        evidence=["persisted write result"],
    )
    agent, router = _build_plan_agent(
        execution_fixture,
        [_completion_response(completion)],
        outcomes=[],
    )
    recovered = PersistedToolResult(
        entry_id="result-approval-1",
        result_ref="memory:result-approval-1",
        result_digest="a" * 64,
        content="approved write completed",
        tool_call_id="approval-tool-1",
        tool_name="write_file",
    )
    recovered_input_json = json.dumps(
        {"path": "artifact.txt", "content": "result"},
        sort_keys=True,
        separators=(",", ":"),
    )

    outcome = await execution_fixture.coordinator.execute_to_boundary(
        context=execution_fixture._context(),
        run_lease_handle=lease_handle,
        runner=HeartbeatStepRunner(
            execution_fixture.database,
            execution_fixture.settings,
            agent,
        ),
        recovered_tool_result=recovered,
        recovered_tool_input_json=recovered_input_json,
    )

    attempts = await execution_fixture.all_attempts()
    assert outcome.state == "completed"
    assert len(attempts) == 1
    assert attempts[0].step_run_id == original_attempt.step_run_id
    assert attempts[0].attempt == original_attempt.attempt == 1
    assert attempts[0].status is PlanStepRunStatus.SUCCEEDED
    assert attempts[0].result_ref is not None
    assert (await execution_fixture.load_result(attempts[0].result_ref)).status == (
        "succeeded"
    )
    agent._execute_tool_batch.assert_not_awaited()
    recovered_messages = router.calls[0]["messages"][-2:]
    assert json.loads(
        recovered_messages[0]["tool_calls"][0]["function"]["arguments"]
    ) == json.loads(recovered_input_json)
    assert recovered_messages[1]["content"] == "approved write completed"
    checkpoints = await execution_fixture.checkpoints()
    dispatches = [
        checkpoint
        for checkpoint in checkpoints
        if checkpoint["phase"] == CheckpointPhase.PLAN_STEP_READY.value
        and json.loads(str(checkpoint["payload_json"]))["execution_cursor"]
        == "dispatch_step"
    ]
    assert len(dispatches) == 1
    final_checkpoint = checkpoints[-1]
    assert final_checkpoint["phase"] == CheckpointPhase.PLAN_STEP_READY.value
    assert json.loads(str(final_checkpoint["payload_json"]))["execution_cursor"] == (
        "select_next"
    )


@pytest.mark.asyncio
async def test_recovered_result_rejects_running_attempt_from_prior_plan_version(
    execution_fixture,
):
    await execution_fixture.materialize(_draft(("lint",)))
    await execution_fixture.approve()
    lease_handle = RunLeaseHandle(execution_fixture.lease)
    await execution_fixture.coordinator.execute_to_boundary(
        context=execution_fixture._context(),
        run_lease_handle=lease_handle,
        runner=ApprovalStepRunner(
            execution_fixture.database,
            execution_fixture.settings,
        ),
    )
    await execution_fixture.activate_second_version()
    resumed = await execution_fixture.service.workflow.transition_run(
        await lease_handle.current(),
        RunStatus.RESUMING,
    )
    await lease_handle.replace(resumed)
    agent, router = _build_plan_agent(execution_fixture, [], outcomes=[])

    with pytest.raises(
        PlanExecutionBlocked,
        match="running step attempt is inconsistent",
    ):
        await execution_fixture.coordinator.execute_to_boundary(
            context=execution_fixture._context(),
            run_lease_handle=lease_handle,
            runner=agent,
            recovered_tool_result=PersistedToolResult(
                entry_id="result-stale-1",
                result_ref="memory:result-stale-1",
                result_digest="b" * 64,
                content="stale result",
                tool_call_id="approval-tool-1",
                tool_name="write_file",
            ),
            recovered_tool_input_json="{}",
        )

    assert router.calls == []
    agent._execute_tool_batch.assert_not_awaited()
    assert await execution_fixture.count_step_runs() == 1
    assert await execution_fixture.count_status(PlanStepRunStatus.RUNNING) == 1
    dispatches = [
        checkpoint
        for checkpoint in await execution_fixture.checkpoints()
        if checkpoint["phase"] == CheckpointPhase.PLAN_STEP_READY.value
        and json.loads(str(checkpoint["payload_json"]))["execution_cursor"]
        == "dispatch_step"
    ]
    assert len(dispatches) == 1


def test_selector_uses_persisted_ordinal_then_step_id_as_tie_breaker():
    plan = _draft()
    # This direct pure-function test is supplied with records in reverse ID order;
    # ordinal remains authoritative and step_id only breaks corrupt/equal ordinals.
    from multiclaw.planner.models import PlanStepRecord, PlanVersionRecord
    from multiclaw.planner.validation import validate_plan_draft

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


@pytest.mark.parametrize(
    ("mutation", "blocked_reason"),
    (
        ("unapproved", "run is not executable"),
        ("current_ahead", "approved current active version"),
        ("active_behind", "approved current active version"),
    ),
)
@pytest.mark.asyncio
async def test_unapproved_or_stale_active_version_dispatches_no_step(
    execution_fixture,
    mutation,
    blocked_reason,
):
    await execution_fixture.prepare_version_mutation(mutation)
    with pytest.raises(PlanExecutionBlocked, match=blocked_reason):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )
    assert await execution_fixture.count_step_runs() == 0


@pytest.mark.parametrize(
    "status",
    (
        RunStatus.AWAITING_USER,
        RunStatus.COMPLETED,
        RunStatus.FAILED_TERMINAL,
        RunStatus.CANCELLED,
    ),
)
@pytest.mark.asyncio
async def test_non_executable_run_dispatches_no_attempt_or_checkpoint(
    execution_fixture,
    status,
):
    await execution_fixture.approve()
    before_checkpoints = await execution_fixture.count_checkpoints()
    await execution_fixture.update_run(run_status=status.value)

    with pytest.raises(PlanExecutionBlocked, match="run is not executable"):
        await execution_fixture.coordinator.select_next(
            context=execution_fixture._context(),
        )
    with pytest.raises(PlanExecutionBlocked, match="run is not executable"):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )

    assert await execution_fixture.count_step_runs() == 0
    assert await execution_fixture.count_checkpoints() == before_checkpoints


@pytest.mark.asyncio
async def test_cancel_requested_run_blocks_pure_selection_and_mutation(
    execution_fixture,
):
    await execution_fixture.approve()
    before_checkpoints = await execution_fixture.count_checkpoints()
    await execution_fixture.update_run(
        run_status=RunStatus.RUNNING.value,
        cancel_requested_at=123,
    )

    with pytest.raises(PlanExecutionBlocked, match="run is not executable"):
        await execution_fixture.coordinator.select_next(
            context=execution_fixture._context(),
        )
    with pytest.raises(PlanExecutionBlocked, match="run is not executable"):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )

    assert await execution_fixture.count_step_runs() == 0
    assert await execution_fixture.count_checkpoints() == before_checkpoints


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
async def test_running_attempt_from_prior_plan_version_blocks_dispatch(
    execution_fixture,
):
    await execution_fixture.approve_with_two_roots()
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    await execution_fixture.activate_second_version()
    before_checkpoints = await execution_fixture.count_checkpoints()

    with pytest.raises(PlanStepAlreadyRunningError):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )

    assert await execution_fixture.count_status(PlanStepRunStatus.RUNNING) == 1
    assert await execution_fixture.count_checkpoints() == before_checkpoints


@pytest.mark.asyncio
async def test_run_lock_interleaving_reselects_after_prior_step_succeeds(
    execution_fixture,
    monkeypatch,
):
    await execution_fixture.approve_with_two_roots()
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    await execution_fixture.succeed(first.step_run, digest="a" * 64)
    await execution_fixture.update_step_run(
        first.step_run,
        status=PlanStepRunStatus.FAILED_RETRYABLE.value,
    )

    dialect_type = type(execution_fixture.database.dialect)
    original_lock_run = dialect_type.lock_run
    interleaved = False

    async def succeed_when_run_locks(dialect, connection, context):
        nonlocal interleaved
        await original_lock_run(dialect, connection, context)
        if interleaved:
            return
        interleaved = True
        await connection.execute(
            update(agent_plan_step_runs)
            .where(
                agent_plan_step_runs.c.tenant_id == context.tenant_id,
                agent_plan_step_runs.c.workspace_id == context.workspace_id,
                agent_plan_step_runs.c.session_id == context.session_id,
                agent_plan_step_runs.c.run_id == context.run_id,
                agent_plan_step_runs.c.step_run_id == first.step_run.step_run_id,
            )
            .values(status=PlanStepRunStatus.SUCCEEDED.value)
        )

    monkeypatch.setattr(dialect_type, "lock_run", succeed_when_run_locks)

    started = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )

    assert started is not None
    assert started.step.logical_step_key == "test"
    assert await execution_fixture.count_step_attempts(first.step.step_id) == 1


@pytest.mark.asyncio
async def test_attempt_insert_rechecks_run_state_immediately_before_write(
    execution_fixture,
    monkeypatch,
):
    await execution_fixture.approve()
    before_checkpoints = await execution_fixture.count_checkpoints()
    original_db_now = PlanRepository._db_now_ms
    cancel_injected = False

    async def cancel_before_insert(repository):
        nonlocal cancel_injected
        now = await original_db_now(repository)
        if cancel_injected:
            return now
        cancel_injected = True
        await repository.connection.execute(
            update(agent_runs)
            .where(
                agent_runs.c.tenant_id == execution_fixture._context().tenant_id,
                agent_runs.c.workspace_id == execution_fixture._context().workspace_id,
                agent_runs.c.session_id == execution_fixture._context().session_id,
                agent_runs.c.run_id == execution_fixture._context().run_id,
            )
            .values(cancel_requested_at=now)
        )
        return now

    monkeypatch.setattr(PlanRepository, "_db_now_ms", cancel_before_insert)

    with pytest.raises(PlanExecutionBlocked, match="run is not executable"):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )

    assert await execution_fixture.count_step_runs() == 0
    assert await execution_fixture.count_checkpoints() == before_checkpoints


@pytest.mark.asyncio
async def test_attempt_insert_rechecks_run_wide_running_gate(
    execution_fixture,
    monkeypatch,
):
    await execution_fixture.approve_with_two_roots()
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    await execution_fixture.finish(
        first.step_run,
        PlanStepRunStatus.FAILED_RETRYABLE,
    )
    await execution_fixture.activate_second_version()
    before_checkpoints = await execution_fixture.count_checkpoints()
    original_require_ready = PlanRepository._require_step_ready
    readiness_checks = 0

    async def inject_prior_version_running(repository, **kwargs):
        nonlocal readiness_checks
        result = await original_require_ready(repository, **kwargs)
        readiness_checks += 1
        if readiness_checks == 2:
            await repository.connection.execute(
                update(agent_plan_step_runs)
                .where(
                    agent_plan_step_runs.c.tenant_id
                    == execution_fixture._context().tenant_id,
                    agent_plan_step_runs.c.workspace_id
                    == execution_fixture._context().workspace_id,
                    agent_plan_step_runs.c.session_id
                    == execution_fixture._context().session_id,
                    agent_plan_step_runs.c.run_id
                    == execution_fixture._context().run_id,
                    agent_plan_step_runs.c.step_run_id
                    == first.step_run.step_run_id,
                )
                .values(status=PlanStepRunStatus.RUNNING.value)
            )
        return result

    monkeypatch.setattr(
        PlanRepository,
        "_require_step_ready",
        inject_prior_version_running,
    )

    with pytest.raises(PlanStepAlreadyRunningError):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )

    assert readiness_checks == 2
    assert await execution_fixture.count_step_runs() == 1
    assert await execution_fixture.count_checkpoints() == before_checkpoints


@pytest.mark.asyncio
async def test_attempt_insert_revalidates_succeeded_step_is_not_ready(
    execution_fixture,
):
    await execution_fixture.approve_with_two_roots()
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    await execution_fixture.succeed(first.step_run, digest="a" * 64)

    with pytest.raises(PlanExecutionBlocked, match="step is no longer ready"):
        async with TenantUnitOfWork(
            execution_fixture.database,
            execution_fixture._context(),
            planning_settings=execution_fixture.settings.planning,
        ) as uow:
            await uow.plans.create_step_attempt(
                execution_fixture.lease,
                plan_id=first.plan.plan_id,
                plan_version=first.plan.current_version,
                step_id=first.step.step_id,
            )

    assert await execution_fixture.count_step_runs() == 1


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
async def test_selector_rejects_incomplete_persisted_dependency_document(
    execution_fixture,
):
    await execution_fixture.approve_chain(["inspect", "verify"])
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    document = PlanStepResultDocument(
        plan_id=first.step_run.plan_id,
        plan_version=first.step_run.plan_version,
        run_id=first.step_run.run_id,
        step_id=first.step_run.step_id,
        step_run_id=first.step_run.step_run_id,
        attempt=first.step_run.attempt,
        status="succeeded",
        summary="inspect complete",
        evidence=[],
        definition_digest=first.step.definition_digest,
        dependency_result_digests={},
        tool_catalog_digest="a" * 64,
        policy_digest="b" * 64,
        skill_set_digest="c" * 64,
    )
    incomplete_content = json.dumps(
        document.model_dump(mode="json", exclude={"evidence"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    async with TenantUnitOfWork(
        execution_fixture.database,
        execution_fixture._context(),
    ) as uow:
        entry = await uow.memory.save(
            MemoryEntry(
                content=incomplete_content,
                type="plan_step_result",
                role="assistant",
                session_id=execution_fixture._context().session_id,
                metadata={
                    "schema_version": 1,
                    "plan_id": first.step_run.plan_id,
                    "step_run_id": first.step_run.step_run_id,
                },
            )
        )
        await uow.conn.execute(
            update(agent_plan_step_runs)
            .where(
                agent_plan_step_runs.c.tenant_id
                == execution_fixture._context().tenant_id,
                agent_plan_step_runs.c.workspace_id
                == execution_fixture._context().workspace_id,
                agent_plan_step_runs.c.session_id
                == execution_fixture._context().session_id,
                agent_plan_step_runs.c.run_id == first.step_run.run_id,
                agent_plan_step_runs.c.step_run_id == first.step_run.step_run_id,
            )
            .values(
                status=PlanStepRunStatus.SUCCEEDED.value,
                result_summary=document.summary,
                result_ref=f"memory:{entry.id}",
                result_digest=document.digest(),
                version=first.step_run.version + 1,
                finished_at=first.step_run.started_at + 1,
            )
        )

    with pytest.raises(PlanExecutionBlocked, match="document is invalid"):
        await execution_fixture.coordinator.select_next(
            context=execution_fixture._context(),
        )


@pytest.mark.asyncio
async def test_disconnected_succeeded_step_without_result_blocks_advancement(
    execution_fixture,
):
    await execution_fixture.approve_with_two_roots()
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    await execution_fixture.update_step_run(
        first.step_run,
        status=PlanStepRunStatus.SUCCEEDED.value,
        result_summary=None,
        result_ref=None,
        result_digest=None,
        finished_at=first.step_run.started_at + 1,
    )

    with pytest.raises(PlanExecutionBlocked, match="succeeded step result"):
        await execution_fixture.coordinator.select_next(
            context=execution_fixture._context(),
        )
    with pytest.raises(PlanExecutionBlocked, match="succeeded step result"):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )

    assert await execution_fixture.count_step_runs() == 1


@pytest.mark.asyncio
async def test_final_succeeded_step_with_missing_result_cannot_complete(
    execution_fixture,
):
    await execution_fixture.materialize(_draft(("finalize",)))
    await execution_fixture.approve()
    final = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert final is not None
    await execution_fixture.update_step_run(
        final.step_run,
        status=PlanStepRunStatus.SUCCEEDED.value,
        result_summary="finalize complete",
        result_ref="memory:missing-result",
        result_digest="a" * 64,
        finished_at=final.step_run.started_at + 1,
    )

    with pytest.raises(PlanExecutionBlocked, match="result document is unavailable"):
        await execution_fixture.coordinator.select_next(
            context=execution_fixture._context(),
        )
    with pytest.raises(PlanExecutionBlocked, match="result document is unavailable"):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
        )

    assert await execution_fixture.count_step_runs() == 1


@pytest.mark.parametrize("proof_mutation", ("missing", "extra", "mismatch"))
@pytest.mark.asyncio
async def test_selector_rejects_inexact_transitive_result_proof(
    execution_fixture,
    proof_mutation,
):
    await execution_fixture.approve_chain(["collect", "transform", "publish"])
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    first_document = await execution_fixture.succeed(
        first.step_run,
        digest="a" * 64,
    )
    second = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert second is not None
    expected_proof = {first.step.logical_step_key: first_document.digest()}
    if proof_mutation == "missing":
        actual_proof = {}
    elif proof_mutation == "extra":
        actual_proof = {**expected_proof, "unrelated": "e" * 64}
    else:
        actual_proof = {first.step.logical_step_key: "f" * 64}
    await execution_fixture.succeed(
        second.step_run,
        digest="d" * 64,
        dependency_result_digests=actual_proof,
    )

    with pytest.raises(PlanExecutionBlocked, match="dependency result proof"):
        await execution_fixture.coordinator.select_next(
            context=execution_fixture._context(),
        )


@pytest.mark.asyncio
async def test_selector_rejects_oversized_persisted_result_before_json_parsing(
    execution_fixture,
):
    await execution_fixture.approve_chain(["collect", "publish"])
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    await execution_fixture.succeed(
        first.step_run,
        digest="a" * 64,
        content_prefix=" " * 262_145,
    )

    with pytest.raises(PlanExecutionBlocked, match="exceeds 262144 bytes"):
        await execution_fixture.coordinator.select_next(
            context=execution_fixture._context(),
        )


@pytest.mark.asyncio
async def test_mutation_locks_results_in_topological_order_after_run_lock(
    execution_fixture,
    monkeypatch,
):
    await execution_fixture.approve_chain(["collect", "transform", "publish"])
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    first_document = await execution_fixture.succeed(
        first.step_run,
        digest="a" * 64,
    )
    second = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert second is not None
    await execution_fixture.succeed(
        second.step_run,
        digest="d" * 64,
        dependency_result_digests={
            first.step.logical_step_key: first_document.digest(),
        },
    )
    expected_result_ids = await execution_fixture.succeeded_result_ids(
        [first.step.step_id, second.step.step_id]
    )

    dialect_type = type(execution_fixture.database.dialect)
    original_lock_run = dialect_type.lock_run
    original_memory_get = MemoryRepository.get
    run_locked = False
    locked_result_ids: list[str] = []

    async def record_run_lock(dialect, connection, context):
        nonlocal run_locked
        await original_lock_run(dialect, connection, context)
        run_locked = True

    async def record_result_lock(
        repository,
        entry_id,
        target_session_id=None,
        *,
        for_update=False,
    ):
        assert run_locked
        assert repository.connection.in_transaction()
        assert for_update is True
        locked_result_ids.append(entry_id)
        return await original_memory_get(
            repository,
            entry_id,
            target_session_id,
            for_update=for_update,
        )

    monkeypatch.setattr(dialect_type, "lock_run", record_run_lock)
    monkeypatch.setattr(MemoryRepository, "get", record_result_lock)

    started = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )

    assert started is not None
    assert started.step.logical_step_key == "publish"
    assert [item.step_id for item in started.dependency_results] == [
        second.step.step_id
    ]
    assert locked_result_ids == expected_result_ids


@pytest.mark.asyncio
async def test_pure_selection_reads_verified_results_without_row_locks(
    execution_fixture,
    monkeypatch,
):
    await execution_fixture.approve_chain(["collect", "publish"])
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(),
        lease=execution_fixture.lease,
    )
    assert first is not None
    await execution_fixture.succeed(first.step_run, digest="a" * 64)
    original_memory_get = MemoryRepository.get
    lock_requests: list[bool] = []

    async def record_lock_request(
        repository,
        entry_id,
        target_session_id=None,
        *,
        for_update=False,
    ):
        lock_requests.append(for_update)
        return await original_memory_get(
            repository,
            entry_id,
            target_session_id,
            for_update=for_update,
        )

    monkeypatch.setattr(MemoryRepository, "get", record_lock_request)

    ready = await execution_fixture.coordinator.select_next(
        context=execution_fixture._context(),
    )

    assert ready is not None
    assert lock_requests == [False]


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


def test_reuse_requires_a_complete_source_proof() -> None:
    """A missing immutable predecessor is never treated as compatible reuse."""
    new_step = PlanStepRecord(
        step_id=str(uuid4()),
        logical_step_key="publish",
        supersedes_step_id=str(uuid4()),
        ordinal=1,
        title="Publish",
        description="Publish the verified artifact.",
        expected_outcome="Artifact is published.",
        assigned_agent_profile_id=None,
        max_attempts=1,
        definition_digest="a" * 64,
    )

    assert not can_reuse_result(
        current_run_id=str(uuid4()),
        new_step=new_step,
        source_step=None,
        source_run=None,
        source_result=None,
        new_dependency_ids=frozenset(),
        dependency_proofs={},
        current_tool_catalog_digest="b" * 64,
        current_policy_digest="c" * 64,
        current_skill_set_digest="d" * 64,
    )


@pytest.mark.asyncio
async def test_copied_reuse_lineage_is_accepted_as_dependency_evidence(
    execution_fixture,
) -> None:
    """A reused row deliberately points to its immutable source document."""
    await execution_fixture.approve_chain(["collect", "publish"])
    source_started = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(), lease=execution_fixture.lease
    )
    assert source_started is not None
    source_document = await execution_fixture.succeed(
        source_started.step_run, digest="a" * 64
    )
    context = execution_fixture._context()
    assert execution_fixture.plan_id is not None
    assert execution_fixture.aggregate_version is not None
    async with TenantUnitOfWork(
        execution_fixture.database,
        context,
        planning_settings=execution_fixture.settings.planning,
    ) as uow:
        original = await uow.plans.get(execution_fixture.plan_id)
        assert original is not None
        revised = await uow.plans.append_version(
            plan_id=original.plan_id,
            expected_version=original.aggregate_version,
            draft=_draft(("collect", "publish"), chain=True),
            parent_version=original.current_version,
            revision_feedback="retain verified collection",
            supersedes={step.logical_step_key: step.step_id for step in original.current.steps},
        )
        await uow.conn.execute(
            update(agent_plans)
            .where(agent_plans.c.id == revised.plan_id)
            .values(status="approved", approved_version=revised.current_version)
        )
        await uow.conn.execute(
            update(agent_runs)
            .where(agent_runs.c.run_id == context.run_id)
            .values(active_plan_version=revised.current_version)
        )
        source_row = await uow.plans.step_run_by_id(
            run_id=str(context.run_id), step_run_id=source_started.step_run.step_run_id
        )
        assert source_row is not None
        assert can_reuse_result(
            current_run_id=str(context.run_id),
            new_step=revised.current.steps[0],
            source_step=original.current.steps[0],
            source_run=source_row,
            source_result=source_document,
            new_dependency_ids=frozenset(),
            dependency_proofs={},
            current_tool_catalog_digest=source_document.tool_catalog_digest,
            current_policy_digest=source_document.policy_digest,
            current_skill_set_digest=source_document.skill_set_digest,
        )
        revised_collect = next(
            step for step in revised.current.steps if step.logical_step_key == "collect"
        )
        reused = await uow.plans.create_reused_step_attempt(
            execution_fixture.lease,
            plan_id=revised.plan_id,
            plan_version=revised.current_version,
            step_id=revised_collect.step_id,
            source_step_run_id=source_started.step_run.step_run_id,
        )
        source_row = await uow.plans.step_run_by_id(
            run_id=str(context.run_id),
            step_run_id=source_started.step_run.step_run_id,
        )
        assert source_row is not None

    ready = await execution_fixture.coordinator.select_next(context=context)

    assert reused.result_ref == source_row.result_ref
    assert reused.result_digest == source_document.digest()
    assert ready is not None
    assert ready.step.logical_step_key == "publish"
    assert ready.dependency_results == (source_document,)


@pytest.mark.asyncio
async def test_failure_revision_materializes_waiting_version_after_replan_checkpoint(
    execution_fixture,
) -> None:
    """Failure revision appends review work without advancing the active version."""
    await execution_fixture.approve_chain(["collect", "publish"])
    execution_fixture.current_lease = await WorkflowCoordinator(
        execution_fixture.database, settings=execution_fixture.settings
    ).transition_run(execution_fixture.lease, RunStatus.RUNNING)
    started = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(), lease=execution_fixture.lease
    )
    assert started is not None
    await execution_fixture.finish(started.step_run, PlanStepRunStatus.FAILED_TERMINAL)
    plan = await execution_fixture.coordinator._load_active_plan(execution_fixture._context())
    failed = await execution_fixture.latest_attempt()
    failure_digest = hashlib.sha256(
        json.dumps(
            {
                "step_run_id": failed.step_run_id,
                "error_code": failed.error_code,
                "error_detail_redacted": failed.error_detail_redacted,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    await WorkflowCoordinator(
        execution_fixture.database, settings=execution_fixture.settings
    ).checkpoint(
        execution_fixture.lease,
        CheckpointPhase.PLAN_REPLAN_REQUIRED,
        {
            "run_id": str(execution_fixture._context().run_id),
            "plan_id": plan.plan_id,
            "plan_version": plan.current_version,
            "plan_digest": plan.current.content_digest,
            "failed_step_run_id": failed.step_run_id,
            "failure_digest": failure_digest,
            "revision_cursor": "generate_revision",
            "cursor": "generate_revision",
        },
    )

    materialized = await execution_fixture.service.materialize_failure_revision(
        FailureRevisionRequest(
            context=execution_fixture._context(),
            lease=execution_fixture.lease,
            plan=plan,
            failed_step_run=failed,
            draft=_draft(("collect", "publish"), chain=True),
        )
    )

    assert materialized.plan.current_version == 2
    assert materialized.plan.approved_version == 1
    assert materialized.run.status is RunStatus.AWAITING_USER
    assert materialized.run.active_plan_version == 1
    assert [checkpoint["phase"] for checkpoint in (await execution_fixture.checkpoints())[-2:]] == [
        CheckpointPhase.PLAN_REPLAN_REQUIRED.value,
        CheckpointPhase.PLAN_AWAITING_APPROVAL.value,
    ]


@pytest.mark.asyncio
async def test_terminal_execution_checkpoints_before_generator_and_waits_for_revision(
    execution_fixture,
) -> None:
    """The generator must observe a durable replan boundary, never a bare failure."""
    draft = _draft(("lint",), max_attempts=1)
    observed_phases: list[str] = []

    async def observe_checkpoint() -> None:
        observed_phases.extend(
            str(item["phase"]) for item in await execution_fixture.checkpoints()
        )

    generator = ObservingFailureGenerator(draft, observe_checkpoint)
    execution_fixture.service._generator = generator
    coordinator = PlanExecutionCoordinator(
        execution_fixture.database,
        settings=execution_fixture.settings,
        planning_service=execution_fixture.service,
    )
    await execution_fixture.materialize(draft)
    await execution_fixture.approve()
    outcome = await coordinator.execute_to_boundary(
        context=execution_fixture._context(),
        run_lease_handle=RunLeaseHandle(execution_fixture.lease),
        runner=ScriptedStepRunner(
            [
                PlanStepCompletion(
                    status="failed",
                    summary="bounded failure",
                    evidence=[],
                    retryable=False,
                )
            ]
        ),
    )

    assert CheckpointPhase.PLAN_REPLAN_REQUIRED.value in observed_phases
    assert len(generator.calls) == 1
    assert outcome.state == "awaiting_user"
    assert outcome.plan.current_version == 2
    assert outcome.plan.approved_version == 1
    assert outcome.run.status is RunStatus.AWAITING_USER
    assert [item["phase"] for item in (await execution_fixture.checkpoints())[-2:]] == [
        CheckpointPhase.PLAN_REPLAN_REQUIRED.value,
        CheckpointPhase.PLAN_AWAITING_APPROVAL.value,
    ]


@pytest.mark.asyncio
async def test_failed_revision_generation_terminates_without_extra_version(
    execution_fixture,
) -> None:
    draft = _draft(("lint",), max_attempts=1)

    async def observe_checkpoint() -> None:
        return None

    generator = ObservingFailureGenerator(draft, observe_checkpoint)
    generator.error = PlanGenerationError("two invalid structured responses")
    execution_fixture.service._generator = generator
    coordinator = PlanExecutionCoordinator(
        execution_fixture.database,
        settings=execution_fixture.settings,
        planning_service=execution_fixture.service,
    )
    await execution_fixture.materialize(draft)
    await execution_fixture.approve()
    outcome = await coordinator.execute_to_boundary(
        context=execution_fixture._context(),
        run_lease_handle=RunLeaseHandle(execution_fixture.lease),
        runner=ScriptedStepRunner(
            [PlanStepCompletion(status="failed", summary="no repair", evidence=[])]
        ),
    )

    snapshot = await coordinator._load_active_plan(execution_fixture._context())
    assert outcome.state == "failed_terminal"
    assert outcome.run.status is RunStatus.FAILED_TERMINAL
    assert snapshot.current_version == 1
    assert len(snapshot.versions) == 1
    assert (await execution_fixture.latest_checkpoint())["phase"] == CheckpointPhase.RUN_TERMINAL.value


@pytest.mark.asyncio
async def test_revision_quota_fails_closed_before_generator_call(execution_fixture) -> None:
    draft = _draft(("lint",), max_attempts=1)

    async def observe_checkpoint() -> None:
        return None

    generator = ObservingFailureGenerator(draft, observe_checkpoint)
    execution_fixture.service._generator = generator
    execution_fixture.settings.planning.max_revisions = 0
    coordinator = PlanExecutionCoordinator(
        execution_fixture.database,
        settings=execution_fixture.settings,
        planning_service=execution_fixture.service,
    )
    await execution_fixture.materialize(draft)
    await execution_fixture.approve()
    outcome = await coordinator.execute_to_boundary(
        context=execution_fixture._context(),
        run_lease_handle=RunLeaseHandle(execution_fixture.lease),
        runner=ScriptedStepRunner(
            [PlanStepCompletion(status="failed", summary="quota", evidence=[])]
        ),
    )

    assert outcome.state == "failed_terminal"
    assert generator.calls == []
    assert outcome.plan.current_version == 1


@pytest.mark.asyncio
async def test_omitted_failed_work_raises_without_appending_or_terminalizing(
    execution_fixture,
) -> None:
    draft = _draft(("lint",), max_attempts=1)

    async def observe_checkpoint() -> None:
        return None

    generator = ObservingFailureGenerator(
        _draft(("replacement",), max_attempts=1), observe_checkpoint
    )
    execution_fixture.service._generator = generator
    coordinator = PlanExecutionCoordinator(
        execution_fixture.database,
        settings=execution_fixture.settings,
        planning_service=execution_fixture.service,
    )
    await execution_fixture.materialize(draft)
    await execution_fixture.approve()
    with pytest.raises(PlanGenerationError, match="failed step must be retained"):
        await coordinator.execute_to_boundary(
            context=execution_fixture._context(),
            run_lease_handle=RunLeaseHandle(execution_fixture.lease),
            runner=ScriptedStepRunner(
                [PlanStepCompletion(status="failed", summary="omitted", evidence=[])]
            ),
        )

    snapshot = await coordinator._load_active_plan(execution_fixture._context())
    run = await execution_fixture.service.workflow.get_run(execution_fixture._context())
    assert snapshot.current_version == 1
    assert run is not None and run.status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_apply_compatible_reuse_creates_explicit_reused_attempt(
    execution_fixture,
) -> None:
    draft = _draft(("collect",), max_attempts=2)
    await execution_fixture.approve_chain(["collect"])
    runner = ScriptedStepRunner([])
    tool_digest = hashlib.sha256(b"[]").hexdigest()
    policy_digest = hashlib.sha256(
        json.dumps(
            redact(execution_fixture.settings.governance.model_dump(mode="json")),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    skill_digest = hashlib.sha256(b"[]").hexdigest()
    source_started = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(), lease=execution_fixture.lease
    )
    assert source_started is not None
    source_document = await execution_fixture.succeed(
        source_started.step_run,
        digest=tool_digest,
        policy_digest=policy_digest,
        skill_set_digest=skill_digest,
    )
    assert source_document.tool_catalog_digest == hashlib.sha256(
        json.dumps(runner.registry.to_openai_schemas(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert source_document.policy_digest == hashlib.sha256(
        json.dumps(redact(execution_fixture.settings.governance.model_dump(mode="json")), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert source_document.skill_set_digest == hashlib.sha256(
        json.dumps(sorted(skill.name for skill in runner.skill_manager.active_skills), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    context = execution_fixture._context()
    async with TenantUnitOfWork(execution_fixture.database, context) as uow:
        original = await uow.plans.get(execution_fixture.plan_id)
        assert original is not None
        revised = await uow.plans.append_version(
            plan_id=original.plan_id,
            expected_version=original.aggregate_version,
            draft=draft,
            parent_version=original.current_version,
            revision_feedback="reuse compatible result",
            supersedes={"collect": original.current.steps[0].step_id},
        )
        await uow.conn.execute(
            update(agent_plans)
            .where(agent_plans.c.id == revised.plan_id)
            .values(status="approved", approved_version=revised.current_version)
        )
        await uow.conn.execute(
            update(agent_runs)
            .where(agent_runs.c.run_id == context.run_id)
            .values(active_plan_version=revised.current_version)
        )
    reused = await execution_fixture.coordinator.apply_compatible_reuse(
        context=context,
        lease=execution_fixture.lease,
        runner=runner,
    )
    repeated = await execution_fixture.coordinator.apply_compatible_reuse(
        context=context,
        lease=execution_fixture.lease,
        runner=runner,
    )

    assert len(reused) == 1
    assert repeated == ()
    async with TenantUnitOfWork(execution_fixture.database, context) as uow:
        current = await uow.plans.get(execution_fixture.plan_id)
        assert current is not None
        target_attempts = await uow.plans.step_attempts(
            plan_id=current.plan_id,
            plan_version=current.current_version,
            run_id=str(context.run_id),
            step_id=current.current.steps[0].step_id,
        )
        source_attempts = await uow.plans.step_attempts(
            plan_id=current.plan_id,
            plan_version=1,
            run_id=str(context.run_id),
            step_id=current.versions[0].steps[0].step_id,
        )
    assert len(target_attempts) == len(source_attempts) == 1
    assert target_attempts[0] == reused[0]
    assert target_attempts[0].reused_from_step_run_id == source_attempts[0].step_run_id
    assert (target_attempts[0].result_ref, target_attempts[0].result_digest, target_attempts[0].result_summary) == (source_attempts[0].result_ref, source_attempts[0].result_digest, source_attempts[0].result_summary)


@pytest.mark.asyncio
async def test_apply_compatible_reuse_proves_a_dependency_chain(
    execution_fixture,
) -> None:
    """Both immutable predecessors are copied in topology order, never rerun."""
    draft = _draft(("collect", "publish"), chain=True)
    await execution_fixture.approve_chain(["collect", "publish"])
    runner = ScriptedStepRunner([])
    tool_digest = hashlib.sha256(b"[]").hexdigest()
    policy_digest = hashlib.sha256(
        json.dumps(
            redact(execution_fixture.settings.governance.model_dump(mode="json")),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    skill_digest = hashlib.sha256(b"[]").hexdigest()
    source_root = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(), lease=execution_fixture.lease
    )
    assert source_root is not None
    root_document = await execution_fixture.succeed(
        source_root.step_run,
        digest=tool_digest,
        policy_digest=policy_digest,
        skill_set_digest=skill_digest,
    )
    source_dependent = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture._context(), lease=execution_fixture.lease
    )
    assert source_dependent is not None
    dependent_document = await execution_fixture.succeed(
        source_dependent.step_run,
        digest=tool_digest,
        dependency_result_digests={"collect": root_document.digest()},
        policy_digest=policy_digest,
        skill_set_digest=skill_digest,
    )
    context = execution_fixture._context()
    async with TenantUnitOfWork(execution_fixture.database, context) as uow:
        original = await uow.plans.get(execution_fixture.plan_id)
        assert original is not None
        revised = await uow.plans.append_version(
            plan_id=original.plan_id,
            expected_version=original.aggregate_version,
            draft=draft,
            parent_version=original.current_version,
            revision_feedback="reuse full chain",
            supersedes={step.logical_step_key: step.step_id for step in original.current.steps},
        )
        await uow.conn.execute(
            update(agent_plans).where(agent_plans.c.id == revised.plan_id).values(
                status="approved", approved_version=revised.current_version
            )
        )
        await uow.conn.execute(
            update(agent_runs).where(agent_runs.c.run_id == context.run_id).values(
                active_plan_version=revised.current_version
            )
        )

    reused = await execution_fixture.coordinator.apply_compatible_reuse(
        context=context, lease=execution_fixture.lease, runner=runner
    )

    assert [item.reused_from_step_run_id for item in reused] == [
        source_root.step_run.step_run_id,
        source_dependent.step_run.step_run_id,
    ]
    assert [item.result_digest for item in reused] == [
        root_document.digest(), dependent_document.digest()
    ]
    assert await execution_fixture.coordinator.select_next(context=context) is None


@pytest.mark.parametrize(
    ("change", "expected_key"),
    [
        ("definition", "collect"),
        ("dependency_definition", "publish"),
        ("dependency_result", "publish"),
        ("policy", "collect"),
        ("missing_proof", "collect"),
    ],
)
@pytest.mark.asyncio
async def test_incomplete_reuse_proof_forces_the_changed_or_unproven_step(
    execution_fixture, change, expected_key
) -> None:
    """Every missing compatibility proof leaves a normal, non-reused attempt."""
    await execution_fixture.approve_chain(["collect", "publish"])
    runner = ScriptedStepRunner([])
    digest = hashlib.sha256(b"[]").hexdigest()
    policy = hashlib.sha256(json.dumps(redact(execution_fixture.settings.governance.model_dump(mode="json")), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    root = await execution_fixture.coordinator.start_next_attempt(context=execution_fixture._context(), lease=execution_fixture.lease)
    assert root is not None
    root_doc = await execution_fixture.succeed(root.step_run, digest=digest, policy_digest=("f" * 64 if change == "policy" else policy), skill_set_digest=digest)
    dependent = await execution_fixture.coordinator.start_next_attempt(context=execution_fixture._context(), lease=execution_fixture.lease)
    assert dependent is not None
    await execution_fixture.succeed(
        dependent.step_run,
        digest=digest,
        dependency_result_digests={"collect": ("e" * 64 if change == "dependency_result" else root_doc.digest())},
        policy_digest=policy,
        skill_set_digest=digest,
    )
    context = execution_fixture._context()
    revision = _draft(("collect", "publish"), chain=True)
    if change == "definition":
        revision = _draft(("collect", "publish"), chain=True, max_attempts=1)
    if change == "dependency_definition":
        revision.steps[1] = revision.steps[1].model_copy(update={"max_attempts": 1})
    async with TenantUnitOfWork(execution_fixture.database, context) as uow:
        original = await uow.plans.get(execution_fixture.plan_id)
        assert original is not None
        revised = await uow.plans.append_version(plan_id=original.plan_id, expected_version=original.aggregate_version, draft=revision, parent_version=1, revision_feedback=change, supersedes={step.logical_step_key: step.step_id for step in original.current.steps})
        await uow.conn.execute(update(agent_plans).where(agent_plans.c.id == revised.plan_id).values(status="approved", approved_version=2))
        await uow.conn.execute(update(agent_runs).where(agent_runs.c.run_id == context.run_id).values(active_plan_version=2))
        if change == "missing_proof":
            await uow.conn.execute(update(agent_plan_step_runs).where(agent_plan_step_runs.c.step_run_id == root.step_run.step_run_id).values(result_ref=None))
    await execution_fixture.coordinator.apply_compatible_reuse(context=context, lease=execution_fixture.lease, runner=runner)
    started = await execution_fixture.coordinator.start_next_attempt(context=context, lease=execution_fixture.lease)
    assert started is not None
    assert started.step.logical_step_key == expected_key
    assert started.step_run.reused_from_step_run_id is None


@pytest.mark.asyncio
async def test_partial_reuse_keeps_unproven_dependent_for_real_execution(
    execution_fixture,
) -> None:
    """A valid root fact never grants its dependent a shortcut by implication."""
    await execution_fixture.approve_chain(["collect", "publish"])
    runner = ScriptedStepRunner([])
    digest = hashlib.sha256(b"[]").hexdigest()
    policy = hashlib.sha256(json.dumps(redact(execution_fixture.settings.governance.model_dump(mode="json")), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    source_root = await execution_fixture.coordinator.start_next_attempt(context=execution_fixture._context(), lease=execution_fixture.lease)
    assert source_root is not None
    root_document = await execution_fixture.succeed(source_root.step_run, digest=digest, policy_digest=policy, skill_set_digest=digest)
    source_dependent = await execution_fixture.coordinator.start_next_attempt(context=execution_fixture._context(), lease=execution_fixture.lease)
    assert source_dependent is not None
    dependent_document = await execution_fixture.succeed(source_dependent.step_run, digest=digest, dependency_result_digests={"collect": "e" * 64}, policy_digest=policy, skill_set_digest=digest)
    context = execution_fixture._context()
    async with TenantUnitOfWork(execution_fixture.database, context) as uow:
        original = await uow.plans.get(execution_fixture.plan_id)
        assert original is not None
        source_root_row = await uow.plans.step_run_by_id(run_id=str(context.run_id), step_run_id=source_root.step_run.step_run_id)
        source_dependent_row = await uow.plans.step_run_by_id(run_id=str(context.run_id), step_run_id=source_dependent.step_run.step_run_id)
        assert source_root_row is not None and source_dependent_row is not None
        revised = await uow.plans.append_version(plan_id=original.plan_id, expected_version=original.aggregate_version, draft=_draft(("collect", "publish"), chain=True), parent_version=1, revision_feedback="dependent proof changed", supersedes={step.logical_step_key: step.step_id for step in original.current.steps})
        await uow.conn.execute(update(agent_plans).where(agent_plans.c.id == revised.plan_id).values(status="approved", approved_version=2))
        await uow.conn.execute(update(agent_runs).where(agent_runs.c.run_id == context.run_id).values(active_plan_version=2))
    reused = await execution_fixture.coordinator.apply_compatible_reuse(context=context, lease=execution_fixture.lease, runner=runner)
    started = await execution_fixture.coordinator.start_next_attempt(context=context, lease=execution_fixture.lease)
    assert [item.reused_from_step_run_id for item in reused] == [source_root.step_run.step_run_id]
    assert reused[0].result_ref == source_root_row.result_ref
    assert reused[0].result_digest == source_root_row.result_digest == root_document.digest()
    assert reused[0].result_summary == source_root_row.result_summary
    assert source_dependent_row.result_digest == dependent_document.digest()
    assert started is not None and started.step.logical_step_key == "publish"
    assert started.step_run.reused_from_step_run_id is None


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


@pytest.mark.asyncio
async def test_mysql_memory_result_lookup_compiles_with_scoped_row_lock(
    execution_fixture,
):
    await execution_fixture.approve()
    context = execution_fixture._context()
    async with TenantUnitOfWork(execution_fixture.database, context) as uow:
        entry = await uow.memory.save(
            MemoryEntry(
                content="{}",
                type="plan_step_result",
                role="assistant",
                session_id=context.session_id,
            )
        )
        recording = StatementRecordingConnection(uow.conn)
        repository = MemoryRepository(  # type: ignore[arg-type]
            recording,
            context,
            MySQLDialect(),
        )
        assert (
            await repository.get(
                entry.id,
                context.session_id,
                for_update=True,
            )
            is not None
        )

    statement = next(
        statement
        for statement in recording.statements
        if getattr(statement, "is_select", False)
        and memory_entries in statement.get_final_froms()
    )
    sql = str(statement.compile(dialect=mysql.dialect()))

    assert statement.get_final_froms() == [memory_entries]
    assert sql.endswith(" FOR UPDATE")
    for column in ("tenant_id", "workspace_id", "session_id", "id"):
        assert f"memory_entries.{column}" in sql


@pytest.mark.asyncio
async def test_mysql_plan_attempt_statements_compile_with_lock_fence_and_scope(
    execution_fixture,
):
    await execution_fixture.approve()
    context = execution_fixture._context()
    async with TenantUnitOfWork(
        execution_fixture.database,
        context,
        planning_settings=execution_fixture.settings.planning,
    ) as uow:
        recording = StatementRecordingConnection(
            uow.conn,
            mysql_run_id=str(context.run_id),
        )
        repository = PlanRepository(  # type: ignore[arg-type]
            recording,
            MySQLDialect(),
            context,
            execution_fixture.settings.planning,
        )
        snapshot = await repository.get(execution_fixture.plan_id)
        assert snapshot is not None
        await repository.create_step_attempt(
            execution_fixture.lease,
            plan_id=snapshot.plan_id,
            plan_version=snapshot.current_version,
            step_id=snapshot.current.steps[0].step_id,
        )

    lock_statement = next(
        statement
        for statement in recording.statements
        if getattr(statement, "is_select", False)
        and getattr(statement, "_for_update_arg", None) is not None
        and agent_runs in statement.get_final_froms()
    )
    fence_statement = next(
        statement
        for statement in recording.statements
        if getattr(statement, "is_select", False)
        and "lease_owner" in str(statement)
        and "lease_expires_at" in str(statement)
    )
    insert_statement = next(
        statement
        for statement in recording.statements
        if getattr(statement, "is_insert", False)
        and statement.table is agent_plan_step_runs
    )

    mysql_dialect = mysql.dialect()
    lock_sql = str(lock_statement.compile(dialect=mysql_dialect))
    fence_sql = str(fence_statement.compile(dialect=mysql_dialect))
    compiled_insert = insert_statement.compile(dialect=mysql_dialect)
    insert_sql = str(compiled_insert)

    assert lock_statement.get_final_froms() == [agent_runs]
    assert lock_sql.endswith(" FOR UPDATE")
    normalized_fence_sql = fence_sql.lower()
    assert "unix_timestamp" in normalized_fence_sql
    assert "current_timestamp" in normalized_fence_sql
    assert "julianday" not in normalized_fence_sql
    for column in ("tenant_id", "workspace_id", "session_id", "run_id"):
        assert f"agent_runs.{column}" in lock_sql
        assert f"agent_runs.{column}" in fence_sql
    for column in ("lease_owner", "fencing_token", "version", "lease_expires_at"):
        assert f"agent_runs.{column}" in fence_sql
    assert insert_statement.table is agent_plan_step_runs
    for column in (
        "tenant_id",
        "workspace_id",
        "session_id",
        "plan_id",
        "plan_version",
        "step_id",
        "step_run_id",
        "run_id",
        "attempt",
        "status",
    ):
        assert column in insert_sql
        assert column in compiled_insert.params
    assert compiled_insert.params["status"] == PlanStepRunStatus.RUNNING.value
    assert compiled_insert.params["attempt"] == 1
    assert compiled_insert.params["tenant_id"] == context.tenant_id
    assert compiled_insert.params["workspace_id"] == context.workspace_id
    assert compiled_insert.params["session_id"] == context.session_id
    assert compiled_insert.params["run_id"] == context.run_id
    assert compiled_insert.params["plan_id"] == snapshot.plan_id
    assert compiled_insert.params["plan_version"] == snapshot.current_version


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
            except (StaleFenceError, PlanStepAlreadyRunningError):
                return None

        assert len([item for item in await asyncio.gather(start(), start()) if item]) == 1
    finally:
        await database.dispose()
