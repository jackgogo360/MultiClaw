# Durable Plan Engine Design

**Date:** 2026-09-05  
**Status:** approved in design discussion; pending written-spec review

## Overview

MultiClaw will replace its advisory, in-memory `plan:` response with a durable,
versioned Plan domain model that can pause for review, execute through the existing
workflow runtime, survive process restarts, and later schedule independent steps
across sub-agents.

The Plan domain owns what should be done. The existing Workflow domain continues
to own how a run executes safely: leases, fencing tokens, checkpoints, tool
approvals, recovery, and terminal state. This separation avoids turning checkpoint
JSON into a query model and avoids a premature rewrite into a general DAG engine.

## Problem

The current implementation does not execute or persist plans:

- `Planner.create_plan()` splits text on the literal string `" and "` and returns
  in-memory Pydantic objects (`src/multiclaw/planner/planner.py:4-23`).
- `Plan` and `PlanStep` have no tenant, session, version, dependency, or execution
  identity (`src/multiclaw/planner/models.py:7-25`).
- Both agent paths special-case the `plan:` prefix, return a summary, and stop
  before the normal workflow/tool loop (`src/multiclaw/agent/multiclaw.py:235-243`
  and `src/multiclaw/agent/multiclaw.py:443-453`).
- The frontend can display messages, tools, and approvals but has no Plan message
  part or Plan state (`frontend/src/components/assistant-ui/thread.tsx:100-130`).

At the same time, MultiClaw already has the reliability primitives a durable Plan
engine needs:

- scoped `agent_runs`, `tool_executions`, approvals, and execution checkpoints
  (`src/multiclaw/storage/schema.py:150-296`);
- run and execution state machines (`src/multiclaw/workflow/models.py:13-64` and
  `src/multiclaw/workflow/models.py:215-274`);
- lease, fencing, CAS, transactional checkpoints, and recovery through
  `WorkflowCoordinator` and `RecoveryService`;
- exact tenant/workspace/session/run event routing and a structured SSE encoder.

## Goals

1. Automatically require a reviewed Plan for complex requests while preserving a
   direct path for simple requests.
2. Preserve `plan:` as an explicit request to force planning.
3. Persist immutable Plan versions, ordered steps, normalized dependencies,
   decisions, and step attempts under the existing tenant scope.
4. Require an explicit user decision before the first execution tool runs.
5. Execute approved steps sequentially in dependency order in the first release.
6. Recover safely at planning, decision, model, tool, and step boundaries.
7. Replan after exhausted recovery without silently skipping a failed step.
8. Keep the schema and coordinator ready for later parallel sub-agent scheduling.
9. Render a durable Plan Card inline in the existing conversation UI.

## Non-goals

- A general-purpose visual workflow or DAG authoring system.
- Parallel step execution in the first release.
- Direct drag-and-drop or field-level editing of Plan steps.
- A separate task-board, Gantt, or graph visualization.
- Binding Plan steps to concrete tools during planning.
- Multi-agent delegation itself; this design only preserves the required extension
  points.
- New Python or frontend package dependencies.

## Confirmed product behavior

### Triggering

`POST /api/chat` accepts an optional `planning_mode`:

- `auto` (default): classify the request as `direct` or `plan`;
- `always`: require a Plan;
- `never`: use the existing direct agent loop.

The existing `plan:` prefix maps to `always` and is removed from the objective
before generation. Explicit modes bypass classification.

### Review

The first release supports three review actions:

- approve the current version and execute it;
- reject the current version and cancel the waiting run;
- provide revision feedback, producing a new immutable version.

The first release does not support direct step editing. Only the current waiting
version can receive a decision.

### Dependencies and execution

The persisted model supports a DAG through explicit dependencies. The first
executor uses a stable topological order and runs one step at a time. A later
multi-agent executor can select every pending step whose dependencies have
succeeded without changing Plan storage.

### Failure and revision

A failed step first uses the existing tool recovery strategy. After permitted
retries are exhausted, execution pauses, a revised Plan version is generated, and
the user must approve it. A failed step is never silently skipped.

Previously successful work is reusable only when the coordinator can prove that
the revised step definition and all dependency result digests are compatible.

## Architecture

