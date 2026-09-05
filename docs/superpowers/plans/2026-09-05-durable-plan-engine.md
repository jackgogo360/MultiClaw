# Durable Plan Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MultiClaw's advisory in-memory `plan:` response with a scoped, immutable, reviewable Plan aggregate that executes deterministically through the existing fenced workflow runtime, survives crashes, and renders as a durable inline Plan Card.

**Architecture:** The Plan domain owns classification, immutable versions, DAG definitions, review decisions, and step-attempt facts; `WorkflowCoordinator` remains the sole owner of run leases, fencing, checkpoints, tool approvals, recovery, and terminal transitions. Planning uses a dedicated structured LLM schema with no execution-tool registry, while approved plans execute one ready step at a time through the existing agent/tool loop. Plan data parts are post-commit notifications only; scoped Plan/Run APIs are the frontend source of truth after refresh, reconnect, or version gaps.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x Core async, Alembic, SQLite/MySQL, pytest/pytest-asyncio, React 19, TypeScript 6, assistant-ui data-part renderers, AI SDK SSE, Vite.

**Approved design:** `docs/superpowers/specs/2026-09-05-durable-plan-engine-design.md`

---

## Scope decision

This remains one implementation plan because the product must not expose a reviewable Plan that is not also durable, fenced, recoverable, deletable, and rehydratable. The work is split into commit-sized slices, but the deployment default must remain `planning.default_mode = "never"` until Task 20 proves the complete SQLite, MySQL, API, recovery, and frontend gate.

The first release deliberately includes:

- `planning_mode=auto|always|never`, with `plan:` normalized to `always`;
- immutable Plan versions and normalized DAG dependencies;
- approve, reject, and revision-feedback decisions;
- stable topological, strictly serial step execution;
- bounded step attempts, failure-driven replanning, and proof-based result reuse;
- durable cancellation and five required restart fault windows;
- scoped Plan/Run APIs and post-commit `data-plan-*` SSE parts;
- an inline assistant-ui Plan Card with hydration, history, decisions, rerun, and summary retry.

It does not add direct graph editing, step-level form editing, parallel execution, sub-agent delegation, a general workflow engine, a graph visualizer, or any new package dependency.

## Execution constraints

- Create an isolated worktree before source implementation; this plan document itself is the only change in the current planning branch.
- Follow TDD in every task: add the focused failing test, observe the named failure, add the minimum implementation, observe the pass, run the listed regression set, then commit.
- Preserve `WorkflowCoordinator` as the only mutation boundary for `agent_runs`, leases, fences, workflow checkpoints, tool approvals, and tool executions. Plan services call coordinator methods on the same SQLAlchemy connection; they never update `agent_runs` directly.
- A repository may accept IDs only after a complete `TenantContext.for_session(...)` or `.for_run(...)` has been constructed from authenticated identity. HTTP bodies and query strings provide `session_id`, never tenant or workspace IDs.
- Planning classification and generation receive no real tool registry. The only schema supplied is `classify_planning_request` or `submit_plan`; one malformed Plan response gets one repair call, and the second failure terminates without objective execution.
- Do not hold a database transaction open across classification or Plan generation. Persist the source user message first, perform side-effect-free model calls, then materialize Plan, run binding, `awaiting_user`, Plan checkpoint, and assistant Plan reference in one short transaction.
- Initial waiting runs persist `initial_plan_version=1` and `active_plan_version=1`; execution is still forbidden until `current_version == approved_version == active_plan_version` and Plan status is `approved`.
- Creating revision `N+1` never changes version `N` and leaves `agent_runs.active_plan_version` unchanged. Approving `N+1` updates `approved_version`, `active_plan_version`, and the run resume fence in one transaction.
- A step attempt mutation requires a current `RunLease` and the existing fence predicate. Before a new attempt, lock the run, prove there is no running attempt, and prove the current/approved/active Plan version equality.
- Persist `PlanStepCompletion` and dependency digests as a redacted `memory_entries.type="plan_step_result"` document. `agent_plan_step_runs.result_ref` points to that document; do not expand the approved Plan schema with an unreviewed result table.
- Decision, rerun, cancel, and summary-retry mutations return AI SDK-compatible SSE so their post-commit `data-plan-*` notifications and continued execution share one response. GET Plan/Run APIs remain authoritative.
- Existing tool approval behavior and recovery strategy remain unchanged inside a Plan step.
- Use database-clock timestamps, portable SQLAlchemy Core, explicit constraints, and file-backed SQLite for transaction/concurrency tests. Run the MySQL contract against MySQL `>=8.0.36` when `MULTICLAW_TEST_MYSQL_URL` is available.
- Keep metric labels bounded to `backend`, `operation`, `status`, `error_class`, `recovery_strategy`, `profile`; Plan/run/tenant/workspace/session/provider/path IDs belong only in redacted trace context.
- Every commit uses Lore trailers and records exact verification commands.

## File structure

### Create

- `src/multiclaw/planner/validation.py` — canonical Plan serialization, hard-limit validation, secret rejection, DAG validation, and stable topological order.
- `src/multiclaw/planner/policy.py` — deterministic explicit modes plus bounded automatic classification.
- `src/multiclaw/planner/generator.py` — structured Plan generation and the single repair attempt.
- `src/multiclaw/planner/service.py` — transactional materialization, decision CAS/idempotency, immutable revisions, and post-commit event descriptions.
- `src/multiclaw/planner/execution.py` — ready-step selection, fenced attempts, completion protocol, replanning, reuse, final summary, and recovery continuation.
- `src/multiclaw/storage/repositories/plans.py` — session-scoped Plan aggregate, version, decision, dependency, and attempt persistence.
- `src/multiclaw/api/plans.py` — scoped Plan list/read/decision/rerun routes.
- `src/multiclaw/api/runs.py` — scoped run read/cancel/summary-retry routes.
- `alembic/versions/20260905_0002_durable_plan_engine.py` — forward-only additive Plan migration for SQLite and MySQL.
- `tests/test_plan_repository.py` — immutability, scope, decision CAS/idempotency, concurrency, and deletion contracts.
- `tests/test_plan_execution.py` — ordering, serial attempts, completion, retry, replan, reuse, cancellation, and summaries.
- `tests/integration/test_plan_faults.py` — the five crash windows and corrupt/foreign/digest-mismatch recovery blocks.
- `frontend/src/lib/plan-store.ts` — version-aware authoritative Plan cache and action-stream consumer.
- `frontend/src/components/plan/PlanCard.tsx` — data-part renderer, status/progress shell, fetch/hydration, history, and action entry points.
- `frontend/src/components/plan/PlanStepList.tsx` — ordered step/dependency/attempt/result presentation.
- `frontend/src/components/plan/PlanDecisionControls.tsx` — approve, reject, revision feedback, conflict refresh, cancel, rerun, and summary retry controls.
- `docs/durable-plans.md` — operator behavior, configuration, recovery, rollout, and browser verification.

### Modify

- `src/multiclaw/config/settings.py:84-276` — add bounded `PlanningSettings` and register it on `Settings`.
- `multiclaw.toml:1-90` and `config/multiclaw.toml:1-90` — add rollout-safe planning examples without credentials.
- `src/multiclaw/planner/models.py:1-25` — replace mutable advisory models with durable domain and API contracts.
- `src/multiclaw/planner/planner.py:1-23` — remove string splitting; retain a narrow compatibility export only until callers migrate.
- `src/multiclaw/planner/__init__.py:1-20` — export the new stable planning contracts.
- `src/multiclaw/storage/schema.py:110-330` — add Plan tables, memory full-session uniqueness, and nullable Plan columns/FKs on `agent_runs`.
- `src/multiclaw/storage/uow.py:175-207` — bind `PlanRepository` and planning limits to the same scoped connection.
- `src/multiclaw/api/dependencies.py:33-50` — pass application planning limits into request UoWs.
- `src/multiclaw/storage/repositories/__init__.py:1-20` — export `PlanRepository`.
- `src/multiclaw/storage/repositories/workflow.py:51-330,953-1040` — bind Plan fields, guarded Plan-run resume/cancel, cancellation checks, and hydrated run fields.
- `src/multiclaw/workflow/models.py:49-236,354-366` — Plan checkpoint payloads/actions, cancellation transition, and Plan fields on `RunRecord`.
- `src/multiclaw/workflow/coordinator.py:56-202,314-399` — atomic Plan run start/pause/resume/cancel and Plan checkpoints on the existing connection.
- `src/multiclaw/workflow/recovery.py:97-216,447-930` — Plan-aware checkpoint validation, recovery classification, worker discovery, and runtime continuation.
- `src/multiclaw/workflow/continuation.py:1-340` — persist/reload structured Plan step result documents without changing tool-result semantics.
- `src/multiclaw/agent/multiclaw.py:80-117,229-687,695-876` — remove `plan:` short-circuits and expose the bounded internal Plan-step runner.
- `src/multiclaw/runtime/models.py:1-120` — expose Plan execution continuation on a tenant runtime.
- `src/multiclaw/runtime/factory.py:244-284` — assemble policy, generator, services, and executor with the tenant router and database.
- `src/multiclaw/api/chat.py:44-615` — accept planning mode, persist source message ID, split generation from transactions, materialize waiting Plan, and encode Plan parts.
- `src/multiclaw/api/sessions.py:27-164` — hydrate typed message metadata and session Plan summaries.
- `src/multiclaw/stream.py:9-155` — encode versioned Plan data parts.
- `src/multiclaw/server.py:121-142` — register Plan/Run routers and recovery wiring.
- `src/multiclaw/storage/repositories/sessions.py:137-184` — return message IDs/metadata and delete Plan rows leaf-to-root.
- `src/multiclaw/storage/repositories/deletions.py:614-669` — purge Plan rows leaf-to-root before runs/sessions.
- `src/multiclaw/observability.py:15-54` — exercise bounded Plan metrics without allowing ID labels.
- `tests/test_config.py` and `tests/test_planner.py:1-36` — replace advisory expectations with settings/domain/generation tests.
- `tests/test_workflow_state.py` and `tests/test_workflow_recovery.py` — Plan transitions and checkpoint payload coverage.
- `tests/test_migrations.py:18-126` and `tests/integration/test_mysql_contract.py:79-182` — revision/table/constraint parity.
- `tests/test_scoped_repositories.py:194-232`, `tests/test_deletion_worker.py:165-280`, and `tests/test_server.py:1-900` — session/account deletion, API, SSE, hydration, and compatibility coverage.
- `frontend/src/lib/api.ts:4-283` — Plan/Run DTOs and scoped mutation helpers.
- `frontend/src/App.tsx:29-295` — planning-mode request, Plan event routing, and store lifecycle.
- `frontend/src/components/session/SessionProvider.tsx:53-104` — hydrate persisted Plan parts and scoped Plan facts.
- `frontend/src/components/assistant-ui/thread.tsx:1-130` — register the `plan-created` data renderer without disturbing tool grouping.
- `frontend/src/index.css` — accessible Plan status, progress, controls, and responsive details.
- `src/multiclaw/static/**` — Vite-generated production bundle; update only through `npm run build`.
- `docs/configuration.md`, `docs/api.md`, `docs/architecture.md`, `docs/testing.md`, and `docs/README.md` — public contract, operations, and release evidence.
- `scripts/check_docs.py:186-226` — require the new planning configuration and Plan/Run route groups.
- `.github/workflows/ci.yml` — keep Plan fault windows visible in the existing SQLite/MySQL and frontend release matrix.

### Delete after replacement tests pass

- Delete the legacy `Planner` behavior from `src/multiclaw/planner/planner.py` and the string-splitting assertions from `tests/test_planner.py`. The module may remain as a compatibility import that re-exports `PlanGenerator`; no mutable in-memory `Plan` or `approve()` path remains.

## Planned public contracts

Keep these names and signatures stable across tasks:

```python
class PlanningMode(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class PlanningRoute(StrEnum):
    DIRECT = "direct"
    PLAN = "plan"


class PlanStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class PlanTriggerMode(StrEnum):
    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"


class PlanDecisionAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"


class PlanStepRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


class PlanningDecision(BaseModel):
    mode: PlanningRoute
    reason: str = Field(max_length=500)


class PlanDraftStep(BaseModel):
    logical_step_key: str
    title: str
    description: str
    expected_outcome: str
    depends_on: list[str] = Field(default_factory=list)
    max_attempts: int = 2


class PlanDraft(BaseModel):
    objective: str
    constraints: list[str] = Field(default_factory=list)
    generation_reason: str
    steps: list[PlanDraftStep]


class PlanStepCompletion(BaseModel):
    status: Literal["succeeded", "failed"]
    summary: str
    evidence: list[str] = Field(default_factory=list)
    retryable: bool = False


class PlanReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tenant_id: str
    workspace_id: str
    plan_id: str
    plan_version: int
    run_id: str
    aggregate_version: int
    session_id: str


@dataclass(frozen=True, slots=True)
class PlanDecisionRecord:
    decision_id: str
    plan_version: int
    expected_plan_cas_version: int
    action: PlanDecisionAction
    feedback: str | None
    decided_by: str
    resulting_plan_version: int | None
    created_at: int


@dataclass(frozen=True, slots=True)
class PlanStepRunRecord:
    step_run_id: str
    run_id: str
    plan_id: str
    plan_version: int
    step_id: str
    attempt: int
    status: PlanStepRunStatus
    result_summary: str | None
    result_ref: str | None
    result_digest: str | None
    error_code: str | None
    error_detail_redacted: str | None
    reused_from_step_run_id: str | None
    version: int
    started_at: int
    finished_at: int | None


class PlanStepResultDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    plan_id: str
    plan_version: int
    run_id: str
    step_id: str
    step_run_id: str
    attempt: int
    status: Literal["succeeded", "failed"]
    summary: str
    evidence: list[str]
    definition_digest: str
    dependency_result_digests: dict[str, str]
    tool_catalog_digest: str
    policy_digest: str
    skill_set_digest: str


@dataclass(frozen=True, slots=True)
class PlanMaterializationResult:
    plan: PlanSnapshot
    reference: PlanReference
    reference_message_id: str
    run: RunRecord
    event: ScopedEvent


@dataclass(frozen=True, slots=True)
class PlanDecisionMutationResult:
    snapshot: PlanSnapshot
    decision: PlanDecisionRecord
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class PlanDecisionResult:
    snapshot: PlanSnapshot
    decision: PlanDecisionRecord
    run: RunRecord
    lease: RunLease | None
    idempotent_replay: bool
    events: tuple[ScopedEvent, ...]


@dataclass(frozen=True, slots=True)
class PlanSummary:
    plan_id: str
    session_id: str
    status: PlanStatus
    current_version: int
    approved_version: int | None
    aggregate_version: int
    latest_run_id: str | None
    latest_run_status: RunStatus | None


@dataclass(frozen=True, slots=True)
class MaterializeInitialPlan:
    context: TenantContext
    runtime_instance_id: str
    source_message_id: str
    assistant_turn_index: int
    trigger_mode: PlanTriggerMode
    draft: PlanDraft


@dataclass(frozen=True, slots=True)
class FailureRevisionRequest:
    context: TenantContext
    lease: RunLease
    plan: PlanSnapshot
    failed_step_run: PlanStepRunRecord
    draft: PlanDraft


@dataclass(frozen=True, slots=True)
class PlanExecutionOutcome:
    state: Literal["awaiting_user", "completed", "cancelled", "failed_terminal"]
    plan: PlanSnapshot
    run: RunRecord
    assistant_content: str | None = None


@dataclass(frozen=True, slots=True)
class PlanStepExecutionRequest:
    context: TenantContext
    lease: RunLease
    plan: PlanSnapshot
    step: PlanStepRecord
    step_run: PlanStepRunRecord
    dependency_results: tuple[PlanStepResultDocument, ...]


class PlanStepRunner(Protocol):
    async def run_plan_step(
        self,
        request: PlanStepExecutionRequest,
        *,
        run_lease_handle: RunLeaseHandle,
        workflow_continuation: WorkflowContinuationService,
        recovered_tool_result: PersistedToolResult | None = None,
        recovered_tool_input_json: str | None = None,
    ) -> PlanStepCompletion: ...


class PlanRepository:
    def __init__(self, connection: AsyncConnection, dialect: Dialect, context: TenantContext, settings: PlanningSettings): ...
    async def create(self, *, plan_id: str, source_message_id: str, trigger_mode: PlanTriggerMode, draft: PlanDraft) -> PlanSnapshot: ...
    async def append_version(self, *, plan_id: str, expected_version: int, draft: PlanDraft, parent_version: int, revision_feedback: str | None, supersedes: Mapping[str, str]) -> PlanSnapshot: ...
    async def get(self, plan_id: str) -> PlanSnapshot | None: ...
    async def list_for_session(self) -> list[PlanSummary]: ...
    async def find_by_run(self, run_id: str) -> PlanSnapshot | None: ...
    async def record_decision(self, request: PlanDecisionRequest, *, decided_by: str) -> PlanDecisionMutationResult: ...
    async def create_step_attempt(self, lease: RunLease, *, plan_id: str, plan_version: int, step_id: str) -> PlanStepRunRecord: ...
    async def finish_step_attempt(self, lease: RunLease, *, step_run_id: str, expected_version: int, status: PlanStepRunStatus, result: PlanStepResultDocument | None, error_code: str | None, error_detail_redacted: str | None) -> PlanStepRunRecord: ...
    async def latest_step_attempts(self, *, plan_id: str, plan_version: int, run_id: str) -> Mapping[str, PlanStepRunRecord]: ...


class PlanningService:
    async def materialize_initial(self, request: MaterializeInitialPlan) -> PlanMaterializationResult: ...
    async def decide(self, request: PlanDecisionRequest, *, decided_by: str, runtime_instance_id: str) -> PlanDecisionResult: ...
    async def materialize_failure_revision(self, request: FailureRevisionRequest) -> PlanMaterializationResult: ...


class PlanExecutionCoordinator:
    async def execute(self, *, runtime: TenantRuntime, context: TenantContext, run_lease_handle: RunLeaseHandle) -> PlanExecutionOutcome: ...
    async def resume(self, *, runtime: TenantRuntime, context: TenantContext, run_lease_handle: RunLeaseHandle, recovery_outcome: RecoveryOutcome, recovered_tool_result: PersistedToolResult | None = None, recovered_tool_input_json: str | None = None) -> PlanExecutionOutcome: ...
```

HTTP scope contracts are also fixed:

```text
GET  /api/sessions/{session_id}/plans
GET  /api/plans/{plan_id}?session_id={session_id}
POST /api/plans/{plan_id}/decision        body includes session_id
POST /api/plans/{plan_id}/runs           body includes session_id
GET  /api/runs/{run_id}?session_id={session_id}
POST /api/runs/{run_id}/cancel            body includes session_id
POST /api/runs/{run_id}/summary/retry     body includes session_id
```

All mutations return `text/event-stream`. The first chunk is `data-run`; later chunks are scoped `data-plan-decision`, `data-plan-revised`, `data-plan-step-status`, or `data-plan-run-status`. The browser refetches on every aggregate-version gap and after every terminal event.

### Task 1: Lock bounded planning configuration and durable domain contracts

**Files:**
- Modify: `src/multiclaw/config/settings.py:84-276`
- Modify: `src/multiclaw/planner/models.py:1-25`
- Modify: `src/multiclaw/planner/__init__.py:1-20`
- Modify: `multiclaw.toml:1-90`
- Modify: `config/multiclaw.toml:1-90`
- Modify: `tests/test_config.py`
- Modify: `tests/test_planner.py:1-36`

- [ ] **Step 1: Replace advisory-model assertions with failing settings and domain tests**

Add these tests:

```python
import pytest
from pydantic import ValidationError

from multiclaw.config import Settings
from multiclaw.planner import (
    PlanDecisionAction,
    PlanDraft,
    PlanDraftStep,
    PlanningMode,
    PlanningRoute,
    PlanStatus,
    PlanStepCompletion,
    PlanStepRunStatus,
)


def test_planning_defaults_and_hard_caps() -> None:
    settings = Settings(_config_file="/nonexistent")

    assert settings.planning.enabled is True
    assert settings.planning.default_mode is PlanningMode.AUTO
    assert settings.planning.classification_model == ""
    assert settings.planning.generation_model == ""
    assert settings.planning.max_steps == 20
    assert settings.planning.max_dependency_depth == 10
    assert settings.planning.max_revisions == 5
    assert settings.planning.max_step_attempts == 2

    for payload in (
        {"max_steps": 21},
        {"max_dependency_depth": 11},
        {"max_revisions": 21},
        {"max_step_attempts": 21},
    ):
        with pytest.raises(ValidationError):
            Settings(_config_file="/nonexistent", planning=payload)


def test_durable_domain_values_are_stable() -> None:
    assert [item.value for item in PlanningMode] == ["auto", "always", "never"]
    assert [item.value for item in PlanningRoute] == ["direct", "plan"]
    assert PlanStatus.AWAITING_APPROVAL.value == "awaiting_approval"
    assert PlanDecisionAction.REVISE.value == "revise"
    assert PlanStepRunStatus.FAILED_TERMINAL.value == "failed_terminal"

    draft = PlanDraft(
        objective="Ship the durable plan engine",
        constraints=["No new dependencies"],
        generation_reason="The request spans storage and runtime work.",
        steps=[
            PlanDraftStep(
                logical_step_key="schema",
                title="Create schema",
                description="Persist immutable plan definitions.",
                expected_outcome="Schema constraints pass on both databases.",
                depends_on=[],
                max_attempts=2,
            )
        ],
    )
    completion = PlanStepCompletion(
        status="succeeded",
        summary="Schema checks passed.",
        evidence=["tests/test_migrations.py"],
    )

    assert draft.steps[0].logical_step_key == "schema"
    assert completion.retryable is False
```

- [ ] **Step 2: Run the tests and observe the missing-contract failure**

Run:

```bash
uv run pytest tests/test_config.py tests/test_planner.py -q
```

Expected: FAIL during collection because `PlanningMode`, `PlanDraft`, `PlanStepCompletion`, and `settings.planning` do not exist; the old mutable `Plan` tests no longer match the approved domain.

- [ ] **Step 3: Add the bounded settings and domain enums/models**

Add to `src/multiclaw/config/settings.py` and register `planning: PlanningSettings = Field(default_factory=PlanningSettings)` on `Settings`:

```python
class PlanningSettings(BaseModel):
    enabled: bool = True
    default_mode: PlanningMode = PlanningMode.AUTO
    classification_model: str = Field(default="", max_length=255)
    generation_model: str = Field(default="", max_length=255)
    max_steps: int = Field(default=20, ge=1, le=20, strict=True)
    max_dependency_depth: int = Field(default=10, ge=1, le=10, strict=True)
    max_revisions: int = Field(default=5, ge=0, le=20, strict=True)
    max_step_attempts: int = Field(default=2, ge=1, le=20, strict=True)
```

Define the enums and Pydantic models in `src/multiclaw/planner/models.py` exactly as shown in **Planned public contracts**, with these field bounds:

```python
LOGICAL_STEP_KEY = r"^[a-z0-9_-]{1,64}$"
ConstraintText = Annotated[str, Field(min_length=1, max_length=1_000)]
EvidenceText = Annotated[str, Field(min_length=1, max_length=2_000)]


class PlanDraftStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_step_key: str = Field(pattern=LOGICAL_STEP_KEY)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4_000)
    expected_outcome: str = Field(min_length=1, max_length=2_000)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    max_attempts: int = Field(default=2, ge=1, le=20, strict=True)


class PlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=16_000)
    constraints: list[ConstraintText] = Field(default_factory=list, max_length=20)
    generation_reason: str = Field(min_length=1, max_length=1_000)
    steps: list[PlanDraftStep] = Field(min_length=1, max_length=20)


class PlanStepCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "failed"]
    summary: str = Field(min_length=1, max_length=4_000)
    evidence: list[EvidenceText] = Field(default_factory=list, max_length=20)
    retryable: bool = False
```

