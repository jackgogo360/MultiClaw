# Tool Calling Design

**Date:** 2026-05-17
**Status:** approved

## Overview

Enable MultiClawAgent to send tool schemas to the LLM and execute tool calls in a
multi-round ReAct loop, with streaming progress and interactive approval for
guarded tools.

### Key decisions

| Decision | Choice |
|----------|--------|
| Max rounds | 10 (configurable via `Settings`) |
| Streaming UX | Transparent — show tool calls and results to user |
| Tool schema | Auto-generate from Pydantic `model_json_schema()`, allow manual override |
| Approval | Block and wait for user via frontend approve/reject |
| Approach | Extend existing ReAct loop (Scheme A) |

## Section 1: Tool Schema Generation

`ToolRegistry.to_openai_schemas()` converts each registered `ToolBuilder` into
OpenAI function-calling format using `Pydantic.model_json_schema()`.

```python
# ToolRegistry
def to_openai_schemas(self) -> list[dict]:
    schemas = []
    for b in self.list_all():
        schema = {
            "type": "function",
            "function": {
                "name": b.name,
                "description": b.description,
                "parameters": b.parameters_schema.model_json_schema(),
            },
        }
        schemas.append(schema)
    return schemas
```

**Override mechanism:** If a `ToolBuilder` subclass defines a
`to_openai_schema()` method, `ToolRegistry` calls that instead of auto-generating.

**No changes needed to `ToolBuilder` base class.** The base class keeps
`parameters_schema` as-is; the registry handles the conversion.

## Section 2: Multi-Round Loop

A new method `run_loop()` on `ReActAgent` wraps `think/act` in a loop:

```
messages = [{"role": "user", "content": user_input}]

for round in range(max_rounds):
    action = await think(messages, tools=registry.to_openai_schemas(), stream=streaming)
    if action.type == RESPONSE:
        return observation  # done
    observation = await act(action)
    messages.append(assistant_message_with_tool_calls)
    messages.append(tool_result_message)
```

### think() signature change

`ToolCallAgent.think()` changes from receiving `user_input: str` to receiving
`messages: list[dict]` and `tools: list[dict]`. The `tool:` prefix hack is removed.

- Non-streaming: calls `router.completion(messages=messages, tools=tools)`
- Streaming: calls `router.stream_completion(messages=messages, tools=tools)`

### handle_message_stream() changes

Yields three event types:
- `{"type": "tool_call", "name": "...", "params": {...}}` — LLM decided to call a tool
- `{"type": "tool_result", "name": "...", "content": "..."}` — tool execution result
- `{"type": "token", "content": "..."}` / `{"type": "done", ...}` — final text response (streamed)

### Tool call result format in messages context

Each tool call and its result are appended to the message list for the next round:

```json
{"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "echo", "arguments": "..."}}]}
{"role": "tool", "tool_call_id": "call_1", "content": "hello world"}
```

## Section 3: Approval Flow

### Scheduler changes

When `PermissionChecker.check()` returns `requires_approval=True`, the scheduler
blocks and waits for an external signal instead of returning `AWAITING_APPROVAL`
immediately:

1. Publish `tool.awaiting_approval` event with tool name and params
2. Create an `asyncio.Event`
3. Store it in a `_pending_approvals: dict[str, asyncio.Event]` keyed by a unique request ID
4. Await the event
5. On signal, check approved/rejected and proceed or cancel

```python
class CoreToolScheduler:
    def __init__(self, ...):
        ...
        self._pending_approvals: dict[str, tuple[asyncio.Event, bool]] = {}

    def resolve_approval(self, request_id: str, approved: bool) -> None:
        if request_id in self._pending_approvals:
            event, _ = self._pending_approvals[request_id]
            self._pending_approvals[request_id] = (event, approved)
            event.set()
```

### Web endpoint

```
POST /approve
Body: {"request_id": "...", "approved": true}
```

The endpoint finds the pending event in the scheduler and sets it.

### Frontend

When SSE receives `{"type": "approval_required", "request_id": "...", "tool": "...", "params": {...}}`:
- Render an approval card with [Approve] [Reject] buttons
- On click, POST to `/approve` with the request_id and decision
- The SSE stream then continues with `tool_result` or `error`

## Section 4: Error Handling

| Scenario | Behavior |
|----------|----------|
| LLM returns unknown tool name | Return error text as tool result, let LLM recover |
| Tool execution fails | Return error content as tool result |
| User rejects approval | Return "rejected by user" as tool result |
| Max rounds exceeded | Return predefined fallback message |
| LLM API failure | Propagate to SSE handler, frontend shows error |
| Streaming tool_call delta | Stop yielding tokens, accumulate tool_call, yield `tool_call` event when complete |

### `max_rounds` configuration

Added to `Settings`:

```python
class AgentSettings(BaseModel):
    max_tool_rounds: int = 10
```

## Files Changed

| File | Change |
|------|--------|
| `tools/registry.py` | Add `to_openai_schemas()` |
| `tools/scheduler.py` | Add blocking approval with `asyncio.Event` |
| `agent/react.py` | Add `run_loop()` with multi-round logic |
| `agent/toolcall.py` | Change `think()` signature to `messages` + `tools`, remove `tool:` hack |
| `agent/multiclaw.py` | Update `handle_message_stream()` for tool_call/tool_result events |
| `llm/providers.py` | `parse_stream_chunk()` also collect tool_call deltas |
| `config/settings.py` | Add `AgentSettings.max_tool_rounds` |
| `server.py` | Add `POST /approve`, update SSE handling for new event types, frontend approval cards |

## Verification

1. **Unit tests:** Tool schema generation, multi-round loop with mocked LLM, approval flow
2. **Manual test with `./start.sh`:**
   - Register a real tool (e.g., `echo`)
   - Send: "echo hello world" → agent calls echo tool via LLM → streams result
   - Register a guarded tool → verify approval card appears → approve → executes
   - Send complex query that triggers multiple tool calls → verify round counting
