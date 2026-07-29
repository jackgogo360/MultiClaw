"""Tests for MCP tool adapter — bridging MCP tools into MultiClaw's ToolBuilder system."""

import asyncio
import threading

import pytest
from pydantic import BaseModel, create_model

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
