import asyncio
import importlib
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.memory import MemoryEntry
from multiclaw.session import SessionStatus
from multiclaw.storage import Database
from multiclaw.storage.repositories import MemoryRepository
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'scoped-repositories.db'}"


async def _upgrade_database(database_url: str) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")


async def _seed_scope(database: Database, *, slug: str) -> TenantContext:
    tenant_id = str(uuid4())
    workspace_id = str(uuid4())

    async with database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users (
                    id, email, auth_epoch, default_workspace_id, status,
                    purge_after, created_at, updated_at, disabled_at, purge_requested_at
                )
                VALUES (
                    :tenant_id,
                    :email,
                    0,
                    NULL,
                    'active',
                    NULL,
                    1,
                    1,
                    NULL,
                    NULL
                )
                """
            ),
            {"tenant_id": tenant_id, "email": f"{slug}@example.com"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                VALUES (:workspace_id, :tenant_id, :slug, :name, 'active', 1, 1)
                """
            ),
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "slug": slug,
                "name": slug.title(),
            },
        )
        await conn.execute(
            text(
                """
                UPDATE users
                SET default_workspace_id = :workspace_id
                WHERE id = :tenant_id
                """
            ),
            {"tenant_id": tenant_id, "workspace_id": workspace_id},
        )

    return TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)


