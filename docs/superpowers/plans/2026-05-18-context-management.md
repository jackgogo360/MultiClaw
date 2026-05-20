# Context Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit chat sessions plus hybrid recent-history and relevant-memory context assembly to MultiClaw without introducing transcript browsing or vector retrieval.

**Architecture:** Introduce a dedicated `session` package for session lifecycle metadata, extend the existing memory layer so chat messages are session-scoped, and add a focused context builder that assembles prompt context from recent turns plus relevant retrieved memory. Keep the current FastAPI server and inline HTML UI, but add minimal session APIs and session selection controls.

**Tech Stack:** Python 3.12+, FastAPI, aiosqlite, pydantic v2, pytest + pytest-asyncio, existing `multiclaw.agent`, `multiclaw.memory`, and `multiclaw.server`

## File Structure

```text
MultiClaw/
├── docs/superpowers/plans/2026-05-18-context-management.md
├── src/multiclaw/
│   ├── agent/
│   │   ├── context.py                 # new context assembly logic
│   │   └── multiclaw.py               # session-aware agent entrypoints
│   ├── config/settings.py             # memory/session settings
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── in_memory.py
│   │   ├── models.py
│   │   ├── protocol.py
│   │   └── sqlite.py
│   ├── server.py                      # session APIs + UI wiring
│   └── session/
│       ├── __init__.py
│       ├── models.py
│       └── sqlite.py
└── tests/
    ├── test_agent.py
    ├── test_context.py                # new
    ├── test_memory.py
    ├── test_server.py                 # new
    └── test_session.py                # new
```

### Task 1: Add the session store

**Files:**
- Create: `src/multiclaw/session/__init__.py`
- Create: `src/multiclaw/session/models.py`
- Create: `src/multiclaw/session/sqlite.py`
- Test: `tests/test_session.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_create_and_list_active_sessions(tmp_path):
    from multiclaw.session import SessionStatus, SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))
    created = await store.create()

    listed = await store.list_sessions()

    assert created.title == "New Chat"
    assert created.status is SessionStatus.ACTIVE
    assert [session.id for session in listed] == [created.id]


@pytest.mark.asyncio
async def test_archive_and_restore_session(tmp_path):
    from multiclaw.session import SessionStatus, SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))
    created = await store.create(title="Alpha")

    archived = await store.archive(created.id)
    active = await store.list_sessions()
    all_sessions = await store.list_sessions(include_archived=True)
    restored = await store.restore(created.id)

    assert archived.status is SessionStatus.ARCHIVED
    assert active == []
    assert [session.id for session in all_sessions] == [created.id]
    assert restored.status is SessionStatus.ACTIVE


@pytest.mark.asyncio
async def test_rename_validates_title(tmp_path):
    from multiclaw.session import InvalidSessionTitleError, SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))
    created = await store.create()

    with pytest.raises(InvalidSessionTitleError):
        await store.rename(created.id, " ")

    renamed = await store.rename(created.id, "Research notes")

    assert renamed.title == "Research notes"


@pytest.mark.asyncio
async def test_get_missing_session_returns_none(tmp_path):
    from multiclaw.session import SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))

    assert await store.get("missing") is None


@pytest.mark.asyncio
async def test_touch_message_updates_last_message_at_and_default_title(tmp_path):
    from multiclaw.session import SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))
    created = await store.create()

    touched = await store.touch_message(created.id, "first message becomes title")

    assert touched.last_message_at is not None
    assert touched.title == "first message becomes title"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'multiclaw.session'`

- [ ] **Step 3: Write the minimal implementation**

Create `src/multiclaw/session/models.py`:

```python
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ChatSession(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    title: str = "New Chat"
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_message_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionError(Exception):
    pass


class InvalidSessionTitleError(SessionError):
    pass
```

