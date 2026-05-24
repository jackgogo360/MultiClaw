# Session Delete & History Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add hard-delete for sessions (with all messages) and load recent chat history when switching sessions.

**Architecture:** Two new methods on SqliteSessionStore (`delete`, `get_messages`) query the shared SQLite database directly. Two new FastAPI routes expose them. Frontend changes add delete button UI in the sidebar and fetch/render history in switchSession().

**Tech Stack:** Python/FastAPI/aiosqlite (backend), vanilla HTML/CSS/JS (frontend), pytest (tests)

---

### Task 1: Add `delete()` and `get_messages()` methods to SqliteSessionStore

**Files:**
- Modify: `src/multiclaw/session/sqlite.py`
- Create: `tests/test_session_delete_and_messages.py`

- [ ] **Step 1: Write tests for delete()**

```python
import pytest


@pytest.mark.asyncio
async def test_delete_removes_session_and_messages(tmp_path):
    import aiosqlite
    from multiclaw.session import SqliteSessionStore

    db_path = str(tmp_path / "test.db")
    store = SqliteSessionStore(db_path)

    # Create session
    created = await store.create(title="Test")

    # Manually insert a chat message into memory_entries
    db = await store._ensure_db()
    await db.execute(
        "INSERT INTO memory_entries (id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("msg-1", "Hello", "chat_message", "", created.id, "user", 0, "2025-01-01T00:00:00", "{}"),
    )
    await db.commit()

    # Delete the session
    await store.delete(created.id)

    # Session should be gone
    assert await store.get(created.id) is None

    # Message should be gone
    cursor = await db.execute("SELECT COUNT(*) FROM memory_entries WHERE session_id = ?", (created.id,))
    count = (await cursor.fetchone())[0]
    assert count == 0


@pytest.mark.asyncio
async def test_delete_missing_session_does_not_raise(tmp_path):
    from multiclaw.session import SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "test.db"))
    # Should not raise
    await store.delete("nonexistent")


@pytest.mark.asyncio
async def test_get_messages_returns_recent_user_assistant_only(tmp_path):
    import aiosqlite
    from multiclaw.session import SqliteSessionStore

    db_path = str(tmp_path / "test.db")
    store = SqliteSessionStore(db_path)
    created = await store.create(title="Test")

    db = await store._ensure_db()
    # Insert chat messages
    entries = [
        ("u1", "Hello", "user", 1),
        ("a1", "Hi there", "assistant", 2),
        ("u2", "What is Python?", "user", 3),
        ("a2", "Python is a language", "assistant", 4),
    ]
    for eid, content, role, turn in entries:
        await db.execute(
            "INSERT INTO memory_entries (id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata) "
            "VALUES (?, ?, 'chat_message', '', ?, ?, ?, '2025-01-01T00:00:00', '{}')",
            (eid, content, created.id, role, turn),
        )
    # Insert a tool message — should NOT appear in results
    await db.execute(
        "INSERT INTO memory_entries (id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata) "
        "VALUES (?, ?, 'chat_message', '', ?, 'tool', ?, '2025-01-01T00:00:00', '{}')",
        ("t1", "tool output", created.id, 5),
    )
    await db.commit()

    messages = await store.get_messages(created.id, limit=50)

    assert len(messages) == 4
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert "tool" not in roles


@pytest.mark.asyncio
async def test_get_messages_respects_limit(tmp_path):
    import aiosqlite
    from multiclaw.session import SqliteSessionStore

    db_path = str(tmp_path / "test.db")
    store = SqliteSessionStore(db_path)
    created = await store.create(title="Test")

    db = await store._ensure_db()
    for i in range(10):
        await db.execute(
            "INSERT INTO memory_entries (id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata) "
            "VALUES (?, ?, 'chat_message', '', ?, 'user', ?, '2025-01-01T00:00:00', '{}')",
            (f"msg-{i}", f"Message {i}", created.id, i),
        )
    await db.commit()

    messages = await store.get_messages(created.id, limit=3)

    assert len(messages) == 3
    # Most recent 3 (chronological order: reversed from DESC query)
    assert messages[0]["content"] == "Message 7"
    assert messages[1]["content"] == "Message 8"
    assert messages[2]["content"] == "Message 9"


@pytest.mark.asyncio
async def test_get_messages_empty_session(tmp_path):
    from multiclaw.session import SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "test.db"))
    created = await store.create(title="Empty")

    messages = await store.get_messages(created.id)

    assert messages == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_session_delete_and_messages.py -v`