async def _seed_workspace(
    database: Database,
    *,
    tenant_id: str,
    slug: str,
) -> TenantContext:
    workspace_id = str(uuid4())

    async with database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                VALUES (:workspace_id, :tenant_id, :slug, :name, 'active', 1, 1)
                """
            ),
            {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "slug": slug,
                "name": slug.title(),
            },
        )

    return TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)


@pytest.fixture
async def scoped_database(tmp_path: Path):
    database_url = _sqlite_url(tmp_path)
    await _upgrade_database(database_url)
    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def scoped_contexts(scoped_database: Database) -> dict[str, TenantContext]:
    primary = await _seed_scope(scoped_database, slug="alpha")
    return {
        "primary": primary,
        "sibling": await _seed_workspace(scoped_database, tenant_id=primary.tenant_id, slug="alpha-lab"),
        "secondary": await _seed_scope(scoped_database, slug="beta"),
    }


@pytest.mark.asyncio
async def test_tenant_uow_binds_scoped_session_and_memory_repositories(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    context = scoped_contexts["primary"]

    async with TenantUnitOfWork(scoped_database, context) as uow:
        assert uow.sessions.connection is uow.conn
        assert uow.memory.connection is uow.conn
        assert uow.users.connection is uow.conn
        assert uow.workspaces.connection is uow.conn


@pytest.mark.asyncio
async def test_scoped_session_crud_and_cross_scope_isolation(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    primary = scoped_contexts["primary"]
    sibling = scoped_contexts["sibling"]
    secondary = scoped_contexts["secondary"]

    async with TenantUnitOfWork(scoped_database, primary) as uow:
        created = await uow.sessions.create("  Alpha chat  ")
        renamed = await uow.sessions.rename(created.id, "  Research notes  ")
        archived = await uow.sessions.archive(created.id)
        restored = await uow.sessions.restore(created.id)
        default_session = await uow.sessions.create()
        touched = await uow.sessions.touch_message(
            default_session.id,
            "  first message becomes the title and keeps only forty chars  ",
        )

    for foreign_context in (sibling, secondary):
        async with TenantUnitOfWork(scoped_database, foreign_context) as foreign:
            assert await foreign.sessions.get(created.id) is None
            assert await foreign.sessions.list() == []
            assert await foreign.sessions.rename(created.id, "Foreign") is None
            assert await foreign.sessions.archive(created.id) is None
            assert await foreign.sessions.restore(created.id) is None
            assert await foreign.sessions.touch_message(created.id, "Foreign") is None
            await foreign.sessions.delete(created.id)

    async with TenantUnitOfWork(scoped_database, primary) as verify:
        active_list = await verify.sessions.list()
        listed = await verify.sessions.list(include_archived=True)
        fetched = await verify.sessions.get(created.id)
        default_fetched = await verify.sessions.get(default_session.id)

    assert created.title == "Alpha chat"
    assert renamed is not None and renamed.title == "Research notes"
    assert archived is not None and archived.status is SessionStatus.ARCHIVED
    assert restored is not None and restored.status is SessionStatus.ACTIVE
    assert touched is not None and touched.title == "first message becomes the title and keep"
    assert fetched is not None and fetched.id == created.id
    assert default_fetched is not None and default_fetched.last_message_at is not None
    assert [session.id for session in active_list] == [default_session.id, created.id]
    assert [session.id for session in listed] == [default_session.id, created.id]


@pytest.mark.asyncio
async def test_scoped_session_delete_messages_limit_and_order(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    context = scoped_contexts["primary"]
    other_workspace = scoped_contexts["sibling"]

    async with TenantUnitOfWork(scoped_database, context) as uow:
        created = await uow.sessions.create()
        session_memory = MemoryRepository(uow.conn, context.for_session(created.id), scoped_database.dialect)
        other_workspace_memory = MemoryRepository(
            uow.conn,
            other_workspace.for_session(str(uuid4())),
            scoped_database.dialect,
        )
        await session_memory.save(MemoryEntry(content="Hello", type="chat_message", role="user", turn_index=1))
        await session_memory.save(MemoryEntry(content="Hi", type="chat_message", role="assistant", turn_index=2))
        await session_memory.save(MemoryEntry(content="tool output", type="chat_message", role="tool", turn_index=3))
        await session_memory.save(MemoryEntry(content="Question", type="chat_message", role="user", turn_index=4))
        await session_memory.save(
            MemoryEntry(content="Answer", type="chat_message", role="assistant", turn_index=5)
        )
        await other_workspace_memory.save(MemoryEntry(content="Foreign", type="note"))
        limited = await uow.sessions.get_messages(created.id, limit=3)

    assert [message["role"] for message in limited] == ["assistant", "user", "assistant"]
    assert [message["content"] for message in limited] == ["Hi", "Question", "Answer"]

    async with TenantUnitOfWork(scoped_database, context) as cleanup:
        root_memory = cleanup.memory
        await root_memory.save(MemoryEntry(content="survives delete", type="note"))
        await cleanup.sessions.delete(created.id)
        assert await cleanup.sessions.get(created.id) is None
        assert await cleanup.sessions.get_messages(created.id) == []
        assert [entry.content for entry in await root_memory.query("survives", top_k=5)] == [
            "survives delete"
        ]


@pytest.mark.asyncio
async def test_scoped_memory_save_query_recent_context_and_forget(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    context = scoped_contexts["primary"]
    sibling = scoped_contexts["sibling"]
    secondary = scoped_contexts["secondary"]

    async with TenantUnitOfWork(scoped_database, context) as uow:
        session = await uow.sessions.create("Alpha")
        session_memory = MemoryRepository(uow.conn, context.for_session(session.id), scoped_database.dialect)
        sibling_memory = MemoryRepository(uow.conn, sibling, scoped_database.dialect)
        secondary_memory = MemoryRepository(uow.conn, secondary, scoped_database.dialect)

        long_term = await uow.memory.save(MemoryEntry(content="workspace memory alpha", type="note"))
        await session_memory.save(
            MemoryEntry(content="workspace note alpha", type="note", session_id=session.id)
        )
        await session_memory.save(
            MemoryEntry(content="older chat", type="chat_message", role="user", turn_index=1)
        )
        await session_memory.save(
            MemoryEntry(content="latest chat", type="chat_message", role="assistant", turn_index=2)
        )
        await sibling_memory.save(MemoryEntry(content="workspace memory sibling", type="note"))
        await secondary_memory.save(MemoryEntry(content="workspace memory beta", type="note"))

        query_results = await session_memory.query("memory alpha", top_k=5)
        recent_results = await session_memory.recent(limit=3, entry_type="chat_message")
        context_results = await session_memory.context(max_chars=12, limit=3)
        await session_memory.forget(long_term.id)

    assert [entry.content for entry in query_results] == [
        "workspace memory alpha",
        "workspace note alpha",
    ]
    assert [entry.content for entry in recent_results] == ["latest chat", "older chat"]
    assert [entry.content for entry in context_results] == ["latest chat"]

    async with TenantUnitOfWork(scoped_database, context) as verify:
        assert await verify.memory.query("alpha", top_k=5) == []


@pytest.mark.asyncio
async def test_non_chat_session_scoped_memory_stays_session_local_and_deletes_with_session(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    context = scoped_contexts["primary"]

    async with TenantUnitOfWork(scoped_database, context) as uow:
        session_a = await uow.sessions.create("Alpha")
        session_b = await uow.sessions.create("Beta")
        repo_a = MemoryRepository(uow.conn, context.for_session(session_a.id), scoped_database.dialect)
        repo_b = MemoryRepository(uow.conn, context.for_session(session_b.id), scoped_database.dialect)

        await repo_a.save(
            MemoryEntry(content="session note alpha", type="note", session_id=session_a.id)
        )
        await uow.memory.save(MemoryEntry(content="workspace longterm alpha", type="note"))

        assert [entry.content for entry in await repo_a.query("alpha", top_k=5)] == [
            "workspace longterm alpha",
            "session note alpha",
        ]
        assert [entry.content for entry in await repo_b.query("alpha", top_k=5)] == [
            "workspace longterm alpha"
        ]
        assert [entry.content for entry in await repo_a.recent(limit=5)] == ["session note alpha"]
        assert await repo_b.recent(limit=5) == []

        await uow.sessions.delete(session_a.id)
        assert [entry.content for entry in await repo_a.query("alpha", top_k=5)] == [
            "workspace longterm alpha"
        ]
        assert [entry.content for entry in await repo_b.query("alpha", top_k=5)] == [
            "workspace longterm alpha"
        ]


@pytest.mark.asyncio
async def test_longterm_memory_is_shared_within_workspace_but_not_across_workspaces(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    primary = scoped_contexts["primary"]
    sibling = scoped_contexts["sibling"]
    secondary = scoped_contexts["secondary"]

    async with TenantUnitOfWork(scoped_database, primary) as uow:
        session = await uow.sessions.create("Alpha")
        await uow.memory.save(MemoryEntry(content="workspace longterm alpha", type="note"))
        assert [entry.content for entry in await MemoryRepository(
            uow.conn,
            primary.for_session(session.id),
            scoped_database.dialect,
        ).query("alpha", top_k=5)] == ["workspace longterm alpha"]

    async with TenantUnitOfWork(scoped_database, sibling) as uow:
        assert await uow.memory.query("alpha", top_k=5) == []

    async with TenantUnitOfWork(scoped_database, secondary) as uow:
        assert await uow.memory.query("alpha", top_k=5) == []


@pytest.mark.asyncio
async def test_memory_save_rejects_foreign_or_missing_session_scope(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    context = scoped_contexts["primary"]
    sibling = scoped_contexts["sibling"]
    secondary = scoped_contexts["secondary"]

    async with TenantUnitOfWork(scoped_database, context) as uow:
        session = await uow.sessions.create("Alpha")
        other_session = await uow.sessions.create("Beta")
        session_memory = MemoryRepository(uow.conn, context.for_session(session.id), scoped_database.dialect)

        with pytest.raises(ValueError, match="session_id"):
            await uow.memory.save(
                MemoryEntry(content="hello", type="chat_message", role="user", turn_index=1)
            )

        with pytest.raises(ValueError, match="session_id"):
            await session_memory.save(
                MemoryEntry(
                    content="foreign chat",
                    type="chat_message",
                    role="user",
                    turn_index=1,
                    session_id=other_session.id,
                )
            )

        with pytest.raises(ValueError, match="session_id"):
            await uow.memory.save(
                MemoryEntry(content="foreign session note", type="note", session_id=session.id)
            )

        with pytest.raises(ValueError, match="session_id"):
            await session_memory.save(
                MemoryEntry(content="wrong session note", type="note", session_id=other_session.id)
            )

        with pytest.raises(IntegrityError):
            await MemoryRepository(
                uow.conn,
                sibling.for_session(session.id),
                scoped_database.dialect,
            ).save(
                MemoryEntry(
                    content="cross workspace",
                    type="chat_message",
                    role="user",
                    turn_index=1,
                    session_id=session.id,
                )
            )

        with pytest.raises(IntegrityError):
            await MemoryRepository(
                uow.conn,
                secondary.for_session(session.id),
                scoped_database.dialect,
            ).save(
                MemoryEntry(
                    content="cross tenant",
                    type="chat_message",
                    role="user",
                    turn_index=1,
                    session_id=session.id,
                )
            )


@pytest.mark.asyncio
async def test_scoped_repositories_roll_back_within_uow(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    context = scoped_contexts["primary"]

    with pytest.raises(RuntimeError, match="boom"):
        async with TenantUnitOfWork(scoped_database, context) as uow:
            session = await uow.sessions.create("Alpha")
            session_memory = MemoryRepository(uow.conn, context.for_session(session.id), scoped_database.dialect)
            await session_memory.save(
                MemoryEntry(content="persist me", type="chat_message", role="user", turn_index=1)
            )
            raise RuntimeError("boom")

    async with TenantUnitOfWork(scoped_database, context) as verify:
        assert await verify.sessions.list(include_archived=True) == []
        assert await verify.memory.query("persist", top_k=5) == []


@pytest.mark.asyncio
async def test_memory_duplicate_save_updates_same_scope_and_rejects_foreign_scope_without_poisoning_tx(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    primary = scoped_contexts["primary"]
    sibling = scoped_contexts["sibling"]

    entry_id = str(uuid4())

    async with TenantUnitOfWork(scoped_database, primary) as uow:
        original = await uow.memory.save(
            MemoryEntry(id=entry_id, content="first value", type="note")
        )
        updated = await uow.memory.save(
            MemoryEntry(id=entry_id, content="second value", type="note")
        )
        assert original.id == updated.id == entry_id
        assert [entry.content for entry in await uow.memory.query("value", top_k=5)] == [
            "second value"
        ]

    async with TenantUnitOfWork(scoped_database, primary) as verify:
        foreign_repo = MemoryRepository(verify.conn, sibling, scoped_database.dialect)

        with pytest.raises(IntegrityError):
            await foreign_repo.save(
                MemoryEntry(id=entry_id, content="foreign overwrite", type="note")
            )

        still_there = await verify.memory.query("value", top_k=5)
        after_error = await verify.memory.save(MemoryEntry(content="tx still usable", type="note"))

    assert [entry.content for entry in still_there] == ["second value"]
    assert after_error.content == "tx still usable"


@pytest.mark.asyncio
async def test_memory_duplicate_save_latest_value_wins_across_uow_retries(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    context = scoped_contexts["primary"]
    entry_id = str(uuid4())

    async with TenantUnitOfWork(scoped_database, context) as first:
        await first.memory.save(MemoryEntry(id=entry_id, content="initial", type="note"))

    async with TenantUnitOfWork(scoped_database, context) as second:
        latest = await second.memory.save(MemoryEntry(id=entry_id, content="latest", type="note"))

    async with TenantUnitOfWork(scoped_database, context) as verify:
        matches = await verify.memory.query("latest", top_k=5)
        row_count = await verify.conn.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM memory_entries
                WHERE tenant_id = :tenant_id AND workspace_id = :workspace_id AND id = :entry_id
                """
            ),
            {
                "tenant_id": context.tenant_id,
                "workspace_id": context.workspace_id,
                "entry_id": entry_id,
            },
        )

    assert latest.content == "latest"
    assert row_count == 1
    assert [entry.content for entry in matches] == ["latest"]


