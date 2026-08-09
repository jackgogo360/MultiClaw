from datetime import datetime, timedelta, timezone
import logging

import jwt
from fastapi.testclient import TestClient


def _make_auth_cookie(app) -> dict:
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


def test_request_logging_records_public_route(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    caplog.set_level(logging.INFO, logger="multiclaw")

    with TestClient(app) as client:
        response = client.get("/auth/me")

    assert response.status_code == 200
    assert "HTTP GET /auth/me -> 200" in caplog.text


def test_request_logging_records_chat_validation_failure(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    from multiclaw.server import app

    caplog.set_level(logging.INFO, logger="multiclaw")

    with TestClient(app) as client:
        client.cookies = _make_auth_cookie(app)
        response = client.post("/api/chat", json={})

    assert response.status_code == 422
    assert "HTTP POST /api/chat -> 422" in caplog.text
