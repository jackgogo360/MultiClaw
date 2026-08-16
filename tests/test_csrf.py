import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


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
