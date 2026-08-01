# OmniAgent Phase 0 + Phase 1 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不复制 OmniAgent GPL 源码的前提下，为 MultiClaw 增加默认关闭的运行时韧性、有界只读工具并发、渐进式上下文预算，并默认启用 WebFetch SSRF 防护，同时修复会阻断这些能力的 MCP 基线问题。

**Status:** Phase 0/1 engineering implementation complete; credential rotation pending operator action.

**Architecture:** 保留现有 `MultiClawAgent → ToolRegistry → CoreToolScheduler` 主链路，新增小型、可单测的策略组件：`ResilienceController` 只判断是否反思/终止，`ToolBatchExecutor` 只规划并执行安全批次，`NetworkPolicy` 只验证 URL/DNS/重定向，`ContextBudget` 只分配 L0/L1/L2。两条 Agent 路径复用相同组件，所有新运行时能力由配置开关控制，静态安全校验不允许被关闭。

**Tech Stack:** Python 3.12、asyncio、Pydantic 2、httpx、Playwright（可选运行时依赖）、pytest、pytest-asyncio；不增加新依赖。

---

## File Map

**Create**

- `src/multiclaw/agent/resilience.py` — 工具调用/结果指纹、无进展判断和反思预算。
- `src/multiclaw/agent/tool_batch.py` — 连续只读批次规划、有界并发和稳定顺序回填。
- `src/multiclaw/context/__init__.py` — 渐进上下文预算公共 API。
- `src/multiclaw/context/budget.py` — Token 估算、L0/L1/L2 配额与构建报告。
- `src/multiclaw/tools/network_policy.py` — HTTP(S) URL、DNS 地址和重定向安全策略。
- `tests/test_agent_resilience.py` — 韧性策略纯单元测试。
- `tests/test_tool_batch.py` — 并发、串行屏障、取消和顺序测试。
- `tests/test_network_policy.py` — IPv4/IPv6/DNS/重定向安全测试。

**Modify**

- `src/multiclaw/config/settings.py` — 新增默认关闭的 agent/tools/context flags。
- `src/multiclaw/agent/context.py` — L0/L1/L2 预算和构建报告。
- `src/multiclaw/agent/multiclaw.py` — 两条 Agent 路径接入韧性和批处理。
- `src/multiclaw/tools/base.py` — 工具显式 `read_only` 元数据。
- `src/multiclaw/tools/scheduler.py` — 并发资格预检。
- `src/multiclaw/tools/{read_file,list_dir,glob,grep,find_dir,web_search,web_fetch}.py` — 标记只读工具。
- `src/multiclaw/tools/web_fetch.py` — 统一安全请求与浏览器子请求拦截。
- `src/multiclaw/tools/registry.py` — 线程安全的注册、注销和命名空间替换。
- `src/multiclaw/mcp/tool_adapter.py` — 同步 MCP 桥接移出主事件循环并传递只读元数据。
- `src/multiclaw/mcp/manager.py` — 动态工具变化回调。
- `src/multiclaw/server.py` — 传入新配置并同步 MCP 热更新。
- `tests/test_{config,context,agent,agent_stream_tool_ids,tools,web_fetch,mcp_tool_adapter,mcp_integration}.py` — 回归和集成覆盖。

## Task 0: Repair the Pre-Existing Test Baseline

**Files:**