Import `Annotated` from `typing`; use `StrEnum` for serialized enum values. Export only the durable names from `planner/__init__.py`; keep `Planner` exported temporarily until Task 15 removes the last caller.

Add `[planning]` to both example TOML files with `enabled=true`, empty model names, the approved limits, and `default_mode="never"`. This is the deployment kill switch; the code default remains `auto` and Task 20 changes the examples after the release gate.

- [ ] **Step 4: Run focused tests and verify the cap direction**

```bash
uv run pytest tests/test_config.py tests/test_planner.py -q
```

Expected: PASS. Values below the hard maxima validate, while any value above the design caps fails before runtime construction.

- [ ] **Step 5: Commit the explicit contract**

```bash
git add src/multiclaw/config/settings.py src/multiclaw/planner/models.py \
  src/multiclaw/planner/__init__.py multiclaw.toml config/multiclaw.toml \
  tests/test_config.py tests/test_planner.py
git commit -m "Make planning limits explicit before durable state exists" \
  -m "Replace the mutable advisory vocabulary with bounded domain types while keeping deployment examples on the rollout kill switch." \
  -m "Constraint: Configured limits may tighten but never exceed approved validation caps" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: uv run pytest tests/test_config.py tests/test_planner.py -q"
```

### Task 2: Validate Plan drafts, canonical digests, and stable DAG order

**Files:**
- Create: `src/multiclaw/planner/validation.py`
- Modify: `src/multiclaw/planner/models.py:1-220`
- Modify: `src/multiclaw/planner/__init__.py:1-40`
- Modify: `tests/test_planner.py`

- [ ] **Step 1: Add failing validation, secret, digest, and ordering tests**

```python
import pytest

from multiclaw.planner import PlanDraft, PlanDraftStep
from multiclaw.planner.validation import (
    PlanValidationError,
    canonical_plan_bytes,
    plan_content_digest,
    step_definition_digest,
    validate_plan_draft,
)


def _step(key: str, *, depends_on: list[str] | None = None, title: str | None = None):
    return PlanDraftStep(
        logical_step_key=key,
        title=title or key.title(),
        description=f"Execute {key}.",
        expected_outcome=f"{key} is verified.",
        depends_on=depends_on or [],
        max_attempts=2,
    )


def _draft(steps: list[PlanDraftStep]) -> PlanDraft:
    return PlanDraft(
        objective="Deliver a tested change",
        constraints=["Preserve behavior"],
        generation_reason="Multiple dependent actions are required.",
        steps=steps,
    )


def test_validation_returns_stable_topological_order() -> None:
    draft = _draft([_step("publish", depends_on=["test"]), _step("lint"), _step("test")])

    validated = validate_plan_draft(draft, max_steps=20, max_depth=10, max_attempts=2)

    assert [step.logical_step_key for step in validated.steps] == ["lint", "test", "publish"]
    assert [step.ordinal for step in validated.steps] == [1, 2, 3]


@pytest.mark.parametrize(
    "steps, message",
    [
        ([_step("a", depends_on=["missing"])], "missing dependency"),
        ([_step("a", depends_on=["a"])], "self dependency"),
        ([_step("a", depends_on=["b"]), _step("b", depends_on=["a"])], "cycle"),
        ([_step("a", depends_on=["b", "b"]), _step("b")], "duplicate dependency"),
        ([_step("a"), _step("a")], "duplicate logical_step_key"),
    ],
)
def test_validation_rejects_invalid_graphs(steps, message) -> None:
    with pytest.raises(PlanValidationError, match=message):
        validate_plan_draft(_draft(steps), max_steps=20, max_depth=10, max_attempts=2)


def test_validation_rejects_raw_credentials_and_oversized_canonical_content() -> None:
    credential = "Authorization: " + "Bearer live-token"
    secret = _draft([_step("inspect", title=f"Use {credential}")])
    with pytest.raises(PlanValidationError, match="credential-shaped"):
        validate_plan_draft(secret, max_steps=20, max_depth=10, max_attempts=2)

    oversized = _draft([_step(f"s{i}") for i in range(20)])
    oversized.steps[0].description = "x" * 4_000
    with pytest.raises(PlanValidationError, match="100 bytes"):
        validate_plan_draft(
            oversized,
            max_steps=20,
            max_depth=10,
            max_attempts=2,
            max_content_bytes=100,
        )


def test_canonical_digests_ignore_mapping_order_but_include_definition_changes() -> None:
    draft = _draft([_step("a"), _step("b", depends_on=["a"])])
    validated = validate_plan_draft(draft, max_steps=20, max_depth=10, max_attempts=2)

    assert canonical_plan_bytes(validated) == canonical_plan_bytes(validated.model_copy(deep=True))
    assert len(plan_content_digest(validated)) == 64
    before = step_definition_digest(validated.steps[0])
    changed = validated.steps[0].model_copy(update={"expected_outcome": "A different result."})
    assert step_definition_digest(changed) != before
```

- [ ] **Step 2: Run the validation slice and observe the missing module**

```bash
uv run pytest tests/test_planner.py -q
```

Expected: FAIL with `ModuleNotFoundError: multiclaw.planner.validation`.

- [ ] **Step 3: Implement canonicalization and Kahn ordering with hard fail-closed checks**

Create `validation.py` with this public shape and algorithm:

```python
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence

from multiclaw.planner.models import PlanDraft, PlanDraftStep, ValidatedPlanDraft, ValidatedPlanStep


MAX_PLAN_CONTENT_BYTES = 262_144
_SECRET_KEY = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+\S+|bearer\s+\S+|"
    r"(?:api[_-]?key|password|secret)\s*[:=]\s*\S+|\b(?:sk|ghp)[_-][A-Za-z0-9_-]+)"
)


class PlanValidationError(ValueError):
    pass


def _reject_credentials(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                raise PlanValidationError("Plan contains credential-shaped field")
            _reject_credentials(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_credentials(item)
        return
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        raise PlanValidationError("Plan contains credential-shaped content")


def sanitize_plan_text(value: str) -> str:
    return _SECRET_VALUE.sub("[REDACTED]", value)


def validate_plan_draft(
    draft: PlanDraft,
    *,
    max_steps: int,
    max_depth: int,
    max_attempts: int,
    max_content_bytes: int = MAX_PLAN_CONTENT_BYTES,
) -> ValidatedPlanDraft:
    if len(draft.steps) > min(max_steps, 20):
        raise PlanValidationError("Plan exceeds configured step limit")
    keys = [step.logical_step_key for step in draft.steps]
    if len(keys) != len(set(keys)):
        raise PlanValidationError("duplicate logical_step_key")
    by_key = {step.logical_step_key: step for step in draft.steps}
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {key: 0 for key in keys}
    depth = {key: 1 for key in keys}
    original = {key: index for index, key in enumerate(keys)}
    for step in draft.steps:
        if step.max_attempts > max_attempts:
            raise PlanValidationError("step max_attempts exceeds configured limit")
        if len(step.depends_on) != len(set(step.depends_on)):
            raise PlanValidationError("duplicate dependency")
        for dependency in step.depends_on:
            if dependency == step.logical_step_key:
                raise PlanValidationError("self dependency")
            if dependency not in by_key:
                raise PlanValidationError(f"missing dependency {dependency}")
            outgoing[dependency].append(step.logical_step_key)
            indegree[step.logical_step_key] += 1
    ready = sorted((key for key, count in indegree.items() if count == 0), key=original.get)
    ordered: list[str] = []
    while ready:
        key = ready.pop(0)
        ordered.append(key)
        for child in sorted(outgoing[key], key=original.get):
            depth[child] = max(depth[child], depth[key] + 1)
            if depth[child] > min(max_depth, 10):
                raise PlanValidationError("dependency depth exceeds configured limit")
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=original.get)
    if len(ordered) != len(keys):
        raise PlanValidationError("Plan dependency cycle")
    validated = ValidatedPlanDraft(
        objective=draft.objective,
        constraints=draft.constraints,
        generation_reason=draft.generation_reason,
        steps=[
            ValidatedPlanStep(**by_key[key].model_dump(), ordinal=index)
            for index, key in enumerate(ordered, start=1)
        ],
    )
    _reject_credentials(validated.model_dump(mode="json"))
    encoded = canonical_plan_bytes(validated)
    if len(encoded) > min(max_content_bytes, MAX_PLAN_CONTENT_BYTES):
        raise PlanValidationError(f"Plan content exceeds {min(max_content_bytes, MAX_PLAN_CONTENT_BYTES)} bytes")
    return validated


def canonical_plan_bytes(plan: ValidatedPlanDraft) -> bytes:
    return json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def plan_content_digest(plan: ValidatedPlanDraft) -> str:
    return hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()


def step_definition_digest(step: ValidatedPlanStep) -> str:
    value = {
        "logical_step_key": step.logical_step_key,
        "title": step.title,
        "description": step.description,
        "expected_outcome": step.expected_outcome,
        "max_attempts": step.max_attempts,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
```

Add `ValidatedPlanStep(PlanDraftStep)` with `ordinal: int = Field(ge=1, le=20)` and `ValidatedPlanDraft` with the same bounded fields as `PlanDraft`. The topological sort is stable by the generator's original order, then ordinals are persisted from that result.

- [ ] **Step 4: Run focused and property-style graph cases**

```bash
uv run pytest tests/test_planner.py -q
```

Expected: PASS for valid disconnected DAGs, deterministic ordering, size/digest behavior, and every invalid-edge case.

- [ ] **Step 5: Commit the persistence gate**

```bash
git add src/multiclaw/planner/models.py src/multiclaw/planner/validation.py \
  src/multiclaw/planner/__init__.py tests/test_planner.py
git commit -m "Reject unsafe or nondeterministic plan graphs before persistence" \
  -m "Canonicalize validated DAGs once so storage, recovery, and reuse compare the same immutable content." \
  -m "Constraint: Plan content is limited to 262144 bytes and dependency depth to 10" \
  -m "Rejected: Validate edges only with database foreign keys | foreign keys cannot reject cycles or enforce stable order" \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: uv run pytest tests/test_planner.py -q"
```

### Task 3: Add the scoped Plan schema and forward-only migration

**Files:**
- Modify: `src/multiclaw/storage/schema.py:110-330`
- Create: `alembic/versions/20260905_0002_durable_plan_engine.py`
- Modify: `tests/test_migrations.py:18-126`
- Modify: `tests/test_schema_contract.py`
- Modify: `tests/integration/test_mysql_contract.py:79-182`

- [ ] **Step 1: Add failing metadata, migration, and constraint tests**

```python
PLAN_TABLES = {
    "agent_plans",
    "agent_plan_versions",
    "agent_plan_steps",
    "agent_plan_step_dependencies",
    "agent_plan_step_runs",
    "agent_plan_decisions",
}


@pytest.mark.asyncio
async def test_upgrade_to_durable_plan_head_matches_metadata(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'plans.db'}"
    config = alembic_config(database_url=database_url)

    await asyncio.to_thread(command.upgrade, config, "head")

    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        assert ScriptDirectory.from_config(config).get_current_head() == "20260905_0002"
        assert await _current_revision(database) == "20260905_0002"
        async with database.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("agent_runs"))
            diffs = await conn.run_sync(
                lambda sync: compare_metadata(
                    MigrationContext.configure(sync, opts={"target_metadata": metadata}), metadata
                )
            )
    finally:
        await database.dispose()

    assert PLAN_TABLES <= tables
    assert {column["name"] for column in columns} >= {
        "plan_id", "initial_plan_version", "active_plan_version", "cancel_requested_at"
    }
    assert diffs == []


@pytest.mark.asyncio
async def test_plan_schema_rejects_cross_session_step_and_dependency(database, seeded_scopes):
    primary, foreign = seeded_scopes
    async with database.write_transaction() as conn:
        with pytest.raises(IntegrityError):
            await insert_cross_session_plan_step(conn, primary, foreign)
        with pytest.raises(IntegrityError):
            await insert_cross_version_dependency(conn, primary)
```

In the MySQL contract, assert the new revision, all six InnoDB tables, `MEDIUMTEXT` for `constraints_json`/feedback/result error fields where used, and the same Plan status/action/step-run check constraints.

- [ ] **Step 2: Run migration tests and observe the old head**

```bash
uv run pytest tests/test_migrations.py tests/test_schema_contract.py -q
```

Expected: FAIL because head is still `20260815_0001`, the Plan tables and run columns are absent, and metadata cannot express their scoped foreign keys.

- [ ] **Step 3: Define all six tables and additive run bindings**

Add SQLAlchemy Core tables in dependency order. Use `PAYLOAD_TEXT` for JSON/text payloads, `UUID_CHAR` for UUID IDs, database-clock integer timestamps, and these exact keys/checks:

```python
agent_plans = Table(
    "agent_plans", metadata,
    Column("id", UUID_CHAR, primary_key=True),
    Column("tenant_id", UUID_CHAR, nullable=False),
    Column("workspace_id", UUID_CHAR, nullable=False),
    Column("session_id", UUID_CHAR, nullable=False),
    Column("source_message_id", UUID_CHAR, nullable=False),
    Column("trigger_mode", String(16), nullable=False),
    Column("status", String(32), nullable=False),
    Column("current_version", Integer, nullable=False),
    Column("approved_version", Integer, nullable=True),
    Column("version", BIGINT, nullable=False, server_default="1"),
    Column("created_at", BIGINT, nullable=False),
    Column("updated_at", BIGINT, nullable=False),
    UniqueConstraint("tenant_id", "id"),
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "id"),
    CheckConstraint("trigger_mode IN ('automatic', 'explicit')", name=conv("ck_agent_plans_trigger_mode_valid")),
    CheckConstraint("status IN ('awaiting_approval', 'approved', 'rejected', 'archived')", name=conv("ck_agent_plans_status_valid")),
    CheckConstraint("current_version >= 1", name=conv("ck_agent_plans_current_version_positive")),
    CheckConstraint("approved_version IS NULL OR approved_version <= current_version", name=conv("ck_agent_plans_approved_version_valid")),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id"],
        ["chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"],
        ondelete="RESTRICT", onupdate="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "workspace_id", "session_id", "source_message_id"],
        ["memory_entries.tenant_id", "memory_entries.workspace_id", "memory_entries.session_id", "memory_entries.id"],
        ondelete="RESTRICT", onupdate="RESTRICT",
    ),
)
```

First add `UniqueConstraint("tenant_id", "workspace_id", "session_id", "id")` to `memory_entries`. Define `agent_plan_versions` with composite primary key `(tenant_id, workspace_id, session_id, plan_id, plan_version)`, fields from the approved design, a same-Plan parent-version FK, and `content_digest CHAR(64)`. Define `agent_plan_steps` with global `step_id` primary key plus unique full version scope, unique `(full version scope, logical_step_key)`, unique `(tenant_id, workspace_id, session_id, plan_id, step_id)` for `supersedes_step_id`, ordinal/max-attempt checks, and the nullable `assigned_agent_profile_id` extension point.

Define dependencies with a composite primary key over full version scope plus both step IDs and two full-version FKs. Define step runs with full Plan/run/step FKs, unique `(tenant_id, workspace_id, session_id, run_id, step_id, attempt)`, status/attempt/version checks, and unique `(tenant_id, workspace_id, session_id, plan_id, run_id, step_run_id)`. The `reused_from_step_run_id` self-FK uses that last key, deliberately excluding `plan_version` so it can reference an earlier immutable version while still enforcing the same scoped Plan run. Define decisions with composite primary key over full Plan scope plus `decision_id`, an action check, and a nullable full-scope FK from `resulting_plan_version` to the version table.

Extend `agent_runs` with:

```python
Column("plan_id", UUID_CHAR, nullable=True),
Column("initial_plan_version", Integer, nullable=True),
Column("active_plan_version", Integer, nullable=True),
Column("cancel_requested_at", BIGINT, nullable=True),
CheckConstraint(
    "(plan_id IS NULL AND initial_plan_version IS NULL AND active_plan_version IS NULL) OR "
    "(plan_id IS NOT NULL AND initial_plan_version IS NOT NULL AND active_plan_version IS NOT NULL)",
    name=conv("ck_agent_runs_plan_binding_complete"),
),
```

Add full-scope foreign keys from both initial and active versions to `agent_plan_versions`. Add indexes for session Plan lists, Plan-version steps, run attempts, and decision history. Do not use cascades; Tasks 16 and 19 prove explicit leaf-to-root deletion.

In `schema.py`, declare `agent_plans`, versions, steps, dependencies, and decisions after `memory_entries` but before `agent_runs`; declare step runs after `agent_runs`. Create migration `20260905_0002` with `down_revision="20260815_0001"` and use this order: add the memory uniqueness; create Plan aggregate/version/step/dependency/decision tables; add nullable run columns/check/FKs with dialect-safe batch alteration; create step-run table; create indexes. This avoids both sides of the run/version/attempt dependency cycle. Mirror metadata exactly and end with:

```python
def downgrade() -> None:
    raise RuntimeError("MultiClaw migrations are forward-only")
```

- [ ] **Step 4: Run SQLite metadata and schema checks**

```bash
uv run pytest tests/test_migrations.py tests/test_schema_contract.py -q
```

Expected: PASS with head `20260905_0002`, zero Alembic metadata diff, full-scope FK rejection, and valid additive nullable columns for legacy direct runs.

- [ ] **Step 5: Run the MySQL schema contract when configured**

```bash
uv run pytest tests/integration/test_mysql_contract.py -q
```

Expected: PASS on MySQL `>=8.0.36`, or SKIP only when `MULTICLAW_TEST_MYSQL_URL` is absent. Constraint names remain within MySQL's identifier limit through the repository naming convention.

- [ ] **Step 6: Commit the fact-store boundary**

```bash
git add src/multiclaw/storage/schema.py \
  alembic/versions/20260905_0002_durable_plan_engine.py \
  tests/test_migrations.py tests/test_schema_contract.py \
  tests/integration/test_mysql_contract.py
git commit -m "Give plan history enforceable scope and immutability" \
  -m "Persist normalized versions, dependencies, decisions, and attempts while keeping legacy direct runs valid through nullable bindings." \
  -m "Constraint: SQLite and MySQL must enforce the same full-session ownership contract" \
  -m "Rejected: Store Plan JSON only in checkpoints | checkpoints are recovery evidence rather than a query model" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_migrations.py tests/test_schema_contract.py -q; uv run pytest tests/integration/test_mysql_contract.py -q"
```

### Task 4: Bind a session-scoped Plan repository to the tenant UoW

**Files:**
- Create: `src/multiclaw/storage/repositories/plans.py`
- Modify: `src/multiclaw/storage/repositories/__init__.py:1-20`
- Modify: `src/multiclaw/storage/uow.py:8-25,175-207`
- Modify: `src/multiclaw/api/dependencies.py:33-50`
- Modify: `src/multiclaw/planner/models.py:1-260`
- Create: `tests/test_plan_repository.py`

- [ ] **Step 1: Write failing repository materialization and isolation tests**

Create the test fixture using the existing `_upgrade_database`, `_seed_scope`, and `TenantUnitOfWork` patterns from `tests/test_scoped_repositories.py`, then add:

```python
from dataclasses import replace
from uuid import uuid4

import pytest

from multiclaw.memory import MemoryEntry
from multiclaw.planner import PlanDraft, PlanDraftStep, PlanStatus, PlanTriggerMode
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.uow import TenantUnitOfWork


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

    assert snapshot.status is PlanStatus.AWAITING_APPROVAL
    assert snapshot.current_version == 1
    assert snapshot.approved_version is None
    assert [step.logical_step_key for step in snapshot.current.steps] == ["inspect", "verify"]
    assert snapshot.current.dependencies == {snapshot.current.steps[1].step_id: (snapshot.current.steps[0].step_id,)}
    assert len(snapshot.current.content_digest) == 64


@pytest.mark.asyncio
async def test_plan_lookup_hides_foreign_tenant_workspace_and_session(plan_database, seeded_plan):
    for foreign in seeded_plan.foreign_contexts:
        async with TenantUnitOfWork(plan_database, foreign) as uow:
            assert await uow.plans.for_context(foreign).get(seeded_plan.plan_id) is None
            assert await uow.plans.for_context(foreign).list_for_session() == []


@pytest.mark.asyncio
async def test_append_version_never_updates_old_rows(plan_database, seeded_plan):
    before = await dump_plan_version_rows(plan_database, seeded_plan.context, seeded_plan.plan_id, 1)
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

    after = await dump_plan_version_rows(plan_database, seeded_plan.context, seeded_plan.plan_id, 1)
    assert after == before
    assert snapshot.current_version == 2
    assert snapshot.current.parent_version == 1
    assert snapshot.current.steps[1].supersedes_step_id == seeded_plan.step_ids["verify"]
```

Implement `plan_database`, `plan_contexts`, `seeded_plan`, and `dump_plan_version_rows` in this file with public repository calls plus SQLAlchemy `select()` for the byte-for-byte assertion. `PlanRepository.for_context()` and every data method require a non-null `session_id`; make a direct data call on the root UoW factory raise `ValueError("PlanRepository requires session scope")`.

- [ ] **Step 2: Run the repository tests and observe the missing UoW member**

```bash
uv run pytest tests/test_plan_repository.py -q
```

Expected: FAIL because `TenantUnitOfWork` has no `plans` repository and no Plan record hydration types exist.

- [ ] **Step 3: Add immutable records and the scoped repository**

Add frozen dataclasses to `planner/models.py`:

```python
@dataclass(frozen=True, slots=True)
class PlanStepRecord:
    step_id: str
    logical_step_key: str
    supersedes_step_id: str | None
    ordinal: int
    title: str
    description: str
    expected_outcome: str
    assigned_agent_profile_id: str | None
    max_attempts: int
    definition_digest: str


@dataclass(frozen=True, slots=True)
class PlanVersionRecord:
    plan_id: str
    plan_version: int
    objective: str
    constraints: tuple[str, ...]
    generation_reason: str
    parent_version: int | None
    revision_feedback: str | None
    schema_version: int
    content_digest: str
    created_at: int
    steps: tuple[PlanStepRecord, ...]
    dependencies: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    context: TenantContext
    plan_id: str
    source_message_id: str
    trigger_mode: PlanTriggerMode
    status: PlanStatus
    current_version: int
    approved_version: int | None
    aggregate_version: int
    created_at: int
    updated_at: int
    current: PlanVersionRecord
    versions: tuple[PlanVersionRecord, ...]
    decisions: tuple[PlanDecisionRecord, ...]
```

In `plans.py`, allow the UoW's root context only as a factory, require session scope in `for_context()` and at every data method, expose `connection`, and implement `create()` as one set of inserts on the UoW connection:

```python
@dataclass(slots=True)
class PlanRepository:
    _conn: AsyncConnection
    _dialect: Dialect
    _context: TenantContext
    _settings: PlanningSettings

    def _require_session(self) -> str:
        if self._context.session_id is None:
            raise ValueError("PlanRepository requires session scope")
        return self._context.session_id

    def for_context(self, context: TenantContext) -> "PlanRepository":
        if context.session_id is None:
            raise ValueError("PlanRepository requires session scope")
        return PlanRepository(self._conn, self._dialect, context, self._settings)

    async def create(self, *, plan_id, source_message_id, trigger_mode, draft):
        self._require_session()
        validated = validate_plan_draft(
            draft,
            max_steps=self._settings.max_steps,
            max_depth=self._settings.max_dependency_depth,
            max_attempts=self._settings.max_step_attempts,
        )
        now = self._dialect.db_now_ms()
        await self._conn.execute(insert(agent_plans).values(
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
        ))
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
        return created
```

