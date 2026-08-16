import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy import text

from multiclaw.auth.models import LOGIN_CODE_PURPOSE, MAX_SENDS_PER_DAY, UserRecord
from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.schema import verification_codes
from multiclaw.storage.repositories.auth import AuthUserRepository, BootstrapProbeError
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
    raise NotImplementedError


class _FakeTransaction:
    def __init__(
        self,
        *,
        rollback_error: BaseException | None = None,
        commit_error: BaseException | None = None,
        active: bool = True,
    ) -> None:
        self.rollback_error = rollback_error
        self.commit_error = commit_error
        self.is_active = active
        self.rollback_calls = 0
        self.commit_calls = 0

    async def rollback(self) -> None:
        self.rollback_calls += 1
        if self.rollback_error is not None:
            raise self.rollback_error
        self.is_active = False

    async def commit(self) -> None:
        self.commit_calls += 1
        if self.commit_error is not None:
            raise self.commit_error
        self.is_active = False


class _FakeConnection:
    def __init__(
        self,
        *,
        close_error: BaseException | None = None,
        execute_side_effects: list[object] | None = None,
        nested_tx: _FakeTransaction | None = None,
    ) -> None:
        self.close_error = close_error
        self.execute_side_effects = list(execute_side_effects or [])
        self.nested_tx = nested_tx or _FakeTransaction()
        self.close_calls = 0
        self.closed = False
        self.begin_nested_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    async def begin_nested(self) -> _FakeTransaction:
        self.begin_nested_calls += 1
        return self.nested_tx

    async def execute(self, *_args, **_kwargs):
        if not self.execute_side_effects:
            return None
        effect = self.execute_side_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        if callable(effect):
            return effect()
        return effect


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def connect(self) -> _FakeConnection:
        return self._connection


class _FakeDialect:
    def __init__(self, tx: _FakeTransaction) -> None:
        self._tx = tx
        self.begin_write_calls = 0

    async def begin_write(self, _connection: _FakeConnection) -> _FakeTransaction:
        self.begin_write_calls += 1
        return self._tx

    def db_now_ms(self) -> int:
        return 1


class _FakeDatabase:
    def __init__(self, connection: _FakeConnection, tx: _FakeTransaction) -> None:
        self.engine = _FakeEngine(connection)
        self.dialect = _FakeDialect(tx)