- Modify: `tests/test_agent.py`
- Modify: `tests/test_frontend_debug.py`
- Modify: `tests/test_frontend_welcome.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Preserve the reproduced baseline evidence**

Run: `uv run pytest -q`
Expected before fixes: 4 failures and 192 passes. Failures are the stale relevant-memory index assertion, two tests that search the migrated React shell for removed inline scripts, and registry pollution from an auto-discovered MCP config.

- [ ] **Step 2: Make the context assertion semantic instead of positional**

```python
relevant_message = next(
    message
    for message in messages
    if message["role"] == "system"
    and "Relevant memory:" in message["content"]
)
assert "alpha project uses SQLite memory" in relevant_message["content"]
assert messages[-1] == {"role": "user", "content": "what does alpha use?"}
```

- [ ] **Step 3: Point frontend migration tests at React sources**

`tests/test_frontend_debug.py` reads `frontend/src/App.tsx` and asserts `shouldLogChatDebug`, the localhost hostname guard, and the three `[chat]` debug calls remain present. `tests/test_frontend_welcome.py` reads `frontend/src/components/assistant-ui/thread.tsx` and asserts the welcome copy is nested under `ThreadPrimitive.Empty`. These tests must not execute or inspect the generated static asset hash.

- [ ] **Step 4: Isolate native-tool registry tests from local MCP configuration**

Add before `create_agent()`:

```python
monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
```

- [ ] **Step 5: Verify the repaired baseline**

Run: `uv run pytest tests/test_agent.py tests/test_frontend_debug.py tests/test_frontend_welcome.py tests/test_tools.py -q`
Expected: all focused tests PASS.

Run: `uv run pytest -q`
Expected: 196 tests PASS with zero failures; existing aiosqlite shutdown warnings may remain and must be reported separately.

## Task 1: Add Feature Flags Without Changing Existing Behavior

**Files:**

- Modify: `src/multiclaw/config/settings.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing configuration tests**

```python
def test_phase_one_features_default_off():
    settings = Settings(_config_file="/nonexistent/multiclaw.toml")
    assert settings.agent.resilience_enabled is False
    assert settings.tools.parallel_read_only_enabled is False
    assert settings.memory.progressive_context_enabled is False
    assert settings.tools.web_fetch_allow_private_networks is False


def test_phase_one_features_load_from_environment(monkeypatch):
    monkeypatch.setenv("MULTICLAW_AGENT__RESILIENCE_ENABLED", "true")
    monkeypatch.setenv("MULTICLAW_TOOLS__PARALLEL_READ_ONLY_ENABLED", "true")
    monkeypatch.setenv("MULTICLAW_MEMORY__PROGRESSIVE_CONTEXT_ENABLED", "true")
    settings = Settings(_config_file="/nonexistent/multiclaw.toml")
    assert settings.agent.resilience_enabled is True
    assert settings.tools.parallel_read_only_enabled is True
    assert settings.memory.progressive_context_enabled is True
```

- [ ] **Step 2: Run tests and confirm the fields are absent**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL with missing `settings.tools` or missing new fields.

- [ ] **Step 3: Add the settings models and TOML mapping**

```python
class ToolSettings(BaseModel):
    parallel_read_only_enabled: bool = False
    parallel_max_concurrency: int = Field(default=4, ge=1, le=16)
    web_fetch_allow_private_networks: bool = False


class AgentSettings(BaseModel):
    max_tool_rounds: int = 10
    resilience_enabled: bool = False
    no_progress_repeat_limit: int = Field(default=3, ge=2, le=10)
    reflection_max_attempts: int = Field(default=1, ge=0, le=3)
```

Insert these fields immediately before the existing `system_prompt` field and leave the prompt text unchanged.

Add to `MemorySettings`:

```python
progressive_context_enabled: bool = False
context_response_reserve_tokens: int = Field(default=4096, ge=256)
context_l1_ratio: float = Field(default=0.6, gt=0.0, lt=1.0)
```

Add `tools: ToolSettings = Field(default_factory=ToolSettings)` to `Settings` and map `[tools]` in `_build_toml_kwargs`.

- [ ] **Step 4: Run configuration tests**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS.

## Task 2: Remove MCP Event-Loop Blocking and Close Dynamic Registration

**Files:**

