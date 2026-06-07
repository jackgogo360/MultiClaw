from fastapi.testclient import TestClient


def test_api_auth_me_is_public(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    with TestClient(app) as client:
        response = client.get("/api/auth/me")

    assert response.status_code == 200


def test_api_auth_send_code_uses_public_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_EMAIL__PROVIDER", "resend")
    monkeypatch.setenv("MULTICLAW_RESEND__MOCK", "true")
    from multiclaw.server import app

    with TestClient(app) as client:
        response = client.post("/api/auth/send-code", json={"email": "test@example.com"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
