# MCP Integration Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add MCP (Model Context Protocol) client support so MultiClaw can discover and call tools from local and remote MCP servers, with MCP tools registered as `ToolBuilder` adapters alongside native tools.

**Architecture:** New `src/multiclaw/mcp/` package with 10 transplanted modules from reference + 1 new `tool_adapter.py`. `MCPToolBuilder` wraps MCP tools as `ToolBuilder`, registered into existing `ToolRegistry`. `CoreToolScheduler` gains a small skip for MCP permission checks. `server.py` lifespan initializes/shuts down the `MCPClientManager`. Config via `.mcp.json`.

**Tech Stack:** `mcp>=1.0.0`, `anyio>=4.0.0`, `httpx>=0.25.0`, `websockets>=12.0` (new deps). Pydantic `create_model` for dynamic schema generation.

---

### Task 1: Add dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add MCP dependencies**

```toml
# In [project] dependencies, add:
    "mcp>=1.0.0",
    "anyio>=4.0.0",
    "websockets>=12.0",
```

Note: `httpx>=0.27` is already a dependency.

- [ ] **Step 2: Install dependencies**

```bash
cd /Users/felix/git/MultiClaw && uv sync
```

Expected: succeeds, no errors.

---

### Task 2: Create transport layer — base, factory, stdio, sse, http

**Files:**
- Create: `src/multiclaw/mcp/transport/__init__.py`
- Create: `src/multiclaw/mcp/transport/base.py`
- Create: `src/multiclaw/mcp/transport/factory.py`
- Create: `src/multiclaw/mcp/transport/stdio.py`
- Create: `src/multiclaw/mcp/transport/sse.py`
- Create: `src/multiclaw/mcp/transport/http.py`
- Create: `src/multiclaw/mcp/transport/ws.py`
- Create: `src/multiclaw/mcp/transport/in_process.py`
- Create: `src/multiclaw/mcp/__init__.py` (empty placeholder)

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p /Users/felix/git/MultiClaw/src/multiclaw/mcp/transport
```

- [ ] **Step 2: Write transport/__init__.py**

```python
from .base import BaseTransport
from .stdio import StdioTransport
from .sse import SSETransport
from .http import StreamableHTTPTransport
from .ws import WebSocketTransport
from .in_process import InProcessTransport, create_linked_transport_pair
from .factory import create_transport