Create `src/multiclaw/session/sqlite.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from multiclaw.session.models import ChatSession, InvalidSessionTitleError, SessionStatus


class SqliteSessionStore:
    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._database_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_message_at TEXT,
                metadata TEXT NOT NULL
            )
            """
        )
        await self._db.commit()

    async def create(self, title: str = "New Chat") -> ChatSession:
        title = _validate_title(title)
        session = ChatSession(title=title)
        db = await self._ensure_db()
        await db.execute(
            """
            INSERT INTO chat_sessions (
                id, title, status, created_at, updated_at, last_message_at, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.title,
                session.status.value,
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                None,
                json.dumps(session.metadata),
            ),
        )
        await db.commit()
        return session

    async def get(self, session_id: str) -> ChatSession | None:
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM chat_sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return _row_to_session(row) if row else None

    async def list_sessions(self, include_archived: bool = False) -> list[ChatSession]:
        db = await self._ensure_db()
        query = "SELECT * FROM chat_sessions"
        params: tuple[str, ...] = ()
        if not include_archived:
            query += " WHERE status = ?"
            params = (SessionStatus.ACTIVE.value,)
        query += " ORDER BY COALESCE(last_message_at, created_at) DESC"
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [_row_to_session(row) for row in rows]

    async def rename(self, session_id: str, title: str) -> ChatSession:
        title = _validate_title(title)
        db = await self._ensure_db()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, session_id),
        )
        await db.commit()
        session = await self.get(session_id)
        assert session is not None
        return session

    async def archive(self, session_id: str) -> ChatSession:
        return await self._set_status(session_id, SessionStatus.ARCHIVED)

    async def restore(self, session_id: str) -> ChatSession:
        return await self._set_status(session_id, SessionStatus.ACTIVE)

    async def touch_message(self, session_id: str, content: str) -> ChatSession:
        db = await self._ensure_db()
        session = await self.get(session_id)
        assert session is not None
        now = datetime.now(timezone.utc).isoformat()
        title = session.title
        if title == "New Chat":
            title = content.strip()[:40] or "New Chat"
        await db.execute(
            """
            UPDATE chat_sessions
            SET title = ?, updated_at = ?, last_message_at = ?
            WHERE id = ?
            """,
            (title, now, now, session_id),
        )
        await db.commit()
        updated = await self.get(session_id)
        assert updated is not None
        return updated

    async def _set_status(self, session_id: str, status: SessionStatus) -> ChatSession:
        db = await self._ensure_db()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE chat_sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, now, session_id),
        )
        await db.commit()
        session = await self.get(session_id)
        assert session is not None
        return session

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.initialize()
        assert self._db is not None
        return self._db


def _validate_title(title: str) -> str:
    normalized = title.strip()
    if not normalized or len(normalized) > 120:
        raise InvalidSessionTitleError("title must be between 1 and 120 characters")
    return normalized


def _row_to_session(row: aiosqlite.Row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        title=row["title"],
        status=SessionStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_message_at=row["last_message_at"],
        metadata=json.loads(row["metadata"]),
    )
```

Create `src/multiclaw/session/__init__.py`:

```python
from multiclaw.session.models import ChatSession, InvalidSessionTitleError, SessionStatus
from multiclaw.session.sqlite import SqliteSessionStore

__all__ = ["ChatSession", "InvalidSessionTitleError", "SessionStatus", "SqliteSessionStore"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/session tests/test_session.py
git commit -m "Add explicit chat session store"
```

### Task 2: Extend memory for session-scoped chat messages

**Files:**
- Modify: `src/multiclaw/memory/models.py`
- Modify: `src/multiclaw/memory/protocol.py`
- Modify: `src/multiclaw/memory/in_memory.py`
- Modify: `src/multiclaw/memory/sqlite.py`
- Modify: `src/multiclaw/memory/__init__.py`
- Modify: `tests/test_memory.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_memory.py`:

```python
@pytest.mark.asyncio
async def test_sqlite_memory_keeps_recent_chat_messages_per_session(tmp_path):
    from multiclaw.memory import MemoryEntry, SqliteMemory

    memory = SqliteMemory(str(tmp_path / "memory.db"))
    await memory.save(
        MemoryEntry(
            content="first user",
            type="chat_message",
            session_id="s1",
            role="user",
            turn_index=1,
        )
    )
    await memory.save(
        MemoryEntry(
            content="other session",
            type="chat_message",
            session_id="s2",
            role="user",
            turn_index=1,
        )
    )
    await memory.save(
        MemoryEntry(
            content="first assistant",
            type="chat_message",
            session_id="s1",
            role="assistant",
            turn_index=2,
        )
    )

    results = await memory.recent(
        limit=3,
        session_id="s1",
        entry_type="chat_message",
    )

    assert [entry.content for entry in results] == ["first assistant", "first user"]


@pytest.mark.asyncio
async def test_sqlite_memory_query_filters_legacy_entries_by_flag_shape(tmp_path):
    from multiclaw.memory import MemoryEntry, SqliteMemory

    memory = SqliteMemory(str(tmp_path / "memory.db"))
    await memory.save(MemoryEntry(content="legacy alpha", type="note"))
    await memory.save(
        MemoryEntry(
            content="session alpha",
            type="note",
            session_id="s1",
        )
    )

    results = await memory.query("alpha", top_k=5, session_id="s1")

    assert [entry.content for entry in results] == ["session alpha"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_memory.py -v`
