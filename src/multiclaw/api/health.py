from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from multiclaw.cli import alembic_config
from multiclaw.observability import increment_metric, record_trace_event
from multiclaw.secrets.keyring import DeploymentKeyring, SecretKeyringError
from multiclaw.storage.repositories.secrets import DeploymentSecretUsageRepository
from multiclaw.storage.schema import metadata


router = APIRouter()
_SQLITE_MIN_VERSION = (3, 35, 0)
_MYSQL_MIN_VERSION = (8, 0, 36)
_VERSION_PREFIX_RE = re.compile(r"^\s*(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)\b")
_MYSQL_ALLOWED_SUFFIX_RE = re.compile(
    r"^\s*(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?:0ubuntu[0-9A-Za-z.+~:-]+|commercial))?\s*$",
    re.IGNORECASE,
)


@router.get("/api/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/api/health/ready")
async def health_ready(request: Request):
    payload = await _build_readiness_payload(request)
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


async def _build_readiness_payload(request: Request) -> dict[str, Any]:
    database = getattr(request.app.state, "database", None)
    settings = getattr(request.app.state, "settings", None)
    workspace_root = getattr(request.app.state, "workspace_root", None)
    failed: list[str] = []

    if database is None or settings is None:
        return _response(["db_connectivity"])

    try:
        async with database.connect() as conn:
            backend_version = await conn.scalar(
                text("select sqlite_version()") if database.dialect.name == "sqlite" else text("select version()")
            )
            if not _backend_version_ok(database.dialect.name, str(backend_version or "")):
                failed.append("backend_version")

            current_revision = await conn.run_sync(
                lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
            )
            expected_revision = ScriptDirectory.from_config(
                alembic_config(database_url=settings.database.url)
            ).get_current_head()
            if current_revision != expected_revision:
                return _response(["schema_revision"])

            if database.dialect.name == "sqlite":
                fk_enabled = int(await conn.scalar(text("PRAGMA foreign_keys")) or 0)
                if fk_enabled != 1:
                    failed.append("sqlite_foreign_keys")
                if not await _sqlite_schema_integrity_ok(conn):
                    failed.append("schema_integrity")
            else:
                time_zone = str(await conn.scalar(text("SELECT @@session.time_zone")) or "")
                isolation = str(await conn.scalar(text("SELECT @@transaction_isolation")) or "")
                if time_zone not in {"+00:00", "UTC"}:
                    failed.append("mysql_time_zone")
                if isolation.upper() != "READ-COMMITTED":
                    failed.append("mysql_isolation")
                if not await _mysql_innodb_ok(conn):
                    failed.append("mysql_innodb")
                if not await _mysql_charset_ok(conn):
                    failed.append("mysql_charset")
                if not await _mysql_schema_integrity_ok(conn):
                    failed.append("schema_integrity")

            if await _has_active_default_workspace_integrity_failure(conn):
                failed.append("active_default_workspace_integrity")
    except Exception:
        return _response(["db_connectivity"])

    if not _workspace_root_permissions_ok(workspace_root):
        failed.append("workspace_root_permissions")

    if not await _keyring_ok(database, settings):
        failed.append("keyring")

    return _response(failed)


def _response(failed: list[str]) -> dict[str, Any]:
    for check_name in failed:
        if check_name == "schema_revision":
            increment_metric(
                "multiclaw_migration_revision_failures_total",
                labels={"backend": "unknown", "operation": "schema_revision", "status": "error", "error_class": "migration_revision"},
            )
            record_trace_event("migration_revision_failure", attributes={"check": check_name})
        if check_name == "keyring":
            increment_metric(
                "multiclaw_keyring_failures_total",
                labels={"backend": "unknown", "operation": "keyring", "status": "error", "error_class": "keyring_failure"},
            )
            record_trace_event("keyring_failure", attributes={"check": check_name})
    return {
        "ready": not failed,
        "status": "ready" if not failed else "not_ready",
        "checks_failed": failed,
    }


def _backend_version_ok(backend_name: str, version: str) -> bool:
    if backend_name == "sqlite":
        match = _VERSION_PREFIX_RE.match(version)
        if match is None:
            return False
        parsed = (
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
        )
        return parsed >= _SQLITE_MIN_VERSION
    if backend_name == "mysql":
        lowered = version.lower()
        if "mariadb" in lowered or "percona" in lowered:
            return False
        match = _MYSQL_ALLOWED_SUFFIX_RE.match(version)
        if match is None:
            return False
        parsed = (
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
        )
        if parsed[0] != 8:
            return False
        return parsed >= _MYSQL_MIN_VERSION
    return False