__all__ = [
    "BaseTransport",
    "StdioTransport",
    "SSETransport",
    "StreamableHTTPTransport",
    "WebSocketTransport",
    "InProcessTransport",
    "create_linked_transport_pair",
    "create_transport",
]
```

- [ ] **Step 3: Write transport/base.py**

Copy from reference `/Users/felix/git/MyObsidianVault/agent-code/20260522-mcp-client/src/transport/base.py` with no changes.

- [ ] **Step 4: Write transport/stdio.py**

Copy from reference `/Users/felix/git/MyObsidianVault/agent-code/20260522-mcp-client/src/transport/stdio.py`. Change the import path:

```python
from .base import BaseTransport
```

- [ ] **Step 5: Write transport/sse.py**

Copy from reference with import change:
```python
from .base import BaseTransport
```

- [ ] **Step 6: Write transport/http.py**

Copy from reference with import change:
```python
from .base import BaseTransport
```

- [ ] **Step 7: Write transport/ws.py**

Copy from reference with import change:
```python
from .base import BaseTransport
```

- [ ] **Step 8: Write transport/in_process.py**

Copy from reference with import change:
```python
from .base import BaseTransport
```

- [ ] **Step 9: Write transport/factory.py**

Copy from reference with import change:
```python
from ..types import (
    HTTPServerConfig,
    InProcessServerConfig,
    SSEServerConfig,
    ServerConfig,
    StdioServerConfig,
    WebSocketServerConfig,
)
from .base import BaseTransport
from .http import StreamableHTTPTransport
from .in_process import InProcessTransport
from .sse import SSETransport
from .stdio import StdioTransport
from .ws import WebSocketTransport
```

- [ ] **Step 10: Write empty __init__.py placeholder**

```python
# MCP client package — Phase 1
```

---

### Task 3: Create types.py

**Files:**
- Create: `src/multiclaw/mcp/types.py`

- [ ] **Step 1: Write types.py**

Copy from reference `/Users/felix/git/MyObsidianVault/agent-code/20260522-mcp-client/src/types.py` with no import path changes needed (it's self-contained).

---

### Task 4: Create security.py

**Files:**
- Create: `src/multiclaw/mcp/security.py`

- [ ] **Step 1: Write security.py**

Copy from reference `/Users/felix/git/MyObsidianVault/agent-code/20260522-mcp-client/src/security.py` with no changes.

---

### Task 5: Create circuit_breaker.py

**Files:**
- Create: `src/multiclaw/mcp/circuit_breaker.py`

- [ ] **Step 1: Write circuit_breaker.py**

Copy from reference `/Users/felix/git/MyObsidianVault/agent-code/20260522-mcp-client/src/circuit_breaker.py` with no changes.

---

### Task 6: Create config.py

**Files:**
- Create: `src/multiclaw/mcp/config.py`

- [ ] **Step 1: Write config.py**

Copy from reference `/Users/felix/git/MyObsidianVault/agent-code/20260522-mcp-client/src/config.py`. Change import:

```python
from .types import (
    HTTPServerConfig,
    InProcessServerConfig,
    OAuthConfig,
    SSEServerConfig,
    ServerConfig,
    StdioServerConfig,
    WebSocketServerConfig,
)
```

Also remove the `InProcessServerConfig` import if not needed (Phase 1 doesn't use it). Keep it for completeness.

---

### Task 7: Create client.py

**Files:**
- Create: `src/multiclaw/mcp/client.py`

- [ ] **Step 1: Write client.py**

Copy from reference `/Users/felix/git/MyObsidianVault/agent-code/20260522-mcp-client/src/client.py`. Change imports:

```python
from .transport.base import BaseTransport
from .types import ServerStatus, ToolCallResult, ToolInfo
```

---

### Task 8: Create manager.py

**Files:**
- Create: `src/multiclaw/mcp/manager.py`

- [ ] **Step 1: Write manager.py**

Copy from reference `/Users/felix/git/MyObsidianVault/agent-code/20260522-mcp-client/src/manager.py`. Change imports:

```python
from .circuit_breaker import CircuitBreaker
from .client import MCPClient
from .transport.factory import create_transport
from .types import ServerConfig, ServerState, ServerStatus, ToolCallResult, ToolInfo
```

Remove the `ToolRegistry` references (lines 14, 39, 41) since we use our own `ToolRegistry`. Replace `ToolRegistry`-related code with simpler internal tracking:

- Remove `self.registry` from `__init__`
- `_register_tools` becomes a no-op (tool registration happens via `tool_adapter.py` externally)
- `_on_tools_changed` updates `self._states[name].tools` only, no registry calls
- Remove `get_tools_for_llm()` — consumers use `get_all_tools()` and register externally

**Modified `__init__`:**

```python
def __init__(
    self,
    *,
    local_batch_size: int = _LOCAL_BATCH_SIZE,
    remote_batch_size: int = _REMOTE_BATCH_SIZE,
) -> None:
    self._local_batch_size = local_batch_size
    self._remote_batch_size = remote_batch_size
    self._clients: dict[str, MCPClient] = {}
    self._states: dict[str, ServerState] = {}
    self._breakers: dict[str, CircuitBreaker] = {}
    self._loop: Optional[asyncio.AbstractEventLoop] = None
    self._thread: Optional[threading.Thread] = None
    self._lock = threading.Lock()
    self._started = False
```

**Modified `call_tool`:**

```python
def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolCallResult:
    breaker = self._breakers.get(server_name)
    if breaker and breaker.is_open:
        remaining = int(breaker.remaining_cooldown)
        raise RuntimeError(
            f"Server '{server_name}' circuit breaker open. Retry in ~{remaining}s."
        )
    try:
        result = self._run_sync(self._call_tool_async(server_name, tool_name, arguments))
        if breaker:
            breaker.record_success()
        return result
    except Exception as e:
        if breaker:
            breaker.record_failure()
        raise
```

**Modified `get_all_tools` (new method):**

```python
def get_all_tools(self) -> list:
    tools = []
    for state in self._states.values():
        tools.extend(state.tools)
    return tools
