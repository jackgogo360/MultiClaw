import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text

from multiclaw.auth.models import UserRecord
from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.repositories.auth import BootstrapProbeError
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext


_ORIGINAL_TEST_MYSQL_URL = os.getenv("MULTICLAW_TEST_MYSQL_URL")


def _sqlite_url(tmp_path: Path, name: str) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / name}"


async def _upgrade_database(database_url: str) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")


async def _create_database(*, driver: str, database_url: str) -> Database:
    if driver == "sqlite":
        await _upgrade_database(database_url)

    return Database.create(DatabaseSettings(driver=driver, url=database_url))


async def _create_mysql_database() -> Database:
    database_url = _ORIGINAL_TEST_MYSQL_URL or os.getenv("MULTICLAW_TEST_MYSQL_URL")
    if not database_url:
        pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")

    await _upgrade_database(database_url)
    return Database.create(DatabaseSettings(driver="mysql", url=database_url))


@pytest.fixture
async def migrated_sqlite_database(tmp_path: Path):
    database = await _create_database(driver="sqlite", database_url=_sqlite_url(tmp_path, "tenant-uow.db"))
    try:
        yield database
    finally:
        await database.dispose()


async def _insert_seed_scope(database: Database) -> TenantContext:
    tenant_id = "tenant-00000000-0000-0000-0000-000000000001"
    workspace_id = "workspace-0000-0000-0000-000000000001"

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
            {"tenant_id": tenant_id, "email": "tenant@example.com"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                VALUES (:workspace_id, :tenant_id, 'default', 'Default', 'active', 1, 1)
                """
            ),
            {"tenant_id": tenant_id, "workspace_id": workspace_id},
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


@pytest.mark.asyncio
async def test_tenant_uow_uses_one_connection_and_active_transaction(migrated_sqlite_database: Database) -> None:
    context = await _insert_seed_scope(migrated_sqlite_database)

    async with TenantUnitOfWork(migrated_sqlite_database, context) as uow:
        assert uow.conn is not None
        assert uow.conn.in_transaction() is True
        assert uow.users.connection is uow.conn
        assert uow.users.connection is uow.users._conn
        assert uow.workspaces.connection is uow.conn
        assert uow.workspaces.connection is uow.workspaces._conn
        assert uow.users.connection is uow.workspaces.connection

        current_user = await uow.users.get_current()
        current_workspace = await uow.workspaces.get_current()

        assert current_user.id == context.tenant_id
        assert current_user.default_workspace_id == context.workspace_id
        assert current_workspace.id == context.workspace_id
        assert current_workspace.tenant_id == context.tenant_id

        for repo in (uow.users, uow.workspaces):
            assert not hasattr(repo, "commit")
            assert not hasattr(repo, "rollback")
            assert not hasattr(repo, "engine")
            assert not hasattr(repo, "rebind")


@pytest.mark.asyncio
async def test_auth_uow_exposes_only_unauthenticated_repositories(migrated_sqlite_database: Database) -> None:
    async with AuthUnitOfWork(migrated_sqlite_database) as uow:
        assert uow.conn is not None
        assert uow.users.connection is uow.conn
        assert uow.verification_codes.connection is uow.conn

        assert hasattr(uow, "users")
        assert hasattr(uow, "verification_codes")

        for forbidden in ("sessions", "memory", "secrets", "runs", "workflow", "workspaces"):
            assert not hasattr(uow, forbidden)


@pytest.mark.asyncio
async def test_auth_uow_bootstraps_user_and_default_workspace_atomically(
    migrated_sqlite_database: Database,
) -> None:
    saved_conn = None

    async with AuthUnitOfWork(migrated_sqlite_database) as uow:
        saved_conn = uow.conn
        created = await uow.users.create_user_with_default_workspace("  USER@example.com  ")

        assert isinstance(created, UserRecord)
        assert created.email == "user@example.com"
        assert created.status == "active"
        assert created.default_workspace_id is not None

        user_row = (
            await uow.conn.execute(
                text(
                    """
                    SELECT id, email, status, default_workspace_id
                    FROM users
                    WHERE id = :user_id
                    """
                ),
                {"user_id": created.id},
            )
        ).mappings().one()
        workspace_row = (
            await uow.conn.execute(
                text(
                    """
                    SELECT id, tenant_id, slug, name, status
                    FROM workspaces
                    WHERE tenant_id = :tenant_id
                    """
                ),
                {"tenant_id": created.id},
            )
        ).mappings().one()

        assert user_row["email"] == "user@example.com"
        assert user_row["status"] == "active"
        assert user_row["default_workspace_id"] == workspace_row["id"]
        assert workspace_row["tenant_id"] == created.id
        assert workspace_row["slug"] == "default"
        assert workspace_row["name"] == "Default"
        assert workspace_row["status"] == "active"

    assert saved_conn is not None
    assert saved_conn.closed is True

    async with migrated_sqlite_database.connect() as conn:
        persisted = (
            await conn.execute(
                text(
                    """
                    SELECT u.email, u.status, u.default_workspace_id, w.slug, w.status AS workspace_status
                    FROM users AS u
                    JOIN workspaces AS w
                      ON w.tenant_id = u.id
                     AND w.id = u.default_workspace_id
                    WHERE u.email = 'user@example.com'
                    """
                )
            )
        ).mappings().one()

    assert persisted["status"] == "active"
    assert persisted["default_workspace_id"] is not None
    assert persisted["slug"] == "default"
    assert persisted["workspace_status"] == "active"


@pytest.mark.asyncio
async def test_auth_uow_rolls_back_bootstrap_probe_and_closes_connection(
    migrated_sqlite_database: Database,
) -> None:
    saved_conn = None

    with pytest.raises(BootstrapProbeError):
        async with AuthUnitOfWork(migrated_sqlite_database) as uow:
            saved_conn = uow.conn
            await uow.users.create_user_with_default_workspace(
                "probe@example.com",
                fail_after_workspace=True,
            )

    assert saved_conn is not None
    assert saved_conn.closed is True

    async with migrated_sqlite_database.connect() as conn:
        user_count = await conn.scalar(text("SELECT COUNT(*) FROM users WHERE email = 'probe@example.com'"))
        workspace_count = await conn.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM workspaces
                WHERE tenant_id IN (SELECT id FROM users WHERE email = 'probe@example.com')
                """
            )
        )

    assert user_count == 0
    assert workspace_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["sqlite", "mysql"])