class _LockTrackingTransaction(_FakeTransaction):
    def __init__(self, events: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._events = events

    async def rollback(self) -> None:
        self._events.append("rollback")
        await super().rollback()

    async def commit(self) -> None:
        self._events.append("commit")
        await super().commit()


class _LockTrackingConnection(_FakeConnection):
    def __init__(self, events: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._events = events

    async def close(self) -> None:
        self._events.append("close")
        await super().close()


class _LockTrackingDialect(_FakeDialect):
    def __init__(self, tx: _FakeTransaction, events: list[str]) -> None:
        super().__init__(tx)
        self._events = events

    async def acquire_verification_codes_lock(
        self,
        connection: _FakeConnection,
        *,
        purpose: str,
        email: str,
        timeout_seconds: int,
    ) -> str:
        self._events.append(f"acquire:{purpose}:{email}:{timeout_seconds}:{id(connection)}")
        return "lock-token"

    async def release_verification_codes_lock(
        self,
        connection: _FakeConnection,
        lock_name: str,
    ) -> None:
        self._events.append(f"release:{lock_name}:{id(connection)}")


class _LockTrackingDatabase(_FakeDatabase):
    def __init__(self, connection: _FakeConnection, tx: _FakeTransaction, events: list[str]) -> None:
        self.engine = _FakeEngine(connection)
        self.dialect = _LockTrackingDialect(tx, events)


class _BrokenBindUnitOfWork(AuthUnitOfWork):
    def __init__(self, database: object, bind_error: BaseException) -> None:
        super().__init__(database)  # type: ignore[arg-type]
        self._bind_error = bind_error

    def _bind_repositories(self) -> None:
        raise self._bind_error


@pytest.fixture
async def migrated_sqlite_database(tmp_path: Path):
    database = await _create_database(driver="sqlite", database_url=_sqlite_url(tmp_path, "tenant-uow.db"))
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def isolated_mysql_database_url():
    database_url = _ORIGINAL_TEST_MYSQL_URL or os.getenv("MULTICLAW_TEST_MYSQL_URL")
    if not database_url:
        pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")

    admin_database = Database.create(DatabaseSettings(driver="mysql", url=database_url))
    schema_name = f"multiclaw_task4_{uuid4().hex[:12]}"
    temporary_url = make_url(database_url).set(database=schema_name).render_as_string(hide_password=False)

    try:
        async with admin_database.write_transaction() as conn:
            await conn.execute(text(f"CREATE DATABASE `{schema_name}` CHARACTER SET utf8mb4"))
        yield temporary_url
    finally:
        async with admin_database.write_transaction() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS `{schema_name}`"))
        await admin_database.dispose()


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


async def _insert_secondary_scope(database: Database) -> tuple[str, str]:
    tenant_id = "tenant-00000000-0000-0000-0000-000000000002"
    workspace_id = "workspace-0000-0000-0000-000000000002"

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
            {"tenant_id": tenant_id, "email": "tenant2@example.com"},
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

    return tenant_id, workspace_id


async def _get_login_code_count(database: Database, email: str) -> int:
    async with database.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM verification_codes
                WHERE email = :email AND purpose = 'login'
                """
            ),
            {"email": email},
        )
    return int(result.scalar_one())


async def _send_code_like_flow(
    database: Database,
    *,
    email: str,
    delay_seconds: float = 0.0,
) -> int:
    async with AuthUnitOfWork(database) as uow:
        await uow.verification_codes.acquire_rate_limit_lock(
            email=email,
            purpose=LOGIN_CODE_PURPOSE,
        )
        recent = await uow.verification_codes.count_recent_codes(
            email,
            purpose=LOGIN_CODE_PURPOSE,
            window_ms=24 * 60 * 60 * 1000,
        )
        if recent >= MAX_SENDS_PER_DAY:
            return 429
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        await uow.verification_codes.issue_code(
            email=email,
            purpose=LOGIN_CODE_PURPOSE,
            code_digest=f"digest-{uuid4().hex}",
            ttl_seconds=900,
        )
        return 200


@pytest.mark.asyncio
async def test_send_code_lock_releases_after_commit_before_close_then_provider_runs() -> None:
    events: list[str] = []
    tx = _LockTrackingTransaction(events)
    conn = _LockTrackingConnection(events)
    database = _LockTrackingDatabase(conn, tx, events)

    async with AuthUnitOfWork(database) as uow:  # type: ignore[arg-type]
        await uow.verification_codes.acquire_rate_limit_lock(
            email="user@example.com",
            purpose=LOGIN_CODE_PURPOSE,
        )
        events.append("body")
    events.append("provider")

    assert events[0].startswith("acquire:login:user@example.com")
    assert events[1] == "body"
    assert events[2] == "commit"
    assert events[3].startswith("release:lock-token")
    assert events[4] == "close"
    assert events[5] == "provider"


@pytest.mark.asyncio
async def test_send_code_lock_releases_after_rollback_before_close() -> None:
    events: list[str] = []
    tx = _LockTrackingTransaction(events)
    conn = _LockTrackingConnection(events)
    database = _LockTrackingDatabase(conn, tx, events)
    uow = AuthUnitOfWork(database)  # type: ignore[arg-type]
    await uow.__aenter__()
    await uow.verification_codes.acquire_rate_limit_lock(
        email="user@example.com",
        purpose=LOGIN_CODE_PURPOSE,
    )
    body_error = RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        await uow.__aexit__(RuntimeError, body_error, None)

    assert events[0].startswith("acquire:login:user@example.com")
    assert events[1] == "rollback"
    assert events[2].startswith("release:lock-token")
    assert events[3] == "close"


@pytest.mark.asyncio
async def test_send_code_lock_releases_on_cancelled_error_before_close() -> None:
    events: list[str] = []
    tx = _LockTrackingTransaction(events)
    conn = _LockTrackingConnection(events)
    database = _LockTrackingDatabase(conn, tx, events)
    uow = AuthUnitOfWork(database)  # type: ignore[arg-type]
    await uow.__aenter__()
    await uow.verification_codes.acquire_rate_limit_lock(
        email="user@example.com",
        purpose=LOGIN_CODE_PURPOSE,
    )
    cancelled = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await uow.__aexit__(asyncio.CancelledError, cancelled, None)

    assert events[0].startswith("acquire:login:user@example.com")
    assert events[1] == "rollback"
    assert events[2].startswith("release:lock-token")
    assert events[3] == "close"


@pytest.mark.asyncio
async def test_send_code_lock_releases_after_commit_failure_before_close() -> None:
    events: list[str] = []
    tx = _LockTrackingTransaction(events, commit_error=RuntimeError("commit failed"))
    conn = _LockTrackingConnection(events)
    database = _LockTrackingDatabase(conn, tx, events)
    uow = AuthUnitOfWork(database)  # type: ignore[arg-type]
    await uow.__aenter__()
    await uow.verification_codes.acquire_rate_limit_lock(
        email="user@example.com",
        purpose=LOGIN_CODE_PURPOSE,
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await uow.__aexit__(None, None, None)

    assert events[0].startswith("acquire:login:user@example.com")
    assert events[1] == "commit"
    assert events[2].startswith("release:lock-token")
    assert events[3] == "close"


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["sqlite", "mysql"])
async def test_concurrent_send_code_flow_never_exceeds_max_sends_per_day(
    driver: str,
    tmp_path: Path,
    isolated_mysql_database_url: str,
) -> None:
    if driver == "sqlite":
        database = await _create_database(
            driver="sqlite",
            database_url=_sqlite_url(tmp_path, "send-code-rate-limit.db"),
        )
    else:
        database = await _create_database(driver="mysql", database_url=isolated_mysql_database_url)

    try:
        email = f"rate-limit-{uuid4()}@example.com"
        results = await asyncio.gather(
            *[
                _send_code_like_flow(
                    database,
                    email=email,
                    delay_seconds=0.05,
                )
                for _ in range(MAX_SENDS_PER_DAY + 2)
            ]
        )

        assert results.count(200) == MAX_SENDS_PER_DAY
        assert results.count(429) == 2
        assert await _get_login_code_count(database, email) == MAX_SENDS_PER_DAY
    finally:
        await database.dispose()


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
async def test_tenant_uow_rejects_mismatched_workspace_scope(migrated_sqlite_database: Database) -> None:
    primary = await _insert_seed_scope(migrated_sqlite_database)
    _other_tenant_id, other_workspace_id = await _insert_secondary_scope(migrated_sqlite_database)
    context = TenantContext(
        tenant_id=primary.tenant_id,
        workspace_id=other_workspace_id,
    )

    async with TenantUnitOfWork(migrated_sqlite_database, context) as uow:
        with pytest.raises(NoResultFound):
            await uow.users.get_current()

        with pytest.raises(NoResultFound):
            await uow.workspaces.get_current()


@pytest.mark.asyncio
async def test_tenant_uow_rejects_missing_workspace_scope(migrated_sqlite_database: Database) -> None:
    primary = await _insert_seed_scope(migrated_sqlite_database)
    context = TenantContext(
        tenant_id=primary.tenant_id,
        workspace_id="workspace-missing",
    )

    async with TenantUnitOfWork(migrated_sqlite_database, context) as uow:
        with pytest.raises(NoResultFound):
            await uow.users.get_current()

        with pytest.raises(NoResultFound):
            await uow.workspaces.get_current()


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
async def test_auth_uow_read_only_skips_begin_write_and_closes_connection() -> None:
    tx = _FakeTransaction()
    conn = _FakeConnection()
    database = _FakeDatabase(conn, tx)

    async with AuthUnitOfWork(database, read_only=True) as uow:  # type: ignore[arg-type]
        assert uow.conn is conn
        assert database.dialect.begin_write_calls == 0
        assert uow.users.connection is conn
        assert uow.verification_codes.connection is conn

    assert conn.close_calls == 1
    assert tx.commit_calls == 0
    assert tx.rollback_calls == 0


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
@pytest.mark.parametrize("seed_mode", ["user_only", "user_and_unbound_workspace"])
async def test_bootstrap_fails_closed_for_existing_half_initialized_user(
    migrated_sqlite_database: Database,
    seed_mode: str,
) -> None:
    async with migrated_sqlite_database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users (
                    id, email, auth_epoch, default_workspace_id, status,
                    purge_after, created_at, updated_at, disabled_at, purge_requested_at
                )
                VALUES (
                    :tenant_id,
                    'half@example.com',
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
            {"tenant_id": "tenant-half-0000-0000-0000-000000000001"},
        )
        if seed_mode == "user_and_unbound_workspace":
            await conn.execute(
                text(
                    """
                    INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                    VALUES (
                        'workspace-half-0000-0000-0000-000000000001',
                        :tenant_id,
                        'default',
                        'Default',
                        'active',
                        1,
                        1
                    )
                    """
                ),
                {"tenant_id": "tenant-half-0000-0000-0000-000000000001"},
            )

    async with AuthUnitOfWork(migrated_sqlite_database) as uow:
        with pytest.raises(RuntimeError, match="not fully bootstrapped"):
            await uow.users.create_user_with_default_workspace("half@example.com")

    async with migrated_sqlite_database.connect() as conn:
        counts = (
            await conn.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM users WHERE email = 'half@example.com') AS user_count,
                        (
                            SELECT COUNT(*)
                            FROM workspaces
                            WHERE tenant_id = 'tenant-half-0000-0000-0000-000000000001'
                        ) AS workspace_count
                    """
                )
            )
        ).mappings().one()

    assert counts["user_count"] == 1
    assert counts["workspace_count"] == (0 if seed_mode == "user_only" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["sqlite", "mysql"])
