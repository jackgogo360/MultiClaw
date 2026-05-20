import pytest


@pytest.mark.asyncio
async def test_create_and_list_active_sessions(tmp_path):
    from multiclaw.session import SessionStatus, SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))
    created = await store.create()

    listed = await store.list_sessions()

    assert created.title == "New Chat"
    assert created.status is SessionStatus.ACTIVE
    assert [session.id for session in listed] == [created.id]


@pytest.mark.asyncio
async def test_archive_and_restore_session(tmp_path):
    from multiclaw.session import SessionStatus, SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))
    created = await store.create(title="Alpha")

    archived = await store.archive(created.id)
    active = await store.list_sessions()
    all_sessions = await store.list_sessions(include_archived=True)
    restored = await store.restore(created.id)

    assert archived.status is SessionStatus.ARCHIVED
    assert active == []
    assert [session.id for session in all_sessions] == [created.id]
    assert restored.status is SessionStatus.ACTIVE


@pytest.mark.asyncio
async def test_rename_validates_title(tmp_path):
    from multiclaw.session import InvalidSessionTitleError, SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))
    created = await store.create()

    with pytest.raises(InvalidSessionTitleError):
        await store.rename(created.id, " ")

    renamed = await store.rename(created.id, "Research notes")

    assert renamed.title == "Research notes"


@pytest.mark.asyncio
async def test_get_missing_session_returns_none(tmp_path):
    from multiclaw.session import SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))

    assert await store.get("missing") is None


@pytest.mark.asyncio
async def test_touch_message_updates_last_message_at_and_default_title(tmp_path):
    from multiclaw.session import SqliteSessionStore

    store = SqliteSessionStore(str(tmp_path / "sessions.db"))
    created = await store.create()

    touched = await store.touch_message(created.id, "first message becomes title")

    assert touched.last_message_at is not None
    assert touched.title == "first message becomes title"