- Modify: `src/multiclaw/tools/registry.py`
- Modify: `src/multiclaw/mcp/tool_adapter.py`
- Modify: `src/multiclaw/mcp/manager.py`
- Modify: `src/multiclaw/server.py`
- Modify: `tests/test_mcp_tool_adapter.py`
- Modify: `tests/test_mcp_integration.py`

- [ ] **Step 1: Write failing MCP regression tests**

```python
@pytest.mark.asyncio
async def test_mcp_invocation_does_not_block_running_event_loop():
    manager = Mock()
    manager.call_tool.side_effect = lambda *_args: (
        time.sleep(0.05) or ToolCallResult(content=[{"type": "text", "text": "ok"}], is_error=False)
    )
    invocation = MCPToolInvocation(manager, "server", "tool", create_model("P")())
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(3):
            await asyncio.sleep(0.01)
            ticks += 1

    result, _ = await asyncio.gather(invocation.execute(), ticker())
    assert result.content == "ok"
    assert ticks == 3


def test_registry_replaces_mcp_server_namespace():
    registry = ToolRegistry()
    registry.register(_builder("mcp__demo__old"))
    registry.replace_namespace("mcp__demo__", [_builder("mcp__demo__new")])
    assert [tool.name for tool in registry.list_all()] == ["mcp__demo__new"]
```

- [ ] **Step 2: Run the focused tests**

Run: `uv run pytest tests/test_mcp_tool_adapter.py tests/test_mcp_integration.py -q`
Expected: FAIL because MCP execution blocks and registry replacement does not exist.

- [ ] **Step 3: Implement the non-blocking bridge and registry replacement**

Use `await asyncio.to_thread(...)` in `MCPToolInvocation.execute()`:

```python
result = await asyncio.to_thread(
    self._manager.call_tool,
    self._server_name,
    self._tool_name,
    self.params.model_dump(),
)
```

Protect `ToolRegistry` with `threading.RLock` and add:

```python
def unregister(self, name: str) -> None:
    with self._lock:
        self._tools.pop(name, None)

def replace_namespace(self, prefix: str, builders: list[ToolBuilder[BaseModel]]) -> None:
    with self._lock:
        for name in [name for name in self._tools if name.startswith(prefix)]:
            del self._tools[name]
        for builder in builders:
            self._tools[builder.name] = builder
```

Add an optional manager callback:

```python
def set_tools_changed_callback(
    self, callback: Callable[[str, list[ToolInfo]], None] | None
) -> None:
    self._tools_changed_callback = callback
```

Call it from `_on_tools_changed` after state replacement. In `server.py`, use one helper for startup and refresh:

```python
def _build_mcp_adapters(
    server_name: str,
    tools: list[ToolInfo],
    manager: MCPClientManager,
    tool_filter: dict[str, list[str]] | None,
) -> list[MCPToolBuilder]:
    adapters = []
    for tool in tools:
        if tool_filter and not _matches_tool_filter(tool.original_name, tool_filter):
            continue
        adapters.append(MCPToolBuilder.from_tool_info(tool, manager))
    return adapters
```

The refresh callback computes the namespace with the same sanitization rule used by `MCPClient` and calls `registry.replace_namespace(prefix, adapters)`. Initial startup registers the same adapters one by one so existing startup logs remain accurate:

```python
def _mcp_namespace(server_name: str) -> str:
    safe_name = "".join(
        char if char.isalnum() or char in ("_", "-") else "_"
        for char in server_name
    )
    return f"mcp__{safe_name}__"
```

- [ ] **Step 4: Run focused and server tests**

Run: `uv run pytest tests/test_mcp_tool_adapter.py tests/test_mcp_integration.py tests/test_server.py -q`
Expected: PASS.

## Task 3: Implement the Pure Resilience Controller

**Files:**

- Create: `src/multiclaw/agent/resilience.py`
- Create: `tests/test_agent_resilience.py`

- [ ] **Step 1: Write failing pure unit tests**