```text
Chat/API
   |
   v
PlanningPolicy -------- direct --------> existing agent loop
   |
   | plan
   v
PlanningService ---> PlanRepository
   |                      |
   | waiting/decision     | Plan fact store
   v                      |
PlanExecutionCoordinator <+
   |
   v
existing WorkflowCoordinator
   |- lease / fencing / checkpoint
   |- model and tool execution
   |- tool approval
   `- recovery
```

### PlanningPolicy

`PlanningPolicy` has one responsibility: return a typed `PlanningDecision` for a
request. `always` and `never` are deterministic. `auto` uses a configurable model
to produce `direct` or `plan` plus a bounded reason.

If automatic classification fails, the request follows the existing direct path
and records a redacted classification failure event. Tool permission and approval
rules remain active on that path. Explicit `always` never falls back to direct
execution.

### PlanGenerator

`PlanGenerator` converts an objective and optional revision context into a
validated `PlanDraft`. It does not receive the execution tool registry, so planning
cannot produce side effects.

The provider returns a structured function/tool payload that is validated with
Pydantic. One repair request is allowed for invalid structured output. A second
failure ends the run without executing the objective.

### PlanningService

`PlanningService` is the application boundary for:

- materializing a validated draft as a new immutable Plan version;
- approving, rejecting, and requesting revision with CAS;
- recording idempotent decision requests;
- associating the waiting run with the Plan;
- emitting post-commit Plan events.

It does not select or execute steps.

### PlanExecutionCoordinator

`PlanExecutionCoordinator`:

1. verifies that the run's active version is the Plan's approved current version;
2. reads step definitions, dependencies, and the run's latest step attempts;
3. chooses the next ready step in stable topological order;
4. creates a fenced step attempt before model/tool dispatch;
5. executes the step through the existing agent and workflow machinery;
6. records a structured result or failure;
7. advances, replans, cancels, or completes the run.

### Workflow integration

`WorkflowCoordinator` remains the only owner of run leases, fencing tokens,
workflow checkpoints, tool executions, tool approvals, and terminal transitions.
Plan code must not implement a second lease or recovery system.

The run transition table gains `awaiting_user -> cancelled` so rejecting a Plan or
requesting durable cancellation can terminate without pretending to resume work.
Approval and revision continue through `awaiting_user -> resuming -> running`.

Plan approval is distinct from tool approval. The existing `approval_requests`
schema binds decisions to `tool_call_id` and must not be overloaded with a fake
Plan tool call.

## Domain model

### Plan statuses

Persisted Plan aggregates begin at `awaiting_approval`; incomplete generator output
is never stored as a draft.

```text
awaiting_approval --approve--> approved
        |                       |
        | revise                | failure requiring replan
        v                       v
awaiting_approval <--------- awaiting_approval (version + 1)
        |
        `--reject--> rejected

approved/rejected --archive--> archived
```

`current_version` identifies the latest materialized version.
`approved_version` identifies the last approved version, if one exists. Execution
is allowed only when both values are equal and the aggregate status is `approved`.

### Step attempt statuses

```text
pending -> running -> succeeded
              |
              +-> failed_retryable -> running (new attempt)
              +-> failed_terminal  -> replan required
              `-> cancelled
