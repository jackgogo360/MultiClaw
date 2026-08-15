from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from multiclaw.config import Settings
from multiclaw.storage import Database


def _driver_for_url(database_url: str) -> str:
    if database_url.startswith("sqlite+aiosqlite://"):
        return "sqlite"
    if database_url.startswith("mysql+aiomysql://"):
        return "mysql"
    raise ValueError(f"Unsupported database URL: {database_url!r}")


def _database_settings(database_url: str | None = None):
    settings = Settings()
    if database_url is None:
        return settings.database
    return settings.database.model_copy(
        update={
            "driver": _driver_for_url(database_url),
            "url": database_url,
        }
    )


def alembic_config(*, database_url: str | None = None) -> Config:
    root = Path(__file__).resolve().parents[2]
    database_settings = _database_settings(database_url)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_settings.url)
    return config


async def check_revision_is_head(*, database_url: str | None = None) -> bool:
    database_settings = _database_settings(database_url)
    config = alembic_config(database_url=database_settings.url)
    head_revision = ScriptDirectory.from_config(config).get_current_head()

    database = Database.create(database_settings)
    try:
        async with database.connect() as conn:
            current_revision = await conn.run_sync(
                lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
            )
    finally:
        await database.dispose()

    return current_revision == head_revision


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="multiclaw")
    subparsers = parser.add_subparsers(dest="command", required=True)

    db_parser = subparsers.add_parser("db")
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
    db_subparsers.add_parser("upgrade")
    db_subparsers.add_parser("current")
    db_subparsers.add_parser("check")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command != "db":
        raise ValueError(f"Unsupported command: {args.command}")

    if args.db_command == "upgrade":
        command.upgrade(alembic_config(), "head")
        return 0
    if args.db_command == "current":
        command.current(alembic_config())
        return 0
    if args.db_command == "check":
        return 0 if asyncio.run(check_revision_is_head()) else 1

    raise ValueError(f"Unsupported db command: {args.db_command}")


if __name__ == "__main__":
    raise SystemExit(main())
