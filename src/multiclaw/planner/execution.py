from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from multiclaw.config import Settings
from multiclaw.planner.models import (
    PlanExecutionBlocked,
    PlanSnapshot,
    PlanStatus,
    PlanStepAlreadyRunningError,
    PlanStepRecord,
    PlanStepResultDocument,
    PlanStepRunRecord,
    PlanStepRunStatus,
    PlanVersionRecord,
)
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.repositories.plans import PlanRepository
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import CheckpointPhase, RunLease, StaleFenceError


_RESULT_REF = re.compile(r"memory:([A-Za-z0-9-]{1,64})")
_TERMINAL_STEP_STATUSES = {
    PlanStepRunStatus.FAILED_TERMINAL,
    PlanStepRunStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class ReadyPlanStep:
    plan: PlanSnapshot
    step: PlanStepRecord
    prior_attempts: tuple[PlanStepRunRecord, ...]
    dependency_results: tuple[PlanStepResultDocument, ...]


@dataclass(frozen=True, slots=True)
class StartedPlanStep:
    plan: PlanSnapshot
    step: PlanStepRecord
    step_run: PlanStepRunRecord
    dependency_results: tuple[PlanStepResultDocument, ...]


def choose_ready_step(
    plan: PlanVersionRecord,
    latest: Mapping[str, PlanStepRunRecord],
) -> PlanStepRecord | None:
    succeeded = {
        step_id
        for step_id, attempt in latest.items()
        if attempt.status is PlanStepRunStatus.SUCCEEDED
    }
    for step in sorted(plan.steps, key=lambda item: (item.ordinal, item.step_id)):
        current = latest.get(step.step_id)
        if current is not None and current.status in {
            PlanStepRunStatus.RUNNING,
            PlanStepRunStatus.SUCCEEDED,
            PlanStepRunStatus.FAILED_TERMINAL,
            PlanStepRunStatus.CANCELLED,
        }:
            continue
        if set(plan.dependencies.get(step.step_id, ())) <= succeeded:
            return step
    return None


class PlanExecutionCoordinator:
    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._database = database
        self._settings = settings or Settings(_config_file="/nonexistent")

    async def select_next(
        self,
        *,
        context: TenantContext,
        lease: RunLease | None = None,
    ) -> ReadyPlanStep | None:
        self._require_run_context(context)
        if lease is not None and lease.context != context:
            raise StaleFenceError("run lease scope is stale")
        async with self._database.connect() as conn:
            plans = PlanRepository(
                conn,
                self._database.dialect,
                context,
                self._settings.planning,
            )
            if lease is not None and not await plans.has_current_lease(lease):
                raise StaleFenceError("run lease is stale")
            workflow = WorkflowRepository(
                conn,
                self._database.dialect,
                self._settings.workflow.heartbeat_ms,
                self._settings.workflow.lease_ttl_ms,
            )
            memory = MemoryRepository(conn, context, self._database.dialect)
            return await self._select_next(
                context=context,
                plans=plans,
                workflow=workflow,
                memory=memory,
            )

    async def start_next_attempt(
        self,
        *,
        context: TenantContext,
        lease: RunLease,
    ) -> StartedPlanStep | None:
        self._require_run_context(context)
        if lease.context != context:
            raise StaleFenceError("run lease scope is stale")

        async with TenantUnitOfWork(
            self._database,
            context,
            planning_settings=self._settings.planning,
            workflow_settings=self._settings.workflow,
        ) as uow:
            if not await uow.plans.has_current_lease(lease):
                raise StaleFenceError("run lease is stale")
            ready = await self._select_next(
                context=context,
                plans=uow.plans,
                workflow=uow.workflow,
                memory=uow.memory,
            )
            if ready is None:
                return None

            step_run = await uow.plans.create_step_attempt(
                lease,
                plan_id=ready.plan.plan_id,
                plan_version=ready.plan.current_version,
                step_id=ready.step.step_id,
            )
            assert uow.conn is not None
            workflow = WorkflowCoordinator(
                self._database,
                settings=self._settings,
                connection=uow.conn,
            )
            await workflow.checkpoint(
                lease,
                CheckpointPhase.PLAN_STEP_READY,
                {
                    "run_id": context.run_id,
                    "plan_id": ready.plan.plan_id,
                    "plan_version": ready.plan.current_version,
                    "plan_digest": ready.plan.current.content_digest,
                    "step_id": ready.step.step_id,
                    "step_run_id": step_run.step_run_id,
                    "attempt": step_run.attempt,
                    "execution_cursor": "dispatch_step",
                    "cursor": "dispatch_step",
                },
            )

        return StartedPlanStep(
            plan=ready.plan,
            step=ready.step,
            step_run=step_run,
            dependency_results=ready.dependency_results,
        )

    async def _select_next(
        self,
        *,
        context: TenantContext,
        plans: PlanRepository,
        workflow: WorkflowRepository,
        memory: MemoryRepository,
    ) -> ReadyPlanStep | None:
        run = await workflow.get_run(context)
        if run is None or run.plan_id is None:
            raise PlanExecutionBlocked(
                "Plan execution requires an approved current active version"
            )
        plan = await plans.get(run.plan_id)
        if (
            plan is None
            or plan.status is not PlanStatus.APPROVED
            or plan.approved_version is None
            or plan.current_version != plan.approved_version
            or run.active_plan_version != plan.current_version
        ):
            raise PlanExecutionBlocked(
                "Plan execution requires an approved current active version"
            )

        latest = await plans.latest_step_attempts(
            plan_id=plan.plan_id,
            plan_version=plan.current_version,
            run_id=str(context.run_id),
        )
        if any(attempt.status is PlanStepRunStatus.RUNNING for attempt in latest.values()):
            raise PlanStepAlreadyRunningError(
                "Plan run already has a running step attempt"
            )
        if any(attempt.status in _TERMINAL_STEP_STATUSES for attempt in latest.values()):
            raise PlanExecutionBlocked(
                "Plan execution is blocked by a failed dependency or terminal step"
            )

        step = choose_ready_step(plan.current, latest)
        if step is None:
            if all(
                (attempt := latest.get(item.step_id)) is not None
                and attempt.status is PlanStepRunStatus.SUCCEEDED
                for item in plan.current.steps
            ):
                return None
            raise PlanExecutionBlocked("Plan execution is blocked by a failed dependency")

        dependency_results = await self._load_dependency_results(
            context=context,
            plan=plan,
            step=step,
            latest=latest,
            memory=memory,
        )
        prior_attempts = await plans.step_attempts(
            plan_id=plan.plan_id,
            plan_version=plan.current_version,
            run_id=str(context.run_id),
            step_id=step.step_id,
        )
        return ReadyPlanStep(
            plan=plan,
            step=step,
            prior_attempts=prior_attempts,
            dependency_results=dependency_results,
        )

    @staticmethod
    async def _load_dependency_results(
        *,
        context: TenantContext,
        plan: PlanSnapshot,
        step: PlanStepRecord,
        latest: Mapping[str, PlanStepRunRecord],
        memory: MemoryRepository,
    ) -> tuple[PlanStepResultDocument, ...]:
        steps_by_id = {item.step_id: item for item in plan.current.steps}
        documents: list[PlanStepResultDocument] = []
        for dependency_id in plan.current.dependencies.get(step.step_id, ()):
            attempt = latest.get(dependency_id)
            if (
                attempt is None
                or attempt.status is not PlanStepRunStatus.SUCCEEDED
                or attempt.result_ref is None
                or attempt.result_digest is None
            ):
                raise PlanExecutionBlocked(
                    "Plan execution is blocked by a missing succeeded dependency result"
                )
            match = _RESULT_REF.fullmatch(attempt.result_ref)
            if match is None:
                raise PlanExecutionBlocked("Plan dependency result reference is invalid")
            entry = await memory.get(match.group(1), context.session_id)
            if (
                entry is None
                or entry.type != "plan_step_result"
                or entry.role != "assistant"
                or entry.metadata.get("schema_version") != 1
                or entry.metadata.get("plan_id") != plan.plan_id
                or entry.metadata.get("step_run_id") != attempt.step_run_id
            ):
                raise PlanExecutionBlocked("Plan dependency result document is unavailable")
            try:
                document = PlanStepResultDocument.model_validate_json(entry.content)
            except ValidationError as error:
                raise PlanExecutionBlocked(
                    "Plan dependency result document is invalid"
                ) from error
            dependency_step = steps_by_id[dependency_id]
            if (
                document.plan_id != plan.plan_id
                or document.plan_version != plan.current_version
                or document.run_id != context.run_id
                or document.step_id != dependency_id
                or document.step_run_id != attempt.step_run_id
                or document.attempt != attempt.attempt
                or document.status != "succeeded"
                or document.definition_digest != dependency_step.definition_digest
                or document.digest() != attempt.result_digest
                or document.summary != attempt.result_summary
            ):
                raise PlanExecutionBlocked("Plan dependency result document is inconsistent")
            documents.append(document)
        return tuple(documents)

    @staticmethod
    def _require_run_context(context: TenantContext) -> None:
        if context.session_id is None or context.run_id is None:
            raise ValueError("Plan execution requires session and run scope")
