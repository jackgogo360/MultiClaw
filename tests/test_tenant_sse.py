import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from alembic import command
from fastapi.testclient import TestClient
from starlette.requests import Request

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.storage import Database
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext

TEST_JWT_SIGNING_KEY = "tenant-sse-jwt-key-material-1234567890"


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


def _decode_sse_messages(body: str) -> list[dict]:
    payloads: list[dict] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            payloads.append(json.loads(line[6:]))
    return payloads


@pytest.mark.asyncio
async def test_chat_streams_are_isolated_by_exact_run_scope(migrated_database, monkeypatch):
    import multiclaw.api.chat as chat_api
    import multiclaw.server as server
    from multiclaw.events import EventScope, ScopedEvent

    with TestClient(server.app):
        user_id, auth_epoch = await _seed_user(migrated_database, "tenant-sse@example.com")
        del auth_epoch
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            session = await uow.sessions.create(title="Shared Session")
        original_acquire = server.app.state.runtime_pool.acquire
        shared_runtime = await original_acquire(context.for_session(session.id))

        labeled_scopes: dict[str, EventScope] = {}
        streams_ready = asyncio.Event()
        first_run_finished = asyncio.Event()
        foreign_published = asyncio.Event()

        async def fake_handle_message_stream(user_input: str, *, context: TenantContext):
            assert context.session_id == session.id
            assert context.run_id is not None

            run_scope = EventScope.from_context(context)
            labeled_scopes[user_input] = run_scope
            if len(labeled_scopes) == 2:
                streams_ready.set()
            await streams_ready.wait()

            await shared_runtime.event_router.publish(
                ScopedEvent.from_scope(
                    run_scope,
                    "tool.completed",
                    {
                        "tool": "echo",
                        "call_id": f"call-{context.run_id}",
                    },
                )
            )

            if user_input == "first":
                yield {
                    "type": "tool_call",
                    "call_id": f"call-{context.run_id}",
                    "name": "echo",
                    "arguments": {"text": "first"},
                }
                yield {
                    "type": "tool_result",
                    "call_id": f"call-{context.run_id}",
                    "name": "echo",
                    "content": "first",
                }
                yield {"type": "done", "content": "first", "data": {}}
                return
            await first_run_finished.wait()
            await shared_runtime.event_router.publish(
                ScopedEvent.from_scope(
                    labeled_scopes["first"],
                    "tool.completed",
                    {
                        "tool": "echo",
                        "call_id": "foreign-after-first",
                    },
                )
            )
            foreign_published.set()
            yield {
                "type": "tool_call",
                "call_id": f"call-{context.run_id}",
                "name": "echo",
                "arguments": {"text": "second"},
            }
            yield {
                "type": "tool_result",
                "call_id": f"call-{context.run_id}",
                "name": "echo",
                "content": "second",
            }
            yield {"type": "done", "content": "second", "data": {}}

        async def acquire_shared_runtime(request_context: TenantContext):
            assert request_context.tenant_id == context.tenant_id
            assert request_context.workspace_id == context.workspace_id
            return shared_runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_shared_runtime)
        monkeypatch.setattr(shared_runtime.agent, "handle_message_stream", fake_handle_message_stream)

        request = Request(
            {
                "type": "http",
                "app": server.app,
                "method": "POST",
                "path": "/api/chat",
                "headers": [],
            }
        )

        async with TenantUnitOfWork(server.app.state.database, context) as uow_a:
            response_a = await chat_api.chat(
                chat_api.ChatRequest(message="first", session_id=session.id),
                request,
                context,
                uow_a,
            )
        async with TenantUnitOfWork(server.app.state.database, context) as uow_b:
            response_b = await chat_api.chat(
                chat_api.ChatRequest(message="second", session_id=session.id),
                request,
                context,
                uow_b,
            )

        async def consume(label: str, response) -> str:
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            if label == "first":
                first_run_finished.set()
            return "".join(chunks)

        body_b_task = asyncio.create_task(consume("second", response_b))
        body_a_task = asyncio.create_task(consume("first", response_a))
        await asyncio.wait_for(streams_ready.wait(), timeout=3)
        body_a, body_b = await asyncio.gather(body_a_task, body_b_task)

    payloads_a = _decode_sse_messages(body_a)
    payloads_b = _decode_sse_messages(body_b)
    run_meta_a = next(payload for payload in payloads_a if payload["type"] == "data-run")
    run_meta_b = next(payload for payload in payloads_b if payload["type"] == "data-run")
    scoped_a = [payload for payload in payloads_a if payload["type"] == "data-event"]
    scoped_b = [payload for payload in payloads_b if payload["type"] == "data-event"]
    tool_inputs_a = [payload for payload in payloads_a if payload["type"] == "tool-input-available"]
    tool_inputs_b = [payload for payload in payloads_b if payload["type"] == "tool-input-available"]

    assert run_meta_a["data"]["session_id"] == session.id
    assert run_meta_b["data"]["session_id"] == session.id
    assert run_meta_a["data"]["run_id"] != run_meta_b["data"]["run_id"]
    assert foreign_published.is_set() is True
    assert {payload["data"]["run_id"] for payload in scoped_a} == {run_meta_a["data"]["run_id"]}
    assert {payload["data"]["run_id"] for payload in scoped_b} == {run_meta_b["data"]["run_id"]}
    assert "foreign-after-first" not in body_a
    assert "foreign-after-first" not in body_b
    assert {payload["toolCallId"] for payload in tool_inputs_a}.isdisjoint(
        {payload["toolCallId"] for payload in tool_inputs_b}
    )
