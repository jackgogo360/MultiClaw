from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from multiclaw.config import Settings
from multiclaw.planner.models import (
    TERMINAL_PLAN_STEP_STATUSES,
    PlanExecutionBlocked,
    PlanSnapshot,
    PlanStatus,
    PlanStepAlreadyRunningError,
    PlanStepRecord,
    PlanStepResultDocument,
    PlanStepRunRecord,
    PlanStepRunStatus,
    PlanVersionRecord,
    is_plan_run_executable,
    is_plan_step_ready,
)
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.repositories.plans import PlanRepository
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    CheckpointPhase,
    RunLease,
    RunRecord,
    StaleFenceError,
)

_RESULT_REF = re.compile(r"memory:([A-Za-z0-9-]{1,64})")
MAX_PLAN_STEP_RESULT_BYTES = 262_144


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
    for step in sorted(plan.steps, key=lambda item: (item.ordinal, item.step_id)):
        if is_plan_step_ready(
            step.step_id,
            plan.dependencies.get(step.step_id, ()),
            latest,
        ):
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
            await uow.plans.lock_run_for_execution(lease)
            ready = await self._select_next(
                context=context,
                plans=uow.plans,
                workflow=uow.workflow,
                memory=uow.memory,
                for_update=True,
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
        for_update: bool = False,
    ) -> ReadyPlanStep | None:
        run = await workflow.get_run(context)
        if run is None:
            raise PlanExecutionBlocked(
                "Plan execution requires an approved current active version"
            )
        self._require_executable_run(run)
        if run.plan_id is None:
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
            for_update=for_update,
        )
        if any(attempt.status is PlanStepRunStatus.RUNNING for attempt in latest.values()):
            raise PlanStepAlreadyRunningError(
                "Plan run already has a running step attempt"
            )
        if any(
            attempt.status in TERMINAL_PLAN_STEP_STATUSES
            for attempt in latest.values()
        ):
            raise PlanExecutionBlocked(
                "Plan execution is blocked by a failed dependency or terminal step"
            )

        verified_results = await self._load_succeeded_results(
            context=context,
            plan=plan,
            latest=latest,
            memory=memory,
            for_update=for_update,
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

        dependency_results = tuple(
            verified_results[dependency_id]
            for dependency_id in plan.current.dependencies.get(step.step_id, ())
        )
        prior_attempts = await plans.step_attempts(
            plan_id=plan.plan_id,
            plan_version=plan.current_version,
            run_id=str(context.run_id),
            step_id=step.step_id,
            for_update=for_update,
        )
        return ReadyPlanStep(
            plan=plan,
            step=step,
            prior_attempts=prior_attempts,
            dependency_results=dependency_results,
        )

    @staticmethod
    async def _load_succeeded_results(
        *,
        context: TenantContext,
        plan: PlanSnapshot,
        latest: Mapping[str, PlanStepRunRecord],
        memory: MemoryRepository,
        for_update: bool,
    ) -> Mapping[str, PlanStepResultDocument]:
        steps_by_id = {item.step_id: item for item in plan.current.steps}
        documents: dict[str, PlanStepResultDocument] = {}
        unknown_succeeded = {
            step_id
            for step_id, attempt in latest.items()
            if attempt.status is PlanStepRunStatus.SUCCEEDED
            and step_id not in steps_by_id
        }
        if unknown_succeeded:
            raise PlanExecutionBlocked("Plan succeeded step result is inconsistent")

        for step in sorted(
            plan.current.steps,
            key=lambda item: (item.ordinal, item.step_id),
        ):
            attempt = latest.get(step.step_id)
            if attempt is None or attempt.status is not PlanStepRunStatus.SUCCEEDED:
                continue
            if (
                attempt.result_ref is None
                or attempt.result_digest is None
                or attempt.result_summary is None
            ):
                raise PlanExecutionBlocked(
                    "Plan succeeded step result is incomplete"
                )
            match = _RESULT_REF.fullmatch(attempt.result_ref)
            if match is None:
                raise PlanExecutionBlocked("Plan succeeded step result reference is invalid")
            entry = await memory.get(
                match.group(1),
                context.session_id,
                for_update=for_update,
            )
            if (
                entry is None
                or entry.type != "plan_step_result"
                or entry.role != "assistant"
                or entry.metadata.get("schema_version") != 1
                or entry.metadata.get("plan_id") != plan.plan_id
                or entry.metadata.get("step_run_id") != attempt.step_run_id
            ):
                raise PlanExecutionBlocked("Plan result document is unavailable")
            try:
                content_bytes = entry.content.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PlanExecutionBlocked("Plan result document is invalid") from error
            if len(content_bytes) > MAX_PLAN_STEP_RESULT_BYTES:
                raise PlanExecutionBlocked(
                    f"Plan result document exceeds {MAX_PLAN_STEP_RESULT_BYTES} bytes"
                )
            try:
                document = PlanStepResultDocument.model_validate_json(content_bytes)
            except ValidationError as error:
                raise PlanExecutionBlocked(
                    "Plan result document is invalid"
                ) from error
            if (
                document.plan_id != plan.plan_id
                or document.plan_version != plan.current_version
                or document.run_id != context.run_id
                or document.step_id != step.step_id
                or document.step_run_id != attempt.step_run_id
                or document.attempt != attempt.attempt
                or document.status != "succeeded"
                or document.definition_digest != step.definition_digest
                or document.digest() != attempt.result_digest
                or document.summary != attempt.result_summary
            ):
                raise PlanExecutionBlocked("Plan result document is inconsistent")

            dependency_ids = plan.current.dependencies.get(step.step_id, ())
            if any(dependency_id not in documents for dependency_id in dependency_ids):
                raise PlanExecutionBlocked(
                    "Plan dependency result proof is inconsistent"
                )
            expected_dependency_digests: dict[str, str] = {}
            for dependency_id in dependency_ids:
                dependency_digest = latest[dependency_id].result_digest
                if dependency_digest is None:
                    raise PlanExecutionBlocked(
                        "Plan dependency result proof is inconsistent"
                    )
                dependency_key = steps_by_id[dependency_id].logical_step_key
                expected_dependency_digests[dependency_key] = dependency_digest
            if document.dependency_result_digests != expected_dependency_digests:
                raise PlanExecutionBlocked(
                    "Plan dependency result proof is inconsistent"
                )
            documents[step.step_id] = document
        return documents

    @staticmethod
    def _require_run_context(context: TenantContext) -> None:
        if context.session_id is None or context.run_id is None:
            raise ValueError("Plan execution requires session and run scope")

    @staticmethod
    def _require_executable_run(run: RunRecord) -> None:
        if not is_plan_run_executable(run.status, run.cancel_requested_at):
            raise PlanExecutionBlocked("Plan execution run is not executable")
