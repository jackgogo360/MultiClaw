# Read-only Subagent Delegation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, bounded tool that lets one parent Run delegate independent read-only research tasks concurrently.

**Architecture:** A dedicated runner owns child prompts, model rounds, tool allowlisting, result bounds, and scoped events. A native tool exposes that runner to the parent agent. RuntimeFactory wires it after the router and scheduler exist; all child inference retains the parent Run's ContextVars and governance boundary.

**Tech Stack:** Python 3.12, Pydantic, asyncio, FastAPI runtime, SQLAlchemy-backed Run journal, pytest.

---

### Task 1: Tool and runner contract

**Files:** Create `src/multiclaw/agent/delegation.py`, `src/multiclaw/tools/delegate_tasks.py`; test `tests/test_subagent_delegation.py`.

- [x] Write failing tests for the Pydantic task limits, opt-in tool schema, and exact native read-only tool list.
- [x] Run `uv run pytest tests/test_subagent_delegation.py -q` and confirm those tests fail because the contract is absent.
- [x] Implement `DelegateTasksParams(tasks: list[DelegatedTask])` with one-to-three items and a builder with `RecoveryStrategy.READ_ONLY_REPLAY`.
- [x] Run focused tests and confirm the contract is green.

### Task 2: Child execution and safety

**Files:** Modify `src/multiclaw/agent/delegation.py`; test `tests/test_subagent_delegation.py`.

- [x] Write failing tests for concurrent execution, independent child histories, forged write-tool rejection, and ordered results.
- [x] Run the focused tests and confirm the expected failures.
- [x] Implement one model/tool loop per child with a fixed read-only registry view, one aggregate parent budget, bounded child rounds/output, and `asyncio.gather` cancellation propagation.
- [x] Run focused tests and confirm they pass.

### Task 3: Runtime integration and observability

**Files:** Modify `src/multiclaw/runtime/factory.py`, `src/multiclaw/config/settings.py`, `src/multiclaw/tools/base.py`, `src/multiclaw/tools/scheduler.py`, `src/multiclaw/governance/sandbox/execution_guard.py`; test `tests/test_subagent_delegation.py` and `tests/test_runtime_factory.py`.

- [x] Write failing tests for disabled-by-default registration, enabled registration, scoped start/finish events, parent cancellation, and bounded tool timeout override.
- [x] Run focused tests and confirm the expected failures.
- [x] Wire the builder after router construction, retain the parent context at invocation time, and permit a bounded timeout for this native tool only.
- [x] Run focused tests and confirm they pass.

### Task 4: Documentation and verification

**Files:** Modify `docs/architecture.md`, `docs/configuration.md`, `config/multiclaw.toml`, `multiclaw.toml`.

- [x] Document opt-in settings, read-only permissions, event replay, and read-only replay cost caveat.
- [x] Run `uv run pytest -q`, `uv run python scripts/check_docs.py`, and `git diff --check`.
- [x] Review the diff for tenant boundaries, allowed and denied tool paths, and unrelated changes.
