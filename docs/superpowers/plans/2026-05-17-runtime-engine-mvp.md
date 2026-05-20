# Runtime Engine MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the missing runtime-engine packages on top of the foundation layer so MultiClaw can plan, schedule mock tools, track memory, and execute a minimal async agent loop entirely in-process.

**Architecture:** Keep this phase strictly in-process and mock-backed. The runtime engine is composed of five tightly related subsystems: governance, tools, memory, planner, and agent. All I/O remains async, `EventBus` stays the cross-module event backbone, and the agent loop uses the existing `ModelRouter` mock completion path until a later web/channel plan wraps it in FastAPI + WebSocket delivery.

**Tech Stack:** Python 3.12+, uv, pydantic v2, pytest + pytest-asyncio, existing `multiclaw.config`, `multiclaw.events`, `multiclaw.llm`, and `multiclaw.storage`

**Scope Split:** This plan intentionally covers the runtime engine only. `web/`, `channel/`, `knowledge/`, `skill/`, and `tenant/` should be planned separately after this plan lands and the core loop is stable.

**Files to create (20 files):**

```text
MultiClaw/
├── src/multiclaw/
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── models.py
│   │   ├── multiclaw.py
│   │   ├── react.py
│   │   └── toolcall.py
│   ├── governance/
│   │   ├── __init__.py
│   │   ├── audit.py
│   │   ├── models.py
│   │   ├── permission.py
│   │   └── sandbox.py
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── in_memory.py
│   │   ├── models.py
│   │   └── protocol.py
│   ├── planner/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   └── planner.py
│   └── tools/
│       ├── __init__.py
│       ├── base.py
│       ├── registry.py
│       └── scheduler.py
└── tests/
    ├── test_agent.py
    ├── test_governance.py
    ├── test_memory.py
    ├── test_planner.py
    └── test_tools.py
```

---

### Task 1: Governance package

**Files:**
- Create: `src/multiclaw/governance/__init__.py`
- Create: `src/multiclaw/governance/models.py`
- Create: `src/multiclaw/governance/permission.py`
- Create: `src/multiclaw/governance/sandbox.py`
- Create: `src/multiclaw/governance/audit.py`
- Test: `tests/test_governance.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_governance.py`:

```python
import pytest

from multiclaw.governance import (
    AuditLog,
    InMemoryAuditLogger,
    PermissionChecker,
    ProcessSandbox,
)


class TestPermissionChecker:
    @pytest.mark.asyncio
    async def test_allows_safe_tool(self):
        checker = PermissionChecker(guarded_tools={"delete_file"})

        decision = await checker.check("echo")

        assert decision.allow is True
        assert decision.requires_approval is False
        assert decision.reason == "allowed"

    @pytest.mark.asyncio
    async def test_requires_approval_for_guarded_tool(self):
        checker = PermissionChecker(guarded_tools={"delete_file"})

        decision = await checker.check("delete_file")

        assert decision.allow is True
        assert decision.requires_approval is True
        assert decision.reason == "approval_required"


class TestAuditLogger:
    @pytest.mark.asyncio
    async def test_records_entries(self):
        logger = InMemoryAuditLogger()

        entry = await logger.record(
            tool_name="echo",
            status="success",
            detail="completed",
            user_id="user-1",
            tenant_id="tenant-1",
        )

        assert isinstance(entry, AuditLog)
        assert entry.tool_name == "echo"
        assert entry.status == "success"
        assert entry.detail == "completed"
        assert len(await logger.list_entries()) == 1


class TestProcessSandbox:
    @pytest.mark.asyncio
    async def test_executes_async_callable(self):
        sandbox = ProcessSandbox()

        async def run() -> str:
            return "done"

        result = await sandbox.run(run)

        assert result == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_governance.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'multiclaw.governance'`

- [ ] **Step 3: Write minimal implementation**

Create `src/multiclaw/governance/models.py`:

```python
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class PermissionDecision(BaseModel):
    allow: bool = True
    requires_approval: bool = False
    reason: str = "allowed"


class AuditLog(BaseModel):
    tool_name: str
    status: str
    detail: str
    user_id: str = ""
    tenant_id: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

Create `src/multiclaw/governance/permission.py`:

```python
from collections.abc import Iterable

