# P0 Reliable Task Runtime Implementation Plan

**Goal:** Complete approved roadmap P0 items 1–3 on a new branch.

**Architecture:** Normalize provider protocols; bound and compact inference context; move run ownership to a tenant-scoped background service with persistent observation and controls. Preserve existing workflow recovery and approvals.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, pytest, React 19, assistant-ui, Vite.

## Task 1 — Provider reliability

- [ ] Reproduce multi-provider routing and Anthropic streaming failures in `tests/test_llm.py`.
- [ ] Implement explicit model/provider mapping, provider request/stream normalization, bounded retry, sanitized errors and normalized usage in `src/multiclaw/llm/`.
- [ ] Validate completion and streaming contracts, fragmented tools, no retry after delivery and credential cleanup.

## Task 2 — Context preparation

- [ ] Add regression tests in `tests/test_context.py` and dedicated compaction tests for budget overflow, tool pairs and instruction containment.
- [ ] Implement instruction loading, model-assisted compaction with deterministic fallback, schema-aware budgets and bounded tool artifacts in `src/multiclaw/agent/` context modules.
- [ ] Wire preparation into direct, streaming, reflection, Plan-step and final-summary calls; keep original objective and control messages.

## Task 3 — Background execution

- [ ] Add regression tests for disconnect survival, replay, cancellation, steering, FIFO queued messages and cross-tenant denial.
- [ ] Implement run ownership, scoped durable replay and controls in `src/multiclaw/runtime/`, API routes, storage and lifecycle modules.
- [ ] Apply to direct chat and Plan execution streams, and shut down/revoke tasks before dependent resources close.

## Task 4 — Limits and UI

- [ ] Implement per-run time/token limits and persistent usage; enforce aggregate tenant budget where configured.
- [ ] Expose run list/status/progress/control and usage in the existing frontend; restore observation after reload.
- [ ] Verify frontend lint/build and browser interaction, documenting unavailable external-provider/environment checks.

## Task 5 — Review and verification

- [ ] Review spec compliance and security-sensitive allowed/denied paths independently.
- [ ] Run `uv run pytest -q`, `uv run python scripts/check_docs.py`, frontend lint/build and `git diff --check`.
- [ ] Update configuration, API, architecture and usage docs with implemented boundaries and configuration examples.
- [ ] Commit focused verified changes with Tested, Confidence and Scope-risk trailers; report branch and remaining environmental limitations.
