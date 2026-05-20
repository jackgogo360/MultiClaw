from abc import ABC, abstractmethod
from typing import Generic, TypeVar

T = TypeVar("T")


class Repository(ABC, Generic[T]):
    @abstractmethod
    async def get(self, id: str) -> T | None: ...

    @abstractmethod
    async def save(self, entity: T) -> T: ...

    @abstractmethod
    async def delete(self, id: str) -> None: ...

    @abstractmethod
    async def list(self, filters: dict[str, object]) -> list[T]: ...
