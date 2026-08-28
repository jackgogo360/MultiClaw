import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from alembic import command

from multiclaw.cli import alembic_config


def _migrate_database(tmp_path) -> None:
    command.upgrade(
        alembic_config(database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"),
        "head",
    )


def test_public_csrf_route_sets_matching_cookie_and_body(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "csrf-jwt-key-material-1234567890")
    import multiclaw.server as server

    with TestClient(server.app) as client:
        response = client.get("/auth/csrf")

    assert response.status_code == 200
    assert response.json()["token"] == client.cookies.get("csrf_token")


@pytest.mark.parametrize(
    ("headers", "cookies"),
    [
        ({}, {"csrf_token": "abc"}),
        ({"Origin": "http://testserver"}, {}),
        ({"Origin": "http://testserver"}, {"csrf_token": "abc"}),
    ],
)
def test_mutations_require_origin_cookie_and_header(tmp_path, monkeypatch, headers, cookies):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "csrf-jwt-key-material-1234567890")
    monkeypatch.setenv("MULTICLAW_EMAIL__PROVIDER", "resend")
    monkeypatch.setenv("MULTICLAW_RESEND__MOCK", "true")
    import multiclaw.server as server

    with TestClient(server.app) as client:
        response = client.post(
            "/auth/send-code",
            json={"email": "csrf@example.com"},
            headers=headers,
            cookies=cookies,
        )

    assert response.status_code == 403


def test_mutations_reject_untrusted_origin_even_with_matching_double_submit(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "csrf-jwt-key-material-1234567890")
    monkeypatch.setenv("MULTICLAW_EMAIL__PROVIDER", "resend")
    monkeypatch.setenv("MULTICLAW_RESEND__MOCK", "true")
    import multiclaw.server as server

    with TestClient(server.app) as client:
        response = client.post(
            "/auth/send-code",
            json={"email": "csrf@example.com"},
            headers={"Origin": "https://evil.example", "X-CSRF-Token": "abc"},
            cookies={"csrf_token": "abc"},
        )

    assert response.status_code == 403


def test_mutations_accept_trusted_referer_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "csrf-jwt-key-material-1234567890")
    monkeypatch.setenv("MULTICLAW_EMAIL__PROVIDER", "resend")
    monkeypatch.setenv("MULTICLAW_RESEND__MOCK", "true")
    _migrate_database(tmp_path)
    import multiclaw.server as server

    with TestClient(server.app) as client:
        response = client.post(
            "/auth/send-code",
            json={"email": "csrf@example.com"},
            headers={"Referer": "http://testserver/settings", "X-CSRF-Token": "abc"},
            cookies={"csrf_token": "abc"},
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "header_name",
    ["Origin", "Referer"],
)
def test_mutations_reject_untrusted_host_confusion(tmp_path, monkeypatch, header_name):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "csrf-jwt-key-material-1234567890")
    monkeypatch.setenv("MULTICLAW_EMAIL__PROVIDER", "resend")
    monkeypatch.setenv("MULTICLAW_RESEND__MOCK", "true")
    _migrate_database(tmp_path)
    import multiclaw.server as server

    value = "http://testserver.evil" if header_name == "Origin" else "http://testserver.evil/path"
    with TestClient(server.app) as client:
        response = client.post(
            "/auth/send-code",
            json={"email": "csrf@example.com"},
            headers={header_name: value, "X-CSRF-Token": "abc"},
            cookies={"csrf_token": "abc"},
        )

    assert response.status_code == 403


def test_csrf_cookie_uses_secure_samesite_and_is_not_httponly_in_production(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "csrf-jwt-key-material-1234567890")
    monkeypatch.setenv("MULTICLAW_APP__ALLOWED_ORIGINS", "[\"https://app.example\"]")
    _migrate_database(tmp_path)
    import multiclaw.server as server

    with TestClient(server.app, base_url="https://app.example") as client:
        response = client.get("/auth/csrf")

    set_cookie = response.headers["set-cookie"]
    assert response.status_code == 200
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "HttpOnly" not in set_cookie


def test_session_cookie_is_secure_samesite_and_httponly_in_production(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "csrf-jwt-key-material-1234567890")
    monkeypatch.setenv("MULTICLAW_EMAIL__PROVIDER", "resend")
    monkeypatch.setenv("MULTICLAW_RESEND__MOCK", "true")
    monkeypatch.setenv("MULTICLAW_APP__ALLOWED_ORIGINS", "[\"https://app.example\"]")
    _migrate_database(tmp_path)
    import multiclaw.server as server

    with TestClient(server.app, base_url="https://app.example") as client:
        client.cookies.set("csrf_token", "abc")
        client.app.state.auth_forced_code = "654321"
        send_response = client.post(
            "/auth/send-code",
            json={"email": "cookie@example.com"},
            headers={"Origin": "https://app.example", "X-CSRF-Token": "abc"},
        )
        assert send_response.status_code == 200
        verify_response = client.post(
            "/auth/verify",
            json={"email": "cookie@example.com", "code": "654321"},
            headers={"Origin": "https://app.example", "X-CSRF-Token": "abc"},
        )

    cookies = verify_response.headers.get_list("set-cookie")
    session_cookie = next(value for value in cookies if value.startswith("token="))
    csrf_cookie = next(value for value in cookies if value.startswith("csrf_token="))
    assert "Secure" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "Secure" in csrf_cookie
    assert "SameSite=lax" in csrf_cookie
    assert "HttpOnly" not in csrf_cookie


@pytest.mark.parametrize("path", ["/api/account/deletion", "/api/account/deletion/recover"])
def test_credentialed_mutation_preflight_returns_204_with_trusted_cors_headers(
    tmp_path,
    monkeypatch,
    path: str,
):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "csrf-jwt-key-material-1234567890")
    _migrate_database(tmp_path)
    import multiclaw.server as server

    with TestClient(server.app) as client:
        response = client.options(
            path,
            headers={
                "Origin": "http://testserver",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "http://testserver"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"


def test_credentialed_mutation_preflight_with_untrusted_origin_omits_allow_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__DRIVER", "sqlite")
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("MULTICLAW_DATABASE__URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MULTICLAW_AUTH_JWT_SIGNING_KEY", "csrf-jwt-key-material-1234567890")
    _migrate_database(tmp_path)
    import multiclaw.server as server

    with TestClient(server.app) as client:
        response = client.options(
            "/api/account/deletion/recover",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 204
    assert "Access-Control-Allow-Origin" not in response.headers


def test_csrf_validation_uses_constant_time_compare():
    from multiclaw.security import csrf as csrf_module

    calls: list[tuple[str, str]] = []
    original = csrf_module.secrets.compare_digest

    def tracking(left: str, right: str) -> bool:
        calls.append((left, right))
        return original(left, right)

    csrf_module.secrets.compare_digest = tracking
    try:
        assert csrf_module.tokens_match("abc", "abc") is True
    finally:
        csrf_module.secrets.compare_digest = original

    assert calls == [("abc", "abc")]


def test_cors_credentials_reject_wildcard_origin_configuration():
    from multiclaw.server import _validate_allowed_origins

    with pytest.raises(ValueError, match="wildcard"):
        _validate_allowed_origins({"*"})
