"""Tests for MCP tool adapter — bridging MCP tools into MultiClaw's ToolBuilder system."""

import pytest
from pydantic import BaseModel, create_model

from multiclaw.mcp.tool_adapter import MCPToolBuilder, _json_schema_to_pydantic


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