`_insert_version()` inserts the version, assigns each step a UUID, writes `definition_digest`, resolves logical dependency keys to the new step IDs, and inserts dependencies. `get()` performs bounded scoped queries for the aggregate, all versions, steps, dependencies, and decisions; mutable attempt facts remain behind `latest_step_attempts()` and are joined only by execution/API services. Order versions by `plan_version`, steps by `ordinal`, and decisions by `created_at, decision_id`. Parse `constraints_json` with `json.loads` and reject non-list data as corrupt rather than coercing it.

`append_version()` locks/selects the scoped Plan, checks `version == expected_version`, enforces `parent_version == current_version`, inserts only new version rows, and CAS-updates the aggregate to `awaiting_approval`, `current_version + 1`, `version + 1`. Validate every `supersedes_step_id` belongs to an earlier version of the same scoped Plan.

Bind a root Plan repository in `TenantUnitOfWork`:

```python
self.plans = PlanRepository(
    self.conn,
    self._database.dialect,
    self._context,
    self._planning_settings,
)
```

Add `planning_settings: PlanningSettings | None = None` to `TenantUnitOfWork.__init__` and default it to `PlanningSettings()` for non-request callers. In `tenant_uow()`, pass `request.app.state.settings.planning`. The root context is allowed only as a factory; all read/write methods themselves call `_require_session()` and tests use `uow.plans.for_context(context.for_session(session.id))`.

- [ ] **Step 4: Run repository and existing UoW tests**

```bash
uv run pytest tests/test_plan_repository.py tests/test_scoped_repositories.py tests/test_tenant_uow.py -q
```

Expected: PASS; foreign contexts observe empty results, old rows remain identical after revision, and all repositories share the UoW connection.

- [ ] **Step 5: Commit the repository boundary**

```bash
git add src/multiclaw/planner/models.py src/multiclaw/storage/repositories/plans.py \
  src/multiclaw/storage/repositories/__init__.py src/multiclaw/storage/uow.py \
  src/multiclaw/api/dependencies.py tests/test_plan_repository.py
git commit -m "Keep plan reads and writes inside one session-scoped transaction" \
  -m "Materialize normalized immutable versions through the tenant UoW and make incomplete scope impossible at repository call sites." \
  -m "Constraint: Plan IDs alone never authorize reads or mutations" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/test_plan_repository.py tests/test_scoped_repositories.py tests/test_tenant_uow.py -q"
```

### Task 5: Make Plan decisions CAS-safe, idempotent, and immutable

**Files:**
- Modify: `src/multiclaw/planner/models.py:1-340`
- Modify: `src/multiclaw/storage/repositories/plans.py`
- Modify: `tests/test_plan_repository.py`

- [ ] **Step 1: Add failing decision retry, stale-version, and concurrency tests**

```python
import asyncio

from pydantic import ValidationError

from multiclaw.planner import (
    PlanDecisionAction,
    PlanDecisionRequest,
    PlanStatus,
    PlanVersionConflictError,
)


def approve_request(seeded_plan, decision_id: str) -> PlanDecisionRequest:
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
        second = await uow.plans.for_context(seeded_plan.context).record_decision(
            request, decided_by=seeded_plan.tenant_id
        )

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert second.snapshot == first.snapshot
    assert second.decision == first.decision
    assert second.snapshot.status is PlanStatus.APPROVED
    assert await count_decisions(plan_database, seeded_plan.plan_id) == 1


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
                stale, decided_by=seeded_revised_plan.tenant_id
            )

    assert raised.value.latest.current_version == 2


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
                    request, decided_by=seeded_plan.tenant_id
                )
        except PlanVersionConflictError as error:
            return error

    outcomes = await asyncio.gather(*(decide(i, action) for i, action in enumerate(actions)))
    winners = [item for item in outcomes if not isinstance(item, PlanVersionConflictError)]
    assert len(winners) == 1
    assert await count_decisions(plan_database, seeded_plan.plan_id) == 1
```

Add the revision path explicitly:

```python
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
    before = await dump_plan_version_rows(plan_database, seeded_plan.context, seeded_plan.plan_id, 1)
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

    assert await dump_plan_version_rows(plan_database, seeded_plan.context, seeded_plan.plan_id, 1) == before
    assert snapshot.current_version == 2
    assert decision.resulting_plan_version == 2
```

The repository-level revision methods record intent and finalize its resulting version in one caller-owned transaction. Task 7 adds generation outside that transaction and coordinates the same repository primitives with the bound run/checkpoint.

- [ ] **Step 2: Run the decision tests and observe missing CAS errors**

```bash
uv run pytest tests/test_plan_repository.py -k 'decision or concurrent or stale' -q
```

Expected: FAIL because decision models, idempotency lookup, Plan CAS, and conflict payloads do not exist.

- [ ] **Step 3: Implement decision records and a one-winner CAS**

Add:

```python
class PlanDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=36, max_length=36)
    plan_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    action: PlanDecisionAction
    feedback: str | None = Field(default=None, max_length=8_000)

    @model_validator(mode="after")
    def validate_feedback(self):
        if self.action is PlanDecisionAction.REVISE:
            if self.feedback is None or not self.feedback.strip():
                raise ValueError("revision feedback is required")
        elif self.feedback is not None:
            raise ValueError("feedback is valid only for revise")
        return self


class PlanVersionConflictError(RuntimeError):
    def __init__(self, latest: PlanSnapshot):
        super().__init__("Plan version conflict")
        self.latest = latest
```

In `record_decision()` first query `(full scope, plan_id, decision_id)`. If present, require every persisted request field and `decided_by` to match and return its original result; a reused key with different input raises `PlanDecisionIdempotencyError`. Otherwise lock the scoped aggregate (`SELECT ... FOR UPDATE` on MySQL; the SQLite UoW already begins an immediate write transaction), require `status=awaiting_approval`, `request.plan_version=current_version`, and `request.expected_version=version`.

For approve/reject, insert the decision and CAS update with this predicate:

```python
updated = await self._conn.execute(
    update(agent_plans)
    .where(
        *self._plan_scope(request.plan_id),
        agent_plans.c.status == PlanStatus.AWAITING_APPROVAL.value,
        agent_plans.c.current_version == request.plan_version,
        agent_plans.c.version == request.expected_version,
    )
    .values(
        status=(PlanStatus.APPROVED if request.action is PlanDecisionAction.APPROVE else PlanStatus.REJECTED).value,
        approved_version=(request.plan_version if request.action is PlanDecisionAction.APPROVE else agent_plans.c.approved_version),
        version=agent_plans.c.version + 1,
        updated_at=self._dialect.db_now_ms(),
    )
)
if updated.rowcount != 1:
    latest = await self.get(request.plan_id)
    if latest is None:
        raise PlanNotFoundError
    raise PlanVersionConflictError(latest)
```

Insert the decision before the CAS in the same transaction; any losing CAS raises and rolls its insert back. Catch only duplicate `decision_id` inside a nested savepoint, reload the exact persisted decision, and apply the identical-request rule. Never catch a general `IntegrityError` and treat it as idempotency.

Expose `begin_revision_decision(request, *, decided_by)` and `finish_revision_decision(*, plan_id, decision_id, resulting_plan_version)` with the signatures used above so Task 7 can insert version `N+1`, CAS the aggregate once, and fill `resulting_plan_version` atomically without first resolving the Plan.

- [ ] **Step 4: Run concurrency repeatedly on file-backed SQLite**

Run the focused command in a shell loop without adding a test dependency:

```bash
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  uv run pytest tests/test_plan_repository.py -k 'decision or concurrent or stale' -q || exit 1
done
```

Expected: every run PASS with one persisted winner and no database-lock leak. Run the test once against MySQL through the existing backend fixture when configured.

- [ ] **Step 5: Commit the review concurrency contract**

```bash
git add src/multiclaw/planner/models.py src/multiclaw/storage/repositories/plans.py \
  tests/test_plan_repository.py
git commit -m "Make review decisions retry-safe under concurrency" \
  -m "Persist client idempotency keys and resolve every Plan action through one aggregate CAS so retries cannot duplicate revisions or continuations." \
  -m "Constraint: The authenticated user is the only decision actor" \
  -m "Rejected: Reuse tool approval rows | Plan review has no tool_call_id and a different lifecycle" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/test_plan_repository.py -k 'decision or concurrent or stale' -q (10 iterations)"
```

### Task 6: Separate PlanningPolicy and structured PlanGenerator from execution tools

**Files:**
- Create: `src/multiclaw/planner/policy.py`
- Create: `src/multiclaw/planner/generator.py`
- Modify: `src/multiclaw/planner/planner.py:1-23`
- Modify: `src/multiclaw/planner/__init__.py:1-60`
- Modify: `tests/test_planner.py`

- [ ] **Step 1: Add failing explicit-mode, fallback, structured-output, and repair tests**

```python
from uuid import uuid4

from multiclaw.llm import LLMResponse, ToolCall
from multiclaw.planner import PlanDraft, PlanDraftStep, PlanningMode, PlanningRoute
from multiclaw.planner.generator import PlanGenerationError, PlanGenerator
from multiclaw.planner.policy import PlanningPolicy, PlanningUnavailableError
from multiclaw.planner.models import PlanRevisionContext


def plan_draft(objective: str = "Deliver the change") -> PlanDraft:
    return PlanDraft(
        objective=objective,
        constraints=["Preserve behavior"],
        generation_reason="The request has dependent steps.",
        steps=[_step("inspect"), _step("verify", depends_on=["inspect"])],
    )


class StubRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def completion(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.asyncio
async def test_explicit_policy_modes_never_call_model():
    router = StubRouter([])
    policy = PlanningPolicy(router=router, default_model="default", classification_model="")

    assert (await policy.decide("simple", PlanningMode.NEVER)).mode is PlanningRoute.DIRECT
    assert (await policy.decide("complex", PlanningMode.ALWAYS)).mode is PlanningRoute.PLAN
    assert router.calls == []


@pytest.mark.asyncio
async def test_auto_classification_failure_falls_back_direct_with_bounded_reason():
    policy = PlanningPolicy(
        router=StubRouter([RuntimeError("provider unavailable token=private")]),
        default_model="default",
        classification_model="classifier",
    )
    decision = await policy.decide("request", PlanningMode.AUTO)

    assert decision.mode is PlanningRoute.DIRECT
    assert decision.reason == "automatic classification unavailable"


@pytest.mark.asyncio
async def test_generator_passes_only_submit_plan_schema_and_validates_payload():
    response = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="plan", name="submit_plan", arguments=plan_draft().model_dump())],
    )
    router = StubRouter([response])
    generated = await PlanGenerator(router, default_model="default", generation_model="planner").generate(
        objective="Deliver the change", revision=None, max_steps=20, max_depth=10, max_attempts=2
    )

    assert generated.objective == "Deliver the change"
    assert [tool["function"]["name"] for tool in router.calls[0]["tools"]] == ["submit_plan"]
    assert "read_file" not in str(router.calls[0]["tools"])


@pytest.mark.asyncio
async def test_generator_repairs_once_then_fails_closed():
    invalid = LLMResponse(content="not structured", tool_calls=[])
    router = StubRouter([invalid, invalid])
    generator = PlanGenerator(router, default_model="default", generation_model="")

    with pytest.raises(PlanGenerationError, match="two invalid structured responses"):
        await generator.generate(
            objective="Deliver", revision=None, max_steps=20, max_depth=10, max_attempts=2
        )
    assert len(router.calls) == 2
    assert all([tool["function"]["name"] for tool in call["tools"]] == ["submit_plan"] for call in router.calls)
```

Cover disabled routing, strict function selection, invalid generations, objective ownership, and revision redaction with these tests:

```python
@pytest.mark.asyncio
async def test_disabled_policy_fails_closed_only_for_explicit_always():
    policy = PlanningPolicy(
        router=StubRouter([]), default_model="default", classification_model="", enabled=False
    )

    assert (await policy.decide("request", PlanningMode.AUTO)).mode is PlanningRoute.DIRECT
    assert (await policy.decide("request", PlanningMode.NEVER)).mode is PlanningRoute.DIRECT
    with pytest.raises(PlanningUnavailableError):
        await policy.decide("request", PlanningMode.ALWAYS)


@pytest.mark.asyncio
async def test_classifier_accepts_only_its_bounded_function_schema():
    valid = LLMResponse(
        content="",
        tool_calls=[ToolCall(
            id="route",
            name="classify_planning_request",
            arguments={"mode": "plan", "reason": "Multiple dependent changes."},
        )],
    )
    router = StubRouter([valid])
    decision = await PlanningPolicy(
        router=router, default_model="default", classification_model="classifier"
    ).decide(
        "request", PlanningMode.AUTO
    )

    assert decision.mode is PlanningRoute.PLAN
    assert len(decision.reason) <= 500
    assert [item["function"]["name"] for item in router.calls[0]["tools"]] == [
        "classify_planning_request"
    ]


@pytest.mark.parametrize("invalid_kind", ["wrong_function", "multiple", "cycle", "credential"])
@pytest.mark.asyncio
async def test_generator_rejects_every_invalid_structured_shape(invalid_kind):
    invalid = invalid_plan_response(invalid_kind)
    generator = PlanGenerator(
        StubRouter([invalid, invalid]), default_model="default", generation_model="planner"
    )

    with pytest.raises(PlanGenerationError, match="two invalid structured responses"):
        await generator.generate(
            objective="Deliver", revision=None, max_steps=20, max_depth=10, max_attempts=2
        )


@pytest.mark.asyncio
async def test_generator_owns_objective_and_redacts_revision_context():
    response = LLMResponse(
        content="",
        tool_calls=[ToolCall(
            id="plan",
            name="submit_plan",
            arguments=plan_draft(objective="Model substituted objective").model_dump(),
        )],
    )
    router = StubRouter([response])
    canary = "Authorization: " + "Bearer revision-canary"
    revision = PlanRevisionContext(
        plan_id=str(uuid4()),
        parent_version=1,
        feedback=f"Retry without {canary}",
        failed_step_key="verify",
        failed_error=canary,
        completed=[],
    )
    generated = await PlanGenerator(
        router, default_model="default", generation_model="planner"
    ).generate(
        objective=f"Deliver while removing {canary}",
        revision=revision,
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )

    assert generated.objective == "Deliver while removing [REDACTED]"
    assert canary not in str(router.calls[0]["messages"])
    assert "[REDACTED]" in str(router.calls[0]["messages"])
```

Use this helper in `tests/test_planner.py`; each returned response is supplied twice above so the test proves the single-repair limit as well as rejection:

```python
def invalid_plan_response(kind: str) -> LLMResponse:
    draft = plan_draft()
    if kind == "wrong_function":
        calls = [ToolCall(id="bad", name="read_file", arguments={"path": "README.md"})]
    elif kind == "multiple":
        calls = [
            ToolCall(id="one", name="submit_plan", arguments=draft.model_dump()),
            ToolCall(id="two", name="submit_plan", arguments=draft.model_dump()),
        ]
    elif kind == "cycle":
        cyclic = plan_draft()
        cyclic.steps = [
            PlanDraftStep(
                logical_step_key="a", title="A", description="A step",
                expected_outcome="A completes", depends_on=["b"],
            ),
            PlanDraftStep(
                logical_step_key="b", title="B", description="B step",
                expected_outcome="B completes", depends_on=["a"],
            ),
        ]
        calls = [ToolCall(id="bad", name="submit_plan", arguments=cyclic.model_dump())]
    elif kind == "credential":
        draft.steps[0].title = "Use Authorization: " + "Bearer generator-canary"
        calls = [ToolCall(id="bad", name="submit_plan", arguments=draft.model_dump())]
    else:
        raise AssertionError(f"unknown invalid kind: {kind}")
    return LLMResponse(content="", tool_calls=calls)
```

- [ ] **Step 2: Run the planner tests and observe missing services**

```bash
uv run pytest tests/test_planner.py -q
```

Expected: FAIL with missing `planner.policy`, `planner.generator`, and generation errors.

- [ ] **Step 3: Implement deterministic policy routing and two-call generation**

Use dedicated schemas created from Pydantic `model_json_schema()`:

```python
def function_schema(name: str, description: str, model: type[BaseModel]) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": model.model_json_schema(),
        },
    }
```

`PlanningPolicy.decide()` resolves explicit modes first. For auto, call `router.completion(model=classification_model or default_model, messages=[system, user], tools=[CLASSIFY_SCHEMA])`; require exactly one `classify_planning_request` call and validate `PlanningDecision`. On any provider, parse, or validation error, increment a bounded failure event in Task 19 and return `PlanningDecision(mode="direct", reason="automatic classification unavailable")` without including the exception.

`PlanGenerator.generate()` builds the initial prompt or this bounded revision object:

```python
class PlanRevisionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    parent_version: int
    feedback: str | None
    failed_step_key: str | None
    failed_error: str | None
    completed: list[CompletedStepContext] = Field(max_length=20)
```

Call only `submit_plan`. Parse exactly one matching tool call, compute `safe_objective = sanitize_plan_text(requested_objective)` and force `draft.objective` to that value, then call `validate_plan_draft()` with configured limits. Sanitize revision feedback with the same credential-only function before the prompt, decision record, and version row; ordinary filesystem paths remain intact. On the first invalid response append a system repair message containing only the Pydantic/PlanValidation error class and bounded field locations, not raw output. The second invalid response raises `PlanGenerationError("two invalid structured responses")`.

Replace `Planner.create_plan/approve/summary` with a deprecated import alias:

```python
from multiclaw.planner.generator import PlanGenerator

Planner = PlanGenerator
```

Do not instantiate that alias until Task 15 migrates runtime callers; its presence only prevents an abrupt import break during the incremental plan.

- [ ] **Step 4: Run the planner contract**

```bash
uv run pytest tests/test_planner.py -q
```

Expected: PASS with zero real tool schemas in both planning calls, exactly one repair, fail-open only for `auto` classification, and fail-closed generation for explicit planning.

- [ ] **Step 5: Commit the side-effect-free planning boundary**

```bash
git add src/multiclaw/planner/policy.py src/multiclaw/planner/generator.py \
  src/multiclaw/planner/planner.py src/multiclaw/planner/__init__.py tests/test_planner.py
git commit -m "Separate planning judgment from side-effect-capable execution" \
  -m "Give classification and generation dedicated structured schemas, bounded failure behavior, and no access to the tenant tool registry." \
  -m "Constraint: Invalid generation receives one repair call and never falls back to objective execution" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/test_planner.py -q"
```

### Task 7: Materialize waiting Plans and immutable revisions atomically

**Files:**
- Create: `src/multiclaw/planner/service.py`
- Modify: `src/multiclaw/planner/models.py:1-420`
- Modify: `src/multiclaw/storage/repositories/plans.py`
- Modify: `src/multiclaw/workflow/coordinator.py:56-110`
- Modify: `src/multiclaw/storage/repositories/workflow.py:189-235`
- Modify: `tests/test_plan_repository.py`
- Modify: `tests/test_workflow_state.py`

- [ ] **Step 1: Add failing atomicity and revision-service tests**

```python
from multiclaw.planner.service import MaterializeInitialPlan, PlanningService
from multiclaw.workflow.models import CheckpointPhase, RunStatus


@pytest.mark.asyncio
async def test_initial_materialization_is_one_transaction(plan_database, seeded_source_message):
    service = PlanningService(plan_database, settings=planning_settings())
    request = MaterializeInitialPlan(
        context=seeded_source_message.context.for_run(seeded_source_message.session_id, str(uuid4())),
        runtime_instance_id="runtime-1",
        source_message_id=seeded_source_message.message_id,
        assistant_turn_index=2,
        trigger_mode=PlanTriggerMode.EXPLICIT,
        draft=plan_draft(),
    )

    materialized = await service.materialize_initial(request)
    run = await load_run(plan_database, request.context)
    checkpoint = await load_latest_checkpoint(plan_database, request.context)
    message = await load_message(plan_database, materialized.reference_message_id)

    assert run.status is RunStatus.AWAITING_USER
    assert (run.plan_id, run.initial_plan_version, run.active_plan_version) == (
        materialized.plan.plan_id, 1, 1
    )
    assert checkpoint.phase == CheckpointPhase.PLAN_AWAITING_APPROVAL.value
    assert message.metadata["parts"] == [{
        "type": "data-plan-created",
        "data": materialized.reference.model_dump(mode="json"),
    }]


@pytest.mark.asyncio
async def test_materialization_failure_rolls_back_plan_run_checkpoint_and_reference(
    plan_database, seeded_source_message, monkeypatch
):
    service = PlanningService(plan_database, settings=planning_settings())
    monkeypatch.setattr(service, "_persist_reference", raising_failure)

    with pytest.raises(RuntimeError, match="reference failure"):
        await service.materialize_initial(materialize_request(seeded_source_message))

    assert await count_plan_rows(plan_database) == 0
    assert await count_runs(plan_database) == 0
    assert await count_checkpoints(plan_database) == 0


@pytest.mark.asyncio
async def test_revision_keeps_active_version_until_new_version_is_approved(seeded_waiting_plan):
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
    run = await load_run(seeded_waiting_plan.database, seeded_waiting_plan.context)

    assert result.snapshot.current_version == 2
    assert result.snapshot.status is PlanStatus.AWAITING_APPROVAL
    assert result.decision.resulting_plan_version == 2
    assert run.active_plan_version == 1
    assert run.status is RunStatus.AWAITING_USER


@pytest.mark.asyncio
async def test_approve_and_resume_are_atomic(seeded_waiting_plan, monkeypatch):
    async def raise_stale_fence(*args, **kwargs):
        raise StaleFenceError("injected")

    monkeypatch.setattr(
        seeded_waiting_plan.workflow,
        "resume_waiting_plan_run",
        raise_stale_fence,
    )
    with pytest.raises(StaleFenceError):
        await seeded_waiting_plan.service.decide(
            approve_request(seeded_waiting_plan, "approve-rollback"),
            decided_by=seeded_waiting_plan.tenant_id,
            runtime_instance_id="runtime-2",
        )

    plan = await seeded_waiting_plan.load_plan()
    run = await seeded_waiting_plan.load_run()
    assert plan.status is PlanStatus.AWAITING_APPROVAL
    assert plan.decisions == ()
    assert run.status is RunStatus.AWAITING_USER


@pytest.mark.asyncio
async def test_reject_and_cancel_commit_together(seeded_waiting_plan):
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
    assert result.lease is None
```

The fixture injects its fake `PlanGenerator` into `PlanningService`. The production `decide()` accepts no generated-draft bypass.

- [ ] **Step 2: Run atomic service tests and observe partial-boundary failures**

```bash
uv run pytest tests/test_plan_repository.py tests/test_workflow_state.py -k 'materialization or revision or plan_run' -q
```

Expected: FAIL because no service can share one connection across Plan rows, run state, checkpoint, and memory metadata.

- [ ] **Step 3: Add a connection-bound Plan run start and transaction-owned service**

Add `WorkflowCoordinator.start_plan_run_with_checkpoint()`; it uses `_write_connection()` so the `connection=` passed by `PlanningService` keeps all operations in the caller's UoW:

