from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from multiclaw.config import Settings
from multiclaw.storage import Database
from multiclaw.storage.schema import metadata


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata


def _driver_for_url(database_url: str) -> str:
    if database_url.startswith("sqlite+aiosqlite://"):
        return "sqlite"
    if database_url.startswith("mysql+aiomysql://"):
        return "mysql"
    raise ValueError(f"Unsupported database URL for migrations: {database_url!r}")


def _database_settings():
    settings = Settings()
    override_url = context.get_x_argument(as_dictionary=True).get("database_url")
    configured_url = override_url or config.get_main_option("sqlalchemy.url") or settings.database.url
    if configured_url == settings.database.url:
        return settings.database
    return settings.database.model_copy(
        update={
            "driver": _driver_for_url(configured_url),
            "url": configured_url,
        }
    )


def _configure_context(connection=None, *, url: str | None = None) -> None:
    database_settings = _database_settings()
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=database_settings.driver == "sqlite",
        literal_binds=connection is None,
        dialect_opts={"paramstyle": "named"} if connection is None else None,
    )


def run_migrations_offline() -> None:
    database_settings = _database_settings()
    _configure_context(url=database_settings.url)

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    _configure_context(connection=connection)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    database_settings = _database_settings()
    database = Database.create(database_settings)
    try:
        async with database.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await database.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
