import asyncio
import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import jwt
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from starlette.requests import Request

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings
from multiclaw.events import Event
from multiclaw.governance.sandbox.models import SandboxProbeResult, SandboxReadiness
from multiclaw.mcp.types import (
    HTTPServerConfig,
    InProcessServerConfig,
    SSEServerConfig,
    StdioServerConfig,
    WebSocketServerConfig,
)
from multiclaw.mcp.types import ToolInfo
from multiclaw.api.chat import iterate_message_stream
from multiclaw.storage import Database
from multiclaw.storage.schema import agent_runs, execution_checkpoints, memory_entries, tool_executions
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext, WorkspaceResolver
from multiclaw.workflow.models import (
    CheckpointPhase,
    RecoveryAction,
    RecoveryOutcome,
    RunLease,
    RunLeaseHandle,
    RunStatus,
)
from multiclaw.tools.code_exec import CodeExecToolBuilder
from multiclaw.tools.shell import ShellToolBuilder
from sandbox_fakes import ReadyRecordingSandboxController, UnavailableSandboxController

TEST_JWT_SIGNING_KEY = "test-jwt-signing-key-material-1234567890"


class _RecordHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


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


async def _checkpoint_rows(database: Database, context: TenantContext) -> list[dict[str, object]]:
    async with database.connect() as conn:
        result = await conn.execute(
            select(
                execution_checkpoints.c.phase,
                execution_checkpoints.c.checkpoint_seq,
                execution_checkpoints.c.payload_json,
            )
            .where(
                execution_checkpoints.c.tenant_id == context.tenant_id,
                execution_checkpoints.c.workspace_id == context.workspace_id,
                execution_checkpoints.c.session_id == context.session_id,
                execution_checkpoints.c.run_id == context.run_id,
            )
            .order_by(execution_checkpoints.c.checkpoint_seq.asc())
        )
        rows = result.mappings().all()
    return [
        {
            "phase": str(row["phase"]),
            "checkpoint_seq": int(row["checkpoint_seq"]),
            "payload": json.loads(str(row["payload_json"])),
        }
        for row in rows
    ]


async def _run_status(database: Database, context: TenantContext) -> str | None:
    async with database.connect() as conn:
        status = await conn.scalar(
            select(agent_runs.c.run_status).where(
                agent_runs.c.tenant_id == context.tenant_id,
                agent_runs.c.workspace_id == context.workspace_id,
                agent_runs.c.session_id == context.session_id,
                agent_runs.c.run_id == context.run_id,
            )
        )
    return None if status is None else str(status)


async def _assistant_chat_messages(database: Database, context: TenantContext) -> list[dict[str, object]]:
    async with database.connect() as conn:
        result = await conn.execute(
            select(
                memory_entries.c.id,
                memory_entries.c.role,
                memory_entries.c.content,
                memory_entries.c.created_at,
                memory_entries.c.turn_index,
            )
            .where(
                memory_entries.c.tenant_id == context.tenant_id,
                memory_entries.c.workspace_id == context.workspace_id,
                memory_entries.c.session_id == context.session_id,
                memory_entries.c.type == "chat_message",
                memory_entries.c.role == "assistant",
            )
            .order_by(
                memory_entries.c.created_at.asc(),
                memory_entries.c.turn_index.asc(),
                memory_entries.c.id.asc(),
            )
        )
        rows = result.mappings().all()
    return [
        {
            "id": str(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"]),
            "created_at": int(row["created_at"]),
            "turn_index": int(row["turn_index"]),
        }
        for row in rows
    ]


async def _expire_lease_with_db_clock(database: Database, context: TenantContext) -> None:
    async with database.write_transaction() as conn:
        await conn.execute(
            agent_runs.update()
            .where(
                agent_runs.c.tenant_id == context.tenant_id,
                agent_runs.c.workspace_id == context.workspace_id,
                agent_runs.c.session_id == context.session_id,
                agent_runs.c.run_id == context.run_id,
            )
            .values(lease_expires_at=database.dialect.db_now_ms() - 1)
        )


async def _latest_run_context(database: Database, context: TenantContext) -> TenantContext:
    async with database.connect() as conn:
        result = await conn.execute(
            select(
                agent_runs.c.session_id,
                agent_runs.c.run_id,
            )
            .where(
                agent_runs.c.tenant_id == context.tenant_id,
                agent_runs.c.workspace_id == context.workspace_id,
            )
            .order_by(agent_runs.c.created_at.desc(), agent_runs.c.run_id.desc())
            .limit(1)
        )
        row = result.mappings().one()
    return context.for_run(str(row["session_id"]), str(row["run_id"]))


class _StaticReportContextBuilder:
    def __init__(self, messages: list[dict[str, str]]) -> None:
        self.messages = messages

    async def build_with_report(self, _request):
        return SimpleNamespace(
            messages=self.messages,
            report=SimpleNamespace(
                used_tokens_by_level={"L0": 1, "L1": 0, "L2": 0},
                dropped_by_level={"L0": 0, "L1": 0, "L2": 0},
                limit_tokens=1000,
                reserved_response_tokens=0,
            ),
        )


class _QueuedStreamRouter:
    def __init__(self, stream_sequences, completion_responses=None) -> None:
        self.stream_sequences = list(stream_sequences)
        self.completion_responses = list(completion_responses or [])

    async def stream_completion(self, **_kwargs):
        events = self.stream_sequences.pop(0)
        for event in events:
            yield event

    async def completion(self, **_kwargs):
        if not self.completion_responses:
            raise AssertionError("unexpected completion call")
        response = self.completion_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _decode_sse_messages(body: str) -> list[dict]:
    payloads: list[dict] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            payloads.append(json.loads(line[6:]))
    return payloads


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
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", TEST_JWT_SIGNING_KEY)
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


@pytest.mark.asyncio
async def test_iterate_message_stream_passes_lease_surfaces_to_kwargs_handler():
    context = TenantContext("tenant-a", "workspace-a", session_id="session-a", run_id="run-a")
    lease = RunLease(
        context=context,
        lease_owner="runtime-1",
        fencing_token=1,
        version=1,
        lease_expires_at=12345,
    )
    lease_handle = RunLeaseHandle(lease)
    workflow_continuation = object()
    captured: dict[str, object] = {}

    async def handler(user_input: str, **kwargs):
        captured["user_input"] = user_input
        captured.update(kwargs)
        yield {"type": "done", "content": ""}

    items = [
        item
        async for item in iterate_message_stream(
            handler,
            "hello",
            context=context,
            run_lease=lease,
            run_lease_handle=lease_handle,
            workflow_continuation=workflow_continuation,
        )
    ]

    assert items == [{"type": "done", "content": ""}]
    assert captured["user_input"] == "hello"
    assert captured["context"] == context
    assert captured["run_lease"] == lease
    assert captured["run_lease_handle"] is lease_handle
    assert captured["workflow_continuation"] is workflow_continuation


def _make_runtime_factory(
    server_module,
    tmp_path: Path,
    *,
    controller_factory=None,
    mcp_manager_factory=None,
):
    database = Database.create(DatabaseSettings(driver="sqlite", url=_sqlite_url(tmp_path)))
    return server_module.create_runtime_factory(
        database=database,
        workspace_resolver=WorkspaceResolver(tmp_path),
        sandbox_controller_factory=controller_factory,
        mcp_manager_factory=mcp_manager_factory or server_module.MCPClientManager,
    )


def _create_runtime(factory, tenant_id: str = "tenant-a", workspace_id: str = "workspace-a"):
    return asyncio.run(factory.create(TenantContext(tenant_id, workspace_id)))


def test_sessions_endpoint_lists_created_sessions(migrated_database):
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app, migrated_database)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        listed = client.get("/api/sessions").json()

    assert created["title"] == "Alpha"
    assert [session["id"] for session in listed] == [created["id"]]


def test_session_lifecycle_endpoints(migrated_database):
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app, migrated_database)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        renamed = client.patch(
            f"/api/sessions/{created['id']}",
            json={"title": "Beta"},
        ).json()
        archived = client.post(f"/api/sessions/{created['id']}/archive").json()
        listed = client.get("/api/sessions").json()
        all_sessions = client.get("/api/sessions?include_archived=true").json()
        restored = client.post(f"/api/sessions/{created['id']}/restore").json()

    assert renamed["title"] == "Beta"
    assert archived["status"] == "archived"
    assert listed == []
    assert [session["id"] for session in all_sessions] == [created["id"]]
    assert restored["status"] == "active"


def test_chat_without_session_emits_session_event(migrated_database):
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app, migrated_database)
        response = client.post("/api/chat", json={"message": "hello"})

    body = response.text
    assert '"type":"data-session"' in body
    assert '"type":"data-run"' in body
    assert '"id":"' in body


def test_chat_returns_retryable_503_when_runtime_pool_is_at_capacity(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.runtime.pool import RuntimeCapacityError

    async def fail_acquire(context):
        del context
        raise RuntimeCapacityError(7)

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", fail_acquire)
        response = client.post("/api/chat", json={"message": "hello"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "7"
    assert response.json() == {"detail": "runtime temporarily unavailable"}


def test_approve_does_not_acquire_runtime_pool(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.runtime.pool import RuntimeCapacityError

    async def fail_acquire(context):
        del context
        raise RuntimeCapacityError(7)

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", fail_acquire)
        response = client.post(
            "/api/approve",
            json={"approval_id": "missing-approval", "approved": True, "version": 1},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "approval not found"}


def test_chat_real_memory_path_does_not_lock_sqlite(migrated_database, monkeypatch):
    import multiclaw.server as server

    async def fake_stream_completion(*, model, messages, tools):
        del model, messages, tools
        yield {"type": "token", "content": "assistant reply"}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent.router, "stream_completion", fake_stream_completion)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)

        response = client.post("/api/chat", json={"message": "hello from user"})
        sessions = client.get("/api/sessions").json()
        session_id = sessions[0]["id"]
        messages = client.get(f"/api/sessions/{session_id}/messages").json()

    assert response.status_code == 200
    assert "database is locked" not in response.text
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert [message["content"] for message in messages] == ["hello from user", "assistant reply"]


def test_server_module_no_longer_exports_process_global_runtime_symbols():
    import multiclaw.server as server_module

    assert not hasattr(server_module, "agent")
    assert not hasattr(server_module, "shared_bus")


def test_chat_rejects_archived_session(migrated_database):
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app, migrated_database)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        client.post(f"/api/sessions/{created['id']}/archive")
        response = client.post(
            "/api/chat",
            json={"message": "hello", "session_id": created["id"]},
        )

    assert response.status_code == 409


def test_delete_session_endpoint(migrated_database):
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app, migrated_database)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        response = client.delete(f"/api/sessions/{created['id']}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    # Verify session is gone
    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app, migrated_database)
        listed = client.get("/api/sessions").json()
    assert created["id"] not in [s["id"] for s in listed]


def test_get_messages_endpoint_returns_empty_for_new_session(migrated_database):
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app, migrated_database)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        sid = created["id"]

        response = client.get(f"/api/sessions/{sid}/messages")
        assert response.status_code == 200
        assert response.json() == []


def test_get_messages_endpoint_respects_limit_param(migrated_database):
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app, migrated_database)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        sid = created["id"]

        response = client.get(f"/api/sessions/{sid}/messages?limit=10")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