Expected: FAIL because `MemoryEntry` has no `session_id/role/turn_index` fields and the memory stores do not filter by session.

- [ ] **Step 3: Write the minimal implementation**

Update `src/multiclaw/memory/models.py`:

```python
from datetime import datetime, timezone
import uuid
from typing import Any

from pydantic import BaseModel, Field


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    content: str
    type: str
    tenant_id: str = ""
    session_id: str = ""
    role: str = "note"
    turn_index: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
```

Update `src/multiclaw/memory/protocol.py`:

```python
from abc import ABC, abstractmethod

from multiclaw.memory.models import MemoryEntry


class MemoryProtocol(ABC):
    @abstractmethod
    async def save(self, entry: MemoryEntry) -> MemoryEntry: ...

    @abstractmethod
    async def query(
        self,
        query: str,
        top_k: int,
        entry_type: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        include_legacy: bool = False,
    ) -> list[MemoryEntry]: ...

    @abstractmethod
    async def recent(
        self,
        limit: int,
        entry_type: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryEntry]: ...

    @abstractmethod
    async def context(
        self,
        max_chars: int,
        limit: int,
        entry_type: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryEntry]: ...

    @abstractmethod
    async def forget(self, entry_id: str) -> None: ...
```

Update the filter signatures in `src/multiclaw/memory/in_memory.py` and `src/multiclaw/memory/sqlite.py` so `query`, `recent`, and `context` accept `session_id`, and only match `entry.session_id == session_id` unless `include_legacy=True`.

Update the SQLite schema and row mapping in `src/multiclaw/memory/sqlite.py`:

```python
await self._db.execute(
    """
    CREATE TABLE IF NOT EXISTS memory_entries (
        sort_order INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        content TEXT NOT NULL,
        type TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        session_id TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'note',
        turn_index INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        metadata TEXT NOT NULL
    )
    """
)
```

And insert/read the new columns:

```python
(
    entry.id,
    entry.content,
    entry.type,
    entry.tenant_id,
    entry.session_id,
    entry.role,
    entry.turn_index,
    entry.created_at.isoformat(),
    json.dumps(entry.metadata),
)
```

Update `src/multiclaw/memory/__init__.py` exports if needed:

```python
from multiclaw.memory.in_memory import InMemoryMemory
from multiclaw.memory.models import MemoryEntry
from multiclaw.memory.protocol import MemoryProtocol
from multiclaw.memory.sqlite import SqliteMemory
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_memory.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/memory tests/test_memory.py
git commit -m "Make memory session-aware"
```

### Task 3: Add a context builder

**Files:**
- Create: `src/multiclaw/agent/context.py`
- Test: `tests/test_context.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_context.py`:

```python
import pytest

from multiclaw.memory import InMemoryMemory, MemoryEntry


@pytest.mark.asyncio
async def test_context_builder_orders_recent_history_then_relevant_memory():
    from multiclaw.agent.context import ContextBuilder, ContextRequest

    memory = InMemoryMemory()
    await memory.save(
        MemoryEntry(
            content="older note about alpha",
            type="note",
            session_id="s1",
        )
    )
    await memory.save(
        MemoryEntry(
            content="hello",
            type="chat_message",
            session_id="s1",
            role="user",
            turn_index=1,
        )
    )
    await memory.save(
        MemoryEntry(
            content="hi there",
            type="chat_message",
            session_id="s1",
            role="assistant",
            turn_index=2,
        )
    )

    builder = ContextBuilder(memory=memory, recent_turns=8, context_history_ratio=0.5)
    messages = await builder.build(
        ContextRequest(
            system_prompt="system",
            user_input="alpha status?",
            session_id="s1",
            context_window_limit=1000,
        )
    )

    assert messages[0] == {"role": "system", "content": "system"}
    assert messages[1] == {"role": "user", "content": "hello"}
    assert messages[2] == {"role": "assistant", "content": "hi there"}
    assert messages[3]["role"] == "system"
    assert "Relevant memory:" in messages[3]["content"]
    assert messages[4] == {"role": "user", "content": "alpha status?"}


@pytest.mark.asyncio
async def test_context_builder_does_not_duplicate_recent_history_in_relevant_memory():
    from multiclaw.agent.context import ContextBuilder, ContextRequest

    memory = InMemoryMemory()
    await memory.save(
        MemoryEntry(
            content="alpha project",
            type="chat_message",
            session_id="s1",
            role="user",
            turn_index=1,
        )
    )

    builder = ContextBuilder(memory=memory, recent_turns=8, context_history_ratio=0.5)
    messages = await builder.build(
        ContextRequest(
            system_prompt="system",
            user_input="alpha project",
            session_id="s1",
            context_window_limit=1000,
        )
    )

    assert len([msg for msg in messages if msg["role"] == "system"]) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'multiclaw.agent.context'`

