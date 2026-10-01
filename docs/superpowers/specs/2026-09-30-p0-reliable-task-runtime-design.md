# P0 reliable task runtime

## Scope and acceptance

The user approved roadmap items 1–3: provider reliability, long-task context, and background execution/control. Implement on `feat/p0-reliable-task-runtime`, retaining standalone deployment, tenant scopes, sandbox enforcement, tool approval and durable Plan semantics.

1. Each model resolves to an explicit provider; legacy single-provider configurations continue working. OpenAI-compatible and Anthropic requests, tools, streaming, errors and usage are normalized. Retry transient failures with bounded backoff only before output has been delivered. Never replay tool side effects as part of an HTTP retry. Record actual usage where reported and conservative estimates otherwise. Bound run time and tokens and document pricing-dependent cost accounting.
2. Each inference fits the configured context window including schemas and response reserve. Preserve instructions, original objective, steering, task decisions and recent complete tool exchanges. Compress older history into a task summary; never leave orphan tool calls/results. Large tool outputs are bounded and stored as readable tenant-workspace artifacts. Project instructions are loaded only within the tenant workspace, respecting path and size limits.
3. Execute independently of the browser stream. Disconnecting a subscriber cannot cancel a run. Persist scoped stream events and expose replay/status/list endpoints. Explicit cancellation stops execution; approval waits remain recoverable. Support steering at inference boundaries and FIFO queued follow-up messages per session, with bounded queues. Application shutdown and tenant deletion close active execution safely; service restart follows existing recovery rules rather than replaying uncertain side effects.
4. Web UI exposes task state, cancel, steering/queued follow-ups and progress after reload. Usage and configured limits are inspectable. Existing chat, plans and approval views continue to work.

## Architecture

Keep provider normalization in `llm/`; use a per-call stream decoder rather than shared mutable adapter state. Add bounded context preparation in `agent/`, shared by direct and Plan inference. Use a scoped background run service to own producer tasks, durable event replay and control requests. HTTP connections are consumers only. Reuse SQLAlchemy tenant-scoped repositories and checkpoint services; add schema/migrations only when persistence requires them. Keep run controls independent of frontend state and use existing CSRF/auth middleware for mutations.

## Validation

Provider contract tests reproduce routing, Anthropic stream/tool conversion, transient failure retry, no retry after first output and sanitized errors. Context tests prove budget accounting, tool-pair preservation, bounded artifacts and confined instruction loading. Background tests disconnect/reconnect, cancel, queue order, steering delivery, tenant denial, shutdown and approval/recovery behavior. Run backend suite, frontend lint/build, documentation checks and browser verification where available. Document environmental skips and real-provider testing limitations.