def test_unauthenticated_requests_return_401(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        response = client.get("/api/sessions")
        assert response.status_code == 401


def test_auth_flows_are_public(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        # These routes should be accessible without auth
        assert client.get("/auth/me").status_code != 401
        assert client.get("/").status_code != 401
        assert client.get("/multiclaw.png").status_code != 401


def test_assets_subtree_is_public_but_similar_prefix_is_not(migrated_database):
    del migrated_database
    from multiclaw.server import app

    with TestClient(app) as client:
        index_response = client.get("/")
        assert index_response.status_code == 200
        match = re.search(r'(?:src|href)="(/assets/[^"]+)"', index_response.text)
        assert match is not None
        asset_path = match.group(1)

        asset_response = client.get(asset_path)
        evil_response = client.get("/assets-evil")

    assert asset_response.status_code == 200
    assert asset_response.headers["content-type"].startswith(
        ("application/javascript", "text/javascript", "text/css")
    )
    assert evil_response.status_code == 401


def test_lifespan_worker_tolerates_missing_workflow_tables_for_public_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        assert client.get("/auth/me").status_code != 500


class _LeakyUnavailableSandboxController:
    def __init__(self, workspace_root: Path) -> None:
        secret_value = "secret-dummy-value"
        password_value = "password-dummy-value"
        bearer_value = "bearer-dummy-value"
        private_root = workspace_root / "private-root"
        hidden_path = workspace_root / ".env.secret"
        self._events = (
            Event(
                type="sandbox.profile_unavailable",
                data={
                    "profile_name": "shell_workspace",
                    "reason": (
                        f"workspace={workspace_root} private_root={private_root} "
                        f"hidden={hidden_path} client_secret={secret_value} "
                        f"PASSWORD = {password_value} Authorization: Bearer {bearer_value}"
                    ),
                },
            ),
        )
        self._readiness = SandboxReadiness(
            ready=False,
            mode="auto",
            backend_name="leaky",
            probe=SandboxProbeResult(
                backend_name="leaky",
                available=False,
                capabilities={},
                reason=(
                    f"workspace={workspace_root} private_root={private_root} "
                    f"hidden={hidden_path} client_secret={secret_value} "
                    f"PASSWORD = {password_value} Authorization: Bearer {bearer_value}"
                ),
            ),
            profiles={
                "shell_workspace": False,
                "code_exec_python": False,
                "mcp_stdio_local": False,
            },
            skipped_capabilities={
                "shell": (
                    f"workspace={workspace_root} private_root={private_root} "
                    f"hidden={hidden_path} client_secret={secret_value} "
                    f"PASSWORD = {password_value} Authorization: Bearer {bearer_value}"
                )
            },
            unsafe_fallback_active=False,
        )

    @property
    def mode(self) -> str:
        return self._readiness.mode

    @property
    def backend_name(self) -> str:
        return self._readiness.backend_name

    @property
    def readiness(self) -> SandboxReadiness:
        return self._readiness

    def initialize(self) -> None:
        return None

    def is_profile_ready(self, profile_name: str) -> bool:
        del profile_name
        return False

    def build_launch_spec(self, request):
        del request
        raise RuntimeError("unavailable")

    async def run(self, request):
        del request
        raise RuntimeError("unavailable")

    def record_blocked_capability(self, name: str, reason: str) -> None:
        del name, reason
        return None

    def finalize_readiness(self) -> SandboxReadiness:
        return self._readiness

    def drain_startup_events(self) -> tuple[Event, ...]:
        events = self._events
        self._events = ()
        return events

    def close(self) -> None:
        return None


def test_build_mcp_adapters_respects_filter_and_sanitized_namespace():
    from multiclaw.mcp.tool_adapter import MCPToolBuilder
    from multiclaw.server import _build_mcp_adapters, _mcp_namespace_prefix

    tools = [
        ToolInfo(
            name="mcp__ignored__read_file",
            server_name="demo server/v1",
            original_name="read file/v2",
            description="read",
            input_schema={},
        ),
        ToolInfo(
            name="mcp__ignored__write_file",
            server_name="demo server/v1",
            original_name="write_file",
            description="write",
            input_schema={},
        ),
        ToolInfo(
            name="mcp__ignored__delete_file",
            server_name="demo server/v1",
            original_name="delete_file",
            description="delete",
            input_schema={},
        ),
    ]

    adapters = _build_mcp_adapters(
        server_name="demo server/v1",
        tools=tools,
        manager=object(),
        tool_filter={"include": ["read", "write"], "exclude": ["write"]},
    )

    assert _mcp_namespace_prefix("demo server/v1") == "mcp__demo_server_v1__"
    assert len(adapters) == 1
    assert isinstance(adapters[0], MCPToolBuilder)
    assert adapters[0].name == "mcp__demo_server_v1__read_file_v2"


def test_health_ready_is_public_and_uses_app_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")

    import multiclaw.server as server_module

    controller = ReadyRecordingSandboxController(workspace_root=tmp_path)
    factory = _make_runtime_factory(
        server_module,
        tmp_path,
        controller_factory=lambda workspace_root, event_bus: controller,
    )
    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: factory)

    with TestClient(server_module.app) as client:
        response = client.get("/health/ready")

        assert response.status_code == 200
        assert response.json()["ready"] is True
        assert server_module.app.state.sandbox_readiness.ready is True
        assert server_module.app.state.sandbox_readiness is controller.finalize_readiness()


def test_health_ready_redacts_sensitive_readiness_details(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")

    import multiclaw.server as server_module

    controller = _LeakyUnavailableSandboxController(tmp_path)
    factory = _make_runtime_factory(
        server_module,
        tmp_path,
        controller_factory=lambda workspace_root, event_bus: controller,
    )
    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: factory)

    with caplog.at_level("INFO"):
        with TestClient(server_module.app) as client:
            response = client.get("/health/ready")

        assert response.status_code == 503
        assert response.json()["ready"] is False
        assert server_module.app.state.sandbox_readiness.ready is False
        assert str(tmp_path) not in response.text
        assert str(tmp_path / "private-root") not in response.text
        assert str(tmp_path / ".env.secret") not in response.text
        assert "secret-dummy-value" not in response.text
        assert "password-dummy-value" not in response.text
        assert "bearer-dummy-value" not in response.text
        assert "secret-dummy-value" not in caplog.text
        assert "password-dummy-value" not in caplog.text
        assert "bearer-dummy-value" not in caplog.text


@pytest.mark.asyncio
async def test_health_ready_reads_request_app_state_and_sanitizes_response(tmp_path):
    import multiclaw.server as server_module
    from fastapi import FastAPI

    app = FastAPI()
    controller = _LeakyUnavailableSandboxController(tmp_path)
    app.state.sandbox_readiness = controller.finalize_readiness()
    app.state.workspace_root = tmp_path
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/health/ready", "headers": []})

    response = await server_module.health_ready(request)

    assert response.status_code == 503
    body = response.body.decode()
    assert str(tmp_path) not in body
    assert "secret-dummy-value" not in body
    assert "password-dummy-value" not in body
    assert "bearer-dummy-value" not in body
    assert app.state.sandbox_readiness is controller.finalize_readiness()


@pytest.mark.parametrize(
    "reason",
    [
        "client_secret=secret-dummy-value",
        "PASSWORD = password-dummy-value",
        "OPENAI_API_KEY=api-dummy-value",
        "Authorization: Bearer bearer-dummy-value",
        "Bearer bearer-dummy-value",
    ],
)
def test_sanitize_public_reason_redacts_assignments_and_auth_values(reason, tmp_path):
    from multiclaw.server import _sanitize_public_reason

    sanitized = _sanitize_public_reason(reason, workspace_root=tmp_path)

    assert "secret-dummy-value" not in sanitized
    assert "password-dummy-value" not in sanitized
    assert "api-dummy-value" not in sanitized
    assert "bearer-dummy-value" not in sanitized
    assert "[REDACTED]" in sanitized


def test_register_mcp_tools_installs_refresh_callback_before_connect(monkeypatch, tmp_path):
    from multiclaw.mcp.types import ServerState, ServerStatus
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    stale_tool = ToolInfo(
        name="mcp__ignored__read_stale",
        server_name="demo server/v1",
        original_name="read_stale",
        description="stale",
        input_schema={},
    )
    refreshed_tool = ToolInfo(
        name="mcp__ignored__read_fresh",
        server_name="demo server/v1",
        original_name="read_fresh",
        description="fresh",
        input_schema={},
    )

    events: list[str] = []

    class FakeManager:
        def __init__(self) -> None:
            self._callback = None
            self._latest_states = {}

        def set_tools_changed_callback(self, callback) -> None:
            events.append("set_callback")
            self._callback = callback

        def connect_servers(self, configs):
            events.append("connect")
            stale_state = ServerState(
                name="demo server/v1",
                config=object(),
                status=ServerStatus.CONNECTED,
                tools=[stale_tool],
            )
            if self._callback is not None:
                events.append("callback")
                self._callback("demo server/v1", [refreshed_tool])
            self._latest_states = {
                "demo server/v1": ServerState(
                    name="demo server/v1",
                    config=object(),
                    status=ServerStatus.CONNECTED,
                    tools=[refreshed_tool],
                )
            }
            return {"demo server/v1": stale_state}

        def get_server_states(self):
            events.append("get_server_states")
            return dict(self._latest_states)

    monkeypatch.setattr(
        "multiclaw.server.load_mcp_config",
        lambda path=None: {"demo server/v1": StdioServerConfig(command="echo")},
    )
    monkeypatch.setattr(
        "multiclaw.server.load_mcp_tools_config",
        lambda path=None: {"demo server/v1": {"include": ["read"], "exclude": []}},
    )

    registry = ToolRegistry()
    _register_mcp_tools(
        registry=registry,
        mcp_manager=FakeManager(),
        config_path=None,
        sandbox_controller=ReadyRecordingSandboxController(workspace_root=tmp_path),
        workspace_root=tmp_path,
        mcp_profile_name="mcp_stdio_local",
    )

    assert events == ["set_callback", "connect", "callback", "get_server_states"]
    assert [builder.name for builder in registry.list_all()] == [
        "mcp__demo_server_v1__read_fresh",
    ]


def test_register_mcp_tools_passes_workspace_root_to_tool_filter_loader(monkeypatch, tmp_path):
    from multiclaw.mcp.types import ServerState, ServerStatus
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    captured: dict[str, object] = {}

    class FakeManager:
        def set_tools_changed_callback(self, callback) -> None:
            captured["callback"] = callback

        def connect_servers(self, configs):
            captured["configs"] = configs
            return {
                "demo": ServerState(
                    name="demo",
                    config=object(),
                    status=ServerStatus.CONNECTED,
                    tools=[],
                )
            }

        def get_server_states(self):
            return {
                "demo": ServerState(
                    name="demo",
                    config=object(),
                    status=ServerStatus.CONNECTED,
                    tools=[],
                )
            }

    monkeypatch.setattr(
        "multiclaw.server.load_mcp_config",
        lambda path=None: {"demo": StdioServerConfig(command="echo")},
    )

    def fake_load_mcp_tools_config(path=None, *, search_parents=True, workspace_root=None):
        captured["path"] = path
        captured["search_parents"] = search_parents
        captured["workspace_root"] = workspace_root
        return {}

    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", fake_load_mcp_tools_config)

    workspace_root = tmp_path / "."
    _register_mcp_tools(
        registry=ToolRegistry(),
        mcp_manager=FakeManager(),
        config_path=None,
        sandbox_controller=ReadyRecordingSandboxController(workspace_root=tmp_path),
        workspace_root=workspace_root.resolve(),
        mcp_profile_name="mcp_stdio_local",
    )

    assert captured["path"] is None
    assert captured["search_parents"] is True
    assert captured["workspace_root"] == workspace_root.resolve()


def test_register_mcp_tools_skips_unready_stdio_but_keeps_remote(
    tmp_path,
    monkeypatch,
):
    from multiclaw.mcp.types import ServerState, ServerStatus
    from multiclaw.server import _register_mcp_tools, logger as server_logger
    from multiclaw.tools.registry import ToolRegistry

    remote_tool = ToolInfo(
        name="mcp__remote__search",
        server_name="remote",
        original_name="search",
        description="search",
        input_schema={},
    )

    class FakeManager:
        def __init__(self) -> None:
            self.connected = None
            self._states = {}

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connected = dict(configs)
            self._states = {
                name: ServerState(
                    name=name,
                    config=config,
                    status=ServerStatus.CONNECTED,
                    tools=[remote_tool] if name == "remote" else [],
                )
                for name, config in configs.items()
            }

        def get_server_states(self):
            return dict(self._states)

    monkeypatch.setattr(
        "multiclaw.server.load_mcp_config",
        lambda path=None: {
            "local": StdioServerConfig(command="python", args=["-m", "demo"]),
            "remote": HTTPServerConfig(url="https://example.com/mcp"),
        },
    )
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    info_calls: list[str] = []
    real_info = server_logger.info

    def _info(message, *args, **kwargs):
        info_calls.append(message % args if args else message)
        return real_info(message, *args, **kwargs)

    monkeypatch.setattr(server_logger, "info", _info)

    manager = FakeManager()
    registry = ToolRegistry()
    _register_mcp_tools(
        registry=registry,
        mcp_manager=manager,
        config_path=None,
        sandbox_controller=UnavailableSandboxController(),
        workspace_root=tmp_path,
        mcp_profile_name="mcp_stdio_local",
    )

    assert list(manager.connected) == ["remote"]
    assert [builder.name for builder in registry.list_all()] == ["mcp__remote__search"]
    assert any("transport_remote_unsandboxed=true" in message for message in info_calls)


def test_register_mcp_tools_skips_conservative_untrusted_stdio_before_connect(
    tmp_path,
    monkeypatch,
    caplog,
):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class FakeManager:
        def __init__(self) -> None:
            self.connected = None
            self.connect_calls = 0

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connect_calls += 1
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    config = StdioServerConfig(command="/usr/bin/env")
    config.config_trust = "workspace_untrusted"
    config.config_source = "auto_workspace"

    monkeypatch.setattr("multiclaw.server.load_mcp_config", lambda path=None, **kwargs: {"local": config})
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    controller = ReadyRecordingSandboxController(workspace_root=tmp_path)
    manager = FakeManager()
    with caplog.at_level("WARNING", logger="multiclaw"):
        _register_mcp_tools(
            registry=ToolRegistry(),
            mcp_manager=manager,
            config_path=None,
            sandbox_controller=controller,
            workspace_root=tmp_path,
            mcp_profile_name="mcp_stdio_local",
        )

    assert manager.connect_calls == 1
    assert manager.connected == {}
    assert controller.requests == []
    events = controller.drain_startup_events()
    assert any(event.type == "sandbox.registration_skipped" for event in events)
    assert "/usr/bin/env" not in caplog.text


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("sandbox_network", "inherit"),
        ("sandbox_workspace", "rw"),
        ("sandbox_allow_subprocesses", True),
        ("sandbox_env_allowlist", ["API_TOKEN"]),
        ("sandbox_read_only_paths", [Path("/opt/example")]),
    ],
)
def test_register_mcp_tools_rejects_untrusted_stdio_privilege_knobs_before_connect(
    tmp_path,
    monkeypatch,
    caplog,
    field_name,
    field_value,
):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class FakeManager:
        def __init__(self) -> None:
            self.connected = None
            self.connect_calls = 0

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connect_calls += 1
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    config = StdioServerConfig(command="/usr/bin/env")
    config.config_trust = "workspace_untrusted"
    config.config_source = "auto_workspace"
    setattr(config, field_name, field_value)

    monkeypatch.setattr("multiclaw.server.load_mcp_config", lambda path=None, **kwargs: {"local": config})
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    controller = ReadyRecordingSandboxController(workspace_root=tmp_path)
    manager = FakeManager()
    with caplog.at_level("WARNING"):
        _register_mcp_tools(
            registry=ToolRegistry(),
            mcp_manager=manager,
            config_path=None,
            sandbox_controller=controller,
            workspace_root=tmp_path,
            mcp_profile_name="mcp_stdio_local",
        )

    assert manager.connect_calls == 1
    assert manager.connected == {}
    assert controller.requests == []
    events = controller.drain_startup_events()
    assert any(event.type == "sandbox.registration_skipped" for event in events)
    assert str(tmp_path) not in caplog.text