async def test_concurrent_bootstrap_same_email_returns_existing_user_once(
    driver: str,
    tmp_path: Path,
    isolated_mysql_database_url: str,
) -> None:
    if driver == "sqlite":
        database = await _create_database(driver="sqlite", database_url=_sqlite_url(tmp_path, "race.db"))
    else:
        database = await _create_database(driver="mysql", database_url=isolated_mysql_database_url)

    try:
        email = f"race-{uuid4()}@example.com"
        conflict_calls = 0

        original = getattr(AuthUserRepository, "_rollback_savepoint")

        async def spy_rollback_savepoint(self, savepoint, primary_error):
            nonlocal conflict_calls
            if isinstance(primary_error, IntegrityError):
                conflict_calls += 1
            return await original(self, savepoint, primary_error)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(AuthUserRepository, "_rollback_savepoint", spy_rollback_savepoint)

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
        assert conflict_calls == 1
    finally:
        monkeypatch.undo()
        await database.dispose()


@pytest.mark.asyncio
async def test_uow_enter_preserves_bind_error_and_notes_cleanup_failures() -> None:
    bind_error = RuntimeError("bind failed")
    rollback_error = RuntimeError("rollback cleanup failed")
    close_error = RuntimeError("close cleanup failed")
    tx = _FakeTransaction(rollback_error=rollback_error)
    conn = _FakeConnection(close_error=close_error)
    database = _FakeDatabase(conn, tx)
    uow = _BrokenBindUnitOfWork(database, bind_error)

    with pytest.raises(RuntimeError, match="bind failed") as exc_info:
        await uow.__aenter__()

    assert exc_info.value is bind_error
    assert tx.rollback_calls == 1
    assert conn.close_calls == 1
    assert exc_info.value.__notes__


