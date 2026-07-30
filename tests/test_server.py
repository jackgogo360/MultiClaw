from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from multiclaw.mcp.types import ToolInfo


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


def test_register_mcp_tools_installs_refresh_callback_before_connect(monkeypatch):
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
        lambda path=None: {"demo server/v1": object()},
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
    )

    assert events == ["set_callback", "connect", "callback", "get_server_states"]
    assert [builder.name for builder in registry.list_all()] == [
        "mcp__demo_server_v1__read_fresh",
    ]


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
