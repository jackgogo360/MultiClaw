from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from multiclaw.config import Settings
from multiclaw.planner.models import (
    TERMINAL_PLAN_STEP_STATUSES,
    PlanExecutionBlocked,
    PlanExecutionOutcome,
    PlanSnapshot,
    PlanStatus,
    PlanStepAlreadyRunningError,
    PlanStepCompletion,
    PlanStepExecutionRequest,
    PlanStepRecord,
    PlanStepResultDocument,
    PlanStepRunner,
    PlanStepRunRecord,
    PlanStepRunStatus,
    PlanVersionRecord,
    is_plan_run_executable,
    is_plan_step_ready,
)
from multiclaw.security.redaction import redact
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.repositories.plans import PlanRepository
from multiclaw.storage.repositories.workflow import WorkflowRepository
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext
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
    RunRecord,
    RunStatus,
    StaleFenceError,
)

_RESULT_REF = re.compile(r"memory:([A-Za-z0-9-]{1,64})")
MAX_PLAN_STEP_RESULT_BYTES = 262_144


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        redact(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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

    async def execute_to_boundary(
        self,
        *,
        context: TenantContext,
        run_lease_handle: RunLeaseHandle,
        runner: PlanStepRunner,
        recovered_tool_result: PersistedToolResult | None = None,
        recovered_tool_input_json: str | None = None,
    ) -> PlanExecutionOutcome:
        if (recovered_tool_result is None) != (recovered_tool_input_json is None):
            raise ValueError("recovered tool result and input must be provided together")
        resume_running_attempt = recovered_tool_result is not None
        last_plan: PlanSnapshot | None = None
        while True:
            lease = await run_lease_handle.current()
            run = await self._load_run(context)
            if run.status is RunStatus.RESUMING:
                lease = await WorkflowCoordinator(
                    self._database,
                    settings=self._settings,
                ).transition_run(lease, RunStatus.RUNNING)
                await run_lease_handle.replace(lease)
            started = (
                await self._resume_running_attempt(context=context, lease=lease)
                if resume_running_attempt
                else await self.start_next_attempt(context=context, lease=lease)
            )
            if started is None:
                if last_plan is None:
                    last_plan = await self._load_active_plan(context)
                run = await self._load_run(context)
                return PlanExecutionOutcome(
                    state="completed",
                    plan=last_plan,
                    run=run,
                )
            last_plan = started.plan
            request = PlanStepExecutionRequest(
                context=context,
                lease=lease,
                plan=started.plan,
                step=started.step,
                step_run=started.step_run,
                dependency_results=started.dependency_results,
            )
            completion = await runner.run_plan_step(
                request,
                run_lease_handle=run_lease_handle,
                workflow_continuation=WorkflowContinuationService(
                    self._database,
                    settings=self._settings,
                ),
                recovered_tool_result=recovered_tool_result,
                recovered_tool_input_json=recovered_tool_input_json,
            )
            recovered_tool_result = None
            recovered_tool_input_json = None
            resume_running_attempt = False
            if isinstance(completion, ContinuationOutcome):
                if completion.state is not ContinuationState.AWAITING_USER:
                    raise PlanExecutionBlocked(
                        "Plan step runner returned an invalid continuation boundary"
                    )
                return PlanExecutionOutcome(
                    state="awaiting_user",
                    plan=started.plan,
                    run=await self._load_run(context),
                    assistant_content=completion.assistant_content,
                )
            if not isinstance(completion, PlanStepCompletion):
                raise PlanExecutionBlocked(
                    "Plan step runner returned an invalid completion"
                )

            if completion.status == "succeeded":
                target_status = PlanStepRunStatus.SUCCEEDED
            elif completion.retryable and started.step_run.attempt < min(
                started.step.max_attempts,
                self._settings.planning.max_step_attempts,
            ):
                target_status = PlanStepRunStatus.FAILED_RETRYABLE
            else:
                target_status = PlanStepRunStatus.FAILED_TERMINAL
            document = self._build_result_document(
                request=request,
                completion=completion,
                runner=runner,
            )

            async with TenantUnitOfWork(
                self._database,
                context,
                planning_settings=self._settings.planning,
                workflow_settings=self._settings.workflow,
            ) as uow:
                await uow.plans.lock_run_for_execution(lease)
                assert uow.conn is not None
                continuation = WorkflowContinuationService(
                    self._database,
                    settings=self._settings,
                    connection=uow.conn,
                )
                persisted = await continuation.persist_plan_step_result(
                    context=context,
                    document=document,
                )
                finished = await uow.plans.finish_step_attempt(
                    lease,
                    step_run_id=started.step_run.step_run_id,
                    expected_version=started.step_run.version,
                    status=target_status,
                    result=document,
                    result_ref=persisted.result_ref,
                    error_code=(
                        None
                        if target_status is PlanStepRunStatus.SUCCEEDED
                        else str(redact("plan_step_failed"))
                    ),
                    error_detail_redacted=(
                        None
                        if target_status is PlanStepRunStatus.SUCCEEDED
                        else str(redact(document.summary))
                    ),
                )
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
                        "plan_id": started.plan.plan_id,
                        "plan_version": started.plan.current_version,
                        "plan_digest": started.plan.current.content_digest,
                        "step_id": started.step.step_id,
                        "step_run_id": finished.step_run_id,
                        "attempt": finished.attempt,
                        "execution_cursor": "select_next",
                        "cursor": "select_next",
                    },
                )

            if target_status is PlanStepRunStatus.FAILED_TERMINAL:
                return PlanExecutionOutcome(
                    state="replan_required",
                    plan=started.plan,
                    run=await self._load_run(context),
                )

    async def _resume_running_attempt(
        self,
        *,
        context: TenantContext,
        lease: RunLease,
    ) -> StartedPlanStep:
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
            plan = await self._load_executable_plan(
                context=context,
                plans=uow.plans,
                workflow=uow.workflow,
            )
            running = await uow.plans.running_step_attempts(
                run_id=str(context.run_id),
                for_update=True,
            )
            if len(running) != 1:
                raise PlanExecutionBlocked(
                    "Plan recovery requires exactly one running step attempt"
                )
            attempt = running[0]
            steps_by_id = {step.step_id: step for step in plan.current.steps}
            step = steps_by_id.get(attempt.step_id)
            if (
                attempt.plan_id != plan.plan_id
                or attempt.plan_version != plan.current_version
                or attempt.run_id != context.run_id
                or step is None
            ):
                raise PlanExecutionBlocked(
                    "Plan recovery running step attempt is inconsistent"
                )
            latest = await uow.plans.latest_step_attempts(
                plan_id=plan.plan_id,
                plan_version=plan.current_version,
                run_id=str(context.run_id),
                for_update=True,
            )
            current = latest.get(attempt.step_id)
            if (
                current is None
                or current.step_run_id != attempt.step_run_id
                or current.status is not PlanStepRunStatus.RUNNING
                or any(
                    item.status in TERMINAL_PLAN_STEP_STATUSES
                    for item in latest.values()
                )
            ):
                raise PlanExecutionBlocked(
                    "Plan recovery running step attempt is inconsistent"
                )
            verified_results = await self._load_succeeded_results(
                context=context,
                plan=plan,
                latest=latest,
                memory=uow.memory,
                for_update=True,
            )
            dependency_ids = plan.current.dependencies.get(attempt.step_id, ())
            if any(
                dependency_id not in verified_results
                for dependency_id in dependency_ids
            ):
                raise PlanExecutionBlocked(
                    "Plan recovery dependency result proof is inconsistent"
                )
            return StartedPlanStep(
                plan=plan,
                step=step,
                step_run=attempt,
                dependency_results=tuple(
                    verified_results[dependency_id]
                    for dependency_id in dependency_ids
                ),
            )

    def _build_result_document(
        self,
        *,
        request: PlanStepExecutionRequest,
        completion: PlanStepCompletion,
        runner,
    ) -> PlanStepResultDocument:
        steps_by_id = {
            step.step_id: step for step in request.plan.current.steps
        }
        dependency_result_digests = {
            steps_by_id[result.step_id].logical_step_key: result.digest()
            for result in request.dependency_results
        }
        summary = str(redact(completion.summary))
        evidence = [str(redact(item)) for item in completion.evidence]
        tool_schemas = runner.registry.to_openai_schemas()
        active_skill_names = sorted(
            skill.name for skill in runner.skill_manager.active_skills
        )
        return PlanStepResultDocument(
            plan_id=request.plan.plan_id,
            plan_version=request.plan.current_version,
            run_id=str(request.context.run_id),
            step_id=request.step.step_id,
            step_run_id=request.step_run.step_run_id,
            attempt=request.step_run.attempt,
            status=completion.status,
            summary=summary,
            evidence=evidence,
            definition_digest=request.step.definition_digest,
            dependency_result_digests=dependency_result_digests,
            tool_catalog_digest=_canonical_digest(tool_schemas),
            policy_digest=_canonical_digest(
                self._settings.governance.model_dump(mode="json")
            ),
            skill_set_digest=_canonical_digest(active_skill_names),
        )

    async def _load_run(self, context: TenantContext) -> RunRecord:
        async with self._database.connect() as conn:
            repository = WorkflowRepository(
                conn,
                self._database.dialect,
                self._settings.workflow.heartbeat_ms,
                self._settings.workflow.lease_ttl_ms,
            )
            run = await repository.get_run(context)
        if run is None:
            raise PlanExecutionBlocked("Plan execution run is unavailable")
        return run

    async def _load_active_plan(self, context: TenantContext) -> PlanSnapshot:
        run = await self._load_run(context)
        if run.plan_id is None:
            raise PlanExecutionBlocked("Plan execution has no active Plan")
        async with self._database.connect() as conn:
            plan = await PlanRepository(
                conn,
                self._database.dialect,
                context,
                self._settings.planning,
            ).get(run.plan_id)
        if plan is None:
            raise PlanExecutionBlocked("Plan execution Plan is unavailable")
        return plan

    async def _select_next(
        self,
        *,
        context: TenantContext,
        plans: PlanRepository,
        workflow: WorkflowRepository,
        memory: MemoryRepository,
        for_update: bool = False,
    ) -> ReadyPlanStep | None:
        plan = await self._load_executable_plan(
            context=context,
            plans=plans,
            workflow=workflow,
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

    async def _load_executable_plan(
        self,
        *,
        context: TenantContext,
        plans: PlanRepository,
        workflow: WorkflowRepository,
    ) -> PlanSnapshot:
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
        return plan

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
