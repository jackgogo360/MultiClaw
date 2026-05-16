# Foundation Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the 4 foundational packages (config, events, llm, storage) with mock data strategy, async throughout, independently testable.

**Architecture:** Greenfield Python project with `uv` + `src` layout. All I/O is async. Packages communicate via a lightweight EventBus (async pub/sub). Settings loaded from TOML via pydantic-settings. LLM routing uses capability tags. Storage uses Repository[T] pattern with aiosqlite backend returning mock data.

**Tech Stack:** Python 3.12+, uv, pydantic-settings, aiosqlite, pytest + pytest-asyncio, httpx (for llm providers)

**Files to create (15 files):**

```
MultiClaw/
├── pyproject.toml
├── config/multiclaw.toml
├── src/multiclaw/
│   ├── __init__.py
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py
│   ├── events/
│   │   ├── __init__.py
│   │   ├── bus.py
│   │   └── types.py
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── router.py
│   │   └── providers.py
│   └── storage/
│       ├── __init__.py
│       ├── repository.py
│       └── sqlite.py
└── tests/
    ├── conftest.py
    ├── test_config.py
    ├── test_events.py
    ├── test_llm.py
    └── test_storage.py
```

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `config/multiclaw.toml`
- Create: `src/multiclaw/__init__.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "multiclaw"
version = "0.1.0"
description = "MultiClaw Agent Runtime"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "aiosqlite>=0.20",
    "httpx>=0.27",
    "tomli>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create config/multiclaw.toml**

```toml
[app]
name = "MultiClaw"
version = "0.1.0"
debug = false

[database]
path = "data/multiclaw.db"

[llm]
default_provider = "openai"
default_model = "gpt-4o"

[llm.providers.openai]
api_key = ""
base_url = "https://api.openai.com/v1"

[llm.providers.anthropic]
api_key = ""
base_url = "https://api.anthropic.com"

[llm.capability_tags]
"gpt-4o" = ["text", "function_calling", "vision"]
"gpt-4o-mini" = ["text", "function_calling"]
"claude-opus-4-7" = ["text", "function_calling", "vision", "extended_thinking"]
"claude-sonnet-4-6" = ["text", "function_calling", "vision"]

[memory]
short_term_limit = 100
context_window_limit = 128000

[governance]
sandbox_mode = "process"
audit_enabled = true
```

- [ ] **Step 3: Create src/multiclaw/__init__.py**

```python
"""MultiClaw - Agent Runtime Core."""
```

- [ ] **Step 4: Install dependencies and verify**

Run: `uv sync`
Expected: Dependencies installed, virtual env created

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock config/multiclaw.toml src/multiclaw/__init__.py
git commit -m "feat: scaffold project with uv + src layout"
```

---

### Task 2: Config package (pydantic-settings)

**Files:**
- Create: `src/multiclaw/config/__init__.py`
- Create: `src/multiclaw/config/settings.py`
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/conftest.py`:

```python
import pytest
from pathlib import Path


@pytest.fixture
def test_config_path(tmp_path):
    config_file = tmp_path / "multiclaw.toml"
    config_file.write_text("""
[app]
name = "TestApp"
version = "0.0.1"
debug = true

[database]
path = ":memory:"

[llm]
default_provider = "openai"
default_model = "gpt-4o-mini"

[llm.providers.openai]
api_key = "test-key"
base_url = "https://test.openai.com"

[llm.capability_tags]
"gpt-4o-mini" = ["text", "function_calling"]

[memory]
short_term_limit = 50
context_window_limit = 64000

[governance]
sandbox_mode = "process"
audit_enabled = false
""")
    return config_file
```

Create `tests/test_config.py`:

```python
import os
from multiclaw.config.settings import Settings, AppSettings, DatabaseSettings, LLMSettings, MemorySettings, GovernanceSettings


