from pathlib import Path

from fastapi.testclient import TestClient


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"


def test_api_auth_me_is_public(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    from multiclaw.server import app

    with TestClient(app) as client:
        response = client.get("/api/auth/me")

    assert response.status_code == 200


def test_api_auth_send_code_uses_public_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_EMAIL__PROVIDER", "resend")
    monkeypatch.setenv("MULTICLAW_RESEND__MOCK", "true")
    from multiclaw.server import app

    with TestClient(app) as client:
        response = client.post("/api/auth/send-code", json={"email": "test@example.com"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
