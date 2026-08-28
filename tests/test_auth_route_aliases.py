from pathlib import Path

from fastapi.testclient import TestClient
from alembic import command

from multiclaw.cli import alembic_config


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"


def _migrate_database(tmp_path: Path) -> None:
    command.upgrade(alembic_config(database_url=_sqlite_url(tmp_path)), "head")


def _prime_csrf(client: TestClient) -> None:
    client.cookies.set("csrf_token", "test-csrf-token")
    client.headers.update(
        {
            "Origin": "http://testserver",
            "X-CSRF-Token": "test-csrf-token",
        }
    )


def _latest_cookie_value(client: TestClient, name: str) -> str | None:
    value: str | None = None
    for cookie in client.cookies.jar:
        if cookie.name == name:
            value = cookie.value
    return value


def test_api_auth_me_is_public(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "route-alias-jwt-key-material-1234567890")
    _migrate_database(tmp_path)
    from multiclaw.server import app

    with TestClient(app) as client:
        response = client.get("/api/auth/me")

    assert response.status_code == 200


def test_api_auth_send_code_uses_public_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "route-alias-jwt-key-material-1234567890")
    monkeypatch.setenv("MULTICLAW_EMAIL__PROVIDER", "resend")
    monkeypatch.setenv("MULTICLAW_RESEND__MOCK", "true")
    _migrate_database(tmp_path)
    from multiclaw.server import app

    with TestClient(app) as client:
        _prime_csrf(client)
        response = client.post("/api/auth/send-code", json={"email": "test@example.com"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_api_auth_csrf_public_alias_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "route-alias-jwt-key-material-1234567890")
    _migrate_database(tmp_path)
    from multiclaw.server import app

    with TestClient(app) as client:
        response = client.get("/api/auth/csrf")

    assert response.status_code == 200
    assert response.json()["token"] == client.cookies.get("csrf_token")


def test_api_auth_deletion_recovery_verify_uses_public_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", _sqlite_url(tmp_path))
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "route-alias-jwt-key-material-1234567890")
    monkeypatch.setenv("MULTICLAW_EMAIL__PROVIDER", "resend")
    monkeypatch.setenv("MULTICLAW_RESEND__MOCK", "true")
    _migrate_database(tmp_path)
    from multiclaw.server import app

    with TestClient(app) as client:
        _prime_csrf(client)
        client.app.state.auth_forced_code = "654321"
        assert client.post("/api/auth/send-code", json={"email": "alias-recovery@example.com"}).status_code == 200
        assert (
            client.post(
                "/api/auth/verify",
                json={"email": "alias-recovery@example.com", "code": "654321"},
            ).status_code
            == 200
        )
        client.headers["X-CSRF-Token"] = _latest_cookie_value(client, "csrf_token")
        assert client.post("/api/account/deletion").status_code == 200
        csrf_response = client.get("/api/auth/csrf")
        assert csrf_response.status_code == 200
        client.headers["X-CSRF-Token"] = _latest_cookie_value(client, "csrf_token")
        client.app.state.auth_forced_code = "112233"
        send_response = client.post(
            "/api/auth/deletion-recovery/send-code",
            json={"email": "alias-recovery@example.com"},
        )
        verify_response = client.post(
            "/api/auth/deletion-recovery/verify",
            json={"email": "alias-recovery@example.com", "code": "112233"},
        )

    assert send_response.status_code == 200
    assert verify_response.status_code == 200
    assert any(cookie.startswith("recovery_token=") for cookie in verify_response.headers.get_list("set-cookie"))