```python
def test_repeated_tool_batch_requests_reflection_on_limit():
    controller = ResilienceController(repeat_limit=3, max_reflections=1)
    calls = [{"name": "web_search", "arguments": {"query": "same"}}]
    assert controller.observe_calls(calls).action == ResilienceAction.CONTINUE
    assert controller.observe_calls(calls).action == ResilienceAction.CONTINUE
    decision = controller.observe_calls(calls)
    assert decision.action == ResilienceAction.REFLECT
    assert "repeated tool call" in decision.reason


def test_repeated_result_after_reflection_terminates_when_budget_exhausted():
    controller = ResilienceController(repeat_limit=2, max_reflections=1)
    controller.observe_results(["Error: timeout"])
    assert controller.observe_results(["Error: timeout"]).action == ResilienceAction.REFLECT
    controller.mark_reflection_used()
    assert controller.observe_results(["Error: timeout"]).action == ResilienceAction.TERMINATE


def test_fingerprints_ignore_tool_call_ids_and_dict_order():
    left = [{"id": "a", "name": "grep", "arguments": {"path": ".", "pattern": "x"}}]
    right = [{"id": "b", "name": "grep", "arguments": {"pattern": "x", "path": "."}}]
    assert fingerprint_calls(left) == fingerprint_calls(right)
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_agent_resilience.py -q`
Expected: FAIL because the module is absent.

- [ ] **Step 3: Implement deterministic decisions**

Define:

```python
class ResilienceAction(str, Enum):
    CONTINUE = "continue"
    REFLECT = "reflect"
    TERMINATE = "terminate"

@dataclass(frozen=True)
class ResilienceDecision:
    action: ResilienceAction
    reason: str = ""

class ResilienceController:
    def __init__(self, repeat_limit: int, max_reflections: int) -> None:
        self.repeat_limit = repeat_limit
        self.max_reflections = max_reflections
        self.reflections_used = 0
        self._last_call_fingerprint = ""
        self._call_repeats = 0
        self._last_result_fingerprint = ""
        self._result_repeats = 0
```

`observe_calls` and `observe_results` increment only consecutive identical fingerprints. `_decision(reason)` returns `REFLECT` while `reflections_used < max_reflections`, otherwise `TERMINATE`. Fingerprints use `json.dumps(..., sort_keys=True, separators=(",", ":"))` and SHA-256; tool-call IDs are excluded.

- [ ] **Step 4: Run the unit tests**

Run: `uv run pytest tests/test_agent_resilience.py -q`
Expected: PASS.

## Task 4: Integrate Bounded Reflection Into Both Agent Paths

**Files:**

- Modify: `src/multiclaw/agent/multiclaw.py`
- Modify: `tests/test_agent.py`
- Modify: `tests/test_agent_stream_tool_ids.py`

- [ ] **Step 1: Write failing non-stream and stream integration tests**

The non-stream router returns the same tool call three times, then a reflection response, then a final answer:

```python
class RepeatingRouter:
    def __init__(self) -> None:
        self.calls = []
        self.responses = [
            LLMResponse(content="", tool_calls=[ToolCall(id=f"c{i}", name="echo", arguments={"text": "same"})])
            for i in range(3)
        ] + [
            LLMResponse(content="The query is unchanged; vary the input."),
            LLMResponse(content="changed approach"),
        ]

    async def completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)
```

After `handle_message`, assert:

```python
assert agent.act.await_count == 2
assert any("Runtime reflection required" in msg["content"] for msg in router.calls[3]["messages"])
assert observation.content == "changed approach"
```

The stream router emits the same call in three rounds, returns a plain-text reflection from `completion()`, then emits `changed approach` from `stream_completion()`. Collect events and assert:

```python
reflection = next(event for event in events if event["type"] == "state")
assert reflection["name"] == "reflection"
assert events[-1] == {"type": "done", "content": "changed approach"}
```

- [ ] **Step 2: Run focused tests**

