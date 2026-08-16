import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.schema import audit_logs
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext


TEST_JWT_SIGNING_KEY = "secret-redaction-jwt-key-material-1234567890"
SECRET_CANARY = "sk-secret-canary-1234567890"


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
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", TEST_JWT_SIGNING_KEY)
    database = asyncio.run(_create_database(tmp_path))
    try:
        yield database
    finally:
        asyncio.run(database.dispose())


@pytest.fixture(autouse=True)
def _csrf_test_defaults(monkeypatch: pytest.MonkeyPatch):
    original_request = TestClient.request

    def request_with_csrf(self, method, url, *args, **kwargs):
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Origin", "http://testserver")
            headers.setdefault("X-CSRF-Token", "test-csrf-token")
            kwargs["headers"] = headers
            self.cookies.set("csrf_token", "test-csrf-token")
        return original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "request", request_with_csrf)


class _RecordHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def _seed_identity(database: Database, *, email: str) -> tuple[dict[str, str], TenantContext]:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(email)
        assert user.default_workspace_id is not None
        token = jwt.encode(
            {
                "sub": user.id,
                "email": email,
                "auth_epoch": user.auth_epoch,
                "aud": "multiclaw-api",
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": datetime.now(timezone.utc) + timedelta(days=1),
            },
            TEST_JWT_SIGNING_KEY,
            algorithm="HS256",
        )
        return {"token": token}, TenantContext(user.id, user.default_workspace_id)


def test_redact_recursively_scrubs_secret_keys_strings_paths_and_bytes(tmp_path: Path):
    from multiclaw.security.redaction import redact

    payload = {
        "Authorization": f"Bearer {SECRET_CANARY}",
        "api_key": SECRET_CANARY,
        "nested": [
            {"password": "password-canary"},
            str(tmp_path / ".env.secret"),
            b"\x00\x01",
            ("refresh_token", SECRET_CANARY),
        ],
    }

    redacted = redact(payload)
    rendered = json.dumps(redacted, default=str)

    assert SECRET_CANARY not in rendered
    assert "password-canary" not in rendered
    assert str(tmp_path) not in rendered
    assert "[REDACTED]" in rendered
    assert "[BINARY REDACTED]" in rendered


@pytest.mark.asyncio
async def test_scoped_audit_logger_persists_only_redacted_detail(migrated_database: Database, tmp_path: Path):
    from multiclaw.governance.audit import ScopedAuditLogger

    cookie, context = await _seed_identity(migrated_database, email="audit@example.com")
    del cookie
    async with TenantUnitOfWork(migrated_database, context) as uow:
        session = await uow.sessions.create("Audit Session")
        scoped = context.for_session(session.id)
        await ScopedAuditLogger().record(
            uow.workflow,
            scoped,
            event_type="tool.error",
            status="error",
            tool_name="shell",
            detail={
                "authorization": f"Bearer {SECRET_CANARY}",
                "workspace_path": str(tmp_path / "private" / ".env"),
            },
        )
        row = (
            await uow.conn.execute(
                select(audit_logs.c.detail_redacted)
                .where(audit_logs.c.tenant_id == scoped.tenant_id)
                .order_by(audit_logs.c.created_at.desc())
                .limit(1)
            )
        ).scalar_one()

    assert SECRET_CANARY not in row
    assert str(tmp_path) not in row
    assert "[REDACTED]" in row


def test_operational_metrics_rejects_high_cardinality_or_forbidden_labels():
    from multiclaw.observability import InvalidMetricLabelError, OperationalMetrics

    metrics = OperationalMetrics()
    metrics.increment(
        "runtime_capacity_events",
        labels={"backend": "sqlite", "operation": "acquire", "status": "error"},
    )

    with pytest.raises(InvalidMetricLabelError):
        metrics.increment(
            "runtime_capacity_events",
            labels={"backend": "sqlite", "tenant_id": "tenant-a"},
        )

    with pytest.raises(InvalidMetricLabelError):
        metrics.increment(
            "runtime_capacity_events",
            labels={"backend": "sqlite", "path": "/tmp/private"},
        )


def test_chat_error_sse_and_logs_do_not_leak_secret_canary(
    migrated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import multiclaw.server as server

    cookie, _ = asyncio.run(_seed_identity(migrated_database, email="stream@example.com"))
    handler = _RecordHandler()
    logger = logging.getLogger("multiclaw")
    logger.addHandler(handler)

    async def broken_stream(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(
            f"Authorization: Bearer {SECRET_CANARY} workspace={tmp_path / 'private' / '.env'}"
        )
        yield

    try:
        with TestClient(server.app) as client:
            client.cookies = cookie
            original_acquire = server.app.state.runtime_pool.acquire

            async def acquire_and_patch(context):
                runtime = await original_acquire(context)
                monkeypatch.setattr(runtime.agent, "handle_message_stream", broken_stream)
                return runtime

            monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
            response = client.post("/api/chat", json={"message": "hello"})
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 200
    assert SECRET_CANARY not in response.text
    assert str(tmp_path) not in response.text
    assert all(SECRET_CANARY not in message for message in handler.messages)
    assert all(str(tmp_path) not in message for message in handler.messages)