```

A step definition is immutable. Mutable execution state exists only in step-run
attempt records.

## Storage design

All tables use database-clock timestamps and carry tenant/workspace/session scope.
Composite foreign keys follow the existing schema pattern so an ID alone never
grants access.

### `agent_plans`

- `id`
- `tenant_id`, `workspace_id`, `session_id`
- `source_message_id`
- `trigger_mode`: `automatic` or `explicit`
- `status`: `awaiting_approval`, `approved`, `rejected`, or `archived`
- `current_version`
- `approved_version`, nullable
- `version`: aggregate CAS value
- `created_at`, `updated_at`

### `agent_plan_versions`

- full scope plus `plan_id`, `plan_version`
- `objective`
- `constraints_json`
- `generation_reason`
- `parent_version`, nullable
- `revision_feedback`, nullable
- `schema_version`
- `content_digest`
- `created_at`

The composite `(tenant_id, workspace_id, session_id, plan_id, plan_version)` is
unique and referenced by step definitions and runs.

### `agent_plan_steps`

- full Plan-version scope
- `step_id`
- `logical_step_key`
- `supersedes_step_id`, nullable
- `ordinal`
- `title`, `description`, `expected_outcome`
- `assigned_agent_profile_id`, nullable and reserved for multi-agent execution
- `max_attempts`
- `definition_digest`

`logical_step_key` is unique inside one Plan version. `supersedes_step_id` maps a
revision to its semantic predecessor but does not itself authorize result reuse.

### `agent_plan_step_dependencies`

- full Plan-version scope
- `step_id`
- `depends_on_step_id`

Both step IDs must belong to the same Plan version. Self-dependencies, duplicate
edges, missing nodes, and cycles are rejected before the version transaction
commits.

### `agent_plan_step_runs`

- full Plan-version and step scope
- `step_run_id`
- `run_id`
- `attempt`
- `status`
- `result_summary`, nullable
- `result_ref`, `result_digest`, nullable
- `error_code`, `error_detail_redacted`, nullable
- `reused_from_step_run_id`, nullable
- `version`: attempt CAS value
- `started_at`, `finished_at`, nullable

The unique key `(run_id, step_id, attempt)` prevents duplicate attempts. Reused
results create an explicit succeeded record referencing the source attempt.

### `agent_plan_decisions`

- full Plan scope
- `decision_id`: client-generated idempotency key
- `plan_version`
- `expected_plan_cas_version`
- `action`: `approve`, `reject`, or `revise`
- `feedback`, nullable
- `decided_by`
- `resulting_plan_version`, nullable
- `created_at`

This supporting record is necessary to return the original result when a client
retries after losing the decision response. It also provides a durable decision
history without treating audit logs as application state.

### Changes to `agent_runs`

Add nullable columns:

- `plan_id`
- `initial_plan_version`
- `active_plan_version`
- `cancel_requested_at`, nullable

Direct runs retain null values. `initial_plan_version` is immutable.
`active_plan_version` changes only when a revised version is approved through the
same fenced/CAS transition that resumes the run. Step-run records and checkpoints
preserve the exact version history.

## Generation contract

### Planning decision

```python
class PlanningDecision(BaseModel):
    mode: Literal["direct", "plan"]
    reason: str = Field(max_length=500)
```

### Plan draft

```python
class PlanDraftStep(BaseModel):
    logical_step_key: str
    title: str
    description: str
    expected_outcome: str
    depends_on: list[str]
    max_attempts: int = 2


class PlanDraft(BaseModel):
    objective: str
    constraints: list[str]
    generation_reason: str
    steps: list[PlanDraftStep]
```

Validation limits:

- 1 to 20 steps;
- dependency depth at most 10;
- logical keys contain 1 to 64 lowercase ASCII letters, digits, `_`, or `-`;
- titles contain 1 to 200 characters;
- descriptions contain 1 to 4,000 characters;
- expected outcomes contain 1 to 2,000 characters;
- at most 20 constraints of at most 1,000 characters each;
- generation reasons contain at most 1,000 characters;
- revision feedback contains at most 8,000 characters;
- canonical serialized Plan-version content is at most 262,144 bytes;
- unique logical keys and deterministic ordinals;
- no missing, duplicate, self, or cyclic dependencies;
- no Secret-shaped fields or raw credentials.

Plan steps state outcomes, not specific tool names. The executor chooses among the
current tenant's built-in and MCP tools when a step runs.

## Step execution contract

Each step receives a bounded context containing:

- the approved Plan objective and constraints;
- the current immutable step definition;
- successful dependency summaries and result digests;
- the current attempt and remaining attempt budget;
- active skill prompts and normal tenant tool schemas.

The executor adds an internal `complete_plan_step` schema. A step succeeds only
after the model returns a validated completion payload:

```python
class PlanStepCompletion(BaseModel):
    status: Literal["succeeded", "failed"]
    summary: str
    evidence: list[str]
    retryable: bool = False