```

**Add `get_server_tool_names`:**

```python
def get_server_tool_names(self) -> dict[str, list[str]]:
    return {name: [t.qualified_name for t in state.tools] for name, state in self._states.items()}
```

- [ ] **Step 2: Verify manager.py imports work**

```bash
cd /Users/felix/git/MultiClaw && python -c "from multiclaw.mcp.manager import MCPClientManager; print('ok')"
```

Expected: `ok`

---

### Task 9: Create tool_adapter.py (the core bridge)

**Files:**
- Create: `src/multiclaw/mcp/tool_adapter.py`
- Test: `tests/test_mcp_tool_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/felix/git/MultiClaw && uv run pytest tests/test_mcp_tool_adapter.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Write tool_adapter.py**

```python
"""MCP → ToolBuilder adapter — bridge MCP tools into MultiClaw's tool system."""
from __future__ import annotations

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
    """Check if a property has a simple type we can map directly."""
    type_str = prop.get("type", "string")
    if type_str in ("object", "array"):
        return False
    if type_str not in _JSON_TYPE_MAP:
        return False
    return True


def _json_schema_to_pydantic(schema: dict) -> type[BaseModel]:
    """Convert a JSON Schema to a Pydantic model for parameter validation.

    Falls back to dict passthrough for complex schemas ($ref, anyOf, nested objects).
    """
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}

    # If no properties, return an empty model
    if not properties:
        return create_model("MCPParams")

    all_simple = all(_is_simple_schema(p) for p in properties.values())

    if not all_simple:
        # Fallback: use dict passthrough for complex schemas
        # We still create a model with extra="allow" so that dict access works
        base: type[BaseModel] = type(
            "_PassthroughModel",
            (BaseModel,),
            {"model_config": {"extra": "allow"}},
        )
        return create_model("MCPParams", __base__=base)  # type: ignore[call-overload]

    for name, prop in properties.items():
        py_type = _json_type_to_python(prop)
        if name in required:
            fields[name] = (py_type, ...)
        else:
            fields[name] = (py_type, None)

    return create_model("MCPParams", **fields)  # type: ignore[call-overload]


def _extract_text(content: list[dict[str, Any]]) -> str:
    """Extract human-readable text from MCP content blocks."""
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


class MCPToolBuilder(ToolBuilder):
    """Adapter that wraps an MCP tool as a MultiClaw ToolBuilder."""

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
    ) -> None:
        self.name = name
        self.description = description
        self._server_name = server_name
        self._original_name = original_name
        self._manager = manager
        # Dynamically generate Pydantic model from JSON Schema
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

    @classmethod
    def from_tool_info(cls, tool_info: Any, manager: MCPClientManager) -> "MCPToolBuilder":
        """Factory from a ToolInfo (from mcp.types)."""
        return cls(
            name=tool_info.qualified_name,
            server_name=tool_info.server_name,
            original_name=tool_info.original_name,
            description=tool_info.description,
            input_schema=tool_info.input_schema,
            manager=manager,
        )

    @classmethod
    def from_dict(cls, server_name: str, tool_info: dict, manager: MCPClientManager) -> "MCPToolBuilder":
        """Factory from server tool dict returned by manager.get_all_tools_raw()."""
        return cls(
            name=tool_info["qualified_name"],
            server_name=server_name,
            original_name=tool_info["original_name"],
            description=tool_info.get("description", ""),
            input_schema=tool_info.get("input_schema", {}),
            manager=manager,
        )


class MCPToolInvocation(ToolInvocation):
    """Executes an MCP tool call through MCPClientManager."""

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
            result = self._manager.call_tool(
                self._server_name,
                self._tool_name,
                self.params.model_dump(),
            )
            text = _extract_text(result.content)
            return ToolExecutionResult(
                status=ToolStatus.ERROR if result.is_error else ToolStatus.SUCCESS,
                content=text,
            )
        except Exception as exc:
            logger.error(
                "MCP tool call failed: %s/%s — %s",
                self._server_name, self._tool_name, exc,
            )
            return ToolExecutionResult(
                status=ToolStatus.ERROR,
                content=str(exc),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/felix/git/MultiClaw && uv run pytest tests/test_mcp_tool_adapter.py -v
```

