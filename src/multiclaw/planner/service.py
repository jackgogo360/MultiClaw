from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from multiclaw.config import Settings
from multiclaw.events.types import ScopedEvent
from multiclaw.memory.models import MemoryEntry
from multiclaw.planner.generator import PlanGenerator
from multiclaw.planner.models import (
    MaterializeInitialPlan,
    PlanDecisionAction,
    PlanDecisionMutationResult,
    PlanDecisionRecord,
    PlanDecisionRequest,
    PlanDecisionResult,
    PlanDraft,
    PlanDraftStep,
    PlanMaterializationResult,
    PlanNotFoundError,
    PlanReference,
    PlanRevisionContext,
    PlanRevisionLimitError,
    PlanSnapshot,
    ValidatedPlanDraft,
)
from multiclaw.planner.validation import sanitize_plan_text, validate_plan_draft
from multiclaw.security.redaction import redact
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.plans import PlanRepository
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import RunRecord


class PlanningService:
    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        generator: PlanGenerator | None = None,
        workflow: WorkflowCoordinator | None = None,
    ) -> None:
        self._database = database
        self._settings = settings or Settings(_config_file="/nonexistent")
        self._generator = generator
        self.workflow = workflow or WorkflowCoordinator(
            database,
            settings=self._settings,
        )

    async def materialize_initial(
        self,
        request: MaterializeInitialPlan,
    ) -> PlanMaterializationResult:
        if request.context.session_id is None or request.context.run_id is None:
            raise ValueError("initial Plan materialization requires session and run scope")

        async with TenantUnitOfWork(
            self._database,
            request.context,
            planning_settings=self._settings.planning,
            workflow_settings=self._settings.workflow,
        ) as uow:
            validate_plan_draft(
                request.draft,
                max_steps=self._settings.planning.max_steps,
                max_depth=self._settings.planning.max_dependency_depth,
                max_attempts=self._settings.planning.max_step_attempts,
            )
            plan = await uow.plans.for_context(request.context).create(
                plan_id=str(uuid4()),
                source_message_id=request.source_message_id,
                trigger_mode=request.trigger_mode,
                draft=request.draft,
            )
            coordinator = self.workflow._scoped(uow.conn)
            await coordinator.start_plan_run_with_checkpoint(
                request.context,
                request.runtime_instance_id,
                plan_id=plan.plan_id,
                plan_version=plan.current_version,
                plan_digest=plan.current.content_digest,
            )
            reference = self._reference(plan, request.context)
            message = await self._persist_reference(uow, request, reference)
            run = await uow.workflow.get_run(request.context)
            if run is None:
                raise RuntimeError("Plan run missing after materialization")

        return PlanMaterializationResult(
            plan=plan,
            reference=reference,
            reference_message_id=message.id,
            run=run,
            event=ScopedEvent.from_context(
                request.context,
                "plan.created",
                reference.model_dump(mode="json"),
            ),
        )

    async def decide(
        self,
        request: PlanDecisionRequest,
        *,
        decided_by: str,
        runtime_instance_id: str,
    ) -> PlanDecisionResult:
        request = self._normalize_decision_request(request)
        context, snapshot, run, replay = await self._read_decision_state(
            request,
            decided_by=decided_by,
        )
        if replay is not None:
            return PlanDecisionResult(
                snapshot=replay.snapshot,
                decision=replay.decision,
                run=run,
                lease=None,
                idempotent_replay=True,
                events=(),
            )

        if request.action is PlanDecisionAction.REVISE:
            return await self._revise(
                request,
                context=context,
                snapshot=snapshot,
                decided_by=decided_by,
                runtime_instance_id=runtime_instance_id,
            )
        return await self._resolve(
            request,
            context=context,
            decided_by=decided_by,
            runtime_instance_id=runtime_instance_id,
        )

    async def _persist_reference(
        self,
        uow: TenantUnitOfWork,
        request: MaterializeInitialPlan,
        reference: PlanReference,
    ) -> MemoryEntry:
        return await uow.memory.save(
            MemoryEntry(
                content="",
                type="chat_message",
                role="assistant",
                turn_index=request.assistant_turn_index,
                metadata={
                    "parts": [
                        {
                            "type": "data-plan-created",
                            "data": reference.model_dump(mode="json"),
                        }
                    ]
                },
            )
        )

    async def _read_decision_state(
        self,
        request: PlanDecisionRequest,
        *,
        decided_by: str,
    ) -> tuple[
        TenantContext,
        PlanSnapshot,
        RunRecord,
        PlanDecisionMutationResult | None,
    ]:
        async with self._database.connect() as conn:
            context = await PlanRepository.locate_context(
                conn,
                tenant_id=decided_by,
                plan_id=request.plan_id,
            )
            repository = PlanRepository(
                conn,
                self._database.dialect,
                context,
                self._settings.planning,
            )
            replay = await repository.replay_decision(
                request,
                decided_by=decided_by,
            )
            snapshot = replay.snapshot if replay is not None else await repository.get(
                request.plan_id
            )
            if snapshot is None:
                raise PlanNotFoundError("Plan not found")
            run = await self.workflow._scoped(conn).get_plan_run(
                context,
                request.plan_id,
            )
            if run is None:
                raise PlanNotFoundError("Plan not found")
        return context, snapshot, run, replay

    async def _revise(
        self,
        request: PlanDecisionRequest,
        *,
        context: TenantContext,
        snapshot: PlanSnapshot,
        decided_by: str,
        runtime_instance_id: str,
    ) -> PlanDecisionResult:
        if self._generator is None:
            raise RuntimeError("Plan generator is required for revision")
        revision_context = PlanRevisionContext(
            plan_id=snapshot.plan_id,
            parent_version=snapshot.current_version,
            feedback=request.feedback,
            failed_step_key=None,
            failed_error=None,
            completed=[],
        )
        generated = await self._generator.generate(
            snapshot.current.objective,
            revision=revision_context,
            max_steps=self._settings.planning.max_steps,
            max_depth=self._settings.planning.max_dependency_depth,
            max_attempts=self._settings.planning.max_step_attempts,
        )
        draft = self._draft(generated)
        validate_plan_draft(
            draft,
            max_steps=self._settings.planning.max_steps,
            max_depth=self._settings.planning.max_dependency_depth,
            max_attempts=self._settings.planning.max_step_attempts,
        )

        async with TenantUnitOfWork(
            self._database,
            context,
            planning_settings=self._settings.planning,
            workflow_settings=self._settings.workflow,
        ) as uow:
            repository = uow.plans.for_context(context)
            claim = await repository.begin_revision_decision(
                request,
                decided_by=decided_by,
            )
            if claim.resulting_plan_version is not None:
                replay = await repository.replay_decision(
                    request,
                    decided_by=decided_by,
                )
                if replay is None:
                    raise RuntimeError("completed revision decision could not be replayed")
                run = await uow.workflow.get_plan_run(context, request.plan_id)
                if run is None:
                    raise PlanNotFoundError("Plan not found")
                return PlanDecisionResult(
                    snapshot=replay.snapshot,
                    decision=replay.decision,
                    run=run,
                    lease=None,
                    idempotent_replay=True,
                    events=(),
                )
            current = await repository.get(request.plan_id)
            if current is None:
                raise PlanNotFoundError("Plan not found")
            if current.current_version - 1 >= self._settings.planning.max_revisions:
                raise PlanRevisionLimitError("Plan revision limit exceeded")
            prior_steps = {
                step.logical_step_key: step.step_id for step in current.current.steps
            }
            revised = await repository.append_version(
                plan_id=request.plan_id,
                expected_version=request.expected_version,
                draft=draft,
                parent_version=request.plan_version,
                revision_feedback=request.feedback,
                supersedes={
                    step.logical_step_key: prior_steps[step.logical_step_key]
                    for step in draft.steps
                    if step.logical_step_key in prior_steps
                },
            )
            run = await uow.workflow.get_plan_run(context, request.plan_id)
            if run is None:
                raise PlanNotFoundError("Plan not found")
            await self.workflow._scoped(uow.conn).fence_waiting_plan_run(
                run.context,
                runtime_instance_id=runtime_instance_id,
                plan_id=request.plan_id,
                plan_version=revised.current_version,
                expected_run_version=run.version,
                plan_digest=revised.current.content_digest,
            )
            decision = await repository.finish_revision_decision(
                plan_id=request.plan_id,
                decision_id=request.decision_id,
                resulting_plan_version=revised.current_version,
            )
            persisted_revision = await repository.get(request.plan_id)
            if persisted_revision is None:
                raise PlanNotFoundError("Plan not found")
            revised = persisted_revision
            run = await uow.workflow.get_plan_run(context, request.plan_id)
            if run is None:
                raise PlanNotFoundError("Plan not found")

        return PlanDecisionResult(
            snapshot=revised,
            decision=decision,
            run=run,
            lease=None,
            idempotent_replay=False,
            events=(
                self._decision_event(
                    revised,
                    run,
                    decision,
                    event_type="plan.revised",
                ),
            ),
        )

    async def _resolve(
        self,
        request: PlanDecisionRequest,
        *,
        context: TenantContext,
        decided_by: str,
        runtime_instance_id: str,
    ) -> PlanDecisionResult:
        async with TenantUnitOfWork(
            self._database,
            context,
            planning_settings=self._settings.planning,
            workflow_settings=self._settings.workflow,
        ) as uow:
            repository = uow.plans.for_context(context)
            mutation = await repository.record_decision(
                request,
                decided_by=decided_by,
            )
            run = await uow.workflow.get_plan_run(context, request.plan_id)
            if run is None:
                raise PlanNotFoundError("Plan not found")
            if mutation.idempotent_replay:
                return PlanDecisionResult(
                    snapshot=mutation.snapshot,
                    decision=mutation.decision,
                    run=run,
                    lease=None,
                    idempotent_replay=True,
                    events=(),
                )
            coordinator = self.workflow._scoped(uow.conn)
            if request.action is PlanDecisionAction.APPROVE:
                lease = await coordinator.resume_waiting_plan_run(
                    run.context,
                    runtime_instance_id=runtime_instance_id,
                    plan_id=request.plan_id,
                    plan_version=request.plan_version,
                    expected_run_version=run.version,
                )
            else:
                await coordinator.cancel_waiting_plan_run(
                    run.context,
                    runtime_instance_id=runtime_instance_id,
                    plan_id=request.plan_id,
                    plan_version=request.plan_version,
                    expected_run_version=run.version,
                )
                lease = None
            run = await uow.workflow.get_run(run.context)
            if run is None:
                raise PlanNotFoundError("Plan not found")

        return PlanDecisionResult(
            snapshot=mutation.snapshot,
            decision=mutation.decision,
            run=run,
            lease=lease,
            idempotent_replay=False,
            events=(
                self._decision_event(
                    mutation.snapshot,
                    run,
                    mutation.decision,
                    event_type="plan.decision",
                ),
            ),
        )

    @staticmethod
    def _reference(snapshot: PlanSnapshot, context: TenantContext) -> PlanReference:
        assert context.session_id is not None and context.run_id is not None
        return PlanReference(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            run_id=context.run_id,
            plan_id=snapshot.plan_id,
            plan_version=snapshot.current_version,
            aggregate_version=snapshot.aggregate_version,
        )

    @staticmethod
    def _normalize_decision_request(
        request: PlanDecisionRequest,
    ) -> PlanDecisionRequest:
        if request.feedback is None:
            return request
        safe_feedback = sanitize_plan_text(request.feedback)
        if safe_feedback == request.feedback:
            return request
        return request.model_copy(update={"feedback": safe_feedback})

    @classmethod
    def _decision_event(
        cls,
        snapshot: PlanSnapshot,
        run: RunRecord,
        decision: PlanDecisionRecord,
        *,
        event_type: str,
    ) -> ScopedEvent:
        data = cls._reference(snapshot, run.context).model_dump(mode="json")
        decision_data = asdict(decision)
        if decision_data["feedback"] is not None:
            decision_data["feedback"] = sanitize_plan_text(decision_data["feedback"])
        data["decision"] = decision_data
        safe_data = redact(data)
        assert isinstance(safe_data, dict)
        return ScopedEvent.from_context(run.context, event_type, safe_data)

    @staticmethod
    def _draft(generated: PlanDraft | ValidatedPlanDraft) -> PlanDraft:
        if isinstance(generated, PlanDraft):
            return generated
        return PlanDraft(
            objective=generated.objective,
            constraints=list(generated.constraints),
            generation_reason=generated.generation_reason,
            steps=[
                PlanDraftStep(
                    logical_step_key=step.logical_step_key,
                    title=step.title,
                    description=step.description,
                    expected_outcome=step.expected_outcome,
                    depends_on=list(step.depends_on),
                    max_attempts=step.max_attempts,
                )
                for step in generated.steps
            ],
        )


__all__ = ["MaterializeInitialPlan", "PlanningService"]