Run: `uv run pytest tests/test_agent.py tests/test_agent_stream_tool_ids.py -q`
Expected: FAIL because repeated calls are still executed until `max_tool_rounds`.

- [ ] **Step 3: Add shared helper methods to `MultiClawAgent`**

```python
REFLECTION_PROMPT = (
    "Runtime reflection required. The previous approach made no progress: {reason}. "
    "Explain the likely root cause in at most 120 words and choose materially different "
    "tools or parameters. Do not call tools in this reflection."
)

async def _generate_reflection(self, messages: list[dict[str, Any]], reason: str) -> str:
    response = await self.router.completion(
        model=self.settings.llm.default_model,
        messages=[*messages, {"role": "system", "content": REFLECTION_PROMPT.format(reason=reason)}],
        tools=None,
    )
    return response.content.strip() or "Use a materially different approach."
```

Create one `ResilienceController` per request when `settings.agent.resilience_enabled` is true. Before executing a repeated batch, reflect and `continue` without running it. After results, observe result fingerprints and queue reflection for the next round. When the decision is `TERMINATE`, break into the existing forced-summary path. In the streaming path emit:

```python
yield {"type": "state", "name": "reflection", "content": reflection}
```

- [ ] **Step 4: Run agent regression tests**

Run: `uv run pytest tests/test_agent.py tests/test_agent_stream_tool_ids.py tests/test_chat_request_compat.py -q`
Expected: PASS with old tool IDs and final-summary behavior preserved.

## Task 5: Add Explicit Read-Only Metadata and a Pure Batch Executor

**Files:**

- Modify: `src/multiclaw/tools/base.py`
- Modify: `src/multiclaw/tools/scheduler.py`
- Modify: read-only native builders listed in File Map
- Modify: `src/multiclaw/mcp/tool_adapter.py`
- Create: `src/multiclaw/agent/tool_batch.py`
- Create: `tests/test_tool_batch.py`

- [ ] **Step 1: Write failing batch tests**

```python
@pytest.mark.asyncio
async def test_consecutive_read_only_calls_run_concurrently_and_keep_order():
    executor = ToolBatchExecutor(registry, scheduler, max_concurrency=2)
    started = asyncio.Event()
    calls = [call("slow_read", "a"), call("slow_read", "b")]
    outcomes = await executor.execute(calls)
    assert [item.call_id for item in outcomes] == ["a", "b"]
    assert max_active.value == 2


@pytest.mark.asyncio
async def test_write_call_is_a_serial_barrier():
    outcomes = await executor.execute([
        call("read", "r1"), call("write", "w"), call("read", "r2")
    ])
    assert timeline == ["r1:start", "r1:end", "w:start", "w:end", "r2:start", "r2:end"]
    assert [item.call_id for item in outcomes] == ["r1", "w", "r2"]


@pytest.mark.asyncio
async def test_approval_eligible_read_is_not_parallelized():
    assert await scheduler.can_run_concurrently(external_read_builder, params) is False
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_tool_batch.py -q`
Expected: FAIL because metadata, preflight and executor are absent.

- [ ] **Step 3: Implement metadata, preflight and stable batching**

Add to `ToolBuilder`:

```python
read_only: bool = False
```

Set `read_only = True` only on `read_file`, `list_dir`, `glob`, `grep`, `find_dir`, `web_search`, and `web_fetch`. MCP adapters are read-only only when the server annotation says `read_only=True` and both `destructive` and `open_world` are false.

Add scheduler preflight:

```python
async def can_run_concurrently(self, builder: ToolBuilder, raw_params: dict) -> bool:
    if not builder.read_only:
        return False
    workspace_root = getattr(builder, "workspace_root", None)
    decision = await self.permission_checker.check(
        builder.name, raw_params, workspace_root=workspace_root
    )
    return decision.allow and not decision.requires_approval
```