```python
async def start_plan_run_with_checkpoint(
    self,
    context: TenantContext,
    runtime_instance_id: str,
    *,
    plan_id: str,
    plan_version: int,
    plan_digest: str,
) -> RunLease:
    async with self._write_connection() as conn:
        repository = self._repository(conn)
        await repository._lock_tenant(context.tenant_id)
        await self._enforce_run_quota(repository, context.tenant_id)
        lease = await repository._create_run(
            context,
            runtime_instance_id=runtime_instance_id,
            status=RunStatus.AWAITING_USER,
            plan_id=plan_id,
            initial_plan_version=plan_version,
            active_plan_version=plan_version,
        )
        await self._scoped(conn).checkpoint(
            lease,
            CheckpointPhase.PLAN_AWAITING_APPROVAL,
            PlanAwaitingApprovalPayload(
                run_id=context.run_id,
                plan_id=plan_id,
                plan_version=plan_version,
                plan_digest=plan_digest,
                decision_cursor=f"plan:{plan_id}:v{plan_version}:decision",
                cursor=f"plan:{plan_id}:v{plan_version}:decision",
            ),
            checkpoint_seq=1,
        )
        return lease
```

Implement `PlanningService.materialize_initial()` with a new `TenantUnitOfWork(database, context)`. Construct `WorkflowCoordinator(database, settings=settings, connection=uow.conn)`, then use this strict order inside one transaction: validate draft; `uow.plans.create`; `coordinator.start_plan_run_with_checkpoint(...)`; save an assistant `chat_message` with empty content and metadata `{"parts":[{"type":"data-plan-created","data": reference}]}`. Return a `PlanMaterializationResult`; publish no event inside the service transaction.

For revise: first perform a scoped read for `decision_id`; an identical completed decision returns immediately without a generator call, while a mismatched reuse raises `PlanDecisionIdempotencyError`. If absent, load the current immutable revision context, close the read transaction, call the injected generator, then open one write UoW and re-read/CAS the current snapshot. Enforce revision quota, begin the decision, append `N+1`, checkpoint `PLAN_AWAITING_APPROVAL` through a coordinator method that fences the waiting run without resuming it, persist `resulting_plan_version`, and commit. `active_plan_version` remains unchanged. Return one redacted `PlanEvent` for the caller to publish after commit.

For approve: inside one UoW record the Plan decision CAS, load the bound waiting run, then call the connection-bound coordinator's `resume_waiting_plan_run(...)` with the approved version and expected run CAS. The coordinator rotates the fence, sets `active_plan_version` and `resuming`, and returns the only lease authorized to continue. For reject: record the Plan decision and call `cancel_waiting_plan_run(...)`, which writes the cancelled terminal checkpoint. A failure in either coordinator call rolls back the Plan decision too.

An identical committed `decision_id` returns `idempotent_replay=True`, the stored decision/snapshot/run, and no continuation lease. If the original approval response was lost after commit, its run is already `resuming`; Task 13 recovery owns continuation. A retry must never construct a second lease or start a second executor.

If generation fails, write no decision/version rows. If any Plan CAS, run CAS, insert, checkpoint, or reference write fails, let the UoW roll back everything. Never call `uow.commit()` from a repository.

- [ ] **Step 4: Prove atomicity and old-version immutability**

```bash
uv run pytest tests/test_plan_repository.py tests/test_workflow_state.py -k 'materialization or revision or plan_run' -q
```

Expected: PASS, including injected failures at each write boundary and a byte-for-byte version-1 dump before/after revision.

- [ ] **Step 5: Commit the atomic application boundary**

```bash
git add src/multiclaw/planner/service.py src/multiclaw/planner/models.py \
  src/multiclaw/storage/repositories/plans.py src/multiclaw/workflow/coordinator.py \
  src/multiclaw/storage/repositories/workflow.py tests/test_plan_repository.py \
  tests/test_workflow_state.py
git commit -m "Make a waiting plan appear atomically with its run and message" \
  -m "Coordinate immutable Plan materialization, workflow binding, approval checkpoint, and assistant reference on one scoped connection." \
  -m "Constraint: LLM generation happens outside database transactions" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_plan_repository.py tests/test_workflow_state.py -k 'materialization or revision or plan_run' -q"
```

### Task 8: Extend the existing workflow fence with Plan boundary checkpoints

**Files:**
- Modify: `src/multiclaw/workflow/models.py:49-236,354-366`
- Modify: `src/multiclaw/workflow/coordinator.py:36-202,314-399`
- Modify: `src/multiclaw/storage/repositories/workflow.py:51-330,953-1040`
- Modify: `tests/test_workflow_state.py`
- Modify: `tests/test_workflow_recovery.py`

- [ ] **Step 1: Add failing transition, payload, binding, and stale-fence tests**

```python
from multiclaw.workflow.models import (
    CheckpointPhase,
    PlanAwaitingApprovalPayload,
    PlanReplanRequiredPayload,
    PlanStepReadyPayload,
    RunStatus,
)


def test_plan_checkpoint_payloads_are_strict_and_cursor_bound():
    awaiting = PlanAwaitingApprovalPayload(
        run_id=RUN_ID,
        plan_id=PLAN_ID,
        plan_version=1,
        plan_digest="a" * 64,
        decision_cursor="decision-1",
        cursor="decision-1",
    )
    ready = PlanStepReadyPayload(
        run_id=RUN_ID,
        plan_id=PLAN_ID,
        plan_version=1,
        plan_digest="a" * 64,
        step_id=STEP_ID,
        step_run_id=STEP_RUN_ID,
        attempt=1,
        execution_cursor="dispatch_step",
        cursor="dispatch_step",
    )
    replan = PlanReplanRequiredPayload(
        run_id=RUN_ID,
        plan_id=PLAN_ID,
        plan_version=1,
        plan_digest="a" * 64,
        failed_step_run_id=STEP_RUN_ID,
        failure_digest="b" * 64,
        revision_cursor="generate_revision",
        cursor="generate_revision",
    )

    assert PHASE_PAYLOADS[CheckpointPhase.PLAN_AWAITING_APPROVAL] is type(awaiting)
    assert PHASE_PAYLOADS[CheckpointPhase.PLAN_STEP_READY] is type(ready)
    assert PHASE_PAYLOADS[CheckpointPhase.PLAN_REPLAN_REQUIRED] is type(replan)


def test_waiting_plan_can_cancel_or_resume_but_not_complete_directly():
    assert LEGAL_RUN_TRANSITIONS[RunStatus.AWAITING_USER] == frozenset(
        {RunStatus.RESUMING, RunStatus.CANCELLED}
    )


@pytest.mark.asyncio
async def test_approve_plan_resumes_with_new_fence_and_active_version(plan_workflow):
    before = await plan_workflow.load_run()
    lease = await plan_workflow.coordinator.resume_waiting_plan_run(
        plan_workflow.context,
        runtime_instance_id="runtime-resume",
        plan_id=plan_workflow.plan_id,
        plan_version=2,
        expected_run_version=before.version,
    )
    after = await plan_workflow.load_run()

    assert after.status is RunStatus.RESUMING
    assert after.active_plan_version == 2
    assert lease.fencing_token == before.fencing_token + 1


@pytest.mark.asyncio
async def test_plan_step_mutation_rejects_stale_fence(plan_workflow):
    stale = plan_workflow.lease
    current = await plan_workflow.coordinator.heartbeat(stale)
    with pytest.raises(StaleFenceError):
        await plan_workflow.coordinator.checkpoint(
            stale,
            CheckpointPhase.PLAN_STEP_READY,
            plan_workflow.ready_payload(),
        )
    assert current.version > stale.version
```

Add the hydration compatibility assertion:

```python
@pytest.mark.asyncio
async def test_run_record_hydrates_plan_binding_without_changing_direct_runs(plan_workflow):
    plan_run = await plan_workflow.load_run()
    direct_run = await plan_workflow.create_and_load_direct_run()

    assert (
        plan_run.plan_id,
        plan_run.initial_plan_version,
        plan_run.active_plan_version,
        plan_run.cancel_requested_at,
    ) == (plan_workflow.plan_id, 1, 1, None)
    assert (
        direct_run.plan_id,
        direct_run.initial_plan_version,
        direct_run.active_plan_version,
        direct_run.cancel_requested_at,
    ) == (None, None, None, None)
```

- [ ] **Step 2: Run workflow tests and observe unknown phases/fields**

```bash
uv run pytest tests/test_workflow_state.py tests/test_workflow_recovery.py -k 'plan or awaiting_user' -q
```

Expected: FAIL because Plan phases are not in `CheckpointPhase`/`PHASE_PAYLOADS`, awaiting-user cancellation is illegal, and run records lack Plan bindings.

- [ ] **Step 3: Add strict Plan payloads and coordinator-only mutations**

Add phases and payloads:

```python
class CheckpointPhase(StrEnum):
    RUN_STARTED = "run_started"
    PLAN_AWAITING_APPROVAL = "plan_awaiting_approval"
    PLAN_STEP_READY = "plan_step_ready"
    PLAN_REPLAN_REQUIRED = "plan_replan_required"
    MODEL_OUTPUT_COMMITTED = "model_output_committed"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTION_DISPATCHING = "execution_dispatching"
    EXECUTION_RESULT_OBSERVED = "execution_result_observed"
    RUN_TERMINAL = "run_terminal"


class PlanAwaitingApprovalPayload(CheckpointPayload):
    run_id: str = UUID_FIELD
    plan_id: str = UUID_FIELD
    plan_version: StrictInt = Field(ge=1)
    plan_digest: str = DIGEST_FIELD
    decision_cursor: str = CURSOR_FIELD
    next_step: Literal["plan_decision"] = "plan_decision"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self):
        if self.cursor != self.decision_cursor:
            raise ValueError("cursor must match decision_cursor")
        return self


class PlanStepReadyPayload(CheckpointPayload):
    run_id: str = UUID_FIELD
    plan_id: str = UUID_FIELD
    plan_version: StrictInt = Field(ge=1)
    plan_digest: str = DIGEST_FIELD
    step_id: str = UUID_FIELD
    step_run_id: str = UUID_FIELD
    attempt: StrictInt = Field(ge=1, le=20)
    execution_cursor: Literal["dispatch_step", "continue_step", "select_next", "final_summary"]
    next_step: Literal["plan_step_execution"] = "plan_step_execution"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self):
        if self.cursor != self.execution_cursor:
            raise ValueError("cursor must match execution_cursor")
        return self


class PlanReplanRequiredPayload(CheckpointPayload):
    run_id: str = UUID_FIELD
    plan_id: str = UUID_FIELD
    plan_version: StrictInt = Field(ge=1)
    plan_digest: str = DIGEST_FIELD
    failed_step_run_id: str = UUID_FIELD
    failure_digest: str = DIGEST_FIELD
    revision_cursor: Literal["generate_revision"] = "generate_revision"
    next_step: Literal["plan_revision"] = "plan_revision"
    cursor: str = CURSOR_FIELD

    @model_validator(mode="after")
    def validate_cursor(self):
        if self.cursor != self.revision_cursor:
            raise ValueError("cursor must match revision_cursor")
        return self
```

Register all payloads. Add `RecoveryAction.AWAIT_PLAN_DECISION`, `RESUME_PLAN_STEP`, and `RESUME_PLAN_REVISION`. Extend `RunRecord` with nullable Plan/cancellation fields and `LEGAL_RUN_TRANSITIONS[AWAITING_USER]` with `CANCELLED`.

Extend `WorkflowRepository._create_run()` with explicit `status`, `plan_id`, `initial_plan_version`, and `active_plan_version` keyword-only arguments. Add:

```python
async def _resume_waiting_plan_run(
    self, context, *, runtime_instance_id, plan_id, plan_version, expected_run_version
) -> RunLease | None:
    result = await self._conn.execute(
        update(agent_runs)
        .where(
            self._run_scope(context),
            agent_runs.c.run_status == RunStatus.AWAITING_USER.value,
            agent_runs.c.plan_id == plan_id,
            agent_runs.c.version == expected_run_version,
        )
        .values(
            run_status=RunStatus.RESUMING.value,
            active_plan_version=plan_version,
            runtime_instance_id=runtime_instance_id,
            lease_owner=runtime_instance_id,
            fencing_token=agent_runs.c.fencing_token + 1,
            lease_expires_at=self._dialect.db_now_ms() + self._lease_ttl_ms,
            heartbeat_at=self._dialect.db_now_ms(),
            version=agent_runs.c.version + 1,
            updated_at=self._dialect.db_now_ms(),
        )
    )
    return None if result.rowcount != 1 else await self._lease_for(context)
```

`WorkflowCoordinator.resume_waiting_plan_run()` calls this and raises `StaleFenceError` on no match. Add `cancel_waiting_plan_run()` with full scope, plan/version/run-version CAS and `finished_at=db_now`, plus `fence_waiting_plan_run()` for revised approval checkpoints without changing status. All checkpoint writes still pass `current_lease_predicate()`.

When hydrating a Run, reject partial Plan bindings as corrupt even though the database check already prevents new invalid rows. Direct runs retain the existing path.

- [ ] **Step 4: Run workflow state and checkpoint regression suites**

```bash
uv run pytest tests/test_workflow_state.py tests/test_workflow_recovery.py -q
```

Expected: PASS; old workflow phases behave unchanged, new payloads fail on extra/mismatched data, stale fences write zero checkpoints, and a waiting Plan can be rejected durably.

- [ ] **Step 5: Commit the shared reliability boundary**

```bash
git add src/multiclaw/workflow/models.py src/multiclaw/workflow/coordinator.py \
  src/multiclaw/storage/repositories/workflow.py tests/test_workflow_state.py \
  tests/test_workflow_recovery.py
git commit -m "Keep workflow fencing authoritative at plan boundaries" \
  -m "Extend the existing run state machine and typed checkpoints instead of creating a second lease or recovery subsystem." \
  -m "Constraint: Every executing Plan mutation carries the current run fence" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_workflow_state.py tests/test_workflow_recovery.py -q"
```

### Task 9: Select ready steps deterministically and create one fenced attempt

**Files:**
- Create: `src/multiclaw/planner/execution.py`
- Modify: `src/multiclaw/planner/models.py:1-500`
- Modify: `src/multiclaw/storage/repositories/plans.py`
- Create: `tests/test_plan_execution.py`

- [ ] **Step 1: Add failing stable-selection, version-gate, and serial-attempt tests**

```python
import asyncio

from multiclaw.planner.execution import PlanExecutionCoordinator
from multiclaw.planner.models import PlanExecutionBlocked, PlanStepRunStatus


@pytest.mark.asyncio
async def test_selector_uses_stable_topological_ordinal(execution_fixture):
    await execution_fixture.approve()
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture.context,
        lease=execution_fixture.lease,
    )
    assert first.step.logical_step_key == "lint"

    await execution_fixture.succeed(first.step_run, digest="a" * 64)
    second = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture.context,
        lease=execution_fixture.current_lease,
    )
    assert second.step.logical_step_key == "test"


@pytest.mark.asyncio
async def test_unapproved_or_stale_active_version_dispatches_no_step(execution_fixture):
    for mutation in ("unapproved", "current_ahead", "active_behind"):
        await execution_fixture.reset(mutation)
        with pytest.raises(PlanExecutionBlocked, match="approved current active version"):
            await execution_fixture.coordinator.start_next_attempt(
                context=execution_fixture.context,
                lease=execution_fixture.current_lease,
            )
        assert await execution_fixture.count_step_runs() == 0


@pytest.mark.asyncio
async def test_concurrent_ready_nodes_still_create_one_running_attempt(execution_fixture):
    await execution_fixture.approve_with_two_roots()

    async def start():
        try:
            return await execution_fixture.new_coordinator().start_next_attempt(
                context=execution_fixture.context,
                lease=execution_fixture.current_lease,
            )
        except (StaleFenceError, PlanStepAlreadyRunningError):
            return None

    outcomes = await asyncio.gather(start(), start())
    started = [item for item in outcomes if item is not None]
    assert len(started) == 1
    assert await execution_fixture.count_status(PlanStepRunStatus.RUNNING) == 1
```

Add the remaining selector/fence limits explicitly:

```python
@pytest.mark.asyncio
async def test_dependent_step_is_not_ready_until_dependency_succeeds(execution_fixture):
    await execution_fixture.approve_chain(["inspect", "verify"])
    first = await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture.context, lease=execution_fixture.current_lease
    )
    assert first.step.logical_step_key == "inspect"

    await execution_fixture.finish(first.step_run, PlanStepRunStatus.FAILED_TERMINAL)
    with pytest.raises(PlanExecutionBlocked, match="failed dependency"):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture.context, lease=execution_fixture.current_lease
        )


@pytest.mark.asyncio
async def test_all_succeeded_returns_no_next_step(execution_fixture):
    await execution_fixture.approve_and_succeed_all()
    assert await execution_fixture.coordinator.start_next_attempt(
        context=execution_fixture.context, lease=execution_fixture.current_lease
    ) is None


@pytest.mark.asyncio
async def test_stale_lease_and_exhausted_attempt_budget_create_no_attempt(execution_fixture):
    stale = await execution_fixture.capture_then_rotate_lease()
    with pytest.raises(StaleFenceError):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture.context, lease=stale
        )

    await execution_fixture.exhaust_current_step_attempts()
    before = await execution_fixture.count_step_runs()
    with pytest.raises(PlanAttemptLimitError):
        await execution_fixture.coordinator.start_next_attempt(
            context=execution_fixture.context, lease=execution_fixture.current_lease
        )
    assert await execution_fixture.count_step_runs() == before
```

- [ ] **Step 2: Run execution tests and observe the missing coordinator**

```bash
uv run pytest tests/test_plan_execution.py -k 'selector or version or concurrent or attempt' -q
```

Expected: FAIL because no Plan execution selector or fenced step-run persistence exists.

- [ ] **Step 3: Implement stable readiness and atomic attempt/checkpoint creation**

Define:

```python
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
        step_id for step_id, attempt in latest.items()
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
```

`PlanRepository.create_step_attempt()` must lock the run through the dialect adapter, verify `current_lease_predicate`, load the scoped Plan, require `status=approved` and `current_version == approved_version == agent_runs.active_plan_version == plan_version`, reject any current `running` attempt for that run, compute `attempt = previous attempt + 1`, and enforce both the immutable step limit and configured tenant limit.

`PlanRepository.create_step_attempt()` inserts only the `running` step-run fact. In the same UoW, `PlanExecutionCoordinator.start_next_attempt()` constructs a connection-bound `WorkflowCoordinator` and calls `checkpoint()` for `PLAN_STEP_READY(execution_cursor="dispatch_step")`; Plan code never writes `execution_checkpoints` directly. If either insert fails, neither survives. Re-check the fence immediately before the attempt insert and use the unique `(run, step, attempt)` key as a second defense.

`PlanExecutionCoordinator.select_next()` is a pure-read helper that returns `ReadyPlanStep | None`. `start_next_attempt()` opens one UoW, loads the snapshot/latest attempts, calls `choose_ready_step`, delegates attempt+checkpoint creation, and returns `StartedPlanStep`. Return `None` only if every step succeeded; if a failed-terminal/cancelled dependency blocks remaining work, raise a typed terminal result instead of treating the Plan as complete.

- [ ] **Step 4: Run the serial executor contract repeatedly**

```bash
for attempt in 1 2 3 4 5; do
  uv run pytest tests/test_plan_execution.py -k 'selector or version or concurrent or attempt' -q || exit 1
done
```

Expected: PASS with one running attempt in every concurrent case, no step rows for an unapproved/mismatched Plan, and stable ordinal selection for disconnected ready nodes.

- [ ] **Step 5: Commit deterministic execution selection**

```bash
git add src/multiclaw/planner/execution.py src/multiclaw/planner/models.py \
  src/multiclaw/storage/repositories/plans.py tests/test_plan_execution.py
git commit -m "Guarantee one deterministic step attempt at a time" \
  -m "Select ready DAG nodes by persisted ordinal and create each running attempt beside its fenced checkpoint." \
  -m "Constraint: First release execution is strictly serial even when multiple nodes are ready" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_plan_execution.py -k 'selector or version or concurrent or attempt' -q (5 iterations)"
```

### Task 10: Require `complete_plan_step` evidence and bounded attempts

**Files:**
- Modify: `src/multiclaw/planner/models.py:1-560`
- Modify: `src/multiclaw/planner/execution.py`
- Modify: `src/multiclaw/agent/multiclaw.py:798-876`
- Modify: `src/multiclaw/workflow/continuation.py:1-340`
- Modify: `src/multiclaw/storage/repositories/plans.py`
- Modify: `tests/test_plan_execution.py`

- [ ] **Step 1: Add failing completion-protocol and retry-budget tests**

```python
from multiclaw.llm import LLMResponse, ToolCall


@pytest.mark.asyncio
async def test_free_text_cannot_complete_a_plan_step(step_runner_fixture):
    step_runner_fixture.router.responses = [
        LLMResponse(content="Done", tool_calls=[]),
        LLMResponse(content="Still done", tool_calls=[]),
    ]

    outcome = await step_runner_fixture.run()

    assert outcome.status == "failed"
    assert outcome.retryable is False
    assert await step_runner_fixture.count_succeeded_attempts() == 0


@pytest.mark.asyncio
async def test_valid_completion_is_persisted_before_step_success(step_runner_fixture):
    step_runner_fixture.router.responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(
                id="complete-1",
                name="complete_plan_step",
                arguments={
                    "status": "succeeded",
                    "summary": "The schema contract passes.",
                    "evidence": ["pytest tests/test_migrations.py: pass"],
                    "retryable": False,
                },
            )],
        )
    ]

    outcome = await step_runner_fixture.run()
    attempt = await step_runner_fixture.latest_attempt()
    document = await step_runner_fixture.load_result(attempt.result_ref)

    assert outcome.status == "succeeded"
    assert attempt.status is PlanStepRunStatus.SUCCEEDED
    assert attempt.result_digest == document.digest()
    assert document.definition_digest == step_runner_fixture.step.definition_digest
    assert document.dependency_result_digests == step_runner_fixture.dependency_digests


@pytest.mark.asyncio
async def test_retryable_completion_creates_only_bounded_attempts(step_runner_fixture):
    step_runner_fixture.set_completions([
        PlanStepCompletion(status="failed", summary="Transient", evidence=[], retryable=True),
        PlanStepCompletion(status="failed", summary="Transient", evidence=[], retryable=True),
        PlanStepCompletion(status="succeeded", summary="Late success", evidence=[]),
    ])

    outcome = await step_runner_fixture.execute_to_boundary(max_attempts=2)

    assert outcome.state == "replan_required"
    assert await step_runner_fixture.attempt_statuses() == [
        PlanStepRunStatus.FAILED_RETRYABLE,
        PlanStepRunStatus.FAILED_TERMINAL,
    ]
    assert step_runner_fixture.runner_calls == 2
```

Add these protocol-boundary tests:

