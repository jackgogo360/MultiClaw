# Durable subagents and agent teams

The user requested independently runnable subagents and cooperating agent teams,
including isolated writes in the first release. Git repositories use dedicated
worktrees; an unsupported project must fail explicitly before any writable worker
starts. The original worktree is never edited by a member. Acceptance of changes
is an explicit user action with conflict detection against the captured base.

## Shared execution

Each assignment has a durable job ID, an internal session, an independent Run,
its own transcript, instructions, tool policy and usage. Parent/session/team
relationships are scoped to tenant and workspace. Jobs start asynchronously and
return a handle; status, transcript, steering, cancellation and result are queried
through authenticated APIs or parent tools. A bounded dispatcher owns workers,
heartbeats and restart reconciliation. Existing workflow leases, checkpoints,
approval, sandbox and model accounting remain authoritative for a worker Run.
Internal sessions are hidden from ordinary conversation lists and are purged
with their owning conversation/account. Parent cancellation propagates to owned
jobs. Jobs waiting for approval remain resumable. A provider or tool failure must
terminalize the job without losing completed siblings.

## Subagents

`spawn_agent`, `agent_status`, `agent_message`, `agent_cancel` and `agent_wait`
expose durable assignments. A child is given an explicit goal and context, not
the complete parent history. Tool access is selected from a deployment-defined
profile and cannot exceed the parent's policy. Read-only and workspace-writer
profiles are supported; writers receive their own isolated project. Recursion is
bounded and cross-tenant IDs behave as missing resources.

## Teams

A Team has a leader, persistent member identities, a shared task board and an
append-only message inbox. Each member assignment becomes a job using the shared
execution layer. Task dependency validation and compare-and-swap claiming prevent
duplicate ownership; cancellation prevents new dispatch. Members can send direct
messages or broadcast within their team. A leader can assign, observe, steer and
summarize work. Member workspaces are distinct; diffs and completion evidence are
available for review before acceptance. Team tasks and messages are persisted,
bounded and scoped; UI refresh reconstructs them from APIs.

## Product and verification

The conversation exposes a collaboration panel with subagent jobs and team task
boards, members, messages, transcripts, controls and change review. APIs require
the existing cookie authentication and CSRF rules. Automated tests cover scoped
CRUD, CAS/dependencies, independent execution, parent/member cancellation, restart
reconciliation, approval waits, worktree containment, write isolation, conflict
refusal and change acceptance. SQLite/MySQL migration structure remains aligned.
Run full backend tests, frontend lint/build and docs checks. Record external model
and unavailable database/browser verification separately.