def test_register_mcp_tools_rejects_untrusted_in_process_even_in_unsafe_mode(
    tmp_path,
    monkeypatch,
):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class FakeManager:
        def __init__(self) -> None:
            self.connected = None

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    config = InProcessServerConfig(server_factory=lambda: object())
    config.config_trust = "workspace_untrusted"
    config.config_source = "auto_workspace"

    monkeypatch.setattr("multiclaw.server.load_mcp_config", lambda path=None, **kwargs: {"local-inproc": config})
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    controller = ReadyRecordingSandboxController(
        workspace_root=tmp_path,
        mode="host_unsafe_dev_only",
    )
    manager = FakeManager()
    _register_mcp_tools(
        registry=ToolRegistry(),
        mcp_manager=manager,
        config_path=None,
        sandbox_controller=controller,
        workspace_root=tmp_path,
        mcp_profile_name="mcp_stdio_local",
    )

    assert manager.connected == {}
    events = controller.drain_startup_events()
    assert any(event.type == "sandbox.registration_skipped" for event in events)
    assert all(event.type != "sandbox.unsafe_fallback_used" for event in events)


@pytest.mark.parametrize(
    ("config_factory", "server_name"),
    [
        (lambda: HTTPServerConfig(url="https://example.com/mcp"), "remote-http"),
        (lambda: SSEServerConfig(url="https://example.com/sse"), "remote-sse"),
        (lambda: WebSocketServerConfig(url="wss://example.com/ws"), "remote-ws"),
    ],
)
def test_register_mcp_tools_skips_untrusted_remote_configs_before_connect(
    tmp_path,
    monkeypatch,
    caplog,
    config_factory,
    server_name,
):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class FakeManager:
        def __init__(self) -> None:
            self.connected = None
            self.connect_calls = 0

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connect_calls += 1
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    config = config_factory()
    config.config_trust = "workspace_untrusted"
    config.config_source = "auto_workspace"

    monkeypatch.setattr("multiclaw.server.load_mcp_config", lambda path=None, **kwargs: {server_name: config})
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    manager = FakeManager()
    controller = ReadyRecordingSandboxController(workspace_root=tmp_path)
    with caplog.at_level("WARNING"):
        _register_mcp_tools(
            registry=ToolRegistry(),
            mcp_manager=manager,
            config_path=None,
            sandbox_controller=controller,
            workspace_root=tmp_path,
            mcp_profile_name="mcp_stdio_local",
        )

    assert manager.connect_calls == 1
    assert manager.connected == {}
    events = controller.drain_startup_events()
    assert any(event.type == "sandbox.registration_skipped" for event in events)
    assert "example.com" not in caplog.text


def test_register_mcp_tools_skips_untrusted_unknown_transport_before_connect(
    tmp_path,
    monkeypatch,
    caplog,
):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class FutureConfig:
        config_trust = "workspace_untrusted"
        config_source = "auto_workspace"

    class FakeManager:
        def __init__(self) -> None:
            self.connected = None
            self.connect_calls = 0

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connect_calls += 1
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    config = FutureConfig()
    monkeypatch.setattr("multiclaw.server.load_mcp_config", lambda path=None, **kwargs: {"future": config})
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    manager = FakeManager()
    controller = ReadyRecordingSandboxController(workspace_root=tmp_path)
    with caplog.at_level("WARNING"):
        _register_mcp_tools(
            registry=ToolRegistry(),
            mcp_manager=manager,
            config_path=None,
            sandbox_controller=controller,
            workspace_root=tmp_path,
            mcp_profile_name="mcp_stdio_local",
        )

    assert manager.connect_calls == 1
    assert manager.connected == {}
    events = controller.drain_startup_events()
    assert any(
        event.type == "sandbox.registration_skipped"
        and event.data["capability"].startswith("mcp_unknown_future_")
        for event in events
    )
    assert "FutureConfig" not in caplog.text


def test_register_mcp_tools_skips_in_process_in_auto_mode(tmp_path, monkeypatch):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class FakeManager:
        def __init__(self) -> None:
            self.connected = None

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    monkeypatch.setattr(
        "multiclaw.server.load_mcp_config",
        lambda path=None: {
            "local-inproc": InProcessServerConfig(server_factory=lambda: object()),
        },
    )
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    controller = ReadyRecordingSandboxController(workspace_root=tmp_path)
    manager = FakeManager()
    registry = ToolRegistry()
    _register_mcp_tools(
        registry=registry,
        mcp_manager=manager,
        config_path=None,
        sandbox_controller=controller,
        workspace_root=tmp_path,
        mcp_profile_name="mcp_stdio_local",
    )

    assert manager.connected == {}
    assert registry.list_all() == []
    events = controller.drain_startup_events()
    assert any(
        event.type == "sandbox.registration_skipped"
        and event.data["capability"].startswith("mcp_in_process_local-inproc_")
        for event in events
    )


def test_register_mcp_tools_keeps_in_process_in_unsafe_mode(tmp_path, monkeypatch):
    from multiclaw.mcp.types import ServerState, ServerStatus
    from multiclaw.server import _register_mcp_tools, logger as server_logger
    from multiclaw.tools.registry import ToolRegistry

    inproc_tool = ToolInfo(
        name="mcp__local_inproc__stat",
        server_name="local-inproc",
        original_name="stat",
        description="stat",
        input_schema={},
    )

    class FakeManager:
        def __init__(self) -> None:
            self.connected = None
            self._states = {}

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connected = dict(configs)
            self._states = {
                name: ServerState(
                    name=name,
                    config=config,
                    status=ServerStatus.CONNECTED,
                    tools=[inproc_tool],
                )
                for name, config in configs.items()
            }

        def get_server_states(self):
            return dict(self._states)

    monkeypatch.setattr(
        "multiclaw.server.load_mcp_config",
        lambda path=None: {
            "local-inproc": InProcessServerConfig(server_factory=lambda: object()),
        },
    )
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    controller = ReadyRecordingSandboxController(
        workspace_root=tmp_path,
        mode="host_unsafe_dev_only",
    )
    warning_calls: list[str] = []
    real_warning = server_logger.warning

    def _warning(message, *args, **kwargs):
        warning_calls.append(message % args if args else message)
        return real_warning(message, *args, **kwargs)

    monkeypatch.setattr(server_logger, "warning", _warning)

    manager = FakeManager()
    registry = ToolRegistry()
    _register_mcp_tools(
        registry=registry,
        mcp_manager=manager,
        config_path=None,
        sandbox_controller=controller,
        workspace_root=tmp_path,
        mcp_profile_name="mcp_stdio_local",
    )

    assert list(manager.connected) == ["local-inproc"]
    assert [builder.name for builder in registry.list_all()] == [
        "mcp__local-inproc__stat",
    ]
    assert any("unsafe" in message for message in warning_calls)
    assert controller.readiness.ready is True
    events = controller.drain_startup_events()
    assert [event.type for event in events] == ["sandbox.unsafe_fallback_used"]
    assert events[0].data["scope"] == "capability"
    assert events[0].data["capability"].startswith("mcp_in_process_local-inproc_")
    assert "unsafe" in events[0].data["reason"]


def test_register_mcp_tools_propagates_blocked_capability_record_errors(tmp_path, monkeypatch):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class RaisingController(ReadyRecordingSandboxController):
        def is_profile_ready(self, profile_name: str) -> bool:
            if profile_name == "mcp_stdio_local":
                return False
            return super().is_profile_ready(profile_name)

        def record_blocked_capability(self, name: str, reason: str) -> None:
            raise RuntimeError(f"record failed for {name}: {reason}")

    class FakeManager:
        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    monkeypatch.setattr(
        "multiclaw.server.load_mcp_config",
        lambda path=None: {
            "local": StdioServerConfig(command="python", args=["-m", "demo"]),
        },
    )
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    with pytest.raises(RuntimeError, match="record failed"):
        _register_mcp_tools(
            registry=ToolRegistry(),
            mcp_manager=FakeManager(),
            config_path=None,
            sandbox_controller=RaisingController(workspace_root=tmp_path),
            workspace_root=tmp_path,
            mcp_profile_name="mcp_stdio_local",
        )


def test_register_mcp_tools_requires_sandbox_controller_for_local_transports(tmp_path, monkeypatch):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class FakeManager:
        def __init__(self) -> None:
            self.connect_calls = 0

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connect_calls += 1
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    monkeypatch.setattr(
        "multiclaw.server.load_mcp_config",
        lambda path=None: {
            "local": StdioServerConfig(command="python", args=["-m", "demo"]),
        },
    )
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    manager = FakeManager()
    with pytest.raises(RuntimeError, match="sandbox controller is required"):
        _register_mcp_tools(
            registry=ToolRegistry(),
            mcp_manager=manager,
            config_path=None,
            sandbox_controller=None,
            workspace_root=tmp_path,
            mcp_profile_name="mcp_stdio_local",
        )

    assert manager.connect_calls == 0


def test_register_mcp_tools_uses_configured_mcp_profile_name(tmp_path, monkeypatch):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class ProfileController(ReadyRecordingSandboxController):
        def is_profile_ready(self, profile_name: str) -> bool:
            return profile_name == "custom_mcp_profile"

    class FakeManager:
        def __init__(self) -> None:
            self.connected = None
            self._states = {}

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    monkeypatch.setattr(
        "multiclaw.server.load_mcp_config",
        lambda path=None: {
            "local": StdioServerConfig(command="python", args=["-m", "demo"]),
        },
    )
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    manager = FakeManager()
    _register_mcp_tools(
        registry=ToolRegistry(),
        mcp_manager=manager,
        config_path=None,
        sandbox_controller=ProfileController(workspace_root=tmp_path),
        workspace_root=tmp_path,
        mcp_profile_name="custom_mcp_profile",
    )

    assert list(manager.connected) == ["local"]


def test_register_mcp_tools_keeps_distinct_blocked_capabilities_for_colliding_server_names(
    tmp_path,
    monkeypatch,
):
    from multiclaw.server import _register_mcp_tools
    from multiclaw.tools.registry import ToolRegistry

    class CollisionController(ReadyRecordingSandboxController):
        def __init__(self) -> None:
            super().__init__(workspace_root=tmp_path)
            self._blocked: dict[str, str] = {}

        def is_profile_ready(self, profile_name: str) -> bool:
            del profile_name
            return False

        def record_blocked_capability(self, name: str, reason: str) -> None:
            self._blocked[name] = reason
            super().record_blocked_capability(name, reason)

        def finalize_readiness(self) -> SandboxReadiness:
            return self.readiness.model_copy(update={"skipped_capabilities": dict(self._blocked)})

    class FakeManager:
        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connected = dict(configs)

        def get_server_states(self):
            return {}

    monkeypatch.setattr(
        "multiclaw.server.load_mcp_config",
        lambda path=None: {
            "a b": StdioServerConfig(command="python", args=["-m", "demo"]),
            "a/b": StdioServerConfig(command="python", args=["-m", "demo"]),
        },
    )
    monkeypatch.setattr("multiclaw.server.load_mcp_tools_config", lambda path=None: {})

    controller = CollisionController()
    _register_mcp_tools(
        registry=ToolRegistry(),
        mcp_manager=FakeManager(),
        config_path=None,
        sandbox_controller=controller,
        workspace_root=tmp_path,
        mcp_profile_name="mcp_stdio_local",
    )

    skipped = controller.finalize_readiness().skipped_capabilities
    assert len(skipped) == 2
    assert len(set(skipped)) == 2
    assert all(name.startswith("mcp_stdio_a_") for name in skipped)
    assert all(len(name) > len("mcp_stdio_a_") for name in skipped)


def test_lifespan_still_disposes_database_when_runtime_pool_close_fails(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    class FakeDatabase:
        def __init__(self) -> None:
            self.dispose_calls = 0
            self.dialect = object()

        async def dispose(self) -> None:
            self.dispose_calls += 1

    class FakeFactory:
        def __init__(self) -> None:
            self.settings = SimpleNamespace(
                database=SimpleNamespace(path=str(tmp_path / "app.db")),
                runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
                workflow=SimpleNamespace(lease_ttl_ms=1000),
                deletion=SimpleNamespace(retention_days=7),
            )
            self.database = FakeDatabase()
            self.workspace_resolver = SimpleNamespace(root=tmp_path)

        def probe_startup(self):
            return SimpleNamespace(ready=True), ()

    class FailingRuntimePool:
        def __init__(self, *, factory, max_resident_tenants, idle_ttl_ms) -> None:
            del max_resident_tenants, idle_ttl_ms
            self.factory = factory
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("pool close failed")

    fake_factory = FakeFactory()
    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module, "RuntimePool", FailingRuntimePool)

    with pytest.raises(RuntimeError, match="pool close failed"):
        with TestClient(server_module.app):
            pass

    assert fake_factory.database.dispose_calls == 1