async def _sqlite_schema_integrity_ok(conn) -> bool:
    tables_result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    table_names = {str(row["name"]) for row in tables_result.mappings().all()}
    if table_names - {"alembic_version"} != set(metadata.tables):
        return False
    integrity = str(await conn.scalar(text("PRAGMA integrity_check")) or "").lower()
    if integrity != "ok":
        return False
    fk_rows = await conn.execute(text("PRAGMA foreign_key_check"))
    return fk_rows.mappings().all() == []


async def _mysql_innodb_ok(conn) -> bool:
    result = await conn.execute(
        text(
            """
            SELECT table_name, engine
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
            """
        )
    )
    rows = result.mappings().all()
    if not rows:
        return False
    return all(str(row["engine"] or "").upper() == "INNODB" for row in rows)


async def _mysql_schema_integrity_ok(conn) -> bool:
    tables = await conn.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_type = 'BASE TABLE'
            """
        )
    )
    table_names = {str(row["table_name"]) for row in tables.mappings().all()}
    if table_names != set(metadata.tables):
        return False

    actual_fk_rows = await conn.execute(
        text(
            """
            SELECT
                table_name,
                constraint_name,
                column_name,
                referenced_table_name,
                referenced_column_name,
                ordinal_position
            FROM information_schema.key_column_usage
            WHERE table_schema = DATABASE()
              AND referenced_table_schema = DATABASE()
              AND referenced_table_name IS NOT NULL
            ORDER BY table_name, constraint_name, ordinal_position
            """
        )
    )
    actual = {
        (
            str(row["table_name"]),
            str(row["constraint_name"]),
            str(row["column_name"]),
            str(row["referenced_table_name"]),
            str(row["referenced_column_name"]),
            int(row["ordinal_position"]),
        )
        for row in actual_fk_rows.mappings().all()
    }
    return actual == _expected_mysql_fk_rows()


async def _mysql_charset_ok(conn) -> bool:
    database_charset = str(await conn.scalar(text("SELECT @@character_set_database")) or "").lower()
    if database_charset != "utf8mb4":
        return False

    result = await conn.execute(
        text(
            """
            SELECT table_name, table_collation
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_type = 'BASE TABLE'
            """
        )
    )
    rows = result.mappings().all()
    if not rows:
        return False
    return all(str(row["table_collation"] or "").lower().startswith("utf8mb4") for row in rows)


def _expected_mysql_fk_rows() -> set[tuple[str, str, str, str, str, int]]:
    rows: set[tuple[str, str, str, str, str, int]] = set()
    for table in metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            if not constraint.elements or constraint.name is None:
                return set()
            referenced_table = next(iter(constraint.elements)).column.table.name
            for ordinal, element in enumerate(constraint.elements, start=1):
                rows.add(
                    (
                        table.name,
                        constraint.name,
                        element.parent.name,
                        referenced_table,
                        element.column.name,
                        ordinal,
                    )
                )
    return rows


async def _has_active_default_workspace_integrity_failure(conn) -> bool:
    result = await conn.scalar(
        text(
            """
            SELECT COUNT(*)
            FROM users
            LEFT JOIN workspaces
              ON workspaces.tenant_id = users.id
             AND workspaces.id = users.default_workspace_id
            WHERE users.status = 'active'
              AND (
                users.default_workspace_id IS NULL
                OR workspaces.id IS NULL
                OR workspaces.status != 'active'
              )
            """
        )
    )
    return int(result or 0) > 0


def _workspace_root_permissions_ok(workspace_root: object) -> bool:
    if workspace_root is None:
        return False
    path = Path(workspace_root)
    return path.exists() and os.access(path, os.R_OK | os.W_OK | os.X_OK)


async def _keyring_ok(database, settings) -> bool:
    try:
        keyring = DeploymentKeyring.load(settings.secrets)
        async with database.connect() as conn:
            usage = await DeploymentSecretUsageRepository(conn).count_key_versions_global()
        keyring.require_versions(usage)
        return True
    except SecretKeyringError:
        return False
    except Exception:
        return False