Expected: PASS (4 tests).

---

### Task 10: Create mcp/__init__.py

**Files:**
- Modify: `src/multiclaw/mcp/__init__.py`

- [ ] **Step 1: Write final __init__.py**

```python
from .config import load_mcp_config
from .manager import MCPClientManager
from .tool_adapter import MCPToolBuilder, MCPToolInvocation
from .types import ServerStatus, ToolCallResult, ToolInfo

__all__ = [
    "MCPClientManager",
    "MCPToolBuilder",
    "MCPToolInvocation",
    "ServerStatus",
    "ToolCallResult",
    "ToolInfo",
    "load_mcp_config",
]
```

---

### Task 11: Add McpSettings to config

**Files:**
- Modify: `src/multiclaw/config/settings.py`
- Modify: `multiclaw.toml`

- [ ] **Step 1: Add McpSettings class to settings.py**

Add after `class SkillSettings` (line 66):

```python
class McpSettings(BaseModel):
    enabled: bool = True
    config_path: str = ""
```

- [ ] **Step 2: Add mcp field to Settings**

In `class Settings`, add after the `resend` field (line 107):

```python
mcp: McpSettings = Field(default_factory=McpSettings)
```

- [ ] **Step 3: Add mcp to _build_toml_kwargs**

In the `_build_toml_kwargs` method, add after the resend block (line 173):

```python
if "mcp" in data:
    result["mcp"] = data["mcp"]
```

- [ ] **Step 4: Add [mcp] to multiclaw.toml**

```toml
[mcp]
enabled = true
```

- [ ] **Step 5: Verify settings load**

```bash
cd /Users/felix/git/MultiClaw && uv run python -c "
from multiclaw.config import Settings
s = Settings()
print('mcp enabled:', s.mcp.enabled)
print('mcp config_path:', repr(s.mcp.config_path))
"
```

Expected: `mcp enabled: True`, `mcp config_path: ''`

---

### Task 12: Modify tools/scheduler.py for MCP bypass

**Files:**
- Modify: `src/multiclaw/tools/scheduler.py`

- [ ] **Step 1: Add MCP detection in run()**

In `CoreToolScheduler.run()`, add MCP bypass before the permission check. After the validation block (around line 47-48, right after `params = builder.validate(raw_params)`), add:

```python
# MCP tools have server-level pre-approval — skip per-call permission check
from multiclaw.mcp.tool_adapter import MCPToolBuilder

is_mcp = isinstance(builder, MCPToolBuilder)
```

Then wrap the permission check block (lines 49-122, covering `decision = await self.permission_checker.check(...)` through the approval flow) in:

```python
if not is_mcp:
    # existing permission check + approval flow
    decision = await self.permission_checker.check(...)
    ...existing code...
else:
    # MCP tools: skip permission, build and execute directly
    invocation = builder.build(params)
```

The exact change: find the line `decision = await self.permission_checker.check(...)` and wrap from there to the `invocation = builder.build(params)` call at line 128.

The cleanest approach — add early return path for MCP:

After line 48 (`params = builder.validate(raw_params)`):

```python
# MCP tools are pre-approved at server-connection time
if isinstance(builder, MCPToolBuilder):
    invocation = builder.build(params)
    try:
        result = await self.sandbox.run(invocation.execute)
    except Exception as exc:
        error_text = str(exc)
        return ToolExecutionResult(status=ToolStatus.ERROR, content=error_text)
    await self.event_bus.publish(
        Event(type="tool.completed", data={"tool": builder.name})
    )
    return result
```

The import at the top of the file:
```python
from multiclaw.mcp.tool_adapter import MCPToolBuilder
```

(Keep all existing code — just add this bypass block after validation.)

---

### Task 13: Wire MCP into server.py lifespan

**Files:**
- Modify: `src/multiclaw/server.py`

- [ ] **Step 1: Import MCP modules**

Add after other imports (around line 104, after tool imports):

If settings say mcp is enabled, import MCP:

Add imports at module level:
```python
from multiclaw.mcp import MCPClientManager, MCPToolBuilder, load_mcp_config
```

- [ ] **Step 2: Modify create_agent() to optionally register MCP tools**

After the existing tool registration block (line 158, after `registry.register(WebSearchToolBuilder(workspace_root))`), add:

