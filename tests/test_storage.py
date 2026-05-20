import pytest
from pydantic import BaseModel

from multiclaw.storage.repository import Repository
from multiclaw.storage.sqlite import SqliteConfig, SqliteRepository


class StorageEntity(BaseModel):
    id: str = ""
    name: str
    value: int = 0


@pytest.fixture
async def repo():
    cfg = SqliteConfig(database_path=":memory:")
    repository = SqliteRepository[StorageEntity](
        entity_type=StorageEntity,
        table_name="test_entities",
        config=cfg,
    )
    await repository.initialize()
    return repository


class TestSqliteRepository:
    @pytest.mark.asyncio
    async def test_save_and_get(self, repo):
        entity = StorageEntity(name="item1", value=42)
        saved = await repo.save(entity)

        assert saved.id != ""
        retrieved = await repo.get(saved.id)
        assert retrieved is not None
        assert retrieved.name == "item1"
        assert retrieved.value == 42

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, repo):
        result = await repo.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_with_no_filters(self, repo):
        await repo.save(StorageEntity(name="a", value=1))
        await repo.save(StorageEntity(name="b", value=2))

        results = await repo.list({})
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_list_with_filters(self, repo):
        await repo.save(StorageEntity(name="alpha", value=10))
        await repo.save(StorageEntity(name="beta", value=10))
        await repo.save(StorageEntity(name="gamma", value=20))

        results = await repo.list({"value": 10})
        assert len(results) == 2
        assert all(result.value == 10 for result in results)

    @pytest.mark.asyncio
    async def test_delete(self, repo):
        saved = await repo.save(StorageEntity(name="to_delete", value=99))
        await repo.delete(saved.id)

        result = await repo.get(saved.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_does_not_raise(self, repo):
        await repo.delete("nonexistent")

    @pytest.mark.asyncio
    async def test_save_preserves_existing_id(self, repo):
        entity = StorageEntity(id="custom-id-123", name="custom", value=7)
        saved = await repo.save(entity)

        assert saved.id == "custom-id-123"
        retrieved = await repo.get("custom-id-123")
        assert retrieved is not None
        assert retrieved.name == "custom"

    @pytest.mark.asyncio
    async def test_save_updates_existing(self, repo):
        saved = await repo.save(StorageEntity(name="original", value=1))
        saved.value = 99
        updated = await repo.save(saved)

        assert updated.id == saved.id
        retrieved = await repo.get(saved.id)
        assert retrieved is not None
        assert retrieved.value == 99

    @pytest.mark.asyncio
    async def test_repository_protocol_is_abstract(self):
        with pytest.raises(TypeError):
            Repository[StorageEntity]()  # type: ignore[abstract]
