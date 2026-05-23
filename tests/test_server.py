from fastapi.testclient import TestClient


def test_sessions_endpoint_lists_created_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        listed = client.get("/sessions").json()

    assert created["title"] == "Alpha"
    assert [session["id"] for session in listed] == [created["id"]]


def test_session_lifecycle_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        renamed = client.patch(
            f"/sessions/{created['id']}",
            json={"title": "Beta"},
        ).json()
        archived = client.post(f"/sessions/{created['id']}/archive").json()
        listed = client.get("/sessions").json()
        all_sessions = client.get("/sessions?include_archived=true").json()
        restored = client.post(f"/sessions/{created['id']}/restore").json()

    assert renamed["title"] == "Beta"
    assert archived["status"] == "archived"
    assert listed == []
    assert [session["id"] for session in all_sessions] == [created["id"]]
    assert restored["status"] == "active"


def test_chat_without_session_emits_session_event(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        response = client.post("/chat", json={"message": "hello"})

    body = response.text
    assert '"type": "session"' in body
    assert '"session_id":' in body


def test_chat_rejects_archived_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        client.post(f"/sessions/{created['id']}/archive")
        response = client.post("/chat", json={"message": "hello", "session_id": created["id"]})

    assert response.status_code == 409


def test_delete_session_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        response = client.delete(f"/sessions/{created['id']}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    # Verify session is gone
    with TestClient(app) as client:
        listed = client.get("/sessions").json()
    assert created["id"] not in [s["id"] for s in listed]


def test_get_messages_endpoint_returns_empty_for_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        sid = created["id"]

        response = client.get(f"/sessions/{sid}/messages")
        assert response.status_code == 200
        assert response.json() == []


def test_get_messages_endpoint_respects_limit_param(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        created = client.post("/sessions", json={"title": "Alpha"}).json()
        sid = created["id"]

        response = client.get(f"/sessions/{sid}/messages?limit=10")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
