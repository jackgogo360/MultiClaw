from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncContextManager, AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect


def install_sqlite_listeners(
    engine: Engine,
    busy_timeout_ms: int,
    *,
    enable_wal: bool,
) -> None:
    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            if enable_wal:
                cursor.execute("PRAGMA journal_mode=WAL")
                _consume_scalar_result(cursor)
                cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def install_mysql_listeners(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def configure_mysql(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET time_zone='+00:00'")
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        finally:
            cursor.close()


def _consume_scalar_result(cursor) -> None:
    try:
        cursor.fetchone()
    except Exception:
        return None
    return None


def _ensure_sqlite_parent_directory(database_url: str) -> bool:
    database_path = make_url(database_url).database
    if not database_path or database_path == ":memory:":
        return False

    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    return True


@dataclass(slots=True)
class Database:
    engine: AsyncEngine
    dialect: SQLiteDialect | MySQLDialect

    @classmethod
    def create(cls, settings: DatabaseSettings) -> "Database":
        sqlite_is_file_backed = False
        if settings.driver == "sqlite":
            sqlite_is_file_backed = _ensure_sqlite_parent_directory(settings.url)

        engine = create_async_engine(
            settings.url,
            pool_pre_ping=True,
            isolation_level="READ COMMITTED" if settings.driver == "mysql" else None,
        )

        if settings.driver == "sqlite":
            install_sqlite_listeners(
                engine.sync_engine,
                settings.sqlite_busy_timeout_ms,
                enable_wal=sqlite_is_file_backed,
            )
            dialect: SQLiteDialect | MySQLDialect = SQLiteDialect()
        else:
            install_mysql_listeners(engine.sync_engine)
            dialect = MySQLDialect()

        return cls(engine=engine, dialect=dialect)

    def connect(self) -> AsyncContextManager[AsyncConnection]:
        return self.engine.connect()

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.connect() as conn:
            tx = await self.dialect.begin_write(conn)
            try:
                yield conn
            except BaseException:
                await tx.rollback()
                raise
            else:
                await tx.commit()

    async def dispose(self) -> None:
        await self.engine.dispose()
