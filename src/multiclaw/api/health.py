from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from multiclaw.cli import alembic_config
from multiclaw.secrets.keyring import DeploymentKeyring, SecretKeyringError
from multiclaw.security.redaction import redact
from multiclaw.storage.repositories.secrets import DeploymentSecretUsageRepository


router = APIRouter()


@router.get("/api/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/api/health/ready")
async def health_ready(request: Request):
    payload = await _build_readiness_payload(request)
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


@router.get("/health/ready", include_in_schema=False)
async def health_ready_alias():
    return RedirectResponse(url="/api/health/ready", status_code=307)


async def _build_readiness_payload(request: Request) -> dict[str, Any]:
    failed: list[str] = []
    details: dict[str, Any] = {}
    readiness = getattr(request.app.state, "sandbox_readiness", None)
    workspace_root = getattr(request.app.state, "workspace_root", None)
    database = getattr(request.app.state, "database", None)
    settings = getattr(request.app.state, "settings", None)

    if database is None or settings is None:
        failed.append("db_connectivity")
        return {"ready": False, "status": "not_ready", "checks_failed": failed}

    try:
        async with database.connect() as conn:
            backend_version = await conn.scalar(text("select sqlite_version()") if database.dialect.name == "sqlite" else text("select version()"))
            details["backend_name"] = database.dialect.name
            details["backend_version"] = str(backend_version)
            current_revision = await conn.run_sync(
                lambda sync_conn: MigrationContext.configure(sync_conn).get_current_revision()
            )
            expected_revision = ScriptDirectory.from_config(
                alembic_config(database_url=settings.database.url)
            ).get_current_head()
            if current_revision != expected_revision:
                failed.append("schema_revision")
            if database.dialect.name == "sqlite":
                fk_enabled = int(
                    await conn.scalar(text("PRAGMA foreign_keys"))
                )
                if fk_enabled != 1:
                    failed.append("sqlite_foreign_keys")
            else:
                time_zone = str(await conn.scalar(text("SELECT @@session.time_zone")))
                isolation = str(await conn.scalar(text("SELECT @@transaction_isolation")))
                if time_zone not in {"SYSTEM", "+00:00", "UTC"}:
                    failed.append("mysql_time_zone")
                if isolation.upper() != "READ-COMMITTED":
                    failed.append("mysql_isolation")
    except Exception:
        failed.append("db_connectivity")
        return {"ready": False, "status": "not_ready", "checks_failed": failed}

    try:
        if workspace_root is None or not Path(workspace_root).exists() or not os.access(workspace_root, os.R_OK | os.W_OK | os.X_OK):
            failed.append("workspace_root_permissions")
    except Exception:
        failed.append("workspace_root_permissions")

    try:
        keyring = DeploymentKeyring.load(settings.secrets)
        async with database.connect() as conn:
            usage = await DeploymentSecretUsageRepository(conn).count_key_versions_global()
        keyring.require_versions(usage)
    except SecretKeyringError:
        failed.append("keyring")
    except Exception:
        failed.append("keyring")

    if readiness is not None:
        details["sandbox"] = redact(readiness.model_dump(mode="json"))
        if not readiness.ready:
            failed.append("sandbox_readiness")

    return {
        "ready": not failed,
        "status": "ready" if not failed else "not_ready",
        "checks_failed": failed,
        **details,
    }
