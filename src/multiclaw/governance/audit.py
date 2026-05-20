from multiclaw.governance.models import AuditLog


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
            user_id=user_id,
            tenant_id=tenant_id,
        )
        self._entries.append(entry)
        return entry

    async def list_entries(self) -> list[AuditLog]:
        return list(self._entries)