```python
# Register MCP tools if enabled
if settings.mcp.enabled:
    mcp_manager = MCPClientManager()
    configs = load_mcp_config(
        settings.mcp.config_path if settings.mcp.config_path else None
    )
    if configs:
        try:
            states = mcp_manager.connect_servers(configs)
            for server_name, state in states.items():
                if state.status.value == "connected":
                    for tool in state.tools:
                        try:
                            adapter = MCPToolBuilder(
                                name=tool.qualified_name,
                                server_name=tool.server_name,
                                original_name=tool.original_name,
                                description=tool.description,
                                input_schema=tool.input_schema,
                                manager=mcp_manager,
                            )
                            registry.register(adapter)
                        except Exception:
                            logger.warning(
                                "Failed to register MCP tool: %s", tool.qualified_name
                            )
                    logger.info(
                        "Registered %d tools from MCP server '%s'",
                        len(state.tools), server_name,
                    )
                else:
                    logger.warning(
                        "MCP server '%s' failed to connect: %s",
                        server_name, state.error,
                    )
        except Exception:
            logger.exception("Failed to connect MCP servers")
    else:
        logger.info("No MCP servers configured (no .mcp.json found)")
```

Store `mcp_manager` on the agent for lifecycle management. In `MultiClawAgent`, it doesn't exist yet — store as an attribute:

```python
runtime_agent.mcp_manager = mcp_manager if (settings.mcp.enabled and configs) else None
```

- [ ] **Step 3: Modify lifespan to clean up MCP**

In `lifespan()`, after `yield`:

```python
# Cleanup MCP connections
if hasattr(agent, 'mcp_manager') and agent.mcp_manager:
    agent.mcp_manager.stop()
```

---

### Task 14: Integration test — end-to-end MCP tool flow

**Files:**
- Create: `tests/test_mcp_integration.py`

- [ ] **Step 1: Write integration test**

```python
"""Integration tests for MCP → ToolRegistry → execution pipeline."""
import pytest

from multiclaw.mcp.tool_adapter import MCPToolBuilder, _json_schema_to_pydantic
from multiclaw.tools.registry import ToolRegistry


class TestMCPToolBuilderRegistration:
    def test_mcp_tool_registers_and_produces_openai_schema(self):
        """MCPToolBuilder should integrate with ToolRegistry seamlessly."""
        schema = {
            "properties": {
                "path": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["path"],
        }

        # Create a mock MCPToolBuilder without a real manager
        # (we don't need manager for schema generation test)
        builder = MCPToolBuilder.__new__(MCPToolBuilder)
        builder.name = "mcp__test__read_file"
        builder.description = "Read a file from the filesystem"
        builder._server_name = "test"
        builder._original_name = "read_file"
        builder._manager = None  # won't be called in this test
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
        """Validation should parse dict into Pydantic model."""
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
        """MCP tool names follow mcp__{server}__{tool} convention."""
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
```

- [ ] **Step 2: Run integration tests**

```bash
cd /Users/felix/git/MultiClaw && uv run pytest tests/test_mcp_integration.py -v
```

Expected: PASS (3 tests).

---

### Task 15: Run all tests and verify nothing broke

- [ ] **Step 1: Run full test suite**

```bash
cd /Users/felix/git/MultiClaw && uv run pytest -v
```

Expected: All existing tests pass, new tests pass. No regressions.

- [ ] **Step 2: Verify imports are clean**

```bash
cd /Users/felix/git/MultiClaw && uv run python -c "
from multiclaw.mcp import MCPClientManager, MCPToolBuilder, load_mcp_config
from multiclaw.mcp.types import StdioServerConfig, HTTPServerConfig, ToolInfo
from multiclaw.mcp.transport import create_transport, StdioTransport, SSETransport
from multiclaw.mcp.security import sanitize_error, scan_tool_description
from multiclaw.mcp.circuit_breaker import CircuitBreaker
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 3: Verify settings + server module loads**

```bash
cd /Users/felix/git/MultiClaw && uv run python -c "
from multiclaw.config import Settings
s = Settings()
assert s.mcp.enabled is True
print('Settings with MCP OK')
"
```

Expected: `Settings with MCP OK`
