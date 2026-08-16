import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from alembic import command
from fastapi.testclient import TestClient

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.uow import AuthUnitOfWork

TEST_JWT_SIGNING_KEY = "chat-request-jwt-key-material-1234567890"


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"


async def _create_database(tmp_path: Path) -> Database:
    database_url = _sqlite_url(tmp_path)
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")
    return Database.create(DatabaseSettings(driver="sqlite", url=database_url))


async def _seed_user(database: Database, email: str) -> tuple[str, int]:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(email)
        return user.id, user.auth_epoch


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


def _make_auth_cookie(app, database: Database, *, email: str = "test@example.com") -> dict:
    user_id, auth_epoch = asyncio.run(_seed_user(database, email))
    token = jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "auth_epoch": auth_epoch,
            "aud": "multiclaw-api",
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


def test_chat_accepts_ai_sdk_message_shape(migrated_database, monkeypatch):
    import multiclaw.server as server

    captured: list[tuple[str, str | None]] = []

    async def fake_handle_message_stream(user_input: str, *, context):
        captured.append((user_input, context.session_id))
        yield {"type": "token", "content": "ok"}
        yield {"type": "done", "content": "ok"}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)

        response = client.post(
            "/api/chat",
            json={
                "trigger": "submit-message",
                "messages": [
                    {"role": "assistant", "content": "previous"},
                    {"role": "user", "content": "hello from sdk"},
                ],
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type":"data-session"' in response.text
    assert '"type":"text-delta"' in response.text
    assert captured[0][0] == "hello from sdk"


def test_chat_accepts_id_alias_for_existing_current_scope_session(migrated_database, monkeypatch):
    import multiclaw.server as server

    captured: list[tuple[str, str | None]] = []

    async def fake_handle_message_stream(user_input: str, *, context):
        captured.append((user_input, context.session_id))
        yield {"type": "done", "content": ""}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        created = client.post("/api/sessions", json={"title": "Alias Target"})
        assert created.status_code == 200
        session_id = created.json()["id"]

        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        before = client.get("/api/sessions").json()
        response = client.post(
            "/api/chat",
            json={
                "id": session_id,
                "messages": [{"role": "user", "content": "hello from alias"}],
            },
        )
        after = client.get("/api/sessions").json()

    assert response.status_code == 200
    assert '"type":"data-session"' in response.text
    assert captured == [("hello from alias", session_id)]
    assert [session["id"] for session in after] == [session["id"] for session in before]


def test_chat_accepts_text_parts_from_ai_sdk_messages(migrated_database, monkeypatch):
    import multiclaw.server as server

    captured: list[str] = []

    async def fake_handle_message_stream(user_input: str, *, context):
        del context
        captured.append(user_input)
        yield {"type": "done", "content": ""}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)

        response = client.post(
            "/api/chat",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "text", "text": " world"},
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert captured == ["hello world"]


def test_chat_stream_emits_step_boundaries_for_tool_rounds(migrated_database, monkeypatch):
    import multiclaw.server as server

    async def fake_handle_message_stream(user_input: str, *, context):
        del user_input, context
        yield {"type": "tool_call", "call_id": "call_1", "name": "web_search", "arguments": {"query": "DeepSeek jobs"}}
        yield {"type": "tool_result", "call_id": "call_1", "name": "web_search", "content": "Search results"}
        yield {"type": "token", "content": "Final summary"}
        yield {"type": "done", "content": "Final summary"}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)

        response = client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "latest DeepSeek jobs"}],
            },
        )

    assert response.status_code == 200
    assert response.text.count('"type":"start-step"') == 2
    assert response.text.count('"type":"finish-step"') == 2
    assert '"type":"tool-input-available"' in response.text
    assert '"type":"tool-output-available"' in response.text