class TestSettings:
    def test_loads_from_toml_file(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.app.name == "TestApp"
        assert settings.app.version == "0.0.1"
        assert settings.app.debug is True

    def test_database_settings(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.database.path == ":memory:"

    def test_llm_settings(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.llm.default_provider == "openai"
        assert settings.llm.default_model == "gpt-4o-mini"
        assert settings.llm.providers["openai"]["api_key"] == "test-key"
        assert settings.llm.capability_tags["gpt-4o-mini"] == ["text", "function_calling"]

    def test_memory_settings(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.memory.short_term_limit == 50
        assert settings.memory.context_window_limit == 64000

    def test_governance_settings(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))

        assert settings.governance.sandbox_mode == "process"
        assert settings.governance.audit_enabled is False

    def test_env_var_override(self, test_config_path, monkeypatch):
        monkeypatch.setenv("MULTICLAW_APP__NAME", "EnvApp")
        settings = Settings(_config_file=str(test_config_path))

        assert settings.app.name == "EnvApp"

    def test_default_config_path_fallback(self, monkeypatch, tmp_path):
        default_config = tmp_path / "multiclaw.toml"
        default_config.write_text("""
[app]
name = "DefaultApp"
version = "9.9.9"
debug = false

[database]
path = "default.db"

[llm]
default_provider = "anthropic"
default_model = "claude-sonnet-4-6"

[llm.providers.anthropic]
api_key = ""
base_url = "https://api.anthropic.com"

[llm.capability_tags]
"claude-sonnet-4-6" = ["text", "function_calling"]

[memory]
short_term_limit = 100
context_window_limit = 128000

[governance]
sandbox_mode = "docker"
audit_enabled = true
""")
        monkeypatch.chdir(tmp_path)
        settings = Settings()

        assert settings.app.name == "DefaultApp"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — ModuleNotFoundError (no module 'multiclaw.config')

- [ ] **Step 3: Write minimal implementation**

Create `src/multiclaw/config/__init__.py`:

```python
from multiclaw.config.settings import Settings, AppSettings, DatabaseSettings, LLMSettings, MemorySettings, GovernanceSettings

__all__ = ["Settings", "AppSettings", "DatabaseSettings", "LLMSettings", "MemorySettings", "GovernanceSettings"]
```

Create `src/multiclaw/config/settings.py`:

```python
from pathlib import Path
from typing import Any
import tomllib

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseModel):
    name: str = "MultiClaw"
    version: str = "0.1.0"
    debug: bool = False


class DatabaseSettings(BaseModel):
    path: str = "data/multiclaw.db"


class LLMProviderSettings(BaseModel):
    api_key: str = ""
    base_url: str = ""


class LLMSettings(BaseModel):
    default_provider: str = "openai"
    default_model: str = "gpt-4o"
    providers: dict[str, dict[str, str]] = {}
    capability_tags: dict[str, list[str]] = {}


class MemorySettings(BaseModel):
    short_term_limit: int = 100
    context_window_limit: int = 128000


class GovernanceSettings(BaseModel):
    sandbox_mode: str = "process"
    audit_enabled: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MULTICLAW_",
        env_nested_delimiter="__",
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)

    def __init__(self, _config_file: str | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        config_path = Path(_config_file) if _config_file else Path("multiclaw.toml")
        if config_path.exists():
            self._load_toml(config_path)

    def _load_toml(self, path: Path) -> None:
        with open(path, "rb") as f:
            data = tomli.load(f)

        if "app" in data:
            self.app = AppSettings(**data["app"])
        if "database" in data:
            self.database = DatabaseSettings(**data["database"])
        if "llm" in data:
            providers_raw = data["llm"].pop("providers", {})
            capability_tags_raw = data["llm"].pop("capability_tags", {})
            self.llm = LLMSettings(
                providers=providers_raw,
                capability_tags=capability_tags_raw,
                **data["llm"],
            )
        if "memory" in data:
            self.memory = MemorySettings(**data["memory"])
        if "governance" in data:
            self.governance = GovernanceSettings(**data["governance"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/config/ tests/conftest.py tests/test_config.py
git commit -m "feat: add config package with pydantic-settings and TOML loading"
```

---

### Task 3: Events package (EventBus)

**Files:**
- Create: `src/multiclaw/events/__init__.py`
- Create: `src/multiclaw/events/types.py`
- Create: `src/multiclaw/events/bus.py`
- Create: `tests/test_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_events.py`:

```python
import asyncio
import pytest
from multiclaw.events.types import Event, AgentStateEvent, AgentState
from multiclaw.events.bus import EventBus


class TestEvent:
    def test_event_serializes_to_dict(self):
        event = AgentStateEvent(
            agent_id="agent-1",
            from_state=AgentState.IDLE,
            to_state=AgentState.THINKING,
        )
        d = event.model_dump()

        assert d["type"] == "agent.state_change"
        assert d["agent_id"] == "agent-1"
        assert d["from_state"] == "IDLE"
        assert d["to_state"] == "THINKING"
        assert "timestamp" in d

    def test_event_timestamp_is_utc(self):
        event = Event(type="test.event", data={})
        assert event.timestamp.tzinfo is not None


class TestEventBus:
    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test.event", handler)
        event = Event(type="test.event", data={"msg": "hello"})
        await bus.publish(event)

        assert len(received) == 1
        assert received[0].type == "test.event"
        assert received[0].data == {"msg": "hello"}

    @pytest.mark.asyncio
    async def test_multiple_handlers(self):
        bus = EventBus()
        results = []

        bus.subscribe("test.event", lambda e: results.append("a"))
        bus.subscribe("test.event", lambda e: results.append("b"))
        await bus.publish(Event(type="test.event", data={}))

        assert results == ["a", "b"]

    @pytest.mark.asyncio
    async def test_wildcard_handler(self):
        bus = EventBus()
        received = []

        bus.subscribe("*", lambda e: received.append(e.type))
        await bus.publish(Event(type="foo.bar", data={}))
        await bus.publish(Event(type="baz.qux", data={}))

        assert received == ["foo.bar", "baz.qux"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        sub_id = bus.subscribe("test.event", handler)
        bus.unsubscribe(sub_id)
        await bus.publish(Event(type="test.event", data={}))

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash_bus(self):
        bus = EventBus()
        second_called = False

        async def failing_handler(event):
            raise RuntimeError("boom")

        async def good_handler(event):
            nonlocal second_called
            second_called = True

        bus.subscribe("test.event", failing_handler)
        bus.subscribe("test.event", good_handler)
        await bus.publish(Event(type="test.event", data={}))

        assert second_called is True

    @pytest.mark.asyncio
    async def test_publish_is_async_non_blocking(self):
        bus = EventBus()
        slow_done = False

        async def slow_handler(event):
            nonlocal slow_done
            await asyncio.sleep(0.1)
            slow_done = True

        bus.subscribe("test.event", slow_handler)
        t0 = asyncio.get_event_loop().time()
        await bus.publish(Event(type="test.event", data={}))
        elapsed = asyncio.get_event_loop().time() - t0

        # Publish returns after all handlers complete (ordered execution)
        # slow_done should be True since publish awaits handlers
        assert slow_done is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_events.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

Create `src/multiclaw/events/__init__.py`:

```python
from multiclaw.events.bus import EventBus
from multiclaw.events.types import Event, AgentStateEvent, AgentState

__all__ = ["EventBus", "Event", "AgentStateEvent", "AgentState"]
```

Create `src/multiclaw/events/types.py`:

```python
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentState(str, Enum):
    IDLE = "IDLE"
    THINKING = "THINKING"
    ACTING = "ACTING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    FINISHED = "FINISHED"
    ERROR = "ERROR"


class Event(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentStateEvent(Event):
    type: str = "agent.state_change"
    agent_id: str
    from_state: AgentState
    to_state: AgentState
```

Create `src/multiclaw/events/bus.py`:

```python
import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Awaitable

from multiclaw.events.types import Event

logger = logging.getLogger(__name__)

Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[str, Handler]]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> str:
        sub_id = uuid.uuid4().hex
        self._handlers[event_type].append((sub_id, handler))
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        for event_type in list(self._handlers):
            self._handlers[event_type] = [
                (sid, h) for sid, h in self._handlers[event_type] if sid != sub_id
            ]

    async def publish(self, event: Event) -> None:
        handlers = self._handlers.get(event.type, []) + self._handlers.get("*", [])
        for _sub_id, handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception("Error in event handler for %s", event.type)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_events.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/events/ tests/test_events.py
git commit -m "feat: add events package with EventBus async pub/sub"
```

---

### Task 4: LLM package (ModelRouter + providers)

**Files:**
- Create: `src/multiclaw/llm/__init__.py`
- Create: `src/multiclaw/llm/providers.py`
- Create: `src/multiclaw/llm/router.py`
- Create: `tests/test_llm.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_llm.py`:

```python
import pytest
from multiclaw.config.settings import Settings
from multiclaw.llm.providers import ProviderAdapter, OpenAIAdapter, AnthropicAdapter
from multiclaw.llm.router import ModelRouter, CapabilityTag


class TestProviderAdapters:
    def test_openai_adapter_formats_request(self):
        adapter = OpenAIAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
        request = adapter.build_request(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )

        assert request["url"] == "https://api.openai.com/v1/chat/completions"
        assert request["headers"]["Authorization"] == "Bearer sk-test"
        assert request["headers"]["Content-Type"] == "application/json"
        assert request["body"]["model"] == "gpt-4o-mini"
        assert request["body"]["messages"] == [{"role": "user", "content": "hello"}]

    def test_anthropic_adapter_formats_request(self):
        adapter = AnthropicAdapter(api_key="ant-test", base_url="https://api.anthropic.com")
        request = adapter.build_request(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )

        assert request["url"] == "https://api.anthropic.com/v1/messages"
        assert request["headers"]["x-api-key"] == "ant-test"
        assert request["headers"]["anthropic-version"] == "2023-06-01"
        assert request["body"]["model"] == "claude-sonnet-4-6"

    def test_openai_adapter_parses_response(self):
        adapter = OpenAIAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
        raw = {
            "choices": [{"message": {"role": "assistant", "content": "hi there"}}],
        }
        result = adapter.parse_response(raw)

        assert result.content == "hi there"
        assert result.role == "assistant"
        assert result.tool_calls == []

    def test_openai_adapter_parses_tool_call_response(self):
        adapter = OpenAIAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
        raw = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "function": {"name": "read_file", "arguments": '{"path": "/tmp/x"}'},
                            }
                        ],
                    }
                }
            ],
        }
        result = adapter.parse_response(raw)

        assert result.content == ""
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"
        assert result.tool_calls[0].arguments == {"path": "/tmp/x"}

    def test_anthropic_adapter_parses_response(self):
        adapter = AnthropicAdapter(api_key="ant-test", base_url="https://api.anthropic.com")
        raw = {
            "content": [{"type": "text", "text": "hello from claude"}],
        }
        result = adapter.parse_response(raw)

        assert result.content == "hello from claude"
        assert result.role == "assistant"