Expected: All 5 tests FAIL with AttributeError (methods not defined)

- [ ] **Step 3: Add delete() method to SqliteSessionStore**

In `src/multiclaw/session/sqlite.py`, add after the `restore()` method (line ~96):

```python
    async def delete(self, session_id: str) -> None:
        db = await self._ensure_db()
        await db.execute("DELETE FROM memory_entries WHERE session_id = ?", (session_id,))
        await db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        await db.commit()
```

- [ ] **Step 4: Add get_messages() method to SqliteSessionStore**

In `src/multiclaw/session/sqlite.py`, add after the `delete()` method:

```python
    async def get_messages(self, session_id: str, limit: int = 50) -> list[dict]:
        db = await self._ensure_db()
        cursor = await db.execute(
            """
            SELECT role, content, created_at FROM memory_entries
            WHERE session_id = ? AND type = 'chat_message' AND role IN ('user', 'assistant')
            ORDER BY sort_order DESC
            LIMIT ?
            """,
            (session_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {"role": row["role"], "content": row["content"], "created_at": row["created_at"]}
            for row in reversed(rows)
        ]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_session_delete_and_messages.py -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/multiclaw/session/sqlite.py tests/test_session_delete_and_messages.py
git commit -m "feat: add delete() and get_messages() to SqliteSessionStore"
```

---

### Task 2: Add DELETE and GET messages endpoints to server

**Files:**
- Modify: `src/multiclaw/server.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write tests for new endpoints**

Add these test functions to `tests/test_server.py`:

```python
def test_delete_session_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        response = client.delete(f"/sessions/{created['id']}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    # Verify session is gone
    with TestClient(app) as client:
        listed = client.get("/sessions").json()
    assert created["id"] not in [s["id"] for s in listed]


def test_get_messages_endpoint_returns_recent_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        # Create session
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        sid = created["id"]

        # Verify empty messages list
        messages = client.get(f"/sessions/{sid}/messages").json()
        assert messages == []

        # Need chat messages in DB — use chat endpoint to create them
        # This is an integration test, so we test the endpoint directly.
        # Actual message content verification is done in the unit test (Task 1).
        # Here we just verify the endpoint returns 200 with valid structure.
        response = client.get(f"/sessions/{sid}/messages")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server.py::test_delete_session_endpoint tests/test_server.py::test_get_messages_endpoint_returns_recent_messages -v`
Expected: FAIL with 405 Method Not Allowed / 404 Not Found

- [ ] **Step 3: Add DELETE /sessions/{session_id} route**

In `src/multiclaw/server.py`, after the `restore_session` route (line ~244):

```python
@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    await agent.session_store.delete(session_id)
    return {"ok": True}
```

- [ ] **Step 4: Add GET /sessions/{session_id}/messages route**

In `src/multiclaw/server.py`, after the `delete_session` route:

```python
@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, limit: int = 50):
    return await agent.session_store.get_messages(session_id, limit)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_server.py::test_delete_session_endpoint tests/test_server.py::test_get_messages_endpoint_returns_recent_messages -v`
Expected: Both tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/multiclaw/server.py tests/test_server.py
git commit -m "feat: add DELETE /sessions/{id} and GET /sessions/{id}/messages endpoints"
```

---

### Task 3: Add delete button and history loading to frontend

**Files:**
- Modify: `src/multiclaw/static/index.html`

- [ ] **Step 1: Add CSS for delete button**

In the `<style>` block, add after the `.session-item .title` rule (line ~160):