- [ ] **Step 3: Write the minimal implementation**

Create `src/multiclaw/agent/context.py`:

```python
from dataclasses import dataclass

from multiclaw.memory import MemoryEntry, MemoryProtocol


@dataclass
class ContextRequest:
    system_prompt: str
    user_input: str
    session_id: str
    context_window_limit: int


class ContextBuilder:
    def __init__(
        self,
        memory: MemoryProtocol,
        recent_turns: int,
        context_history_ratio: float,
        include_legacy_memory: bool = False,
    ) -> None:
        self.memory = memory
        self.recent_turns = recent_turns
        self.context_history_ratio = context_history_ratio
        self.include_legacy_memory = include_legacy_memory

    async def build(self, request: ContextRequest) -> list[dict]:
        messages = [{"role": "system", "content": request.system_prompt}]

        recent_entries = await self.memory.recent(
            limit=self.recent_turns * 2,
            entry_type="chat_message",
            session_id=request.session_id,
        )
        recent_entries = list(reversed(recent_entries))
        for entry in recent_entries:
            messages.append({"role": entry.role, "content": entry.content})

        recent_contents = {entry.content for entry in recent_entries}
        relevant_entries = await self.memory.query(
            request.user_input,
            top_k=5,
            session_id=request.session_id,
            include_legacy=self.include_legacy_memory,
        )
        relevant_entries = [
            entry
            for entry in relevant_entries
            if entry.content not in recent_contents and entry.type != "chat_message"
        ]
        relevant_text = self._fit_relevant_memory(
            relevant_entries,
            request.context_window_limit,
        )
        if relevant_text:
            messages.append({"role": "system", "content": relevant_text})

        messages.append({"role": "user", "content": request.user_input})
        return messages

    def _fit_relevant_memory(
        self,
        entries: list[MemoryEntry],
        context_window_limit: int,
    ) -> str:
        budget = int(context_window_limit * self.context_history_ratio)
        lines: list[str] = []
        used = 0
        for entry in entries:
            line = f"- [{entry.type}] {entry.content}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""
        return "Relevant memory:\n" + "\n".join(lines)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_context.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/agent/context.py tests/test_context.py
git commit -m "Add hybrid context builder"
```

### Task 4: Integrate sessions and context into the agent

**Files:**
- Modify: `src/multiclaw/agent/multiclaw.py`
- Modify: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent.py`:

```python
@pytest.mark.asyncio
async def test_agent_saves_user_and_assistant_messages_with_session_id(agent):
    from multiclaw.memory import MemoryEntry

    session_id = "session-1"
    await agent.memory.save(
        MemoryEntry(
            content="prior note",
            type="note",
            session_id=session_id,
        )
    )

    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "response"}}]
    }
    mock_response.raise_for_status = Mock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.post.return_value = mock_response

    with patch("httpx.AsyncClient", return_value=mock_client):
        await agent.handle_message("hello", session_id=session_id)

    recent = await agent.memory.recent(
        limit=2,
        entry_type="chat_message",
        session_id=session_id,
    )

    assert [entry.role for entry in recent] == ["assistant", "user"]
    assert [entry.content for entry in recent] == ["response", "hello"]


