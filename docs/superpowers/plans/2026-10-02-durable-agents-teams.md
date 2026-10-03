# Durable Agents and Teams Implementation Plan

**Goal:** Deliver durable subagent assignments and cooperating teams with isolated
Git worktree writes, authenticated controls, restart handling and a UI.

**Architecture:** Reuse independent internal sessions and existing agent Runs.
Scoped collaboration tables record jobs, team membership, task ownership and
messages. A service/worker layer schedules jobs; workspace isolation and review
are implemented by a focused Git service. Each worker gets a fresh agent/runtime.

**Tech stack:** Python, asyncio, SQLAlchemy/Alembic, FastAPI, React/TypeScript.

1. **Workspace isolation** — Create `collaboration/workspaces.py` and real-Git
   tests. Capture a clean project base, create contained per-job worktrees,
   produce bounded diffs, accept after base/conflict checks and retain artifacts.
2. **Persistent domain** — Create `collaboration/models.py`, scoped repositories,
   schema and migration. Test jobs/teams/tasks/messages, tenant denial, task claim
   CAS and dependency checks before implementing the corresponding operations.
3. **Execution** — Create service and dispatcher, fresh worker runtime, bounded
   job admission, parent linkage, controls and reconciliation. Test asynchronous
   handles, independent histories, cancellation, failures and restart behavior.
4. **Integration** — Add authenticated collaboration API and parent/member tools;
   wire server startup/shutdown, recovery ownership, hidden sessions and deletion.
   Test allowed and denied API paths and lifecycle cleanup.
5. **Product UI** — Add collaboration panel for job/team creation, task board,
   member controls/messages, transcripts and diff review. Pass lint/build and
   manually exercise available browser paths.
6. **Review** — Independently review spec compliance and code quality, resolve
   substantive findings, then run complete tests, docs and diff checks.

Execution occurs in the dedicated worktree. No deployment or merge is included.

## Implementation and verification record

All six implementation/review steps have code and automated coverage. Independent
backend and UI reviews found no remaining Critical or Important findings. Existing
chat tests were stabilized without changing production behavior: Run identity comes
from response headers rather than racing the SSE consumer, and two heartbeat tests
retain 50 ms refreshes with a 1000 ms lease instead of a 120 ms startup window.

- Backend regression: 1807 passed, 179 skipped, 5 deselected, 1 failed. The failure
  was the existing Plan reuse-proof test (`missing_proof-collect`) reporting a stale
  lease; its five-case group subsequently passed without changes. A fresh combined
  run of the entire Plan execution module and collaboration tests then passed:
  155 passed, 1 skipped. A completely green full-suite run is not claimed.
- The five deselected nsjail tests require a loopback listener; this managed
  environment rejects its socket bind. They still require verification outside
  this environment; no production sandbox behavior was weakened.
- Frontend lint and production build passed (existing bundle-size warning only).
- Documentation check, Python compile check and diff whitespace check passed.
- Real-Git isolation, explicit standalone/team acceptance, approval/resume and
  tenant-denial tests passed. Browser manual testing, live-provider end-to-end
  testing and a live MySQL deployment were not performed.

At the user's request, the complete implementation was subsequently consolidated
into `/Users/felix/git/MultiClaw` on `feat/subagent-delegation-optimization`. All 51
source/test/documentation files were compared with the feature worktree, and the
frontend bundle was regenerated in the main checkout. The original worktree is
retained only as a backup. The initial `.git` write restriction was subsequently
lifted by the user to allow committing and pushing the consolidated branch.
No merge or deployment is included.

Pre-commit verification after lifting the restriction passed: 95 tests covering
collaboration, migrations, the entire nsjail test module and the three stabilized
chat cases. This includes the five previously blocked socket-listener tests.
Frontend lint/build, documentation validation and staged diff checks also passed.
The earlier full-suite Plan lease flake remains recorded above; no subsequent
fully green full-suite run is claimed.
