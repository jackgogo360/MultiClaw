from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from uuid import uuid4

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction

from multiclaw.config.settings import PlanningSettings
from multiclaw.planner.models import (
    PlanDecisionAction,
    PlanDecisionRecord,
    PlanDraft,
    PlanDraftStep,
    PlanSnapshot,
    PlanStatus,
    PlanStepRecord,
    PlanStepRunRecord,
    PlanStepRunStatus,
    PlanSummary,
    PlanTriggerMode,
    PlanVersionRecord,
    ValidatedPlanDraft,
)
from multiclaw.planner.validation import (
    plan_content_digest,
    step_definition_digest,
    validate_plan_draft,
)
from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect
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
from multiclaw.workflow.models import RunStatus

Dialect = SQLiteDialect | MySQLDialect


def _note_cleanup_error(primary: BaseException, phase: str, error: BaseException) -> None:
    primary.add_note(f"{phase} cleanup failed: {type(error).__name__}: {error}")


@dataclass(slots=True)
class PlanRepository:
    _conn: AsyncConnection
    _dialect: Dialect
    _context: TenantContext
    _settings: PlanningSettings

    @property
    def connection(self) -> AsyncConnection:
        return self._conn

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
        return PlanRepository(self._conn, self._dialect, context, self._settings)

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
            now = self._dialect.db_now_ms()
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
            await self._insert_version(
                plan_id=plan_id,
                plan_version=next_version,
                validated=validated,
                parent_version=parent_version,
                revision_feedback=revision_feedback,
                supersedes=supersedes,
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
                    updated_at=self._dialect.db_now_ms(),
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
    ) -> Mapping[str, PlanStepRunRecord]:
        self._require_session()
        result = await self._conn.execute(
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
        latest: dict[str, PlanStepRunRecord] = {}
        for row in result.mappings():
            step_id = str(row["step_id"])
            latest.setdefault(step_id, self._hydrate_step_run(row))
        return latest

    async def _insert_version(
        self,
        *,
        plan_id: str,
        plan_version: int,
        validated: ValidatedPlanDraft,
        parent_version: int | None,
        revision_feedback: str | None,
        supersedes: Mapping[str, str],
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
                created_at=self._dialect.db_now_ms(),
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
    ) -> None:
        if not savepoint.is_active:
            return
        try:
            await savepoint.rollback()
        except BaseException as error:  # noqa: BLE001 - cleanup must not replace the primary
            _note_cleanup_error(primary, "savepoint rollback", error)

    def _scope_values(self) -> dict[str, str]:
        return {
            "tenant_id": self._context.tenant_id,
            "workspace_id": self._context.workspace_id,
            "session_id": self._require_session(),
        }

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