@pytest.mark.asyncio
async def test_agent_uses_recent_chat_history_for_same_session(agent):
    from multiclaw.memory import MemoryEntry

    await agent.memory.save(
        MemoryEntry(
            content="session one user",
            type="chat_message",
            session_id="s1",
            role="user",
            turn_index=1,
        )
    )
    await agent.memory.save(
        MemoryEntry(
            content="session two user",
            type="chat_message",
            session_id="s2",
            role="user",
            turn_index=1,
        )
    )

    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}]
    }
    mock_response.raise_for_status = Mock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.post.return_value = mock_response

    with patch("httpx.AsyncClient", return_value=mock_client):
        await agent.handle_message("follow-up", session_id="s1")

    messages = mock_client.post.call_args.kwargs["json"]["messages"]
    payload = [message["content"] for message in messages if "content" in message]

    assert "session one user" in payload
    assert "session two user" not in payload
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_agent.py -v`
Expected: FAIL because `handle_message()` does not accept `session_id` and does not save assistant chat messages.

- [ ] **Step 3: Write the minimal implementation**

Update `src/multiclaw/agent/multiclaw.py`:

```python
from multiclaw.agent.context import ContextBuilder, ContextRequest
```

Add context builder to `__init__`:

```python
self.context_builder = ContextBuilder(
    memory=memory,
    recent_turns=settings.memory.recent_turns,
    context_history_ratio=settings.memory.context_history_ratio,
    include_legacy_memory=settings.memory.include_legacy_memory_in_retrieval,
)
```

Change method signatures:

```python
async def handle_message(self, user_input: str, session_id: str = "") -> Observation:
```

```python
async def handle_message_stream(
    self,
    user_input: str,
    session_id: str = "",
) -> AsyncIterator[dict[str, Any]]:
```

Use the context builder:

```python
messages = await self.context_builder.build(
    ContextRequest(
        system_prompt=self.settings.agent.system_prompt,
        user_input=user_input,
        session_id=session_id,
        context_window_limit=self.settings.memory.context_window_limit,
    )
)
```

Save chat messages with explicit role/type/session:

```python
await self.memory.save(
    MemoryEntry(
        content=user_input,
        type="chat_message",
        role="user",
        session_id=session_id,
    )
)
```

Before returning a final text observation:

```python
await self.memory.save(
    MemoryEntry(
        content=response.content,
        type="chat_message",
        role="assistant",
        session_id=session_id,
    )
)
```

After saving chat messages, update session activity:

```python
await self.session_store.touch_message(session_id, user_input)
await self.session_store.touch_message(session_id, response.content)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_agent.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/agent/multiclaw.py tests/test_agent.py
git commit -m "Make agent session-aware"
```

### Task 5: Add session APIs and minimal UI wiring

**Files:**
- Modify: `src/multiclaw/config/settings.py`
- Modify: `src/multiclaw/server.py`
- Create: `tests/test_server.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_server.py`:

```python
from fastapi.testclient import TestClient


def test_sessions_endpoint_lists_created_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        listed = client.get("/sessions").json()

    assert created["title"] == "Alpha"
    assert [session["id"] for session in listed] == [created["id"]]


def test_session_lifecycle_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        renamed = client.patch(
            f"/sessions/{created['id']}",
            json={"title": "Beta"},
        ).json()
        archived = client.post(f"/sessions/{created['id']}/archive").json()
        listed = client.get("/sessions").json()
        all_sessions = client.get("/sessions?include_archived=true").json()
        restored = client.post(f"/sessions/{created['id']}/restore").json()

    assert renamed["title"] == "Beta"
    assert archived["status"] == "archived"
    assert listed == []
    assert [session["id"] for session in all_sessions] == [created["id"]]
    assert restored["status"] == "active"


