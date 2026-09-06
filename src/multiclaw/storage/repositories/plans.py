from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from uuid import uuid4

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction

from multiclaw.config.settings import PlanningSettings
from multiclaw.planner.models import (
    TERMINAL_PLAN_STEP_STATUSES,
    PlanAttemptLimitError,
    PlanDecisionAction,
    PlanDecisionIdempotencyError,
    PlanDecisionMutationResult,
    PlanDecisionRecord,
    PlanDecisionRequest,
    PlanDraft,
    PlanDraftStep,
    PlanExecutionBlocked,
    PlanNotFoundError,
    PlanSnapshot,
    PlanStatus,
    PlanStepAlreadyRunningError,
    PlanStepRecord,
    PlanStepResultDocument,
    PlanStepRunRecord,
    PlanStepRunStatus,
    PlanSummary,
    PlanTriggerMode,
    PlanVersionConflictError,
    PlanVersionRecord,
    ValidatedPlanDraft,
    is_plan_run_executable,
    is_plan_step_ready,
)
from multiclaw.planner.validation import (
    plan_content_digest,
    step_definition_digest,
    validate_plan_draft,
)
from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect
from multiclaw.storage.repositories.workflow import current_lease_predicate
from multiclaw.storage.schema import (
    agent_plan_decisions,
    agent_plan_step_dependencies,
    agent_plan_step_runs,
    agent_plan_steps,
    agent_plan_versions,
    agent_plans,
    agent_runs,
)
from multiclaw.tenancy.context import TenantContext
from multiclaw.workflow.models import (
    RunLease,
    RunStatus,
    StaleFenceError,
    VersionConflictError,
)

Dialect = SQLiteDialect | MySQLDialect


def _note_cleanup_error(primary: BaseException, phase: str, error: BaseException) -> None:
    primary.add_note(f"{phase} cleanup failed: {type(error).__name__}: {error}")