```python
@pytest.mark.asyncio
async def test_malformed_completion_uses_only_bounded_repairs(step_runner_fixture):
    step_runner_fixture.set_malformed_completions(count=10)
    outcome = await step_runner_fixture.run()

    assert outcome.status == "failed"
    assert step_runner_fixture.invalid_completion_repairs == (
        step_runner_fixture.settings.agent.reflection_max_attempts
    )
    assert step_runner_fixture.router_calls <= (
        step_runner_fixture.settings.agent.reflection_max_attempts + 1
    )


@pytest.mark.asyncio
async def test_ordinary_tool_result_returns_to_step_completion(step_runner_fixture):
    step_runner_fixture.set_tool_then_completion("read_file", PlanStepCompletion(
        status="succeeded", summary="Inspected the file", evidence=["read_file result"]
    ))
    outcome = await step_runner_fixture.run()

    assert outcome.status == "succeeded"
    assert step_runner_fixture.dispatched_tools == ["read_file"]


def test_internal_completion_protocol_is_not_a_registered_tool(step_runner_fixture):
    assert step_runner_fixture.registry.get("complete_plan_step") is None


@pytest.mark.asyncio
async def test_tool_approval_keeps_run_and_attempt_resumable(step_runner_fixture):
    outcome = await step_runner_fixture.run_until_tool_approval()
    run = await step_runner_fixture.load_run()
    attempt = await step_runner_fixture.latest_attempt()

    assert outcome.state == "awaiting_user"
    assert run.status is RunStatus.AWAITING_USER
    assert attempt.status is PlanStepRunStatus.RUNNING
    assert (await step_runner_fixture.latest_checkpoint()).phase == CheckpointPhase.AWAITING_APPROVAL.value
```

- [ ] **Step 2: Run the completion slice and observe free-text false positives**

```bash
uv run pytest tests/test_plan_execution.py -k 'completion or retryable or free_text or approval' -q
```

Expected: FAIL because the current agent treats no-tool free text as terminal success and has no internal completion schema.

- [ ] **Step 3: Add an internal completion schema and structured result document**

Define the internal schema from `PlanStepCompletion.model_json_schema()` and append it after tenant tool schemas only for `run_plan_step()`. Intercept it before `_execute_tool_batch`; require exactly one completion call in a model turn, validate its arguments, and never register it in `ToolRegistry`.

Build the bounded step context exactly from persisted facts:

```python
def build_plan_step_messages(request: PlanStepExecutionRequest, system_prompt: str) -> list[dict]:
    context = {
        "objective": request.plan.current.objective,
        "constraints": list(request.plan.current.constraints),
        "step": {
            "logical_step_key": request.step.logical_step_key,
            "title": request.step.title,
            "description": request.step.description,
            "expected_outcome": request.step.expected_outcome,
        },
        "dependencies": [result.public_context() for result in request.dependency_results],
        "attempt": request.step_run.attempt,
        "remaining_attempts": request.step.max_attempts - request.step_run.attempt,
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": "Execute exactly one Plan step and finish with complete_plan_step."},
        {"role": "user", "content": json.dumps(context, sort_keys=True, ensure_ascii=False)},
    ]
```

`MultiClawAgent.run_plan_step()` uses the normal registry schemas, skill prompts, `_execute_tool_batch`, approvals, recovery continuation, and `settings.agent.max_tool_rounds`. A no-tool response appends a repair instruction; after `reflection_max_attempts + 1` invalid terminal responses, return `PlanStepCompletion(status="failed", summary="Step completion protocol was not satisfied", evidence=[], retryable=False)`.

Before marking success, save this document through `WorkflowContinuationService` on the same UoW connection:

```python
class PlanStepResultDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    plan_id: str
    plan_version: int
    run_id: str
    step_id: str
    step_run_id: str
    attempt: int
    status: Literal["succeeded", "failed"]
    summary: str
    evidence: list[str]
    definition_digest: str
    dependency_result_digests: dict[str, str]
    tool_catalog_digest: str
    policy_digest: str
    skill_set_digest: str

    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()
```

Save it as a `MemoryEntry(type="plan_step_result", role="assistant", session_id=context.session_id, metadata={"schema_version": 1, "plan_id": request.plan.plan_id, "step_run_id": request.step_run.step_run_id})`; use `result_ref=f"memory:{entry.id}"`. Populate the three compatibility digests from deterministic, redacted tool schemas, sandbox/governance settings, and sorted active skill names. Redact summary/evidence before persistence and again before API/SSE. In one UoW, finish the step-run through `PlanRepository` and write `PLAN_STEP_READY(execution_cursor="select_next")` through a connection-bound `WorkflowCoordinator`; neither repository writes the other domain's tables directly.

Persist the same bounded document with `status="failed"` for failed attempts so recovery, audit, and total-round accounting do not depend on process memory; failure rows may therefore carry a result ref/digest as well as the redacted error fields.

For failure, persist redacted `error_code` and `error_detail_redacted`. If retryable and budget remains, finish the attempt as `failed_retryable` then select a new attempt. If the budget is exhausted, use `failed_terminal` and return `replan_required`; never choose another dependent step.

- [ ] **Step 4: Run completion plus agent-loop regressions**

```bash
uv run pytest tests/test_plan_execution.py tests/test_agent.py tests/test_tool_batch.py \
  tests/test_workflow_continuation.py -q
```

Expected: PASS; ordinary direct chat behavior remains unchanged, Plan free text cannot advance state, successful completion has a verified result document, and retries stop at the configured/step minimum.

- [ ] **Step 5: Commit the evidence boundary**

```bash
git add src/multiclaw/planner/models.py src/multiclaw/planner/execution.py src/multiclaw/agent/multiclaw.py \
  src/multiclaw/workflow/continuation.py src/multiclaw/storage/repositories/plans.py \
  tests/test_plan_execution.py
git commit -m "Require evidence-bearing completion before advancing a plan" \
  -m "Treat complete_plan_step as an internal protocol and persist its bounded result document before a step can succeed." \
  -m "Constraint: Free-form assistant text alone never marks a Plan step successful" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_plan_execution.py tests/test_agent.py tests/test_tool_batch.py tests/test_workflow_continuation.py -q"
```

### Task 11: Replan terminal failures and reuse results only with a complete proof

**Files:**
- Modify: `src/multiclaw/planner/execution.py`
- Modify: `src/multiclaw/planner/service.py`
- Modify: `src/multiclaw/storage/repositories/plans.py`
- Modify: `src/multiclaw/workflow/coordinator.py:314-399`
- Modify: `tests/test_plan_execution.py`
- Modify: `tests/test_plan_repository.py`

- [ ] **Step 1: Add failing failure-revision and reuse-proof tests**

```python
@pytest.mark.asyncio
async def test_exhausted_step_checkpoints_then_materializes_waiting_revision(replan_fixture):
    await replan_fixture.fail_current_step(retryable=True, attempts=2)

    outcome = await replan_fixture.coordinator.execute(
        runtime=replan_fixture.runtime,
        context=replan_fixture.context,
        run_lease_handle=replan_fixture.lease_handle,
    )
    run = await replan_fixture.load_run()
    checkpoints = await replan_fixture.load_checkpoints()

    assert outcome.state == "awaiting_user"
    assert [item.phase for item in checkpoints[-2:]] == [
        CheckpointPhase.PLAN_REPLAN_REQUIRED.value,
        CheckpointPhase.PLAN_AWAITING_APPROVAL.value,
    ]
    assert run.status is RunStatus.AWAITING_USER
    assert run.active_plan_version == 1
    assert outcome.plan.current_version == 2
    assert outcome.plan.approved_version == 1


@pytest.mark.parametrize(
    "change",
    ["definition", "dependency_definition", "dependency_result", "policy", "missing_proof"],
)
@pytest.mark.asyncio
async def test_incomplete_reuse_proof_forces_execution(reuse_fixture, change):
    await reuse_fixture.prepare_revision(change=change)
    await reuse_fixture.approve_revision()

    next_step = await reuse_fixture.coordinator.start_next_attempt(
        context=reuse_fixture.context,
        lease=reuse_fixture.current_lease,
    )

    assert next_step.step.logical_step_key == reuse_fixture.changed_or_unproven_key(change)
    assert next_step.step_run.reused_from_step_run_id is None


@pytest.mark.asyncio
async def test_complete_reuse_proof_creates_explicit_succeeded_attempt(reuse_fixture):
    source = await reuse_fixture.prepare_compatible_revision()
    await reuse_fixture.approve_revision()

    await reuse_fixture.coordinator.apply_compatible_reuse(
        context=reuse_fixture.context,
        lease=reuse_fixture.current_lease,
    )
    reused = await reuse_fixture.latest_revised_attempt(source.logical_step_key)

    assert reused.status is PlanStepRunStatus.SUCCEEDED
    assert reused.reused_from_step_run_id == source.step_run_id
    assert reused.result_digest == source.result_digest
```

Add the terminal failure guards:

```python
@pytest.mark.asyncio
async def test_revision_limit_fails_closed_without_an_extra_version(replan_fixture):
    await replan_fixture.seed_revision_count(replan_fixture.settings.planning.max_revisions)
    before = await replan_fixture.count_versions()
    outcome = await replan_fixture.replan_failed_step()

    assert outcome.state == "failed_terminal"
    assert await replan_fixture.count_versions() == before
    assert (await replan_fixture.load_run()).status is RunStatus.FAILED_TERMINAL


@pytest.mark.asyncio
async def test_failed_revision_generation_terminates_without_more_execution(replan_fixture):
    replan_fixture.generator.fail_twice()
    dispatches = replan_fixture.tool_dispatch_count
    outcome = await replan_fixture.replan_failed_step()

    assert outcome.state == "failed_terminal"
    assert replan_fixture.tool_dispatch_count == dispatches
    assert await replan_fixture.count_versions() == 1


@pytest.mark.asyncio
async def test_revision_cannot_omit_failed_logical_work(replan_fixture):
    omitted = replan_fixture.draft_without_failed_step_or_superseder()
    replan_fixture.generator.next_draft = omitted

    with pytest.raises(PlanGenerationError, match="failed step must be retained or superseded"):
        await replan_fixture.replan_failed_step()
    assert await replan_fixture.count_versions() == 1
    assert replan_fixture.tool_dispatch_count == 0
```

- [ ] **Step 2: Run replan/reuse tests and observe missing boundary logic**

```bash
uv run pytest tests/test_plan_execution.py -k 'replan or reuse or exhausted' -q
```

Expected: FAIL because terminal attempts do not produce a Plan checkpoint/version and revised steps have no compatibility proof.

- [ ] **Step 3: Implement checkpoint-first replanning and proof-based reuse**

When an attempt exhausts recovery, call `WorkflowCoordinator.checkpoint()` with `PLAN_REPLAN_REQUIRED` before a generator call. The failure digest is SHA-256 over `{step_run_id,error_code,error_detail_redacted}`; no raw tool payload enters the checkpoint.

Build `PlanRevisionContext` from immutable current definitions plus redacted successful summaries/digests. Validate the generated revision, require every failed logical key to be present or explicitly superseded, then call `PlanningService.materialize_failure_revision()` to append `N+1`, set Plan status `awaiting_approval`, transition the run `running -> awaiting_user`, and write `PLAN_AWAITING_APPROVAL` in one transaction. Keep the run's active version at `N` until approval.

Implement reuse as a pure proof function:

```python
@dataclass(frozen=True, slots=True)
class DependencyReuseProof:
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
    current_tool_catalog_digest: str,
    current_policy_digest: str,
    current_skill_set_digest: str,
) -> bool:
    if source_step is None or source_run is None or source_result is None:
        return False
    if source_run.run_id != current_run_id or source_run.status is not PlanStepRunStatus.SUCCEEDED:
        return False
    if new_step.supersedes_step_id != source_step.step_id:
        return False
    if new_step.definition_digest != source_step.definition_digest:
        return False
    if (
        source_result.run_id != current_run_id
        or source_result.step_id != source_step.step_id
        or source_result.step_run_id != source_run.step_run_id
        or source_result.definition_digest != source_step.definition_digest
        or source_run.result_digest != source_result.digest()
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
        expected = source_result.dependency_result_digests[predecessor_key]
        if proof.new_step.supersedes_step_id != proof.source_step.step_id:
            return False
        if proof.new_step.definition_digest != proof.source_step.definition_digest:
            return False
        if proof.source_run.run_id != current_run_id:
            return False
        if proof.source_run.status is not PlanStepRunStatus.SUCCEEDED:
            return False
        if proof.source_result.step_run_id != proof.source_run.step_run_id:
            return False
        if proof.source_run.result_digest != expected or proof.source_result.digest() != expected:
            return False
        if proof.reused_run.reused_from_step_run_id != proof.source_run.step_run_id:
            return False
        if proof.reused_run.status is not PlanStepRunStatus.SUCCEEDED:
            return False
        if proof.reused_run.result_digest != expected:
            return False
    return True
```

Resolve `dependency_proofs` from the revised step's actual dependency IDs; its keys are predecessor logical keys because those are the keys stored in `dependency_result_digests`. The exact digest comparisons make reuse fail when the tool catalog, sandbox/governance policy, or active skill set changed. If the current code has no stable version for one of those inputs, record a deterministic digest of the redacted tool schemas/policy/skill names at attempt start; absence always forces rerun.

For every reusable step, create a new succeeded attempt under the revised version with `reused_from_step_run_id`, copied result ref/digest, and a fresh attempt CAS row. Do not rewrite the source row. Apply reuse in stable topological order so dependencies are proven before dependents.

- [ ] **Step 4: Run replan, reuse, and immutable-version tests**

```bash
uv run pytest tests/test_plan_execution.py tests/test_plan_repository.py -k 'replan or reuse or revision or immutable' -q
```

Expected: PASS; definition/dependency/policy changes force work, exact proofs create explicit reused rows, revision limits fail closed, and every terminal failure ends at waiting review or terminal run state.

- [ ] **Step 5: Commit failure-driven revision semantics**

```bash
git add src/multiclaw/planner/execution.py src/multiclaw/planner/service.py \
  src/multiclaw/storage/repositories/plans.py src/multiclaw/workflow/coordinator.py \
  tests/test_plan_execution.py tests/test_plan_repository.py
git commit -m "Preserve valid completed work without trusting stale dependencies" \
  -m "Checkpoint exhausted failures before revision and require definition, dependency-result, and policy compatibility for explicit reuse rows." \
  -m "Constraint: A failed step is never silently skipped" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_plan_execution.py tests/test_plan_repository.py -k 'replan or reuse or revision or immutable' -q"
```

### Task 12: Persist cancellation and observe it at every side-effect boundary

**Files:**
- Modify: `src/multiclaw/workflow/coordinator.py:141-202`
- Modify: `src/multiclaw/storage/repositories/workflow.py:235-390`
- Modify: `src/multiclaw/planner/execution.py`
- Modify: `src/multiclaw/agent/multiclaw.py:798-876`
- Modify: `src/multiclaw/agent/tool_batch.py:1-260`
- Modify: `tests/test_plan_execution.py`
- Modify: `tests/test_workflow_state.py`

- [ ] **Step 1: Add failing cancellation-boundary tests**

```python
@pytest.mark.parametrize(
    "boundary",
    ["before_model", "before_tool", "before_retry", "before_next_step"],
)
@pytest.mark.asyncio
async def test_persisted_cancel_stops_at_next_boundary(cancel_fixture, boundary):
    await cancel_fixture.pause_at(boundary)
    first = await cancel_fixture.coordinator.request_cancellation(cancel_fixture.context)
    second = await cancel_fixture.coordinator.request_cancellation(cancel_fixture.context)
    await cancel_fixture.release(boundary)

    outcome = await cancel_fixture.execution_task
    run = await cancel_fixture.load_run()

    assert first.cancel_requested_at is not None
    assert second.cancel_requested_at == first.cancel_requested_at
    assert outcome.state == "cancelled"
    assert run.status is RunStatus.CANCELLED
    assert cancel_fixture.calls_after(boundary) == 0


@pytest.mark.asyncio
async def test_waiting_plan_cancel_is_immediately_terminal(cancel_fixture):
    await cancel_fixture.make_waiting()
    result = await cancel_fixture.coordinator.request_cancellation(cancel_fixture.context)

    assert result.status is RunStatus.CANCELLED
    assert (await cancel_fixture.latest_checkpoint()).phase == CheckpointPhase.RUN_TERMINAL.value


@pytest.mark.asyncio
async def test_cancel_during_external_side_effect_keeps_observed_or_uncertain_semantics(cancel_fixture):
    await cancel_fixture.dispatch_non_idempotent_tool()
    await cancel_fixture.coordinator.request_cancellation(cancel_fixture.context)
    cancel_fixture.crash_before_observation()

    recovered = await cancel_fixture.recover()

    assert recovered.action is RecoveryAction.MARK_MANUAL_UNCERTAIN
    assert cancel_fixture.external_dispatch_count == 1
```

Add the immutable-history assertion:

```python
@pytest.mark.asyncio
async def test_cancellation_preserves_plan_versions_and_succeeded_attempts(cancel_fixture):
    await cancel_fixture.succeed_first_step()
    versions_before = await cancel_fixture.dump_plan_versions()
    succeeded_before = await cancel_fixture.dump_succeeded_step_runs()

    await cancel_fixture.coordinator.request_cancellation(cancel_fixture.context)
    await cancel_fixture.run_to_cancel_boundary()

    assert await cancel_fixture.dump_plan_versions() == versions_before
    assert await cancel_fixture.dump_succeeded_step_runs() == succeeded_before
```

- [ ] **Step 2: Run cancellation tests and observe unconsumed request state**

```bash
uv run pytest tests/test_plan_execution.py tests/test_workflow_state.py -k cancel -q
```

Expected: FAIL because `cancel_requested_at` is not mutated/read and the agent/tool loop has no boundary guard.

- [ ] **Step 3: Add idempotent request persistence and boundary guards**

Add `WorkflowCoordinator.request_cancellation(context)` with a connection-owned transaction. Lock the scoped run and:

- return unchanged terminal runs;
- transition `awaiting_user -> cancelled` and append `RUN_TERMINAL` atomically;
- for `running|resuming`, set `cancel_requested_at=COALESCE(cancel_requested_at, db_now)` and increment run CAS version;
- never clear the request or mutate Plan rows.

Add this check to `PlanExecutionCoordinator` before model, retry, next-step selection, and final-summary calls:

```python
async def raise_if_cancel_requested(self, context: TenantContext) -> None:
    run = await self._workflow.get_run(context)
    if run is None:
        raise PlanExecutionBlocked("run is missing")
    if run.cancel_requested_at is not None or run.status is RunStatus.CANCELLED:
        raise PlanCancellationRequested
```

Pass an async `before_dispatch` callback into `ToolBatchExecutor.execute()`. Invoke it immediately before each `scheduler.execute()` call, including each branch in parallel read-only mode. A cancellation detected after inputs are validated but before dispatch creates no tool execution. A request arriving after dispatch follows the existing observed-result or `manual_uncertain` recovery rule; do not claim the side effect was reversed.

Catch `PlanCancellationRequested` only at the outer Plan executor, cancel a running step attempt with its fence, call `finish_run_with_checkpoint(lease, RunStatus.CANCELLED)`, and emit one post-commit run-status event.

- [ ] **Step 4: Run cancellation and tool recovery regressions**

```bash
uv run pytest tests/test_plan_execution.py tests/test_workflow_state.py \
  tests/test_tool_batch.py tests/test_workflow_recovery.py -k 'cancel or uncertain or dispatch' -q
```

Expected: PASS with zero calls after each boundary, idempotent cancellation timestamps, unchanged successful history, and unchanged uncertainty semantics for in-flight external work.

- [ ] **Step 5: Commit durable cancellation**

```bash
git add src/multiclaw/workflow/coordinator.py src/multiclaw/storage/repositories/workflow.py \
  src/multiclaw/planner/execution.py src/multiclaw/agent/multiclaw.py \
  src/multiclaw/agent/tool_batch.py tests/test_plan_execution.py tests/test_workflow_state.py
git commit -m "Make cancellation survive disconnects and restarts" \
  -m "Persist one run request and check it immediately before every model, tool, retry, and step transition while preserving uncertain external outcomes." \
  -m "Constraint: Already-dispatched side effects cannot be represented as reversed" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_plan_execution.py tests/test_workflow_state.py tests/test_tool_batch.py tests/test_workflow_recovery.py -k 'cancel or uncertain or dispatch' -q"
```

### Task 13: Recover all Plan boundaries without duplicate external effects

**Files:**
- Modify: `src/multiclaw/workflow/recovery.py:97-240,447-930`
- Modify: `src/multiclaw/planner/execution.py`
- Modify: `src/multiclaw/runtime/models.py:1-120`
- Modify: `src/multiclaw/runtime/factory.py:244-284`
- Modify: `tests/test_workflow_recovery.py`
- Create: `tests/integration/test_plan_faults.py`

- [ ] **Step 1: Add the five failing crash-window and fail-closed tests**

```python
CRASH_WINDOWS = (
    "after_plan_commit_before_sse",
    "after_decision_commit_before_resume",
    "after_step_run_before_dispatch",
    "after_tool_completion_before_step_result",
    "after_step_success_before_next_selection",
)


@pytest.mark.parametrize("window", CRASH_WINDOWS)
@pytest.mark.asyncio
async def test_plan_recovery_does_not_duplicate_side_effect(plan_fault_harness, window):
    await plan_fault_harness.run_until_crash(window)

    await plan_fault_harness.restart_runtime_and_recover()
    await plan_fault_harness.run_to_boundary()

    assert all(count == 1 for count in plan_fault_harness.external_effects_by_idempotency_key.values())
    assert plan_fault_harness.duplicate_effect_count == 0
    assert await plan_fault_harness.invariants_hold()


@pytest.mark.parametrize("corruption", ["missing_plan", "foreign_scope", "digest_mismatch", "missing_step"])
@pytest.mark.asyncio
async def test_corrupt_plan_context_blocks_before_tools(plan_fault_harness, corruption):
    await plan_fault_harness.seed_recovery_checkpoint()
    await plan_fault_harness.corrupt(corruption)

    outcome = await plan_fault_harness.recovery.recover(
        plan_fault_harness.context, "runtime-recovered"
    )

    assert outcome.status in {RunStatus.BLOCKED_CORRUPT, RunStatus.BLOCKED_INCOMPATIBLE}
    assert outcome.executions_started == 0
    assert plan_fault_harness.external_effect_count == 0
```

The Plan-commit/SSE case asserts GET hydration recovers the reference without requiring replay. The decision-commit case kills the process with run status `resuming`; the recovery worker must continue the approved Plan exactly once.

- [ ] **Step 2: Run fault tests and observe Plan phases classified as incompatible**

```bash
uv run pytest tests/integration/test_plan_faults.py tests/test_workflow_recovery.py -k plan -q
```

Expected: FAIL because recovery cannot validate Plan digests, interpret Plan phases, or route a recovered tool result back into the Plan-step loop.

- [ ] **Step 3: Validate Plan context before every recovery action and route continuation**

Add a scoped loader used before `_classify()` whenever `run.plan_id` is non-null:

```python
async def _validate_plan_context(
    self,
    context: TenantContext,
    run: RunRecord,
    checkpoint: CheckpointRecord,
    phase: CheckpointPhase,
    payload: CheckpointPayload,
) -> PlanRecoveryContext:
    async with TenantUnitOfWork(self._database, context) as uow:
        plan = await uow.plans.for_context(context).get(run.plan_id)
        if plan is None:
            raise CorruptCheckpointError("missing scoped Plan")
        if run.active_plan_version is None or run.initial_plan_version is None:
            raise CorruptCheckpointError("incomplete Plan run binding")
        expected_version = getattr(payload, "plan_version", run.active_plan_version)
        version = next(
            (item for item in plan.versions if item.plan_version == expected_version),
            None,
        )
        if version is None:
            raise CorruptCheckpointError("missing Plan version")
        payload_digest = getattr(payload, "plan_digest", version.content_digest)
        if payload_digest != version.content_digest:
            raise CorruptCheckpointError("Plan digest mismatch")
        running = await uow.plans.for_context(context).running_step_attempt(context.run_id)
        return PlanRecoveryContext(plan=plan, version=version, running_step=running)
```