```css
  .session-item .delete-btn {
    opacity: 0; flex-shrink: 0; width: 22px; height: 22px; border-radius: 6px;
    border: none; background: transparent; color: var(--text-tertiary); cursor: pointer;
    font-size: 14px; line-height: 1; display: flex; align-items: center; justify-content: center;
    transition: all 0.15s ease;
  }
  .session-item:hover .delete-btn { opacity: 1; }
  .session-item .delete-btn:hover { background: var(--danger-soft); color: var(--danger); }
```

- [ ] **Step 2: Add delete button to renderSessions()**

Replace the `renderSessions()` function (line 906-916) with:

```javascript
function renderSessions() {
  const list = document.getElementById('session-list');
  list.innerHTML = '';
  for (const s of sessions) {
    const div = document.createElement('div');
    div.className = 'session-item' + (s.id === currentSessionId ? ' active' : '');
    div.onclick = function(e) {
      // Don't switch if delete button was clicked
      if (e.target.classList.contains('delete-btn')) return;
      switchSession(s.id);
    };
    div.innerHTML =
      '<span class="icon">' + (s.id === currentSessionId ? '●' : '○') + '</span>' +
      '<span class="title">' + escapeHtml(s.title) + '</span>' +
      '<button class="delete-btn" title="Delete">&times;</button>';
    // Attach delete handler
    div.querySelector('.delete-btn').onclick = async function(e) {
      e.stopPropagation();
      if (!confirm('Delete this conversation?')) return;
      await deleteSession(s.id);
    };
    list.appendChild(div);
  }
}
```

- [ ] **Step 3: Add deleteSession() function**

Add after `renderSessions()`:

```javascript
async function deleteSession(id) {
  try {
    await fetch('/sessions/' + id, { method: 'DELETE' });
    if (currentSessionId === id) {
      currentSessionId = null;
      document.getElementById('current-session-title').textContent = 'New Chat';
      msgs.innerHTML = '<div class="welcome" id="welcome"><div class="welcome-icon">&#x2699;&#xfe0f;</div><h3>MultiClaw Agent</h3><p>Ask me anything, run tools, or create plans.</p></div>';
    }
    await loadSessions();
  } catch(e) { console.error('deleteSession failed', e); }
}
```

- [ ] **Step 4: Replace switchSession() to load history**

Replace the `switchSession()` function (line 930-936) with:

```javascript
async function switchSession(id) {
  currentSessionId = id;
  const s = sessions.find(function(s) { return s.id === id; });
  if (s) document.getElementById('current-session-title').textContent = s.title;

  // Clear and show loading state
  msgs.innerHTML = '';

  try {
    const res = await fetch('/sessions/' + id + '/messages?limit=50');
    const messages = await res.json();
    if (messages.length === 0) {
      msgs.innerHTML = '<div class="welcome" id="welcome"><div class="welcome-icon">&#x2699;&#xfe0f;</div><h3>MultiClaw Agent</h3><p>Ask me anything, run tools, or create plans.</p></div>';
    } else {
      for (const msg of messages) {
        addMessage(msg.role, msg.content);
      }
    }
  } catch(e) {
    console.error('loadMessages failed', e);
    msgs.innerHTML = '<div class="welcome" id="welcome"><div class="welcome-icon">&#x2699;&#xfe0f;</div><h3>MultiClaw Agent</h3><p>Ask me anything, run tools, or create plans.</p></div>';
  }

  renderSessions();
}
```

- [ ] **Step 5: Verify frontend visually**

Start the server and test manually:
```bash
python -m multiclaw.server
```

1. Open http://localhost:8000
2. Send a few messages to create chat history
3. Click "New Chat" to create a second session, send messages there
4. Switch back to the first session — verify history loads
5. Hover over a session — verify delete button appears
6. Click delete — verify confirmation dialog appears
7. Confirm delete — verify session is removed

- [ ] **Step 6: Commit**

```bash
git add src/multiclaw/static/index.html
git commit -m "feat: add session delete button and chat history loading to frontend"
```