class TestModelRouter:
    @pytest.fixture
    def router(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))
        return ModelRouter(settings)

    def test_router_loads_capability_tags(self, router):
        tags = router.list_models()
        assert "gpt-4o-mini" in tags
        assert router.has_capability("gpt-4o-mini", CapabilityTag.TEXT)

    def test_route_selects_model_with_required_capability(self, router):
        model = router.route(required=[CapabilityTag.TEXT])

        assert model in ("gpt-4o-mini",)

    def test_route_returns_default_when_no_constraints(self, router):
        model = router.route()

        assert model == "gpt-4o-mini"

    def test_route_raises_when_no_model_has_capability(self, router):
        with pytest.raises(ValueError, match="No model found"):
            router.route(required=[CapabilityTag.VISION])

    def test_get_adapter_returns_correct_provider(self, router):
        adapter = router.get_adapter("gpt-4o-mini")

        assert isinstance(adapter, OpenAIAdapter)
        assert adapter.api_key == "test-key"

    def test_completion_returns_mock_response(self, router):
        result = router.completion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hello"}],
        )

        assert result.content != ""
        assert result.role == "assistant"


class TestCapabilityTag:
    def test_capability_tags_are_strings(self):
        assert CapabilityTag.TEXT == "text"
        assert CapabilityTag.FUNCTION_CALLING == "function_calling"
        assert CapabilityTag.VISION == "vision"
        assert CapabilityTag.EXTENDED_THINKING == "extended_thinking"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

