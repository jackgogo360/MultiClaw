from __future__ import annotations
import re
from dataclasses import dataclass

from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from multiclaw.memory.models import MemoryEntry
from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect
from multiclaw.storage.schema import memory_entries
from multiclaw.tenancy.context import TenantContext


Dialect = SQLiteDialect | MySQLDialect


def _note_cleanup_error(primary: BaseException, phase: str, error: BaseException) -> None:
    primary.add_note(f"{phase} cleanup failed: {type(error).__name__}: {error}")


def _terms(value: str) -> set[str]:
    return set(re.findall(r"\w+", value.lower()))


def _score(content: str, terms: set[str]) -> int:
    if not terms:
        return 0
    return len(_terms(content) & terms)


@dataclass(slots=True)
class MemoryRepository:
    _conn: AsyncConnection
    _context: TenantContext
    _dialect: Dialect

    @property
    def connection(self) -> AsyncConnection:
        return self._conn

    async def save(self, entry: MemoryEntry) -> MemoryEntry:
        row = self._prepare_entry(entry)
        now_ms = self._dialect.db_now_ms()
        values = {
            "id": row.id,
            "tenant_id": self._context.tenant_id,
            "workspace_id": self._context.workspace_id,
            "session_id": row.session_id,
            "content": row.content,
            "type": row.type,
            "role": row.role,
            "turn_index": row.turn_index,
            "created_at": now_ms,
            "metadata_json": row.metadata_json(),
        }
        update_result = await self._full_scope_update(row.id, row.session_id, values)
        if update_result.rowcount == 0:
            savepoint = await self._conn.begin_nested()
            try:
                await self._conn.execute(insert(memory_entries).values(**values))
                await savepoint.commit()
            except IntegrityError as primary:
                await self._rollback_savepoint(savepoint, primary)
                retry_result = await self._full_scope_update(row.id, row.session_id, values)
                if retry_result.rowcount == 0:
                    raise primary
            except BaseException as primary:
                await self._rollback_savepoint(savepoint, primary)
                raise primary
        saved = await self._get_row(row.id, row.session_id)
        assert saved is not None
        return saved

    async def query(
        self,
        query: str,
        top_k: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        rows = await self._load_rows(
            entry_type=entry_type,
            limit=None,
            visible_scope="session_or_long_term",
        )
        terms = _terms(query)
        ranked = [
            (row, _score(row.content, terms), index)
            for index, row in enumerate(rows)
            if (entry_type != "chat_message" or row.session_id == self._context.session_id)
        ]
        ranked.sort(
            key=lambda item: (
                item[1],
                item[0].created_at,
                item[0].turn_index,
                -item[2],
            ),
            reverse=True,
        )
        return [row for row, score, _ in ranked if score > 0][:top_k]

    async def recent(
        self,
        limit: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        return await self._load_rows(
            entry_type=entry_type,
            limit=limit,
            visible_scope="session_only",
        )

    async def context(
        self,
        max_chars: int,
        limit: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]:
        selected: list[MemoryEntry] = []
        used = 0
        for entry in await self.recent(limit=limit, entry_type=entry_type):
            entry_len = len(entry.content)
            separator = 1 if selected else 0
            if used + separator + entry_len > max_chars:
                continue
            selected.append(entry)
            used += separator + entry_len
        return list(reversed(selected))

    async def forget(self, entry_id: str) -> None:
        await self._conn.execute(
            delete(memory_entries).where(
                memory_entries.c.tenant_id == self._context.tenant_id,
                memory_entries.c.workspace_id == self._context.workspace_id,
                memory_entries.c.id == entry_id,
                self._visibility_filter(include_long_term=True),
            )
        )

    async def _load_rows(
        self,
        *,
        entry_type: str | None,
        limit: int | None,
        visible_scope: str,
    ) -> list[MemoryEntry]:
        filters = [
            memory_entries.c.tenant_id == self._context.tenant_id,
            memory_entries.c.workspace_id == self._context.workspace_id,
        ]
        if entry_type is not None:
            filters.append(memory_entries.c.type == entry_type)
        else:
            filters.append(memory_entries.c.type != "tool_result")
        if entry_type == "chat_message" and self._context.session_id is None:
            raise ValueError("session_id is required for chat_message queries")
        filters.append(self._visibility_filter(include_long_term=visible_scope == "session_or_long_term"))

        query = (
            select(
                memory_entries.c.id,
                memory_entries.c.content,
                memory_entries.c.type,
                memory_entries.c.session_id,
                memory_entries.c.role,
                memory_entries.c.turn_index,
                memory_entries.c.created_at,
                memory_entries.c.metadata_json,
            )
            .where(*filters)
            .order_by(
                memory_entries.c.created_at.desc(),
                memory_entries.c.turn_index.desc(),
                memory_entries.c.id.desc(),
            )
        )
        if limit is not None:
            query = query.limit(limit)
        result = await self._conn.execute(query)
        return [MemoryEntry.from_row(row) for row in result.mappings().all()]

    async def _get_row(self, entry_id: str, target_session_id: str | None) -> MemoryEntry | None:
        result = await self._conn.execute(
            select(
                memory_entries.c.id,
                memory_entries.c.content,
                memory_entries.c.type,
                memory_entries.c.session_id,
                memory_entries.c.role,
                memory_entries.c.turn_index,
                memory_entries.c.created_at,
                memory_entries.c.metadata_json,
            )
            .where(
                memory_entries.c.tenant_id == self._context.tenant_id,
                memory_entries.c.workspace_id == self._context.workspace_id,
                memory_entries.c.id == entry_id,
                self._exact_scope_filter(target_session_id),
            )
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else MemoryEntry.from_row(row)

    async def get(self, entry_id: str, target_session_id: str | None = None) -> MemoryEntry | None:
        return await self._get_row(entry_id, target_session_id)

    def _prepare_entry(self, entry: MemoryEntry) -> MemoryEntry:
        if entry.type == "chat_message":
            if self._context.session_id is None:
                raise ValueError("session_id is required for chat_message entries")
            session_id = self._context.session_id if entry.session_id is None else entry.session_id
            if session_id != self._context.session_id:
                raise ValueError("session_id must match the current context")
            return entry.model_copy(update={"session_id": session_id})
        if entry.session_id is None:
            return entry
        if self._context.session_id is None or entry.session_id != self._context.session_id:
            raise ValueError("session_id must match the current context")
        return entry

    async def _full_scope_update(
        self,
        entry_id: str,
        target_session_id: str | None,
        values: dict[str, object],
    ):
        return await self._conn.execute(
            update(memory_entries)
            .where(
                memory_entries.c.tenant_id == self._context.tenant_id,
                memory_entries.c.workspace_id == self._context.workspace_id,
                memory_entries.c.id == entry_id,
                self._exact_scope_filter(target_session_id),
            )
            .values(**values)
        )

    async def _rollback_savepoint(self, savepoint, primary: BaseException) -> None:
        if not savepoint.is_active:
            return
        try:
            await savepoint.rollback()
        except BaseException as error:
            _note_cleanup_error(primary, "savepoint rollback", error)
            raise primary

    def _visibility_filter(self, *, include_long_term: bool):
        if self._context.session_id is None:
            return memory_entries.c.session_id.is_(None)
        if include_long_term:
            return or_(
                memory_entries.c.session_id == self._context.session_id,
                memory_entries.c.session_id.is_(None),
            )
        return memory_entries.c.session_id == self._context.session_id

    def _exact_scope_filter(self, target_session_id: str | None):
        if target_session_id is None:
            return memory_entries.c.session_id.is_(None)
        return memory_entries.c.session_id == target_session_id