def test_lifespan_disposes_database_when_auth_context_build_fails(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    class FakeDatabase:
        def __init__(self) -> None:
            self.dispose_calls = 0
            self.dialect = object()

        async def dispose(self) -> None:
            self.dispose_calls += 1

    fake_database = FakeDatabase()
    fake_factory = SimpleNamespace(
        settings=SimpleNamespace(
            database=SimpleNamespace(path=str(tmp_path / "app.db")),
            runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
            workflow=SimpleNamespace(lease_ttl_ms=1000),
            deletion=SimpleNamespace(retention_days=7),
        ),
        database=fake_database,
        workspace_resolver=SimpleNamespace(root=tmp_path),
        probe_startup=lambda: (SimpleNamespace(), ()),
        create=None,
    )

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(
        server_module,
        "build_auth_runtime",
        lambda settings: (_ for _ in ()).throw(RuntimeError("auth context init failed")),
    )

    with pytest.raises(RuntimeError, match="auth context init failed"):
        with TestClient(server_module.app):
            pass

    assert fake_database.dispose_calls == 1


def test_lifespan_preserves_primary_error_when_auth_close_fails(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    class FakeDatabase:
        def __init__(self) -> None:
            self.dispose_calls = 0
            self.dialect = object()

        async def dispose(self) -> None:
            self.dispose_calls += 1

    fake_database = FakeDatabase()
    fake_factory = SimpleNamespace(
        settings=SimpleNamespace(
            database=SimpleNamespace(path=str(tmp_path / "app.db")),
            runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
            workflow=SimpleNamespace(lease_ttl_ms=1000),
            deletion=SimpleNamespace(retention_days=7),
        ),
        database=fake_database,
        workspace_resolver=SimpleNamespace(root=tmp_path),
        probe_startup=lambda: (SimpleNamespace(), ()),
        create=None,
    )

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    class FakeAuth:
        signing_key = b"x" * 32
        allowed_origins = frozenset({"http://testserver"})

        async def close(self) -> None:
            raise RuntimeError("auth close failed")

    monkeypatch.setattr(
        server_module,
        "build_auth_runtime",
        lambda settings: (_ for _ in ()).throw(RuntimeError("auth init failed")),
    )
    monkeypatch.setattr(server_module, "build_auth_runtime", lambda settings: FakeAuth())
    monkeypatch.setattr(
        server_module,
        "_validate_allowed_origins",
        lambda origins: frozenset(origins),
    )

    original_probe = fake_factory.probe_startup

    def probe_then_fail():
        return original_probe()

    fake_factory.probe_startup = probe_then_fail

    with pytest.raises(RuntimeError, match="auth close failed") as exc_info:
        with TestClient(server_module.app):
            pass

    assert fake_database.dispose_calls == 1
    assert str(exc_info.value) == "auth close failed"


def test_lifespan_preserves_primary_error_when_database_dispose_fails(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    class FakeDatabase:
        def __init__(self) -> None:
            self.dispose_calls = 0
            self.dialect = object()

        async def dispose(self) -> None:
            self.dispose_calls += 1
            raise RuntimeError("database dispose failed")

    fake_database = FakeDatabase()
    fake_factory = SimpleNamespace(
        settings=SimpleNamespace(
            database=SimpleNamespace(path=str(tmp_path / "app.db")),
            runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
        ),
        database=fake_database,
        workspace_resolver=SimpleNamespace(root=tmp_path),
        probe_startup=lambda: (SimpleNamespace(), ()),
        create=None,
    )

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(
        server_module,
        "build_auth_runtime",
        lambda settings: (_ for _ in ()).throw(RuntimeError("auth init failed")),
    )

    with pytest.raises(RuntimeError, match="auth init failed") as exc_info:
        with TestClient(server_module.app):
            pass

    assert fake_database.dispose_calls == 1
    assert exc_info.value.__notes__
    assert any(
        "database.dispose" in note and "database dispose failed" in note
        for note in exc_info.value.__notes__
    )


def test_lifespan_probe_failure_closes_pool_before_disposing_database(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    call_order: list[str] = []

    class FakeDatabase:
        async def dispose(self) -> None:
            call_order.append("database.dispose")

    class FakeRuntimePool:
        def __init__(self, *, factory, max_resident_tenants, idle_ttl_ms) -> None:
            del factory, max_resident_tenants, idle_ttl_ms

        async def close(self) -> None:
            call_order.append("runtime_pool.close")

    class FakeFactory:
        def __init__(self) -> None:
            self.settings = SimpleNamespace(
                database=SimpleNamespace(path=str(tmp_path / "app.db")),
                runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
            )
            self.database = FakeDatabase()
            self.workspace_resolver = SimpleNamespace(root=tmp_path)

        def probe_startup(self):
            raise RuntimeError("probe failed")

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: FakeFactory())
    monkeypatch.setattr(server_module, "RuntimePool", FakeRuntimePool)

    with pytest.raises(RuntimeError, match="probe failed"):
        with TestClient(server_module.app):
            pass

    assert call_order == ["runtime_pool.close", "database.dispose"]


def test_lifespan_probe_failure_preserves_primary_and_cleanup_notes(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    class FakeDatabase:
        async def dispose(self) -> None:
            raise RuntimeError("database dispose failed")

    class FailingRuntimePool:
        def __init__(self, *, factory, max_resident_tenants, idle_ttl_ms) -> None:
            del factory, max_resident_tenants, idle_ttl_ms

        async def close(self) -> None:
            raise RuntimeError("runtime pool close failed")

    class FakeFactory:
        def __init__(self) -> None:
            self.settings = SimpleNamespace(
                database=SimpleNamespace(path=str(tmp_path / "app.db")),
                runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
            )
            self.database = FakeDatabase()
            self.workspace_resolver = SimpleNamespace(root=tmp_path)

        def probe_startup(self):
            raise RuntimeError("probe failed")

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: FakeFactory())
    monkeypatch.setattr(server_module, "RuntimePool", FailingRuntimePool)

    with pytest.raises(RuntimeError, match="probe failed") as error:
        with TestClient(server_module.app):
            pass

    assert error.value.__notes__
    assert any("runtime_pool.close" in note and "runtime pool close failed" in note for note in error.value.__notes__)
    assert any("database.dispose" in note and "database dispose failed" in note for note in error.value.__notes__)


def test_lifespan_normal_shutdown_preserves_primary_and_calls_all_cleanup_in_order(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    call_order: list[str] = []

    class FakeDatabase:
        async def dispose(self) -> None:
            call_order.append("database.dispose")
            raise RuntimeError("database dispose failed")

    class FailingRuntimePool:
        def __init__(self, *, factory, max_resident_tenants, idle_ttl_ms) -> None:
            del factory, max_resident_tenants, idle_ttl_ms

        async def close(self) -> None:
            call_order.append("runtime_pool.close")
            raise RuntimeError("runtime pool close failed")

    fake_factory = SimpleNamespace(
        settings=SimpleNamespace(
            database=SimpleNamespace(path=str(tmp_path / "app.db")),
            runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
            workflow=SimpleNamespace(lease_ttl_ms=1000),
            deletion=SimpleNamespace(retention_days=7),
        ),
        database=FakeDatabase(),
        workspace_resolver=SimpleNamespace(root=tmp_path),
        probe_startup=lambda: (SimpleNamespace(ready=True), ()),
        create=None,
    )

    class FakeAuth:
        signing_key = b"x" * 32
        allowed_origins = frozenset({"http://testserver"})

        async def close(self) -> None:
            call_order.append("auth.close")
            raise RuntimeError("auth close failed")

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module, "RuntimePool", FailingRuntimePool)
    monkeypatch.setattr(server_module, "build_auth_runtime", lambda settings: FakeAuth())
    monkeypatch.setattr(server_module, "_validate_allowed_origins", lambda origins: frozenset(origins))

    with pytest.raises(RuntimeError, match="auth close failed") as error:
        with TestClient(server_module.app):
            pass

    assert call_order == ["auth.close", "runtime_pool.close", "database.dispose"]
    assert error.value.__notes__
    assert any("runtime_pool.close" in note and "runtime pool close failed" in note for note in error.value.__notes__)
    assert any("database.dispose" in note and "database dispose failed" in note for note in error.value.__notes__)


def test_lifespan_builds_one_shared_deletion_service_and_worker_and_stops_worker(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    tracker: dict[str, object] = {
        "calls": 0,
        "init_kwargs": [],
        "stop_states": [],
        "batch_sizes": [],
        "intervals": [],
        "completed": 0,
        "recovery_runs": 0,
        "auth_cleanup_runs": 0,
    }
    deletion_started = threading.Event()
    scheduled_tasks: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    class FakeDatabase:
        def __init__(self) -> None:
            self.dispose_calls = 0
            self.dialect = object()

        async def dispose(self) -> None:
            self.dispose_calls += 1

        def connect(self):
            class _Connect:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return None

            return _Connect()

    class FakeRuntimePool:
        def __init__(self, *, factory, max_resident_tenants, idle_ttl_ms) -> None:
            del factory, max_resident_tenants, idle_ttl_ms
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    class FakeAuth:
        signing_key = b"x" * 32
        allowed_origins = frozenset({"http://testserver"})

        async def close(self) -> None:
            return None

    class FakeFactory:
        def __init__(self) -> None:
            self.settings = SimpleNamespace(
                database=SimpleNamespace(path=str(tmp_path / "app.db")),
                runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
                workflow=SimpleNamespace(heartbeat_ms=1000, lease_ttl_ms=1000),
                deletion=SimpleNamespace(retention_days=7),
            )
            self.database = FakeDatabase()
            self.workspace_resolver = SimpleNamespace(root=tmp_path)

        def probe_startup(self):
            return SimpleNamespace(ready=True), ()

    class FakeRecoveryWorker:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def run_once(self) -> None:
            tracker["recovery_runs"] = int(tracker["recovery_runs"]) + 1
            await asyncio.sleep(0)

    class FakeAuthCleanupWorker:
        def __init__(self, database) -> None:
            del database

        async def run_once(self) -> None:
            tracker["auth_cleanup_runs"] = int(tracker["auth_cleanup_runs"]) + 1
            await asyncio.sleep(0)

    class FakeDeletionWorker:
        def __init__(self, **kwargs) -> None:
            init_kwargs = tracker["init_kwargs"]
            assert isinstance(init_kwargs, list)
            init_kwargs.append(kwargs)

        async def run_until_stopped(
            self,
            *,
            stop_event: asyncio.Event,
            batch_size: int = 10,
            interval_seconds: float = 1.0,
        ) -> None:
            deletion_started.set()
            tracker["calls"] = int(tracker["calls"]) + 1
            cast_stop_states = tracker["stop_states"]
            cast_batch_sizes = tracker["batch_sizes"]
            cast_intervals = tracker["intervals"]
            assert isinstance(cast_stop_states, list)
            assert isinstance(cast_batch_sizes, list)
            assert isinstance(cast_intervals, list)
            cast_stop_states.append(stop_event.is_set())
            cast_batch_sizes.append(batch_size)
            cast_intervals.append(interval_seconds)
            await stop_event.wait()
            tracker["completed"] = int(tracker["completed"]) + 1

    fake_factory = FakeFactory()
    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module, "RuntimePool", FakeRuntimePool)
    monkeypatch.setattr(server_module, "build_auth_runtime", lambda settings: FakeAuth())
    monkeypatch.setattr(server_module, "_validate_allowed_origins", lambda origins: frozenset(origins))
    monkeypatch.setattr(server_module, "WorkflowRecoveryWorker", FakeRecoveryWorker)
    monkeypatch.setattr(server_module, "AuthCleanupWorker", FakeAuthCleanupWorker)
    monkeypatch.setattr(server_module, "DeletionWorker", FakeDeletionWorker)
    monkeypatch.setattr(
        server_module.asyncio,
        "create_task",
        lambda coro: scheduled_tasks.append(real_create_task(coro)) or scheduled_tasks[-1],
    )

    with TestClient(server_module.app):
        deletion_service = server_module.app.state.deletion_service
        assert deletion_service._database is server_module.app.state.database
        assert deletion_service._runtime_pool is server_module.app.state.runtime_pool
        assert deletion_service._settings is server_module.app.state.settings
        assert server_module.app.state.deletion_worker is not None
        assert deletion_started.wait(timeout=1.0)

    assert tracker["calls"] == 1
    assert len(tracker["init_kwargs"]) == 1
    worker_kwargs = tracker["init_kwargs"][0]
    assert isinstance(worker_kwargs, dict)
    assert worker_kwargs["database"] is fake_factory.database
    assert worker_kwargs["runtime_pool"] is server_module.app.state.runtime_pool
    assert worker_kwargs["workspace_resolver"] is fake_factory.workspace_resolver
    assert worker_kwargs["settings"] is fake_factory.settings
    assert tracker["stop_states"] == [False]
    assert tracker["batch_sizes"] == [8]
    assert tracker["intervals"] == [1.0]
    assert tracker["completed"] == 1
    assert tracker["recovery_runs"] >= 1
    assert tracker["auth_cleanup_runs"] >= 1
    assert len(scheduled_tasks) == 3
    assert all(task.done() for task in scheduled_tasks)
    assert fake_factory.database.dispose_calls == 1


def test_lifespan_does_not_start_deletion_worker_when_readiness_is_not_ready(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    tracker = {"calls": 0}

    class FakeDatabase:
        def __init__(self) -> None:
            self.dispose_calls = 0

        async def dispose(self) -> None:
            self.dispose_calls += 1

        def connect(self):
            class _Connect:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return None

            return _Connect()

    class FakeRuntimePool:
        def __init__(self, *, factory, max_resident_tenants, idle_ttl_ms) -> None:
            del factory, max_resident_tenants, idle_ttl_ms

        async def close(self) -> None:
            return None

    class FakeAuth:
        signing_key = b"x" * 32
        allowed_origins = frozenset({"http://testserver"})

        async def close(self) -> None:
            return None

    fake_factory = SimpleNamespace(
        settings=SimpleNamespace(
            database=SimpleNamespace(path=str(tmp_path / "app.db")),
            runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
            workflow=SimpleNamespace(heartbeat_ms=1000, lease_ttl_ms=1000),
            deletion=SimpleNamespace(retention_days=7),
        ),
        database=FakeDatabase(),
        workspace_resolver=SimpleNamespace(root=tmp_path),
        probe_startup=lambda: (SimpleNamespace(ready=False), ()),
    )

    class FakeRecoveryWorker:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def run_once(self) -> None:
            await asyncio.sleep(0)

    class FakeAuthCleanupWorker:
        def __init__(self, database) -> None:
            del database

        async def run_once(self) -> None:
            await asyncio.sleep(0)

    class FakeDeletionWorker:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def run_until_stopped(self, **kwargs) -> None:
            del kwargs
            tracker["calls"] += 1

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module, "RuntimePool", FakeRuntimePool)
    monkeypatch.setattr(server_module, "build_auth_runtime", lambda settings: FakeAuth())
    monkeypatch.setattr(server_module, "_validate_allowed_origins", lambda origins: frozenset(origins))
    monkeypatch.setattr(server_module, "WorkflowRecoveryWorker", FakeRecoveryWorker)
    monkeypatch.setattr(server_module, "AuthCleanupWorker", FakeAuthCleanupWorker)
    monkeypatch.setattr(server_module, "DeletionWorker", FakeDeletionWorker)

    with TestClient(server_module.app):
        assert server_module.app.state.deletion_service is not None

    assert tracker["calls"] == 0


def test_lifespan_startup_failure_does_not_start_deletion_worker(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    tracker = {"calls": 0}

    class FakeDatabase:
        def __init__(self) -> None:
            self.dispose_calls = 0

        async def dispose(self) -> None:
            self.dispose_calls += 1

    fake_database = FakeDatabase()
    fake_factory = SimpleNamespace(
        settings=SimpleNamespace(
            database=SimpleNamespace(path=str(tmp_path / "app.db")),
            runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
        ),
        database=fake_database,
        workspace_resolver=SimpleNamespace(root=tmp_path),
        probe_startup=lambda: (SimpleNamespace(ready=True), ()),
        create=None,
    )

    class FakeDeletionWorker:
        def __init__(self, **kwargs) -> None:
            del kwargs
            tracker["calls"] += 1

        async def run_until_stopped(self, **kwargs) -> None:
            del kwargs

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module, "DeletionWorker", FakeDeletionWorker)
    monkeypatch.setattr(
        server_module,
        "build_auth_runtime",
        lambda settings: (_ for _ in ()).throw(RuntimeError("auth init failed")),
    )

    with pytest.raises(RuntimeError, match="auth init failed"):
        with TestClient(server_module.app):
            pass

    assert tracker["calls"] == 0
    assert fake_database.dispose_calls == 1


def test_account_router_is_registered_once_under_api_prefix_and_reachable(tmp_path, monkeypatch):
    import multiclaw.server as server_module
    import multiclaw.api.account as account_module

    class FakeDatabase:
        async def dispose(self) -> None:
            return None

    class FakeRuntimePool:
        def __init__(self, *, factory, max_resident_tenants, idle_ttl_ms) -> None:
            del factory, max_resident_tenants, idle_ttl_ms

        async def close(self) -> None:
            return None

    class FakeAuth:
        signing_key = b"x" * 32
        allowed_origins = frozenset({"http://testserver"})

        async def close(self) -> None:
            return None

    fake_factory = SimpleNamespace(
        settings=SimpleNamespace(
            database=SimpleNamespace(path=str(tmp_path / "app.db")),
            runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
            workflow=SimpleNamespace(lease_ttl_ms=1000),
            deletion=SimpleNamespace(retention_days=7),
        ),
        database=FakeDatabase(),
        workspace_resolver=SimpleNamespace(root=tmp_path),
        probe_startup=lambda: (SimpleNamespace(ready=True), ()),
    )

    class FakeDeletionService:
        async def get_status(self, tenant_id: str) -> dict[str, object]:
            assert tenant_id == "tenant-route"
            return {"status": "scheduled", "purge_after": 12345}

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module, "RuntimePool", FakeRuntimePool)
    monkeypatch.setattr(server_module, "build_auth_runtime", lambda settings: FakeAuth())
    monkeypatch.setattr(server_module, "_validate_allowed_origins", lambda origins: frozenset(origins))

    route_paths = [route.path for route in server_module.app.routes if hasattr(route, "path")]
    assert route_paths.count("/api/account/deletion") == 2
    assert "/api/account/deletion/recover" in route_paths
    assert not any(path.startswith("/api/api/account") for path in route_paths)

    server_module.app.dependency_overrides[account_module.require_recovery_auth] = lambda: account_module.RecoveryAuthContext(
        tenant_id="tenant-route",
        email="route@example.com",
        job_id="job-route",
    )
    try:
        with TestClient(server_module.app) as client:
            client.app.state.deletion_service = FakeDeletionService()
            response = client.get("/api/account/deletion")
    finally:
        server_module.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "scheduled", "purge_after": 12345}


def test_create_runtime_factory_passes_configured_mcp_profile_name(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "true")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
    monkeypatch.setenv(
        "MULTICLAW_GOVERNANCE__SANDBOX__PROFILES__MCP_STDIO",
        "custom_mcp_profile",
    )

    import multiclaw.server as server_module

    captured: dict[str, str] = {}
    real_register = server_module._register_mcp_tools

    def _register_stub(*, registry, mcp_manager, config_path, sandbox_controller, workspace_root, mcp_profile_name):
        del registry, mcp_manager, config_path, sandbox_controller, workspace_root
        captured["mcp_profile_name"] = mcp_profile_name

    monkeypatch.setattr(server_module, "_register_mcp_tools", _register_stub)
    try:
        factory = _make_runtime_factory(
            server_module,
            tmp_path,
            controller_factory=lambda workspace_root, event_bus: ReadyRecordingSandboxController(
                workspace_root=workspace_root
            ),
        )
        runtime = _create_runtime(factory)
    finally:
        monkeypatch.setattr(server_module, "_register_mcp_tools", real_register)
        asyncio.run(runtime.close())
        asyncio.run(factory.database.dispose())

    assert captured["mcp_profile_name"] == "custom_mcp_profile"


def test_create_runtime_factory_injects_sandbox_context_into_mcp_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "true")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")

    import multiclaw.server as server_module

    captured = {}

    class FakeManager:
        def __init__(self, *, sandbox_controller=None, workspace_root=None, **kwargs):
            captured["sandbox_controller"] = sandbox_controller
            captured["workspace_root"] = workspace_root
            captured["kwargs"] = kwargs
            self.stop_calls = 0

        def set_tools_changed_callback(self, callback) -> None:
            self._callback = callback

        def connect_servers(self, configs):
            self.connected = dict(configs)
            return {}

        def get_server_states(self):
            return {}

        def stop(self) -> None:
            self.stop_calls += 1

    monkeypatch.setattr(server_module, "load_mcp_config", lambda path=None: {})
    monkeypatch.setattr(server_module, "load_mcp_tools_config", lambda path=None: {})

    controller = ReadyRecordingSandboxController(workspace_root=tmp_path)
    factory = _make_runtime_factory(
        server_module,
        tmp_path,
        controller_factory=lambda workspace_root, event_bus: controller,
        mcp_manager_factory=FakeManager,
    )
    runtime = _create_runtime(factory)

    assert captured["sandbox_controller"] is controller
    assert captured["workspace_root"] == runtime.workspace_root
    assert runtime.mcp_manager is not None
    asyncio.run(runtime.close())
    asyncio.run(factory.database.dispose())


