import json
import re
from pathlib import Path

import aiosqlite

from multiclaw.memory.models import MemoryEntry
from multiclaw.memory.protocol import MemoryProtocol


class SqliteMemory(MemoryProtocol):
    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        if self._database_path != ":memory:":
            Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._database_path)
        self._db.row_factory = aiosqlite.Row
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
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def save(self, entry: MemoryEntry) -> MemoryEntry:
        db = await self._ensure_db()
        await db.execute(
            """
            INSERT INTO memory_entries (
                id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                type = excluded.type,
                tenant_id = excluded.tenant_id,
                session_id = excluded.session_id,
                role = excluded.role,
                turn_index = excluded.turn_index,
                created_at = excluded.created_at,
                metadata = excluded.metadata
            """,
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
            ),
        )
        await db.commit()
        return entry

    async def query(
        self,
        query: str,
        top_k: int,
        entry_type: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        include_legacy: bool = False,
    ) -> list[MemoryEntry]:
        terms = _terms(query)
        rows = await self._load_rows(
            entry_type=entry_type,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        ranked = [
            (row, _score(row["content"], terms))
            for row in rows
        ]
        ranked.sort(key=lambda item: (item[1], item[0]["sort_order"]), reverse=True)
        return [
            _row_to_entry(row)
            for row, score in ranked
            if score > 0
        ][:top_k]

    async def recent(
        self,
        limit: int,
        entry_type: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryEntry]:
        rows = await self._load_rows(
            entry_type=entry_type,
            tenant_id=tenant_id,
            session_id=session_id,
            order="DESC",
            limit=limit,
        )
        return [_row_to_entry(row) for row in rows]

    async def context(
        self,
        max_chars: int,
        limit: int,
        entry_type: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryEntry]:
        selected: list[MemoryEntry] = []
        used = 0
        for entry in await self.recent(
            limit=limit,
            entry_type=entry_type,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            entry_len = len(entry.content)
            separator = 1 if selected else 0
            if used + separator + entry_len > max_chars:
                continue
            selected.append(entry)
            used += separator + entry_len
        return list(reversed(selected))

    async def forget(self, entry_id: str) -> None:
        db = await self._ensure_db()
        await db.execute("DELETE FROM memory_entries WHERE id = ?", (entry_id,))
        await db.commit()

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.initialize()
        assert self._db is not None
        return self._db

    async def _load_rows(
        self,
        entry_type: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        order: str = "ASC",
        limit: int | None = None,
    ) -> list[aiosqlite.Row]:
        db = await self._ensure_db()
        clauses: list[str] = []
        values: list[str | int] = []
        if entry_type is not None:
            clauses.append("type = ?")
            values.append(entry_type)
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            values.append(tenant_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            values.append(session_id)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            values.append(limit)
        cursor = await db.execute(
            f"""
            SELECT sort_order, id, content, type, tenant_id, session_id, role, turn_index, created_at, metadata
            FROM memory_entries
            {where}
            ORDER BY sort_order {order}
            {limit_sql}
            """,
            tuple(values),
        )
        return await cursor.fetchall()


def _row_to_entry(row: aiosqlite.Row) -> MemoryEntry:
    return MemoryEntry(
        id=row["id"],
        content=row["content"],
        type=row["type"],
        tenant_id=row["tenant_id"],
        session_id=row["session_id"],
        role=row["role"],
        turn_index=row["turn_index"],
        created_at=row["created_at"],
        metadata=json.loads(row["metadata"]),
    )


def _terms(value: str) -> set[str]:
    return set(re.findall(r"\w+", value.lower()))


def _score(content: str, terms: set[str]) -> int:
    if not terms:
        return 0
    content_terms = _terms(content)
    return len(terms & content_terms)