Create `src/multiclaw/llm/__init__.py`:

```python
from multiclaw.llm.providers import (
    ProviderAdapter,
    OpenAIAdapter,
    AnthropicAdapter,
    LLMResponse,
    ToolCall,
)
from multiclaw.llm.router import ModelRouter, CapabilityTag

__all__ = [
    "ProviderAdapter",
    "OpenAIAdapter",
    "AnthropicAdapter",
    "LLMResponse",
    "ToolCall",
    "ModelRouter",
    "CapabilityTag",
]
```

Create `src/multiclaw/llm/providers.py`:

```python
import json
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class ToolCall(BaseModel):
    id: str = ""
    name: str
    arguments: dict[str, Any] = {}


class LLMResponse(BaseModel):
    content: str
    role: str = "assistant"
    tool_calls: list[ToolCall] = []


class ProviderAdapter(ABC):
    def __init__(self, api_key: str = "", base_url: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    def build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    @abstractmethod
    def parse_response(self, raw: dict[str, Any]) -> LLMResponse: ...


class OpenAIAdapter(ProviderAdapter):
    def build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        return {
            "url": f"{self.base_url}/chat/completions",
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "body": body,
        }

    def parse_response(self, raw: dict[str, Any]) -> LLMResponse:
        choice = raw["choices"][0]["message"]
        tool_calls = []
        if choice.get("tool_calls"):
            for tc in choice["tool_calls"]:
                tool_calls.append(
                    ToolCall(
                        id=tc.get("id", ""),
                        name=tc["function"]["name"],
                        arguments=json.loads(tc["function"]["arguments"]),
                    )
                )
        return LLMResponse(
            content=choice.get("content") or "",
            role=choice.get("role", "assistant"),
            tool_calls=tool_calls,
        )


class AnthropicAdapter(ProviderAdapter):
    def build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": 4096,
        }
        if tools:
            body["tools"] = tools
        return {
            "url": f"{self.base_url}/v1/messages",
            "headers": {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            "body": body,
        }

    def parse_response(self, raw: dict[str, Any]) -> LLMResponse:
        text_parts = []
        for block in raw.get("content", []):
            if block["type"] == "text":
                text_parts.append(block["text"])
        return LLMResponse(
            content="\n".join(text_parts),
            role="assistant",
        )
```

