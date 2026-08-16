"""MCP -> ToolBuilder adapter - bridge MCP tools into MultiClaw's tool system."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, create_model

from multiclaw.tools.base import (
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolStatus,
)
from multiclaw.workflow.models import RecoveryStrategy

if TYPE_CHECKING:
    from multiclaw.mcp.manager import MCPClientManager

logger = logging.getLogger(__name__)

_JSON_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _json_type_to_python(prop: dict) -> type:
    type_str = prop.get("type", "string")
    if type_str == "array":
        return list
    if type_str == "object":
        return dict
    return _JSON_TYPE_MAP.get(type_str, str)


def _is_simple_schema(prop: dict) -> bool:
    type_str = prop.get("type", "string")
    if type_str in ("object", "array"):
        return False
    if type_str not in _JSON_TYPE_MAP:
        return False
    return True


def _json_schema_to_pydantic(schema: dict) -> type[BaseModel]:
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}

    if not properties:
        return create_model("MCPParams")

    all_simple = all(_is_simple_schema(p) for p in properties.values())

    if not all_simple:
        base: type[BaseModel] = type(
            "_PassthroughModel",
            (BaseModel,),
            {"model_config": {"extra": "allow"}},
        )
        return create_model("MCPParams", __base__=base)

    for name, prop in properties.items():
        py_type = _json_type_to_python(prop)
        if name in required:
            fields[name] = (py_type, ...)
        else:
            fields[name] = (py_type, None)

    return create_model("MCPParams", **fields)


def _extract_text(content: list[dict[str, Any]]) -> str:
    parts = []
    for item in content:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        elif item.get("type") == "resource":
            resource = item.get("resource", {})
            parts.append(resource.get("text", ""))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(parts)


def _is_read_only_tool_info(tool_info: Any) -> bool:
    return (
        getattr(tool_info, "read_only", None) is True
        and getattr(tool_info, "destructive", None) is False
        and getattr(tool_info, "open_world", None) is False
    )


class MCPToolBuilder(ToolBuilder):
    tool_kind = "mcp"
    name: str
    description: str
    parameters_schema: type[BaseModel]
    _server_name: str
    _original_name: str
    _manager: MCPClientManager

    def __init__(
        self,
        name: str,
        server_name: str,
        original_name: str,
        description: str,
        input_schema: dict,
        manager: MCPClientManager,
        read_only: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self._server_name = server_name
        self._original_name = original_name
        self._manager = manager
        self.read_only = read_only
        try:
            self.parameters_schema = _json_schema_to_pydantic(input_schema)
        except Exception:
            logger.warning(
                "Failed to create Pydantic model for MCP tool '%s', using passthrough",
                name,
            )
            self.parameters_schema = _json_schema_to_pydantic({})

    def validate(self, params: dict[str, Any]) -> BaseModel:
        return self.parameters_schema(**params)

    def build(self, params: BaseModel) -> ToolInvocation:
        return MCPToolInvocation(
            manager=self._manager,
            server_name=self._server_name,
            tool_name=self._original_name,
            params=params,
        )

    def approval_description(self, params: dict[str, Any]) -> str:
        return f"Call MCP tool {self.name} with {json.dumps(params, ensure_ascii=False)}"

    @property
    def recovery_strategy(self) -> RecoveryStrategy:
        return (
            RecoveryStrategy.READ_ONLY_REPLAY
            if self.read_only
            else RecoveryStrategy.MANUAL_UNCERTAIN
        )

    @classmethod
    def from_tool_info(cls, tool_info: Any, manager: MCPClientManager) -> "MCPToolBuilder":
        return cls(
            name=tool_info.name,
            server_name=tool_info.server_name,
            original_name=tool_info.original_name,
            description=tool_info.description,
            input_schema=tool_info.input_schema,
            manager=manager,
            read_only=_is_read_only_tool_info(tool_info),
        )


class MCPToolInvocation(ToolInvocation):
    def __init__(
        self,
        manager: MCPClientManager,
        server_name: str,
        tool_name: str,
        params: BaseModel,
    ) -> None:
        super().__init__(name=tool_name, params=params)
        self._manager = manager
        self._server_name = server_name
        self._tool_name = tool_name

    async def execute(self) -> ToolExecutionResult:
        try:
            result = await asyncio.to_thread(
                self._manager.call_tool,
                self._server_name,
                self._tool_name,
                self.params.model_dump(),
            )
        except Exception as exc:
            logger.error(
                "MCP tool call failed: %s/%s - %s",
                self._server_name, self._tool_name, exc,
            )
            return ToolExecutionResult(
                status=ToolStatus.ERROR,
                content=str(exc),
            )

        if result.external_request_id is not None and self.progress_recorder is not None:
            await self.progress_recorder.record_external_request_id(result.external_request_id)
        text = _extract_text(result.content)
        return ToolExecutionResult(
            status=ToolStatus.ERROR if result.is_error else ToolStatus.SUCCESS,
            content=text,
            external_request_id=result.external_request_id,
        )
