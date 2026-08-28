import importlib
import json

import pytest

from multiclaw.session import ChatSession, InvalidSessionTitleError, SessionStatus


def test_chat_session_defaults_include_full_scope() -> None:
    session = ChatSession(
        tenant_id="00000000-0000-0000-0000-000000000001",
        workspace_id="00000000-0000-0000-0000-000000000002",
    )

    assert len(session.id) == 36
    assert session.tenant_id == "00000000-0000-0000-0000-000000000001"
    assert session.workspace_id == "00000000-0000-0000-0000-000000000002"
    assert session.title == "New Chat"
    assert session.status is SessionStatus.ACTIVE
    assert isinstance(session.created_at, int)
    assert isinstance(session.updated_at, int)
    assert session.last_message_at is None
    assert session.metadata == {}


def test_chat_session_normalizes_title_and_enforces_bounds() -> None:
    session = ChatSession(
        tenant_id="00000000-0000-0000-0000-000000000001",
        workspace_id="00000000-0000-0000-0000-000000000002",
        title="  Research notes  ",
    )

    assert session.title == "Research notes"

    with pytest.raises(ValueError, match="title"):
        ChatSession(
            tenant_id="00000000-0000-0000-0000-000000000001",
            workspace_id="00000000-0000-0000-0000-000000000002",
            title=" ",
        )

    with pytest.raises(ValueError, match="title"):
        ChatSession(
            tenant_id="00000000-0000-0000-0000-000000000001",
            workspace_id="00000000-0000-0000-0000-000000000002",
            title="x" * 121,
        )


def test_chat_session_from_row_parses_metadata_json_mapping() -> None:
    session = ChatSession.from_row(
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "tenant_id": "22222222-2222-2222-2222-222222222222",
            "workspace_id": "33333333-3333-3333-3333-333333333333",
            "title": "Scoped session",
            "status": "archived",
            "created_at": 10,
            "updated_at": 20,
            "last_message_at": 30,
            "metadata_json": json.dumps({"topic": "alpha"}, sort_keys=True),
        }
    )

    assert session == ChatSession(
        id="11111111-1111-1111-1111-111111111111",
        tenant_id="22222222-2222-2222-2222-222222222222",
        workspace_id="33333333-3333-3333-3333-333333333333",
        title="Scoped session",
        status=SessionStatus.ARCHIVED,
        created_at=10,
        updated_at=20,
        last_message_at=30,
        metadata={"topic": "alpha"},
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "tenant_id": "",
                "workspace_id": "33333333-3333-3333-3333-333333333333",
            },
            "tenant_id",
        ),
        (
            {
                "tenant_id": "22222222-2222-2222-2222-222222222222",
                "workspace_id": "",
            },
            "workspace_id",
        ),
    ],
)
def test_chat_session_rejects_empty_scope(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ChatSession(**kwargs)


def test_session_package_exports_scoped_api_only() -> None:
    import multiclaw.session as session_package

    assert session_package.ChatSession is ChatSession
    assert session_package.InvalidSessionTitleError is InvalidSessionTitleError
    assert session_package.SessionStatus is SessionStatus
    assert not hasattr(session_package, "SqliteSessionStore")
    assert importlib.util.find_spec("multiclaw.session.sqlite") is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("multiclaw.session.sqlite")
