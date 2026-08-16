import asyncio
from datetime import datetime, timedelta, timezone
import logging

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from alembic import command
from starlette.requests import Request

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.observability import OperationalMetrics, TraceEventSink, observability_scope
from multiclaw.storage import Database
from multiclaw.storage.uow import AuthUnitOfWork

TEST_JWT_SIGNING_KEY = "request-logging-jwt-key-material-1234567890"


def _sqlite_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"


async def _create_database(tmp_path) -> Database:
    database_url = _sqlite_url(tmp_path)
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")
    return Database.create(DatabaseSettings(driver="sqlite", url=database_url))


async def _seed_user(database: Database, email: str) -> tuple[str, int]:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(email)
        return user.id, user.auth_epoch


class _RecordHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _make_auth_cookie(database: Database, *, email: str = "test@example.com") -> dict:
    user_id, auth_epoch = asyncio.run(_seed_user(database, email))
    token = jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "aud": "multiclaw-api",
            "auth_epoch": auth_epoch,
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        TEST_JWT_SIGNING_KEY,
        algorithm="HS256",
    )
    return {"token": token}


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


def test_request_logging_records_public_route(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", TEST_JWT_SIGNING_KEY)
    database = asyncio.run(_create_database(tmp_path))
    from multiclaw.server import app

    caplog.set_level(logging.INFO, logger="multiclaw")
    handler = _RecordHandler()
    logger = logging.getLogger("multiclaw")
    logger.addHandler(handler)

    try:
        with TestClient(app) as client:
            response = client.get("/auth/me")
    finally:
        logger.removeHandler(handler)
        asyncio.run(database.dispose())

    assert response.status_code == 200
    assert any("HTTP GET /auth/me -> 200" in message for message in handler.messages)


def test_request_logging_records_chat_validation_failure(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", TEST_JWT_SIGNING_KEY)
    database = asyncio.run(_create_database(tmp_path))
    from multiclaw.server import app

    caplog.set_level(logging.INFO, logger="multiclaw")
    handler = _RecordHandler()
    logger = logging.getLogger("multiclaw")
    logger.addHandler(handler)

    try:
        with TestClient(app) as client:
            client.cookies = _make_auth_cookie(database)
            response = client.post("/api/chat", json={})
    finally:
        logger.removeHandler(handler)
        asyncio.run(database.dispose())

    assert response.status_code == 422
    assert any("HTTP POST /api/chat -> 422" in message for message in handler.messages)


def test_request_logging_does_not_leak_secret_canary(tmp_path, monkeypatch, caplog):
    secret_canary = "sk-request-log-canary-1234567890"
    hidden_path = tmp_path / "private" / ".env.secret"
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", TEST_JWT_SIGNING_KEY)
    database = asyncio.run(_create_database(tmp_path))
    from multiclaw.server import app

    caplog.set_level(logging.INFO, logger="multiclaw")
    handler = _RecordHandler()
    logger = logging.getLogger("multiclaw")
    logger.addHandler(handler)

    async def broken_stream(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(f"Authorization: Bearer {secret_canary} path={hidden_path}")
        yield

    try:
        with TestClient(app) as client:
            client.cookies = _make_auth_cookie(database)
            original_acquire = app.state.runtime_pool.acquire

            async def acquire_and_patch(context):
                runtime = await original_acquire(context)
                monkeypatch.setattr(runtime.agent, "handle_message_stream", broken_stream)
                return runtime

            monkeypatch.setattr(app.state.runtime_pool, "acquire", acquire_and_patch)
            response = client.post("/api/chat", json={"message": "hello"})
    finally:
        logger.removeHandler(handler)
        asyncio.run(database.dispose())

    assert response.status_code == 200
    assert secret_canary not in response.text
    assert str(hidden_path) not in response.text
    assert all(secret_canary not in message for message in handler.messages)
    assert all(str(hidden_path) not in message for message in handler.messages)


@pytest.mark.asyncio
async def test_log_http_requests_records_sqlite_busy_metric():
    import multiclaw.server as server

    metrics = OperationalMetrics()
    trace_sink = TraceEventSink()
    app = FastAPI()
    app.state.database = type("Database", (), {"dialect": type("Dialect", (), {"name": "sqlite"})()})()
    app.state.operational_metrics = metrics
    app.state.trace_sink = trace_sink
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/busy", "headers": []})

    async def fail(_request):
        raise RuntimeError("database is locked")

    with pytest.raises(RuntimeError, match="database is locked"):
        await server.log_http_requests(request, fail)

    assert any(metric_name == "multiclaw_sqlite_busy_total" for metric_name, _labels in metrics.counters)


@pytest.mark.asyncio
async def test_log_http_requests_records_mysql_lock_timeout_metric():
    import multiclaw.server as server

    metrics = OperationalMetrics()
    trace_sink = TraceEventSink()
    app = FastAPI()
    app.state.database = type("Database", (), {"dialect": type("Dialect", (), {"name": "mysql"})()})()
    app.state.operational_metrics = metrics
    app.state.trace_sink = trace_sink
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/lock-timeout", "headers": []})

    async def fail(_request):
        raise RuntimeError("Lock wait timeout exceeded; try restarting transaction")

    with pytest.raises(RuntimeError, match="Lock wait timeout exceeded"):
        await server.log_http_requests(request, fail)

    assert any(metric_name == "multiclaw_mysql_lock_timeout_total" for metric_name, _labels in metrics.counters)