Create `src/multiclaw/llm/router.py`:

```python
import json
from enum import Enum

from multiclaw.config.settings import Settings
from multiclaw.llm.providers import (
    LLMResponse,
    ProviderAdapter,
    OpenAIAdapter,
    AnthropicAdapter,
)


class CapabilityTag(str, Enum):
    TEXT = "text"
    FUNCTION_CALLING = "function_calling"
    VISION = "vision"
    EXTENDED_THINKING = "extended_thinking"


_PROVIDER_MAP: dict[str, type[ProviderAdapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
}


class ModelRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._capability_tags: dict[str, list[str]] = settings.llm.capability_tags
        self._adapters: dict[str, ProviderAdapter] = {}
        self._model_provider: dict[str, str] = {}

        for provider_name, provider_config in settings.llm.providers.items():
            adapter_cls = _PROVIDER_MAP.get(provider_name)
            if adapter_cls:
                adapter = adapter_cls(
                    api_key=provider_config.get("api_key", ""),
                    base_url=provider_config.get("base_url", ""),
                )
                self._adapters[provider_name] = adapter
                for model in self._capability_tags:
                    self._model_provider[model] = provider_name

    def list_models(self) -> list[str]:
        return list(self._capability_tags)

    def has_capability(self, model: str, capability: CapabilityTag) -> bool:
        tags = self._capability_tags.get(model, [])
        return capability.value in tags

    def route(
        self,
        required: list[CapabilityTag] | None = None,
        preferred: list[CapabilityTag] | None = None,
    ) -> str:
        required = required or []
        candidates = [
            model
            for model, tags in self._capability_tags.items()
            if all(c.value in tags for c in required)
        ]
        if not candidates:
            raise ValueError(
                f"No model found with required capabilities: {[c.value for c in required]}"
            )
        return candidates[0]

    def get_adapter(self, model: str) -> ProviderAdapter | None:
        provider = self._model_provider.get(model) or self._settings.llm.default_provider
        return self._adapters.get(provider)

    def completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            content='{"action": "mock_response", "message": "This is a mock LLM response"}',
            role="assistant",
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_llm.py -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/llm/ tests/test_llm.py
git commit -m "feat: add llm package with ModelRouter and provider adapters"
```

