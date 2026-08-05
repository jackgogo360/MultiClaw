from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from multiclaw.events import Event
from multiclaw.governance.sandbox.models import SandboxProbeResult, SandboxReadiness
from multiclaw.mcp.types import HTTPServerConfig, InProcessServerConfig, StdioServerConfig
from multiclaw.mcp.types import ToolInfo
from multiclaw.tools.code_exec import CodeExecToolBuilder
from multiclaw.tools.shell import ShellToolBuilder
from sandbox_fakes import ReadyRecordingSandboxController, UnavailableSandboxController


def _make_auth_cookie(app) -> dict:
    """Generate a valid JWT cookie for test requests."""
    secret = app.state.auth_store.jwt_secret
    token = jwt.encode(
        {
            "sub": "test-user-id",
            "email": "test@example.com",
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        secret,
        algorithm="HS256",
    )
    return {"token": token}


def test_sessions_endpoint_lists_created_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        listed = client.get("/api/sessions").json()

    assert created["title"] == "Alpha"
    assert [session["id"] for session in listed] == [created["id"]]


def test_session_lifecycle_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app)
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


def test_chat_without_session_emits_session_event(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app)
        response = client.post("/api/chat", json={"message": "hello"})

    body = response.text
    assert '"type":"data-session"' in body
    assert '"id":"' in body


def test_chat_rejects_archived_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        client.post(f"/api/sessions/{created['id']}/archive")
        response = client.post(
            "/api/chat",
            json={"message": "hello", "session_id": created["id"]},
        )

    assert response.status_code == 409


def test_delete_session_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        response = client.delete(f"/api/sessions/{created['id']}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    # Verify session is gone
    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app)
        listed = client.get("/api/sessions").json()
    assert created["id"] not in [s["id"] for s in listed]


def test_get_messages_endpoint_returns_empty_for_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app)
        created = client.post("/api/sessions", json={"title": "Alpha"}).json()
        sid = created["id"]

        response = client.get(f"/api/sessions/{sid}/messages")
        assert response.status_code == 200
        assert response.json() == []


def test_get_messages_endpoint_respects_limit_param(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app)
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

    real_create_agent = server_module.create_agent
    controller = ReadyRecordingSandboxController(workspace_root=tmp_path)

    def _create_agent(*, sandbox_controller=None):
        del sandbox_controller
        return real_create_agent(sandbox_controller=controller)

    monkeypatch.setattr(server_module, "create_agent", _create_agent)

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

    real_create_agent = server_module.create_agent
    controller = _LeakyUnavailableSandboxController(tmp_path)

    def _create_agent(*, sandbox_controller=None):
        del sandbox_controller
        return real_create_agent(sandbox_controller=controller)

    monkeypatch.setattr(server_module, "create_agent", _create_agent)

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


def test_register_mcp_tools_skips_unready_stdio_but_keeps_remote(
    tmp_path,
    monkeypatch,
    caplog,
):
    from multiclaw.mcp.types import ServerState, ServerStatus
    from multiclaw.server import _register_mcp_tools
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

    manager = FakeManager()
    registry = ToolRegistry()
    with caplog.at_level("INFO"):
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
    assert "transport_remote_unsandboxed=true" in caplog.text


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


def test_register_mcp_tools_keeps_in_process_in_unsafe_mode(tmp_path, monkeypatch, caplog):
    from multiclaw.mcp.types import ServerState, ServerStatus
    from multiclaw.server import _register_mcp_tools
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
    manager = FakeManager()
    registry = ToolRegistry()
    with caplog.at_level("WARNING"):
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
    assert "unsafe" in caplog.text
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


def test_lifespan_still_closes_controller_when_mcp_stop_fails(tmp_path, monkeypatch, caplog):
    import multiclaw.server as server_module

    class FakeController(ReadyRecordingSandboxController):
        def __init__(self) -> None:
            super().__init__(workspace_root=tmp_path)
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class FakeManager:
        def __init__(self) -> None:
            self.stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1
            raise RuntimeError(f"stop leaked path {tmp_path}")

    controller = FakeController()
    manager = FakeManager()
    real_create_agent = server_module.create_agent

    def _create_agent(*, sandbox_controller=None):
        del sandbox_controller
        runtime_agent = real_create_agent(sandbox_controller=controller)
        runtime_agent.mcp_manager = manager
        return runtime_agent

    monkeypatch.setattr(server_module, "create_agent", _create_agent)

    with caplog.at_level("WARNING"):
        with TestClient(server_module.app):
            pass

    assert manager.stop_calls == 1
    assert controller.close_calls == 1
    assert str(tmp_path) not in caplog.text


def test_create_agent_passes_configured_mcp_profile_name(tmp_path, monkeypatch):
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
        server_module.create_agent(
            sandbox_controller=ReadyRecordingSandboxController(workspace_root=tmp_path)
        )
    finally:
        monkeypatch.setattr(server_module, "_register_mcp_tools", real_register)

    assert captured["mcp_profile_name"] == "custom_mcp_profile"


def test_create_agent_passes_configured_shell_profile_and_controller(tmp_path, monkeypatch):
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

    controller = ProfileController(workspace_root=tmp_path)
    agent = server_module.create_agent(sandbox_controller=controller)
    shell_builder = agent.registry.get("shell")

    assert isinstance(shell_builder, ShellToolBuilder)
    assert shell_builder.sandbox_controller is controller
    assert shell_builder.profile_name == "custom_shell_profile"


def test_create_agent_passes_configured_code_exec_profile_and_controller(tmp_path, monkeypatch):
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

    controller = ProfileController(workspace_root=tmp_path)
    agent = server_module.create_agent(sandbox_controller=controller)
    code_exec_builder = agent.registry.get("code_exec")

    assert isinstance(code_exec_builder, CodeExecToolBuilder)
    assert code_exec_builder.sandbox_controller is controller
    assert code_exec_builder.profile_name == "custom_code_profile"


@pytest.mark.parametrize(
    ("allow_private_networks", "expected"),
    [
        (None, False),
        ("true", True),
    ],
)
def test_create_agent_wires_web_fetch_private_network_flag(
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

    from multiclaw.server import create_agent
    from multiclaw.tools.web_fetch import WebFetchToolBuilder

    agent = create_agent()
    web_fetch_builder = agent.registry.get("web_fetch")

    assert isinstance(web_fetch_builder, WebFetchToolBuilder)
    assert web_fetch_builder.allow_private_networks is expected
