import asyncio
from pathlib import Path

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"


async def _create_database(tmp_path: Path) -> Database:
    database_url = _sqlite_url(tmp_path)
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")
    return Database.create(DatabaseSettings(driver="sqlite", url=database_url))


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "readiness-jwt-key-material-1234567890")
    database = asyncio.run(_create_database(tmp_path))
    try:
        yield database
    finally:
        asyncio.run(database.dispose())


def test_readiness_fails_closed_on_stale_schema_revision_without_running_upgrade(
    migrated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    import multiclaw.server as server

    calls: list[tuple[object, str]] = []

    def _unexpected_upgrade(config, revision):
        calls.append((config, revision))
        raise AssertionError("readiness must not run alembic upgrade")

    monkeypatch.setattr(command, "upgrade", _unexpected_upgrade)
    asyncio.run(
        _set_revision(
            migrated_database,
            revision="00000000_not_head",
        )
    )

    with TestClient(server.app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload.get("status") == "not_ready"
    assert "schema_revision" in payload.get("checks_failed", [])
    assert calls == []


def test_liveness_is_public_and_does_not_touch_database(
    migrated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    import multiclaw.server as server

    touched = False

    class _BrokenDatabase:
        dialect = migrated_database.dialect

        def connect(self):
            nonlocal touched
            touched = True
            raise AssertionError("liveness must not touch the database")

    with TestClient(server.app) as client:
        monkeypatch.setattr(server.app.state, "database", _BrokenDatabase())
        response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "live"}
    assert touched is False


async def _set_revision(database: Database, *, revision: str) -> None:
    async with database.write_transaction() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = :revision"), {"revision": revision})
