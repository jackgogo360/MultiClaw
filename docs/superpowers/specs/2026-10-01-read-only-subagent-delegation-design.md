# Read-only subagent delegation

## Scope

Add a bounded, opt-in `delegate_tasks` tool to the current tenant runtime. One
invocation accepts one to three independent research tasks and executes them
concurrently. Each child starts with its own message history, uses only native
  read-only workspace discovery tools, and returns a short result to the parent. This phase
  does not grant children network, write, shell, code execution, MCP, or further delegation.

## Boundaries

- The parent Run remains the sole durable workflow owner. Children inherit its
  exact tenant, workspace, session, and run context, cancellation, deadline, and
  aggregate inference budget. Tool arguments cannot select another scope.
- The delegation tool is disabled by default. Operators may enable it under
  `agent` and bound task count, model rounds, child token use, and wall time.
- Each task may select the default model or a model explicitly listed in
  `llm.model_providers`; arbitrary model names are rejected.
- The child receives only its task objective and a read-only system instruction;
  it does not inherit the parent's private conversation. The existing inference
  wrapper loads applicable project rules and accounts for every model call.
  Child preparation does not consume the parent's steering or inject its
  objective; child limits are checked after project rules are added.
- A fixed native-tool allowlist and `read_only` check are enforced both in
  advertised schemas and at dispatch. Child tool calls use the existing
  permission checker, path policy, and execution guard.
- Children cannot start nested children. Scoped events contain parent Run ID,
  child ID, label, and state; the existing Run event journal replays them. The
  parent receives an ordered result for every child, including bounded errors.
- Parent cancellation cancels all active children. A crash can replay this
  read-only tool, so model usage may be repeated; no child gets a separate
  durable Run or independently recoverable checkpoint in this phase.

## Validation

Tests prove opt-in registration, isolated histories, concurrent children,
read-only dispatch despite forged tool names, bounded rounds and output,
inherited scope/budget, cancellation, scoped events, and timeout behavior.
Run focused tests, the backend suite, documentation checks, and frontend checks
if frontend files change.
