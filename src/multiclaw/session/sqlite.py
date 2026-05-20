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
