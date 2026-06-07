import aiosqlite
import uuid
from typing import TypeVar

from pydantic import BaseModel

from multiclaw.storage.repository import Repository
from multiclaw.sqlite_utils import configure_sqlite_connection

T = TypeVar("T", bound=BaseModel)


class SqliteConfig(BaseModel):
    database_path: str = "data/multiclaw.db"


class SqliteRepository(Repository[T]):
    def __init__(
        self,
        entity_type: type[T],
        table_name: str,
        config: SqliteConfig,
    ) -> None:
        self._entity_type = entity_type
        self._table_name = table_name
        self._config = config
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self._config.database_path)
        self._db.row_factory = aiosqlite.Row
        await configure_sqlite_connection(self._db)
        await self._db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table_name} (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
            """
        )
        await self._db.commit()

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.initialize()
        assert self._db is not None
        return self._db

    async def get(self, id: str) -> T | None:
        db = await self._ensure_db()
        cursor = await db.execute(
            f"SELECT data FROM {self._table_name} WHERE id = ?",
            (id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._entity_type.model_validate_json(row[0])

    async def save(self, entity: T) -> T:
        db = await self._ensure_db()
        if not getattr(entity, "id", ""):
            entity.id = uuid.uuid4().hex
        data = entity.model_dump_json()
        await db.execute(
            f"INSERT OR REPLACE INTO {self._table_name} (id, data) VALUES (?, ?)",
            (entity.id, data),
        )
        await db.commit()
        return entity

    async def delete(self, id: str) -> None:
        db = await self._ensure_db()
        await db.execute(
            f"DELETE FROM {self._table_name} WHERE id = ?",
            (id,),
        )
        await db.commit()

    async def list(self, filters: dict[str, object]) -> list[T]:
        db = await self._ensure_db()
        cursor = await db.execute(f"SELECT data FROM {self._table_name}")
        rows = await cursor.fetchall()
        results = [self._entity_type.model_validate_json(row[0]) for row in rows]
        if filters:
            results = [
                result
                for result in results
                if all(getattr(result, key, None) == value for key, value in filters.items())
            ]
        return results
