from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient


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