def test_create_runtime_factory_passes_configured_shell_profile_and_controller(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
    monkeypatch.setenv(
        "MULTICLAW_GOVERNANCE__SANDBOX__PROFILES__SHELL",
        "custom_shell_profile",
    )

    import multiclaw.server as server_module

    class ProfileController(ReadyRecordingSandboxController):
        def is_profile_ready(self, profile_name: str) -> bool:
            return profile_name in {"custom_shell_profile", "code_exec_python"}

    factory = _make_runtime_factory(
        server_module,
        tmp_path,
        controller_factory=lambda workspace_root, event_bus: ProfileController(workspace_root=workspace_root),
    )
    runtime = _create_runtime(factory)
    shell_builder = runtime.registry.get("shell")

    assert isinstance(shell_builder, ShellToolBuilder)
    assert shell_builder.sandbox_controller is runtime.sandbox_controller
    assert shell_builder.profile_name == "custom_shell_profile"
    asyncio.run(runtime.close())
    asyncio.run(factory.database.dispose())


def test_create_runtime_factory_passes_configured_code_exec_profile_and_controller(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
    monkeypatch.setenv(
        "MULTICLAW_GOVERNANCE__SANDBOX__PROFILES__CODE_EXEC",
        "custom_code_profile",
    )

    import multiclaw.server as server_module

    class ProfileController(ReadyRecordingSandboxController):
        def is_profile_ready(self, profile_name: str) -> bool:
            return profile_name in {"shell_workspace", "custom_code_profile"}

    factory = _make_runtime_factory(
        server_module,
        tmp_path,
        controller_factory=lambda workspace_root, event_bus: ProfileController(workspace_root=workspace_root),
    )
    runtime = _create_runtime(factory)
    code_exec_builder = runtime.registry.get("code_exec")

    assert isinstance(code_exec_builder, CodeExecToolBuilder)
    assert code_exec_builder.sandbox_controller is runtime.sandbox_controller
    assert code_exec_builder.profile_name == "custom_code_profile"
    asyncio.run(runtime.close())
    asyncio.run(factory.database.dispose())


@pytest.mark.parametrize(
    ("allow_private_networks", "expected"),
    [
        (None, False),
        ("true", True),
    ],
)
def test_create_runtime_factory_wires_web_fetch_private_network_flag(
    tmp_path,
    monkeypatch,
    allow_private_networks,
    expected,
):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
    if allow_private_networks is None:
        monkeypatch.delenv("MULTICLAW_TOOLS__WEB_FETCH_ALLOW_PRIVATE_NETWORKS", raising=False)
    else:
        monkeypatch.setenv(
            "MULTICLAW_TOOLS__WEB_FETCH_ALLOW_PRIVATE_NETWORKS",
            allow_private_networks,
        )

    from multiclaw.tools.web_fetch import WebFetchToolBuilder
    import multiclaw.server as server_module

    factory = _make_runtime_factory(
        server_module,
        tmp_path,
        controller_factory=lambda workspace_root, event_bus: ReadyRecordingSandboxController(
            workspace_root=workspace_root
        ),
    )
    runtime = _create_runtime(factory)
    web_fetch_builder = runtime.registry.get("web_fetch")

    assert isinstance(web_fetch_builder, WebFetchToolBuilder)
    assert web_fetch_builder.allow_private_networks is expected
    asyncio.run(runtime.close())
    asyncio.run(factory.database.dispose())


def test_chat_runtime_signal_blocks_idle_eviction_until_stream_finishes(migrated_database, monkeypatch):
    import multiclaw.server as server

    started = threading.Event()
    release = threading.Event()
    holder: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context):
        del user_input, context
        started.set()
        await asyncio.to_thread(release.wait)
        yield {"type": "done", "content": ""}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            holder["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)

        response_box: dict[str, object] = {}

        def run_request() -> None:
            response_box["response"] = client.post("/api/chat", json={"message": "hello"})

        request_thread = threading.Thread(target=run_request)
        request_thread.start()
        assert started.wait(timeout=3)
        runtime = holder["runtime"]
        assert runtime.active_executing_run_count == 1
        assert runtime.active_run_count == 1
        assert runtime.can_evict(runtime.last_used_at_ms + server.app.state.runtime_pool.idle_ttl_ms + 1, server.app.state.runtime_pool.idle_ttl_ms) is False

        release.set()
        request_thread.join(timeout=5)

        assert request_thread.is_alive() is False
    assert runtime.active_executing_run_count == 0
    assert runtime.active_run_count == 0
    assert response_box["response"].status_code == 200


@pytest.mark.asyncio
async def test_chat_establishes_run_lease_before_first_stream_iteration(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.storage.uow import TenantUnitOfWork

    release = asyncio.Event()
    holder: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context):
        del user_input, context
        await release.wait()
        yield {"type": "done", "content": ""}

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "stale@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            holder["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request(
            {
                "type": "http",
                "app": server.app,
                "method": "POST",
                "path": "/api/chat",
                "headers": [],
            }
        )
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(
                server.ChatRequest(message="hello"),
                request,
                context,
                uow,
            )
            runtime = holder["runtime"]
            assert runtime.active_executing_run_count == 1
            assert runtime.active_run_count == 1
            runtime.mark_unavailable()
            await anext(response.body_iterator)
            release.set()
            async for _ in response.body_iterator:
                pass

    assert runtime.active_executing_run_count == 0
    assert runtime.active_run_count == 0


@pytest.mark.asyncio
async def test_chat_passes_db_run_lease_to_stream_handler(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.storage.uow import TenantUnitOfWork

    release = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context, run_lease):
        del user_input
        captured["context"] = context
        captured["run_lease"] = run_lease
        async with TenantUnitOfWork(server.app.state.database, context) as verify:
            persisted = await verify.workflow.get_run(context)
            assert persisted is not None
            assert persisted.lease_owner == run_lease.lease_owner
            assert persisted.fencing_token == run_lease.fencing_token
        await release.wait()
        yield {"type": "done", "content": ""}

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "db-run-lease@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            captured["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request(
            {
                "type": "http",
                "app": server.app,
                "method": "POST",
                "path": "/api/chat",
                "headers": [],
            }
        )
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(
                server.ChatRequest(message="hello"),
                request,
                context,
                uow,
            )
            body: list[str] = []

            async def consume() -> None:
                async for chunk in response.body_iterator:
                    body.append(chunk)

            consume_task = asyncio.create_task(consume())
            for _ in range(30):
                if "run_lease" in captured:
                    break
                await asyncio.sleep(0.05)
            release.set()
            await consume_task
            assert '"type":"error"' not in "".join(body)

    runtime = captured["runtime"]
    assert "run_lease" in captured
    assert runtime.active_executing_run_count == 0
    assert runtime.active_run_count == 0


@pytest.mark.asyncio
async def test_chat_persists_structured_run_start_and_terminal_checkpoints(migrated_database, monkeypatch):
    import multiclaw.server as server

    captured: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context, run_lease):
        del user_input, run_lease
        captured["context"] = context
        yield {"type": "done", "content": "ok", "data": {}}

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "structured-checkpoints@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)
        async for _ in response.body_iterator:
            pass

    run_context = captured["context"]
    checkpoints = await _checkpoint_rows(migrated_database, run_context)

    assert [row["phase"] for row in checkpoints] == [
        CheckpointPhase.RUN_STARTED.value,
        CheckpointPhase.RUN_TERMINAL.value,
    ]
    assert checkpoints[0]["payload"]["tenant_id"] == run_context.tenant_id
    assert checkpoints[0]["payload"]["workspace_id"] == run_context.workspace_id
    assert checkpoints[0]["payload"]["session_id"] == run_context.session_id
    assert checkpoints[0]["payload"]["run_id"] == run_context.run_id
    assert checkpoints[0]["payload"]["next_step"] == "model_inference"
    assert checkpoints[1]["payload"]["run_id"] == run_context.run_id
    assert checkpoints[1]["payload"]["terminal_status"] == RunStatus.COMPLETED.value
    assert checkpoints[1]["payload"]["next_step"] is None
    assert checkpoints[1]["payload"]["cursor"] is None
    assert await _run_status(migrated_database, run_context) == RunStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_chat_start_run_checkpoint_failure_rolls_back_live_run_creation(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.storage.repositories.workflow import WorkflowRepository

    async def fail_start_checkpoint(self, lease, **kwargs):
        if kwargs.get("phase") == CheckpointPhase.RUN_STARTED.value:
            raise RuntimeError("start checkpoint failed")
        return True

    monkeypatch.setattr(WorkflowRepository, "_insert_checkpoint", fail_start_checkpoint)

    user_id, _ = await _seed_user(migrated_database, "start-checkpoint-failure@example.com")
    async with AuthUnitOfWork(migrated_database) as auth_uow:
        user = await auth_uow.users.get_by_id(user_id)
        assert user is not None
        workspace_id = user.default_workspace_id
        assert workspace_id is not None
    context = TenantContext(user_id, workspace_id)
    request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})

    with TestClient(server.app):
        with pytest.raises(RuntimeError, match="start checkpoint failed"):
            async with TenantUnitOfWork(server.app.state.database, context) as uow:
                await server.chat(server.ChatRequest(message="hello"), request, context, uow)

    async with migrated_database.connect() as conn:
        run_count = await conn.scalar(select(agent_runs.c.run_id).where(agent_runs.c.tenant_id == context.tenant_id))
        checkpoint_count = await conn.scalar(
            select(execution_checkpoints.c.checkpoint_id).where(
                execution_checkpoints.c.tenant_id == context.tenant_id,
            )
        )
    assert run_count is None
    assert checkpoint_count is None