async def test_concurrent_bootstrap_same_email_returns_existing_user_once(
    driver: str,
    tmp_path: Path,
) -> None:
    if driver == "sqlite":
        database = await _create_database(driver="sqlite", database_url=_sqlite_url(tmp_path, "race.db"))
    else:
        database = await _create_mysql_database()

    try:
        email = f"race-{uuid4()}@example.com"

        async def bootstrap() -> UserRecord:
            async with AuthUnitOfWork(database) as uow:
                return await uow.users.create_user_with_default_workspace(email)

        first, second = await asyncio.gather(bootstrap(), bootstrap())

        assert first.id == second.id
        assert first.email == email
        assert second.email == email
        assert first.default_workspace_id == second.default_workspace_id
        assert first.default_workspace_id is not None

        async with database.connect() as conn:
            counts = (
                await conn.execute(
                    text(
                        """
                        SELECT
                            (SELECT COUNT(*) FROM users WHERE email = :email) AS user_count,
                            (
                                SELECT COUNT(*)
                                FROM workspaces
                                WHERE tenant_id = (SELECT id FROM users WHERE email = :email)
                                  AND slug = 'default'
                                  AND status = 'active'
                            ) AS workspace_count,
                            (
                                SELECT COUNT(*)
                                FROM users
                                WHERE email = :email
                                  AND default_workspace_id IS NOT NULL
                                  AND status = 'active'
                            ) AS initialized_user_count
                        """
                    ),
                    {"email": email},
                )
            ).mappings().one()

        assert counts["user_count"] == 1
        assert counts["workspace_count"] == 1
        assert counts["initialized_user_count"] == 1
    finally:
        await database.dispose()