def test_chat_without_session_emits_session_event(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        response = client.post("/chat", json={"message": "hello"})

    body = response.text
    assert '"type": "session"' in body
    assert '"session_id":' in body


def test_chat_rejects_archived_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        client.post(f"/sessions/{created['id']}/archive")
        response = client.post("/chat", json={"message": "hello", "session_id": created["id"]})

    assert response.status_code == 409
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL because `/sessions` does not exist and `/chat` cannot emit session metadata.

- [ ] **Step 3: Write the minimal implementation**

Update `src/multiclaw/config/settings.py`:

```python
class MemorySettings(BaseModel):
    short_term_limit: int = 100
    context_window_limit: int = 128000
    recent_turns: int = 8
    context_history_ratio: float = 0.5
    include_archived_in_retrieval: bool = False
    include_legacy_memory_in_retrieval: bool = False
```

Update `src/multiclaw/server.py` request models:

```python
class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class SessionCreateRequest(BaseModel):
    title: str = "New Chat"


class SessionRenameRequest(BaseModel):
    title: str
```

Create the session store alongside the memory store in `create_agent()`:

```python
from multiclaw.session import SqliteSessionStore
```

```python
session_store = SqliteSessionStore(settings.database.path)
memory = SqliteMemory(settings.database.path)
```

Attach the session store on the agent for first-pass wiring:

```python
runtime_agent = MultiClawAgent(...)
runtime_agent.session_store = session_store  # first-pass runtime dependency
return runtime_agent
```

Add endpoints:

```python
@app.get("/sessions")
async def list_sessions(include_archived: bool = False):
    sessions = await agent.session_store.list_sessions(include_archived=include_archived)
    return [session.model_dump(mode="json") for session in sessions]


@app.post("/sessions")
async def create_session(req: SessionCreateRequest):
    session = await agent.session_store.create(title=req.title)
    return session.model_dump(mode="json")


@app.patch("/sessions/{session_id}")
async def rename_session(session_id: str, req: SessionRenameRequest):
    session = await agent.session_store.rename(session_id, req.title)
    return session.model_dump(mode="json")


@app.post("/sessions/{session_id}/archive")
async def archive_session(session_id: str):
    session = await agent.session_store.archive(session_id)
    return session.model_dump(mode="json")


@app.post("/sessions/{session_id}/restore")
async def restore_session(session_id: str):
    session = await agent.session_store.restore(session_id)
    return session.model_dump(mode="json")
```

Update `/chat`:

```python
session = None
if req.session_id:
    session = await agent.session_store.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.status.value == "archived":
        raise HTTPException(status_code=409, detail="session is archived")
else:
    session = await agent.session_store.create()
```

Emit the SSE session event before streaming:

```python
yield (
    "data: "
    + json.dumps(
        {
            "type": "session",
            "session_id": session.id,
            "title": session.title,
        }
    )
    + "\n\n"
)
```

Pass `session.id` into the agent:

```python
async for item in agent.handle_message_stream(req.message, session_id=session.id):
```

Add minimal session UI state in the inline HTML:

```javascript
let currentSessionId = null;
let sessions = [];
```

When the SSE stream emits `type === 'session'`, store `currentSessionId` and refresh `/sessions`.

Add minimal UI actions:

```javascript
async function loadSessions() {
  const res = await fetch('/sessions');
  sessions = await res.json();
  renderSessions();
}

async function createSession() {
  const res = await fetch('/sessions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: 'New Chat'}),
  });
  const session = await res.json();
  currentSessionId = session.id;
  await loadSessions();
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/config/settings.py src/multiclaw/server.py tests/test_server.py
git commit -m "Add session APIs and chat session wiring"
```

### Task 6: Verify the whole feature set

**Files:**
- Verify only

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run pytest tests/test_session.py tests/test_memory.py tests/test_context.py tests/test_agent.py tests/test_server.py -v
```

Expected: PASS

- [ ] **Step 2: Run the full test suite**

Run:

```bash
uv run pytest
```

Expected: PASS

- [ ] **Step 3: Run a syntax/compile pass**

Run:

```bash
uv run python -m compileall src tests
```

Expected: PASS

- [ ] **Step 4: Manual smoke test**

Run:

```bash
./start.sh
```

Manual checks:

- Create a new session.
- Send a first message and confirm a session event is emitted.
- Create a second session and confirm later messages do not reuse the first session's recent history.
- Rename a session and verify it appears in the list.
- Archive and restore a session.

- [ ] **Step 5: Commit any final integration fixes**

```bash
git add src tests
git commit -m "Finalize session-scoped context management"
```

## Self-Review

Spec coverage:

- Session lifecycle: covered in Task 1 and Task 5.
- Session-scoped memory entries: covered in Task 2.
- Hybrid recent-history plus retrieval context: covered in Task 3 and Task 4.
- `/chat` session creation, archived-session rejection, and session routing: covered in Task 5.
- Verification strategy: covered in Task 6.

Placeholder scan:

- No placeholder markers or deferred implementation notes remain in task steps.

Type consistency:

- `ChatSession`, `SqliteSessionStore`, `MemoryEntry.session_id`, and `ContextBuilder` are named consistently across tasks.
- `handle_message(..., session_id=...)` and `handle_message_stream(..., session_id=...)` use the same API surface throughout the plan.
