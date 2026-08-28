from __future__ import annotations
from dataclasses import dataclass

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from multiclaw.session.models import ChatSession, InvalidSessionTitleError, SessionStatus
from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect
from multiclaw.storage.schema import (
    approval_requests,
    chat_sessions,
    memory_entries,
    tool_executions,
)
from multiclaw.tenancy.context import TenantContext


Dialect = SQLiteDialect | MySQLDialect


def _validate_title(title: str) -> str:
    try:
        return ChatSession(
            tenant_id="scope",
            workspace_id="scope",
            title=title,
        ).title
    except ValueError as exc:
        raise InvalidSessionTitleError(str(exc)) from exc


@dataclass(slots=True)
class SessionRepository:
    _conn: AsyncConnection
    _context: TenantContext
    _dialect: Dialect

    @property
    def connection(self) -> AsyncConnection:
        return self._conn

    async def create(self, title: str = "New Chat") -> ChatSession:
        session = ChatSession(
            tenant_id=self._context.tenant_id,
            workspace_id=self._context.workspace_id,
            title=_validate_title(title),
        )
        now_ms = self._dialect.db_now_ms()
        await self._conn.execute(
            insert(chat_sessions).values(
                id=session.id,
                tenant_id=session.tenant_id,
                workspace_id=session.workspace_id,
                title=session.title,
                status=SessionStatus.ACTIVE.value,
                created_at=now_ms,
                updated_at=now_ms,
                last_message_at=None,
                metadata_json=session.metadata_json(),
            )
        )
        created = await self.get(session.id)
        assert created is not None
        return created

    async def get(self, session_id: str) -> ChatSession | None:
        result = await self._conn.execute(
            select(
                chat_sessions.c.id,
                chat_sessions.c.tenant_id,
                chat_sessions.c.workspace_id,
                chat_sessions.c.title,
                chat_sessions.c.status,
                chat_sessions.c.created_at,
                chat_sessions.c.updated_at,
                chat_sessions.c.last_message_at,
                chat_sessions.c.metadata_json,
            )
            .where(
                chat_sessions.c.tenant_id == self._context.tenant_id,
                chat_sessions.c.workspace_id == self._context.workspace_id,
                chat_sessions.c.id == session_id,
            )
            .limit(1)
        )
        row = result.mappings().first()
        return None if row is None else ChatSession.from_row(row)

    async def list(self, include_archived: bool = False) -> list[ChatSession]:
        query = (
            select(
                chat_sessions.c.id,
                chat_sessions.c.tenant_id,
                chat_sessions.c.workspace_id,
                chat_sessions.c.title,
                chat_sessions.c.status,
                chat_sessions.c.created_at,
                chat_sessions.c.updated_at,
                chat_sessions.c.last_message_at,
                chat_sessions.c.metadata_json,
            )
            .where(
                chat_sessions.c.tenant_id == self._context.tenant_id,
                chat_sessions.c.workspace_id == self._context.workspace_id,
            )
            .order_by(
                func.coalesce(chat_sessions.c.last_message_at, chat_sessions.c.created_at).desc(),
                chat_sessions.c.id.asc(),
            )
        )
        if not include_archived:
            query = query.where(chat_sessions.c.status == SessionStatus.ACTIVE.value)
        result = await self._conn.execute(query)
        return [ChatSession.from_row(row) for row in result.mappings().all()]

    async def rename(self, session_id: str, title: str) -> ChatSession | None:
        normalized = _validate_title(title)
        result = await self._conn.execute(
            update(chat_sessions)
            .where(
                chat_sessions.c.tenant_id == self._context.tenant_id,
                chat_sessions.c.workspace_id == self._context.workspace_id,
                chat_sessions.c.id == session_id,
            )
            .values(title=normalized, updated_at=self._dialect.db_now_ms())
        )
        if result.rowcount == 0:
            return None
        return await self.get(session_id)

    async def archive(self, session_id: str) -> ChatSession | None:
        return await self._set_status(session_id, SessionStatus.ARCHIVED)

    async def restore(self, session_id: str) -> ChatSession | None:
        return await self._set_status(session_id, SessionStatus.ACTIVE)

    async def delete(self, session_id: str) -> None:
        await self._conn.execute(
            delete(memory_entries).where(
                memory_entries.c.tenant_id == self._context.tenant_id,
                memory_entries.c.workspace_id == self._context.workspace_id,
                memory_entries.c.session_id == session_id,
            )
        )
        await self._conn.execute(
            delete(chat_sessions).where(
                chat_sessions.c.tenant_id == self._context.tenant_id,
                chat_sessions.c.workspace_id == self._context.workspace_id,
                chat_sessions.c.id == session_id,
            )
        )

    async def get_messages(self, session_id: str, limit: int = 50) -> list[dict[str, object]]:
        result = await self._conn.execute(
            select(
                memory_entries.c.role,
                memory_entries.c.content,
                memory_entries.c.created_at,
                memory_entries.c.turn_index,
            )
            .where(
                memory_entries.c.tenant_id == self._context.tenant_id,
                memory_entries.c.workspace_id == self._context.workspace_id,
                memory_entries.c.session_id == session_id,
                memory_entries.c.type == "chat_message",
                memory_entries.c.role.in_(("user", "assistant")),
            )
            .order_by(
                memory_entries.c.created_at.desc(),
                memory_entries.c.turn_index.desc(),
                memory_entries.c.id.desc(),
            )
            .limit(limit)
        )
        rows = list(result.mappings().all())
        rows.reverse()
        return [
            {
                "role": str(row["role"]),
                "content": str(row["content"]),
                "created_at": int(row["created_at"]),
            }
            for row in rows
        ]

    async def list_pending_approvals(self, session_id: str) -> list[dict[str, object]]:
        result = await self._conn.execute(
            select(
                approval_requests.c.approval_id,
                approval_requests.c.approval_status,
                approval_requests.c.version,
                approval_requests.c.expires_at,
                approval_requests.c.resolved_at,
                approval_requests.c.tool_call_id,
                tool_executions.c.tool_name,
                tool_executions.c.input_payload_json,
            )
            .select_from(
                approval_requests.join(
                    tool_executions,
                    and_(
                        tool_executions.c.tenant_id == approval_requests.c.tenant_id,
                        tool_executions.c.workspace_id == approval_requests.c.workspace_id,
                        tool_executions.c.session_id == approval_requests.c.session_id,
                        tool_executions.c.run_id == approval_requests.c.run_id,
                        tool_executions.c.approval_id == approval_requests.c.approval_id,
                        tool_executions.c.tool_call_id == approval_requests.c.tool_call_id,
                    ),
                )
            )
            .where(
                approval_requests.c.tenant_id == self._context.tenant_id,
                approval_requests.c.workspace_id == self._context.workspace_id,
                approval_requests.c.session_id == session_id,
                approval_requests.c.approval_status == "awaiting_user",
            )
            .order_by(
                approval_requests.c.requested_at.asc(),
                approval_requests.c.approval_id.asc(),
            )
        )
        return [dict(row) for row in result.mappings().all()]

    async def touch_message(self, session_id: str, content: str) -> ChatSession | None:
        current = await self.get(session_id)
        if current is None:
            return None
        next_title = current.title
        if current.title == "New Chat":
            next_title = content.strip()[:40] or "New Chat"
        now_ms = self._dialect.db_now_ms()
        result = await self._conn.execute(
            update(chat_sessions)
            .where(
                chat_sessions.c.tenant_id == self._context.tenant_id,
                chat_sessions.c.workspace_id == self._context.workspace_id,
                chat_sessions.c.id == session_id,
            )
            .values(
                title=next_title,
                updated_at=now_ms,
                last_message_at=now_ms,
            )
        )
        if result.rowcount == 0:
            return None
        return await self.get(session_id)

    async def _set_status(self, session_id: str, status: SessionStatus) -> ChatSession | None:
        result = await self._conn.execute(
            update(chat_sessions)
            .where(
                chat_sessions.c.tenant_id == self._context.tenant_id,
                chat_sessions.c.workspace_id == self._context.workspace_id,
                chat_sessions.c.id == session_id,
            )
            .values(status=status.value, updated_at=self._dialect.db_now_ms())
        )
        if result.rowcount == 0:
            return None
        return await self.get(session_id)
