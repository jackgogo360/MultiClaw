# Session Delete & History Loading

## Overview

Two feature additions to the MultiClaw frontend:
1. Hard-delete sessions (with all associated messages) from the sidebar
2. Load and display recent chat history when switching to a session

## Backend Changes

### DELETE /sessions/{session_id}

Hard-deletes a session and all its associated `memory_entries`.

- Deletes the row from `chat_sessions`
- Deletes all rows from `memory_entries` where `session_id` matches
- Both operations in a single SQLite transaction
- Returns `{"ok": true}`

New method on `SqliteSessionStore`: `delete(session_id) -> None`

### GET /sessions/{session_id}/messages?limit=50

Returns recent user/assistant text messages for a session.

- Queries `memory_entries` where `session_id` matches, `type="chat_message"`, `role IN ("user", "assistant")`
- Ordered by `sort_order DESC`, limited to `limit` (default 50)
- Returns `[{role, content, created_at}, ...]` in chronological order

New method on `SqliteSessionStore`: `get_messages(session_id, limit=50) -> list[dict]`

### Server wiring

- `sqlite_memory` instance passed to `SqliteSessionStore` constructor so it can query `memory_entries`
- Two new FastAPI routes added to `server.py`

## Frontend Changes

### Delete button

Each `.session-item` gets a delete button (× icon) on the right side:
- Visible on hover only (CSS `opacity` transition)
- Click triggers `confirm("Delete this conversation?")` 
- On confirm: `DELETE /sessions/{id}`, refresh session list
- If deleting current active session: clear messages area, reset to welcome state, clear `currentSessionId`

### History loading

`switchSession(id)` modified to:
1. Set `currentSessionId` and update title in top bar
2. `GET /sessions/{id}/messages?limit=50`
3. Clear `#messages`, hide welcome
4. Render each message as a `.msg-wrap` (user messages right-aligned, agent messages left-aligned, text only, no tool cards)
5. Scroll to bottom

### CSS additions

- `.session-item .delete-btn` — positioned right, hidden by default, shown on `.session-item:hover`
- Hover state: red color on delete button hover

## Files Changed

| File | Change |
|------|--------|
| `src/multiclaw/session/sqlite.py` | Add `delete()`, `get_messages()` methods |
| `src/multiclaw/server.py` | Add `DELETE /sessions/{id}`, `GET /sessions/{id}/messages` routes; wire `memory` into session store |
| `src/multiclaw/static/index.html` | Delete button UI + logic, `switchSession` history loading, CSS |
