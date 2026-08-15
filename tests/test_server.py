import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from alembic import command
from fastapi.testclient import TestClient
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
from multiclaw.storage import Database
from multiclaw.storage.uow import AuthUnitOfWork
from multiclaw.tenancy import TenantContext, WorkspaceResolver
from multiclaw.tools.code_exec import CodeExecToolBuilder
from multiclaw.tools.shell import ShellToolBuilder
from sandbox_fakes import ReadyRecordingSandboxController, UnavailableSandboxController


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


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
    database = asyncio.run(_create_database(tmp_path))
    try:
        yield database
    finally:
        asyncio.run(database.dispose())


def _make_auth_cookie(app, database: Database, *, email: str = "test@example.com") -> dict:
    secret = app.state.auth_store.jwt_secret
    user_id, auth_epoch = asyncio.run(_seed_user(database, email))
    token = jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "auth_epoch": auth_epoch,
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        secret,
        algorithm="HS256",
    )
    return {"token": token}


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


def test_approve_returns_retryable_503_when_runtime_pool_is_at_capacity(migrated_database, monkeypatch):
    import multiclaw.server as server
    from multiclaw.runtime.pool import RuntimeCapacityError

    async def fail_acquire(context):
        del context
        raise RuntimeCapacityError(7)

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app, migrated_database)
        monkeypatch.setattr(server.app.state.runtime_pool, "acquire", fail_acquire)
        response = client.post("/api/approve", json={"request_id": "req-1", "approved": True})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "7"
    assert response.json() == {"detail": "runtime temporarily unavailable"}


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

        async def dispose(self) -> None:
            self.dispose_calls += 1

    class FakeFactory:
        def __init__(self) -> None:
            self.settings = SimpleNamespace(
                database=SimpleNamespace(path=str(tmp_path / "app.db")),
                runtime=SimpleNamespace(max_resident_tenants=1, idle_ttl_seconds=1),
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


def test_lifespan_disposes_database_when_auth_store_initialize_fails(tmp_path, monkeypatch):
    import multiclaw.server as server_module

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
        probe_startup=lambda: (SimpleNamespace(), ()),
        create=None,
    )

    async def _boom(self) -> None:
        raise RuntimeError("auth store init failed")

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module.AuthStore, "initialize", _boom)

    with pytest.raises(RuntimeError, match="auth store init failed"):
        with TestClient(server_module.app):
            pass

    assert fake_database.dispose_calls == 1


def test_lifespan_preserves_primary_error_when_auth_store_close_fails(tmp_path, monkeypatch):
    import multiclaw.server as server_module

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
        probe_startup=lambda: (SimpleNamespace(), ()),
        create=None,
    )

    async def _boom(self) -> None:
        raise RuntimeError("auth store init failed")

    async def _close_boom(self) -> None:
        raise RuntimeError("auth store close failed")

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module.AuthStore, "initialize", _boom)
    monkeypatch.setattr(server_module.AuthStore, "close", _close_boom)

    with pytest.raises(RuntimeError, match="auth store init failed") as exc_info:
        with TestClient(server_module.app):
            pass

    assert fake_database.dispose_calls == 1
    assert exc_info.value.__notes__
    assert any("auth_store.close" in note and "auth store close failed" in note for note in exc_info.value.__notes__)


def test_lifespan_preserves_primary_error_when_database_dispose_fails(tmp_path, monkeypatch):
    import multiclaw.server as server_module

    class FakeDatabase:
        def __init__(self) -> None:
            self.dispose_calls = 0

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

    async def _boom(self) -> None:
        raise RuntimeError("auth store init failed")

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module.AuthStore, "initialize", _boom)

    with pytest.raises(RuntimeError, match="auth store init failed") as exc_info:
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
        ),
        database=FakeDatabase(),
        workspace_resolver=SimpleNamespace(root=tmp_path),
        probe_startup=lambda: (SimpleNamespace(ready=True), ()),
        create=None,
    )

    async def fake_initialize(self) -> None:
        self.jwt_secret = "secret"

    async def fake_close(self) -> None:
        call_order.append("auth_store.close")
        raise RuntimeError("auth store close failed")

    monkeypatch.setattr(server_module, "create_runtime_factory", lambda: fake_factory)
    monkeypatch.setattr(server_module, "RuntimePool", FailingRuntimePool)
    monkeypatch.setattr(server_module.AuthStore, "initialize", fake_initialize)
    monkeypatch.setattr(server_module.AuthStore, "close", fake_close)

    with pytest.raises(RuntimeError, match="auth store close failed") as error:
        with TestClient(server_module.app):
            pass

    assert call_order == ["auth_store.close", "runtime_pool.close", "database.dispose"]
    assert error.value.__notes__
    assert any("runtime_pool.close" in note and "runtime pool close failed" in note for note in error.value.__notes__)
    assert any("database.dispose" in note and "database dispose failed" in note for note in error.value.__notes__)


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
    cancelled = threading.Event()
    holder: dict[str, object] = {}

    async def fake_handle_message_stream(user_input: str, *, context):
        del user_input, context
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
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
            await anext(response.body_iterator)
            await anext(response.body_iterator)
            await anext(response.body_iterator)
            pending_chunk = asyncio.create_task(anext(response.body_iterator))
            assert await asyncio.to_thread(started.wait, 3)
            pending_chunk.cancel()
            await asyncio.gather(pending_chunk, return_exceptions=True)
            await response.body_iterator.aclose()

        assert await asyncio.to_thread(cancelled.wait, 3)

    runtime = holder["runtime"]
    assert runtime.active_executing_run_count == 0
    assert runtime.active_run_count == 0
