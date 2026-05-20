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