For an executing Plan, also require `plan.current_version == plan.approved_version == run.active_plan_version`; the approval checkpoint is the only phase allowed while the current version is not approved.

Classify using both phase and current durable run/Plan state:

- `PLAN_AWAITING_APPROVAL` with run `awaiting_user` and `approved_version != current_version` -> `AWAIT_PLAN_DECISION`, no lease/tool;
- `PLAN_AWAITING_APPROVAL` with run `resuming` and `current_version == approved_version == active_plan_version` -> `RESUME_PLAN_STEP`; this is the decision-commit-before-continuation window;
- `PLAN_STEP_READY` with cursor `dispatch_step|continue_step|select_next` -> `RESUME_PLAN_STEP`, acquire a recovery lease;
- `PLAN_STEP_READY` with cursor `final_summary` and run `awaiting_user` -> `AWAIT_USER`; only the summary-retry endpoint resumes it;
- `PLAN_STEP_READY` with cursor `final_summary` and run `running|resuming` -> `RESUME_PLAN_STEP` at the summary-only branch; never select or rerun a step;
- `PLAN_REPLAN_REQUIRED` -> `RESUME_PLAN_REVISION`, acquire a recovery lease;
- Plan-bound `RUN_STARTED` -> `RESUME_PLAN_STEP` at the selection cursor; this is the rerun-start-before-first-attempt window;
- existing model/tool phases inside a Plan -> existing recovery action plus `PlanRecoveryContext`.

If the latest phase is `EXECUTION_RESULT_OBSERVED`, reload the persisted tool result as today, but call `runtime.plan_execution.resume(runtime=runtime, context=context, run_lease_handle=run_lease_handle, recovery_outcome=outcome, recovered_tool_result=result, recovered_tool_input_json=input_json)` rather than `agent.resume_recovery()`. It reconstructs the running step from Plan facts, appends the known tool result, and continues toward `complete_plan_step`; it never dispatches that tool again.

If the step row already succeeded, select the next step from persisted attempts. If a Plan decision committed with `resuming`, transition to `running` under the recovered fence before selection. If `PLAN_REPLAN_REQUIRED` is latest and no `N+1` exists, rerun only side-effect-free generation from persisted failure context.

Extend worker discovery to include nonterminal Plan runs. Do not make the in-memory event router a recovery prerequisite; all post-restart UI convergence happens via GET APIs.

- [ ] **Step 4: Run the fault suite repeatedly and all workflow recovery tests**

```bash
for attempt in 1 2 3; do
  uv run pytest tests/integration/test_plan_faults.py -q || exit 1
done
uv run pytest tests/test_workflow_recovery.py tests/test_workflow_continuation.py -q
```

Expected: all five windows PASS in three runs, corrupt/foreign/digest-mismatched facts start zero tools, and existing direct-run recovery remains green.

- [ ] **Step 5: Commit Plan-aware recovery**

```bash
git add src/multiclaw/workflow/recovery.py src/multiclaw/planner/execution.py \
  src/multiclaw/runtime/models.py src/multiclaw/runtime/factory.py \
  tests/test_workflow_recovery.py tests/integration/test_plan_faults.py
git commit -m "Recover plan execution without replaying external side effects" \
  -m "Validate scoped immutable Plan facts before classifying checkpoints and resume through the Plan-step continuation when a run is bound to a Plan." \
  -m "Constraint: Missing, foreign, corrupt, or digest-mismatched Plan data blocks before tool dispatch" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/integration/test_plan_faults.py -q (3 iterations); uv run pytest tests/test_workflow_recovery.py tests/test_workflow_continuation.py -q"
```

### Task 14: Expose scoped Plan and Run APIs with streaming mutations

**Files:**
- Create: `src/multiclaw/api/plans.py`
- Create: `src/multiclaw/api/runs.py`
- Modify: `src/multiclaw/server.py:121-142`
- Modify: `src/multiclaw/planner/models.py:1-560`
- Modify: `src/multiclaw/planner/execution.py`
- Modify: `src/multiclaw/stream.py:9-155`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Add failing authenticated API, scope-hiding, conflict, and stream tests**

```python
def test_plan_and_run_gets_require_authenticated_session_scope(plan_api_fixture):
    owner = plan_api_fixture.owner_client
    foreign = plan_api_fixture.foreign_client
    plan = plan_api_fixture.plan

    assert owner.get(f"/api/sessions/{plan.session_id}/plans").status_code == 200
    assert owner.get(f"/api/plans/{plan.plan_id}?session_id={plan.session_id}").status_code == 200
    assert owner.get(f"/api/runs/{plan.run_id}?session_id={plan.session_id}").status_code == 200

    owner_unknown = owner.get(f"/api/plans/{uuid4()}?session_id={plan.session_id}")
    foreign_known = foreign.get(f"/api/plans/{plan.plan_id}?session_id={plan.session_id}")
    assert (owner_unknown.status_code, owner_unknown.json()) == (
        foreign_known.status_code, foreign_known.json()
    ) == (404, {"detail": "resource not found"})


def test_stale_plan_decision_returns_latest_summary(plan_api_fixture):
    response = plan_api_fixture.owner_client.post(
        f"/api/plans/{plan_api_fixture.plan.plan_id}/decision",
        json={
            "session_id": plan_api_fixture.plan.session_id,
            "decision_id": "stale-api",
            "plan_version": 1,
            "expected_version": 1,
            "action": "approve",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "plan_version_conflict"
    assert response.json()["detail"]["latest"]["current_version"] == 2


def test_approve_stream_emits_scoped_progress_and_executes_once(plan_api_fixture):
    with plan_api_fixture.owner_client.stream(
        "POST",
        f"/api/plans/{plan_api_fixture.plan.plan_id}/decision",
        json={
            "session_id": plan_api_fixture.plan.session_id,
            "decision_id": "approve-api",
            "plan_version": 1,
            "expected_version": 1,
            "action": "approve",
        },
    ) as response:
        chunks = decode_sse(response.iter_lines())

    assert response.status_code == 200
    assert chunks[0]["type"] == "data-run"
    assert any(item["type"] == "data-plan-decision" for item in chunks)
    assert any(item["type"] == "data-plan-step-status" for item in chunks)
    assert chunks[-1]["type"] == "finish"
    assert plan_api_fixture.tool_dispatch_count == 1
```

Add the remaining route contracts:

```python
def test_decision_actor_cannot_be_spoofed(plan_api_fixture):
    body = plan_api_fixture.decision_body("approve", decision_id="spoof")
    response = plan_api_fixture.owner_client.post(
        f"/api/plans/{plan_api_fixture.plan.plan_id}/decision",
        json={**body, "decided_by": plan_api_fixture.foreign_user_id},
    )
    assert response.status_code == 422


def test_revision_and_rejection_stream_terminal_review_facts(plan_api_fixture):
    revised = plan_api_fixture.post_decision(
        "revise", decision_id="revise-api", feedback="Cover both databases"
    )
    assert [part["type"] for part in revised] == [
        "data-run", "data-plan-revised", "data-plan-decision", "finish"
    ]
    latest = plan_api_fixture.get_plan()
    assert latest["current_version"] == 2
    assert latest["versions"][1]["revision_feedback"] == "Cover both databases"

    rejected_fixture = plan_api_fixture.fresh_waiting_plan()
    rejected = rejected_fixture.post_decision("reject", decision_id="reject-api")
    assert any(part["type"] == "data-plan-decision" for part in rejected)
    assert rejected_fixture.get_run()["status"] == "cancelled"
    assert rejected_fixture.tool_dispatch_count == 0


def test_rerun_is_single_winner_and_uses_the_same_approved_version(plan_api_fixture):
    plan_api_fixture.complete_approved_plan()
    responses = plan_api_fixture.post_two_reruns_concurrently()

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert plan_api_fixture.count_new_runs() == 1
    run = plan_api_fixture.only_new_run()
    assert run.initial_plan_version == run.active_plan_version == 1


def test_cancel_is_idempotent_and_run_get_returns_latest_attempts(plan_api_fixture):
    run_id = plan_api_fixture.start_running_plan()
    first = plan_api_fixture.post_run_action(run_id, "cancel")
    first_state = plan_api_fixture.get_run(run_id)
    second = plan_api_fixture.post_run_action(run_id, "cancel")
    second_state = plan_api_fixture.get_run(run_id)

    assert first.status_code == second.status_code == 200
    assert second_state["cancel_requested_at"] == first_state["cancel_requested_at"]
    assert second_state["attempts"] == plan_api_fixture.persisted_latest_attempts(run_id)


def test_summary_retry_creates_no_step_or_tool_attempt(plan_api_fixture):
    run_id = plan_api_fixture.seed_summary_wait()
    before = plan_api_fixture.count_step_and_tool_rows(run_id)
    chunks = plan_api_fixture.post_run_action(run_id, "summary/retry", decode=True)

    assert chunks[-1]["type"] == "finish"
    assert plan_api_fixture.count_step_and_tool_rows(run_id) == before
    assert plan_api_fixture.get_run(run_id)["status"] == "completed"
```

`post_two_reruns_concurrently()` uses two independently authenticated clients and a barrier so both requests reach the aggregate lock before either returns. The route tests compare persisted rows after the streams close; they never infer success from SSE alone.

- [ ] **Step 2: Run server API tests and observe missing routes**

```bash
uv run pytest tests/test_server.py -k 'plan_api or run_api or plan_decision or plan_scope' -q
```

Expected: FAIL with `404` for every Plan/Run route.

- [ ] **Step 3: Implement authenticated session reconstruction and uniform not-found responses**

Use request models that forbid extra fields:

```python
class SessionScopedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=36, max_length=36)


class PlanDecisionBody(SessionScopedRequest):
    decision_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    action: PlanDecisionAction
    feedback: str | None = Field(default=None, max_length=8_000)
```

For every route, derive `request_context = authenticated_context.for_session(session_id)`, build a UoW/PlanRepository with that context, and first verify the session through `uow.sessions.get(session_id)`. Return exactly `HTTPException(404, "resource not found")` for missing session, missing Plan/run, or any foreign scope. Never accept tenant/workspace/actor fields.

Serialize a redacted `PlanResponse` containing all immutable versions/dependencies/decisions plus run summaries/latest attempts. Serialize a `RunResponse` containing Plan binding, status, cancel timestamp, attempt state, final-summary availability, and no raw tool payload.

Decision action behavior:

- approve: acquire runtime, call `PlanningService.decide()` to atomically approve/activate/resume, commit, then stream `PlanExecutionCoordinator.execute()`;
- reject: atomically reject and cancel the waiting run, commit, stream decision/run-terminal parts, execute zero tools;
- revise: generate before the transaction, atomically append version/decision/checkpoint, commit, stream revised+decision parts, remain waiting;
- identical `decision_id`: return/stream the stored result and never start a second continuation;
- stale/conflicting input: JSON `409` with redacted latest Plan summary.

`POST /plans/{id}/runs` locks the scoped Plan aggregate, requires the current version be approved, and rejects with `409` when that Plan already has a nonterminal run. Under the same lock it creates one new Plan-bound run with `initial=active=current` plus the existing `RUN_STARTED` checkpoint, emits `data-run`, and executes it. This one-active-run invariant keeps failure-driven revision and the singular `active_run_id` API fact unambiguous; future sub-agent parallelism occurs inside that run. Plan-aware recovery maps the checkpoint to first-step selection, and the route never creates a Plan version.

When every step is succeeded, write `PLAN_STEP_READY(execution_cursor="final_summary")`, call the final model with objective plus persisted step summaries only, persist the assistant message, and finish the run. On summary failure, transition to `awaiting_user` with the final-summary checkpoint. `POST /runs/{id}/summary/retry` calls a dedicated coordinator `resume_waiting_summary_run()` run-CAS/fence method, verifies every step is still succeeded, and invokes only the summary call; it does not create a Plan decision, step attempt, or tool execution.

Use a shared streaming driver that closes runtime/lease resources on success, error, disconnect, and waiting states. Encode Plan events with the exact named data part, not nested only under `data-event`.

- [ ] **Step 4: Run API and workflow integration tests**

```bash
uv run pytest tests/test_server.py tests/test_plan_execution.py \
  -k 'plan or run or summary or scope' -q
```

Expected: PASS with uniform 404s, authenticated actors, one continuation per winning decision, SSE progress after approval, and summary retry performing zero step/tool dispatches.

- [ ] **Step 5: Commit the public Plan control plane**

```bash
git add src/multiclaw/api/plans.py src/multiclaw/api/runs.py src/multiclaw/server.py \
  src/multiclaw/planner/models.py src/multiclaw/planner/execution.py \
  src/multiclaw/stream.py tests/test_server.py
git commit -m "Hide plan and run existence outside authenticated session scope" \
  -m "Expose durable reads and streaming review/run actions while deriving every actor and tenant boundary from authenticated context." \
  -m "Constraint: Mutation streams are advisory; GET responses are authoritative" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_server.py tests/test_plan_execution.py -k 'plan or run or summary or scope' -q"
```

### Task 15: Route chat through planning policy and emit a durable Plan message part

**Files:**
- Modify: `src/multiclaw/api/chat.py:44-615`
- Modify: `src/multiclaw/stream.py:110-147`
- Modify: `src/multiclaw/agent/multiclaw.py:229-243,429-453`
- Modify: `src/multiclaw/runtime/factory.py:244-284`
- Modify: `src/multiclaw/planner/planner.py:1-8`
- Modify: `src/multiclaw/planner/__init__.py:1-60`
- Modify: `tests/test_server.py`
- Modify: `tests/test_planner.py`
- Modify: `tests/test_agent.py`

- [ ] **Step 1: Add failing chat-mode, prefix, atomic-reference, and no-tool tests**

```python
@pytest.mark.parametrize("mode", ["never", None])
def test_never_mode_preserves_direct_chat_and_creates_no_plan(chat_plan_fixture, mode):
    chat_plan_fixture.settings.planning.default_mode = PlanningMode.NEVER
    payload = {"message": "Answer directly", "planning_mode": mode} if mode else {"message": "Answer directly"}
    response = chat_plan_fixture.client.post("/api/chat", json=payload)

    assert response.status_code == 200
    assert chat_plan_fixture.count_plans() == 0
    assert chat_plan_fixture.direct_agent_calls == 1


def test_always_mode_persists_one_waiting_plan_and_plan_part(chat_plan_fixture):
    chunks = decode_chat_stream(chat_plan_fixture.client.post(
        "/api/chat",
        json={"message": "Inspect and change the repository", "planning_mode": "always"},
    ))
    plan = chat_plan_fixture.only_plan()
    run = chat_plan_fixture.only_run()

    assert plan.current_version == 1
    assert run.status is RunStatus.AWAITING_USER
    assert chat_plan_fixture.tool_dispatch_count == 0
    created = next(item for item in chunks if item["type"] == "data-plan-created")
    assert created.get("transient") is not True
    assert created["data"]["plan_id"] == plan.plan_id
    assert chunks[-1] == {"type": "finish", "finishReason": "tool-calls"}


def test_plan_prefix_forces_planning_and_is_removed_from_objective(chat_plan_fixture):
    chat_plan_fixture.client.post("/api/chat", json={"message": "plan:   Ship the change"})

    assert chat_plan_fixture.generator_objectives == ["Ship the change"]
    assert chat_plan_fixture.only_plan().trigger_mode is PlanTriggerMode.EXPLICIT


def test_explicit_always_generation_failure_never_calls_direct_agent(chat_plan_fixture):
    chat_plan_fixture.generator.fail_twice()
    response = chat_plan_fixture.client.post(
        "/api/chat", json={"message": "Do complex work", "planning_mode": "always"}
    )

    assert response.status_code == 200
    assert "error" in [item["type"] for item in decode_chat_stream(response)]
    assert chat_plan_fixture.direct_agent_calls == 0
    assert chat_plan_fixture.tool_dispatch_count == 0
```

Add the remaining chat boundary cases:

```python
@pytest.mark.parametrize(
    "classification, expected_plans, expected_direct_calls",
    [("plan", 1, 0), ("direct", 0, 1), ("failure", 0, 1)],
)
def test_auto_classification_routes_or_falls_back_without_partial_plan(
    chat_plan_fixture, classification, expected_plans, expected_direct_calls
):
    chat_plan_fixture.classifier.set_outcome(classification)
    response = chat_plan_fixture.client.post(
        "/api/chat", json={"message": "Handle this request", "planning_mode": "auto"}
    )

    assert response.status_code == 200
    assert chat_plan_fixture.count_plans() == expected_plans
    assert chat_plan_fixture.direct_agent_calls == expected_direct_calls


def test_disabled_explicit_planning_returns_unavailable_without_execution(chat_plan_fixture):
    chat_plan_fixture.settings.planning.enabled = False
    response = chat_plan_fixture.client.post(
        "/api/chat", json={"message": "Plan this", "planning_mode": "always"}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "planning is unavailable"}
    assert chat_plan_fixture.count_plans() == 0
    assert chat_plan_fixture.direct_agent_calls == 0
    assert chat_plan_fixture.tool_dispatch_count == 0


def test_plan_source_matches_persisted_user_message(chat_plan_fixture):
    chat_plan_fixture.client.post(
        "/api/chat", json={"message": "Inspect and verify", "planning_mode": "always"}
    )
    plan = chat_plan_fixture.only_plan()
    assert plan.source_message_id == chat_plan_fixture.only_user_message().id


@pytest.mark.parametrize("failure", ["plan", "run", "checkpoint", "reference"])
def test_materialization_failure_rolls_back_the_whole_waiting_boundary(chat_plan_fixture, failure):
    chat_plan_fixture.inject_materialization_failure(failure)
    response = chat_plan_fixture.client.post(
        "/api/chat", json={"message": "Inspect and verify", "planning_mode": "always"}
    )

    assert "error" in [item["type"] for item in decode_chat_stream(response)]
    assert chat_plan_fixture.count_plan_boundary_rows() == {
        "plans": 0, "versions": 0, "steps": 0, "runs": 0, "checkpoints": 0,
        "assistant_plan_references": 0,
    }
```

- [ ] **Step 2: Run chat tests and observe advisory prefix behavior**

```bash
uv run pytest tests/test_server.py tests/test_planner.py -k 'planning_mode or plan_prefix or plan_part or auto_classification' -q
```

Expected: FAIL because `ChatRequest` rejects/ignores `planning_mode`, `plan:` still short-circuits in the agent, and no durable Plan part is encoded.

- [ ] **Step 3: Normalize intent once and split model calls from write transactions**

Add:

```python
class ChatRequest(BaseModel):
    message: str | None = None
    session_id: str | None = None
    id: str | None = None
    messages: list[dict[str, Any]] | None = None
    planning_mode: PlanningMode | None = None


def normalize_planning_request(message: str, requested: PlanningMode | None, default: PlanningMode):
    stripped = message.lstrip()
    if stripped.lower().startswith("plan:"):
        objective = stripped[5:].strip()
        if not objective:
            raise HTTPException(status_code=422, detail="Plan objective is empty")
        return objective, PlanningMode.ALWAYS, PlanTriggerMode.EXPLICIT
    mode = requested or default
    trigger = PlanTriggerMode.EXPLICIT if requested is PlanningMode.ALWAYS else PlanTriggerMode.AUTOMATIC
    return message, mode, trigger
```

Refactor the route into these phases:

1. short UoW: resolve/create scoped session, touch it, save the user `MemoryEntry`, retain returned `id` and `turn_index`, commit;
2. no transaction: acquire tenant runtime, normalize prefix/mode, call policy, and if needed generate validated draft;
3. direct: create the normal run/checkpoint and invoke the unchanged agent stream with the already-persisted user turn;
4. plan: call `PlanningService.materialize_initial()` so Plan/run/approval checkpoint/assistant reference share one new transaction; publish/encode after commit; end stream with waiting state.

If an explicit/automatic Plan generation reaches its second invalid response or provider failure, create an unbound run and its `RUN_STARTED` plus `FAILED_TERMINAL` checkpoint in one short transaction, emit `data-run` followed by a redacted SSE error, and start zero tools. This satisfies the run terminal contract without persisting an incomplete Plan or disguising the request as direct execution.

Remove both `plan:` branches from `MultiClawAgent`; the agent never classifies or creates Plans. Remove the legacy `Planner` constructor from `RuntimeFactory` and instead assemble `PlanningPolicy`, `PlanGenerator`, `PlanningService`, and `PlanExecutionCoordinator` on `TenantRuntime` using the same tenant-scoped router/database/event router.

Add encoder helpers:

```python
@classmethod
def plan_part(cls, event_type: str, data: dict[str, Any], *, durable: bool = False) -> str:
    if event_type not in {
        "plan-created", "plan-revised", "plan-decision", "plan-step-status", "plan-run-status"
    }:
        raise ValueError("unsupported Plan event")
    return cls.data_part(
        f"data-{event_type}",
        redact(data),
        transient=not durable,
        part_id=(f"plan:{data['plan_id']}:v{data['plan_version']}" if durable else None),
    )
```

Only `plan-created` is durable in the assistant message/initial response. Every payload contains `schema_version`, all four scope IDs, `plan_id`, `plan_version`, `run_id`, and `aggregate_version`. Never emit until the UoW has committed.

- [ ] **Step 4: Run chat, SSE, direct-agent, and atomicity regressions**

```bash
uv run pytest tests/test_server.py tests/test_planner.py tests/test_agent.py \
  -k 'chat or plan or direct or stream' -q
```

Expected: PASS; never/direct paths create zero Plan rows, forced Plan paths execute zero tools before approval, source IDs match, the prefix is absent from the objective, and generation failure cannot fall through to execution.

- [ ] **Step 5: Remove the legacy mutable Planner and commit routing**

Delete `create_plan`, `approve`, and `summary` plus their obsolete tests. Keep `planner.py` as a documented compatibility re-export of `PlanGenerator` because the current package import surface includes `multiclaw.planner.planner`; update `planner/__init__.py` to export `PlanGenerator` directly.

```bash
git add src/multiclaw/api/chat.py src/multiclaw/stream.py \
  src/multiclaw/agent/multiclaw.py src/multiclaw/runtime/factory.py \
  src/multiclaw/planner/planner.py src/multiclaw/planner/__init__.py \
  tests/test_server.py tests/test_planner.py tests/test_agent.py
git commit -m "Make chat planning modes durable before execution begins" \
  -m "Normalize explicit intent at the API boundary, keep model calls outside transactions, and persist one waiting Plan reference before emitting it." \
  -m "Constraint: Explicit planning never falls back to the direct agent loop" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_server.py tests/test_planner.py tests/test_agent.py -k 'chat or plan or direct or stream' -q"
```

### Task 16: Rehydrate Plan message parts and delete Plan state leaf-to-root

**Files:**
- Modify: `src/multiclaw/storage/repositories/sessions.py:137-220`
- Modify: `src/multiclaw/api/sessions.py:121-164`
- Modify: `src/multiclaw/storage/repositories/deletions.py:614-669`
- Modify: `tests/test_scoped_repositories.py:194-232`
- Modify: `tests/test_deletion_worker.py:165-360`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Add failing hydration, compatibility, and orphan-removal tests**

