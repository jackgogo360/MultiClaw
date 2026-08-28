import json

from multiclaw.stream import DataStreamEncoder


def _decode_sse(line: str) -> dict:
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    return json.loads(line[6:-2])


def test_encode_text_delta():
    payload = _decode_sse(DataStreamEncoder.text_delta("text-1", "Hello"))
    assert payload == {"type": "text-delta", "id": "text-1", "delta": "Hello"}


def test_encode_tool_input_available():
    payload = _decode_sse(
        DataStreamEncoder.tool_input_available("call_1", "read_file", {"path": "/x"})
    )
    assert payload["toolCallId"] == "call_1"
    assert payload["toolName"] == "read_file"
    assert payload["input"] == {"path": "/x"}


def test_encode_tool_output_available():
    payload = _decode_sse(DataStreamEncoder.tool_output_available("call_1", {"content": "ok"}))
    assert payload["toolCallId"] == "call_1"
    assert payload["output"] == {"content": "ok"}


def test_encode_tool_approval_request():
    payload = _decode_sse(DataStreamEncoder.tool_approval_request("approval_1", "call_1"))
    assert payload == {
        "type": "tool-approval-request",
        "approvalId": "approval_1",
        "toolCallId": "call_1",
    }


def test_encode_tool_output_error():
    payload = _decode_sse(DataStreamEncoder.tool_output_error("call_1", "Something failed"))
    assert payload["type"] == "tool-output-error"
    assert payload["errorText"] == "Something failed"


def test_encode_data_event():
    payload = _decode_sse(
        DataStreamEncoder.data_part("data-reasoning", {"text": "hmm"}, transient=True)
    )
    assert payload == {
        "type": "data-reasoning",
        "data": {"text": "hmm"},
        "transient": True,
    }


def test_encode_data_session():
    payload = _decode_sse(
        DataStreamEncoder.data_part("data-session", {"session_id": "s1", "title": "Chat"})
    )
    assert payload["type"] == "data-session"


def test_encode_data_run():
    payload = _decode_sse(
        DataStreamEncoder.data_part(
            "data-run",
            {"session_id": "s1", "run_id": "r1"},
            transient=True,
        )
    )
    assert payload == {
        "type": "data-run",
        "data": {"session_id": "s1", "run_id": "r1"},
        "transient": True,
    }


def test_encode_scoped_event():
    payload = _decode_sse(
        DataStreamEncoder.data_part(
            "data-event",
            {
                "tenant_id": "t1",
                "workspace_id": "w1",
                "session_id": "s1",
                "run_id": "r1",
                "event_type": "tool.completed",
                "occurred_at_ms": 123,
                "data": {"tool": "echo"},
            },
            transient=True,
        )
    )
    assert payload == {
        "type": "data-event",
        "data": {
            "tenant_id": "t1",
            "workspace_id": "w1",
            "session_id": "s1",
            "run_id": "r1",
            "event_type": "tool.completed",
            "occurred_at_ms": 123,
            "data": {"tool": "echo"},
        },
        "transient": True,
    }


def test_encode_finish():
    payload = _decode_sse(DataStreamEncoder.finish("stop"))
    assert payload == {"type": "finish", "finishReason": "stop"}


def test_encode_finish_step():
    payload = _decode_sse(DataStreamEncoder.finish_step())
    assert payload == {"type": "finish-step"}


def test_encode_error():
    payload = _decode_sse(DataStreamEncoder.error("Something went wrong"))
    assert payload == {"type": "error", "errorText": "Something went wrong"}
