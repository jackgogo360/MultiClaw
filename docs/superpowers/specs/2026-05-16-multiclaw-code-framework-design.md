# MultiClaw Python Code Framework Design

**Date:** 2026-05-16
**Status:** Approved
**Scope:** MVP + extension point skeletons

## Decisions

| Decision | Choice |
|----------|--------|
| Scope | MVP + extension point skeletons |
| Runtime | Async + FastAPI + WebSocket |
| Package tool | uv + src layout |
| Architecture | Clear hierarchy + Protocol interfaces + EventBus (OpenManus + Gemini CLI hybrid) |

## References

- OpenManus: Agent class hierarchy (BaseAgent → ReActAgent → ToolCallAgent → Manus)
- Gemini CLI: ToolBuilder → ToolInvocation pattern + CoreToolScheduler state machine
- HermesAgent: Callback system, gateway/channel abstraction
- OpenCode: Event-driven SSE bus, multi-client architecture

---

## 1. Directory Structure

```
MultiClaw/
├── pyproject.toml
├── src/multiclaw/
│   ├── config/           # Settings (pydantic-settings), TOML config
│   ├── events/           # Lightweight EventBus (async pub/sub)
│   ├── agent/            # BaseAgent → ReActAgent → ToolCallAgent → MultiClawAgent
│   ├── planner/          # Plan Mode: Planner, Plan, PlanStep
│   ├── tools/            # ToolBuilder → ToolInvocation, ToolRegistry, CoreToolScheduler
│   ├── memory/           # MemoryProtocol, short/long-term memory, Memory sub-agent
│   ├── knowledge/        # Obsidian vault management, vector indexing, RAG retrieval
│   ├── llm/              # ModelRouter with capability tags, multi-provider adapters
│   ├── storage/          # Repository[T] pattern, SQLite backend, vector DB abstraction
│   ├── governance/       # PermissionChecker, Sandbox, AuditLogger
│   ├── channel/          # ChannelProtocol, Web/CLI adapters
│   ├── skill/            # SkillRegistry skeleton (future)
│   ├── tenant/           # TenantContext skeleton (future)
│   └── web/              # FastAPI app, REST routes, WebSocket, middleware
├── tests/
└── config/multiclaw.toml
```

14 packages, ~55 module files.

## 2. Core Class Hierarchy

### 2.1 Agent Chain (OpenManus style)

```
BaseAgent (ABC)
  - AgentState state machine: IDLE → THINKING → ACTING → WAITING_APPROVAL → FINISHED | ERROR
  - run() main loop, stuck detection
  - holds: MemoryProtocol, ToolCollection, EventBus

ReActAgent (ABC)
  - step() = think() → act() (Template Method)
  - think() → Action, act() → Observation

ToolCallAgent
  - think(): assemble prompt → call LLM → parse function_call
  - act(): lookup tool → schedule → execute → collect result
  - holds: ToolCollection, tool_choices (auto|required|none)

MultiClawAgent
  - Integrates Planner, Knowledge, Channel
  - plan_mode: plan → user review → execute
  - RAG context injection + audit logging
```

### 2.2 Tool System (Gemini CLI style)

```
ToolBuilder[TParams, TResult] (Protocol)
  - name, description, parameters_schema
  - validate(params) → TParams
  - build(params) → ToolInvocation

ToolInvocation[TParams, TResult]
  - Encapsulates single call, separates definition from execution
  - execute() → TResult

CoreToolScheduler
  - Queue: Scheduled → Validating → AwaitingApproval → Executing → Success|Error|Cancelled
  - Permission gate, EventBus publishing

ToolRegistry
  - register(tool), get(name), list_all(toolset)
```

### 2.3 Communication Protocols

```python
class MemoryProtocol(Protocol):
    async def save(self, entry: MemoryEntry) -> None: ...
    async def query(self, query: str, top_k: int) -> list[MemoryEntry]: ...
    async def forget(self, entry_id: str) -> None: ...

class ChannelProtocol(Protocol):
    async def send(self, message: AgentMessage) -> None: ...
    async def receive(self) -> AsyncIterator[UserMessage]: ...
    def channel_type(self) -> str: ...

class Repository[T](Protocol):
    async def get(self, id: str) -> T | None: ...
    async def save(self, entity: T) -> T: ...
    async def delete(self, id: str) -> None: ...
    async def list(self, filters: dict) -> list[T]: ...
```

EventBus for cross-module events: `publish(event)` / `subscribe(event_type, handler)`.

### 2.4 Core Data Models (Pydantic)

- `AgentState`: IDLE, THINKING, ACTING, WAITING_APPROVAL, FINISHED, ERROR
- `Action`: next step (tool_call | response | plan | ask_user)
- `Observation`: result (tool_result | user_response | plan_approved | error)
- `MemoryEntry`: id, content, embedding, type, tenant_id, created_at
- `Plan`: id, steps: list[PlanStep], status, approved_by
- `PlanStep`: order, description, tool_call, expected_outcome
- `ToolCall`: tool_name, params, status, result, timestamps
- `AuditLog`: id, tenant_id, user_id, action, tool_call, result, timestamp

## 3. Key Flows

### 3.1 Agent Main Loop

User message → Channel.receive()
  → Knowledge RAG retrieval → inject context
  → Short-term memory loading
  → Plan Mode branch: complex task → plan → user review → step execution
  → think(): system prompt → ModelRouter.route() → LLM → function_call/text
  → act(): PermissionChecker → ToolScheduler → Sandbox → AuditLogger
  → Observation → loop back to think()
  → FINISHED → Memory sub-agent refine → knowledge draft → user review → vault

### 3.2 WebSocket Real-time Push

Agent events (ToolStarted, ToolProgress, ThinkingStep, PlanGenerated, ToolCompleted) → EventBus → ws.send() → Web Dashboard real-time rendering

### 3.3 Memory Lifecycle

During session: user/agent messages → ShortTermMemory → context window overflow → ContextCompressor
Session end: Memory sub-agent scans → extract valuable info → KnowledgeDraft → user review → vault → vector embedding
Long-term: UserPreference detection → LongTermMemory update → cross-session retrieval

## 4. Mock Data Strategy

All classes in this scaffolding phase return mock data:
- `ModelRouter.route()` returns a mock LLM response
- `ToolInvocation.execute()` returns canned results
- `Vault.list_notes()` returns empty or sample Markdown notes
- `MemoryProtocol` implementations return empty/fake entries
- `EventBus` logs events but no real side effects
- WebSocket pushes mock events for dashboard testing

## 5. Extension Points (Skeletons)

- `skill/` — SkillRegistry, SkillScanner interface (for future plugin marketplace)
- `tenant/` — TenantContext, migration interface (for future multi-tenancy)
- `governance/sandbox.py` — Docker sandbox path (MVP uses process isolation only)
- `channel/cli.py` — CLI adapter skeleton (MVP uses web only)

---

## 6. Out of Scope

- Multi-tenancy implementation (skeleton only)
- Team collaboration features
- Dynamic Skill marketplace
- Docker sandbox (process isolation only for MVP)
- Mobile app / IDE extension channels
- Multi-model multimodal adaptation
- Enterprise compliance features