`ToolBatchExecutor.execute()` partitions calls into consecutive eligible runs. Eligible runs use a semaphore and `asyncio.gather`; barriers execute one at a time. Gather results are stored by original index and returned in input order. On cancellation, cancel unfinished tasks and await them with `return_exceptions=True` before re-raising.

- [ ] **Step 4: Run batch and tool tests**

Run: `uv run pytest tests/test_tool_batch.py tests/test_tools.py tests/test_governance.py -q`
Expected: PASS.

## Task 6: Integrate Tool Batches Into Both Agent Paths

**Files:**

- Modify: `src/multiclaw/agent/multiclaw.py`
- Modify: `tests/test_agent.py`
- Modify: `tests/test_agent_stream_tool_ids.py`

- [ ] **Step 1: Write failing agent-level concurrency tests**

For both paths, return two read-only tool calls in one model response. Configure `parallel_read_only_enabled=True`, make each invocation wait on the other, and assert both complete. Add a mixed read/write/read case and assert the write remains a barrier. Preserve every original call ID in messages and stream events.

- [ ] **Step 2: Run focused tests**

Run: `uv run pytest tests/test_agent.py tests/test_agent_stream_tool_ids.py -q`
Expected: FAIL or deadlock because the current loops execute each call serially.

- [ ] **Step 3: Normalize calls and use `ToolBatchExecutor`**

Create `ToolCallSpec(call_id, name, arguments)` and `ToolCallOutcome(call_id, name, observation)` in `tool_batch.py`. Initialize one executor in `MultiClawAgent.__init__`. Replace both per-call loops with:

```python
outcomes = await self.tool_batch_executor.execute(call_specs)
for outcome in outcomes:
    messages.append(_build_tool_result_msg(outcome.call_id, outcome.observation.content))
    await self.remember(outcome.observation.content, "tool_result")
```

The stream path must emit all `tool_call` events first, then emit ordered `tool_result` events after execution. If the flag is false, the executor treats every call as a barrier and exactly preserves legacy behavior.

- [ ] **Step 4: Run agent and SSE tests**

Run: `uv run pytest tests/test_agent.py tests/test_agent_stream_tool_ids.py tests/test_chat_request_compat.py tests/test_stream.py -q`
Expected: PASS.

## Task 7: Add HTTP(S), DNS and Redirect SSRF Enforcement

**Files:**

- Create: `src/multiclaw/tools/network_policy.py`
- Modify: `src/multiclaw/tools/web_fetch.py`
- Create: `tests/test_network_policy.py`
- Modify: `tests/test_web_fetch.py`

- [ ] **Step 1: Write failing policy tests**

```python
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://127.0.0.1/admin",
    "http://[::1]/admin",
    "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.1/",
    "http://localhost/",
])
def test_policy_blocks_non_public_targets(url):
    with pytest.raises(NetworkPolicyError):
        policy.validate_url(url)


def test_policy_blocks_hostname_when_any_dns_answer_is_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_answers("93.184.216.34", "127.0.0.1"))
    with pytest.raises(NetworkPolicyError):
        policy.validate_url("https://mixed.example/path")


def test_http_fetch_revalidates_redirect_target(monkeypatch):
    fake_client = FakeHttpxClient([
        FakeHttpxResponse(status_code=302, headers={"location": "http://127.0.0.1/admin"})
    ])
    monkeypatch.setattr("httpx.Client", lambda **_kwargs: fake_client)
    result = await build_fetch("https://public.example/start")
    assert result.status == "error"
    assert "blocked network target" in result.content.lower()
```

- [ ] **Step 2: Run policy and fetch tests**

Run: `uv run pytest tests/test_network_policy.py tests/test_web_fetch.py -q`
Expected: FAIL because URLs are only normalized today.

- [ ] **Step 3: Implement public-network validation and manual redirects**