```python
@pytest.mark.asyncio
async def test_session_messages_hydrate_plan_reference_metadata(plan_session_fixture):
    await plan_session_fixture.seed_plan_message()

    async with TenantUnitOfWork(plan_session_fixture.database, plan_session_fixture.context) as uow:
        messages = await uow.sessions.get_messages(plan_session_fixture.session_id)

    assistant = messages[-1]
    assert assistant["id"] == plan_session_fixture.reference_message_id
    assert assistant["content"] == ""
    assert assistant["parts"] == [{
        "type": "data-plan-created",
        "data": plan_session_fixture.reference.model_dump(mode="json"),
    }]


@pytest.mark.asyncio
async def test_legacy_message_without_parts_keeps_text_shape(plan_session_fixture):
    await plan_session_fixture.seed_legacy_messages()
    async with TenantUnitOfWork(plan_session_fixture.database, plan_session_fixture.context) as uow:
        messages = await uow.sessions.get_messages(plan_session_fixture.session_id)

    assert messages == [
        {"id": plan_session_fixture.user_message_id, "role": "user", "content": "Hello", "created_at": 1, "parts": []},
        {"id": plan_session_fixture.assistant_message_id, "role": "assistant", "content": "Hi", "created_at": 2, "parts": []},
    ]


@pytest.mark.asyncio
async def test_session_delete_removes_plan_and_workflow_rows_in_fk_order(plan_session_fixture):
    await plan_session_fixture.seed_complete_graph()
    async with TenantUnitOfWork(plan_session_fixture.database, plan_session_fixture.context) as uow:
        await uow.sessions.delete(plan_session_fixture.session_id)

    counts = await plan_session_fixture.count_session_rows()
    assert counts == {
        "agent_plan_step_runs": 0,
        "agent_plan_decisions": 0,
        "agent_plan_step_dependencies": 0,
        "agent_plan_steps": 0,
        "agent_plan_versions": 0,
        "agent_plans": 0,
        "execution_checkpoints": 0,
        "tool_executions": 0,
        "approval_requests": 0,
        "agent_runs": 0,
        "memory_entries": 0,
        "chat_sessions": 0,
    }
```

Add account-purge and foreign-session protection tests:

```python
@pytest.mark.parametrize("backend", ["sqlite", "mysql"])
@pytest.mark.asyncio
async def test_account_purge_removes_all_plan_tables(deletion_backend, backend):
    fixture = await deletion_backend(backend)
    await fixture.seed_complete_plan_graph()
    await fixture.purge_account_to_completion()

    assert await fixture.plan_table_counts() == {
        "agent_plan_step_runs": 0,
        "agent_plan_decisions": 0,
        "agent_plan_step_dependencies": 0,
        "agent_plan_steps": 0,
        "agent_plan_versions": 0,
        "agent_plans": 0,
    }
    assert await fixture.workspace_files() == []


@pytest.mark.asyncio
async def test_foreign_session_delete_cannot_touch_owner_plan(plan_session_fixture):
    before = await plan_session_fixture.dump_owner_plan_rows()
    async with TenantUnitOfWork(
        plan_session_fixture.database, plan_session_fixture.foreign_context
    ) as uow:
        await uow.sessions.delete(plan_session_fixture.session_id)

    assert await plan_session_fixture.dump_owner_plan_rows() == before
```

The MySQL parameter uses the repository's existing configured-or-skip fixture; only a missing `MULTICLAW_TEST_MYSQL_URL` may skip that case.

- [ ] **Step 2: Run hydration/deletion tests and observe metadata loss/FK failures**

```bash
uv run pytest tests/test_scoped_repositories.py tests/test_deletion_worker.py \
  tests/test_server.py -k 'plan and (message or delete or purge or hydrate)' -q
```

Expected: FAIL because session messages omit IDs/metadata, deletion does not know Plan tables, and Plan/run foreign keys block parent deletion.

- [ ] **Step 3: Parse only allowlisted message parts and delete exact scoped leaves first**

Extend `get_messages()` to select `memory_entries.id` and `metadata_json`. Parse with a helper that returns only valid Plan references:

```python
def _message_parts(metadata_json: object) -> list[dict[str, object]]:
    try:
        metadata = metadata_json if isinstance(metadata_json, dict) else json.loads(str(metadata_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    parts = metadata.get("parts", []) if isinstance(metadata, dict) else []
    if not isinstance(parts, list):
        return []
    accepted = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "data-plan-created":
            continue
        try:
            reference = PlanReference.model_validate(part.get("data"))
        except ValidationError:
            continue
        accepted.append({"type": "data-plan-created", "data": reference.model_dump(mode="json")})
    return accepted
```

Return `{id,role,content,created_at,parts}` from repository/API. Apply `redact()` to the serialized payload even though references contain no free-form Plan content.

In session delete, constrain every statement by tenant, workspace, and session. Use this order:

1. `agent_plan_step_runs`;
2. `execution_checkpoints`, `audit_logs`, `tool_executions`, `approval_requests`;
3. `agent_runs` (it references active/initial Plan versions);
4. `agent_plan_decisions`;
5. `agent_plan_step_dependencies`;
6. `agent_plan_steps`;
7. `agent_plan_versions`;
8. `agent_plans`;
9. `memory_entries`;
10. `chat_sessions`.

Use the same Plan leaf order in `DeletionWorkflowRepository.purge_account()` before its existing run/session/user deletes. Do not enable cascade deletes and do not delete workspace files until the database purge transaction has committed, preserving the existing deletion worker contract.

- [ ] **Step 4: Run SQLite/MySQL deletion and API hydration tests**

```bash
uv run pytest tests/test_scoped_repositories.py tests/test_deletion_worker.py \
  tests/test_server.py -k 'message or delete or purge or hydrate' -q
```

Expected: PASS on file-backed SQLite and on MySQL when configured, with no Plan orphans and unchanged legacy text-only session responses.

- [ ] **Step 5: Commit lifecycle integration**

```bash
git add src/multiclaw/storage/repositories/sessions.py src/multiclaw/api/sessions.py \
  src/multiclaw/storage/repositories/deletions.py tests/test_scoped_repositories.py \
  tests/test_deletion_worker.py tests/test_server.py
git commit -m "Make plan history follow the session lifecycle" \
  -m "Hydrate allowlisted Plan references from message metadata and remove Plan facts explicitly before their run, message, session, and account parents." \
  -m "Constraint: Referential cleanup remains explicit and non-cascading" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/test_scoped_repositories.py tests/test_deletion_worker.py tests/test_server.py -k 'message or delete or purge or hydrate' -q"
```

### Task 17: Add typed frontend Plan APIs and a version-aware authoritative store

**Files:**
- Modify: `frontend/src/lib/api.ts:4-283`
- Create: `frontend/src/lib/plan-store.ts`
- Modify: `frontend/src/App.tsx:29-295`
- Modify: `frontend/src/components/session/SessionProvider.tsx:53-104`

- [ ] **Step 1: Add compile-failing Plan imports at the integration points**

Add these imports/usages before creating the module:

```tsx
// frontend/src/App.tsx
import type { PlanDataPart } from "@/lib/api";
import { planStore } from "@/lib/plan-store";

// inside onData, before data-session handling
if (part.type.startsWith("data-plan-")) {
  planStore.acceptDataPart(part as PlanDataPart);
  return;
}
```

```tsx
// frontend/src/components/session/SessionProvider.tsx
import { planStore } from "@/lib/plan-store";

// after session messages are loaded and before chatStore.setMessages
await planStore.hydrateSession(sessionId, messages);
```

- [ ] **Step 2: Run TypeScript and observe the missing module**

```bash
cd frontend && npm run build
```

Expected: FAIL with `Cannot find module '@/lib/plan-store'` and missing Plan DTOs on `api.ts`.

- [ ] **Step 3: Define exact API DTOs and scoped streaming actions**

Add discriminated DTOs to `api.ts`:

```typescript
export type PlanStatus = "awaiting_approval" | "approved" | "rejected" | "archived";
export type PlanStepStatus = "pending" | "running" | "succeeded" | "failed_retryable" | "failed_terminal" | "cancelled";

export interface PlanReference {
  schema_version: 1;
  tenant_id: string;
  workspace_id: string;
  session_id: string;
  run_id: string;
  plan_id: string;
  plan_version: number;
  aggregate_version: number;
}

export type PersistedMessagePart = {
  type: "data-plan-created";
  data: PlanReference;
};

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at?: number;
  parts: PersistedMessagePart[];
}

export interface PlanStepView {
  step_id: string;
  logical_step_key: string;
  ordinal: number;
  title: string;
  description: string;
  expected_outcome: string;
  depends_on: string[];
  status: PlanStepStatus;
  attempt: number | null;
  result_summary: string | null;
  error_detail: string | null;
}

export interface PlanVersionView {
  plan_version: number;
  objective: string;
  constraints: string[];
  generation_reason: string;
  parent_version: number | null;
  revision_feedback: string | null;
  content_digest: string;
  steps: PlanStepView[];
}

export interface PlanView {
  schema_version: 1;
  plan_id: string;
  session_id: string;
  status: PlanStatus;
  current_version: number;
  approved_version: number | null;
  aggregate_version: number;
  active_run_id: string | null;
  run_status: string | null;
  cancel_requested_at: number | null;
  summary_retry_available: boolean;
  versions: PlanVersionView[];
  decisions: Array<{
    decision_id: string;
    plan_version: number;
    action: "approve" | "reject" | "revise";
    resulting_plan_version: number | null;
    created_at: number;
  }>;
}
```

Expose `planApi.list(sessionId)`, `planApi.get(sessionId,planId)`, `runApi.get(sessionId,runId)`. Mutations return `Response` after calling a shared authenticated/CSRF streaming fetch, not parsed JSON, because the caller consumes SSE:

```typescript
export type PlanDataPart = {
  type: `data-plan-${"created" | "revised" | "decision" | "step-status" | "run-status"}`;
  data: PlanReference & Record<string, unknown>;
};

async function streamMutation(path: string, body: object): Promise<Response> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": await ensureCsrfToken(),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await parseErrorResponse(response);
  return response;
}

export async function consumePlanAction(
  response: Response,
  onPart: (part: PlanDataPart) => void,
): Promise<void> {
  if (!response.body) throw new ApiError({ status: 502, message: "Missing action stream" });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  for (;;) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    if (done && buffer.trim()) {
      frames.push(buffer);
      buffer = "";
    }
    for (const frame of frames) {
      const line = frame.split(/\r?\n/).find((item) => item.startsWith("data: "));
      if (!line) continue;
      const payload = JSON.parse(line.slice(6)) as { type?: unknown; data?: unknown };
      if (payload.type === "error") {
        const errorText = (payload as { errorText?: unknown }).errorText;
        throw new Error(typeof errorText === "string" ? errorText : "Plan action failed");
      }
      if (payload.type === "finish") finished = true;
      if (typeof payload.type === "string" && payload.type.startsWith("data-plan-")) {
        onPart(payload as PlanDataPart);
      }
    }
    if (done) break;
  }
  if (!finished) throw new Error("Plan action stream ended before completion");
}
```

`planApi.decision/rerun` and `runApi.cancel/retrySummary` call `streamMutation`. Preserve the existing one-time CSRF invalidation/retry behavior by extracting its retry branch into a shared helper used by both JSON and streaming requests.

- [ ] **Step 4: Implement the external store with scope/version rejection**

Create `plan-store.ts`:

```typescript
type Listener = () => void;
type PlanStoreSnapshot = Readonly<{
  sessionId: string | null;
  plans: Readonly<Record<string, PlanView>>;
  loading: Readonly<Record<string, boolean>>;
  errors: Readonly<Record<string, string>>;
}>;

let snapshot: PlanStoreSnapshot = { sessionId: null, plans: {}, loading: {}, errors: {} };
const listeners = new Set<Listener>();
const refreshEpochs = new Map<string, number>();
let nextRefreshEpoch = 0;

function publish(next: PlanStoreSnapshot) {
  snapshot = next;
  listeners.forEach((listener) => listener());
}

async function refresh(reference: PlanReference): Promise<void> {
  if (snapshot.sessionId !== reference.session_id) return;
  const requestKey = `${reference.session_id}:${reference.plan_id}`;
  const epoch = ++nextRefreshEpoch;
  refreshEpochs.set(requestKey, epoch);
  publish({
    ...snapshot,
    loading: { ...snapshot.loading, [reference.plan_id]: true },
    errors: { ...snapshot.errors, [reference.plan_id]: "" },
  });
  try {
    const plan = await planApi.get(reference.session_id, reference.plan_id);
    if (snapshot.sessionId !== reference.session_id) return;
    if (refreshEpochs.get(requestKey) !== epoch) return;
    const latest = snapshot.plans[reference.plan_id];
    if (latest && latest.aggregate_version > plan.aggregate_version) return;
    publish({
      ...snapshot,
      plans: { ...snapshot.plans, [plan.plan_id]: plan },
      loading: { ...snapshot.loading, [plan.plan_id]: false },
      errors: { ...snapshot.errors, [plan.plan_id]: "" },
    });
  } catch (error) {
    if (snapshot.sessionId !== reference.session_id) return;
    if (refreshEpochs.get(requestKey) !== epoch) return;
    publish({
      ...snapshot,
      loading: { ...snapshot.loading, [reference.plan_id]: false },
      errors: { ...snapshot.errors, [reference.plan_id]: error instanceof Error ? error.message : "Plan refresh failed" },
    });
  }
}

export const planStore = {
  subscribe(listener: Listener) { listeners.add(listener); return () => listeners.delete(listener); },
  getSnapshot() { return snapshot; },
  reset(sessionId: string | null) {
    refreshEpochs.clear();
    publish({ sessionId, plans: {}, loading: {}, errors: {} });
  },
  async hydrateSession(sessionId: string, messages: Message[]) {
    this.reset(sessionId);
    const references = messages.flatMap((message) => message.parts ?? [])
      .filter((part): part is { type: "data-plan-created"; data: PlanReference } => part.type === "data-plan-created")
      .map((part) => part.data);
    await Promise.all(references.map(refresh));
  },
  acceptDataPart(part: PlanDataPart) {
    const reference = readPlanReference(part.data);
    if (!reference || reference.session_id !== snapshot.sessionId) return;
    const current = snapshot.plans[reference.plan_id];
    if (!current || reference.aggregate_version >= current.aggregate_version) void refresh(reference);
  },
  refresh,
};
```

`readPlanReference()` requires schema version 1 and every string/number field. Ignore foreign sessions, lower aggregate-version notifications, and unknown schema versions. Equal aggregate versions are valid for step/run facts stored outside the Plan aggregate and trigger a refetch; higher versions cover decisions/revisions, and a jump greater than one also refetches rather than applying a patch. The explicit `refresh()` method always performs the scoped GET even when its durable message reference is old, so a `409` handler can fetch the latest version. Per-Plan request epochs prevent an earlier equal-aggregate response from overwriting newer mutable run/step facts, while the aggregate comparison rejects older decision/version responses.

Update `Message` with `id` and typed `parts`. In session hydration preserve server IDs, convert each persisted part to AI SDK `{type:"data-plan-created",data}` alongside text only when content is non-empty, and reset `planStore` on auth/session reset. Set chat `planning_mode` from a single UI preference/default, initially omitted so the server default owns behavior.

- [ ] **Step 5: Run frontend lint and type/build gates**

```bash
cd frontend && npm run lint && npm run build
```

Expected: PASS with no unsafe `any`, no hook-order issues, no stale-session state after switching, and production assets generated through Vite rather than hand edits.

- [ ] **Step 6: Commit the authoritative client state boundary**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/plan-store.ts frontend/src/App.tsx \
  frontend/src/components/session/SessionProvider.tsx
git commit -m "Treat persisted APIs as the source of truth for live plan state" \
  -m "Use data parts only to trigger scoped, aggregate-version-aware refetches and rebuild Plan state from message references after every hydration." \
  -m "Constraint: The in-memory SSE router has no replay guarantee" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: cd frontend && npm run lint && npm run build"
