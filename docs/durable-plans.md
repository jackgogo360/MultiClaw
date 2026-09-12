# Durable Plans

Durable Plans turn an explicitly requested objective into a reviewable,
session-scoped execution record. The feature is opt-in during rollout: set
`planning.default_mode = "never"` to preserve direct chat, `"auto"` to let the
classifier choose, or `"always"` to require a Plan. A `plan:` prefix is an
explicit `always` request and is removed from the stored objective.

## Review and execution contract

The chat stream first emits a Plan reference. No execution tool is dispatched
until the user approves the current version. Review actions are approve,
reject, or revise; each request carries the session scope, current version,
and an idempotency key. Repeating a decision returns its recorded result.
Revisions append immutable version `N+1`; stale version `N` requests return
`409` and cannot rewrite history. Rejection cancels the run and dispatches no
tools.

Steps form a bounded DAG and execute in deterministic topological order. Only
one ready step is active for a run. Step attempts and dependency depth are
validated before insertion and again in the transaction that writes state.
Tool approval is separate from Plan approval: a Plan may be approved while an
individual tool still requires its own user decision.

## Recovery, cancellation, and reuse

Run leases and checkpoints make restart recovery fail closed. Missing, corrupt,
foreign, or digest-mismatched context blocks the run instead of guessing. A
cancellation request is persisted and checked before model calls, step writes,
tool dispatch, and finalization. Cancellation cannot undo an external effect
already accepted by a provider; such effects require the provider's own
idempotency and reconciliation contract.

Retries are bounded by `planning.max_step_attempts`. Exhaustion creates a
reviewable replan boundary. A reused result is accepted only when the step
definition, dependency-result digests, and source attempt proof match; an
existing `tool_call_id` is replayed without creating another execution row.
Final summaries are persisted and may be retried or rerun from the durable
Plan version.

## Configuration and operations

See [configuration](configuration.md) for all planning
limits. The aggregate round ceiling is:

```
planning.max_steps * planning.max_step_attempts * agent.max_tool_rounds
```

The `multiclaw_plan_operations_total` metric uses only bounded
`operation`, `status`, and `error_class` labels. Plan/run identifiers are
sanitized trace attributes, never metric labels. Recovery, replan, completion,
and cancellation outcomes are emitted after their durable transition.

SQLite and MySQL use the same migration contract and deletion order. Session
deletion and account purge remove Plan rows, attempts, checkpoints, tool
executions, and session-scoped round ledger entries. Keep the rollout kill
switch on until both backend suites and the release checks pass.

## API and manual browser checks

The scoped endpoints are documented in [API overview](api.md):
`/api/plans`, `/api/runs`, and the existing chat/approval routes. Unknown or
foreign resources are indistinguishable (`404`), while stale versions and
conflicting decisions return `409`. SSE Plan notifications are advisory;
refresh and session switching hydrate state through the scoped GET endpoints.

Before release, manually verify: Plan creation and review; reject with zero
tool dispatch; revision and stale-version conflict; refresh and session
switch; approval-required tools; bounded retry and replan; cancellation at
each boundary; restart recovery; summary retry/rerun; and session/account
deletion. Record identifiers only in private test notes, not documentation.