@pytest.mark.asyncio
async def test_session_and_memory_ordering_is_deterministic_when_timestamps_tie(
    scoped_database: Database,
    scoped_contexts: dict[str, TenantContext],
) -> None:
    context = scoped_contexts["primary"]

    async with TenantUnitOfWork(scoped_database, context) as uow:
        session_a = await uow.sessions.create("Alpha")
        session_b = await uow.sessions.create("Beta")
        await uow.conn.execute(
            text(
                """
                UPDATE chat_sessions
                SET created_at = 1000, updated_at = 1000, last_message_at = 1000
                WHERE id IN (:a, :b)
                """
            ).bindparams(a=session_a.id, b=session_b.id)
        )

        repo = MemoryRepository(uow.conn, context.for_session(session_a.id), scoped_database.dialect)
        message_a = await repo.save(
            MemoryEntry(content="match", type="chat_message", role="user", turn_index=1)
        )
        message_b = await repo.save(
            MemoryEntry(content="match", type="chat_message", role="assistant", turn_index=1)
        )
        note_a = await repo.save(
            MemoryEntry(content="match", type="note", session_id=session_a.id)
        )
        note_b = await repo.save(
            MemoryEntry(content="match", type="note", session_id=session_a.id)
        )

        await uow.conn.execute(
            text(
                """
                UPDATE memory_entries
                SET created_at = 2000, turn_index = 1
                WHERE id IN (:message_a, :message_b, :note_a, :note_b)
                """
            ).bindparams(
                message_a=message_a.id,
                message_b=message_b.id,
                note_a=note_a.id,
                note_b=note_b.id,
            )
        )

        listed_once = await uow.sessions.list(include_archived=True)
        listed_twice = await uow.sessions.list(include_archived=True)
        messages_once = await uow.sessions.get_messages(session_a.id, limit=10)
        messages_twice = await uow.sessions.get_messages(session_a.id, limit=10)
        query_once = await repo.query("match", top_k=10)
        query_twice = await repo.query("match", top_k=10)

    assert [session.id for session in listed_once] == [session.id for session in listed_twice]
    assert [message["content"] for message in messages_once] == [message["content"] for message in messages_twice]
    assert [message["role"] for message in messages_once] == [message["role"] for message in messages_twice]
    assert [entry.id for entry in query_once] == [entry.id for entry in query_twice]


def test_scoped_packages_remove_legacy_exports_and_modules() -> None:
    import multiclaw.memory as memory_package
    import multiclaw.session as session_package
    from multiclaw.storage import repositories

    assert hasattr(repositories, "SessionRepository")
    assert hasattr(repositories, "MemoryRepository")
    assert not hasattr(session_package, "SqliteSessionStore")
    assert not hasattr(memory_package, "SqliteMemory")
    assert not hasattr(memory_package, "InMemoryMemory")
    assert importlib.util.find_spec("multiclaw.session.sqlite") is None
    assert importlib.util.find_spec("multiclaw.memory.sqlite") is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("multiclaw.session.sqlite")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("multiclaw.memory.sqlite")