from multiclaw.governance.models import PermissionDecision


class PermissionChecker:
    def __init__(self, guarded_tools: Iterable[str] | None = None) -> None:
        self._guarded_tools = set(guarded_tools or [])

    async def check(self, tool_name: str) -> PermissionDecision:
        if tool_name in self._guarded_tools:
            return PermissionDecision(
                allow=True,
                requires_approval=True,
                reason="approval_required",
            )
        return PermissionDecision(
            allow=True,
            requires_approval=False,
            reason="allowed",
        )
```

Create `src/multiclaw/governance/sandbox.py`:

```python
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class ProcessSandbox:
    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        return await operation()
```

Create `src/multiclaw/governance/audit.py`:

```python
from multiclaw.governance.models import AuditLog


class InMemoryAuditLogger:
    def __init__(self) -> None:
        self._entries: list[AuditLog] = []

    async def record(
        self,
        tool_name: str,
        status: str,
        detail: str,
        user_id: str = "",
        tenant_id: str = "",
    ) -> AuditLog:
        entry = AuditLog(
            tool_name=tool_name,
            status=status,
            detail=detail,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        self._entries.append(entry)
        return entry

    async def list_entries(self) -> list[AuditLog]:
        return list(self._entries)
```

Create `src/multiclaw/governance/__init__.py`:

```python
from multiclaw.governance.audit import InMemoryAuditLogger
from multiclaw.governance.models import AuditLog, PermissionDecision
from multiclaw.governance.permission import PermissionChecker
from multiclaw.governance.sandbox import ProcessSandbox

__all__ = [
    "AuditLog",
    "InMemoryAuditLogger",
    "PermissionChecker",
    "PermissionDecision",
    "ProcessSandbox",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_governance.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/governance tests/test_governance.py
git commit -F - <<'EOF'
Establish runtime governance primitives for tool execution

Add the minimal permission, sandbox, and audit components needed
to gate tool execution inside the in-process runtime loop.

Constraint: MVP keeps process-only sandboxing; Docker remains a future hook
Rejected: Real subprocess sandboxing now | too early before web/channel integration
Confidence: high
Scope-risk: narrow
Directive: Keep permission outcomes serializable because web delivery will stream them later
Tested: uv run pytest tests/test_governance.py -v
Not-tested: Cross-process isolation behavior
EOF
```

---

### Task 2: Tools package

**Files:**
- Create: `src/multiclaw/tools/__init__.py`
- Create: `src/multiclaw/tools/base.py`
- Create: `src/multiclaw/tools/registry.py`
- Create: `src/multiclaw/tools/scheduler.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools.py`:

```python
import pytest
from pydantic import BaseModel

from multiclaw.events import EventBus
from multiclaw.governance import InMemoryAuditLogger, PermissionChecker, ProcessSandbox
from multiclaw.tools import (
    CoreToolScheduler,
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)


class EchoParams(BaseModel):
    text: str


class EchoInvocation(ToolInvocation[EchoParams]):
    async def execute(self) -> ToolExecutionResult:
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=self.params.text,
            data={"echoed": self.params.text},
        )


class EchoToolBuilder(ToolBuilder[EchoParams]):
    name = "echo"
    description = "Echoes the supplied text"
    parameters_schema = EchoParams

    def validate(self, params: dict) -> EchoParams:
        return EchoParams(**params)

    def build(self, params: EchoParams) -> ToolInvocation[EchoParams]:
        return EchoInvocation(name=self.name, params=params)


class DeleteToolBuilder(EchoToolBuilder):
    name = "delete_file"


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        builder = EchoToolBuilder()

        registry.register(builder)

        assert registry.get("echo") is builder
        assert [tool.name for tool in registry.list_all()] == ["echo"]


class TestCoreToolScheduler:
    @pytest.fixture
    def scheduler(self):
        return CoreToolScheduler(
            permission_checker=PermissionChecker(guarded_tools={"delete_file"}),
            sandbox=ProcessSandbox(),
            audit_logger=InMemoryAuditLogger(),
            event_bus=EventBus(),
        )

    @pytest.mark.asyncio
    async def test_executes_safe_tool(self, scheduler):
        result = await scheduler.run(EchoToolBuilder(), {"text": "hello"})

        assert result.status == ToolStatus.SUCCESS
        assert result.content == "hello"
        assert result.data == {"echoed": "hello"}

    @pytest.mark.asyncio
    async def test_guarded_tool_waits_for_approval(self, scheduler):
        result = await scheduler.run(DeleteToolBuilder(), {"text": "danger"})

        assert result.status == ToolStatus.AWAITING_APPROVAL
        assert result.content == "approval required"

    @pytest.mark.asyncio
    async def test_records_audit_entries(self, scheduler):
        await scheduler.run(EchoToolBuilder(), {"text": "audit"})

        entries = await scheduler.audit_logger.list_entries()
        assert len(entries) == 1
        assert entries[0].tool_name == "echo"
        assert entries[0].status == "success"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'multiclaw.tools'`

- [ ] **Step 3: Write minimal implementation**

Create `src/multiclaw/tools/base.py`:

```python
from abc import ABC, abstractmethod
from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

TParams = TypeVar("TParams", bound=BaseModel)


class ToolStatus(str, Enum):
    SCHEDULED = "scheduled"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class ToolExecutionResult(BaseModel):
    status: ToolStatus
    content: str
    data: dict[str, str] = Field(default_factory=dict)


class ToolInvocation(ABC, Generic[TParams]):
    def __init__(self, name: str, params: TParams) -> None:
        self.name = name
        self.params = params

    @abstractmethod
    async def execute(self) -> ToolExecutionResult:
        raise NotImplementedError


class ToolBuilder(ABC, Generic[TParams]):
    name: str
    description: str
    parameters_schema: type[TParams]

    @abstractmethod
    def validate(self, params: dict) -> TParams:
        raise NotImplementedError

    @abstractmethod
    def build(self, params: TParams) -> ToolInvocation[TParams]:
        raise NotImplementedError
```

Create `src/multiclaw/tools/registry.py`:

```python
from multiclaw.tools.base import ToolBuilder


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolBuilder] = {}

    def register(self, builder: ToolBuilder) -> None:
        self._tools[builder.name] = builder

    def get(self, name: str) -> ToolBuilder | None:
        return self._tools.get(name)

    def list_all(self) -> list[ToolBuilder]:
        return [self._tools[name] for name in sorted(self._tools)]
