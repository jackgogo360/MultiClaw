"""Integration tests for MCP -> ToolRegistry -> execution pipeline."""
import pytest

from multiclaw.mcp.tool_adapter import MCPToolBuilder, _json_schema_to_pydantic
from multiclaw.tools.registry import ToolRegistry


class TestMCPToolBuilderRegistration:
    def test_mcp_tool_registers_and_produces_openai_schema(self):
        schema = {
            "properties": {
                "path": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["path"],
        }
        builder = MCPToolBuilder.__new__(MCPToolBuilder)
        builder.name = "mcp__test__read_file"
        builder.description = "Read a file from the filesystem"
        builder._server_name = "test"
        builder._original_name = "read_file"
        builder._manager = None
        builder.parameters_schema = _json_schema_to_pydantic(schema)

        registry = ToolRegistry()
        registry.register(builder)

        schemas = registry.to_openai_schemas()
        assert len(schemas) == 1
        schema_obj = schemas[0]
        assert schema_obj["type"] == "function"
        assert schema_obj["function"]["name"] == "mcp__test__read_file"
        assert "description" in schema_obj["function"]
        assert "parameters" in schema_obj["function"]

    def test_mcp_tool_builder_validate(self):
        schema = {
            "properties": {
                "path": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["path"],
        }
        builder = MCPToolBuilder.__new__(MCPToolBuilder)
        builder.name = "mcp__test__tool"
        builder.description = "test"
        builder._server_name = "test"
        builder._original_name = "tool"
        builder._manager = None
        builder.parameters_schema = _json_schema_to_pydantic(schema)

        params = builder.validate({"path": "/tmp", "count": 3})
        assert params.path == "/tmp"
        assert params.count == 3

    def test_mcp_tool_name_format(self):
        builder = MCPToolBuilder.__new__(MCPToolBuilder)
        builder.name = "mcp__filesystem__read_file"
        builder.description = "..."
        builder._server_name = "filesystem"
        builder._original_name = "read_file"
        builder._manager = None
        builder.parameters_schema = _json_schema_to_pydantic({})

        assert builder.name.startswith("mcp__")
        assert "filesystem" in builder.name
        assert "read_file" in builder.name
