import importlib
import inspect
import json

import pytest

from multiclaw.memory import MemoryEntry, MemoryProtocol


def test_memory_entry_defaults_are_scope_free() -> None:
    entry = MemoryEntry(content="remember alpha", type="note")

    dumped = entry.model_dump()

    assert len(entry.id) == 36
    assert entry.session_id is None
    assert entry.role == "note"
    assert isinstance(entry.created_at, int)
    assert dumped["metadata"] == {}
    assert "tenant_id" not in dumped
    assert "workspace_id" not in dumped


def test_memory_entry_from_row_parses_metadata_json() -> None:
    entry = MemoryEntry.from_row(
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "content": "hello",
            "type": "chat_message",
            "session_id": "44444444-4444-4444-4444-444444444444",
            "role": "assistant",
            "turn_index": 4,
            "created_at": 123,
            "metadata_json": json.dumps({"a": 1}, sort_keys=True),
        }
    )

    assert entry == MemoryEntry(
        id="11111111-1111-1111-1111-111111111111",
        content="hello",
        type="chat_message",
        session_id="44444444-4444-4444-4444-444444444444",
        role="assistant",
        turn_index=4,
        created_at=123,
        metadata={"a": 1},
    )


def test_memory_entry_allows_unbound_chat_messages_until_repository_save() -> None:
    entry = MemoryEntry(content="hello", type="chat_message", role="user")

    assert entry.session_id is None


def test_memory_entry_normalizes_empty_session_id_to_none() -> None:
    entry = MemoryEntry(content="hello", type="note", session_id="")

    assert entry.session_id is None

def test_memory_protocol_methods_take_context_first() -> None:
    assert list(inspect.signature(MemoryProtocol.save).parameters) == [
        "self",
        "context",
        "entry",
    ]
    assert list(inspect.signature(MemoryProtocol.query).parameters)[:4] == [
        "self",
        "context",
        "query",
        "top_k",
    ]
    assert list(inspect.signature(MemoryProtocol.recent).parameters)[:3] == [
        "self",
        "context",
        "limit",
    ]
    assert list(inspect.signature(MemoryProtocol.context).parameters)[:4] == [
        "self",
        "context",
        "max_chars",
        "limit",
    ]


def test_memory_package_exports_scoped_api_only() -> None:
    import multiclaw.memory as memory_package

    assert memory_package.MemoryEntry is MemoryEntry
    assert memory_package.MemoryProtocol is MemoryProtocol
    assert not hasattr(memory_package, "InMemoryMemory")
    assert not hasattr(memory_package, "SqliteMemory")
    assert importlib.util.find_spec("multiclaw.memory.sqlite") is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("multiclaw.memory.sqlite")