`NetworkPolicy.validate_url()` must require `http` or `https`, reject credentials in URLs, require a hostname, resolve with `socket.getaddrinfo`, normalize IPv4-mapped IPv6, and require every answer to satisfy `ip.is_global`. `allow_private_networks=True` may bypass address classification only; it must not enable non-HTTP schemes or URL credentials.

In WebFetch, replace `follow_redirects=True` with a shared helper that follows at most five redirects:

```python
def _request_with_policy(self, client: httpx.Client, url: str) -> httpx.Response:
    current = url
    for _ in range(6):
        self.network_policy.validate_url(current)
        response = client.get(current, follow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current = urljoin(current, location)
    raise NetworkPolicyError("too many redirects")
```

Use it in light and markdown modes. Convert `NetworkPolicyError` to a stable tool error without including resolved internal addresses beyond the rejected host.

- [ ] **Step 4: Run security tests**

Run: `uv run pytest tests/test_network_policy.py tests/test_web_fetch.py -q`
Expected: PASS.

## Task 8: Apply the Same Network Policy to Browser Mode

**Files:**

- Modify: `src/multiclaw/tools/web_fetch.py`
- Modify: `src/multiclaw/server.py`
- Modify: `tests/test_web_fetch.py`

- [ ] **Step 1: Write failing browser-route tests**

Use fake route/request objects and capture the callback installed by `page.route("**/*", callback)`:

```python
safe = FakeRoute("https://example.com/app.js")
blocked = FakeRoute("http://127.0.0.1/admin")
route_callback(safe)
route_callback(blocked)
assert safe.continued is True
assert blocked.aborted is True

builder = WebFetchToolBuilder(allow_private_networks=True)
invocation = builder.build(builder.validate({"url": "http://127.0.0.1"}))
assert invocation.network_policy.allow_private_networks is True
```

- [ ] **Step 2: Run browser tests**

Run: `uv run pytest tests/test_web_fetch.py -q`
Expected: FAIL because browser mode has no route policy.

- [ ] **Step 3: Install the route guard before navigation**

```python
def guard(route) -> None:
    try:
        self.network_policy.validate_url(route.request.url)
    except NetworkPolicyError:
        route.abort()
    else:
        route.continue_()

page.route("**/*", guard)
self.network_policy.validate_url(url)
page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
```

Pass `settings.tools.web_fetch_allow_private_networks` from `create_agent()` into `WebFetchToolBuilder`.

- [ ] **Step 4: Run all web tool tests**

Run: `uv run pytest tests/test_web_fetch.py tests/test_web_search.py -q`
Expected: PASS.

## Task 9: Implement Progressive L0/L1/L2 Context Budgeting

**Files:**

- Create: `src/multiclaw/context/__init__.py`
- Create: `src/multiclaw/context/budget.py`
- Modify: `src/multiclaw/agent/context.py`
- Modify: `tests/test_context.py`

- [ ] **Step 1: Write failing budget and ordering tests**

```python
@pytest.mark.asyncio
async def test_progressive_context_prioritizes_l0_then_newest_l1_then_l2():
    builder = ContextBuilder(
        memory=memory,
        recent_turns=8,
        context_history_ratio=0.5,
        progressive_enabled=True,
        response_reserve_tokens=16,
        l1_ratio=0.6,
    )
    result = await builder.build_with_report(request(context_window_limit=80))
    assert result.messages[0]["content"] == "system"
    assert result.messages[-1]["content"] == "current question"
    assert "newest history" in contents(result.messages)
    assert "oldest history" not in contents(result.messages)
    assert result.report.dropped_by_level["L1"] >= 1


def test_estimate_tokens_is_conservative_and_deterministic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
```

- [ ] **Step 2: Run context tests**

Run: `uv run pytest tests/test_context.py -q`
Expected: FAIL because progressive mode and reports do not exist.

- [ ] **Step 3: Add build result, report and budget policy**

Define in `src/multiclaw/context/budget.py` and export from `src/multiclaw/context/__init__.py`:

```python
@dataclass(frozen=True)
class ContextBuildReport:
    limit_tokens: int
    reserved_response_tokens: int
    used_tokens_by_level: dict[str, int]
    dropped_by_level: dict[str, int]

@dataclass(frozen=True)
class ContextBuildResult:
    messages: list[dict]
    report: ContextBuildReport

def estimate_tokens(text: str) -> int:
    return 0 if not text else (len(text) + 3) // 4
```

Keep `build()` as a compatibility wrapper returning `result.messages`. `build_with_report()` uses legacy logic when the flag is false. In progressive mode:

- L0: system prompt, temporal anchor, current user input; always retained.
- L1: active skill prompts and newest chat history; receives `remaining * l1_ratio` and selects newest history before restoring chronological order.
- L2: relevant non-chat memory; consumes the rest in relevance order.
- If L0 alone exceeds the available budget, retain the full system prompt and user input, drop temporal anchor first, and record the drop; never truncate the current user input.

- [ ] **Step 4: Run context tests**

Run: `uv run pytest tests/test_context.py -q`
Expected: PASS with legacy ordering tests unchanged.

## Task 10: Wire Context Reports and Finish Verification

**Files:**

- Modify: `src/multiclaw/agent/multiclaw.py`
- Modify: `tests/test_agent.py`
- Modify: `docs/superpowers/specs/2026-07-28-omniagent-migration-design.md`

- [ ] **Step 1: Write a failing agent wiring test**

Configure progressive context on and replace `build_with_report` with an `AsyncMock`. Assert both stream and non-stream paths use `.messages` and log one structured report containing used/dropped counts, without adding the report to the model prompt.

- [ ] **Step 2: Run agent wiring tests**

Run: `uv run pytest tests/test_agent.py tests/test_agent_stream_tool_ids.py -q`
Expected: FAIL because the agent still calls `build()`.

- [ ] **Step 3: Wire `build_with_report` and log observability**

Use one helper:

```python
async def _build_context(self, request: ContextRequest) -> list[dict[str, Any]]:
    result = await self.context_builder.build_with_report(request)
    logger.info(
        "context_budget used=%s dropped=%s limit=%d reserve=%d",
        result.report.used_tokens_by_level,
        result.report.dropped_by_level,
        result.report.limit_tokens,
        result.report.reserved_response_tokens,
    )
    return result.messages
```

Update the migration spec status to `Phase 0/1 engineering implementation complete; credential rotation pending operator action` only after the full verification below succeeds. Add a short “implementation evidence” section listing commands and results; do not claim credential rotation, Phase 2, or Phase 3.

- [ ] **Step 4: Run backend verification**

Run: `uv run pytest -q`
Expected: all tests PASS with zero failures.

- [ ] **Step 5: Run frontend verification**

Run: `npm run lint` in `frontend/`
Expected: exit 0.

Run: `npm run build` in `frontend/`
Expected: exit 0.

- [ ] **Step 6: Run static diff checks**

Run: `git diff --check`
Expected: exit 0.

Run: `git status --short`
Expected: only the migration files plus the user's pre-existing `src/multiclaw/skills/{activation,parser,types}.py` and `AGENTS.md` changes are present; no credential files are added or printed.

## Execution Notes

- Implement in a dedicated worktree because the primary worktree contains unrelated user changes.
- Fresh implementation workers must not read `/Users/felix/git/OmniAgent`; this plan and the approved design are the complete behavioral specification.
- Workers own only the files assigned per task and must not revert shared or user-owned changes.
- Do not add dependencies, edit credential-bearing configuration files, or enable experimental flags globally.
- Credential rotation and history-cleanup decisions remain an operator action because they affect external systems; report this as an open Phase 0 item instead of modifying secrets automatically.
- Commits, if created in the isolated worktree, must follow the repository Lore Commit Protocol and name the exact tests run. Do not commit or stage the user's existing changes.
