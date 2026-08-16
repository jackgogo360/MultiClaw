from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import insert

from multiclaw.governance.models import AuditLog
from multiclaw.security.redaction import redact
from multiclaw.storage.schema import audit_logs


class InMemoryAuditLogger:
    def __init__(self):
        self._entries: list[AuditLog] = []

    async def record(
        self,
        *,
        tool_name: str,
        status: str,
        detail: str,
        user_id: str = "",
        tenant_id: str = "",
    ) -> AuditLog:
        entry = AuditLog(
            tool_name=tool_name,
            status=status,
            detail=detail,
            event_type="tool.event",
            user_id=user_id,
            tenant_id=tenant_id,
        )
        self._entries.append(entry)
        return entry

    async def list_entries(self) -> list[AuditLog]:
        return list(self._entries)


class ScopedAuditLogger:
    async def record(
        self,
        workflow_repository=None,
        context=None,
        *,
        event_type: str = "tool.event",
        status: str,
        tool_name: str | None = None,
        detail="",
        user_id: str = "",
        tenant_id: str = "",
    ):
        if workflow_repository is None or context is None:
            return AuditLog(
                tool_name=tool_name or "",
                status=status,
                detail=json.dumps(redact(detail), ensure_ascii=False, sort_keys=True)
                if not isinstance(detail, str)
                else str(redact(detail)),
                event_type=event_type,
                user_id=user_id,
                tenant_id=tenant_id or getattr(context, "tenant_id", ""),
            )

        await workflow_repository.insert_audit_log(
            context,
            event_type=event_type,
            status=status,
            tool_name=tool_name,
            detail_redacted=json.dumps(redact(detail), ensure_ascii=False, sort_keys=True)
            if not isinstance(detail, str)
            else str(redact(detail)),
        )
        return AuditLog(
            tool_name=tool_name or "",
            status=status,
            detail=json.dumps(redact(detail), ensure_ascii=False, sort_keys=True)
            if not isinstance(detail, str)
            else str(redact(detail)),
            event_type=event_type,
            user_id=user_id,
            tenant_id=getattr(context, "tenant_id", tenant_id),
        )
