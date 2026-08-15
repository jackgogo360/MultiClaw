import json

from multiclaw.session import ChatSession, SessionStatus


def test_chat_session_from_row_handles_missing_last_message() -> None:
    session = ChatSession.from_row(
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "tenant_id": "22222222-2222-2222-2222-222222222222",
            "workspace_id": "33333333-3333-3333-3333-333333333333",
            "title": "New Chat",
            "status": "active",
            "created_at": 1,
            "updated_at": 2,
            "last_message_at": None,
            "metadata_json": json.dumps({}, sort_keys=True),
        }
    )

    assert session.status is SessionStatus.ACTIVE
    assert session.last_message_at is None


def test_chat_session_model_dump_uses_scoped_fields_only() -> None:
    session = ChatSession(
        id="11111111-1111-1111-1111-111111111111",
        tenant_id="22222222-2222-2222-2222-222222222222",
        workspace_id="33333333-3333-3333-3333-333333333333",
        title="Scoped session",
        status=SessionStatus.ACTIVE,
        created_at=1,
        updated_at=2,
        last_message_at=None,
        metadata={"topic": "alpha"},
    )

    dumped = session.model_dump()

    assert dumped["tenant_id"] == "22222222-2222-2222-2222-222222222222"
    assert dumped["workspace_id"] == "33333333-3333-3333-3333-333333333333"
    assert "user_id" not in dumped
