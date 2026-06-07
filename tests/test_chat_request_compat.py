from datetime import datetime, timedelta, timezone

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


def test_chat_accepts_ai_sdk_message_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    import multiclaw.server as server

    captured: list[tuple[str, str]] = []

    async def fake_handle_message_stream(user_input: str, session_id: str = ""):
        captured.append((user_input, session_id))
        yield {"type": "token", "content": "ok"}
        yield {"type": "done", "content": "ok"}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app)
        monkeypatch.setattr(server.agent, "handle_message_stream", fake_handle_message_stream)

        response = client.post(
            "/api/chat",
            json={
                "id": "thread-1",
                "trigger": "submit-message",
                "messages": [
                    {"role": "assistant", "content": "previous"},
                    {"role": "user", "content": "hello from sdk"},
                ],
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type":"data-session"' in response.text
    assert '"type":"text-delta"' in response.text
    assert captured[0][0] == "hello from sdk"


def test_chat_accepts_text_parts_from_ai_sdk_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    import multiclaw.server as server

    captured: list[str] = []

    async def fake_handle_message_stream(user_input: str, session_id: str = ""):
        captured.append(user_input)
        yield {"type": "done", "content": ""}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app)
        monkeypatch.setattr(server.agent, "handle_message_stream", fake_handle_message_stream)

        response = client.post(
            "/api/chat",
            json={
                "id": "thread-1",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "text", "text": " world"},
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert captured == ["hello world"]


def test_chat_stream_emits_step_boundaries_for_tool_rounds(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
    import multiclaw.server as server

    async def fake_handle_message_stream(user_input: str, session_id: str = ""):
        yield {"type": "tool_call", "call_id": "call_1", "name": "web_search", "arguments": {"query": "DeepSeek jobs"}}
        yield {"type": "tool_result", "call_id": "call_1", "name": "web_search", "content": "Search results"}
        yield {"type": "token", "content": "Final summary"}
        yield {"type": "done", "content": "Final summary"}

    with TestClient(server.app) as client:
        client.cookies = _make_auth_cookie(server.app)
        monkeypatch.setattr(server.agent, "handle_message_stream", fake_handle_message_stream)

        response = client.post(
            "/api/chat",
            json={
                "id": "thread-1",
                "messages": [{"role": "user", "content": "latest DeepSeek jobs"}],
            },
        )

    assert response.status_code == 200
    assert response.text.count('"type":"start-step"') == 2
    assert response.text.count('"type":"finish-step"') == 2
    assert '"type":"tool-input-available"' in response.text
    assert '"type":"tool-output-available"' in response.text