```

Create `src/multiclaw/tools/scheduler.py`:

```python
from multiclaw.events import Event, EventBus
from multiclaw.governance import InMemoryAuditLogger, PermissionChecker, ProcessSandbox
from multiclaw.tools.base import ToolBuilder, ToolExecutionResult, ToolStatus


class CoreToolScheduler:
    def __init__(
        self,
        permission_checker: PermissionChecker,
        sandbox: ProcessSandbox,
        audit_logger: InMemoryAuditLogger,
        event_bus: EventBus,
    ) -> None:
        self.permission_checker = permission_checker
        self.sandbox = sandbox
        self.audit_logger = audit_logger
        self.event_bus = event_bus

    async def run(self, builder: ToolBuilder, raw_params: dict) -> ToolExecutionResult:
        await self.event_bus.publish(Event(type="tool.scheduled", data={"tool": builder.name}))
        params = builder.validate(raw_params)
        await self.event_bus.publish(Event(type="tool.validating", data={"tool": builder.name}))

        decision = await self.permission_checker.check(builder.name)
        if not decision.allow:
            result = ToolExecutionResult(
                status=ToolStatus.CANCELLED,
                content=decision.reason,
            )
            await self.audit_logger.record(builder.name, "cancelled", decision.reason)
            return result

        if decision.requires_approval:
            await self.event_bus.publish(
                Event(type="tool.awaiting_approval", data={"tool": builder.name})
            )
            await self.audit_logger.record(
                builder.name,
                "awaiting_approval",
                "approval required",
            )
            return ToolExecutionResult(
                status=ToolStatus.AWAITING_APPROVAL,
                content="approval required",
            )

        invocation = builder.build(params)
        await self.event_bus.publish(Event(type="tool.executing", data={"tool": builder.name}))
        try:
            result = await self.sandbox.run(invocation.execute)
        except Exception as exc:
            await self.audit_logger.record(builder.name, "error", str(exc))
            await self.event_bus.publish(
                Event(type="tool.error", data={"tool": builder.name, "error": str(exc)})
            )
            return ToolExecutionResult(status=ToolStatus.ERROR, content=str(exc))

        await self.audit_logger.record(builder.name, "success", result.content)
        await self.event_bus.publish(Event(type="tool.completed", data={"tool": builder.name}))
        return result