@pytest.mark.asyncio
async def test_uow_exit_preserves_body_error_when_rollback_and_close_fail() -> None:
    tx = _FakeTransaction(rollback_error=RuntimeError("rollback cleanup failed"))
    conn = _FakeConnection(close_error=RuntimeError("close cleanup failed"))
    database = _FakeDatabase(conn, tx)
    uow = AuthUnitOfWork(database)  # type: ignore[arg-type]
    await uow.__aenter__()
    body_error = RuntimeError("body failed")

    with pytest.raises(RuntimeError, match="body failed") as exc_info:
        await uow.__aexit__(RuntimeError, body_error, None)

    assert tx.rollback_calls == 1
    assert conn.close_calls == 1
    assert exc_info.value is body_error
    assert exc_info.value.__notes__


@pytest.mark.asyncio
async def test_uow_exit_preserves_commit_error_when_close_also_fails() -> None:
    tx = _FakeTransaction(commit_error=RuntimeError("commit failed"))
    conn = _FakeConnection(close_error=RuntimeError("close cleanup failed"))
    database = _FakeDatabase(conn, tx)
    uow = AuthUnitOfWork(database)  # type: ignore[arg-type]
    await uow.__aenter__()

    with pytest.raises(RuntimeError, match="commit failed") as exc_info:
        await uow.__aexit__(None, None, None)

    assert tx.commit_calls == 1
    assert conn.close_calls == 1
    assert exc_info.value.__notes__


