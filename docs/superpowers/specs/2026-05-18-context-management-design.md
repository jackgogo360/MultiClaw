# MultiClaw Context Management Design

**Date:** 2026-05-18
**Status:** Approved for implementation planning
**Scope:** Explicit multi-session management plus hybrid prompt context assembly

## Decisions

| Decision | Choice |
|----------|--------|
| Session model | Explicit multi-session management |
| First-version feature level | Create, list, switch, rename, archive, restore |
| Context strategy | Recent session turns plus relevant memory under a budget |
| Archived sessions | Excluded from retrieval by default; controlled by `include_archived_in_retrieval` |
| History browsing | Out of scope for this version |
| Retrieval | Existing keyword scoring; no vector dependency in this phase |

## Goals

MultiClaw should stop treating persisted memory as one shared pool. The runtime
needs explicit chat sessions so context can be isolated, resumed, renamed, and
archived. Each request should receive enough recent conversation to preserve
local continuity, while still allowing a small amount of relevant older memory
to be injected when useful.

This design keeps the implementation small enough for the current runtime. It
does not introduce vector search, full transcript browsing, or a knowledge/RAG
pipeline.

## Architecture

Use a two-layer model:

1. `SessionStore` owns session lifecycle and metadata.
2. `MemoryProtocol` owns message and memory entries used to build prompt context.

Session state and message content stay separate. Session list, title, archive,
and restore operations use `SessionStore`. Prompt construction uses memory
entries filtered by `session_id`, `role`, `type`, and archive policy.

## Data Model

### ChatSession

Create a new model and SQLite table for session metadata:

```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_message_at TEXT,
    metadata TEXT NOT NULL
);
```

Fields:

- `id`: stable session identifier.
- `title`: user-visible session title.
- `status`: `active` or `archived`.
- `created_at`: UTC creation timestamp.
- `updated_at`: UTC metadata update timestamp.
- `last_message_at`: UTC timestamp of the newest saved chat message.
- `metadata`: JSON object for future non-contractual attributes.

### MemoryEntry

Extend `MemoryEntry` so it can represent chat history:

- `session_id`: session that owns this entry. Empty string means legacy/global.
- `role`: `user`, `assistant`, `tool`, `system`, or `note`.
- `turn_index`: monotonically increasing integer within a session.
- `content`: stored text.
- `type`: existing category such as `chat_message`, `tool_result`, or `note`.
- `tenant_id`: existing tenant field.
- `created_at`: UTC timestamp.
- `metadata`: JSON object.

Extend the existing SQLite `memory_entries` table with:

```sql
ALTER TABLE memory_entries ADD COLUMN session_id TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_entries ADD COLUMN role TEXT NOT NULL DEFAULT 'note';
ALTER TABLE memory_entries ADD COLUMN turn_index INTEGER NOT NULL DEFAULT 0;
```

Legacy entries remain valid. They do not appear in a new session's recent
history. Whether they can participate in relevant-memory retrieval is controlled
by configuration and defaults to off.

## Backend API

Add session management endpoints:

```text
GET    /sessions
POST   /sessions
PATCH  /sessions/{session_id}
POST   /sessions/{session_id}/archive
POST   /sessions/{session_id}/restore
```

`GET /sessions` returns active sessions by default and supports
`include_archived=true`.

`POST /sessions` accepts an optional title. If no title is supplied, create the
session with `New Chat`; after the first user message, replace the default title
with a title derived from the first message.

`PATCH /sessions/{session_id}` only changes title. Title validation rejects empty
strings and titles longer than 120 characters.

Archive and restore use dedicated endpoints so lifecycle changes stay explicit.

Update chat requests:

```json
{
  "session_id": "session-id",
  "message": "user message"
}
```

If `session_id` is omitted, `/chat` creates a new session and sends an SSE event
before normal chat events:

```json
{"type": "session", "session_id": "session-id", "title": "New Chat"}
```

If `session_id` is present but unknown, return 404. If the session is archived,
return 409 and require restore before accepting new messages.

## Frontend Interaction

The first UI version adds a left session rail:

- New session button.
- Active session list.
- Session selection.
- Rename current session.
- Archive current session.
- Archived view or filter with restore action.

Switching sessions changes the `session_id` used by future `/chat` requests.
This version does not restore and render the full transcript when switching.
Full history browsing requires a separate message-history API and is out of
scope.

## Context Assembly

Every chat request builds prompt context in this order:

```text
system prompt
+ current session recent turns
+ relevant memory
+ current user message
```

Rules:

- Build context before saving the current user message so the request cannot
  retrieve itself.
- Recent history comes only from the current active session.
- Recent history includes `type="chat_message"` entries with role `user` or
  `assistant`, sorted by `turn_index` ascending.
- Default recent target is 8 turns, which is at most 16 user/assistant messages.
- Tool results are not included as raw recent history in the first version.
- Relevant memory is keyword-scored using the existing memory query behavior.
- Relevant memory excludes archived sessions by default.
- Relevant memory excludes entries already present in recent history.
- Budgeting uses `MemorySettings.context_window_limit` as a coarse character
  budget. No tokenizer dependency is introduced in this phase.
- History plus retrieved memory may use at most 50 percent of the configured
  context window. The remaining budget is reserved for the current message,
  system prompt, tool messages, and model response.

Relevant-memory retrieval first checks the current session. Cross-session
retrieval from active sessions can be added behind a setting after the basic
session boundary is stable. Archived-session retrieval remains disabled by
default even if cross-session retrieval is enabled.

## Storage Timing

For normal chat:

1. Validate or create the session.
2. Build prompt context.
3. Save the user message with `role="user"` and `type="chat_message"`.
4. Execute the existing LLM/tool loop.
5. Save the final assistant response with `role="assistant"` and
   `type="chat_message"`.
6. Save tool results as `role="tool"` and `type="tool_result"`.
7. Update `ChatSession.last_message_at` and `updated_at`.
8. If the session still has the default title, derive a title from the first
   user message.

Turn indexes are assigned by the storage layer from the current session's max
`turn_index + 1`, not by callers guessing the next value.

## Error Handling

- Unknown `session_id`: return 404.
- Archived `session_id` in `/chat`: return 409.
- Empty or overlong title: return 422.
- Context assembly failure: log the error and degrade to system prompt plus
  current user message. The chat request should not fail solely because memory
  retrieval failed.
- Session creation failure: fail the chat request, because the message would
  otherwise have no safe persistence boundary.

## Configuration

Add memory settings:

```python
recent_turns: int = 8
context_history_ratio: float = 0.5
include_archived_in_retrieval: bool = False
include_legacy_memory_in_retrieval: bool = False
```

Existing `short_term_limit` can remain for compatibility, but new context
assembly should use `recent_turns`.

## Testing

Unit tests:

- `SessionStore` creates, lists, renames, archives, restores, and validates
  titles.
- `SessionStore` excludes archived sessions unless `include_archived=true`.
- `MemoryProtocol` stores and retrieves entries by `session_id`, `role`, and
  `type`.
- `MemoryProtocol` returns recent chat messages by `turn_index`.
- `ContextBuilder` orders context as system prompt, recent history, relevant
  memory, current user message.
- `ContextBuilder` does not retrieve the current user message.
- `ContextBuilder` removes duplicate relevant memory already present in recent
  history.
- `ContextBuilder` respects the character budget.
- `MultiClawAgent` saves user and assistant chat messages with the same
  `session_id`.
- `MultiClawAgent` rejects archived sessions.

Server tests:

- `POST /chat` without `session_id` emits a `session` SSE event.
- `POST /chat` with unknown `session_id` returns 404.
- `POST /chat` with archived `session_id` returns 409.
- `GET /sessions` lists active sessions.
- `GET /sessions?include_archived=true` includes archived sessions.

## Out of Scope

- Full transcript browsing and replay.
- Deleting individual messages.
- Clearing a session.
- LLM-generated titles.
- Summarization of old turns.
- Vector search or semantic retrieval.
- Knowledge vault integration.
- Multi-user authentication or tenant enforcement beyond existing `tenant_id`.