```

Create `src/multiclaw/tools/__init__.py`:

```python
from multiclaw.tools.base import (
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolStatus,
)
from multiclaw.tools.registry import ToolRegistry
from multiclaw.tools.scheduler import CoreToolScheduler

__all__ = [
    "CoreToolScheduler",
    "ToolBuilder",
    "ToolExecutionResult",
    "ToolInvocation",
    "ToolRegistry",
    "ToolStatus",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/tools tests/test_tools.py
git commit -F - <<'EOF'
Add tool registry and scheduler for the runtime core

Introduce the ToolBuilder/ToolInvocation split plus a minimal
async scheduler that validates, permissions, executes, and audits
mock tool calls through the EventBus.

Constraint: Scheduler must remain async and event-driven for later web streaming
Rejected: Inline tool execution from agents | would collapse scheduler boundaries too early
Confidence: high
Scope-risk: moderate
Directive: Preserve ToolStatus values because downstream plans will assert on them
Tested: uv run pytest tests/test_tools.py -v
Not-tested: Concurrent tool scheduling under load
EOF
```

---

### Task 3: Memory package

**Files:**
- Create: `src/multiclaw/memory/__init__.py`
- Create: `src/multiclaw/memory/models.py`
- Create: `src/multiclaw/memory/protocol.py`
- Create: `src/multiclaw/memory/in_memory.py`
- Test: `tests/test_memory.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory.py`:

```python
import pytest

from multiclaw.memory import InMemoryMemory, MemoryEntry


class TestInMemoryMemory:
    @pytest.mark.asyncio
    async def test_save_and_query(self):
        memory = InMemoryMemory()
        await memory.save(MemoryEntry(content="remember alpha", type="note"))
        await memory.save(MemoryEntry(content="remember beta", type="note"))

        results = await memory.query("alpha", top_k=5)

        assert len(results) == 1
        assert results[0].content == "remember alpha"

    @pytest.mark.asyncio
    async def test_forget_removes_entry(self):
        memory = InMemoryMemory()
        entry = await memory.save(MemoryEntry(content="erase me", type="note"))

        await memory.forget(entry.id)
        results = await memory.query("erase", top_k=5)

        assert results == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'multiclaw.memory'`

- [ ] **Step 3: Write minimal implementation**

Create `src/multiclaw/memory/models.py`:

```python
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    content: str
    type: str
    tenant_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, str] = Field(default_factory=dict)
```

Create `src/multiclaw/memory/protocol.py`:

```python
from typing import Protocol

from multiclaw.memory.models import MemoryEntry


class MemoryProtocol(Protocol):
    async def save(self, entry: MemoryEntry) -> MemoryEntry: ...

    async def query(self, query: str, top_k: int) -> list[MemoryEntry]: ...

    async def forget(self, entry_id: str) -> None: ...
```

Create `src/multiclaw/memory/in_memory.py`:

```python
from multiclaw.memory.models import MemoryEntry
from multiclaw.memory.protocol import MemoryProtocol


class InMemoryMemory(MemoryProtocol):
    def __init__(self) -> None:
        self._entries: list[MemoryEntry] = []

    async def save(self, entry: MemoryEntry) -> MemoryEntry:
        self._entries.append(entry)
        return entry

    async def query(self, query: str, top_k: int) -> list[MemoryEntry]:
        query_lower = query.lower()
        matches = [
            entry
            for entry in self._entries
            if query_lower in entry.content.lower()
        ]
        return matches[:top_k]

    async def forget(self, entry_id: str) -> None:
        self._entries = [entry for entry in self._entries if entry.id != entry_id]
