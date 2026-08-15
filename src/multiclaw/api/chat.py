from __future__ import annotations

from typing import Any

from multiclaw.events import ScopedEvent
from multiclaw.stream import DataStreamEncoder


def encode_session_metadata(session_payload: dict[str, Any]) -> str:
    return DataStreamEncoder.data_part(
        "data-session",
        session_payload,
        transient=True,
    )


def encode_run_metadata(session_id: str, run_id: str) -> str:
    return DataStreamEncoder.run_metadata(session_id, run_id)


def encode_scoped_event(event: ScopedEvent) -> str:
    return DataStreamEncoder.scoped_event(event)