---

### Task 5: Storage package (Repository pattern + SQLite)

**Files:**
- Create: `src/multiclaw/storage/__init__.py`
- Create: `src/multiclaw/storage/repository.py`
- Create: `src/multiclaw/storage/sqlite.py`
- Create: `tests/test_storage.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_storage.py`:

```python
import pytest
from pydantic import BaseModel
from multiclaw.storage.repository import Repository
from multiclaw.storage.sqlite import SqliteRepository, SqliteConfig


class TestEntity(BaseModel):
    id: str = ""
    name: str
    value: int = 0


@pytest.fixture
async def repo():
    cfg = SqliteConfig(database_path=":memory:")
    r = SqliteRepository[TestEntity](
        entity_type=TestEntity,
        table_name="test_entities",
        config=cfg,
    )
    await r.initialize()
    return r


class TestSqliteRepository:
    @pytest.mark.asyncio
    async def test_save_and_get(self, repo):
        entity = TestEntity(name="item1", value=42)
        saved = await repo.save(entity)

        assert saved.id != ""
        retrieved = await repo.get(saved.id)
        assert retrieved is not None
        assert retrieved.name == "item1"
        assert retrieved.value == 42

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, repo):
        result = await repo.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_with_no_filters(self, repo):
        await repo.save(TestEntity(name="a", value=1))
        await repo.save(TestEntity(name="b", value=2))

        results = await repo.list({})
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_list_with_filters(self, repo):
        await repo.save(TestEntity(name="alpha", value=10))
        await repo.save(TestEntity(name="beta", value=10))
        await repo.save(TestEntity(name="gamma", value=20))

        results = await repo.list({"value": 10})
        assert len(results) == 2
        assert all(r.value == 10 for r in results)

    @pytest.mark.asyncio
    async def test_delete(self, repo):
        saved = await repo.save(TestEntity(name="to_delete", value=99))
        await repo.delete(saved.id)

        result = await repo.get(saved.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_does_not_raise(self, repo):
        await repo.delete("nonexistent")

    @pytest.mark.asyncio
    async def test_save_preserves_existing_id(self, repo):
        entity = TestEntity(id="custom-id-123", name="custom", value=7)
        saved = await repo.save(entity)

        assert saved.id == "custom-id-123"
        retrieved = await repo.get("custom-id-123")
        assert retrieved is not None
        assert retrieved.name == "custom"

    @pytest.mark.asyncio
    async def test_save_updates_existing(self, repo):
        saved = await repo.save(TestEntity(name="original", value=1))
        saved.value = 99
        updated = await repo.save(saved)

        assert updated.id == saved.id
        retrieved = await repo.get(saved.id)
        assert retrieved is not None
        assert retrieved.value == 99

    @pytest.mark.asyncio
    async def test_repository_protocol_is_abstract(self):
        with pytest.raises(TypeError):
            Repository[TestEntity]()  # type: ignore[abstract]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage.py -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

Create `src/multiclaw/storage/__init__.py`:

```python
from multiclaw.storage.repository import Repository
from multiclaw.storage.sqlite import SqliteRepository, SqliteConfig