@pytest.mark.asyncio
async def test_uow_exit_raises_close_failure_without_primary_error() -> None:
    tx = _FakeTransaction(active=False)
    conn = _FakeConnection(close_error=RuntimeError("close cleanup failed"))
    database = _FakeDatabase(conn, tx)
    uow = AuthUnitOfWork(database)  # type: ignore[arg-type]
    await uow.__aenter__()

    with pytest.raises(RuntimeError, match="close cleanup failed"):
        await uow.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_bootstrap_preserves_integrity_error_when_savepoint_rollback_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    integrity_error = IntegrityError("INSERT INTO users", {"email": "dup@example.com"}, RuntimeError("dup"))
    rollback_error = RuntimeError("savepoint rollback failed")
    conn = _FakeConnection(
        execute_side_effects=[integrity_error],
        nested_tx=_FakeTransaction(rollback_error=rollback_error),
    )
    repo = AuthUserRepository(conn, _FakeDialect(_FakeTransaction()))  # type: ignore[arg-type]
    monkeypatch.setattr(repo, "_get_existing_bootstrapped_user", _unexpected_existing_lookup)

    with pytest.raises(IntegrityError) as exc_info:
        await repo.create_user_with_default_workspace("dup@example.com")

    assert exc_info.value is integrity_error
    assert exc_info.value.__notes__


@pytest.mark.asyncio
async def test_bootstrap_preserves_probe_error_when_savepoint_rollback_fails() -> None:
    rollback_error = RuntimeError("savepoint rollback failed")
    conn = _FakeConnection(
        execute_side_effects=[None, None],
        nested_tx=_FakeTransaction(rollback_error=rollback_error),
    )
    repo = AuthUserRepository(conn, _FakeDialect(_FakeTransaction()))  # type: ignore[arg-type]

    with pytest.raises(BootstrapProbeError) as exc_info:
        await repo.create_user_with_default_workspace(
            "dup@example.com",
            fail_after_workspace=True,
        )

    assert exc_info.value.__notes__


@pytest.mark.asyncio
async def test_bootstrap_propagates_savepoint_commit_failure() -> None:
    commit_error = RuntimeError("savepoint commit failed")
    conn = _FakeConnection(
        execute_side_effects=[None, None, None],
        nested_tx=_FakeTransaction(commit_error=commit_error),
    )
    repo = AuthUserRepository(conn, _FakeDialect(_FakeTransaction()))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="savepoint commit failed"):
        await repo.create_user_with_default_workspace("dup@example.com")


async def _unexpected_existing_lookup(_email: str) -> UserRecord:
    raise AssertionError("existing-user lookup should not run after rollback failure")