```

Create `src/multiclaw/memory/__init__.py`:

```python
from multiclaw.memory.in_memory import InMemoryMemory
from multiclaw.memory.models import MemoryEntry
from multiclaw.memory.protocol import MemoryProtocol

__all__ = ["InMemoryMemory", "MemoryEntry", "MemoryProtocol"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_memory.py -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/memory tests/test_memory.py
git commit -F - <<'EOF'
Add in-memory short-term memory for the runtime loop

Create the minimal MemoryProtocol and an in-memory implementation
so agents can persist user and tool observations during a session.

Constraint: This phase uses substring retrieval only; embeddings come later
Rejected: SQLite-backed memory now | redundant before long-term memory behavior exists
Confidence: high
Scope-risk: narrow
Directive: Keep MemoryEntry fields stable because planner and knowledge layers will reuse them
Tested: uv run pytest tests/test_memory.py -v
Not-tested: Large-session memory pruning behavior
EOF
```

---

### Task 4: Planner package

**Files:**
- Create: `src/multiclaw/planner/__init__.py`
- Create: `src/multiclaw/planner/models.py`
- Create: `src/multiclaw/planner/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_planner.py`:

```python
from multiclaw.planner import PlanStatus, Planner


class TestPlanner:
    def test_simple_request_creates_single_step_plan(self):
        planner = Planner()

        plan = planner.create_plan("summarize the latest note")

        assert plan.status == PlanStatus.DRAFT
        assert len(plan.steps) == 1
        assert plan.steps[0].description == "summarize the latest note"

    def test_complex_request_splits_on_and(self):
        planner = Planner()

        plan = planner.create_plan("collect facts and summarize findings")

        assert len(plan.steps) == 2
        assert plan.steps[0].description == "collect facts"
        assert plan.steps[1].description == "summarize findings"

    def test_approve_sets_status_and_reviewer(self):
        planner = Planner()
        plan = planner.create_plan("draft answer")

        approved = planner.approve(plan, reviewer="user-1")

        assert approved.status == PlanStatus.APPROVED
        assert approved.approved_by == "user-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_planner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'multiclaw.planner'`

- [ ] **Step 3: Write minimal implementation**

Create `src/multiclaw/planner/models.py`:

```python
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


class PlanStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


class PlanStep(BaseModel):
    order: int
    description: str
    tool_name: str | None = None
    expected_outcome: str = ""


class Plan(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    steps: list[PlanStep]
    status: PlanStatus = PlanStatus.DRAFT
    approved_by: str | None = None
```

Create `src/multiclaw/planner/planner.py`:

```python
from multiclaw.planner.models import Plan, PlanStatus, PlanStep


class Planner:
    def create_plan(self, request: str) -> Plan:
        parts = [part.strip() for part in request.split(" and ") if part.strip()]
        if not parts:
            parts = [request.strip()]

        steps = [
            PlanStep(
                order=index,
                description=part,
                expected_outcome=f"completed: {part}",
            )
            for index, part in enumerate(parts, start=1)
        ]
        return Plan(steps=steps)

    def approve(self, plan: Plan, reviewer: str) -> Plan:
        plan.status = PlanStatus.APPROVED
        plan.approved_by = reviewer
        return plan

    def summary(self, plan: Plan) -> str:
        return " | ".join(
            f"{step.order}. {step.description}"
            for step in plan.steps
        )
```

Create `src/multiclaw/planner/__init__.py`:

```python
from multiclaw.planner.models import Plan, PlanStatus, PlanStep
from multiclaw.planner.planner import Planner

__all__ = ["Plan", "PlanStatus", "PlanStep", "Planner"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_planner.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/planner tests/test_planner.py
git commit -F - <<'EOF'
Introduce a minimal plan-mode domain model

Add draft plan objects plus a simple planner that can split
compound requests into ordered mock execution steps.

Constraint: Planner remains heuristic until real LLM-backed planning exists
Rejected: Embedding planning into the agent directly | weakens separable plan review flow
Confidence: high
Scope-risk: narrow
Directive: Keep PlanStep fields aligned with future scheduler integration
Tested: uv run pytest tests/test_planner.py -v
Not-tested: Nested or branching plan generation
EOF
```

---

### Task 5: Agent package

**Files:**
- Create: `src/multiclaw/agent/__init__.py`
- Create: `src/multiclaw/agent/models.py`
- Create: `src/multiclaw/agent/base.py`
- Create: `src/multiclaw/agent/react.py`
- Create: `src/multiclaw/agent/toolcall.py`
- Create: `src/multiclaw/agent/multiclaw.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent.py`:

```python
import pytest
from pydantic import BaseModel

from multiclaw.agent import MultiClawAgent, ObservationType
from multiclaw.config import Settings
from multiclaw.events import EventBus
from multiclaw.governance import InMemoryAuditLogger, PermissionChecker, ProcessSandbox
from multiclaw.llm import ModelRouter
from multiclaw.memory import InMemoryMemory
from multiclaw.planner import Planner
from multiclaw.tools import (
    CoreToolScheduler,
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)


class EchoParams(BaseModel):
    text: str


class EchoInvocation(ToolInvocation[EchoParams]):
    async def execute(self) -> ToolExecutionResult:
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=self.params.text,
            data={"echoed": self.params.text},
        )


class EchoToolBuilder(ToolBuilder[EchoParams]):
    name = "echo"
    description = "Echo tool"
    parameters_schema = EchoParams

    def validate(self, params: dict) -> EchoParams:
        return EchoParams(**params)

    def build(self, params: EchoParams) -> ToolInvocation[EchoParams]:
        return EchoInvocation(name=self.name, params=params)


@pytest.fixture
def agent(test_config_path):
    settings = Settings(_config_file=str(test_config_path))
    registry = ToolRegistry()
    registry.register(EchoToolBuilder())
    scheduler = CoreToolScheduler(
        permission_checker=PermissionChecker(),
        sandbox=ProcessSandbox(),
        audit_logger=InMemoryAuditLogger(),
        event_bus=EventBus(),
    )
    return MultiClawAgent(
        settings=settings,
        router=ModelRouter(settings),
        registry=registry,
        scheduler=scheduler,
        memory=InMemoryMemory(),
        planner=Planner(),
        event_bus=EventBus(),
    )


class TestMultiClawAgent:
    @pytest.mark.asyncio
    async def test_executes_tool_action(self, agent):
        observation = await agent.handle_message("tool:echo hello")

        assert observation.type == ObservationType.TOOL_RESULT
        assert observation.content == "hello"
        assert observation.data["echoed"] == "hello"

    @pytest.mark.asyncio
    async def test_uses_planner_for_plan_mode(self, agent):
        observation = await agent.handle_message("plan: collect facts and summarize findings")

        assert observation.type == ObservationType.USER_RESPONSE
        assert observation.content == "1. collect facts | 2. summarize findings"

    @pytest.mark.asyncio
    async def test_plain_message_uses_mock_llm_response(self, agent):
        observation = await agent.handle_message("hello")

        assert observation.type == ObservationType.USER_RESPONSE
        assert "mock_response" in observation.content

    @pytest.mark.asyncio
    async def test_saves_user_messages_to_memory(self, agent):
        await agent.handle_message("remember this")

        matches = await agent.memory.query("remember", top_k=5)
        assert len(matches) == 1
        assert matches[0].content == "remember this"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'multiclaw.agent'`

- [ ] **Step 3: Write minimal implementation**

Create `src/multiclaw/agent/models.py`:

```python
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    TOOL_CALL = "tool_call"
    RESPONSE = "response"
    PLAN = "plan"
    ASK_USER = "ask_user"


class ObservationType(str, Enum):
    TOOL_RESULT = "tool_result"
    USER_RESPONSE = "user_response"
    PLAN_APPROVED = "plan_approved"
    ERROR = "error"


class Action(BaseModel):
    type: ActionType
    content: str = ""
    tool_name: str = ""
    tool_params: dict[str, str] = Field(default_factory=dict)


class Observation(BaseModel):
    type: ObservationType
    content: str
    data: dict[str, str] = Field(default_factory=dict)


class UserMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    content: str


class AgentMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    content: str
```

Create `src/multiclaw/agent/base.py`:

```python
from abc import ABC, abstractmethod

from multiclaw.agent.models import Observation
from multiclaw.events import AgentState, AgentStateEvent, EventBus
from multiclaw.memory import InMemoryMemory, MemoryEntry


class BaseAgent(ABC):
    def __init__(self, memory: InMemoryMemory, event_bus: EventBus) -> None:
        self.memory = memory
        self.event_bus = event_bus
        self.state = AgentState.IDLE

    async def transition(self, next_state: AgentState) -> None:
        event = AgentStateEvent(
            agent_id=self.__class__.__name__,
            from_state=self.state,
            to_state=next_state,
        )
        self.state = next_state
        await self.event_bus.publish(event)

    async def remember(self, content: str, entry_type: str) -> None:
        await self.memory.save(MemoryEntry(content=content, type=entry_type))

    @abstractmethod
    async def handle_message(self, user_input: str) -> Observation:
        raise NotImplementedError
```

Create `src/multiclaw/agent/react.py`:

```python
from abc import ABC, abstractmethod

from multiclaw.agent.base import BaseAgent
from multiclaw.agent.models import Action, Observation
from multiclaw.events import AgentState


class ReActAgent(BaseAgent, ABC):
    async def step(self, user_input: str) -> Observation:
        await self.transition(AgentState.THINKING)
        action = await self.think(user_input)
        await self.transition(AgentState.ACTING)
        observation = await self.act(action)
        await self.transition(AgentState.FINISHED)
        return observation

    @abstractmethod
    async def think(self, user_input: str) -> Action:
        raise NotImplementedError

    @abstractmethod
    async def act(self, action: Action) -> Observation:
        raise NotImplementedError
```

Create `src/multiclaw/agent/toolcall.py`:

```python
from multiclaw.agent.models import Action, ActionType, Observation, ObservationType
from multiclaw.config import Settings
from multiclaw.llm import ModelRouter
from multiclaw.memory import InMemoryMemory
from multiclaw.tools import CoreToolScheduler, ToolRegistry
from multiclaw.events import EventBus
from multiclaw.agent.react import ReActAgent


class ToolCallAgent(ReActAgent):
    def __init__(
        self,
        settings: Settings,
        router: ModelRouter,
        registry: ToolRegistry,
        scheduler: CoreToolScheduler,
        memory: InMemoryMemory,
        event_bus: EventBus,
    ) -> None:
        super().__init__(memory=memory, event_bus=event_bus)
        self.settings = settings
        self.router = router
        self.registry = registry
        self.scheduler = scheduler

    async def think(self, user_input: str) -> Action:
        if user_input.startswith("tool:"):
            payload = user_input[len("tool:"):].strip()
            tool_name, text = payload.split(" ", 1)
            return Action(
                type=ActionType.TOOL_CALL,
                tool_name=tool_name,
                tool_params={"text": text},
            )

        response = self.router.completion(
            model=self.settings.llm.default_model,
            messages=[{"role": "user", "content": user_input}],
        )
        return Action(type=ActionType.RESPONSE, content=response.content)

    async def act(self, action: Action) -> Observation:
        if action.type == ActionType.TOOL_CALL:
            builder = self.registry.get(action.tool_name)
            if builder is None:
                return Observation(
                    type=ObservationType.ERROR,
                    content=f"unknown tool: {action.tool_name}",
                )
            result = await self.scheduler.run(builder, action.tool_params)
            return Observation(
                type=ObservationType.TOOL_RESULT,
                content=result.content,
                data=result.data,
            )

        return Observation(
            type=ObservationType.USER_RESPONSE,
            content=action.content,
        )
```

Create `src/multiclaw/agent/multiclaw.py`:

```python
from multiclaw.agent.models import Action, ActionType, Observation, ObservationType
from multiclaw.agent.toolcall import ToolCallAgent
from multiclaw.config import Settings
from multiclaw.events import EventBus
from multiclaw.llm import ModelRouter
from multiclaw.memory import InMemoryMemory
from multiclaw.planner import Planner
from multiclaw.tools import CoreToolScheduler, ToolRegistry


class MultiClawAgent(ToolCallAgent):
    def __init__(
        self,
        settings: Settings,
        router: ModelRouter,
        registry: ToolRegistry,
        scheduler: CoreToolScheduler,
        memory: InMemoryMemory,
        planner: Planner,
        event_bus: EventBus,
    ) -> None:
        super().__init__(
            settings=settings,
            router=router,
            registry=registry,
            scheduler=scheduler,
            memory=memory,
            event_bus=event_bus,
        )
        self.planner = planner

    async def handle_message(self, user_input: str) -> Observation:
        await self.remember(user_input, "user_message")
        if user_input.startswith("plan:"):
            request = user_input[len("plan:"):].strip()
            plan = self.planner.create_plan(request)
            return Observation(
                type=ObservationType.USER_RESPONSE,
                content=self.planner.summary(plan),
            )
        return await self.step(user_input)
```

Create `src/multiclaw/agent/__init__.py`:

```python
from multiclaw.agent.base import BaseAgent
from multiclaw.agent.models import (
    Action,
    ActionType,
    AgentMessage,
    Observation,
    ObservationType,
    UserMessage,
)
from multiclaw.agent.multiclaw import MultiClawAgent
from multiclaw.agent.react import ReActAgent
from multiclaw.agent.toolcall import ToolCallAgent

__all__ = [
    "Action",
    "ActionType",
    "AgentMessage",
    "BaseAgent",
    "MultiClawAgent",
    "Observation",
    "ObservationType",
    "ReActAgent",
    "ToolCallAgent",
    "UserMessage",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/agent tests/test_agent.py
git commit -F - <<'EOF'
Build the minimal async agent chain for MultiClaw

Add BaseAgent, ReActAgent, ToolCallAgent, and MultiClawAgent so
the runtime can remember messages, create mock plans, and route
tool invocations through the scheduler.

Constraint: LLM interaction remains mock-backed through ModelRouter.completion
Rejected: Real parsing of provider tool-calls now | unnecessary before scheduler/web integration
Confidence: medium
Scope-risk: moderate
Directive: Keep handle_message as the public entrypoint until channel/web adapters are introduced
Tested: uv run pytest tests/test_agent.py -v
Not-tested: Long-running multi-step run loop behavior
EOF
```

---

### Task 6: Integration verification

**Files:**
- Modify: `src/multiclaw/governance/__init__.py` if export fixes are needed
- Modify: `src/multiclaw/tools/__init__.py` if export fixes are needed
- Modify: `src/multiclaw/memory/__init__.py` if export fixes are needed
- Modify: `src/multiclaw/planner/__init__.py` if export fixes are needed
- Modify: `src/multiclaw/agent/__init__.py` if export fixes are needed

- [ ] **Step 1: Run the full runtime-engine test suite**

Run: `uv run pytest tests/test_governance.py tests/test_tools.py tests/test_memory.py tests/test_planner.py tests/test_agent.py -v`
Expected: `17 passed`

- [ ] **Step 2: Run the entire project test suite**

Run: `uv run pytest tests/ -v`
Expected: `53 passed`

- [ ] **Step 3: Verify imports for every new package**

Run: `uv run python -c "from multiclaw.agent import MultiClawAgent; from multiclaw.governance import PermissionChecker; from multiclaw.memory import InMemoryMemory; from multiclaw.planner import Planner; from multiclaw.tools import CoreToolScheduler; print('runtime imports ok')"`
Expected: `runtime imports ok`

- [ ] **Step 4: Commit any export or integration fixes**

```bash
git add src/multiclaw/agent src/multiclaw/governance src/multiclaw/memory src/multiclaw/planner src/multiclaw/tools tests
git commit -F - <<'EOF'
Verify the runtime engine works as an integrated in-process loop

Run the targeted and full test suites, fix any package export gaps,
and confirm the runtime-engine packages compose cleanly together.

Constraint: Verification must cover both the new runtime packages and the existing foundation layer
Confidence: high
Scope-risk: narrow
Directive: Do not proceed to web/channel planning until this suite is stable and repeatable
Tested: uv run pytest tests/ -v; uv run python -c "from multiclaw.agent import MultiClawAgent; from multiclaw.governance import PermissionChecker; from multiclaw.memory import InMemoryMemory; from multiclaw.planner import Planner; from multiclaw.tools import CoreToolScheduler; print('runtime imports ok')"
Not-tested: External HTTP runtime behavior
EOF
```
