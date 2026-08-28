from abc import ABC, abstractmethod

from multiclaw.memory.models import MemoryEntry
from multiclaw.tenancy.context import TenantContext


class MemoryProtocol(ABC):
    @abstractmethod
    async def save(self, context: TenantContext, entry: MemoryEntry) -> MemoryEntry: ...

    @abstractmethod
    async def query(
        self,
        context: TenantContext,
        query: str,
        top_k: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]: ...

    @abstractmethod
    async def recent(
        self,
        context: TenantContext,
        limit: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]: ...

    @abstractmethod
    async def context(
        self,
        context: TenantContext,
        max_chars: int,
        limit: int,
        entry_type: str | None = None,
    ) -> list[MemoryEntry]: ...

    @abstractmethod
    async def forget(self, context: TenantContext, entry_id: str) -> None: ...
