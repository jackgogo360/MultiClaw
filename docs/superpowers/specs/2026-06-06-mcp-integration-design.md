# MCP Integration Design

**Date**: 2026-06-06
**Status**: draft
**Phase**: 1 (of 4)

## Overview

Introduce MCP (Model Context Protocol) client support to MultiClaw, allowing the agent to discover and call tools from local and remote MCP servers. MCP tools co-exist with native tools in the agent's tool list and follow the same execution path through `CoreToolScheduler`.

Reference implementation: `/Users/felix/git/MyObsidianVault/agent-code/20260522-mcp-client/` — a production-grade MCP client built on `mcp>=1.0.0` Python SDK.

## Architecture

**Pattern**: Adapter — MCP tools are wrapped as `ToolBuilder` instances, registered into the existing `ToolRegistry`, and executed through the same `CoreToolScheduler.run()` path.

```
MCPClientManager ──→ MCPToolBuilder (per tool) ──→ ToolRegistry.register()
                                                          │
                                                          ▼
                                                   MultiClawAgent
                                                     │
                                         ┌───────────┴───────────┐
                                         ▼                       ▼
                                 CoreToolScheduler        CoreToolScheduler
                                 .run(native builder)     .run(MCP builder)
```

Key design decisions:
- MCP tools appear to the LLM as regular OpenAI function-calling schemas, prefixed `mcp__{server}__{tool}`
- Server-level permission: approved once at connection time, individual tool calls skip re-checking
- Dynamic Pydantic model generation from JSON Schema for parameter validation, with dict fallback for complex schemas

## Module Structure

New package `src/multiclaw/mcp/` — 13 files total, 10 transplanted from reference, 1 new (`tool_adapter.py`):

```
src/multiclaw/mcp/
  __init__.py                   # Public API
  config.py                     # .mcp.json loading + ${ENV} expansion (reused)
  types.py                      # ServerConfig variants, ToolInfo, ServerState (reused)
  manager.py                    # MCPClientManager: lifecycle, tool discovery (reused core)
  client.py                     # MCPClient: single-server connection (reused)
  tool_adapter.py               # ★ NEW: MCP → ToolBuilder bridge
  circuit_breaker.py            # Circuit breaker (reused, zero changes)
  security.py                   # Credential sanitization, injection detection (reused)
  transport/                    # 5 transport types (reused)
    __init__.py, base.py, factory.py
    stdio.py, sse.py, http.py, ws.py, in_process.py
  oauth.py                      # OAuth PKCE (reused, Phase 2 enablement)
  sampling.py                   # Sampling extension point (reused, Phase 3)
```

## Core Component: tool_adapter.py

### Dynamic Schema → Pydantic Model

```python
def _json_schema_to_pydantic(schema: dict) -> type[BaseModel]:
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields = {}
    for name, prop in properties.items():
        py_type = _json_type_to_python(prop)  # string→str, number→float, etc.
        default = ... if name in required else None
        fields[name] = (py_type, default)
    return create_model("MCPParams", **fields)
```

Fallback: `$ref`, `anyOf`, nested objects → `dict[str, Any]` passthrough.

### MCPToolBuilder

```python
class MCPToolBuilder(ToolBuilder):
    """Each MCP tool gets one MCPToolBuilder instance registered in ToolRegistry."""
    name: str              # "mcp__filesystem__read_file"
    description: str
    parameters_schema: type[BaseModel]
    _server_name: str
    _original_name: str
    _manager: MCPClientManager

    def validate(self, params):
        return self.parameters_schema(**params)

    def build(self, params):
        return MCPToolInvocation(
            manager=self._manager,
            server_name=self._server_name,
            tool_name=self._original_name,
            params=params,
        )
```

### MCPToolInvocation

```python
class MCPToolInvocation(ToolInvocation):
    async def execute(self) -> ToolExecutionResult:
        result = await self._manager.call_tool(
            self._server_name, self._tool_name, self.params.model_dump()
        )
        text = _extract_text(result.content)
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS if not result.is_error else ToolStatus.ERROR,
            content=text,
        )
```

## Existing Code Changes

All changes are minimal (~10 lines each), complexity isolated in `mcp/`.

### multiclaw.toml — new `[mcp]` section

```toml
[mcp]
enabled = true
# config_path defaults to auto-search: ~/.mcp.json → ./.mcp.json → parents
```

### config/settings.py

New `McpSettings` model with `enabled: bool = True` and `config_path: str = ""`.

### server.py — lifespan integration

```python
# startup
if settings.mcp.enabled:
    mcp_manager = MCPClientManager()
    configs = load_mcp_config(settings.mcp.config_path)
    await mcp_manager.connect_servers(configs)
    for tool in mcp_manager.get_all_tools():
        tool_registry.register(MCPToolBuilder.from_tool_info(tool, mcp_manager))

# shutdown
await mcp_manager.stop()
```

### tools/registry.py

No changes. `ToolRegistry` depends only on `ToolBuilder` interface.

### agent/multiclaw.py

No changes. Agent loop calls `to_openai_schemas()` and `act()`, completely tool-source agnostic.

### tools/scheduler.py

Minor: detect `MCPToolBuilder` instances, skip permission check (already approved at server level), pass raw params through.

## Error Handling

| Layer | Scenario | Behavior |
|-------|----------|----------|
| Transport | Connection timeout (30s), process crash | MCPClient raises → Manager marks FAILED, circuit breaker counts |
| Circuit Breaker | 3 consecutive failures | Open for 60s, RuntimeError raised to caller |
| Tool call | Timeout (300s), server error | `ToolExecutionResult(status=ERROR)` → visible to LLM |
| Credential leak | Error messages contain API keys | `security.sanitize_error()` → `[REDACTED]` |
| Config | Malformed .mcp.json | warn + skip that server, startup continues |

## Frontend Impact

No changes required for Phase 1:
- Tool names (`mcp__...`) rendered normally by existing `ToolGroup` / `tool-fallback` components
- No per-tool approval UI needed (server-level approval)
- Tool results returned as `ToolExecutionResult.content: str`, same format as native tools

## Dependencies

New: `mcp>=1.0.0`, `anyio>=4.0.0`, `httpx>=0.25.0`, `websockets>=12.0`

## Phase Plan

| Phase | Scope | Dependencies |
|-------|-------|--------------|
| 1 | transport (stdio+sse+http), client, manager, tool_adapter, circuit_breaker, config, server.py integration | `mcp>=1.0.0`, `anyio`, `httpx`, `websockets` |
| 2 | OAuth PKCE, server-level approval UI | Phase 1 complete |
| 3 | Dynamic refresh (`tools/list_changed`), sampling hook | Phase 2 complete |
| 4 | Sidebar MCP status panel (frontend) | Phase 3 complete |

## Constraints

- MCP tools must use `mcp__{server}__{tool}` naming to avoid collisions with native tools
- Server-level permission only: all tools from an approved server are callable without per-call checks
- Complex JSON Schemas (`$ref`, `anyOf`) degrade to `dict` passthrough — enough for most real-world MCP servers
- `.mcp.json` auto-search stops at the nearest `.git` directory boundary
