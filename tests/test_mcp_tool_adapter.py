"""Tests for MCP tool adapter — bridging MCP tools into MultiClaw's ToolBuilder system."""

import asyncio
import threading
from pathlib import Path

import pytest
from pydantic import BaseModel, create_model

from multiclaw.governance.sandbox.models import SandboxedLaunchSpec
from multiclaw.mcp.transport.stdio import StdioTransport
from multiclaw.mcp.tool_adapter import MCPToolBuilder, _json_schema_to_pydantic
from multiclaw.mcp.types import ToolCallResult
from multiclaw.tools.base import ToolStatus


class TestJsonSchemaToPydantic:
    def test_simple_types(self):
        schema = {
            "properties": {
                "path": {"type": "string"},
                "count": {"type": "integer"},
                "enabled": {"type": "boolean"},
                "score": {"type": "number"},
            },
            "required": ["path"],
        }
        model = _json_schema_to_pydantic(schema)
        inst = model(path="/tmp", count=5, enabled=True, score=3.5)
        assert inst.path == "/tmp"
        assert inst.count == 5
        assert inst.enabled is True
        assert inst.score == 3.5

    def test_empty_schema(self):
        model = _json_schema_to_pydantic({})
        inst = model()
        assert inst.model_dump() == {}

    def test_optional_fields_default_to_none(self):
        schema = {
            "properties": {
                "name": {"type": "string"},
            },
            "required": [],
        }
        model = _json_schema_to_pydantic(schema)
        inst = model()
        assert inst.name is None

    def test_nested_object_falls_back_to_dict(self):
        schema = {
            "properties": {
                "config": {"type": "object", "properties": {"key": {"type": "string"}}},
            },
            "required": ["config"],
        }
        model = _json_schema_to_pydantic(schema)
        inst = model(config={"key": "value"})
        assert inst.config == {"key": "value"}


@pytest.mark.asyncio
async def test_mcp_invocation_does_not_block_running_event_loop():
    params_model = create_model("Params", path=(str, ...))
    release_call = threading.Event()

    class SlowManager:
        def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> ToolCallResult:
            if not release_call.wait(timeout=1.0):
                return ToolCallResult(
                    content=[{"type": "text", "text": "ticker did not run"}],
                    is_error=True,
                )
            return ToolCallResult(content=[{"type": "text", "text": arguments["path"]}])

    builder = MCPToolBuilder(
        name="mcp__demo__read_file",
        server_name="demo",
        original_name="read_file",
        description="Read a file",
        input_schema={"properties": {"path": {"type": "string"}}, "required": ["path"]},
        manager=SlowManager(),
    )
    invocation = builder.build(params_model(path="/tmp/demo.txt"))

    invocation_task = asyncio.create_task(invocation.execute())

    async def ticker() -> None:
        await asyncio.sleep(0.01)
        release_call.set()

    await ticker()
    result = await invocation_task

    assert release_call.is_set()
    assert result.status == ToolStatus.SUCCESS
    assert result.content == "/tmp/demo.txt"


def _launch_spec(tmp_path: Path) -> SandboxedLaunchSpec:
    private_root = tmp_path / "private"
    home = private_root / "home"
    tmp = private_root / "tmp"
    home.mkdir(parents=True)
    tmp.mkdir(parents=True)
    return SandboxedLaunchSpec(
        executable="/usr/bin/env",
        args=("demo",),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "USER": "sandbox", "CUSTOM": "1"},
        stdin_bytes=None,
        private_root=private_root,
        backend_name="recording",
        profile_name="mcp_stdio_local",
        correlation_id="corr-123",
    )


@pytest.mark.asyncio
async def test_stdio_transport_blanks_sdk_defaults_and_cleans_up_private_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    events: list[str] = []
    spec = _launch_spec(tmp_path)

    class FakeContext:
        async def __aenter__(self):
            events.append("enter")
            return ("read", "write")

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            events.append("exit")
            assert spec.private_root.exists()

    monkeypatch.setattr(
        "multiclaw.mcp.transport.stdio.get_default_environment",
        lambda: {"PATH": "/host", "HOME": "/host-home", "TERM": "dumb", "USER": "host"},
    )

    def fake_stdio_client(params):
        captured["params"] = params
        return FakeContext()

    monkeypatch.setattr("multiclaw.mcp.transport.stdio.stdio_client", fake_stdio_client)

    transport = StdioTransport(server_name="demo", launch_spec=spec)
    await transport.connect()
    await transport.disconnect()
    await transport.disconnect()

    params = captured["params"]
    assert params.command == spec.executable
    assert params.args == list(spec.args)
    assert params.cwd == spec.cwd
    assert params.env["PATH"] == "/usr/bin:/bin"
    assert params.env["HOME"] == spec.env["HOME"]
    assert params.env["USER"] == "sandbox"
    assert params.env["TERM"] == ""
    assert params.env["CUSTOM"] == "1"
    assert events == ["enter", "exit"]
    assert spec.private_root.exists() is False


@pytest.mark.asyncio
async def test_stdio_transport_cleans_up_on_connect_failure_and_cannot_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _launch_spec(tmp_path)

    class FailingContext:
        async def __aenter__(self):
            raise RuntimeError("boom")

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return None

    monkeypatch.setattr(
        "multiclaw.mcp.transport.stdio.get_default_environment",
        lambda: {"PATH": "/host"},
    )
    monkeypatch.setattr("multiclaw.mcp.transport.stdio.stdio_client", lambda params: FailingContext())

    transport = StdioTransport(server_name="demo", launch_spec=spec)
    with pytest.raises(RuntimeError, match="boom"):
        await transport.connect()

    assert spec.private_root.exists() is False
    with pytest.raises(RuntimeError, match="private root"):
        await transport.connect()