```

Free-form assistant text alone cannot mark a step successful. Invalid completion
payloads consume the normal bounded reflection/repair budget and eventually become
a step failure.

After all steps succeed, a final model call summarizes the objective and persisted
step results for the user. Failure of this summary does not rerun successful steps;
the user can retry summary generation.

## Revision and result reuse

Revision input consists of the current immutable version, the user's feedback, the
failed step if any, and redacted summaries/digests of completed work.

For each step in the revised version, result reuse requires all of the following:

1. `supersedes_step_id` resolves to a succeeded attempt from the same Plan run;
2. the new `definition_digest` equals the predecessor definition digest;
3. every dependency maps to a succeeded compatible predecessor;
4. all dependency result digests equal the values used by the predecessor attempt;
5. no policy or tool-catalog compatibility rule requires re-execution.

If any proof is missing, the step remains pending. Reuse creates a new succeeded
step-run row with `reused_from_step_run_id`; history is never rewritten.

## Checkpoint and recovery design

Add three Plan boundary phases:

- `PLAN_AWAITING_APPROVAL`: Plan ID, current version, Plan digest, and decision
  cursor;
- `PLAN_STEP_READY`: Plan ID/version, step ID, step-run ID, attempt, and execution
  cursor;
- `PLAN_REPLAN_REQUIRED`: Plan ID, failed step-run ID, failure digest, and revision
  cursor.

Model and tool activity inside a step continues to use the existing
`MODEL_OUTPUT_COMMITTED`, `AWAITING_APPROVAL`, `EXECUTION_DISPATCHING`, and
`EXECUTION_RESULT_OBSERVED` phases. Recovery obtains Plan context from the scoped
run and step-run records before interpreting the existing phase.

Every Plan checkpoint includes a digest. A missing Plan/version/step, scope
mismatch, or digest mismatch fails closed through the existing corrupt or
incompatible recovery states.

Required crash windows are listed in Acceptance criteria.

## API design

### Chat

`POST /api/chat` adds:

```json
{
  "message": "Analyze the repository and fix the issue",
  "session_id": "uuid",
  "planning_mode": "auto"
}
```

The initial chat SSE stream ends after a Plan is materialized, while the durable run
remains `awaiting_user`.

### Plan and run routes

| Method and route | Purpose |
|---|---|
| `GET /api/sessions/{session_id}/plans` | List scoped Plan summaries for a session |
| `GET /api/plans/{plan_id}` | Read the current version, dependencies, decisions, and latest execution |
| `POST /api/plans/{plan_id}/decision` | Approve, reject, or request a revision |
| `POST /api/plans/{plan_id}/runs` | Execute the current approved version again |
| `GET /api/runs/{run_id}` | Read run and step-attempt state |
| `POST /api/runs/{run_id}/cancel` | Persist a cancellation request |
| `POST /api/runs/{run_id}/summary/retry` | Retry only the final summary without rerunning steps |

Plan decision requests include `decision_id`, `plan_version`, and
`expected_version`. Stale decisions return `409` with the latest Plan summary.
Foreign-scope resources use the repository's existing scope-hiding response.

The decision actor is derived from authenticated context, never from request JSON.

## SSE contract

Add versioned data parts:

- `data-plan-created`
- `data-plan-revised`
- `data-plan-decision`
- `data-plan-step-status`
- `data-plan-run-status`

Every payload carries `schema_version`, `plan_id`, `plan_version`, `run_id`, Plan
aggregate version, and the exact tenant/workspace/session/run scope already used by
the event router.

SSE is a notification path, not a fact store. After disconnect or session switch,
the client reads Plan/Run APIs to reconstruct state. The first release does not
require replay of the in-memory event stream.

## Frontend design

The conversation renders a first-class Plan message part rather than a synthetic
tool call.

The inline Plan Card shows:

- objective, version, status, and aggregate progress;
- ordered steps and textual dependency status;
- pending, active, succeeded, failed, and cancelled states;
- expandable result summaries and redacted errors;
- approve, reject, and revision-feedback controls when waiting;
- historical versions as read-only views;
- retry-summary and rerun actions when applicable.

The active step expands automatically. The first release does not render a graph.

Session hydration returns a persisted Plan reference as a message part. The client
then reads `GET /api/plans/{plan_id}` and treats that response as authoritative.
Live SSE updates are applied only when their scope and aggregate version are current.

The Plan reference is stored in the existing chat message's `metadata_json` as a
typed, versioned message part. Plan definitions and mutable execution state remain
in Plan tables; message metadata contains only the scoped Plan ID and display type.

Likely frontend boundaries are:

- `frontend/src/components/plan/PlanCard.tsx`
- `frontend/src/components/plan/PlanStepList.tsx`
- `frontend/src/components/plan/PlanDecisionControls.tsx`
- `frontend/src/lib/api.ts`
- `frontend/src/components/session/SessionProvider.tsx`
- `frontend/src/components/assistant-ui/thread.tsx`

## Cancellation

Cancellation is durable. The run records `cancel_requested` and checks it before
each model call, tool dispatch, retry, and step transition. A currently running
external side effect may not be reversible. Unknown outcomes retain the existing
`manual_uncertain` recovery semantics.

Cancellation never rewrites Plan definitions or prior successful step attempts.

## Security and governance

- Planning has no execution tools and cannot create side effects.
- All Plan repositories require full tenant/workspace/session scope.
- Every decision and mutable step transition uses CAS; every executing transition
  also uses the current run fence.
- A run verifies that `current_version == approved_version == active_plan_version`
  before starting a new step.
- Plan text, feedback, evidence, and errors use existing response/audit redaction
  before SSE or logs.
- Metric labels do not contain Plan, run, tenant, session, provider, or path IDs.
- Per-tenant limits cover active Plans, steps per version, revision count, step
  attempts, and total agent/tool rounds.
- Session and account deletion explicitly remove decisions, step runs,
  dependencies, steps, versions, and Plan aggregates in referential order.

## Configuration

Add typed planning settings:

```toml
[planning]
enabled = true
default_mode = "auto"
classification_model = ""
generation_model = ""
max_steps = 20
max_dependency_depth = 10
max_revisions = 5
max_step_attempts = 2
```

Empty model names use the existing default model. `planning_mode=never` remains an
explicit compatibility and operational escape path. Disabling planning maps all
requests except an explicit `always` request to direct; explicit `always` returns a
clear unavailable error rather than executing directly.

## Observability

Use the current redacted operational sink for initial counters and trace events:

- Plan classified direct/plan/failure;
- Plan version materialized;
- decision approved/rejected/revision requested/conflicted;
- step attempt started/succeeded/retryable/terminal/reused;
- Plan recovery outcome;
- run completed/cancelled/replan required.

Only bounded labels such as operation, status, and error class are allowed. Plan IDs
belong in redacted trace context, not metric labels.

## Compatibility and rollout

- Schema additions and `agent_runs` columns are additive and nullable.
- Existing direct runs and sessions continue to hydrate without Plan records.
- Existing chat clients can omit `planning_mode`.
- The `plan:` response intentionally changes from an advisory summary to a durable
  waiting Plan.
- Rollout can set `planning.default_mode="never"` while migrations and UI deploy,
  then switch to the confirmed default `auto` after backend and frontend support are
  available.
- SQLite and MySQL must use the same schema and state-transition contract.

## Alternatives considered

### Checkpoint-only Plan snapshots

Store Plan JSON as a new checkpoint phase without new domain tables.

Rejected because individual-step queries, immutable version comparison, repeated
execution, dependency scheduling, multi-agent assignment, and reporting would all
require scanning checkpoint payloads. Checkpoints are recovery evidence, not a Plan
fact store.

### First-class Plan aggregate with existing Workflow execution

Chosen. It creates explicit query and mutation boundaries while reusing the
project's strongest reliability primitives.

### General DAG workflow engine

Defer. It has the highest long-term ceiling but would prematurely combine plans,
tools, approvals, loops, conditions, and sub-agents while replacing already-tested
workflow behavior. The normalized Plan schema can later feed such an engine.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Plan and Workflow become competing state machines | Plan owns definition/review; Workflow exclusively owns run execution/recovery |
| A run loses version history after replan | Store immutable initial and mutable active versions; step runs/checkpoints preserve exact version history |
| Duplicate decisions resume twice | Persist `decision_id`, use Plan CAS, and make identical retries return the recorded result |
| Reused work is stale under a revised dependency | Require definition and dependency-result digest equality; otherwise rerun |
| A crash occurs between DB commit and SSE | Commit first; SSE is advisory and clients rehydrate through Plan/Run APIs |
| Planning increases latency and model cost | Bypass classification for explicit modes; allow configurable planning models; record classification outcomes |
| Auto classification fails open | Preserve existing direct behavior but record the failure; explicit `always` never fails open |
| New phases break old recovery code | Version payloads, reject unknown versions as incompatible, and add migration/recovery tests before defaulting to auto |
| Frontend applies stale events | Compare scope, Plan aggregate version, and active run before applying; refetch after gaps |
| Cross-tenant Plan IDs leak existence | Require scoped repositories and return the existing scope-hiding response |

## Acceptance criteria

1. `planning_mode=never` produces no Plan rows and preserves the current direct
   chat/tool behavior.
2. `planning_mode=always` creates exactly one scoped Plan and version, atomically
   persists valid steps/dependencies, emits a Plan part, and leaves the run in
   `awaiting_user`.
3. An explicit `plan:` request is equivalent to `planning_mode=always` after the
   prefix is removed from the objective.
4. No execution tool is dispatched before the current Plan version is approved.
5. Approving the current version resumes the waiting run and executes steps once in
   deterministic topological order.
6. Rejecting the current version cancels the waiting run and executes zero tools.
7. Revision feedback creates version `N+1`; all rows for version `N` remain byte-for-
   byte unchanged, and stale version `N` decisions fail with `409`.
8. Retrying a completed decision with the same `decision_id` returns the recorded
   result and does not create a second version or resume the run twice.
9. Ten concurrent mixed decisions for one Plan produce one committed winner and
   nine idempotent/conflict responses without duplicate continuation.
10. The first executor runs only one step at a time, even when multiple DAG nodes
    are ready.
11. A step succeeds only after a valid `complete_plan_step` payload is persisted.
12. Retryable failures create bounded attempts; exhausted attempts create a revised
    waiting version and do not silently skip the failed step.
13. Result reuse occurs only when step-definition and dependency-result digests
    match; changing either forces execution.
14. A cancellation request is persisted and observed before the next model, tool,
    retry, or step boundary.
15. Restart recovery produces no duplicate external side effect when a crash occurs:
    after Plan commit before SSE, after decision commit before resume, after step-run
    creation before dispatch, after tool completion before step-result commit, or
    after step success before next-step selection.
16. Corrupt, missing, foreign-scope, or digest-mismatched Plan data blocks recovery
    without executing tools.
17. Unknown or foreign tenant/workspace/session Plan and run resources reveal no
    resource existence through API error differences.
18. Session deletion and account purge leave no Plan-related orphan rows or
    workspace files.
19. Refreshing the browser or switching away and back reconstructs the same Plan,
    version, decisions, and step progress from persisted APIs.
20. Existing sessions without Plan parts continue to render correctly.
21. SQLite and MySQL migrations produce the same constraints and pass their existing
    schema/readiness checks.
22. Full backend tests, documentation validation, frontend lint/build, and the
    documented browser scenarios pass before `planning.default_mode` changes to
    `auto` in deployment configuration.

## Test boundaries

- `tests/test_planner.py`: structured generation, repair limits, validation, DAG
  ordering, and planning policy.
- `tests/test_plan_repository.py`: immutable versions, decision idempotency, CAS,
  scope isolation, and deletion.
- `tests/test_plan_execution.py`: step selection, attempts, completion protocol,
  cancellation, replan, and result reuse.
- `tests/integration/test_plan_faults.py`: the five required crash windows and
  duplicate-side-effect assertions.
- `tests/test_server.py`: authenticated API, SSE part ordering, stale decisions,
  hydration, and recovery integration.
- `tests/test_migrations.py` and MySQL contract tests: schema parity, constraints,
  and deletion order.
- Frontend: existing lint/build gates plus manual browser verification for initial
  Plan, revision, stale decision conflict, execution progress, refresh recovery,
  cancellation, and historical version display. No frontend test dependency is
  introduced in this feature.

## Expected implementation boundaries

Likely new backend modules:

- `src/multiclaw/planner/policy.py`
- `src/multiclaw/planner/generator.py`
- `src/multiclaw/planner/service.py`
- `src/multiclaw/planner/execution.py`
- `src/multiclaw/storage/repositories/plans.py`
- `src/multiclaw/api/plans.py`
- `src/multiclaw/api/runs.py`

Likely modified backend modules:

- `src/multiclaw/planner/models.py`
- `src/multiclaw/agent/multiclaw.py`
- `src/multiclaw/workflow/models.py`
- `src/multiclaw/workflow/coordinator.py`
- `src/multiclaw/workflow/recovery.py`
- `src/multiclaw/storage/schema.py`
- `src/multiclaw/storage/uow.py`
- `src/multiclaw/api/chat.py`
- `src/multiclaw/api/sessions.py`
- `src/multiclaw/stream.py`
- `src/multiclaw/config/settings.py`
- `src/multiclaw/deletion/service.py`

The implementation plan must split schema/repository, planning/review, execution,
recovery, API/SSE, and frontend work into independently tested slices. It must not
replace the existing workflow reliability boundary in one large change.
