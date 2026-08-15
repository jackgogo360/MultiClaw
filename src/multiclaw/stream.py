"""UIMessageChunk SSE encoder for AI SDK / assistant-ui clients."""

import json
from typing import Any

from multiclaw.events import ScopedEvent


class DataStreamEncoder:
    """Encode AI SDK UIMessageChunk events as JSON SSE lines."""

    @staticmethod
    def _event(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"

    @classmethod
    def start(cls, message_id: str | None = None) -> str:
        payload: dict[str, Any] = {"type": "start"}
        if message_id:
            payload["messageId"] = message_id
        return cls._event(payload)

    @classmethod
    def start_step(cls) -> str:
        return cls._event({"type": "start-step"})

    @classmethod
    def finish_step(cls) -> str:
        return cls._event({"type": "finish-step"})

    @classmethod
    def text_start(cls, part_id: str) -> str:
        return cls._event({"type": "text-start", "id": part_id})

    @classmethod
    def text_delta(cls, part_id: str, text: str) -> str:
        return cls._event({"type": "text-delta", "id": part_id, "delta": text})

    @classmethod
    def text_end(cls, part_id: str) -> str:
        return cls._event({"type": "text-end", "id": part_id})

    @classmethod
    def reasoning_start(cls, part_id: str) -> str:
        return cls._event({"type": "reasoning-start", "id": part_id})

    @classmethod
    def reasoning_delta(cls, part_id: str, text: str) -> str:
        return cls._event({"type": "reasoning-delta", "id": part_id, "delta": text})

    @classmethod
    def reasoning_end(cls, part_id: str) -> str:
        return cls._event({"type": "reasoning-end", "id": part_id})

    @classmethod
    def tool_input_available(
        cls,
        tool_call_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> str:
        return cls._event(
            {
                "type": "tool-input-available",
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "input": tool_input,
            }
        )

    @classmethod
    def tool_approval_request(cls, approval_id: str, tool_call_id: str) -> str:
        return cls._event(
            {
                "type": "tool-approval-request",
                "approvalId": approval_id,
                "toolCallId": tool_call_id,
            }
        )

    @classmethod
    def tool_output_available(
        cls,
        tool_call_id: str,
        output: dict[str, Any],
    ) -> str:
        return cls._event(
            {
                "type": "tool-output-available",
                "toolCallId": tool_call_id,
                "output": output,
            }
        )

    @classmethod
    def tool_output_error(
        cls,
        tool_call_id: str,
        error_text: str,
    ) -> str:
        return cls._event(
            {
                "type": "tool-output-error",
                "toolCallId": tool_call_id,
                "errorText": error_text,
            }
        )

    @classmethod
    def data_part(
        cls,
        part_type: str,
        data: dict[str, Any],
        *,
        transient: bool = False,
        part_id: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {"type": part_type, "data": data}
        if transient:
            payload["transient"] = True
        if part_id:
            payload["id"] = part_id
        return cls._event(payload)

    @classmethod
    def run_metadata(cls, session_id: str, run_id: str) -> str:
        return cls.data_part(
            "data-run",
            {"session_id": session_id, "run_id": run_id},
            transient=True,
        )

    @classmethod
    def scoped_event(cls, event: ScopedEvent) -> str:
        return cls.data_part(
            "data-event",
            {
                "tenant_id": event.tenant_id,
                "workspace_id": event.workspace_id,
                "session_id": event.session_id,
                "run_id": event.run_id,
                "event_type": event.event_type,
                "occurred_at_ms": event.occurred_at_ms,
                "data": event.data,
            },
            transient=True,
        )

    @classmethod
    def finish(cls, reason: str) -> str:
        return cls._event({"type": "finish", "finishReason": reason})

    @classmethod
    def error(cls, message: str) -> str:
        return cls._event({"type": "error", "errorText": message})
