import re

from multiclaw.memory.models import MemoryEntry
from multiclaw.memory.protocol import MemoryProtocol


class InMemoryMemory(MemoryProtocol):
    def __init__(self) -> None:
        self._entries: dict[str, MemoryEntry] = {}
        self._order: dict[str, int] = {}
        self._next_order = 0

    async def save(self, entry: MemoryEntry) -> MemoryEntry:
        self._entries[entry.id] = entry
        if entry.id not in self._order:
            self._order[entry.id] = self._next_order
            self._next_order += 1
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
        matches = [
            (entry, _score(entry.content, terms))
            for entry in self._filter(
                entry_type=entry_type,
                tenant_id=tenant_id,
                session_id=session_id,
            )
        ]
        ranked = [
            entry
            for entry, score in sorted(
                matches,
                key=lambda item: (item[1], self._order[item[0].id]),
                reverse=True,
            )
            if score > 0
        ]
        return ranked[:top_k]

    async def recent(
        self,
        limit: int,
        entry_type: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryEntry]:
        entries = sorted(
            self._filter(entry_type=entry_type, tenant_id=tenant_id, session_id=session_id),
            key=lambda entry: self._order[entry.id],
            reverse=True,
        )
        return entries[:limit]

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
        for entry in await self.recent(limit=limit, entry_type=entry_type, tenant_id=tenant_id, session_id=session_id):
            entry_len = len(entry.content)
            separator = 1 if selected else 0
            if used + separator + entry_len > max_chars:
                continue
            selected.append(entry)
            used += separator + entry_len
        return list(reversed(selected))

    async def forget(self, entry_id: str) -> None:
        self._entries.pop(entry_id, None)
        self._order.pop(entry_id, None)

    def _filter(
        self,
        entry_type: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryEntry]:
        entries = list(self._entries.values())
        if entry_type is not None:
            entries = [entry for entry in entries if entry.type == entry_type]
        if tenant_id is not None:
            entries = [entry for entry in entries if entry.tenant_id == tenant_id]
        if session_id is not None:
            entries = [entry for entry in entries if entry.session_id == session_id]
        return entries


def _terms(value: str) -> set[str]:
    return set(re.findall(r"\w+", value.lower()))


def _score(content: str, terms: set[str]) -> int:
    if not terms:
        return 0
    content_terms = _terms(content)
    return len(terms & content_terms)
