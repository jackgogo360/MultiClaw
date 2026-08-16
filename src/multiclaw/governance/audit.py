from __future__ import annotations

import json

from multiclaw.governance.models import AuditLog
from multiclaw.security.redaction import redact


class InMemoryAuditLogger:
    def __init__(self):
        self._entries: list[AuditLog] = []

    async def record(
        self,
        workflow_repository=None,
        context=None,
        *,
        event_type: str = "tool.event",
        tool_name: str | None = None,
        status: str,
        detail: str = "",
        user_id: str = "",
        tenant_id: str = "",
        approval_id: str | None = None,
        execution_id: str | None = None,
    ) -> AuditLog:
        del workflow_repository, context, approval_id, execution_id
        entry = AuditLog(
            tool_name=tool_name or "",
            status=status,
            detail=detail,
            event_type=event_type,
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
        approval_id: str | None = None,
        execution_id: str | None = None,
    ):
        if workflow_repository is None or context is None:
            raise ValueError("workflow_repository and context are required")

        redacted_detail = (
            json.dumps(redact(detail), ensure_ascii=False, sort_keys=True)
            if not isinstance(detail, str)
            else str(redact(detail))
        )

        await workflow_repository.insert_audit_log(
            context,
            event_type=event_type,
            status=status,
            tool_name=tool_name,
            detail_redacted=redacted_detail,
            approval_id=approval_id,
            execution_id=execution_id,
        )
        return AuditLog(
            tool_name=tool_name or "",
            status=status,
            detail=redacted_detail,
            event_type=event_type,
            user_id=user_id,
            tenant_id=getattr(context, "tenant_id", tenant_id),
        )