@dataclass(slots=True)
class _PlanTransactionState:
    pending_revisions: dict[tuple[str, str], PlanDecisionRecord] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class PlanRepository:
    _conn: AsyncConnection
    _dialect: Dialect
    _context: TenantContext
    _settings: PlanningSettings
    _transaction_state: _PlanTransactionState = field(
        default_factory=_PlanTransactionState
    )

    @property
    def connection(self) -> AsyncConnection:
        return self._conn

    @staticmethod
    async def locate_context(
        connection: AsyncConnection,
        *,
        tenant_id: str,
        plan_id: str,
    ) -> TenantContext:
        result = await connection.execute(
            select(
                agent_plans.c.tenant_id,
                agent_plans.c.workspace_id,
                agent_plans.c.session_id,
            )
            .where(
                agent_plans.c.tenant_id == tenant_id,
                agent_plans.c.id == plan_id,
            )
            .limit(2)
        )
        rows = result.mappings().all()
        if len(rows) != 1:
            raise PlanNotFoundError("Plan not found")
        row = rows[0]
        if str(row["tenant_id"]) != tenant_id:
            raise PlanNotFoundError("Plan not found")
        return TenantContext(
            tenant_id=tenant_id,
            workspace_id=str(row["workspace_id"]),
            session_id=str(row["session_id"]),
        )

    def _require_session(self) -> str:
        if self._context.session_id is None:
            raise ValueError("PlanRepository requires session scope")
        return self._context.session_id

    def for_context(self, context: TenantContext) -> PlanRepository:
        if context.session_id is None:
            raise ValueError("PlanRepository requires session scope")
        if (
            context.tenant_id != self._context.tenant_id
            or context.workspace_id != self._context.workspace_id
        ):
            raise ValueError("PlanRepository context must remain within the UoW scope")
        return PlanRepository(
            self._conn,
            self._dialect,
            context,
            self._settings,
            self._transaction_state,
        )

    def require_no_unfinished_revisions(self) -> None:
        if self._transaction_state.pending_revisions:
            raise RuntimeError("Cannot commit with an unfinished plan revision")

    async def create(
        self,
        *,
        plan_id: str,
        source_message_id: str,
        trigger_mode: PlanTriggerMode,
        draft: PlanDraft,
    ) -> PlanSnapshot:
        self._require_session()
        validated = validate_plan_draft(
            draft,
            max_steps=self._settings.max_steps,
            max_depth=self._settings.max_dependency_depth,
            max_attempts=self._settings.max_step_attempts,
        )
        savepoint = await self._conn.begin_nested()
        try:
            now = await self._db_now_ms()
            await self._conn.execute(
                insert(agent_plans).values(
                    id=plan_id,
                    **self._scope_values(),
                    source_message_id=source_message_id,
                    trigger_mode=trigger_mode.value,
                    status=PlanStatus.AWAITING_APPROVAL.value,
                    current_version=1,
                    approved_version=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await self._insert_version(
                plan_id=plan_id,
                plan_version=1,
                validated=validated,
                parent_version=None,
                revision_feedback=None,
                supersedes={},
                created_at=now,
            )
            created = await self.get(plan_id)
            if created is None:
                raise RuntimeError("Plan missing after materialization")
            await savepoint.commit()
            return created
        except BaseException as primary:
            await self._rollback_savepoint(savepoint, primary)
            raise

    async def append_version(
        self,
        *,
        plan_id: str,
        expected_version: int,
        draft: PlanDraft,
        parent_version: int,
        revision_feedback: str | None,
        supersedes: Mapping[str, str],
    ) -> PlanSnapshot:
        self._require_session()
        validated = validate_plan_draft(
            draft,
            max_steps=self._settings.max_steps,
            max_depth=self._settings.max_dependency_depth,
            max_attempts=self._settings.max_step_attempts,
        )
        current_result = await self._conn.execute(
            select(agent_plans.c.current_version, agent_plans.c.version)
            .where(self._plan_predicate(plan_id))
            .with_for_update()
        )
        current = current_result.mappings().first()
        if current is None:
            raise ValueError("Plan not found")
        current_version = int(current["current_version"])
        aggregate_version = int(current["version"])
        if aggregate_version != expected_version:
            raise ValueError("Plan version conflict")
        if parent_version != current_version:
            raise ValueError("parent_version must equal current_version")

        next_version = current_version + 1
        await self._validate_supersedes(
            plan_id=plan_id,
            plan_version=next_version,
            validated=validated,
            supersedes=supersedes,
        )

        savepoint = await self._conn.begin_nested()
        try:
            now = await self._db_now_ms()
            await self._insert_version(
                plan_id=plan_id,
                plan_version=next_version,
                validated=validated,
                parent_version=parent_version,
                revision_feedback=revision_feedback,
                supersedes=supersedes,
                created_at=now,
            )
            updated = await self._conn.execute(
                update(agent_plans)
                .where(
                    self._plan_predicate(plan_id),
                    agent_plans.c.current_version == current_version,
                    agent_plans.c.version == expected_version,
                )
                .values(
                    status=PlanStatus.AWAITING_APPROVAL.value,
                    current_version=next_version,
                    version=expected_version + 1,
                    updated_at=now,
                )
            )
            if int(updated.rowcount or 0) != 1:
                raise ValueError("Plan version conflict")
            await savepoint.commit()
        except BaseException as primary:
            await self._rollback_savepoint(savepoint, primary)
            raise

        revised = await self.get(plan_id)
        if revised is None:
            raise RuntimeError("Plan missing after revision")
        return revised

    async def record_decision(
        self,
        request: PlanDecisionRequest,
        *,
        decided_by: str,
    ) -> PlanDecisionMutationResult:
        self._require_session()
        pending_revision = self._pending_revision_claim(request.plan_id)
        if (
            pending_revision is not None
            and pending_revision.decision_id != request.decision_id
        ):
            raise PlanDecisionIdempotencyError(
                "Plan already has an unfinished revision decision claim"
            )
        existing = await self._get_decision(request.plan_id, request.decision_id)
        if existing is not None:
            return await self._decision_replay(request, decided_by, existing)
        if request.action is PlanDecisionAction.REVISE:
            raise ValueError("revision decisions require begin_revision_decision")

        latest = await self._lock_plan(request.plan_id)
        existing = await self._get_decision(
            request.plan_id,
            request.decision_id,
            for_update=True,
        )
        if existing is not None:
            return await self._decision_replay(request, decided_by, existing)
        self._require_current_decision_target(request, latest)
        decision_history = await self._require_unambiguous_decision_history(
            request.plan_id,
            request.expected_version,
        )
        if any(
            decision.expected_plan_cas_version == request.expected_version
            for decision in decision_history
        ):
            raise PlanDecisionIdempotencyError(
                "Plan aggregate version already has a different decision claim"
            )

        savepoint = await self._conn.begin_nested()
        try:
            now = await self._db_now_ms()
            await self._insert_decision(
                request,
                decided_by=decided_by,
                resulting_plan_version=None,
                created_at=now,
            )
            updated = await self._conn.execute(
                update(agent_plans)
                .where(
                    self._plan_predicate(request.plan_id),
                    agent_plans.c.status == PlanStatus.AWAITING_APPROVAL.value,
                    agent_plans.c.current_version == request.plan_version,
                    agent_plans.c.version == request.expected_version,
                )
                .values(
                    status=(
                        PlanStatus.APPROVED
                        if request.action is PlanDecisionAction.APPROVE
                        else PlanStatus.REJECTED
                    ).value,
                    approved_version=(
                        request.plan_version
                        if request.action is PlanDecisionAction.APPROVE
                        else agent_plans.c.approved_version
                    ),
                    version=agent_plans.c.version + 1,
                    updated_at=now,
                )
            )
            if int(updated.rowcount or 0) == 1:
                result = await self._decision_result(
                    request.plan_id,
                    request.decision_id,
                    idempotent_replay=False,
                )
                await savepoint.commit()
                return result
        except IntegrityError as primary:
            if not await self._rollback_savepoint(savepoint, primary):
                raise
            if not self._is_decision_id_duplicate(primary):
                raise
            existing = await self._get_decision(
                request.plan_id,
                request.decision_id,
                for_update=True,
            )
            if existing is None:
                raise
            return await self._decision_replay(request, decided_by, existing)
        except BaseException as primary:
            await self._rollback_savepoint(savepoint, primary)
            raise

        cas_error = RuntimeError("Plan decision compare-and-swap failed")
        if not await self._rollback_savepoint(savepoint, cas_error):
            raise cas_error
        conflict_snapshot = await self.get(request.plan_id)
        if conflict_snapshot is None:
            raise PlanNotFoundError("Plan not found")
        raise PlanVersionConflictError(conflict_snapshot)

    async def replay_decision(
        self,
        request: PlanDecisionRequest,
        *,
        decided_by: str,
    ) -> PlanDecisionMutationResult | None:
        self._require_session()
        existing = await self._get_decision(request.plan_id, request.decision_id)
        if existing is None:
            return None
        return await self._decision_replay(request, decided_by, existing)

    async def begin_revision_decision(
        self,
        request: PlanDecisionRequest,
        *,
        decided_by: str,
    ) -> PlanDecisionRecord:
        self._require_session()
        if request.action is not PlanDecisionAction.REVISE:
            raise ValueError("begin_revision_decision requires a revise action")

        pending_revision = self._pending_revision_claim(request.plan_id)
        if pending_revision is not None:
            if pending_revision.decision_id != request.decision_id:
                raise PlanDecisionIdempotencyError(
                    "Plan already has a different revision decision claim"
                )
            self._require_identical_decision(request, decided_by, pending_revision)
            return pending_revision

        existing = await self._get_decision(request.plan_id, request.decision_id)
        if existing is not None:
            self._require_identical_decision(request, decided_by, existing)
            if existing.resulting_plan_version is not None:
                return existing

        latest = await self._lock_plan(request.plan_id)
        existing = await self._get_decision(
            request.plan_id,
            request.decision_id,
            for_update=True,
        )
        if existing is not None:
            self._require_identical_decision(request, decided_by, existing)
            if existing.resulting_plan_version is None:
                self._require_current_decision_target(request, latest)
                await self._require_unambiguous_decision_history(
                    request.plan_id,
                    request.expected_version,
                )
                self._register_revision_claim(request.plan_id, existing)
            return existing
        self._require_current_decision_target(request, latest)
        decision_history = await self._require_unambiguous_decision_history(
            request.plan_id,
            request.expected_version,
        )
        if any(
            decision.expected_plan_cas_version == request.expected_version
            for decision in decision_history
        ):
            raise PlanDecisionIdempotencyError(
                "Plan aggregate version already has a different decision claim"
            )

        savepoint = await self._conn.begin_nested()
        try:
            now = await self._db_now_ms()
            await self._insert_decision(
                request,
                decided_by=decided_by,
                resulting_plan_version=None,
                created_at=now,
            )
            decision = await self._get_decision(request.plan_id, request.decision_id)
            if decision is None:
                raise RuntimeError("Plan decision missing after insert")
            await savepoint.commit()
            self._register_revision_claim(request.plan_id, decision)
            return decision
        except IntegrityError as primary:
            if not await self._rollback_savepoint(savepoint, primary):
                raise
            if not self._is_decision_id_duplicate(primary):
                raise
            existing = await self._get_decision(
                request.plan_id,
                request.decision_id,
                for_update=True,
            )
            if existing is None:
                raise
            self._require_identical_decision(request, decided_by, existing)
            if existing.resulting_plan_version is None:
                self._register_revision_claim(request.plan_id, existing)
            return existing
        except BaseException as primary:
            await self._rollback_savepoint(savepoint, primary)
            raise

    async def finish_revision_decision(
        self,
        *,
        plan_id: str,
        decision_id: str,
        resulting_plan_version: int,
    ) -> PlanDecisionRecord:
        self._require_session()
        if resulting_plan_version < 1:
            raise ValueError("resulting_plan_version must be positive")

        existing = await self._get_decision(plan_id, decision_id, for_update=True)
        if existing is None:
            raise PlanNotFoundError("Plan decision not found")
        if existing.action is not PlanDecisionAction.REVISE:
            raise PlanDecisionIdempotencyError(
                "Plan decision is not a revision decision"
            )
        pending_revision = self._pending_revision_claim(plan_id)
        if existing.resulting_plan_version is not None:
            if existing.resulting_plan_version != resulting_plan_version:
                raise PlanDecisionIdempotencyError(
                    "Plan decision already has a different resulting version"
                )
            if pending_revision is not None:
                raise PlanDecisionIdempotencyError(
                    "Revision decision changed after this transaction claimed it"
                )
            return existing
        if pending_revision is None or pending_revision != existing:
            raise PlanDecisionIdempotencyError(
                "Revision decision is not claimed by this transaction"
            )
        await self._require_unambiguous_decision_history(
            plan_id,
            existing.expected_plan_cas_version,
        )

        valid_resulting_version = existing.plan_version + 1
        plan_match = await self._conn.execute(
            select(agent_plans.c.id)
            .where(
                self._plan_predicate(plan_id),
                agent_plans.c.status == PlanStatus.AWAITING_APPROVAL.value,
                agent_plans.c.current_version == valid_resulting_version,
                agent_plans.c.version
                == existing.expected_plan_cas_version + 1,
            )
            .with_for_update()
        )
        version_result = await self._conn.execute(
            select(
                agent_plan_versions.c.parent_version,
                agent_plan_versions.c.revision_feedback,
            )
            .where(
                self._version_scope_predicate(plan_id),
                agent_plan_versions.c.plan_version == valid_resulting_version,
            )
            .with_for_update()
        )
        version_row = version_result.mappings().first()
        if (
            resulting_plan_version != valid_resulting_version
            or plan_match.scalar_one_or_none() is None
            or version_row is None
            or version_row["parent_version"] != existing.plan_version
            or version_row["revision_feedback"] != existing.feedback
        ):
            latest = await self.get(plan_id)
            if latest is None:
                raise PlanNotFoundError("Plan not found")
            raise PlanVersionConflictError(latest)

        updated = await self._conn.execute(
            update(agent_plan_decisions)
            .where(
                self._decision_scope_predicate(plan_id),
                agent_plan_decisions.c.decision_id == decision_id,
                agent_plan_decisions.c.action == PlanDecisionAction.REVISE.value,
                agent_plan_decisions.c.plan_version == existing.plan_version,
                agent_plan_decisions.c.expected_plan_cas_version
                == existing.expected_plan_cas_version,
                agent_plan_decisions.c.decided_by == existing.decided_by,
                agent_plan_decisions.c.created_at == existing.created_at,
                agent_plan_decisions.c.resulting_plan_version.is_(None),
            )
            .values(resulting_plan_version=resulting_plan_version)
        )
        if int(updated.rowcount or 0) != 1:
            existing = await self._get_decision(plan_id, decision_id, for_update=True)
            if existing is None:
                raise PlanNotFoundError("Plan decision not found")
            if existing.resulting_plan_version != resulting_plan_version:
                raise PlanDecisionIdempotencyError(
                    "Plan decision already has a different resulting version"
                )
            self._clear_revision_claim(plan_id, decision_id)
            return existing

        finished = await self._get_decision(plan_id, decision_id)
        if finished is None:
            raise RuntimeError("Plan decision missing after finalization")
        self._clear_revision_claim(plan_id, decision_id)
        return finished

    async def get(self, plan_id: str) -> PlanSnapshot | None:
        self._require_session()
        plan_result = await self._conn.execute(
            select(agent_plans).where(self._plan_predicate(plan_id)).limit(1)
        )
        plan_row = plan_result.mappings().first()
        if plan_row is None:
            return None

        version_result = await self._conn.execute(
            select(agent_plan_versions)
            .where(self._version_scope_predicate(plan_id))
            .order_by(agent_plan_versions.c.plan_version)
        )
        version_rows = version_result.mappings().all()
        step_result = await self._conn.execute(
            select(agent_plan_steps)
            .where(self._step_scope_predicate(plan_id))
            .order_by(agent_plan_steps.c.plan_version, agent_plan_steps.c.ordinal)
        )
        step_rows = step_result.mappings().all()
        dependency_result = await self._conn.execute(
            select(agent_plan_step_dependencies).where(
                self._dependency_scope_predicate(plan_id)
            )
        )
        dependency_rows = dependency_result.mappings().all()
        decision_result = await self._conn.execute(
            select(agent_plan_decisions)
            .where(self._decision_scope_predicate(plan_id))
            .order_by(
                agent_plan_decisions.c.created_at,
                agent_plan_decisions.c.decision_id,
            )
        )
        decision_rows = decision_result.mappings().all()

        steps_by_version: dict[int, tuple[PlanStepRecord, ...]] = {}
        step_ordinals: dict[tuple[int, str], int] = {}
        for row in step_rows:
            plan_version = int(row["plan_version"])
            step_ordinals[(plan_version, str(row["step_id"]))] = int(row["ordinal"])
        for version in {int(row["plan_version"]) for row in step_rows}:
            steps_by_version[version] = tuple(
                self._hydrate_step(row)
                for row in step_rows
                if int(row["plan_version"]) == version
            )

        dependency_lists: dict[tuple[int, str], list[str]] = {}
        for row in dependency_rows:
            key = (int(row["plan_version"]), str(row["step_id"]))
            dependency_lists.setdefault(key, []).append(str(row["depends_on_step_id"]))
        dependencies_by_version: dict[int, dict[str, tuple[str, ...]]] = {}
        for (version, step_id), dependencies in dependency_lists.items():
            dependencies.sort(key=lambda dependency: step_ordinals[(version, dependency)])
            dependencies_by_version.setdefault(version, {})[step_id] = tuple(dependencies)

        versions = tuple(
            self._hydrate_version(
                row,
                steps=steps_by_version.get(int(row["plan_version"]), ()),
                dependencies=dependencies_by_version.get(int(row["plan_version"]), {}),
            )
            for row in version_rows
        )
        current_version = int(plan_row["current_version"])
        current = next(
            (version for version in versions if version.plan_version == current_version),
            None,
        )
        if current is None:
            raise ValueError("Plan current_version references missing version data")

        return PlanSnapshot(
            context=self._context,
            plan_id=str(plan_row["id"]),
            source_message_id=str(plan_row["source_message_id"]),
            trigger_mode=PlanTriggerMode(str(plan_row["trigger_mode"])),
            status=PlanStatus(str(plan_row["status"])),
            current_version=current_version,
            approved_version=(
                None
                if plan_row["approved_version"] is None
                else int(plan_row["approved_version"])
            ),
            aggregate_version=int(plan_row["version"]),
            created_at=int(plan_row["created_at"]),
            updated_at=int(plan_row["updated_at"]),
            current=current,
            versions=versions,
            decisions=tuple(self._hydrate_decision(row) for row in decision_rows),
        )

    async def _lock_plan(self, plan_id: str) -> PlanSnapshot:
        result = await self._conn.execute(
            select(agent_plans.c.id)
            .where(self._plan_predicate(plan_id))
            .with_for_update()
        )
        if result.scalar_one_or_none() is None:
            raise PlanNotFoundError("Plan not found")
        latest = await self.get(plan_id)
        if latest is None:
            raise PlanNotFoundError("Plan not found")
        return latest

    @staticmethod
    def _require_current_decision_target(
        request: PlanDecisionRequest,
        latest: PlanSnapshot,
    ) -> None:
        if (
            latest.status is not PlanStatus.AWAITING_APPROVAL
            or latest.current_version != request.plan_version
            or latest.aggregate_version != request.expected_version
        ):
            raise PlanVersionConflictError(latest)

    async def _insert_decision(
        self,
        request: PlanDecisionRequest,
        *,
        decided_by: str,
        resulting_plan_version: int | None,
        created_at: int,
    ) -> None:
        await self._conn.execute(
            insert(agent_plan_decisions).values(
                **self._scope_values(),
                plan_id=request.plan_id,
                decision_id=request.decision_id,
                plan_version=request.plan_version,
                expected_plan_cas_version=request.expected_version,
                action=request.action.value,
                feedback=request.feedback,
                decided_by=decided_by,
                resulting_plan_version=resulting_plan_version,
                created_at=created_at,
            )
        )

    async def _get_decision(
        self,
        plan_id: str,
        decision_id: str,
        *,
        for_update: bool = False,
    ) -> PlanDecisionRecord | None:
        statement = (
            select(agent_plan_decisions)
            .where(
                self._decision_scope_predicate(plan_id),
                agent_plan_decisions.c.decision_id == decision_id,
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._conn.execute(statement)
        row = result.mappings().first()
        return None if row is None else self._hydrate_decision(row)

    async def _require_unambiguous_decision_history(
        self,
        plan_id: str,
        through_expected_plan_cas_version: int,
    ) -> tuple[PlanDecisionRecord, ...]:
        result = await self._conn.execute(
            select(agent_plan_decisions)
            .where(
                self._decision_scope_predicate(plan_id),
                agent_plan_decisions.c.expected_plan_cas_version
                <= through_expected_plan_cas_version,
            )
            .with_for_update()
        )
        decisions = tuple(self._hydrate_decision(row) for row in result.mappings())
        boundaries = {
            decision.expected_plan_cas_version for decision in decisions
        }
        if len(boundaries) != len(decisions):
            raise PlanDecisionIdempotencyError(
                "Plan decision history has an ambiguous aggregate boundary"
            )
        return decisions

    async def _decision_replay(
        self,
        request: PlanDecisionRequest,
        decided_by: str,
        existing: PlanDecisionRecord,
    ) -> PlanDecisionMutationResult:
        self._require_identical_decision(request, decided_by, existing)
        return await self._decision_result(
            request.plan_id,
            request.decision_id,
            idempotent_replay=True,
        )

    async def _decision_result(
        self,
        plan_id: str,
        decision_id: str,
        *,
        idempotent_replay: bool,
    ) -> PlanDecisionMutationResult:
        snapshot = await self.get(plan_id)
        if snapshot is None:
            raise PlanNotFoundError("Plan not found")
        decision = next(
            (
                candidate
                for candidate in snapshot.decisions
                if candidate.decision_id == decision_id
            ),
            None,
        )
        if decision is None:
            raise RuntimeError("Plan decision missing from snapshot")
        snapshot = self._snapshot_at_decision(snapshot, decision)
        return PlanDecisionMutationResult(
            snapshot=snapshot,
            decision=decision,
            idempotent_replay=idempotent_replay,
        )

    @staticmethod
    def _snapshot_at_decision(
        snapshot: PlanSnapshot,
        decision: PlanDecisionRecord,
    ) -> PlanSnapshot:
        decisions = tuple(
            candidate
            for candidate in snapshot.decisions
            if candidate.expected_plan_cas_version
            <= decision.expected_plan_cas_version
        )
        decision_boundaries = {
            candidate.expected_plan_cas_version for candidate in decisions
        }
        if len(decision_boundaries) != len(decisions) or decision not in decisions:
            raise PlanDecisionIdempotencyError(
                "Plan decision history has an ambiguous aggregate boundary"
            )
        if decision.action is PlanDecisionAction.REVISE:
            if decision.resulting_plan_version is None:
                raise ValueError("revision decisions require begin_revision_decision")
            current_version = decision.resulting_plan_version
            status = PlanStatus.AWAITING_APPROVAL
        else:
            current_version = decision.plan_version
            status = (
                PlanStatus.APPROVED
                if decision.action is PlanDecisionAction.APPROVE
                else PlanStatus.REJECTED
            )

        versions = tuple(
            version
            for version in snapshot.versions
            if version.plan_version <= current_version
        )
        current = next(
            (
                version
                for version in versions
                if version.plan_version == current_version
            ),
            None,
        )
        if current is None:
            raise ValueError("Plan decision references missing version data")

        approvals = tuple(
            candidate
            for candidate in decisions
            if candidate.action is PlanDecisionAction.APPROVE
        )
        approved_version = (
            None
            if not approvals
            else max(
                approvals,
                key=lambda candidate: candidate.expected_plan_cas_version,
            ).plan_version
        )

        return replace(
            snapshot,
            status=status,
            current_version=current_version,
            approved_version=approved_version,
            aggregate_version=decision.expected_plan_cas_version + 1,
            updated_at=(
                current.created_at
                if decision.action is PlanDecisionAction.REVISE
                else decision.created_at
            ),
            current=current,
            versions=versions,
            decisions=decisions,
        )

    @staticmethod
    def _require_identical_decision(
        request: PlanDecisionRequest,
        decided_by: str,
        existing: PlanDecisionRecord,
    ) -> None:
        if (
            existing.plan_version != request.plan_version
            or existing.expected_plan_cas_version != request.expected_version
            or existing.action is not request.action
            or existing.feedback != request.feedback
            or existing.decided_by != decided_by
        ):
            raise PlanDecisionIdempotencyError(
                "decision_id was already used with different input"
            )

    def _is_decision_id_duplicate(self, error: IntegrityError) -> bool:
        original = error.orig
        if self._dialect.name == "sqlite":
            duplicate_codes = {
                sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
                sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            }
            if getattr(original, "sqlite_errorcode", None) not in duplicate_codes:
                return False
            message = str(original)
            return all(
                f"agent_plan_decisions.{column}" in message
                for column in (
                    "tenant_id",
                    "workspace_id",
                    "session_id",
                    "plan_id",
                    "decision_id",
                )
            )
        arguments = getattr(original, "args", ())
        if not arguments or arguments[0] != 1062:
            return False
        message = str(arguments[1] if len(arguments) > 1 else original).lower()
        if "duplicate entry" not in message or "for key" not in message:
            return False
        key_name = message.rsplit("for key", 1)[1].strip().strip("'`")
        return key_name in {"primary", "agent_plan_decisions.primary"}

    async def list_for_session(self) -> list[PlanSummary]:
        session_id = self._require_session()
        latest_runs = (
            select(
                agent_runs.c.tenant_id,
                agent_runs.c.workspace_id,
                agent_runs.c.session_id,
                agent_runs.c.plan_id,
                agent_runs.c.run_id,
                agent_runs.c.run_status,
                func.row_number()
                .over(
                    partition_by=(
                        agent_runs.c.tenant_id,
                        agent_runs.c.workspace_id,
                        agent_runs.c.session_id,
                        agent_runs.c.plan_id,
                    ),
                    order_by=(agent_runs.c.created_at.desc(), agent_runs.c.run_id.desc()),
                )
                .label("run_rank"),
            )
            .where(
                agent_runs.c.tenant_id == self._context.tenant_id,
                agent_runs.c.workspace_id == self._context.workspace_id,
                agent_runs.c.session_id == session_id,
                agent_runs.c.plan_id.is_not(None),
            )
            .subquery()
        )
        result = await self._conn.execute(
            select(
                agent_plans.c.id,
                agent_plans.c.session_id,
                agent_plans.c.status,
                agent_plans.c.current_version,
                agent_plans.c.approved_version,
                agent_plans.c.version,
                latest_runs.c.run_id.label("latest_run_id"),
                latest_runs.c.run_status.label("latest_run_status"),
            )
            .outerjoin(
                latest_runs,
                and_(
                    latest_runs.c.tenant_id == agent_plans.c.tenant_id,
                    latest_runs.c.workspace_id == agent_plans.c.workspace_id,
                    latest_runs.c.session_id == agent_plans.c.session_id,
                    latest_runs.c.plan_id == agent_plans.c.id,
                    latest_runs.c.run_rank == 1,
                ),
            )
            .where(self._plan_scope_predicate())
            .order_by(agent_plans.c.created_at.desc(), agent_plans.c.id.desc())
        )
        return [
            PlanSummary(
                plan_id=str(row["id"]),
                session_id=str(row["session_id"]),
                status=PlanStatus(str(row["status"])),
                current_version=int(row["current_version"]),
                approved_version=(
                    None if row["approved_version"] is None else int(row["approved_version"])
                ),
                aggregate_version=int(row["version"]),
                latest_run_id=(
                    None if row["latest_run_id"] is None else str(row["latest_run_id"])
                ),
                latest_run_status=(
                    None
                    if row["latest_run_status"] is None
                    else RunStatus(str(row["latest_run_status"]))
                ),
            )
            for row in result.mappings()
        ]

    async def latest_step_attempts(
        self,
        *,
        plan_id: str,
        plan_version: int,
        run_id: str,
        for_update: bool = False,
    ) -> Mapping[str, PlanStepRunRecord]:
        self._require_session()
        statement = (
            select(agent_plan_step_runs)
            .where(
                agent_plan_step_runs.c.tenant_id == self._context.tenant_id,
                agent_plan_step_runs.c.workspace_id == self._context.workspace_id,
                agent_plan_step_runs.c.session_id == self._require_session(),
                agent_plan_step_runs.c.plan_id == plan_id,
                agent_plan_step_runs.c.plan_version == plan_version,
                agent_plan_step_runs.c.run_id == run_id,
            )
            .order_by(
                agent_plan_step_runs.c.step_id,
                agent_plan_step_runs.c.attempt.desc(),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._conn.execute(statement)
        latest: dict[str, PlanStepRunRecord] = {}
        for row in result.mappings():
            step_id = str(row["step_id"])
            latest.setdefault(step_id, self._hydrate_step_run(row))
        return latest

    async def step_attempts(
        self,
        *,
        plan_id: str,
        plan_version: int,
        run_id: str,
        step_id: str,
        for_update: bool = False,
    ) -> tuple[PlanStepRunRecord, ...]:
        self._require_session()
        statement = (
            select(agent_plan_step_runs)
            .where(
                self._step_run_scope_predicate(run_id),
                agent_plan_step_runs.c.plan_id == plan_id,
                agent_plan_step_runs.c.plan_version == plan_version,
                agent_plan_step_runs.c.step_id == step_id,
            )
            .order_by(agent_plan_step_runs.c.attempt)
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._conn.execute(statement)
        return tuple(self._hydrate_step_run(row) for row in result.mappings())

    async def running_step_attempts(
        self,
        *,
        run_id: str,
        for_update: bool = False,
    ) -> tuple[PlanStepRunRecord, ...]:
        self._require_session()
        statement = (
            select(agent_plan_step_runs)
            .where(
                self._step_run_scope_predicate(run_id),
                agent_plan_step_runs.c.status == PlanStepRunStatus.RUNNING.value,
            )
            .order_by(
                agent_plan_step_runs.c.plan_version,
                agent_plan_step_runs.c.step_id,
                agent_plan_step_runs.c.attempt,
                agent_plan_step_runs.c.step_run_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._conn.execute(statement)
        return tuple(self._hydrate_step_run(row) for row in result.mappings())

    async def has_current_lease(self, lease: RunLease) -> bool:
        if not self._lease_matches_context(lease):
            return False
        result = await self._conn.execute(
            select(agent_runs.c.run_id)
            .where(current_lease_predicate(lease, self._dialect))
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def lock_run_for_execution(self, lease: RunLease) -> RowMapping:
        self._require_session()
        if not self._lease_matches_context(lease):
            raise StaleFenceError("run lease scope is stale")
        await self._dialect.lock_run(self._conn, lease.context)
        return await self._require_current_executable_lease(lease)

    async def _require_current_executable_lease(self, lease: RunLease) -> RowMapping:
        if not self._lease_matches_context(lease):
            raise StaleFenceError("run lease scope is stale")
        if not await self.has_current_lease(lease):
            raise StaleFenceError("run lease is stale")
        result = await self._conn.execute(
            select(
                agent_runs.c.plan_id,
                agent_runs.c.active_plan_version,
                agent_runs.c.run_status,
                agent_runs.c.cancel_requested_at,
            )
            .where(
                agent_runs.c.tenant_id == self._context.tenant_id,
                agent_runs.c.workspace_id == self._context.workspace_id,
                agent_runs.c.session_id == self._require_session(),
                agent_runs.c.run_id == lease.context.run_id,
            )
            .with_for_update()
        )
        run = result.mappings().first()
        if run is None:
            raise StaleFenceError("run lease is stale")
        if not is_plan_run_executable(
            RunStatus(str(run["run_status"])),
            None
            if run["cancel_requested_at"] is None
            else int(run["cancel_requested_at"]),
        ):
            raise PlanExecutionBlocked("Plan execution run is not executable")
        return run

    async def _require_active_plan_version(
        self,
        run: RowMapping,
        *,
        plan_id: str,
        plan_version: int,
    ) -> None:
        result = await self._conn.execute(
            select(
                agent_plans.c.status,
                agent_plans.c.current_version,
                agent_plans.c.approved_version,
            )
            .where(self._plan_predicate(plan_id))
            .with_for_update()
        )
        plan = result.mappings().first()
        if (
            plan is None
            or str(run["plan_id"]) != plan_id
            or PlanStatus(str(plan["status"])) is not PlanStatus.APPROVED
            or int(plan["current_version"]) != plan_version
            or plan["approved_version"] is None
            or int(plan["approved_version"]) != plan_version
            or run["active_plan_version"] is None
            or int(run["active_plan_version"]) != plan_version
        ):
            raise PlanExecutionBlocked(
                "Plan execution requires an approved current active version"
            )

    async def _require_step_ready(
        self,
        *,
        run_id: str,
        plan_id: str,
        plan_version: int,
        step_id: str,
    ) -> tuple[int, int]:
        step_result = await self._conn.execute(
            select(agent_plan_steps.c.max_attempts)
            .where(
                agent_plan_steps.c.tenant_id == self._context.tenant_id,
                agent_plan_steps.c.workspace_id == self._context.workspace_id,
                agent_plan_steps.c.session_id == self._require_session(),
                agent_plan_steps.c.plan_id == plan_id,
                agent_plan_steps.c.plan_version == plan_version,
                agent_plan_steps.c.step_id == step_id,
            )
            .with_for_update()
        )
        raw_step_limit = step_result.scalar_one_or_none()
        if raw_step_limit is None:
            raise PlanExecutionBlocked("Plan step does not belong to the active version")

        latest = await self.latest_step_attempts(
            plan_id=plan_id,
            plan_version=plan_version,
            run_id=run_id,
            for_update=True,
        )
        if any(
            attempt.status is PlanStepRunStatus.RUNNING
            for attempt in latest.values()
        ):
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

        dependency_result = await self._conn.execute(
            select(agent_plan_step_dependencies.c.depends_on_step_id)
            .where(
                self._dependency_scope_predicate(plan_id),
                agent_plan_step_dependencies.c.plan_version == plan_version,
                agent_plan_step_dependencies.c.step_id == step_id,
            )
            .order_by(agent_plan_step_dependencies.c.depends_on_step_id)
            .with_for_update()
        )
        dependency_ids = tuple(str(value) for value in dependency_result.scalars())
        if not is_plan_step_ready(step_id, dependency_ids, latest):
            raise PlanExecutionBlocked("Plan step is no longer ready")

        current = latest.get(step_id)
        attempt = (0 if current is None else current.attempt) + 1
        step_limit = self._positive_attempt_limit(
            raw_step_limit,
            "Plan step max_attempts",
        )
        tenant_limit = self._positive_attempt_limit(
            self._settings.max_step_attempts,
            "planning.max_step_attempts",
        )
        attempt_limit = min(step_limit, tenant_limit)
        if attempt > attempt_limit:
            raise PlanAttemptLimitError("Plan step attempt limit exceeded")
        return attempt, attempt_limit

    async def create_step_attempt(
        self,
        lease: RunLease,
        *,
        plan_id: str,
        plan_version: int,
        step_id: str,
    ) -> PlanStepRunRecord:
        self._require_session()
        if not self._lease_matches_context(lease):
            raise StaleFenceError("run lease scope is stale")

        run = await self.lock_run_for_execution(lease)
        await self._require_no_running_attempt(str(lease.context.run_id))
        await self._require_active_plan_version(
            run,
            plan_id=plan_id,
            plan_version=plan_version,
        )
        attempt, attempt_limit = await self._require_step_ready(
            run_id=str(lease.context.run_id),
            plan_id=plan_id,
            plan_version=plan_version,
            step_id=step_id,
        )

        step_run_id = str(uuid4())
        started_at = await self._db_now_ms()
        savepoint = await self._conn.begin_nested()
        try:
            # The run lock serializes competing writers. This final read keeps the
            # durable fence and readiness adjacent to the fact insert they authorize.
            attempt, attempt_limit = await self._require_step_ready(
                run_id=str(lease.context.run_id),
                plan_id=plan_id,
                plan_version=plan_version,
                step_id=step_id,
            )
            run = await self._require_current_executable_lease(lease)
            await self._require_active_plan_version(
                run,
                plan_id=plan_id,
                plan_version=plan_version,
            )
            await self._require_no_running_attempt(str(lease.context.run_id))
            await self._conn.execute(
                insert(agent_plan_step_runs).values(
                    **self._scope_values(),
                    plan_id=plan_id,
                    plan_version=plan_version,
                    step_id=step_id,
                    step_run_id=step_run_id,
                    run_id=lease.context.run_id,
                    attempt=attempt,
                    status=PlanStepRunStatus.RUNNING.value,
                    result_summary=None,
                    result_ref=None,
                    result_digest=None,
                    error_code=None,
                    error_detail_redacted=None,
                    reused_from_step_run_id=None,
                    version=1,
                    started_at=started_at,
                    finished_at=None,
                )
            )
            inserted = await self._get_step_run(
                run_id=str(lease.context.run_id),
                step_run_id=step_run_id,
            )
            if inserted is None:
                raise RuntimeError("Plan step attempt missing after insert")
            await savepoint.commit()
            return inserted
        except IntegrityError as primary:
            if not await self._rollback_savepoint(savepoint, primary):
                raise
            if await self._has_running_attempt(str(lease.context.run_id)):
                raise PlanStepAlreadyRunningError(
                    "Plan run already has a running step attempt"
                ) from primary
            latest_attempt = await self._latest_attempt_number(
                run_id=str(lease.context.run_id),
                step_id=step_id,
            )
            if latest_attempt >= attempt_limit:
                raise PlanAttemptLimitError("Plan step attempt limit exceeded") from primary
            raise

    async def step_run_by_id(
        self,
        *,
        run_id: str,
        step_run_id: str,
        for_update: bool = False,
    ) -> PlanStepRunRecord | None:
        """Read one scoped attempt; callers that prove reuse keep it locked."""
        query = select(agent_plan_step_runs).where(
            self._step_run_scope_predicate(run_id),
            agent_plan_step_runs.c.step_run_id == step_run_id,
        )
        if for_update:
            query = query.with_for_update()
        result = await self._conn.execute(query)
        row = result.mappings().first()
        return None if row is None else self._hydrate_step_run(row)

    async def create_reused_step_attempt(
        self,
        lease: RunLease,
        *,
        plan_id: str,
        plan_version: int,
        step_id: str,
        source_step_run_id: str,
    ) -> PlanStepRunRecord:
        """Persist a new, explicit succeeded fact referencing a locked source fact.

        The source document remains immutable.  Its result reference intentionally
        remains the evidence reference; execution validates the explicit lineage
        before accepting that document for a revised row.
        """
        self._require_session()
        if not self._lease_matches_context(lease):
            raise StaleFenceError("run lease scope is stale")
        run = await self.lock_run_for_execution(lease)
        await self._require_no_running_attempt(str(lease.context.run_id))
        await self._require_active_plan_version(
            run, plan_id=plan_id, plan_version=plan_version
        )
        source = await self.step_run_by_id(
            run_id=str(lease.context.run_id),
            step_run_id=source_step_run_id,
            for_update=True,
        )
        if (
            source is None
            or source.plan_id != plan_id
            or source.run_id != str(lease.context.run_id)
            or source.status is not PlanStepRunStatus.SUCCEEDED
            or source.reused_from_step_run_id is not None
            or source.result_ref is None
            or source.result_digest is None
            or source.result_summary is None
        ):
            raise PlanExecutionBlocked("Plan reuse source is not a complete succeeded attempt")
        attempt, _ = await self._require_step_ready(
            run_id=str(lease.context.run_id),
            plan_id=plan_id,
            plan_version=plan_version,
            step_id=step_id,
        )
        now = await self._db_now_ms()
        step_run_id = str(uuid4())
        savepoint = await self._conn.begin_nested()
        try:
            # Keep the final proof and insert adjacent under the run lock.
            run = await self._require_current_executable_lease(lease)
            await self._require_active_plan_version(
                run, plan_id=plan_id, plan_version=plan_version
            )
            await self._require_no_running_attempt(str(lease.context.run_id))
            source = await self.step_run_by_id(
                run_id=str(lease.context.run_id),
                step_run_id=source_step_run_id,
                for_update=True,
            )
            if (
                source is None
                or source.status is not PlanStepRunStatus.SUCCEEDED
                or source.reused_from_step_run_id is not None
                or source.result_ref is None
                or source.result_digest is None
                or source.result_summary is None
            ):
                raise PlanExecutionBlocked("Plan reuse source changed while locked")
            attempt, _ = await self._require_step_ready(
                run_id=str(lease.context.run_id),
                plan_id=plan_id,
                plan_version=plan_version,
                step_id=step_id,
            )
            await self._conn.execute(
                insert(agent_plan_step_runs).values(
                    **self._scope_values(),
                    plan_id=plan_id,
                    plan_version=plan_version,
                    step_id=step_id,
                    step_run_id=step_run_id,
                    run_id=lease.context.run_id,
                    attempt=attempt,
                    status=PlanStepRunStatus.SUCCEEDED.value,
                    result_summary=source.result_summary,
                    result_ref=source.result_ref,
                    result_digest=source.result_digest,
                    error_code=None,
                    error_detail_redacted=None,
                    reused_from_step_run_id=source.step_run_id,
                    version=2,
                    started_at=now,
                    finished_at=now,
                )
            )
            inserted = await self.step_run_by_id(
                run_id=str(lease.context.run_id),
                step_run_id=step_run_id,
                for_update=True,
            )
            if inserted is None:
                raise RuntimeError("Reused Plan step attempt missing after insert")
            await savepoint.commit()
            return inserted
        except IntegrityError as primary:
            if not await self._rollback_savepoint(savepoint, primary):
                raise
            existing = await self.latest_step_attempts(
                plan_id=plan_id,
                plan_version=plan_version,
                run_id=str(lease.context.run_id),
                for_update=True,
            )
            current = existing.get(step_id)
            if (
                current is not None
                and current.status is PlanStepRunStatus.SUCCEEDED
                and current.reused_from_step_run_id == source_step_run_id
            ):
                return current
            raise
        except BaseException as primary:
            await self._rollback_savepoint(savepoint, primary)
            raise

    async def finish_step_attempt(
        self,
        lease: RunLease,
        *,
        step_run_id: str,
        expected_version: int,
        status: PlanStepRunStatus,
        result: PlanStepResultDocument,
        result_ref: str,
        error_code: str | None = None,
        error_detail_redacted: str | None = None,
    ) -> PlanStepRunRecord:
        if not self._lease_matches_context(lease):
            raise StaleFenceError("run lease scope is stale")
        if status not in {
            PlanStepRunStatus.SUCCEEDED,
            PlanStepRunStatus.FAILED_RETRYABLE,
            PlanStepRunStatus.FAILED_TERMINAL,
        }:
            raise ValueError("Plan step attempt target status is not terminal")
        if not result_ref.startswith("memory:") or not result_ref.removeprefix(
            "memory:"
        ):
            raise ValueError("Plan step result reference is invalid")
        expected_result_status = (
            "succeeded" if status is PlanStepRunStatus.SUCCEEDED else "failed"
        )
        if result.status != expected_result_status:
            raise ValueError("Plan step result status does not match attempt status")
        if status is PlanStepRunStatus.SUCCEEDED:
            if error_code is not None or error_detail_redacted is not None:
                raise ValueError("Succeeded Plan step attempts cannot carry errors")
        elif not error_code or not error_detail_redacted:
            raise ValueError("Failed Plan step attempts require redacted error detail")

        await self._require_current_executable_lease(lease)
        current_result = await self._conn.execute(
            select(agent_plan_step_runs)
            .where(
                self._step_run_scope_predicate(str(lease.context.run_id)),
                agent_plan_step_runs.c.step_run_id == step_run_id,
            )
            .with_for_update()
        )
        row = current_result.mappings().first()
        if row is None:
            raise PlanExecutionBlocked("Plan step attempt does not exist")
        current = self._hydrate_step_run(row)
        if current.status is not PlanStepRunStatus.RUNNING:
            raise PlanExecutionBlocked("Plan step attempt is no longer running")
        if current.version != expected_version:
            raise VersionConflictError("Plan step attempt version conflict")
        if (
            result.plan_id != current.plan_id
            or result.plan_version != current.plan_version
            or result.run_id != current.run_id
            or result.step_id != current.step_id
            or result.step_run_id != current.step_run_id
            or result.attempt != current.attempt
        ):
            raise ValueError("Plan step result identity does not match attempt")

        definition_result = await self._conn.execute(
            select(agent_plan_steps.c.definition_digest).where(
                agent_plan_steps.c.tenant_id == self._context.tenant_id,
                agent_plan_steps.c.workspace_id == self._context.workspace_id,
                agent_plan_steps.c.session_id == self._require_session(),
                agent_plan_steps.c.plan_id == current.plan_id,
                agent_plan_steps.c.plan_version == current.plan_version,
                agent_plan_steps.c.step_id == current.step_id,
            )
        )
        definition_digest = definition_result.scalar_one_or_none()
        if definition_digest is None or result.definition_digest != str(
            definition_digest
        ):
            raise ValueError("Plan step result definition proof is invalid")

        finished_at = await self._db_now_ms()
        updated = await self._conn.execute(
            update(agent_plan_step_runs)
            .where(
                self._step_run_scope_predicate(str(lease.context.run_id)),
                agent_plan_step_runs.c.step_run_id == step_run_id,
                agent_plan_step_runs.c.status == PlanStepRunStatus.RUNNING.value,
                agent_plan_step_runs.c.version == expected_version,
            )
            .values(
                status=status.value,
                result_summary=result.summary,
                result_ref=result_ref,
                result_digest=result.digest(),
                error_code=error_code,
                error_detail_redacted=error_detail_redacted,
                version=expected_version + 1,
                finished_at=finished_at,
            )
        )
        if int(updated.rowcount or 0) != 1:
            raise VersionConflictError("Plan step attempt version conflict")
        finished = await self._get_step_run(
            run_id=str(lease.context.run_id),
            step_run_id=step_run_id,
        )
        if finished is None:
            raise RuntimeError("Plan step attempt missing after finish")
        return finished

    async def _insert_version(
        self,
        *,
        plan_id: str,
        plan_version: int,
        validated: ValidatedPlanDraft,
        parent_version: int | None,
        revision_feedback: str | None,
        supersedes: Mapping[str, str],
        created_at: int,
    ) -> None:
        self._require_session()
        scope = self._scope_values()
        await self._conn.execute(
            insert(agent_plan_versions).values(
                **scope,
                plan_id=plan_id,
                plan_version=plan_version,
                objective=validated.objective,
                constraints_json=json.dumps(
                    list(validated.constraints),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                generation_reason=validated.generation_reason,
                parent_version=parent_version,
                revision_feedback=revision_feedback,
                schema_version=1,
                content_digest=plan_content_digest(validated),
                created_at=created_at,
            )
        )

        step_ids = {step.logical_step_key: str(uuid4()) for step in validated.steps}
        if validated.steps:
            await self._conn.execute(
                insert(agent_plan_steps),
                [
                    {
                        **scope,
                        "plan_id": plan_id,
                        "plan_version": plan_version,
                        "step_id": step_ids[step.logical_step_key],
                        "logical_step_key": step.logical_step_key,
                        "supersedes_step_id": supersedes.get(step.logical_step_key),
                        "ordinal": step.ordinal,
                        "title": step.title,
                        "description": step.description,
                        "expected_outcome": step.expected_outcome,
                        "assigned_agent_profile_id": None,
                        "max_attempts": step.max_attempts,
                        "definition_digest": step_definition_digest(step),
                    }
                    for step in validated.steps
                ],
            )

        dependency_values = [
            {
                **scope,
                "plan_id": plan_id,
                "plan_version": plan_version,
                "step_id": step_ids[step.logical_step_key],
                "depends_on_step_id": step_ids[dependency],
            }
            for step in validated.steps
            for dependency in step.depends_on
        ]
        if dependency_values:
            await self._conn.execute(
                insert(agent_plan_step_dependencies),
                dependency_values,
            )

    async def _validate_supersedes(
        self,
        *,
        plan_id: str,
        plan_version: int,
        validated: ValidatedPlanDraft,
        supersedes: Mapping[str, str],
    ) -> None:
        valid_keys = {step.logical_step_key for step in validated.steps}
        if not set(supersedes).issubset(valid_keys):
            raise ValueError("supersedes contains an unknown logical_step_key")
        referenced_ids = set(supersedes.values())
        if not referenced_ids:
            return
        result = await self._conn.execute(
            select(agent_plan_steps.c.step_id).where(
                self._step_scope_predicate(plan_id),
                agent_plan_steps.c.plan_version < plan_version,
                agent_plan_steps.c.step_id.in_(referenced_ids),
            )
        )
        found_ids = {str(step_id) for step_id in result.scalars().all()}
        if found_ids != referenced_ids:
            raise ValueError(
                "Every supersedes_step_id must belong to an earlier version of the scoped Plan"
            )

    @staticmethod
    async def _rollback_savepoint(
        savepoint: AsyncTransaction,
        primary: BaseException,
    ) -> bool:
        if not savepoint.is_active:
            return True
        try:
            await savepoint.rollback()
        except BaseException as error:  # noqa: BLE001 - cleanup must not replace the primary
            _note_cleanup_error(primary, "savepoint rollback", error)
            return False
        return True

    async def _db_now_ms(self) -> int:
        result = await self._conn.execute(select(self._dialect.db_now_ms()))
        return int(result.scalar_one())

    def _scope_values(self) -> dict[str, str]:
        return {
            "tenant_id": self._context.tenant_id,
            "workspace_id": self._context.workspace_id,
            "session_id": self._require_session(),
        }

    def _step_run_scope_predicate(self, run_id: str):
        return and_(
            agent_plan_step_runs.c.tenant_id == self._context.tenant_id,
            agent_plan_step_runs.c.workspace_id == self._context.workspace_id,
            agent_plan_step_runs.c.session_id == self._require_session(),
            agent_plan_step_runs.c.run_id == run_id,
        )

    def _lease_matches_context(self, lease: RunLease) -> bool:
        return (
            lease.context.tenant_id == self._context.tenant_id
            and lease.context.workspace_id == self._context.workspace_id
            and lease.context.session_id == self._require_session()
            and lease.context.run_id is not None
            and self._context.run_id in (None, lease.context.run_id)
        )

    @staticmethod
    def _positive_attempt_limit(value: object, label: str) -> int:
        if type(value) is not int or value < 1:  # bool is not a valid integer limit
            raise ValueError(f"{label} must be a positive integer")
        return value

    async def _get_step_run(
        self,
        *,
        run_id: str,
        step_run_id: str,
    ) -> PlanStepRunRecord | None:
        result = await self._conn.execute(
            select(agent_plan_step_runs)
            .where(
                self._step_run_scope_predicate(run_id),
                agent_plan_step_runs.c.step_run_id == step_run_id,
            )
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else self._hydrate_step_run(row)

    async def _has_running_attempt(
        self,
        run_id: str,
        *,
        for_update: bool = False,
    ) -> bool:
        statement = (
            select(agent_plan_step_runs.c.step_run_id)
            .where(
                self._step_run_scope_predicate(run_id),
                agent_plan_step_runs.c.status == PlanStepRunStatus.RUNNING.value,
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._conn.execute(statement)
        return result.scalar_one_or_none() is not None

    async def _require_no_running_attempt(self, run_id: str) -> None:
        if await self._has_running_attempt(run_id, for_update=True):
            raise PlanStepAlreadyRunningError(
                "Plan run already has a running step attempt"
            )

    async def _latest_attempt_number(self, *, run_id: str, step_id: str) -> int:
        result = await self._conn.execute(
            select(func.max(agent_plan_step_runs.c.attempt)).where(
                self._step_run_scope_predicate(run_id),
                agent_plan_step_runs.c.step_id == step_id,
            )
        )
        value = result.scalar_one_or_none()
        return 0 if value is None else int(value)

    def _revision_claim_key(self, plan_id: str) -> tuple[str, str]:
        return self._require_session(), plan_id

    def _register_revision_claim(
        self,
        plan_id: str,
        decision: PlanDecisionRecord,
    ) -> None:
        key = self._revision_claim_key(plan_id)
        existing = self._transaction_state.pending_revisions.get(key)
        if existing is not None and existing != decision:
            raise PlanDecisionIdempotencyError(
                "Plan already has a different revision decision claim"
            )
        self._transaction_state.pending_revisions[key] = decision

    def _pending_revision_claim(
        self,
        plan_id: str,
    ) -> PlanDecisionRecord | None:
        return self._transaction_state.pending_revisions.get(
            self._revision_claim_key(plan_id)
        )

    def _clear_revision_claim(self, plan_id: str, decision_id: str) -> None:
        key = self._revision_claim_key(plan_id)
        claim = self._transaction_state.pending_revisions.get(key)
        if claim is not None and claim.decision_id == decision_id:
            del self._transaction_state.pending_revisions[key]

    def _plan_scope_predicate(self):
        return and_(
            agent_plans.c.tenant_id == self._context.tenant_id,
            agent_plans.c.workspace_id == self._context.workspace_id,
            agent_plans.c.session_id == self._require_session(),
        )

    def _plan_predicate(self, plan_id: str):
        return and_(self._plan_scope_predicate(), agent_plans.c.id == plan_id)

    def _version_scope_predicate(self, plan_id: str):
        return and_(
            agent_plan_versions.c.tenant_id == self._context.tenant_id,
            agent_plan_versions.c.workspace_id == self._context.workspace_id,
            agent_plan_versions.c.session_id == self._require_session(),
            agent_plan_versions.c.plan_id == plan_id,
        )

    def _step_scope_predicate(self, plan_id: str):
        return and_(
            agent_plan_steps.c.tenant_id == self._context.tenant_id,
            agent_plan_steps.c.workspace_id == self._context.workspace_id,
            agent_plan_steps.c.session_id == self._require_session(),
            agent_plan_steps.c.plan_id == plan_id,
        )

    def _dependency_scope_predicate(self, plan_id: str):
        return and_(
            agent_plan_step_dependencies.c.tenant_id == self._context.tenant_id,
            agent_plan_step_dependencies.c.workspace_id == self._context.workspace_id,
            agent_plan_step_dependencies.c.session_id == self._require_session(),
            agent_plan_step_dependencies.c.plan_id == plan_id,
        )

    def _decision_scope_predicate(self, plan_id: str):
        return and_(
            agent_plan_decisions.c.tenant_id == self._context.tenant_id,
            agent_plan_decisions.c.workspace_id == self._context.workspace_id,
            agent_plan_decisions.c.session_id == self._require_session(),
            agent_plan_decisions.c.plan_id == plan_id,
        )

    @staticmethod
    def _hydrate_step(row: RowMapping) -> PlanStepRecord:
        return PlanStepRecord(
            step_id=str(row["step_id"]),
            logical_step_key=str(row["logical_step_key"]),
            supersedes_step_id=(
                None if row["supersedes_step_id"] is None else str(row["supersedes_step_id"])
            ),
            ordinal=int(row["ordinal"]),
            title=str(row["title"]),
            description=str(row["description"]),
            expected_outcome=str(row["expected_outcome"]),
            assigned_agent_profile_id=(
                None
                if row["assigned_agent_profile_id"] is None
                else str(row["assigned_agent_profile_id"])
            ),
            max_attempts=int(row["max_attempts"]),
            definition_digest=str(row["definition_digest"]),
        )

    def _hydrate_version(
        self,
        row: RowMapping,
        *,
        steps: tuple[PlanStepRecord, ...],
        dependencies: Mapping[str, tuple[str, ...]],
    ) -> PlanVersionRecord:
        raw_constraints = row["constraints_json"]
        try:
            constraints = json.loads(
                raw_constraints
                if isinstance(raw_constraints, (str, bytes, bytearray))
                else str(raw_constraints)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("Plan constraints_json is corrupt") from error
        if not isinstance(constraints, list) or any(
            not isinstance(constraint, str) for constraint in constraints
        ):
            raise ValueError("Plan constraints_json must contain a list of strings")
        version = PlanVersionRecord(
            plan_id=str(row["plan_id"]),
            plan_version=int(row["plan_version"]),
            objective=str(row["objective"]),
            constraints=tuple(constraints),
            generation_reason=str(row["generation_reason"]),
            parent_version=(
                None if row["parent_version"] is None else int(row["parent_version"])
            ),
            revision_feedback=(
                None if row["revision_feedback"] is None else str(row["revision_feedback"])
            ),
            schema_version=int(row["schema_version"]),
            content_digest=str(row["content_digest"]),
            created_at=int(row["created_at"]),
            steps=steps,
            dependencies=MappingProxyType(dict(dependencies)),
        )
        self._validate_version_integrity(version)
        return version

    @staticmethod
    def _validate_version_integrity(version: PlanVersionRecord) -> None:
        if version.schema_version != 1:
            raise ValueError("Plan version schema is corrupt")
        expected_ordinals = tuple(range(1, len(version.steps) + 1))
        if tuple(step.ordinal for step in version.steps) != expected_ordinals:
            raise ValueError("Plan step ordinals are corrupt")

        steps_by_id = {step.step_id: step for step in version.steps}
        if len(steps_by_id) != len(version.steps):
            raise ValueError("Plan step identifiers are corrupt")
        if any(step_id not in steps_by_id for step_id in version.dependencies):
            raise ValueError("Plan dependency targets are corrupt")
        if any(
            dependency_id not in steps_by_id
            for dependencies in version.dependencies.values()
            for dependency_id in dependencies
        ):
            raise ValueError("Plan dependency sources are corrupt")

        try:
            draft = PlanDraft(
                objective=version.objective,
                constraints=list(version.constraints),
                generation_reason=version.generation_reason,
                steps=[
                    PlanDraftStep(
                        logical_step_key=step.logical_step_key,
                        title=step.title,
                        description=step.description,
                        expected_outcome=step.expected_outcome,
                        depends_on=[
                            steps_by_id[dependency_id].logical_step_key
                            for dependency_id in version.dependencies.get(step.step_id, ())
                        ],
                        max_attempts=step.max_attempts,
                    )
                    for step in version.steps
                ],
            )
            validated = validate_plan_draft(
                draft,
                max_steps=20,
                max_depth=10,
                max_attempts=20,
            )
        except (KeyError, ValueError) as error:
            raise ValueError("Plan version content is corrupt") from error

        if tuple(
            (step.logical_step_key, step.ordinal) for step in validated.steps
        ) != tuple(
            (step.logical_step_key, step.ordinal) for step in version.steps
        ):
            raise ValueError("Plan step ordering is corrupt")
        validated_by_key = {step.logical_step_key: step for step in validated.steps}
        for step in version.steps:
            if step_definition_digest(validated_by_key[step.logical_step_key]) != step.definition_digest:
                raise ValueError("Plan step definition_digest is corrupt")
        if plan_content_digest(validated) != version.content_digest:
            raise ValueError("Plan version content_digest is corrupt")

    @staticmethod
    def _hydrate_decision(row: RowMapping) -> PlanDecisionRecord:
        return PlanDecisionRecord(
            decision_id=str(row["decision_id"]),
            plan_version=int(row["plan_version"]),
            expected_plan_cas_version=int(row["expected_plan_cas_version"]),
            action=PlanDecisionAction(str(row["action"])),
            feedback=None if row["feedback"] is None else str(row["feedback"]),
            decided_by=str(row["decided_by"]),
            resulting_plan_version=(
                None
                if row["resulting_plan_version"] is None
                else int(row["resulting_plan_version"])
            ),
            created_at=int(row["created_at"]),
        )

    @staticmethod
    def _hydrate_step_run(row: RowMapping) -> PlanStepRunRecord:
        return PlanStepRunRecord(
            step_run_id=str(row["step_run_id"]),
            run_id=str(row["run_id"]),
            plan_id=str(row["plan_id"]),
            plan_version=int(row["plan_version"]),
            step_id=str(row["step_id"]),
            attempt=int(row["attempt"]),
            status=PlanStepRunStatus(str(row["status"])),
            result_summary=(
                None if row["result_summary"] is None else str(row["result_summary"])
            ),
            result_ref=None if row["result_ref"] is None else str(row["result_ref"]),
            result_digest=(
                None if row["result_digest"] is None else str(row["result_digest"])
            ),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            error_detail_redacted=(
                None
                if row["error_detail_redacted"] is None
                else str(row["error_detail_redacted"])
            ),
            reused_from_step_run_id=(
                None
                if row["reused_from_step_run_id"] is None
                else str(row["reused_from_step_run_id"])
            ),
            version=int(row["version"]),
            started_at=int(row["started_at"]),
            finished_at=None if row["finished_at"] is None else int(row["finished_at"]),
        )