@pytest.mark.asyncio
async def test_chat_success_terminal_sse_waits_for_terminal_persistence_commit(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.workflow.coordinator import WorkflowCoordinator

    captured: dict[str, object] = {}
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    async def fake_handle_message_stream(user_input: str, *, context, run_lease):
        del user_input, run_lease
        captured["context"] = context
        yield {"type": "done", "content": "ok", "data": {}}

    original_finish = WorkflowCoordinator.finish_run_with_checkpoint

    async def delayed_finish(self, lease, target):
        captured["terminal_target"] = target
        persist_started.set()
        await release_persist.wait()
        return await original_finish(self, lease, target)

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "terminal-ordering@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        monkeypatch.setattr(WorkflowCoordinator, "finish_run_with_checkpoint", delayed_finish)
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        consume_task = asyncio.create_task(consume())
        await persist_started.wait()
        assert await _run_status(migrated_database, captured["context"]) == RunStatus.RUNNING.value
        assert [row["phase"] for row in await _checkpoint_rows(migrated_database, captured["context"])] == [
            CheckpointPhase.RUN_STARTED.value
        ]
        assert not any('"type":"finish"' in chunk for chunk in chunks)
        release_persist.set()
        await consume_task

    assert captured["terminal_target"] is RunStatus.COMPLETED
    assert any('"type":"finish"' in chunk for chunk in chunks)
    assert await _run_status(migrated_database, captured["context"]) == RunStatus.COMPLETED.value
    assert [row["phase"] for row in await _checkpoint_rows(migrated_database, captured["context"])] == [
        CheckpointPhase.RUN_STARTED.value,
        CheckpointPhase.RUN_TERMINAL.value,
    ]


@pytest.mark.asyncio
async def test_chat_invokes_live_recovery_validation_before_stream_iteration(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.workflow.models import RecoveryAction

    validation_called = asyncio.Event()
    captured: dict[str, object] = {}

    class FakeRecoveryService:
        async def validate_live_run(self, context):
            checkpoints = await _checkpoint_rows(migrated_database, context)
            captured["validated_context"] = context
            captured["validated_phases"] = [row["phase"] for row in checkpoints]
            validation_called.set()
            return SimpleNamespace(action=RecoveryAction.RESUME_MODEL, reason="", status=None)

    async def fake_handle_message_stream(user_input: str, *, context, run_lease):
        del user_input, run_lease
        assert validation_called.is_set() is True
        captured["handler_context"] = context
        yield {"type": "done", "content": "ok", "data": {}}

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "live-recovery-validation@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        monkeypatch.setattr(server, "build_workflow_recovery_service", lambda database, settings: FakeRecoveryService())
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)
        async for _ in response.body_iterator:
            pass

    assert validation_called.is_set() is True
    assert captured["validated_context"] == captured["handler_context"]
    assert captured["validated_phases"] == [CheckpointPhase.RUN_STARTED.value]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        (
            RecoveryOutcome(
                action=None,
                status=RunStatus.BLOCKED_CORRUPT,
                reason="corrupt checkpoint",
            ),
            RunStatus.BLOCKED_CORRUPT,
        ),
        (
            RecoveryOutcome(
                action=None,
                status=RunStatus.BLOCKED_INCOMPATIBLE,
                reason="incompatible checkpoint",
            ),
            RunStatus.BLOCKED_INCOMPATIBLE,
        ),
    ],
)
async def test_chat_validation_failure_cleans_up_blocked_run_without_stream_start(
    migrated_database,
    monkeypatch,
    outcome: RecoveryOutcome,
    expected_status: RunStatus,
):
    import multiclaw.server as server
    from multiclaw.workflow.coordinator import WorkflowCoordinator

    captured: dict[str, object] = {}

    class FakeRecoveryService:
        async def validate_live_run(self, context):
            captured["validated_context"] = context
            return outcome

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "validation-blocked@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)
            original_handler = runtime.agent.handle_message_stream

            async def fail_if_started(*args, **kwargs):
                raise AssertionError("stream handler should not start after blocked validation")

            monkeypatch.setattr(runtime.agent, "handle_message_stream", fail_if_started)
            captured["runtime"] = runtime
            captured["original_handler"] = original_handler
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        monkeypatch.setattr(server, "build_workflow_recovery_service", lambda database, settings: FakeRecoveryService())
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})

        with pytest.raises(RuntimeError, match="live workflow checkpoint validation failed"):
            async with TenantUnitOfWork(server.app.state.database, context) as uow:
                await server.chat(server.ChatRequest(message="hello"), request, context, uow)

    run_context = await _latest_run_context(migrated_database, context)
    assert await _run_status(migrated_database, run_context) == expected_status.value
    assert [row["phase"] for row in await _checkpoint_rows(migrated_database, run_context)] == [
        CheckpointPhase.RUN_STARTED.value,
        CheckpointPhase.RUN_TERMINAL.value,
    ]
    terminal = (await _checkpoint_rows(migrated_database, run_context))[-1]
    assert terminal["payload"]["terminal_status"] == expected_status.value
    runtime = captured["runtime"]
    assert runtime.active_run_count == 0
    assert runtime.active_executing_run_count == 0

    followup_context = TenantContext(context.tenant_id, context.workspace_id)
    async with TenantUnitOfWork(server.app.state.database, followup_context) as uow:
        session = await uow.sessions.create(title="followup")
    another_run = followup_context.for_run(session.id, str(uuid4()))
    lease = await WorkflowCoordinator(server.app.state.database, settings=server.app.state.settings).start_run(
        another_run,
        "runtime-followup",
    )
    await WorkflowCoordinator(server.app.state.database, settings=server.app.state.settings).finish_run(
        lease,
        RunStatus.CANCELLED,
    )


@pytest.mark.asyncio
async def test_chat_validation_exception_cleans_up_failed_run_without_stream_start(
    migrated_database,
    monkeypatch,
):
    import multiclaw.server as server

    captured: dict[str, object] = {}

    class FakeRecoveryService:
        async def validate_live_run(self, context):
            captured["validated_context"] = context
            raise RuntimeError("validation exploded")

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "validation-exception@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)

            async def fail_if_started(*args, **kwargs):
                raise AssertionError("stream handler should not start after validation exception")

            monkeypatch.setattr(runtime.agent, "handle_message_stream", fail_if_started)
            captured["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        monkeypatch.setattr(server, "build_workflow_recovery_service", lambda database, settings: FakeRecoveryService())
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})

        with pytest.raises(RuntimeError, match="validation exploded"):
            async with TenantUnitOfWork(server.app.state.database, context) as uow:
                await server.chat(server.ChatRequest(message="hello"), request, context, uow)

    run_context = await _latest_run_context(migrated_database, context)
    assert await _run_status(migrated_database, run_context) == RunStatus.FAILED_TERMINAL.value
    assert [row["phase"] for row in await _checkpoint_rows(migrated_database, run_context)] == [
        CheckpointPhase.RUN_STARTED.value,
        CheckpointPhase.RUN_TERMINAL.value,
    ]
    terminal = (await _checkpoint_rows(migrated_database, run_context))[-1]
    assert terminal["payload"]["terminal_status"] == RunStatus.FAILED_TERMINAL.value
    runtime = captured["runtime"]
    assert runtime.active_run_count == 0
    assert runtime.active_executing_run_count == 0


@pytest.mark.asyncio
async def test_chat_validation_cleanup_fallback_terminal_row_recovery_is_terminal_noop(
    migrated_database,
    monkeypatch,
):
    import multiclaw.server as server
    from multiclaw.storage.repositories.workflow import WorkflowRepository
    from multiclaw.workflow.recovery import RecoveryService

    original_insert = WorkflowRepository._insert_checkpoint
    captured: dict[str, object] = {}

    class FakeRecoveryService:
        async def validate_live_run(self, context):
            captured["validated_context"] = context
            raise RuntimeError("validation fallback cleanup")

    async def fail_terminal_checkpoint(self, lease, **kwargs):
        if kwargs.get("phase") == CheckpointPhase.RUN_TERMINAL.value:
            raise RuntimeError("terminal checkpoint insert failed")
        return await original_insert(self, lease, **kwargs)

    monkeypatch.setattr(WorkflowRepository, "_insert_checkpoint", fail_terminal_checkpoint)

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "validation-fallback-terminal@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)

            async def fail_if_started(*args, **kwargs):
                raise AssertionError("stream handler should not start after validation cleanup fallback")

            monkeypatch.setattr(runtime.agent, "handle_message_stream", fail_if_started)
            captured["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        monkeypatch.setattr(server, "build_workflow_recovery_service", lambda database, settings: FakeRecoveryService())
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})

        with pytest.raises(RuntimeError, match="validation fallback cleanup"):
            async with TenantUnitOfWork(server.app.state.database, context) as uow:
                await server.chat(server.ChatRequest(message="hello"), request, context, uow)

    run_context = await _latest_run_context(migrated_database, context)
    assert await _run_status(migrated_database, run_context) == RunStatus.FAILED_TERMINAL.value
    checkpoints = await _checkpoint_rows(migrated_database, run_context)
    assert [row["phase"] for row in checkpoints] == [CheckpointPhase.RUN_STARTED.value]

    outcome = await RecoveryService(migrated_database).recover(run_context, "runtime-2")
    assert outcome.action is not None
    assert outcome.action.value == "terminal_noop"
    assert outcome.status == RunStatus.FAILED_TERMINAL
    assert outcome.executions_started == 0
    assert outcome.lease is None