```

### Task 18: Render and control the first-class assistant-ui Plan Card

**Files:**
- Create: `frontend/src/components/plan/PlanCard.tsx`
- Create: `frontend/src/components/plan/PlanStepList.tsx`
- Create: `frontend/src/components/plan/PlanDecisionControls.tsx`
- Modify: `frontend/src/components/assistant-ui/thread.tsx:1-130`
- Modify: `frontend/src/index.css`
- Modify: `src/multiclaw/static/**` (generated by `npm run build`; hashed asset names are build outputs)

- [ ] **Step 1: Register a compile-failing named assistant data renderer**

```tsx
import { makeAssistantDataUI } from "@assistant-ui/react";
import { PlanCard } from "@/components/plan/PlanCard";
import type { PlanReference } from "@/lib/api";

const PlanCreatedUI = makeAssistantDataUI<PlanReference>({
  name: "plan-created",
  render: ({ data }) => <PlanCard reference={data} />,
});

const planDataRenderer = <PlanCreatedUI />;
```

Insert `{planDataRenderer}` as the first child of the existing `ThreadPrimitive.Root`; keep the current `ChatStatusBar`, viewport, grouped parts, composer, and scroll controls unchanged.

- [ ] **Step 2: Run frontend build and observe missing Plan components**

```bash
cd frontend && npm run build
```

Expected: FAIL because `PlanCard` and its step/decision components do not exist.

- [ ] **Step 3: Implement Plan steps and accessible review controls**

Implement `PlanStepList` with semantic ordered content and automatic expansion:

```tsx
export function PlanStepList({ version }: { version: PlanVersionView }) {
  return (
    <ol className="plan-step-list" aria-label={`Plan version ${version.plan_version} steps`}>
      {version.steps.map((step) => {
        const active = step.status === "running" || step.status === "failed_retryable";
        return (
          <li key={step.step_id} className="plan-step" data-status={step.status}>
            <details open={active}>
              <summary>
                <span className="plan-step-index">{step.ordinal}</span>
                <span>{step.title}</span>
                <span className="plan-status" aria-label={`Status: ${step.status}`}>
                  {step.status.replaceAll("_", " ")}
                </span>
              </summary>
              <p>{step.description}</p>
              <p><strong>Expected:</strong> {step.expected_outcome}</p>
              {step.depends_on.length > 0 && <p><strong>Depends on:</strong> {step.depends_on.join(", ")}</p>}
              {step.result_summary && <p><strong>Result:</strong> {step.result_summary}</p>}
              {step.error_detail && <p role="alert"><strong>Error:</strong> {step.error_detail}</p>}
            </details>
          </li>
        );
      })}
    </ol>
  );
}
```

`PlanDecisionControls` owns local feedback/action/error state. Disable all controls while one request streams. Generate `decision_id` with `crypto.randomUUID()` once per submitted action and reuse it when retrying the same failed network request. On `ApiError.status===409`, refresh the Plan and announce “Plan changed; review the latest version.” Never send `decided_by`.

Approve/reject/revise call `planApi.decision()` then `consumePlanAction(response,planStore.acceptDataPart)`. Cancel, rerun, and summary retry use the corresponding run/plan API. Require nonblank feedback up to 8,000 characters, a confirmation step for reject, and `aria-live="polite"` for status updates.

Use this action core in `PlanDecisionControls.tsx`:

```tsx
export function PlanDecisionControls({
  plan,
  reference,
  allowDecision,
  allowRunControls,
}: {
  plan: PlanView;
  reference: PlanReference;
  allowDecision: boolean;
  allowRunControls: boolean;
}) {
  const [feedback, setFeedback] = useState("");
  const [pending, setPending] = useState<
    "approve" | "reject" | "revise" | "cancel" | "rerun" | "retry-summary" | null
  >(null);
  const [message, setMessage] = useState("");
  const retryDecision = useRef<{ fingerprint: string; decisionId: string } | null>(null);

  const submit = async (action: "approve" | "reject" | "revise") => {
    if (action === "revise" && !feedback.trim()) {
      setMessage("Revision feedback is required.");
      return;
    }
    if (action === "reject" && !window.confirm("Reject this Plan and cancel its waiting run?")) return;
    const submittedFeedback = action === "revise" ? feedback.trim() : null;
    const fingerprint = JSON.stringify([
      action, submittedFeedback, plan.current_version, plan.aggregate_version,
    ]);
    const decisionId = retryDecision.current?.fingerprint === fingerprint
      ? retryDecision.current.decisionId
      : crypto.randomUUID();
    retryDecision.current = { fingerprint, decisionId };
    setPending(action);
    setMessage("");
    try {
      const response = await planApi.decision(reference.session_id, plan.plan_id, {
        decision_id: decisionId,
        plan_version: plan.current_version,
        expected_version: plan.aggregate_version,
        action,
        feedback: submittedFeedback,
      });
      await consumePlanAction(response, planStore.acceptDataPart);
      await planStore.refresh(reference);
      retryDecision.current = null;
      setFeedback("");
      setMessage("Plan action completed.");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        retryDecision.current = null;
        await planStore.refresh(reference);
        setMessage("Plan changed; review the latest version.");
      } else {
        await planStore.refresh(reference);
        setMessage(error instanceof Error ? error.message : "Plan action failed.");
      }
    } finally {
      setPending(null);
    }
  };

  const streamRunAction = async (
    action: "cancel" | "rerun" | "retry-summary",
    request: () => Promise<Response>,
  ) => {
    setPending(action);
    setMessage("");
    try {
      await consumePlanAction(await request(), planStore.acceptDataPart);
      await planStore.refresh(reference);
      setMessage("Run action completed.");
    } catch (error) {
      await planStore.refresh(reference);
      setMessage(error instanceof Error ? error.message : "Run action failed.");
    } finally {
      setPending(null);
    }
  };

  const active = plan.active_run_id !== null && ["running", "resuming", "awaiting_user"].includes(plan.run_status ?? "");

  return (
    <div className="plan-controls">
      {allowDecision && <>
        <textarea
          value={feedback}
          maxLength={8000}
          disabled={pending !== null}
          aria-label="Revision feedback"
          onChange={(event) => setFeedback(event.target.value)}
        />
        <button disabled={pending !== null} onClick={() => void submit("approve")}>Approve</button>
        <button disabled={pending !== null} onClick={() => void submit("revise")}>Request revision</button>
        <button disabled={pending !== null} onClick={() => void submit("reject")}>Reject</button>
      </>}
      {allowRunControls && active && plan.active_run_id && (
        <button disabled={pending !== null} onClick={() => void streamRunAction("cancel", () => runApi.cancel(reference.session_id, plan.active_run_id!))}>Cancel run</button>
      )}
      {allowRunControls && plan.summary_retry_available && plan.active_run_id && (
        <button disabled={pending !== null} onClick={() => void streamRunAction("retry-summary", () => runApi.retrySummary(reference.session_id, plan.active_run_id!))}>Retry summary</button>
      )}
      {allowRunControls && plan.status === "approved" && !active && (
        <button disabled={pending !== null} onClick={() => void streamRunAction("rerun", () => planApi.rerun(reference.session_id, plan.plan_id))}>Run again</button>
      )}
      <p aria-live="polite">{message}</p>
    </div>
  );
}
```

- [ ] **Step 4: Implement authoritative PlanCard state, progress, and history**

```tsx
export function PlanCard({ reference }: { reference: PlanReference }) {
  const state = useSyncExternalStore(planStore.subscribe, planStore.getSnapshot);
  const [selectedVersion, setSelectedVersion] = useState(reference.plan_version);
  const plan = state.plans[reference.plan_id];
  const error = state.errors[reference.plan_id];

  useEffect(() => {
    if (state.sessionId === reference.session_id && !plan) void planStore.refresh(reference);
  }, [plan, reference, state.sessionId]);

  useEffect(() => {
    if (plan) setSelectedVersion(plan.current_version);
  }, [plan?.current_version]);

  if (state.sessionId !== reference.session_id) return null;
  if (!plan && error) return (
    <section className="plan-card" role="alert">
      <p>{error}</p>
      <button onClick={() => void planStore.refresh(reference)}>Retry loading Plan</button>
    </section>
  );
  if (!plan) return <section className="plan-card" aria-busy="true">Loading Plan…</section>;

  const version = plan.versions.find((item) => item.plan_version === selectedVersion)
    ?? plan.versions.find((item) => item.plan_version === plan.current_version)!;
  const succeeded = version.steps.filter((step) => step.status === "succeeded").length;
  const waiting = (
    plan.status === "awaiting_approval"
    && plan.run_status === "awaiting_user"
    && plan.active_run_id !== null
    && selectedVersion === plan.current_version
  );
  const currentView = selectedVersion === plan.current_version;

  return (
    <section className="plan-card" aria-label="Execution plan">
      <header>
        <div><span>Plan</span><span className="plan-status">{plan.status.replaceAll("_", " ")}</span></div>
        <label>Version
          <select value={version.plan_version} onChange={(event) => setSelectedVersion(Number(event.target.value))}>
            {plan.versions.map((item) => <option key={item.plan_version} value={item.plan_version}>v{item.plan_version}</option>)}
          </select>
        </label>
      </header>
      <h3>{version.objective}</h3>
      {plan.run_status && <p>Latest run: {plan.run_status.replaceAll("_", " ")}</p>}
      <progress value={succeeded} max={version.steps.length}>{succeeded}/{version.steps.length}</progress>
      <p>{succeeded} of {version.steps.length} steps succeeded</p>
      {version.constraints.length > 0 && <ul>{version.constraints.map((item) => <li key={item}>{item}</li>)}</ul>}
      {error && <p role="alert">{error}</p>}
      <PlanStepList version={version} />
      <PlanDecisionControls
        plan={plan}
        reference={reference}
        allowDecision={waiting}
        allowRunControls={currentView}
      />
    </section>
  );
}
```

Historical versions are read-only. Show cancel only for running/resuming/awaiting runs, retry summary only when `summary_retry_available`, and rerun only for an approved current Plan without an active nonterminal run. Use text plus color for every state, visible keyboard focus, responsive wrapping, and no graph library.

Add focused CSS under `.plan-card`, `.plan-step`, and `[data-status=...]`; do not edit generated `src/multiclaw/static` assets by hand.

- [ ] **Step 5: Run frontend static gates and manual browser scenarios**

```bash
cd frontend && npm run lint && npm run build
```

Expected: PASS.

Run `./start.sh`, then verify in a browser:

1. forced Plan appears inline and survives refresh;
2. approve streams one active step at a time;
3. revision creates a read-only v1 and active v2;
4. a stale open tab gets conflict feedback and refreshes;
5. reject runs zero tools;
6. cancel stops before the next boundary;
7. switching sessions and back reconstructs identical progress;
8. summary retry does not rerun steps;
9. rerun creates a new run and retains version history;
10. a legacy text-only session renders unchanged;
11. keyboard-only review controls and screen-reader labels are usable at mobile and desktop widths.

Stop the development processes with `./stop.sh` after verification.

- [ ] **Step 6: Commit the Plan Card**

```bash
git add frontend/src/components/plan/PlanCard.tsx \
  frontend/src/components/plan/PlanStepList.tsx \
  frontend/src/components/plan/PlanDecisionControls.tsx \
  frontend/src/components/assistant-ui/thread.tsx frontend/src/index.css \
  src/multiclaw/static
git commit -m "Let users review and control durable plans inline" \
  -m "Render persisted Plan references as first-class assistant data parts with accessible decisions, progress, history, cancellation, rerun, and summary recovery." \
  -m "Constraint: Historical versions are read-only and the first release has no graph editor" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: cd frontend && npm run lint && npm run build; manual Plan browser scenarios 1-11"
```

### Task 19: Complete quotas, observability, documentation, and deletion parity

**Files:**
- Modify: `src/multiclaw/planner/service.py`
- Modify: `src/multiclaw/planner/execution.py`
- Modify: `src/multiclaw/workflow/coordinator.py`
- Modify: `src/multiclaw/storage/repositories/workflow.py`
- Modify: `src/multiclaw/observability.py:15-54`
- Modify: `tests/test_plan_repository.py`
- Modify: `tests/test_plan_execution.py`
- Modify: `tests/test_secret_redaction.py`
- Modify: `tests/test_observability.py`
- Modify: `tests/test_deletion_worker.py`
- Create: `docs/durable-plans.md`
- Modify: `docs/configuration.md`
- Modify: `docs/api.md`
- Modify: `docs/architecture.md`
- Modify: `docs/testing.md`
- Modify: `docs/README.md`
- Modify: `scripts/check_docs.py:186-226`

- [ ] **Step 1: Add failing quota, redaction, metric-label, and docs tests**

```python
@pytest.mark.asyncio
async def test_plan_budgets_bound_every_growth_axis(quota_fixture):
    assert quota_fixture.max_active_plans == quota_fixture.settings.runtime.max_concurrent_runs_per_tenant
    with pytest.raises(TenantRunQuotaError):
        await quota_fixture.create_one_more_waiting_plan()
    with pytest.raises(PlanRevisionLimitError):
        await quota_fixture.create_revision(quota_fixture.settings.planning.max_revisions + 1)
    with pytest.raises(PlanAttemptLimitError):
        await quota_fixture.create_attempt(quota_fixture.settings.planning.max_step_attempts + 1)
    assert quota_fixture.max_total_rounds == (
        quota_fixture.settings.planning.max_steps
        * quota_fixture.settings.planning.max_step_attempts
        * quota_fixture.settings.agent.max_tool_rounds
    )
    await quota_fixture.consume_work_units(quota_fixture.max_total_rounds)
    with pytest.raises(PlanRoundBudgetExceeded):
        await quota_fixture.reserve_model_call(round_kind="step_model")


def test_plan_observability_rejects_identifier_labels():
    metrics = OperationalMetrics()
    metrics.increment(
        "multiclaw_plan_operations_total",
        labels={"operation": "step", "status": "succeeded", "error_class": "none"},
    )
    for key in ("plan_id", "run_id", "tenant_id", "session_id", "provider_name", "path"):
        with pytest.raises(InvalidMetricLabelError):
            metrics.increment("multiclaw_plan_operations_total", labels={key: "identifier"})


def test_plan_text_is_redacted_from_public_channels(plan_redaction_canary):
    canary = "Authorization: " + "Bearer durable-plan-canary"
    outputs = plan_redaction_canary.exercise(canary)
    assert all(canary not in output for output in outputs.logs)
    assert all(canary not in output for output in outputs.sse)
    assert all(canary not in output for output in outputs.audit)
    assert all(canary not in output for output in outputs.trace)
    assert all(canary not in output for output in outputs.checkpoints)


@pytest.mark.parametrize("deletion", ["session", "account"])
@pytest.mark.asyncio
async def test_plan_round_ledger_is_removed_by_lifecycle_deletion(plan_deletion_fixture, deletion):
    await plan_deletion_fixture.seed_round_entry(entry_type="plan_round_counter")
    assert await plan_deletion_fixture.count_round_entries() == 1

    await plan_deletion_fixture.delete(deletion)

    assert await plan_deletion_fixture.count_round_entries() == 0
```

Extend documentation-check required groups with `planning`, required routes with `/api/plans` and `/api/runs`, and require `docs/durable-plans.md`.

- [ ] **Step 2: Run governance/docs tests and observe missing events/docs**

```bash
uv run pytest tests/test_plan_repository.py tests/test_plan_execution.py \
  tests/test_secret_redaction.py tests/test_observability.py -k 'quota or plan or label or redaction' -q
uv run python scripts/check_docs.py
```

Expected: FAIL because Plan operation counters/traces, aggregate round budget, documentation, and docs-manifest requirements are not complete.

- [ ] **Step 3: Enforce bounded growth and emit only low-cardinality telemetry**

Count nonterminal Plan-bound runs through the existing tenant run quota; every active Plan owns at least one nonterminal run, so `runtime.max_concurrent_runs_per_tenant` is the active-Plan ceiling without a second quota setting. Enforce steps/depth/revisions/attempts from `PlanningSettings` at both validation and transactional insert boundaries.

For each Plan-bound run compute the fixed aggregate work-unit ceiling without adding a schema column:

```python
total_round_budget = (
    settings.planning.max_steps
    * settings.planning.max_step_attempts
    * settings.agent.max_tool_rounds
)
```

The ceiling is intentionally conservative: every post-materialization model request and every new `tool_executions` row consumes one unit from the shared run budget. Initial automatic classification is one call, and initial generation is at most two calls, before a Plan/run exists; those are bounded by Task 6 and are not charged to a nonexistent run. Revision generation (including its repair), step-model/reflection calls, final summary, and summary retries are charged because their run already exists.

Before each charged model request, lock the scoped run, count existing `plan_round_counter` entries plus scoped `tool_executions`, fail with `PlanRoundBudgetExceeded` when the next unit would exceed the ceiling, and persist this session-scoped ledger entry in that same short UoW. Commit before calling the model; never hold the transaction across inference:

```python
round_entry = MemoryEntry(
    content="model",
    type="plan_round_counter",
    role="system",
    session_id=context.session_id,
    turn_index=0,
    metadata={
        "schema_version": 1,
        "plan_id": plan.plan_id,
        "run_id": context.run_id,
        "step_run_id": step_run_id,
        "round_kind": round_kind,
    },
)
```

Use bounded `round_kind` values: `revision_generation`, `step_model`, `reflection`, and `final_summary`; `step_run_id` is null when the call is not inside an attempt. Pass a reservation callback to `PlanGenerator` for revisions so each initial/repair request is charged independently.

Count ledger and execution rows across every attempt and revision of the run. In the existing `WorkflowCoordinator` tool-execution creation transaction, lock the Plan-bound run and apply the same count before inserting a new execution row; replaying an existing `tool_call_id` consumes no new unit. This keeps a future parallel read-only batch from racing past the limit. The pre-call/model-ledger insert and pre-dispatch/tool-execution insert make crashes consume budget conservatively. Recovery always recomputes from persisted rows rather than trusting process memory. Session deletion and account purge remove these session-scoped ledger entries with other `memory_entries`, and the deletion regression above seeds one explicitly.

Use one counter family and bounded trace names:

```python
increment_metric(
    "multiclaw_plan_operations_total",
    labels={"operation": operation, "status": status, "error_class": error_class},
)
record_trace_event(
    f"plan_{operation}",
    attributes={
        "plan_id": plan_id,
        "run_id": run_id,
        "status": status,
        "error": redacted_error,
    },
)
```

Emit operations for classification direct/plan/failure, materialization, approve/reject/revise/conflict, step start/succeed/retry/terminal/reuse, recovery outcomes, replan, complete, and cancel. IDs are sanitized by `record_trace_event`; they are never labels.

- [ ] **Step 4: Document the exact public and operational contract**

`docs/durable-plans.md` must document triggering, review actions, immutable history, serial DAG behavior, Plan versus tool approval, cancellation limits for in-flight external effects, recovery/fail-closed states, result reuse proof, summary retry/rerun, configuration, SQLite/MySQL migration, rollout kill switch, metrics, and all manual browser scenarios.

Update API/configuration/architecture/testing indexes with exact payload fields, session-scope requirement, 404/409 semantics, SSE notification role, and release commands. Do not include example credentials, private paths, raw provider errors, or claims of parallel/sub-agent execution.

- [ ] **Step 5: Run governance, docs, and deletion regressions**

```bash
uv run pytest tests/test_plan_repository.py tests/test_plan_execution.py \
  tests/test_secret_redaction.py tests/test_observability.py tests/test_deletion_worker.py -q
uv run python scripts/check_docs.py
git diff --check
```

Expected: all tests PASS, documentation check prints `documentation check passed`, no secret canary crosses a public channel, no high-cardinality metric label is accepted, and diff check is silent.

- [ ] **Step 6: Commit governance and operations**

```bash
git add src/multiclaw/planner/service.py src/multiclaw/planner/execution.py \
  src/multiclaw/workflow/coordinator.py src/multiclaw/storage/repositories/workflow.py \
  src/multiclaw/observability.py tests/test_plan_repository.py tests/test_plan_execution.py \
  tests/test_secret_redaction.py tests/test_observability.py tests/test_deletion_worker.py docs/durable-plans.md \
  docs/configuration.md docs/api.md docs/architecture.md docs/testing.md docs/README.md \
  scripts/check_docs.py
git commit -m "Bound plan growth and make recovery operations diagnosable" \
  -m "Apply existing tenant concurrency plus explicit graph, revision, attempt, and total-round budgets while documenting low-cardinality operational signals." \
  -m "Constraint: Plan identifiers may appear only in redacted trace context" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/test_plan_repository.py tests/test_plan_execution.py tests/test_secret_redaction.py tests/test_observability.py tests/test_deletion_worker.py -q; uv run python scripts/check_docs.py; git diff --check"
```

### Task 20: Prove the dual-backend release gate before enabling automatic planning

**Files:**
- Modify: `multiclaw.toml:1-100`
- Modify: `config/multiclaw.toml:1-100`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/integration/test_mysql_contract.py`
- Modify: `tests/integration/test_plan_faults.py`
- Modify: `tests/test_server.py`
- Modify: `docs/durable-plans.md`

- [ ] **Step 1: Add final end-to-end release assertions while the kill switch remains active**

```python
@pytest.mark.parametrize("plan_e2e", ["sqlite", "mysql"], indirect=True)
@pytest.mark.asyncio
async def test_plan_release_journey_is_durable_and_scoped(plan_e2e):
    created = await plan_e2e.force_plan("Inspect, change, and verify")
    assert created.run_status == "awaiting_user"
    assert plan_e2e.tool_dispatch_count == 0

    revised = await plan_e2e.revise(created, "Verify both database backends")
    assert revised.current_version == 2
    assert revised.versions[0].content_digest == created.versions[0].content_digest

    approved = await plan_e2e.approve(revised)
    await plan_e2e.wait_terminal(approved.run_id)
    assert await plan_e2e.step_keys(approved.run_id) == await plan_e2e.stable_topological_keys(2)
    assert plan_e2e.max_simultaneous_steps == 1

    hydrated = await plan_e2e.rehydrate(created.session_id)
    assert hydrated.plan_id == created.plan_id
    assert hydrated.current_version == 2
    assert hydrated.decisions == await plan_e2e.persisted_decisions()

    rerun = await plan_e2e.rerun(hydrated)
    assert rerun.run_id != approved.run_id
    assert rerun.plan_version == 2
    assert await plan_e2e.foreign_client_sees(rerun) is False
    assert await plan_e2e.unexpected_duplicate_tool_rows() == 0
    assert await plan_e2e.duplicate_external_idempotency_keys() == []
```

The indirect fixture uses file-backed SQLite and the configured MySQL database, runs migrations before seeding, and skips only the MySQL case when `MULTICLAW_TEST_MYSQL_URL` is absent. Keep the Task 13 fault matrix in this release suite; the final two assertions query persisted `tool_executions` and the fake external provider after every injected restart.

- [ ] **Step 2: Run the release tests before changing defaults**

```bash
uv run pytest tests/integration/test_plan_faults.py tests/test_server.py \
  tests/integration/test_mysql_contract.py -q
```

Expected: PASS on SQLite; MySQL tests PASS when configured and otherwise report only the existing explicit environment skip. A local MySQL skip is acceptable while implementing earlier steps but is not release evidence: do not change the example default if any test fails or if the MySQL matrix has not produced a recorded pass.

- [ ] **Step 3: Extend CI with the Plan-specific release matrix**

In the backend SQLite and MySQL jobs, run migrations followed by the full suite, and retain Plan fault tests as named commands so their evidence is visible:

```yaml
- name: Durable Plan fault windows
  run: uv run pytest tests/integration/test_plan_faults.py -q
- name: Backend suite
  run: uv run pytest -q
```

Keep the frontend job on `npm ci`, `npm run lint`, and `npm run build`. Do not add a frontend test package solely for this release.

- [ ] **Step 4: Run complete local static, backend, docs, and frontend verification**

```bash
git diff --check
uv lock --check
uv run python -m compileall -q src tests
uv run pytest -q
uv run python scripts/check_docs.py
cd frontend && npm ci && npm run lint && npm run build
```

Expected: every command exits `0`; pytest has no unexpected skip/error/failure, docs prints `documentation check passed`, and Vite produces the static bundle.

With MySQL `>=8.0.36` configured, run:

```bash
MULTICLAW_DATABASE__DRIVER=mysql \
MULTICLAW_DATABASE__URL="$MULTICLAW_TEST_MYSQL_URL" \
MULTICLAW_TEST_MYSQL_URL="$MULTICLAW_TEST_MYSQL_URL" \
  uv run pytest -q
```

Expected: exit `0`, including migration parity, Plan concurrency, deletion, and all recovery windows.

- [ ] **Step 5: Request independent correctness and security review**

Use `superpowers:requesting-code-review` with the approved design and this plan. Require the reviewer to check scope hiding, transaction boundaries, fence/CAS predicates, checkpoint digests, idempotent decisions, result reuse proof, cancellation, redaction, deletion order, and SQLite/MySQL parity. Resolve every critical/high finding and rerun the affected focused suite before continuing.

- [ ] **Step 6: Repeat the manual browser gate against the release build**

Serve the built application and repeat Task 18 scenarios 1-11. Record browser, viewport, backend, Plan/run IDs in private test notes only; repository documentation records outcomes without identifiers. Require zero stale state after refresh/session switch and zero extra step/tool rows after retry/rerun scenarios.

- [ ] **Step 7: Enable automatic planning in deployment examples and rerun config/docs gates**

Only after Steps 2-6 pass with an actual MySQL `>=8.0.36` result (not a skip), change both example files from:

```toml
default_mode = "never"
```

to:

```toml
default_mode = "auto"
```

Then run:

```bash
uv run pytest tests/test_config.py tests/test_server.py -k 'planning or config or direct' -q
uv run python scripts/check_docs.py
git diff --check
```

Expected: PASS; clients omitting `planning_mode` use automatic classification, explicit `never` remains a compatibility escape path, and disabled explicit `always` remains fail-closed.

- [ ] **Step 8: Commit the release property**

```bash
git add multiclaw.toml config/multiclaw.toml .github/workflows/ci.yml \
  tests/integration/test_mysql_contract.py tests/integration/test_plan_faults.py \
  tests/test_server.py docs/durable-plans.md
git commit -m "Enable automatic planning only after durable execution is proven" \
  -m "Gate the default change on dual-backend schema, concurrency, recovery, deletion, API, frontend, and manual browser evidence." \
  -m "Constraint: planning_mode=never remains the operational escape path" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: git diff --check; uv lock --check; uv run python -m compileall -q src tests; uv run pytest -q; MySQL uv run pytest -q; uv run python scripts/check_docs.py; cd frontend && npm ci && npm run lint && npm run build; manual release browser scenarios 1-11" \
  -m "Not-tested: External side effects cannot be reversed after a provider has accepted them"
```

## Acceptance-criteria coverage

| # | Approved behavior | Implemented and proven by |
|---|---|---|
| 1 | `never` creates no Plan and preserves direct behavior | Tasks 1, 6, 15; `test_never_mode_preserves_direct_chat_and_creates_no_plan` |
| 2 | `always` atomically creates one scoped waiting Plan/version/part | Tasks 3, 4, 7, 15; initial materialization and rollback tests |
| 3 | `plan:` equals `always` after prefix removal | Task 15; `test_plan_prefix_forces_planning_and_is_removed_from_objective` |
| 4 | No execution tool before approval | Tasks 8, 9, 15; version-gate and forced-Plan tests |
| 5 | Approval resumes once in deterministic topological order | Tasks 5, 8, 9, 14; concurrency and approval-stream tests |
| 6 | Rejection cancels and runs zero tools | Tasks 5, 8, 14, 18; reject API/browser cases |
| 7 | Revision creates immutable `N+1`; stale `N` returns 409 | Tasks 4, 5, 7, 14; byte dump and stale API tests |
| 8 | Same `decision_id` returns the recorded result once | Tasks 5 and 14; decision retry test |
| 9 | Ten mixed decisions produce one winner | Task 5; repeated concurrent decision test on both backends |
| 10 | Only one step runs even with multiple ready nodes | Task 9; concurrent ready-node test |
| 11 | Valid `complete_plan_step` is required | Task 10; free-text and valid-payload tests |
| 12 | Retries are bounded; exhaustion replans without skipping | Tasks 10 and 11; retry-budget/replan tests |
| 13 | Reuse requires definition and dependency-result proof | Task 11; parameterized compatibility tests |
| 14 | Cancellation is persisted and checked at every boundary | Task 12; four-boundary and uncertainty tests |
| 15 | Five crash windows cause no duplicate external effect | Task 13; `CRASH_WINDOWS` integration matrix |
| 16 | Corrupt/missing/foreign/digest mismatch blocks recovery | Task 13; parameterized corrupt-context test |
| 17 | Foreign/unknown Plan/run API resources are indistinguishable | Task 14; exact response-equality test |
| 18 | Session/account deletion leaves no Plan rows/files | Task 16; session delete and dual-backend purge tests |
| 19 | Refresh/session switch reconstructs identical state | Tasks 16-18; API hydration and browser scenarios |
| 20 | Sessions without Plan parts still render | Tasks 16 and 18; legacy message/API/browser test |
| 21 | SQLite/MySQL migrations and constraints match | Tasks 3, 5, 16, 20; migration/MySQL contract matrix |
| 22 | Backend/docs/frontend/manual gates pass before default `auto` | Tasks 19 and 20; ordered release gate and final config change |

## Final self-review checklist

- [x] Every design section maps to at least one task and every acceptance criterion maps to a named test or release scenario.
- [x] `Plan` owns immutable definitions/review while `WorkflowCoordinator` exclusively mutates run/lease/fence/checkpoint/tool execution state outside explicit lifecycle deletion.
- [x] Initial materialization and every decision/revision transition have a single explicit transaction boundary.
- [x] No repository/API path authorizes by Plan/run ID without authenticated tenant/workspace plus client-provided session scope.
- [x] All executing Plan/step writes use current fence and CAS; review writes use aggregate/run CAS and idempotency keys.
- [x] Plan versions/steps/dependencies are immutable; mutable state exists only in aggregate pointers, decisions, runs, and attempts.
- [x] Canonical serialization, definition digests, dependency-result digests, checkpoint hashes, and reuse comparison use consistent field names.
- [x] Generation and classification receive only their dedicated function schema and never the real tool registry.
- [x] `complete_plan_step` is intercepted internally and never dispatched as a registered tool.
- [x] SSE emission is post-commit and advisory; frontend hydration/refetch uses scoped GET APIs.
- [x] Rejection, cancellation, summary retry, rerun, tool approval, and restart continuation each have one unambiguous owner.
- [x] Session/account deletion order accounts for `agent_runs` references to Plan versions and leaves no Plan result memory/files.
- [x] No new dependency is introduced; frontend verification remains lint/build plus documented browser scenarios.
- [x] Example configuration stays `never` until both database matrices and all release evidence pass, then changes once to `auto`.
- [x] Task 20 makes independent correctness/security review with zero unresolved critical/high findings an explicit release gate.

## Implementation handoff

Use one of these execution modes after this plan is approved:

1. **Subagent-Driven (recommended):** use `superpowers:subagent-driven-development`, dispatch a fresh implementation agent per task, and run requirements review followed by code-quality review before accepting each task.
2. **Inline Execution:** use `superpowers:executing-plans`, execute tasks in ordered batches with review checkpoints after Tasks 3, 8, 13, 16, and 20.

Tasks are ordered by dependency. Do not start Task 7 before the schema/repository/decision contracts pass, Task 9 before workflow Plan checkpoints pass, Task 14 before recovery passes, or Task 20 before all focused gates and manual scenarios are complete.
