import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"


def _keyring_payload() -> str:
    return base64.b64encode(
        json.dumps(
            {
                "active_key_version": 3,
                "keys": {
                    "3": base64.b64encode(bytes(range(32))).decode("ascii"),
                },
            }
        ).encode("utf-8")
    ).decode("ascii")


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
    monkeypatch.setenv("MULTICLAW_SECRETS_KEYRING_B64", _keyring_payload())
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
        client.app.state.operational_metrics.clear()
        response = client.get("/api/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert set(payload) == {"ready", "status", "checks_failed"}
    assert payload.get("status") == "not_ready"
    assert "schema_revision" in payload.get("checks_failed", [])
    assert calls == []
    assert _metric_count_for(server.app.state.operational_metrics, "multiclaw_migration_revision_failures_total") == 1


def test_readiness_records_keyring_failure_metric(migrated_database: Database, monkeypatch: pytest.MonkeyPatch):
    import multiclaw.server as server

    monkeypatch.delenv("MULTICLAW_SECRETS_KEYRING_B64", raising=False)

    with TestClient(server.app) as client:
        client.app.state.operational_metrics.clear()
        response = client.get("/api/health/ready")

    assert response.status_code == 503
    assert "keyring" in response.json()["checks_failed"]
    assert _metric_count_for(server.app.state.operational_metrics, "multiclaw_keyring_failures_total") == 1


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


def test_readiness_reports_active_default_workspace_integrity_without_leaking_details(
    migrated_database: Database,
):
    import multiclaw.server as server

    asyncio.run(_seed_active_user_without_default_workspace(migrated_database))

    with TestClient(server.app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "status": "not_ready",
        "checks_failed": ["active_default_workspace_integrity"],
    }


def test_readiness_mysql_branch_uses_low_cardinality_contract_failures(monkeypatch: pytest.MonkeyPatch):
    import multiclaw.api.health as health_module
    from fastapi import FastAPI
    from starlette.requests import Request

    class _FakeConn:
        async def scalar(self, stmt):
            sql = str(stmt)
            lowered = sql.lower()
            if "version()" in lowered:
                return "8.0.35"
            if "@@session.time_zone" in sql:
                return "+08:00"
            if "@@transaction_isolation" in sql:
                return "REPEATABLE-READ"
            if "@@character_set_database" in sql:
                return "latin1"
            return None

        async def execute(self, stmt):
            sql = str(stmt).lower()
            if "information_schema.tables" in sql and "engine" in sql:
                return _MappingsResult([{"engine": "MyISAM", "table_name": "users"}])
            if "information_schema.tables" in sql and "table_collation" in sql:
                return _MappingsResult([{"table_name": "users", "table_collation": "latin1_swedish_ci"}])
            if "information_schema.tables" in sql and "table_name" in sql:
                return _MappingsResult([{"table_name": name} for name in health_module.metadata.tables])
            if "information_schema.referential_constraints" in sql:
                return _MappingsResult([])
            return _MappingsResult([])

        async def run_sync(self, fn):
            return fn(SimpleNamespace())

    class _FakeConnect:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeDatabase:
        dialect = SimpleNamespace(name="mysql")

        def connect(self):
            return _FakeConnect()

    monkeypatch.setattr(
        health_module.MigrationContext,
        "configure",
        lambda sync_conn: SimpleNamespace(get_current_revision=lambda: "20260815_0001"),
    )
    monkeypatch.setattr(
        health_module.ScriptDirectory,
        "from_config",
        lambda config: SimpleNamespace(get_current_head=lambda: "20260815_0001"),
    )
    monkeypatch.setattr(
        health_module.DeploymentKeyring,
        "load",
        lambda settings: SimpleNamespace(require_versions=lambda usage: None),
    )

    app = FastAPI()
    app.state.database = _FakeDatabase()
    app.state.settings = SimpleNamespace(
        database=SimpleNamespace(url="mysql+aiomysql://fake"),
        secrets=SimpleNamespace(),
    )
    app.state.workspace_root = Path.cwd()
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/api/health/ready", "headers": []})

    response = asyncio.run(health_module.health_ready(request))

    assert response.status_code == 503
    assert json.loads(response.body.decode()) == {
        "ready": False,
        "status": "not_ready",
        "checks_failed": [
            "backend_version",
            "mysql_time_zone",
            "mysql_isolation",
            "mysql_innodb",
            "mysql_charset",
            "schema_integrity",
        ],
    }


@pytest.mark.parametrize(
    ("database_charset", "table_collations", "expected_failed"),
    [
        ("utf8mb4", ["utf8mb4_0900_ai_ci", "utf8mb4_bin"], []),
        ("latin1", ["utf8mb4_0900_ai_ci", "utf8mb4_bin"], ["mysql_charset"]),
        ("utf8mb4", ["utf8mb4_0900_ai_ci", "latin1_swedish_ci"], ["mysql_charset"]),
    ],
)
def test_readiness_mysql_charset_branch_respects_database_and_table_collation(
    monkeypatch: pytest.MonkeyPatch,
    database_charset: str,
    table_collations: list[str],
    expected_failed: list[str],
):
    import multiclaw.api.health as health_module
    from fastapi import FastAPI
    from starlette.requests import Request

    expected_fk_rows = _expected_mysql_fk_rows(health_module)

    class _FakeConn:
        async def scalar(self, stmt):
            sql = str(stmt)
            lowered = sql.lower()
            if "version()" in lowered:
                return "8.0.36"
            if "@@session.time_zone" in sql:
                return "+00:00"
            if "@@transaction_isolation" in sql:
                return "READ-COMMITTED"
            if "@@character_set_database" in sql:
                return database_charset
            if "count(*)" in lowered and "from users" in lowered:
                return 0
            return None

        async def execute(self, stmt):
            sql = str(stmt).lower()
            if "information_schema.tables" in sql and "engine" in sql:
                return _MappingsResult(
                    [
                        {"engine": "InnoDB", "table_name": name}
                        for name in health_module.metadata.tables
                    ]
                )
            if "information_schema.tables" in sql and "table_collation" in sql:
                return _MappingsResult(
                    [
                        {
                            "table_name": table_name,
                            "table_collation": table_collation,
                        }
                        for table_name, table_collation in zip(health_module.metadata.tables, table_collations, strict=False)
                    ]
                )
            if "information_schema.tables" in sql and "table_name" in sql:
                return _MappingsResult([{"table_name": name} for name in health_module.metadata.tables])
            if "information_schema.key_column_usage" in sql:
                return _MappingsResult(expected_fk_rows)
            if "information_schema.referential_constraints" in sql:
                return _MappingsResult([{"constraint_name": "fk_demo"}])
            return _MappingsResult([])

        async def run_sync(self, fn):
            return fn(SimpleNamespace())

    class _FakeConnect:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeDatabase:
        dialect = SimpleNamespace(name="mysql")

        def connect(self):
            return _FakeConnect()

    monkeypatch.setattr(
        health_module.MigrationContext,
        "configure",
        lambda sync_conn: SimpleNamespace(get_current_revision=lambda: "20260815_0001"),
    )
    monkeypatch.setattr(
        health_module.ScriptDirectory,
        "from_config",
        lambda config: SimpleNamespace(get_current_head=lambda: "20260815_0001"),
    )
    monkeypatch.setattr(
        health_module.DeploymentKeyring,
        "load",
        lambda settings: SimpleNamespace(require_versions=lambda usage: None),
    )

    app = FastAPI()
    app.state.database = _FakeDatabase()
    app.state.settings = SimpleNamespace(
        database=SimpleNamespace(url="mysql+aiomysql://fake"),
        secrets=SimpleNamespace(),
    )
    app.state.workspace_root = Path.cwd()
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/api/health/ready", "headers": []})

    response = asyncio.run(health_module.health_ready(request))
    payload = json.loads(response.body.decode())

    if expected_failed:
        assert response.status_code == 503
        assert payload == {
            "ready": False,
            "status": "not_ready",
            "checks_failed": expected_failed,
        }
    else:
        assert response.status_code == 200
        assert payload == {
            "ready": True,
            "status": "ready",
            "checks_failed": [],
        }


@pytest.mark.parametrize(
    ("backend_name", "version", "expected"),
    [
        ("mysql", "8.0.35-0ubuntu0.22.04.1", False),
        ("mysql", "8.0.36-0ubuntu0.22.04.1", True),
        ("mysql", "8.0.36", True),
        ("mysql", "8.4.0", True),
        ("mysql", "8.4.2-commercial", True),
        ("mysql", "8.0.36-foobar", False),
        ("mysql", "8.0.36-customfork", False),
        ("mysql", "8.0.36-cluster", False),
        ("mysql", "10.11.2-MariaDB-1:10.11.2+maria~ubu2204", False),
        ("mysql", "percona-8.0.36", False),
        ("mysql", "5.7.44", False),
        ("mysql", "nonsense", False),
        ("sqlite", "3.34.9", False),
        ("sqlite", "3.35.0", True),
    ],
)
def test_backend_version_ok_strict_vendor_and_suffix_parsing(
    backend_name: str,
    version: str,
    expected: bool,
):
    import multiclaw.api.health as health_module

    assert health_module._backend_version_ok(backend_name, version) is expected


@pytest.mark.parametrize(
    ("mutator", "expected_failed"),
    [
        (lambda rows: rows[:-1], ["schema_integrity"]),
        (
            lambda rows: [
                {**row, "referenced_column_name": "wrong_column"}
                if row["ordinal_position"] == 1
                else row
                for row in rows
            ],
            ["schema_integrity"],
        ),
        (lambda rows: rows, []),
    ],
)
def test_readiness_mysql_schema_integrity_requires_exact_foreign_key_contract(
    monkeypatch: pytest.MonkeyPatch,
    mutator,
    expected_failed: list[str],
):
    import multiclaw.api.health as health_module
    from fastapi import FastAPI
    from starlette.requests import Request

    expected_fk_rows = _expected_mysql_fk_rows(health_module)
    actual_fk_rows = mutator(expected_fk_rows)

    class _FakeConn:
        async def scalar(self, stmt):
            sql = str(stmt)
            lowered = sql.lower()
            if "version()" in lowered:
                return "8.0.36-0ubuntu0.22.04.1"
            if "@@session.time_zone" in sql:
                return "+00:00"
            if "@@transaction_isolation" in sql:
                return "READ-COMMITTED"
            if "@@character_set_database" in sql:
                return "utf8mb4"
            if "count(*)" in lowered and "from users" in lowered:
                return 0
            return None

        async def execute(self, stmt):
            sql = str(stmt).lower()
            if "information_schema.tables" in sql and "engine" in sql:
                return _MappingsResult(
                    [
                        {"engine": "InnoDB", "table_name": name}
                        for name in health_module.metadata.tables
                    ]
                )
            if "information_schema.tables" in sql and "table_collation" in sql:
                return _MappingsResult(
                    [
                        {"table_name": name, "table_collation": "utf8mb4_0900_ai_ci"}
                        for name in health_module.metadata.tables
                    ]
                )
            if "information_schema.tables" in sql and "table_name" in sql:
                return _MappingsResult([{"table_name": name} for name in health_module.metadata.tables])
            if "information_schema.key_column_usage" in sql:
                return _MappingsResult(actual_fk_rows)
            if "information_schema.referential_constraints" in sql:
                return _MappingsResult([{"constraint_name": "placeholder"}])
            return _MappingsResult([])

        async def run_sync(self, fn):
            return fn(SimpleNamespace())

    class _FakeConnect:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeDatabase:
        dialect = SimpleNamespace(name="mysql")

        def connect(self):
            return _FakeConnect()

    monkeypatch.setattr(
        health_module.MigrationContext,
        "configure",
        lambda sync_conn: SimpleNamespace(get_current_revision=lambda: "20260815_0001"),
    )
    monkeypatch.setattr(
        health_module.ScriptDirectory,
        "from_config",
        lambda config: SimpleNamespace(get_current_head=lambda: "20260815_0001"),
    )
    monkeypatch.setattr(
        health_module.DeploymentKeyring,
        "load",
        lambda settings: SimpleNamespace(require_versions=lambda usage: None),
    )

    app = FastAPI()
    app.state.database = _FakeDatabase()
    app.state.settings = SimpleNamespace(
        database=SimpleNamespace(url="mysql+aiomysql://fake"),
        secrets=SimpleNamespace(),
    )
    app.state.workspace_root = Path.cwd()
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/api/health/ready", "headers": []})

    response = asyncio.run(health_module.health_ready(request))
    payload = json.loads(response.body.decode())

    if expected_failed:
        assert response.status_code == 503
        assert payload == {"ready": False, "status": "not_ready", "checks_failed": expected_failed}
    else:
        assert response.status_code == 200
        assert payload == {"ready": True, "status": "ready", "checks_failed": []}


async def _set_revision(database: Database, *, revision: str) -> None:
    async with database.write_transaction() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = :revision"), {"revision": revision})


async def _seed_active_user_without_default_workspace(database: Database) -> None:
    async with database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users (
                    id, email, auth_epoch, default_workspace_id, status,
                    purge_after, created_at, updated_at, disabled_at, purge_requested_at
                )
                VALUES (:user_id, :email, 0, NULL, 'active', NULL, 1, 1, NULL, NULL)
                """
            ),
            {"user_id": "11111111-1111-1111-1111-111111111111", "email": "broken-active@example.com"},
        )


class _MappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


def _expected_mysql_fk_rows(health_module) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for table in health_module.metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            if not constraint.elements:
                continue
            referenced_table = next(iter(constraint.elements)).column.table.name
            for ordinal, element in enumerate(constraint.elements, start=1):
                rows.append(
                    {
                        "table_name": table.name,
                        "constraint_name": constraint.name,
                        "column_name": element.parent.name,
                        "referenced_table_name": referenced_table,
                        "referenced_column_name": element.column.name,
                        "ordinal_position": ordinal,
                    }
                )
    return rows


def _metric_count_for(metrics, name: str) -> int:
    return sum(value for (metric_name, _labels), value in metrics.counters.items() if metric_name == name)