@pytest.mark.asyncio
async def test_chat_stale_stream_aborts_after_foreign_takeover_progress_and_does_not_reacquire(
    migrated_database,
    monkeypatch,
):
    import multiclaw.server as server
    from multiclaw.workflow.coordinator import WorkflowCoordinator
    from multiclaw.workflow.models import StaleFenceError

    started = asyncio.Event()
    continue_after_takeover = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context, run_lease_handle):
        del user_input
        coordinator = WorkflowCoordinator(server.app.state.database, settings=server.app.state.settings)
        captured["context"] = context
        captured["run_lease_handle"] = run_lease_handle
        started.set()
        await continue_after_takeover.wait()
        try:
            await run_lease_handle.use_current(
                lambda lease: coordinator.checkpoint(
                    lease,
                    CheckpointPhase.MODEL_OUTPUT_COMMITTED,
                    {
                        "run_id": context.run_id,
                        "message_id": str(uuid4()),
                        "output_digest": "9" * 64,
                        "model_cursor": "cursor-stale-stream",
                        "cursor": "cursor-stale-stream",
                    },
                )
            )
        except Exception as error:
            captured["handler_error"] = error
            raise
        captured["handler_checkpoint_succeeded"] = True
        yield {"type": "done", "content": "stale-success", "data": {}}

    with TestClient(server.app):
        monkeypatch.setattr(server.app.state.settings.workflow, "heartbeat_ms", 20)
        monkeypatch.setattr(server.app.state.settings.workflow, "lease_ttl_ms", 90)
        user_id, _ = await _seed_user(migrated_database, "stale-reacquire@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            captured["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        consume_task = asyncio.create_task(consume())
        await started.wait()
        run_context = captured["context"]
        coordinator = WorkflowCoordinator(server.app.state.database, settings=server.app.state.settings)
        await _expire_lease_with_db_clock(migrated_database, run_context)
        takeover = await coordinator.acquire_run(run_context, "runtime-2")
        await coordinator.checkpoint(
            takeover,
            CheckpointPhase.MODEL_OUTPUT_COMMITTED,
            {
                "run_id": run_context.run_id,
                "message_id": str(uuid4()),
                "output_digest": "8" * 64,
                "model_cursor": "cursor-takeover",
                "cursor": "cursor-takeover",
            },
        )
        await _expire_lease_with_db_clock(migrated_database, run_context)
        await asyncio.sleep(0.08)
        continue_after_takeover.set()
        await consume_task

    body = "".join(chunks)
    if "handler_error" in captured:
        assert isinstance(captured["handler_error"], (StaleFenceError, asyncio.CancelledError))
    assert captured.get("handler_checkpoint_succeeded") is not True
    assert '"type":"finish"' not in body
    assert '"type":"error"' in body
    assert "stale-success" not in body
    phases = [row["phase"] for row in await _checkpoint_rows(migrated_database, run_context)]
    assert phases == [
        CheckpointPhase.RUN_STARTED.value,
        CheckpointPhase.MODEL_OUTPUT_COMMITTED.value,
    ]


@pytest.mark.asyncio
async def test_chat_stream_persists_assistant_output_and_model_checkpoint_before_done(
    migrated_database,
    monkeypatch,
):
    import multiclaw.server as server
    from multiclaw.storage.repositories.workflow import WorkflowRepository

    checkpoint_started = asyncio.Event()
    release_checkpoint = asyncio.Event()
    captured: dict[str, object] = {}
    original_insert = WorkflowRepository._insert_checkpoint

    async def gated_insert(self, lease, **kwargs):
        if kwargs.get("phase") == CheckpointPhase.MODEL_OUTPUT_COMMITTED.value:
            checkpoint_started.set()
            await release_checkpoint.wait()
        return await original_insert(self, lease, **kwargs)

    monkeypatch.setattr(WorkflowRepository, "_insert_checkpoint", gated_insert)

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "assistant-output@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)
            runtime.agent.context_builder = _StaticReportContextBuilder(
                [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                ]
            )
            runtime.agent.router = _QueuedStreamRouter(
                stream_sequences=[[{"type": "token", "content": "streamed reply"}]]
            )
            captured["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        consume_task = asyncio.create_task(consume())
        await checkpoint_started.wait()

        run_event = next(
            json.loads(chunk[6:])
            for chunk in chunks
            if chunk.startswith("data: ") and '"type":"data-run"' in chunk
        )
        run_context = context.for_run(
            run_event["data"]["session_id"],
            run_event["data"]["run_id"],
        )
        assert await _assistant_chat_messages(migrated_database, run_context) == []
        assert [row["phase"] for row in await _checkpoint_rows(migrated_database, run_context)] == [
            CheckpointPhase.RUN_STARTED.value
        ]
        assert not any('"type":"finish"' in chunk for chunk in chunks)

        release_checkpoint.set()
        await consume_task

    assistant_messages = await _assistant_chat_messages(migrated_database, run_context)
    checkpoints = await _checkpoint_rows(migrated_database, run_context)
    assistant_message = assistant_messages[-1]
    model_checkpoint = next(
        row for row in checkpoints if row["phase"] == CheckpointPhase.MODEL_OUTPUT_COMMITTED.value
    )
    assert assistant_message["content"] == "streamed reply"
    assert model_checkpoint["payload"]["message_id"] == assistant_message["id"]
    assert model_checkpoint["payload"]["output_digest"] == hashlib.sha256(
        assistant_message["content"].encode("utf-8")
    ).hexdigest()
    assert model_checkpoint["payload"]["cursor"] == model_checkpoint["payload"]["model_cursor"]
    assert any('"type":"finish"' in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_chat_forced_summary_persists_assistant_output_and_model_checkpoint_before_done(
    migrated_database,
    monkeypatch,
):
    import multiclaw.server as server
    from multiclaw.storage.repositories.workflow import WorkflowRepository

    checkpoint_started = asyncio.Event()
    release_checkpoint = asyncio.Event()
    original_insert = WorkflowRepository._insert_checkpoint

    async def gated_insert(self, lease, **kwargs):
        if kwargs.get("phase") == CheckpointPhase.MODEL_OUTPUT_COMMITTED.value:
            checkpoint_started.set()
            await release_checkpoint.wait()
        return await original_insert(self, lease, **kwargs)

    monkeypatch.setattr(WorkflowRepository, "_insert_checkpoint", gated_insert)

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "forced-summary-output@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)
            runtime.agent.context_builder = _StaticReportContextBuilder(
                [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                ]
            )
            runtime.agent.router = _QueuedStreamRouter(
                stream_sequences=[
                    [
                        {
                            "type": "tool_calls",
                            "calls": [{"id": "call-1", "name": "web_search", "arguments": {"query": "hello"}}],
                            "reasoning_content": "",
                        }
                    ],
                    [{"type": "token", "content": "forced summary"}],
                ]
            )
            runtime.agent._execute_tool_batch = AsyncMock(
                return_value=[
                    SimpleNamespace(
                        call_id="call-1",
                        name="web_search",
                        observation=SimpleNamespace(content="tool result"),
                    )
                ]
            )
            runtime.agent.settings.agent.max_tool_rounds = 1
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)

        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        consume_task = asyncio.create_task(consume())
        await checkpoint_started.wait()

        run_event = next(
            json.loads(chunk[6:])
            for chunk in chunks
            if chunk.startswith("data: ") and '"type":"data-run"' in chunk
        )
        run_context = context.for_run(
            run_event["data"]["session_id"],
            run_event["data"]["run_id"],
        )
        assert await _assistant_chat_messages(migrated_database, run_context) == []
        assert [row["phase"] for row in await _checkpoint_rows(migrated_database, run_context)] == [
            CheckpointPhase.RUN_STARTED.value
        ]
        assert not any('"type":"finish"' in chunk for chunk in chunks)

        release_checkpoint.set()
        await consume_task

    assistant_messages = await _assistant_chat_messages(migrated_database, run_context)
    checkpoints = await _checkpoint_rows(migrated_database, run_context)
    assistant_message = assistant_messages[-1]
    model_checkpoint = next(
        row for row in checkpoints if row["phase"] == CheckpointPhase.MODEL_OUTPUT_COMMITTED.value
    )
    assert assistant_message["content"] == "forced summary"
    assert model_checkpoint["payload"]["message_id"] == assistant_message["id"]
    assert model_checkpoint["payload"]["output_digest"] == hashlib.sha256(b"forced summary").hexdigest()
    assert model_checkpoint["payload"]["cursor"] == model_checkpoint["payload"]["model_cursor"]
    assert any('"type":"finish"' in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_chat_model_output_checkpoint_failure_rolls_back_assistant_message_and_blocks_success(
    migrated_database,
    monkeypatch,
):
    import multiclaw.server as server
    from multiclaw.storage.repositories.workflow import WorkflowRepository

    original_insert = WorkflowRepository._insert_checkpoint

    async def fail_model_output_insert(self, lease, **kwargs):
        if kwargs.get("phase") == CheckpointPhase.MODEL_OUTPUT_COMMITTED.value:
            raise RuntimeError("model checkpoint failed")
        return await original_insert(self, lease, **kwargs)

    monkeypatch.setattr(WorkflowRepository, "_insert_checkpoint", fail_model_output_insert)

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "assistant-output-failure@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)
            runtime.agent.context_builder = _StaticReportContextBuilder(
                [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                ]
            )
            runtime.agent.router = _QueuedStreamRouter(
                stream_sequences=[[{"type": "token", "content": "streamed reply"}]]
            )
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)

        body = "".join([chunk async for chunk in response.body_iterator])

    payloads = _decode_sse_messages(body)
    run_event = next(payload for payload in payloads if payload["type"] == "data-run")
    run_context = context.for_run(run_event["data"]["session_id"], run_event["data"]["run_id"])
    assert await _assistant_chat_messages(migrated_database, run_context) == []
    assert [row["phase"] for row in await _checkpoint_rows(migrated_database, run_context)] == [
        CheckpointPhase.RUN_STARTED.value,
        CheckpointPhase.RUN_TERMINAL.value,
    ]
    assert await _run_status(migrated_database, run_context) == RunStatus.FAILED_TERMINAL.value
    assert '"type":"finish"' not in body
    assert '"type":"error"' in body


@pytest.mark.asyncio
async def test_chat_forced_summary_checkpoint_failure_rolls_back_assistant_message_and_blocks_success(
    migrated_database,
    monkeypatch,
):
    import multiclaw.server as server
    from multiclaw.storage.repositories.workflow import WorkflowRepository

    original_insert = WorkflowRepository._insert_checkpoint

    async def fail_model_output_insert(self, lease, **kwargs):
        if kwargs.get("phase") == CheckpointPhase.MODEL_OUTPUT_COMMITTED.value:
            raise RuntimeError("forced summary checkpoint failed")
        return await original_insert(self, lease, **kwargs)

    monkeypatch.setattr(WorkflowRepository, "_insert_checkpoint", fail_model_output_insert)

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "forced-summary-output-failure@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(request_context):
            runtime = await original_acquire(request_context)
            runtime.agent.context_builder = _StaticReportContextBuilder(
                [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                ]
            )
            runtime.agent.router = _QueuedStreamRouter(
                stream_sequences=[
                    [
                        {
                            "type": "tool_calls",
                            "calls": [{"id": "call-1", "name": "web_search", "arguments": {"query": "hello"}}],
                            "reasoning_content": "",
                        }
                    ],
                    [{"type": "token", "content": "forced summary"}],
                ]
            )
            runtime.agent._execute_tool_batch = AsyncMock(
                return_value=[
                    SimpleNamespace(
                        call_id="call-1",
                        name="web_search",
                        observation=SimpleNamespace(content="tool result"),
                    )
                ]
            )
            runtime.agent.settings.agent.max_tool_rounds = 1
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)

        body = "".join([chunk async for chunk in response.body_iterator])

    payloads = _decode_sse_messages(body)
    run_event = next(payload for payload in payloads if payload["type"] == "data-run")
    run_context = context.for_run(run_event["data"]["session_id"], run_event["data"]["run_id"])
    assert await _assistant_chat_messages(migrated_database, run_context) == []
    assert [row["phase"] for row in await _checkpoint_rows(migrated_database, run_context)] == [
        CheckpointPhase.RUN_STARTED.value,
        CheckpointPhase.RUN_TERMINAL.value,
    ]
    assert await _run_status(migrated_database, run_context) == RunStatus.FAILED_TERMINAL.value
    assert '"type":"finish"' not in body
    assert '"type":"error"' in body


@pytest.mark.asyncio
async def test_chat_heartbeats_active_run_lease_during_long_stream(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.storage.uow import TenantUnitOfWork
    from multiclaw.workflow.coordinator import WorkflowCoordinator
    from multiclaw.workflow.models import LeaseConflictError

    release = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context, run_lease):
        del user_input
        captured["context"] = context
        captured["run_lease"] = run_lease
        await release.wait()
        yield {"type": "done", "content": ""}

    with TestClient(server.app):
        monkeypatch.setattr(server.app.state.settings.workflow, "heartbeat_ms", 50)
        monkeypatch.setattr(server.app.state.settings.workflow, "lease_ttl_ms", 250)
        user_id, _ = await _seed_user(migrated_database, "heartbeat-lease@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request(
            {
                "type": "http",
                "app": server.app,
                "method": "POST",
                "path": "/api/chat",
                "headers": [],
            }
        )
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(
                server.ChatRequest(message="hello"),
                request,
                context,
                uow,
            )

        async def consume() -> None:
            async for _ in response.body_iterator:
                pass

        consume_task = asyncio.create_task(consume())
        for _ in range(30):
            if "context" in captured:
                break
            await asyncio.sleep(0.02)

        coordinator = WorkflowCoordinator(server.app.state.database, settings=server.app.state.settings)
        await asyncio.sleep(0.25)
        with pytest.raises(LeaseConflictError):
            await coordinator.acquire_run(captured["context"], "runtime-2")

        release.set()
        await consume_task


@pytest.mark.asyncio
async def test_chat_passes_run_lease_handle_with_refreshed_snapshot(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.storage.uow import TenantUnitOfWork
    from multiclaw.workflow.coordinator import WorkflowCoordinator
    from multiclaw.workflow.models import ExecutionStatus, StaleFenceError

    release = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context, run_lease, run_lease_handle):
        del user_input
        coordinator = WorkflowCoordinator(server.app.state.database, settings=server.app.state.settings)
        execution_id = str(uuid4())
        async with TenantUnitOfWork(
            server.app.state.database,
            context,
            workflow_settings=server.app.state.settings.workflow,
        ) as uow:
            await uow.conn.execute(
                insert(tool_executions).values(
                    execution_id=execution_id,
                    tenant_id=context.tenant_id,
                    workspace_id=context.workspace_id,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    approval_id=None,
                    tool_call_id=f"call-{execution_id}",
                    tool_name="echo",
                    tool_kind="builtin",
                    execution_status=ExecutionStatus.NOT_STARTED.value,
                    recovery_strategy="idempotent_retry",
                    idempotency_key=None,
                    input_payload_json="{}",
                    input_hash="3" * 64,
                    external_request_id=None,
                    result_ref=None,
                    result_digest=None,
                    schema_version=1,
                    version=1,
                    created_at=server.app.state.database.dialect.db_now_ms(),
                    updated_at=server.app.state.database.dialect.db_now_ms(),
                    finished_at=None,
                )
            )
        initial_snapshot = await run_lease_handle.current()
        captured["initial_version"] = initial_snapshot.version
        captured["initial_expiry"] = initial_snapshot.lease_expires_at
        await asyncio.sleep(0.16)
        refreshed = await run_lease_handle.current()
        captured["refreshed_version"] = refreshed.version
        captured["refreshed_expiry"] = refreshed.lease_expires_at
        with pytest.raises(StaleFenceError):
            await coordinator.write_checkpoint(
                run_lease,
                checkpoint_id=str(uuid4()),
                checkpoint_seq=1,
                phase="run",
                payload_json="{}",
                payload_hash="4" * 64,
                schema_version=1,
            )
        lease_after_transition = await run_lease_handle.use_current(
            lambda lease: coordinator.transition_execution(
                lease,
                execution_id,
                expected_status=ExecutionStatus.NOT_STARTED,
                expected_version=1,
                target=ExecutionStatus.REPLAYING,
            )
        )
        lease_after_transition = await run_lease_handle.use_current(
            lambda lease: coordinator.transition_execution(
                lease,
                execution_id,
                expected_status=ExecutionStatus.REPLAYING,
                expected_version=2,
                target=ExecutionStatus.EXECUTING,
            )
        )
        lease_after_transition = await run_lease_handle.use_current(
            lambda lease: coordinator.transition_execution(
                lease,
                execution_id,
                expected_status=ExecutionStatus.EXECUTING,
                expected_version=3,
                target=ExecutionStatus.SUCCEEDED,
            )
        )
        await run_lease_handle.use_current(
            lambda lease: coordinator.write_checkpoint(
                lease,
                checkpoint_id=str(uuid4()),
                checkpoint_seq=2,
                phase="run",
                payload_json="{}",
                payload_hash="5" * 64,
                schema_version=1,
                execution_id=execution_id,
            )
        )
        captured["transitioned_version"] = lease_after_transition.version
        release.set()
        yield {"type": "done", "content": ""}

    with TestClient(server.app):
        monkeypatch.setattr(server.app.state.settings.workflow, "heartbeat_ms", 50)
        monkeypatch.setattr(server.app.state.settings.workflow, "lease_ttl_ms", 120)
        user_id, _ = await _seed_user(migrated_database, "handle-lease@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            captured["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request(
            {
                "type": "http",
                "app": server.app,
                "method": "POST",
                "path": "/api/chat",
                "headers": [],
            }
        )
        async with TenantUnitOfWork(
            server.app.state.database,
            context,
            workflow_settings=server.app.state.settings.workflow,
        ) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)
            body = [chunk async for chunk in response.body_iterator]

    assert '"type":"error"' not in "".join(body)
    assert captured["refreshed_version"] > captured["initial_version"]
    assert captured["refreshed_expiry"] > captured["initial_expiry"]


@pytest.mark.asyncio
async def test_chat_client_cancel_stops_lease_heartbeat_updates(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.storage.uow import TenantUnitOfWork
    from multiclaw.workflow.coordinator import WorkflowCoordinator
    from multiclaw.workflow.models import RunStatus

    started = asyncio.Event()
    release = asyncio.Event()
    captured: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context, run_lease_handle):
        del user_input
        captured["context"] = context
        captured["run_lease_handle"] = run_lease_handle
        started.set()
        await release.wait()
        yield {"type": "done", "content": ""}

    with TestClient(server.app):
        monkeypatch.setattr(server.app.state.settings.workflow, "heartbeat_ms", 50)
        monkeypatch.setattr(server.app.state.settings.workflow, "lease_ttl_ms", 120)
        user_id, _ = await _seed_user(migrated_database, "cancel-heartbeat@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request({"type": "http", "app": server.app, "method": "POST", "path": "/api/chat", "headers": []})
        async with TenantUnitOfWork(
            server.app.state.database,
            context,
            workflow_settings=server.app.state.settings.workflow,
        ) as uow:
            response = await server.chat(server.ChatRequest(message="hello"), request, context, uow)
        async def consume() -> None:
            async for _ in response.body_iterator:
                pass

        consume_task = asyncio.create_task(consume())
        await started.wait()
        initial_version = (await captured["run_lease_handle"].current()).version
        await asyncio.sleep(0.18)
        mid_version = (await captured["run_lease_handle"].current()).version
        consume_task.cancel()
        await asyncio.gather(consume_task, return_exceptions=True)
        await response.body_iterator.aclose()
        await asyncio.sleep(0.18)
        final_snapshot = await captured["run_lease_handle"].current()
        await asyncio.sleep(0.18)
        stable_snapshot = await captured["run_lease_handle"].current()
        release.set()

    record = await WorkflowCoordinator(server.app.state.database, settings=server.app.state.settings).get_run(
        captured["context"]
    )
    assert mid_version > initial_version
    assert final_snapshot.version >= mid_version
    assert stable_snapshot.version == final_snapshot.version
    assert record is not None
    assert record.status is RunStatus.CANCELLED


def test_chat_returns_retryable_503_when_begin_run_rejects_unavailable_runtime(migrated_database, monkeypatch):
    import multiclaw.server as server

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_unavailable(context):
            runtime = await original_acquire(context)
            runtime.mark_unavailable()
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_unavailable)
        response = client.post("/api/chat", json={"message": "hello"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "900"
    assert response.json() == {"detail": "runtime temporarily unavailable"}


def test_chat_returns_429_when_tenant_run_quota_is_exhausted(migrated_database, monkeypatch):
    import multiclaw.server as server

    started = threading.Event()
    release = threading.Event()
    responses: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context, run_lease=None):
        del user_input, context, run_lease
        started.set()
        await asyncio.to_thread(release.wait)
        yield {"type": "done", "content": ""}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        monkeypatch.setattr(server.app.state.settings.runtime, "max_concurrent_runs_per_tenant", 1)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)

        def first_request() -> None:
            responses["first"] = client.post("/api/chat", json={"message": "first"})

        request_thread = threading.Thread(target=first_request)
        request_thread.start()
        assert started.wait(timeout=3)

        second = client.post("/api/chat", json={"message": "second"})
        responses["second"] = second

        release.set()
        request_thread.join(timeout=5)
        assert request_thread.is_alive() is False

    first = responses["first"]
    assert first.status_code == 200
    assert second.status_code == 429


def test_chat_runtime_signal_resets_after_stream_error(migrated_database, monkeypatch):
    import multiclaw.server as server

    started = threading.Event()
    holder: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context):
        del user_input, context
        started.set()
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            holder["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        response = client.post("/api/chat", json={"message": "hello"})

    assert started.is_set() is True
    runtime = holder["runtime"]
    assert runtime.active_executing_run_count == 0
    assert runtime.active_run_count == 0
    assert response.status_code == 200
    assert '"type":"error"' in response.text


@pytest.mark.asyncio
async def test_chat_runtime_signal_resets_after_client_disconnect(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.storage.uow import TenantUnitOfWork

    started = threading.Event()
    holder: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context):
        del user_input, context
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        yield {"type": "done", "content": ""}

    with TestClient(server.app) as client:
        original_acquire = server.app.state.runtime_pool.acquire
        user_id, _ = await _seed_user(migrated_database, "disconnect@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            holder["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request(
            {
                "type": "http",
                "app": server.app,
                "method": "POST",
                "path": "/api/chat",
                "headers": [],
            }
        )
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(
                server.ChatRequest(message="hello"),
                request,
                context,
                uow,
            )
            for _ in range(6):
                await anext(response.body_iterator)
                if started.is_set():
                    break
            pending_chunk = asyncio.create_task(anext(response.body_iterator))
            assert await asyncio.to_thread(started.wait, 3)
            pending_chunk.cancel()
            await asyncio.gather(pending_chunk, return_exceptions=True)
            await response.body_iterator.aclose()

    runtime = holder["runtime"]
    assert runtime.active_executing_run_count == 0
    assert runtime.active_run_count == 0


@pytest.mark.asyncio
async def test_chat_runtime_signal_resets_when_client_closes_after_first_chunk(
    migrated_database,
    monkeypatch,
):
    import multiclaw.server as server
    from multiclaw.storage.uow import TenantUnitOfWork

    holder: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context):
        del user_input, context
        yield {"type": "done", "content": ""}

    with TestClient(server.app):
        user_id, _ = await _seed_user(migrated_database, "first-chunk-close@example.com")
        async with AuthUnitOfWork(migrated_database) as auth_uow:
            user = await auth_uow.users.get_by_id(user_id)
            assert user is not None
            workspace_id = user.default_workspace_id
            assert workspace_id is not None
        context = TenantContext(user_id, workspace_id)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            holder["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        request = Request(
            {
                "type": "http",
                "app": server.app,
                "method": "POST",
                "path": "/api/chat",
                "headers": [],
            }
        )
        async with TenantUnitOfWork(server.app.state.database, context) as uow:
            response = await server.chat(
                server.ChatRequest(message="hello"),
                request,
                context,
                uow,
            )
            runtime = holder["runtime"]
            assert runtime.active_executing_run_count == 1
            assert runtime.active_run_count == 1
            await anext(response.body_iterator)
            await response.body_iterator.aclose()

    runtime = holder["runtime"]
    assert runtime.active_executing_run_count == 0
    assert runtime.active_run_count == 0


def test_chat_closes_run_lease_when_streaming_response_constructor_raises(
    migrated_database,
    monkeypatch,
):
    import multiclaw.server as server

    captured: dict[str, object] = {}

    class BoomStreamingResponse:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("constructor failed")

    async def fake_handle_message_stream(user_input: str, *, context):
        del user_input, context
        yield {"type": "done", "content": ""}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        original_acquire = server.app.state.runtime_pool.acquire

        async def acquire_and_patch(context):
            runtime = await original_acquire(context)
            monkeypatch.setattr(runtime.agent, "handle_message_stream", fake_handle_message_stream)
            captured["runtime"] = runtime
            return runtime

        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", acquire_and_patch)
        monkeypatch.setattr(server, "StreamingResponse", BoomStreamingResponse)

        with pytest.raises(RuntimeError, match="constructor failed"):
            client.post("/api/chat", json={"message": "hello"})

    runtime = captured["runtime"]
    assert runtime.active_executing_run_count == 0
    assert runtime.active_run_count == 0
