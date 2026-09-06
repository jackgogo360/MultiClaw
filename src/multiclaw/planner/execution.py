from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from multiclaw.config import Settings
from multiclaw.events import EventRouter, ScopedEvent
from multiclaw.planner.generator import PlanGenerationError
from multiclaw.planner.models import (
    TERMINAL_PLAN_STEP_STATUSES,
    CompletedStepContext,
    PlanCancellationRequested,
    PlanExecutionBlocked,
    PlanExecutionOutcome,
    PlanReference,
    PlanRevisionContext,
    PlanRevisionLimitError,
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
    revision_current_plan_context,
)
from multiclaw.planner.service import FailureRevisionRequest, PlanningService
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


@dataclass(frozen=True, slots=True)
class DependencyReuseProof:
    """The already-verified revised dependency and its immutable predecessor."""

    new_step: PlanStepRecord
    reused_run: PlanStepRunRecord
    source_step: PlanStepRecord
    source_run: PlanStepRunRecord
    source_result: PlanStepResultDocument


def can_reuse_result(
    *,
    current_run_id: str,
    new_step: PlanStepRecord,
    source_step: PlanStepRecord | None,
    source_run: PlanStepRunRecord | None,
    source_result: PlanStepResultDocument | None,
    new_dependency_ids: frozenset[str],
    dependency_proofs: Mapping[str, DependencyReuseProof],
    current_tool_catalog_digest: str | None,
    current_policy_digest: str | None,
    current_skill_set_digest: str | None,
) -> bool:
    """Return true only for a complete, same-run immutable reuse proof.

    Compatibility is deliberately all-or-nothing.  Missing current compatibility
    inputs are not a reason to guess that old tool or policy state is safe.
    """
    if (
        source_step is None
        or source_run is None
        or source_result is None
        or current_tool_catalog_digest is None
        or current_policy_digest is None
        or current_skill_set_digest is None
    ):
        return False
    if (
        source_run.run_id != current_run_id
        or source_run.step_id != source_step.step_id
        or source_run.status is not PlanStepRunStatus.SUCCEEDED
        or source_run.reused_from_step_run_id is not None
        or new_step.supersedes_step_id != source_step.step_id
        or new_step.definition_digest != source_step.definition_digest
    ):
        return False
    if (
        source_result.plan_id != source_run.plan_id
        or source_result.plan_version != source_run.plan_version
        or source_result.run_id != current_run_id
        or source_result.step_id != source_step.step_id
        or source_result.step_run_id != source_run.step_run_id
        or source_result.attempt != source_run.attempt
        or source_result.status != "succeeded"
        or source_result.definition_digest != source_step.definition_digest
        or source_run.result_digest != source_result.digest()
        or source_run.result_summary != source_result.summary
        or source_run.result_ref is None
    ):
        return False
    if (
        source_result.tool_catalog_digest != current_tool_catalog_digest
        or source_result.policy_digest != current_policy_digest
        or source_result.skill_set_digest != current_skill_set_digest
    ):
        return False
    if set(source_result.dependency_result_digests) != set(dependency_proofs):
        return False
    if {proof.new_step.step_id for proof in dependency_proofs.values()} != new_dependency_ids:
        return False
    for predecessor_key, proof in dependency_proofs.items():
        expected = source_result.dependency_result_digests.get(predecessor_key)
        if expected is None:
            return False
        if (
            proof.new_step.supersedes_step_id != proof.source_step.step_id
            or proof.new_step.definition_digest != proof.source_step.definition_digest
            or proof.source_run.run_id != current_run_id
            or proof.source_run.step_id != proof.source_step.step_id
            or proof.source_run.status is not PlanStepRunStatus.SUCCEEDED
            or proof.source_run.reused_from_step_run_id is not None
            or proof.source_result.plan_id != proof.source_run.plan_id
            or proof.source_result.plan_version != proof.source_run.plan_version
            or proof.source_result.run_id != current_run_id
            or proof.source_result.step_id != proof.source_step.step_id
            or proof.source_result.step_run_id != proof.source_run.step_run_id
            or proof.source_result.attempt != proof.source_run.attempt
            or proof.source_result.status != "succeeded"
            or proof.source_result.definition_digest != proof.source_step.definition_digest
            or proof.source_run.result_digest != expected
            or proof.source_run.result_summary != proof.source_result.summary
            or proof.source_run.result_ref is None
            or proof.source_result.digest() != expected
            or proof.reused_run.plan_id != proof.source_run.plan_id
            or proof.reused_run.run_id != current_run_id
            or proof.reused_run.step_id != proof.new_step.step_id
            or proof.reused_run.reused_from_step_run_id != proof.source_run.step_run_id
            or proof.reused_run.status is not PlanStepRunStatus.SUCCEEDED
            or proof.reused_run.result_ref != proof.source_run.result_ref
            or proof.reused_run.result_digest != expected
            or proof.reused_run.result_summary != proof.source_result.summary
        ):
            return False
    return True


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
        planning_service: PlanningService | None = None,
        event_router: EventRouter | None = None,
    ) -> None:
        self._database = database
        self._settings = settings or Settings(_config_file="/nonexistent")
        self._planning_service = planning_service
        self._event_router = event_router

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

    async def apply_compatible_reuse(
        self,
        *,
        context: TenantContext,
        lease: RunLease,
        runner: PlanStepRunner,
    ) -> tuple[PlanStepRunRecord, ...]:
        """Materialize only fully proven same-run results in stable DAG order."""
        tool_digest = _canonical_digest(runner.registry.to_openai_schemas())
        policy_digest = _canonical_digest(self._settings.governance.model_dump(mode="json"))
        skill_digest = _canonical_digest(sorted(skill.name for skill in runner.skill_manager.active_skills))
        created: list[PlanStepRunRecord] = []
        async with TenantUnitOfWork(
            self._database,
            context,
            planning_settings=self._settings.planning,
            workflow_settings=self._settings.workflow,
        ) as uow:
            await uow.plans.lock_run_for_execution(lease)
            plan = await self._load_executable_plan(
                context=context, plans=uow.plans, workflow=uow.workflow
            )
            all_steps = {
                step.step_id: (version, step)
                for version in plan.versions
                for step in version.steps
            }
            current_latest = dict(await uow.plans.latest_step_attempts(
                plan_id=plan.plan_id,
                plan_version=plan.current_version,
                run_id=str(context.run_id),
                for_update=True,
            ))
            for step in sorted(plan.current.steps, key=lambda item: (item.ordinal, item.step_id)):
                if step.step_id in current_latest or step.supersedes_step_id is None:
                    continue
                source_item = all_steps.get(step.supersedes_step_id)
                if source_item is None:
                    continue
                source_version, source_step = source_item
                source_latest = await uow.plans.latest_step_attempts(
                    plan_id=plan.plan_id,
                    plan_version=source_version.plan_version,
                    run_id=str(context.run_id),
                    for_update=True,
                )
                source_run = source_latest.get(source_step.step_id)
                source_result = await self._reuse_document(
                    uow.memory, context, source_run, source_step
                )
                if source_run is None or source_result is None:
                    continue
                proofs: dict[str, DependencyReuseProof] = {}
                valid_dependencies = True
                dependency_ids = frozenset(plan.current.dependencies.get(step.step_id, ()))
                for dependency_id in dependency_ids:
                    reused_run = current_latest.get(dependency_id)
                    if reused_run is None or reused_run.reused_from_step_run_id is None:
                        valid_dependencies = False
                        break
                    source_dependency = await uow.plans.step_run_by_id(
                        run_id=str(context.run_id),
                        step_run_id=reused_run.reused_from_step_run_id,
                        for_update=True,
                    )
                    if source_dependency is None:
                        valid_dependencies = False
                        break
                    source_dependency_item = all_steps.get(source_dependency.step_id)
                    if source_dependency_item is None:
                        valid_dependencies = False
                        break
                    _, source_dependency_step = source_dependency_item
                    source_dependency_document = await self._reuse_document(
                        uow.memory,
                        context,
                        source_dependency,
                        source_dependency_step,
                    )
                    if source_dependency_document is None:
                        valid_dependencies = False
                        break
                    new_dependency_step = next(
                        item for item in plan.current.steps if item.step_id == dependency_id
                    )
                    proofs[new_dependency_step.logical_step_key] = DependencyReuseProof(
                        new_step=next(
                            item for item in plan.current.steps if item.step_id == dependency_id
                        ),
                        reused_run=reused_run,
                        source_step=source_dependency_step,
                        source_run=source_dependency,
                        source_result=source_dependency_document,
                    )
                if not valid_dependencies or not can_reuse_result(
                    current_run_id=str(context.run_id),
                    new_step=step,
                    source_step=source_step,
                    source_run=source_run,
                    source_result=source_result,
                    new_dependency_ids=dependency_ids,
                    dependency_proofs=proofs,
                    current_tool_catalog_digest=tool_digest,
                    current_policy_digest=policy_digest,
                    current_skill_set_digest=skill_digest,
                ):
                    continue
                reused = await uow.plans.create_reused_step_attempt(
                    lease,
                    plan_id=plan.plan_id,
                    plan_version=plan.current_version,
                    step_id=step.step_id,
                    source_step_run_id=source_run.step_run_id,
                )
                current_latest[step.step_id] = reused
                created.append(reused)
        return tuple(created)

    @staticmethod
    async def _reuse_document(
        memory: MemoryRepository,
        context: TenantContext,
        attempt: PlanStepRunRecord | None,
        step: PlanStepRecord,
    ) -> PlanStepResultDocument | None:
        if (
            attempt is None
            or attempt.status is not PlanStepRunStatus.SUCCEEDED
            or attempt.result_ref is None
            or attempt.result_digest is None
            or attempt.result_summary is None
        ):
            return None
        match = _RESULT_REF.fullmatch(attempt.result_ref)
        if match is None:
            return None
        entry = await memory.get(match.group(1), context.session_id, for_update=True)
        if (
            entry is None
            or entry.type != "plan_step_result"
            or entry.role != "assistant"
            or entry.metadata.get("schema_version") != 1
            or entry.metadata.get("plan_id") != attempt.plan_id
            or entry.metadata.get("step_run_id") != attempt.step_run_id
        ):
            return None
        try:
            content_bytes = entry.content.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if len(content_bytes) > MAX_PLAN_STEP_RESULT_BYTES:
            return None
        try:
            document = PlanStepResultDocument.model_validate_json(content_bytes)
        except ValidationError:
            return None
        if (
            document.plan_id != attempt.plan_id
            or document.plan_version != attempt.plan_version
            or document.run_id != attempt.run_id
            or document.step_id != attempt.step_id
            or document.step_run_id != attempt.step_run_id
            or document.attempt != attempt.attempt
            or document.status != "succeeded"
            or document.definition_digest != step.definition_digest
            or document.digest() != attempt.result_digest
            or document.summary != attempt.result_summary
        ):
            return None
        return document

    async def execute_to_boundary(
        self,
        *,
        context: TenantContext,
        run_lease_handle: RunLeaseHandle,
        runner: PlanStepRunner,
        recovered_tool_result: PersistedToolResult | None = None,
        recovered_tool_input_json: str | None = None,
    ) -> PlanExecutionOutcome:
        try:
            return await self._execute_to_boundary(
                context=context,
                run_lease_handle=run_lease_handle,
                runner=runner,
                recovered_tool_result=recovered_tool_result,
                recovered_tool_input_json=recovered_tool_input_json,
            )
        except PlanCancellationRequested:
            return await self._cancel_at_boundary(context, run_lease_handle)

    async def _execute_to_boundary(
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
        started: StartedPlanStep | None
        while True:
            await self._raise_if_cancel_requested(context)
            lease = await run_lease_handle.current()
            run = await self._load_run(context)
            if run.status is RunStatus.RESUMING:
                lease = await WorkflowCoordinator(
                    self._database,
                    settings=self._settings,
                ).transition_run(lease, RunStatus.RUNNING)
                await run_lease_handle.replace(lease)
            if resume_running_attempt:
                started = await self._resume_running_attempt(context=context, lease=lease)
            else:
                await self.apply_compatible_reuse(
                    context=context,
                    lease=lease,
                    runner=runner,
                )
                await self._raise_if_cancel_requested(context)
                started = await self.start_next_attempt(context=context, lease=lease)
            if started is None:
                if last_plan is None:
                    last_plan = await self._load_active_plan(context)
                run = await self._load_run(context)
                await self._raise_if_cancel_requested(context)
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
            await self._raise_if_cancel_requested(context)
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

            await self._raise_if_cancel_requested(context)
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

            await run_lease_handle.use_current(
                lambda current_lease,
                started=started,
                target_status=target_status,
                document=document: self._finalize_step_attempt(
                    context=context,
                    lease=current_lease,
                    started=started,
                    target_status=target_status,
                    document=document,
                )
            )

            if target_status is PlanStepRunStatus.FAILED_TERMINAL:
                if self._planning_service is not None:
                    return await self._replan_terminal_failure(
                        context=context,
                        run_lease_handle=run_lease_handle,
                        plan=started.plan,
                        failed_step=started.step,
                        failed_step_run_id=started.step_run.step_run_id,
                    )
                return PlanExecutionOutcome(
                    state="replan_required",
                    plan=started.plan,
                    run=await self._load_run(context),
                )

    async def _raise_if_cancel_requested(self, context: TenantContext) -> None:
        await WorkflowCoordinator(
            self._database, settings=self._settings
        ).raise_if_cancel_requested(context)

    async def _cancel_at_boundary(
        self,
        context: TenantContext,
        run_lease_handle: RunLeaseHandle,
    ) -> PlanExecutionOutcome:
        run = await self._load_run(context)
        if run.status is RunStatus.CANCELLED:
            return PlanExecutionOutcome(
                state="cancelled",
                plan=await self._load_active_plan(context),
                run=run,
            )
        if run.lease_owner is None or run.lease_expires_at is None:
            raise PlanExecutionBlocked("cancelled Plan run has no current lease")
        lease = RunLease(
            context=context,
            lease_owner=run.lease_owner,
            fencing_token=run.fencing_token,
            version=run.version,
            lease_expires_at=run.lease_expires_at,
        )
        await run_lease_handle.replace(lease)
        async with TenantUnitOfWork(
            self._database,
            context,
            planning_settings=self._settings.planning,
            workflow_settings=self._settings.workflow,
        ) as uow:
            running = await uow.plans.running_step_attempts(
                run_id=str(context.run_id), for_update=True
            )
            if len(running) > 1:
                raise PlanExecutionBlocked("Plan run has multiple running step attempts")
            if running:
                attempt = running[0]
                await uow.plans.cancel_step_attempt(
                    lease,
                    step_run_id=attempt.step_run_id,
                    expected_version=attempt.version,
                )
        terminal = await WorkflowCoordinator(
            self._database, settings=self._settings
        ).finish_run_with_checkpoint(lease, RunStatus.CANCELLED)
        await run_lease_handle.replace(terminal)
        plan = await self._load_active_plan(context)
        final_run = await self._load_run(context)
        if self._event_router is not None:
            assert context.session_id is not None and context.run_id is not None
            reference = PlanReference(
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                session_id=context.session_id,
                run_id=context.run_id,
                plan_id=plan.plan_id,
                plan_version=plan.current_version,
                aggregate_version=plan.aggregate_version,
            ).model_dump(mode="json")
            await self._event_router.publish(
                ScopedEvent.from_context(
                    context,
                    "plan.run_status",
                    {**reference, "status": RunStatus.CANCELLED.value},
                )
            )
        return PlanExecutionOutcome(
            state="cancelled",
            plan=plan,
            run=final_run,
        )

    async def _replan_terminal_failure(
        self,
        *,
        context: TenantContext,
        run_lease_handle: RunLeaseHandle,
        plan: PlanSnapshot,
        failed_step: PlanStepRecord,
        failed_step_run_id: str,
    ) -> PlanExecutionOutcome:
        """Durably mark failure before the no-tools revision generation boundary."""
        await self._raise_if_cancel_requested(context)
        failed = await self._step_attempt(context, failed_step_run_id)
        if failed.status is not PlanStepRunStatus.FAILED_TERMINAL:
            raise PlanExecutionBlocked("Plan replan requires a terminal failed attempt")
        lease = await run_lease_handle.current()
        failure_digest = _canonical_digest(
            {
                "step_run_id": failed.step_run_id,
                "error_code": failed.error_code,
                "error_detail_redacted": failed.error_detail_redacted,
            }
        )
        await WorkflowCoordinator(self._database, settings=self._settings).checkpoint(
            lease,
            CheckpointPhase.PLAN_REPLAN_REQUIRED,
            {
                "run_id": str(context.run_id),
                "plan_id": plan.plan_id,
                "plan_version": plan.current_version,
                "plan_digest": plan.current.content_digest,
                "failed_step_run_id": failed.step_run_id,
                "failure_digest": failure_digest,
                "revision_cursor": "generate_revision",
                "cursor": "generate_revision",
            },
        )
        if plan.current_version - 1 >= self._settings.planning.max_revisions:
            return await self._fail_replan_terminal(run_lease_handle, plan)
        planning_service = self._planning_service
        if planning_service is None:
            return await self._fail_replan_terminal(run_lease_handle, plan)
        generator = planning_service._generator
        if generator is None:
            return await self._fail_replan_terminal(run_lease_handle, plan)
        revision = await self._revision_context(
            context=context,
            lease=lease,
            plan=plan,
            failed_step=failed_step,
            failed=failed,
            failure_digest=failure_digest,
        )
        try:
            await self._raise_if_cancel_requested(context)
            generated = await generator.generate(
                plan.current.objective,
                revision=revision,
                max_steps=self._settings.planning.max_steps,
                max_depth=self._settings.planning.max_dependency_depth,
                max_attempts=self._settings.planning.max_step_attempts,
            )
            await self._raise_if_cancel_requested(context)
        except PlanCancellationRequested:
            raise
        except Exception:  # noqa: BLE001 - generator faults terminate this boundary
            return await self._fail_replan_terminal(run_lease_handle, plan)
        draft = PlanningService._draft(generated)
        if failed_step.logical_step_key not in {
            step.logical_step_key for step in draft.steps
        }:
            raise PlanGenerationError("failed step must be retained or superseded")
        try:
            result = await planning_service.materialize_failure_revision(
                FailureRevisionRequest(
                    context=context,
                    lease=lease,
                    plan=plan,
                    failed_step_run=failed,
                    draft=draft,
                )
            )
        except PlanRevisionLimitError:
            return await self._fail_replan_terminal(run_lease_handle, plan)
        await run_lease_handle.replace(result.lease)
        return PlanExecutionOutcome(state="awaiting_user", plan=result.plan, run=result.run)

    async def _fail_replan_terminal(
        self,
        run_lease_handle: RunLeaseHandle,
        plan: PlanSnapshot,
    ) -> PlanExecutionOutcome:
        lease = await run_lease_handle.current()
        terminal = await WorkflowCoordinator(
            self._database, settings=self._settings
        ).finish_run_with_checkpoint(lease, RunStatus.FAILED_TERMINAL)
        await run_lease_handle.replace(terminal)
        return PlanExecutionOutcome(
            state="failed_terminal",
            plan=plan,
            run=await self._load_run(lease.context),
        )

    async def _revision_context(
        self,
        *,
        context: TenantContext,
        lease: RunLease,
        plan: PlanSnapshot,
        failed_step: PlanStepRecord,
        failed: PlanStepRunRecord,
        failure_digest: str,
    ) -> PlanRevisionContext:
        """Build revision input from one locked, strictly verified Plan version."""
        async with TenantUnitOfWork(
            self._database,
            context,
            planning_settings=self._settings.planning,
            workflow_settings=self._settings.workflow,
        ) as uow:
            await uow.plans.lock_run_for_execution(lease)
            current = await uow.plans.get(plan.plan_id)
            if (
                current is None
                or current.current_version != plan.current_version
                or current.aggregate_version != plan.aggregate_version
            ):
                raise PlanExecutionBlocked("Plan revision context changed")
            current_steps = {
                step.step_id: step for step in current.current.steps
            }
            current_failed_step = current_steps.get(failed.step_id)
            if (
                failed.plan_id != current.plan_id
                or failed.plan_version != current.current_version
                or failed.run_id != str(context.run_id)
                or current_failed_step is None
                or current_failed_step.step_id != failed_step.step_id
                or failed.status is not PlanStepRunStatus.FAILED_TERMINAL
            ):
                raise PlanExecutionBlocked("Plan revision failure is inconsistent")
            latest = await uow.plans.latest_step_attempts(
                plan_id=current.plan_id,
                plan_version=current.current_version,
                run_id=str(context.run_id),
                for_update=True,
            )
            verified_results = await self._load_succeeded_results(
                context=context,
                plan=current,
                latest=latest,
                memory=uow.memory,
                plans=uow.plans,
                for_update=True,
            )
            completed = [
                CompletedStepContext(
                    logical_step_key=step.logical_step_key,
                    summary=str(redact(verified_results[step.step_id].summary)),
                    result_digest=verified_results[step.step_id].digest(),
                )
                for step in sorted(
                    current.current.steps,
                    key=lambda step: (step.ordinal, step.step_id),
                )
                if step.step_id in verified_results
            ]
        return PlanRevisionContext(
            plan_id=plan.plan_id,
            parent_version=plan.current_version,
            feedback=None,
            failed_step_key=failed_step.logical_step_key,
            failed_error=f"failure_digest:{failure_digest}",
            completed=completed,
            current_plan=revision_current_plan_context(current.current),
        )

    async def _step_attempt(
        self, context: TenantContext, step_run_id: str
    ) -> PlanStepRunRecord:
        async with self._database.connect() as conn:
            plans = PlanRepository(
                conn, self._database.dialect, context, self._settings.planning
            )
            attempt = await plans.step_run_by_id(
                run_id=str(context.run_id), step_run_id=step_run_id
            )
        if attempt is None:
            raise PlanExecutionBlocked("Plan failed step attempt is unavailable")
        return attempt

    async def _finalize_step_attempt(
        self,
        *,
        context: TenantContext,
        lease: RunLease,
        started: StartedPlanStep,
        target_status: PlanStepRunStatus,
        document: PlanStepResultDocument,
    ) -> None:
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
                plans=uow.plans,
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
        runner: PlanStepRunner,
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
            plans=plans,
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
        plans: PlanRepository,
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
            document_source_attempt = attempt
            document_source_step = step
            if attempt.reused_from_step_run_id is not None:
                lineage_source_attempt = await plans.step_run_by_id(
                    run_id=str(context.run_id),
                    step_run_id=attempt.reused_from_step_run_id,
                    for_update=for_update,
                )
                if (
                    lineage_source_attempt is None
                    or lineage_source_attempt.reused_from_step_run_id is not None
                    or lineage_source_attempt.plan_id != plan.plan_id
                    or lineage_source_attempt.run_id != str(context.run_id)
                    or lineage_source_attempt.status is not PlanStepRunStatus.SUCCEEDED
                    or lineage_source_attempt.result_ref != attempt.result_ref
                    or lineage_source_attempt.result_digest != attempt.result_digest
                    or lineage_source_attempt.result_summary != attempt.result_summary
                ):
                    raise PlanExecutionBlocked("Plan reused result lineage is inconsistent")
                lineage_source_version = next(
                    (
                        version
                        for version in plan.versions
                        if version.plan_version == lineage_source_attempt.plan_version
                    ),
                    None,
                )
                lineage_source_step = (
                    None
                    if lineage_source_version is None
                    else next(
                        (
                            item
                            for item in lineage_source_version.steps
                            if item.step_id == lineage_source_attempt.step_id
                        ),
                        None,
                    )
                )
                if (
                    lineage_source_step is None
                    or step.supersedes_step_id != lineage_source_step.step_id
                    or step.definition_digest != lineage_source_step.definition_digest
                ):
                    raise PlanExecutionBlocked("Plan reused result lineage is inconsistent")
                document_source_attempt = lineage_source_attempt
                document_source_step = lineage_source_step
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
                or entry.metadata.get("step_run_id") != document_source_attempt.step_run_id
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
                or document.plan_version != document_source_attempt.plan_version
                or document.run_id != context.run_id
                or document.step_id != document_source_step.step_id
                or document.step_run_id != document_source_attempt.step_run_id
                or document.attempt != document_source_attempt.attempt
                or document.status != "succeeded"
                or document.definition_digest != document_source_step.definition_digest
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
                if (
                    attempt.reused_from_step_run_id is not None
                    and latest[dependency_id].reused_from_step_run_id is None
                ):
                    raise PlanExecutionBlocked(
                        "Plan reused dependency lineage is inconsistent"
                    )
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