__all__ = ["Repository", "SqliteRepository", "SqliteConfig"]
```

Create `src/multiclaw/storage/repository.py`:

```python
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

T = TypeVar("T")


class Repository(ABC, Generic[T]):
    @abstractmethod
    async def get(self, id: str) -> T | None: ...

    @abstractmethod
    async def save(self, entity: T) -> T: ...

    @abstractmethod
    async def delete(self, id: str) -> None: ...

    @abstractmethod
    async def list(self, filters: dict) -> list[T]: ...
```

Create `src/multiclaw/storage/sqlite.py`:

```python
import json
import uuid
from typing import TypeVar
from pydantic import BaseModel
import aiosqlite

from multiclaw.storage.repository import Repository

T = TypeVar("T", bound=BaseModel)


class SqliteConfig(BaseModel):
    database_path: str = "data/multiclaw.db"


class SqliteRepository(Repository[T]):
    def __init__(
        self,
        entity_type: type[T],
        table_name: str,
        config: SqliteConfig,
    ) -> None:
        self._entity_type = entity_type
        self._table_name = table_name
        self._config = config
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self._config.database_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table_name} (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
            """
        )
        await self._db.commit()

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.initialize()
        assert self._db is not None
        return self._db

    async def get(self, id: str) -> T | None:
        db = await self._ensure_db()
        cursor = await db.execute(
            f"SELECT data FROM {self._table_name} WHERE id = ?", (id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._entity_type.model_validate_json(row[0])

    async def save(self, entity: T) -> T:
        db = await self._ensure_db()
        if not entity.id:
            entity.id = uuid.uuid4().hex
        data = entity.model_dump_json()
        await db.execute(
            f"INSERT OR REPLACE INTO {self._table_name} (id, data) VALUES (?, ?)",
            (entity.id, data),
        )
        await db.commit()
        return entity

    async def delete(self, id: str) -> None:
        db = await self._ensure_db()
        await db.execute(f"DELETE FROM {self._table_name} WHERE id = ?", (id,))
        await db.commit()

    async def list(self, filters: dict) -> list[T]:
        db = await self._ensure_db()
        cursor = await db.execute(f"SELECT data FROM {self._table_name}")
        rows = await cursor.fetchall()
        results = [self._entity_type.model_validate_json(row[0]) for row in rows]
        if filters:
            results = [
                r
                for r in results
                if all(
                    getattr(r, k, None) == v for k, v in filters.items()
                )
            ]
        return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/storage/ tests/test_storage.py
git commit -m "feat: add storage package with Repository pattern and SQLite backend"
```

---

### Task 6: Integration — run full test suite

- [ ] **Step 1: Run all tests together**

Run: `uv run pytest tests/ -v`
Expected: 34 PASS (7 config + 8 events + 10 llm + 9 storage)

- [ ] **Step 2: Verify no dependency issues**

Run: `uv run python -c "from multiclaw.config import Settings; from multiclaw.events import EventBus; from multiclaw.llm import ModelRouter; from multiclaw.storage import SqliteRepository; print('All imports OK')"`
Expected: `All imports OK`

- [ ] **Step 3: Commit if any files were touched during integration fixes**

```bash
git add -A
git commit -m "chore: verify full test suite passes"
```