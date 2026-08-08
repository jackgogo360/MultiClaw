# Native Sandbox Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add fail-closed native process isolation for shell, Python execution, and local MCP stdio servers using Seatbelt on macOS and nsjail on Linux.

**Architecture:** Replace the misleading timeout-only sandbox with an `ExecutionGuard` plus a runtime-owned `SandboxManager`. The manager selects and probes one OS backend, renders a common exec-form launch contract, gates risky registration, and delegates one-shot lifecycle management to `SandboxProcessRunner`; MCP stdio reuses the installed SDK with a wrapped command.

**Tech Stack:** Python 3.12+, asyncio, Pydantic v2, FastAPI, MCP Python SDK, pytest/pytest-asyncio, macOS Seatbelt, Linux nsjail. No new Python dependencies.

**Approved inputs:**

- Design: `docs/superpowers/specs/2026-08-03-native-sandbox-backends-design.md`
- PRD: `docs/superpowers/specs/2026-08-03-native-sandbox-backends-prd.md`
- Test spec: `docs/superpowers/specs/2026-08-03-native-sandbox-backends-test-spec.md`
- Ralph execution-gate mirrors: `.omx/plans/prd-native-sandbox-backends.md` and
  `.omx/plans/test-spec-native-sandbox-backends.md`

---

## Execution constraints

- Create an isolated worktree before source implementation.
- Use TDD for every behavior change: failing test, observed failure, minimal code,
  observed pass, focused commit.
- Do not weaken deny rules to make compatibility tests pass; repair runtime roots,
  env shaping, or capture semantics instead.
- Native sandbox tests must run on the host platform, not inside another restrictive
  agent/container sandbox.
- Do not enable a production `off` mode or automatic host fallback.
- Do not add a Python dependency. nsjail remains an external Linux prerequisite.

## File structure

### Create

- `src/multiclaw/governance/sandbox/__init__.py` — public sandbox API
- `src/multiclaw/governance/sandbox/errors.py` — typed configuration/probe/policy/launch errors
- `src/multiclaw/governance/sandbox/models.py` — requests, specs, results, policies, probes, readiness
- `src/multiclaw/governance/sandbox/execution_guard.py` — in-process timeout/cancellation only
- `src/multiclaw/governance/sandbox/environment.py` — env scrub and private home/tmp construction
- `src/multiclaw/governance/sandbox/backend.py` — backend/controller protocols and unsafe host backend
- `src/multiclaw/governance/sandbox/runner.py` — async one-shot process lifecycle
- `src/multiclaw/governance/sandbox/seatbelt_profiles.py` — reviewed static SBPL templates
- `src/multiclaw/governance/sandbox/seatbelt.py` — macOS rendering and behavioral probe
- `src/multiclaw/governance/sandbox/nsjail_profiles.py` — reviewed protobuf-text templates
- `src/multiclaw/governance/sandbox/nsjail.py` — Linux rendering and behavioral probe
- `src/multiclaw/governance/sandbox/manager.py` — OS selection, profile registry, readiness, event buffer
- `src/multiclaw/tools/_code_runner.py` — stdin-to-JSON code execution protocol
- `tests/sandbox_fakes.py` — ready/unavailable recording controllers for unit tests
- `tests/test_sandbox_models.py`
- `tests/test_sandbox_environment.py`
- `tests/test_sandbox_runner.py`
- `tests/test_sandbox_seatbelt.py`
- `tests/test_sandbox_nsjail.py`
- `tests/test_sandbox_manager.py`
- `tests/test_mcp_config.py`
- `tests/integration/test_sandbox_macos.py`
- `tests/integration/test_sandbox_linux.py`

### Modify

- `src/multiclaw/config/settings.py`
- `src/multiclaw/governance/__init__.py`
- `src/multiclaw/tools/base.py`
- `src/multiclaw/tools/scheduler.py`
- `src/multiclaw/tools/shell.py`
- `src/multiclaw/tools/code_exec.py`
- `src/multiclaw/mcp/types.py`
- `src/multiclaw/mcp/config.py`
- `src/multiclaw/mcp/manager.py`
- `src/multiclaw/mcp/transport/factory.py`
- `src/multiclaw/mcp/transport/stdio.py`
- `src/multiclaw/server.py`
- `src/multiclaw/auth/middleware.py`
- `multiclaw.toml`
- `config/multiclaw.toml`
- `pyproject.toml`
- focused existing tests listed in the test specification

### Delete

- `src/multiclaw/governance/sandbox.py` after its timeout behavior is moved to
  `sandbox/execution_guard.py`

## Specification coverage

| Approved requirement | Implemented by | Verified by |
| --- | --- | --- |
| FR-1 typed modes and legacy migration | Task 2 | CFG-01 through CFG-10 |
| FR-2 common contract and honest timeout boundary | Tasks 3-4 and 7 | MOD-01 through MOD-07; RUN-01 through RUN-10 |
| FR-3 environment and path policy | Tasks 3, 5-7, and 11 | ENV-01 through ENV-11; native path denials |
| FR-4 Seatbelt and nsjail backends | Tasks 5-6 | SB-01 through SB-06; NS-01 through NS-06 |
| FR-5 fail-closed readiness and registration | Tasks 7-8 | SRV-01 through SRV-05 |
| FR-6 shell compatibility | Tasks 1, 9, and 12 | SH-01 through SH-11 |
| FR-7 single-child Python execution | Tasks 1, 10, and 12 | PY-01 through PY-10 |
| FR-8 MCP transport matrix and explicit grants | Tasks 8 and 11 | MCP-01 through MCP-13 |
| FR-9 events, audit, and redaction | Tasks 7-13 | EVT-01 through EVT-05 plus response-redaction tests |
| Dual-platform release proof | Tasks 12-13 | macOS/Linux native gates and verifier review |

## Planned public contracts

Keep these names and signatures consistent across all tasks:

```python
class SandboxController(Protocol):
    @property
    def mode(self) -> Literal["auto", "host_unsafe_dev_only"]: ...
    @property
    def backend_name(self) -> str: ...
    @property
    def readiness(self) -> SandboxReadiness: ...
    def initialize(self) -> None: ...
    def is_profile_ready(self, profile_name: str) -> bool: ...
    def build_launch_spec(self, request: SandboxExecRequest) -> SandboxedLaunchSpec: ...
    async def run(self, request: SandboxExecRequest) -> SandboxExecResult: ...
    def record_blocked_capability(self, name: str, reason: str) -> None: ...
    def finalize_readiness(self) -> SandboxReadiness: ...
    def drain_startup_events(self) -> tuple[Event, ...]: ...
    def close(self) -> None: ...

class SandboxBackend(Protocol):
    name: str
    def probe(self, workspace_root: Path, policies: tuple[SandboxProfilePolicy, ...]) -> SandboxProbeResult: ...
    def build_launch_spec(
        self,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
        environment: SandboxEnvironment,
    ) -> SandboxedLaunchSpec: ...

class SandboxProcessRunner:
    async def run(
        self,
        spec: SandboxedLaunchSpec,
        timeout_seconds: float,
    ) -> SandboxExecResult: ...
```

---

### Task 1: Lock existing shell, code-exec, and scheduler behavior

**Files:**
- Modify: `tests/test_shell.py`
- Modify: `tests/test_code_exec.py`
- Modify: `tests/test_tools.py`

- [x] **Step 1: Add passing shell characterization tests**

Add explicit tests before changing production code:

```python
@pytest.mark.asyncio
async def test_shell_preserves_pipeline_redirect_quote_glob_and_env(workspace):
    builder = ShellToolBuilder(str(workspace))
    command = (
        "VALUE='a b'; export VALUE; "
        "touch one.py two.py; "
        "printf '%s\\n' *.py > files.txt; "
        "printf '%s|' \"$VALUE\"; tail -n 1 files.txt"
    )
    result = await builder.build(builder.validate({"command": command})).execute()
    assert result.status == "success"
    assert "a b|two.py" in result.content

@pytest.mark.asyncio
async def test_shell_preserves_nonzero_exit_code_and_stderr(workspace):
    builder = ShellToolBuilder(str(workspace))
    result = await builder.build(
        builder.validate({"command": "printf problem >&2; exit 7"})
    ).execute()
    assert "[stderr]" in result.content
    assert "problem" in result.content
    assert result.data == {"exit_code": 7}
```

- [x] **Step 2: Add exact code-exec result characterization tests**

```python
@pytest.mark.asyncio
async def test_code_exec_success_data_contract(tmp_path):
    result = await CodeExecToolBuilder(str(tmp_path)).build(
        CodeExecParams(code="print('ok')")
    ).execute()
    assert result.status == "success"
    assert result.data == {"success": True}

@pytest.mark.asyncio
async def test_code_exec_exception_data_contract(tmp_path):
    result = await CodeExecToolBuilder(str(tmp_path)).build(
        CodeExecParams(code="raise ValueError('bad')")
    ).execute()
    assert result.status == "success"
    assert result.data["success"] is False
    assert "ValueError: bad" in result.data["error"]

@pytest.mark.asyncio
async def test_code_exec_timeout_data_contract(tmp_path):
    result = await CodeExecToolBuilder(str(tmp_path)).build(
        CodeExecParams(code="while True: pass", timeout=0.2)
    ).execute()
    assert result.status == "success"
    assert result.data == {}
    assert "timed out" in result.content.lower()
```

- [x] **Step 3: Add scheduler event-order characterization**

Subscribe a wildcard handler and assert successful guarded execution retains:

```python
assert seen == [
    "tool.scheduled",
    "tool.validating",
    "tool.executing",
    "tool.completed",
]
```

Also assert the audit success entry is recorded before the completion assertion is
made by the test.

- [x] **Step 4: Run the characterization tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_shell.py \
  tests/test_code_exec.py \
  tests/test_tools.py -q
```

Expected: PASS. If a proposed characterization does not match the current public
contract, correct the test to the observed contract before continuing; do not change
production code in this task.

- [x] **Step 5: Commit the behavior lock**

```bash
git add tests/test_shell.py tests/test_code_exec.py tests/test_tools.py
git commit -m "Preserve process-tool contracts before isolation work" \
  -m "Lock shell parsing, code execution result data, and scheduler event ordering before replacing the underlying process launch mechanisms." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: focused shell, code_exec, and tools tests"
```

### Task 2: Introduce typed sandbox configuration and migration

**Files:**
- Modify: `src/multiclaw/config/settings.py`
- Modify: `tests/test_config.py`
- Modify: `tests/conftest.py`
- Modify: `multiclaw.toml`
- Modify: `config/multiclaw.toml`

- [x] **Step 1: Write failing typed-config tests**

Add tests for defaults, legacy values, debug gating, nested env overrides, and MCP
profile defaults:

```python
def test_sandbox_defaults_to_auto():
    settings = Settings(_config_file="/nonexistent")
    assert settings.governance.sandbox.mode == "auto"
    assert settings.governance.sandbox.profiles.shell == "shell_workspace"

def test_legacy_process_maps_to_auto_with_warning(tmp_path):
    path = tmp_path / "multiclaw.toml"
    path.write_text("[governance]\\nsandbox_mode='process'\\n")
    with pytest.warns(DeprecationWarning, match="sandbox_mode.*process"):
        settings = Settings(_config_file=str(path))
    assert settings.governance.sandbox.mode == "auto"

def test_legacy_docker_is_rejected(tmp_path):
    path = tmp_path / "multiclaw.toml"
    path.write_text("[governance]\\nsandbox_mode='docker'\\n")
    with pytest.raises(ValidationError, match="docker"):
        Settings(_config_file=str(path))

def test_legacy_and_nested_sandbox_config_cannot_be_mixed(tmp_path):
    path = tmp_path / "multiclaw.toml"
    path.write_text(
        "[governance]\\nsandbox_mode='process'\\n"
        "[governance.sandbox]\\nmode='auto'\\n"
    )
    with pytest.raises(ValidationError, match="cannot be combined"):
        Settings(_config_file=str(path))

def test_unsafe_mode_requires_debug(tmp_path):
    path = tmp_path / "multiclaw.toml"
    path.write_text(
        "[app]\\ndebug=false\\n"
        "[governance.sandbox]\\nmode='host_unsafe_dev_only'\\n"
    )
    with pytest.raises(ValidationError, match="app.debug"):
        Settings(_config_file=str(path))
```

- [x] **Step 2: Run tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`

Expected: FAIL because `GovernanceSettings` has no nested `sandbox` model and legacy
values are not validated.

- [x] **Step 3: Add typed settings and validators**

Implement these models in `settings.py`:

```python
class SandboxProfileNames(BaseModel):
    shell: str = "shell_workspace"
    code_exec: str = "code_exec_python"
    mcp_stdio: str = "mcp_stdio_local"

class MacOSSandboxSettings(BaseModel):
    seatbelt_profile_dir: str = ""

class LinuxSandboxSettings(BaseModel):
    nsjail_path: str = "/usr/bin/nsjail"
    nsjail_config_dir: str = ""

class SandboxSettings(BaseModel):
    mode: Literal["auto", "host_unsafe_dev_only"] = "auto"
    backend_probe_on_startup: bool = True
    unsafe_fallback_requires_debug: Literal[True] = True
    write_protected_workspace_paths: list[str] = Field(default_factory=lambda: [".git"])
    read_hidden_workspace_paths: list[str] = Field(default_factory=lambda: [".env", ".env.*"])
    profiles: SandboxProfileNames = Field(default_factory=SandboxProfileNames)
    macos: MacOSSandboxSettings = Field(default_factory=MacOSSandboxSettings)
    linux: LinuxSandboxSettings = Field(default_factory=LinuxSandboxSettings)

class GovernanceSettings(BaseModel):
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    audit_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_mode(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "sandbox_mode" not in value:
            return value
        migrated = dict(value)
        legacy_mode = migrated.pop("sandbox_mode")
        if "sandbox" in migrated:
            raise ValueError(
                "governance.sandbox_mode cannot be combined with governance.sandbox"
            )
        if legacy_mode == "process":
            warnings.warn(
                "governance.sandbox_mode='process' is deprecated; using governance.sandbox.mode='auto'",
                DeprecationWarning,
                stacklevel=2,
            )
            migrated["sandbox"] = {"mode": "auto"}
            return migrated
        raise ValueError(f"Unsupported legacy sandbox_mode: {legacy_mode}")
```

Add a `Settings` model validator that rejects unsafe mode when `app.debug` is false.
The hard error for mixed legacy/nested configuration avoids order-dependent security
semantics; nested environment overrides continue to win over TOML through the existing
`_apply_env_overrides()` merge.
Also test that `unsafe_fallback_requires_debug=false` is rejected. In `auto`, setting
`backend_probe_on_startup=false` is allowed only as a diagnostic configuration and
must yield blocked readiness with no risky registration; it never treats unprobed
profiles as ready.

- [x] **Step 4: Update checked-in configs and fixtures**

Replace legacy `[governance] sandbox_mode = "process"` with:

```toml
[governance]
audit_enabled = true

[governance.sandbox]
mode = "auto"
backend_probe_on_startup = true
unsafe_fallback_requires_debug = true
write_protected_workspace_paths = [".git"]
read_hidden_workspace_paths = [".env", ".env.*"]
```

Do not alter or reproduce unrelated credential-bearing values in the config files.

- [x] **Step 5: Run config tests**

Run: `.venv/bin/python -m pytest tests/test_config.py -q`

Expected: PASS with only the test-captured legacy deprecation warning.

- [x] **Step 6: Commit typed configuration**

```bash
git add src/multiclaw/config/settings.py tests/test_config.py tests/conftest.py multiclaw.toml config/multiclaw.toml
git commit -m "Make unsafe process execution an explicit configuration decision" \
  -m "Typed sandbox settings replace inert free-form values and reject unsupported or production-unsafe modes before runtime wiring." \
  -m "Constraint: host fallback is valid only in debug mode" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: tests/test_config.py"
```

### Task 3: Create the core sandbox models, environment policy, and execution guard

**Files:**
- Delete: `src/multiclaw/governance/sandbox.py`
- Create: `src/multiclaw/governance/sandbox/__init__.py`
- Create: `src/multiclaw/governance/sandbox/errors.py`
- Create: `src/multiclaw/governance/sandbox/models.py`
- Create: `src/multiclaw/governance/sandbox/execution_guard.py`
- Create: `src/multiclaw/governance/sandbox/environment.py`
- Create: `src/multiclaw/governance/sandbox/backend.py`
- Modify: `src/multiclaw/governance/__init__.py`
- Modify: `src/multiclaw/tools/scheduler.py`
- Modify: `src/multiclaw/tools/base.py`
- Modify: `tests/test_governance.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_tool_batch.py`
- Modify: `tests/test_agent.py`
- Create: `tests/test_sandbox_models.py`
- Create: `tests/test_sandbox_environment.py`

- [x] **Step 1: Write failing model and environment tests**

Cover request exclusivity, frozen readiness, secret-key rejection, and private paths:

```python
def test_shell_request_requires_only_command(tmp_path):
    with pytest.raises(ValidationError):
        SandboxExecRequest(
            tool_name="shell",
            profile_name="shell_workspace",
            mode="shell_string",
            command="echo ok",
            argv=["echo", "ok"],
            workspace_root=tmp_path,
            cwd=tmp_path,
            timeout_seconds=1,
        )

def test_environment_scrubs_secrets_and_redirects_home(tmp_path):
    result = build_sandbox_environment(
        base_env={"LANG": "C", "GITHUB_TOKEN": "secret", "HOME": "/host"},
        overrides={},
        allowed_secret_keys=frozenset(),
        temp_root=tmp_path,
    )
    assert result.env["LANG"] == "C"
    assert "GITHUB_TOKEN" not in result.env
    assert result.private_root.parent == tmp_path
    assert result.private_root.name.startswith("launch-")
    assert result.env["HOME"] == str(result.private_root / "home")
    assert result.env["USER"] == "sandbox"
    assert result.env["LOGNAME"] == "sandbox"
```

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_sandbox_models.py \
  tests/test_sandbox_environment.py \
  tests/test_governance.py -q
```

Expected: FAIL because the package and new names do not exist.

- [x] **Step 3: Implement typed errors and models**

Define `SandboxConfigurationError`, `SandboxUnavailableError`,
`SandboxPolicyError`, and `SandboxLaunchError` in
`errors.py`.

In `models.py`, define frozen Pydantic models for:

```python
class SandboxExecRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    tool_name: str
    profile_name: str
    mode: Literal["shell_string", "exec_argv"]
    command: str | None = None
    argv: tuple[str, ...] | None = None
    workspace_root: Path
    cwd: Path
    stdin_bytes: bytes | None = None
    timeout_seconds: float = Field(gt=0)
    env_overrides: dict[str, str] = Field(default_factory=dict)
    allowed_secret_env: frozenset[str] = frozenset()
    network_mode: Literal["disabled", "inherit"] | None = None
    workspace_mode: Literal["ro", "rw"] | None = None
    allow_subprocesses: bool | None = None
    read_only_paths: tuple[Path, ...] = ()
    correlation_id: str = ""
    mcp_server_name: str | None = None

    @model_validator(mode="after")
    def validate_launch_mode(self) -> "SandboxExecRequest":
        shell_ok = self.mode == "shell_string" and self.command is not None and self.argv is None
        exec_ok = self.mode == "exec_argv" and self.argv is not None and self.command is None
        if not (shell_ok or exec_ok):
            raise ValueError("exactly one launch payload must match mode")
        return self
```

Define the remaining frozen models with these exact fields:

```python
class SandboxEnvironment(BaseModel):
    model_config = ConfigDict(frozen=True)
    env: dict[str, str]
    private_root: Path
    home: Path
    tmp: Path

class SandboxProfilePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    workspace_mode: Literal["ro", "rw"]
    network_mode: Literal["disabled", "inherit"]
    allow_subprocesses: bool
    entrypoints: tuple[Path, ...]
    runtime_read_only_paths: tuple[Path, ...] = ()
    write_protected_patterns: tuple[str, ...] = (".git",)
    read_hidden_patterns: tuple[str, ...] = (".env", ".env.*")

class SandboxedLaunchSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    executable: str
    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    stdin_bytes: bytes | None
    private_root: Path
    backend_name: str
    profile_name: str
    correlation_id: str
    unsafe_fallback_used: bool = False

class SandboxExecResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    exit_code: int | None
    timed_out: bool
    signal: str | None
    stdout: bytes
    stderr: bytes
    backend_name: str
    profile_name: str
    unsafe_fallback_used: bool = False

class SandboxProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    backend_name: str
    available: bool
    capabilities: dict[str, bool]
    reason: str = ""

class SandboxReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)
    ready: bool
    mode: Literal["auto", "host_unsafe_dev_only"]
    backend_name: str
    probe: SandboxProbeResult
    profiles: dict[str, bool]
    skipped_capabilities: dict[str, str]
    unsafe_fallback_active: bool = False
```

- [x] **Step 4: Move timeout behavior to `ExecutionGuard` and rename scheduler wiring**

Move the current implementation unchanged except for names:

```python
class ExecutionTimeoutError(asyncio.TimeoutError):
    pass

class ExecutionGuard:
    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    async def run(self, operation: Callable[[], T | Awaitable[T]]) -> T:
        ...
```

Rename `CoreToolScheduler.__init__(sandbox=...)` to
`execution_guard: ExecutionGuard`, update `self.execution_guard`, and replace both
`self.sandbox.run(...)` calls. Update all imports/tests; do not leave a
`ProcessSandbox` compatibility alias.
Export `ExecutionGuard` and `ExecutionTimeoutError` from `multiclaw.governance`;
process-level timeouts remain represented by `SandboxExecResult.timed_out` rather
than a second, ambiguously named timeout exception.

- [x] **Step 5: Implement deterministic environment shaping**

`build_sandbox_environment()` must:

- create a unique `launch-*` directory with `tempfile.mkdtemp(dir=temp_root)` and
  mode `0o700`, then create `home`, `tmp`, and XDG subdirectories below it;
- pass only LANG/LC_ALL/optional TERM plus synthetic USER/SHELL/PATH/HOME/TMPDIR/XDG;
- reject secret-shaped override keys unless present in `allowed_secret_env`;
- never include host socket/agent variables;
- return an immutable `SandboxEnvironment`.

The manager owns a startup temp root created with mode `0o700`. One-shot launch
subdirectories are removed in `SandboxManager.run(...).finally`; MCP launch roots are
removed by `StdioTransport.disconnect()`. `SandboxController.close()` removes the
empty manager root during application shutdown and raises/logs if non-empty roots
remain.

Use `fnmatch` for secret/path patterns and sanitize error messages to include key
names but never values. Inject synthetic `LOGNAME=sandbox` along with USER. Cap
explicit runtime read-only roots at 16 across both backends; reject larger requests
before rendering so the common contract stays bounded. `correlation_id` remains
metadata only and must never become a filesystem path component.

Normalize override keys to uppercase for classification. Start with these deny
patterns: `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*API_KEY*`, `*ACCESS_KEY*`, and
`*PRIVATE_KEY*`. Even an allowlist cannot override runtime-owned keys: `HOME`,
`TMPDIR`, `PATH`, `USER`, `LOGNAME`, `SHELL`, or any `XDG_*` key. Pass an explicit
`default_path` into the helper (`/usr/bin:/bin:/usr/sbin:/sbin` on macOS and
`/usr/bin:/bin` on Linux); never derive the child PATH from `os.environ`.

- [x] **Step 6: Add protocols and unsafe host backend**

Define the `SandboxBackend` and `SandboxController` protocols shown above. Add
`HostUnsafeBackend` that returns direct `/bin/sh -c` or argv specs, sets
`unsafe_fallback_used=True`, and still uses the common scrubbed environment and cwd
validation. It is constructed only by the manager in explicit unsafe debug mode.

- [x] **Step 7: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_sandbox_models.py \
  tests/test_sandbox_environment.py \
  tests/test_governance.py \
  tests/test_tools.py \
  tests/test_tool_batch.py \
  tests/test_agent.py -q
```

Expected: PASS; no `ProcessSandbox` references remain under `src/` or `tests/`.

- [x] **Step 8: Commit core contracts**

```bash
git add \
  src/multiclaw/governance/__init__.py \
  src/multiclaw/governance/sandbox.py \
  src/multiclaw/governance/sandbox \
  src/multiclaw/tools/base.py \
  src/multiclaw/tools/scheduler.py \
  tests/test_governance.py \
  tests/test_tools.py \
  tests/test_tool_batch.py \
  tests/test_agent.py \
  tests/test_sandbox_models.py \
  tests/test_sandbox_environment.py
git commit -m "Separate execution timeouts from process isolation" \
  -m "The governance API now names timeout protection honestly and introduces immutable sandbox contracts plus deterministic environment shaping for later backends." \
  -m "Rejected: Keep ProcessSandbox as an alias | it would preserve the misleading security contract" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: governance, sandbox model/environment, scheduler, batch, and agent tests"
```

### Task 4: Implement the one-shot sandbox process runner

**Files:**
- Create: `src/multiclaw/governance/sandbox/runner.py`
- Create: `tests/test_sandbox_runner.py`

- [x] **Step 1: Write failing runner lifecycle tests**

Use `sys.executable -c` children to test stdout/stderr, stdin, non-zero exit,
SIGTERM, TERM-ignore/KILL, and descendant cleanup. The orphan test records the child
PID in a workspace file and asserts `os.kill(pid, 0)` raises `ProcessLookupError`
after runner completion.

Superseding note (2026-08-08): the accepted public contract is cleanup of the
original process group plus ordinary descendants that remain in that PGID. macOS
breakaway children created via `setsid`, `setpgid`, or double-fork are explicitly
out of contract for forced cleanup; the diagnostic harness must detect any
surviving recorded PID and terminate it precisely during teardown.

Representative test:

```python
@pytest.mark.asyncio
async def test_runner_kills_process_group_after_timeout(tmp_path):
    spec = SandboxedLaunchSpec(
        executable=sys.executable,
        args=("-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"),
        cwd=tmp_path,
        env=dict(os.environ),
        stdin_bytes=None,
        private_root=tmp_path,
        backend_name="fake",
        profile_name="test",
        correlation_id="timeout",
    )
    result = await SandboxProcessRunner(term_grace_seconds=0.05).run(spec, 0.05)
    assert result.timed_out is True
    assert result.signal == "SIGKILL"
```

- [x] **Step 2: Run tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_sandbox_runner.py -q`

Expected: FAIL because `SandboxProcessRunner` does not exist.

- [x] **Step 3: Implement process creation and capture**

Use only exec-form spawning:

```python
proc = await asyncio.create_subprocess_exec(
    spec.executable,
    *spec.args,
    stdin=asyncio.subprocess.PIPE if spec.stdin_bytes is not None else None,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=str(spec.cwd),
    env=dict(spec.env),
    start_new_session=True,
)
```

Wrap `proc.communicate(spec.stdin_bytes)` in `asyncio.wait_for`. On timeout, send
TERM to `os.getpgid(proc.pid)`, wait the configured grace period, then KILL the
group. Convert negative return codes to stable signal names and raise
`SandboxLaunchError` for pre-spawn failures. Preserve the communicate task with
`asyncio.shield()` so timeout escalation can still drain stdout/stderr:

```python
communicate_task = asyncio.create_task(proc.communicate(spec.stdin_bytes))
try:
    stdout, stderr = await asyncio.wait_for(
        asyncio.shield(communicate_task), timeout=timeout_seconds
    )
except asyncio.TimeoutError:
    await self._terminate_process_group(proc, communicate_task)
except asyncio.CancelledError:
    await self._terminate_process_group(proc, communicate_task)
    raise
```

Add a cancellation test as well as timeout tests; cancellation must leave no child
or descendant process. Private-root deletion is manager/transport ownership, not the
runner's responsibility.

- [x] **Step 4: Run runner tests**

Run: `.venv/bin/python -m pytest tests/test_sandbox_runner.py -q`

Expected: PASS, including no surviving descendant PID within the original process
group; any detected breakaway survivor PID is then cleaned up precisely by the
diagnostic harness during teardown.

- [x] **Step 5: Commit the runner**

```bash
git add src/multiclaw/governance/sandbox/runner.py tests/test_sandbox_runner.py
git commit -m "Make process lifetime part of the sandbox contract" \
  -m "One exec-form runner now owns stdio, process groups, deterministic timeout escalation, and orphan cleanup for one-shot tools." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: tests/test_sandbox_runner.py"
```

### Task 5: Implement the macOS Seatbelt backend

**Files:**
- Create: `src/multiclaw/governance/sandbox/seatbelt_profiles.py`
- Create: `src/multiclaw/governance/sandbox/seatbelt.py`
- Create: `tests/test_sandbox_seatbelt.py`

- [x] **Step 1: Write failing Seatbelt rendering tests**

Tests must assert:

- backend executable is exactly `/usr/bin/sandbox-exec`;
- shell target suffix is `-- /bin/sh -c <raw command>`;
- request paths are passed only as `-D KEY=value` arguments;
- the raw command never appears inside SBPL text;
- network-disabled and code-child-deny clauses are present;
- missing binary or incomplete probe proof returns unavailable.

```python
def test_seatbelt_shell_spec_keeps_wrapper_exec_form(tmp_path):
    backend = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec"))
    spec = backend.build_launch_spec(request, shell_policy, environment)
    assert spec.executable == "/usr/bin/sandbox-exec"
    assert spec.args[-4:] == ("--", "/bin/sh", "-c", request.command)
    profile_text = spec.args[spec.args.index("-p") + 1]
    assert request.command not in profile_text
```

- [x] **Step 2: Run tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_sandbox_seatbelt.py -q`

Expected: FAIL because no Seatbelt backend exists.

- [x] **Step 3: Add reviewed static profiles**

Define separate immutable profile strings for shell, code exec, and MCP stdio.
Each profile must start from `(deny default)` and include explicit rules for:

- system/runtime read and exec roots;
- workspace ro/rw;
- private home/tmp read/write;
- network deny unless policy is `inherit`;
- global `.git` write denial regex;
- global `.env`/`.env.*` read denial regex;
- code profile descendant fork/exec denial.

Use `(param "WORKSPACE")`, `(param "PRIVATE_HOME")`, `(param "PRIVATE_TMP")`,
and numbered runtime-root params. Never format path or command text into the profile.

- [x] **Step 4: Implement spec rendering**

`SeatbeltBackend.build_launch_spec()` must canonicalize the selected static profile,
generate stable `-D` arguments, then append `--` and the target argv. It must reject
network values other than `disabled`/`inherit` and reject a profile whose required
protected-path capability is not represented.

- [x] **Step 5: Implement a behavioral probe**

The probe uses `subprocess.run(..., shell=False, timeout=...)` with temporary workspace
and sentinel paths. It records booleans for allowed execution, denied outside write,
denied network, hidden read, protected write, and denied child creation. Unit tests
mock subprocess results; real behavior is proven in Task 12.

- [x] **Step 6: Run Seatbelt unit tests on any OS**

Run: `.venv/bin/python -m pytest tests/test_sandbox_seatbelt.py -q`

Expected: PASS without executing Seatbelt because unit tests inject binary existence
and subprocess results.

- [x] **Step 7: Commit the Seatbelt backend**

```bash
git add src/multiclaw/governance/sandbox/seatbelt.py src/multiclaw/governance/sandbox/seatbelt_profiles.py tests/test_sandbox_seatbelt.py
git commit -m "Express macOS process policy through reviewed Seatbelt profiles" \
  -m "Static SBPL templates and parameter-only path binding keep shell text out of policy generation while exposing behavioral probe evidence." \
  -m "Constraint: /usr/bin/sandbox-exec is the selected macOS backend" \
  -m "Confidence: medium" \
  -m "Scope-risk: moderate" \
  -m "Tested: tests/test_sandbox_seatbelt.py" \
  -m "Not-tested: native Seatbelt denial behavior until the platform gate"
```

### Task 6: Implement the Linux nsjail backend

**Files:**
- Create: `src/multiclaw/governance/sandbox/nsjail_profiles.py`
- Create: `src/multiclaw/governance/sandbox/nsjail.py`
- Create: `tests/test_sandbox_nsjail.py`

- [x] **Step 1: Write failing nsjail rendering tests**

Assert normalized config fragments for:

- user/mount/PID/IPC/UTS namespaces and `no_new_privs`;
- dropped capabilities;
- workspace ro/rw mount, private home/tmp, runtime roots;
- `.git` read-only overlay and hidden `.env*` mounts;
- isolated network namespace for disabled mode;
- code profile child-process seccomp/rlimit rules;
- exact target argv after `--`;
- protobuf-text escaping for quotes, backslashes, and newlines.

- [x] **Step 2: Run tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_sandbox_nsjail.py -q`

Expected: FAIL because no nsjail renderer exists.

- [x] **Step 3: Implement protobuf-text rendering helpers**

Add a strict string encoder:

```python
def protobuf_quote(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
```

Build mount blocks from canonical paths only. Reject paths containing NUL and reject
relative roots after canonicalization. The renderer returns text written to a private
config file below the launch temp root; permissions are `0o600`.

- [x] **Step 4: Implement nsjail launch specs**

The spec executable is the configured canonical nsjail binary. Args include the
private config path, `--`, and the exact target argv. The backend never invokes a
shell. Network `inherit` removes only the isolated-network clause; all filesystem,
env, capability, and process constraints remain.

- [x] **Step 5: Implement behavioral probe interpretation**

Probe binary existence/executability, required namespace/kernel support, config
loading, allowed execution, outside-write denial, network denial, hidden/protected
paths, and code child denial. Return unavailable if any required capability is not
proven; do not downgrade to a weaker profile.

- [x] **Step 6: Run nsjail unit tests on any OS**

Run: `.venv/bin/python -m pytest tests/test_sandbox_nsjail.py -q`

Expected: PASS with subprocess/kernel behavior mocked.

- [x] **Step 7: Commit the nsjail backend**

```bash
git add src/multiclaw/governance/sandbox/nsjail.py src/multiclaw/governance/sandbox/nsjail_profiles.py tests/test_sandbox_nsjail.py
git commit -m "Describe Linux isolation as a verifiable nsjail configuration" \
  -m "Namespace, mount, network, seccomp, and rlimit policy is serialized without shell interpretation and remains unavailable until behavioral probes prove it." \
  -m "Constraint: nsjail is an external Linux prerequisite" \
  -m "Confidence: medium" \
  -m "Scope-risk: moderate" \
  -m "Tested: tests/test_sandbox_nsjail.py" \
  -m "Not-tested: native kernel enforcement until the Linux platform gate"
```

### Task 7: Implement manager selection, profiles, readiness, and events

**Files:**
- Create: `src/multiclaw/governance/sandbox/manager.py`
- Modify: `src/multiclaw/governance/sandbox/__init__.py`
- Create: `tests/sandbox_fakes.py`
- Create: `tests/test_sandbox_manager.py`

- [x] **Step 1: Write failing manager tests**

Cover Darwin/Linux/unsupported selection, probe failure, unsafe debug gating,
profile lookup, run delegation, startup-event buffering, and immutable readiness.

```python
def test_manager_selects_platform_backend(tmp_path, settings):
    darwin = SandboxManager.create(
        settings=settings.governance.sandbox,
        debug=settings.app.debug,
        workspace_root=tmp_path,
        platform_name="Darwin",
    )
    linux = SandboxManager.create(
        settings=settings.governance.sandbox,
        debug=settings.app.debug,
        workspace_root=tmp_path,
        platform_name="Linux",
    )
    assert darwin.backend_name == "seatbelt"
    assert linux.backend_name == "nsjail"

def test_auto_probe_failure_never_builds_host_spec(tmp_path, settings, failed_backend):
    manager = SandboxManager.create(
        settings=settings.governance.sandbox,
        debug=False,
        workspace_root=tmp_path,
        platform_name="Linux",
        backend_override=failed_backend,
        runner=AsyncMock(),
    )
    manager.initialize()
    with pytest.raises(SandboxUnavailableError):
        manager.build_launch_spec(shell_request(tmp_path))

def test_readiness_freezes_after_registration_gating(manager):
    manager.initialize()
    manager.record_blocked_capability("shell", "profile unavailable")
    readiness = manager.finalize_readiness()
    assert readiness.skipped_capabilities == {"shell": "profile unavailable"}
    assert manager.finalize_readiness() is readiness
    with pytest.raises(RuntimeError, match="finalized"):
        manager.record_blocked_capability("code_exec", "late mutation")
```

Add parameterized async coverage proving success, timeout, cancellation, and
pre-spawn failure all remove the generated per-launch root while leaving the manager
root itself available for later launches.

- [x] **Step 2: Run tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_sandbox_manager.py -q`

Expected: FAIL because `SandboxManager` does not exist.

- [x] **Step 3: Build the fixed profile registry**

Create exactly three `SandboxProfilePolicy` instances:

- `shell_workspace`: workspace rw, network disabled, subprocesses allowed,
  entrypoint `/bin/sh`;
- `code_exec_python`: workspace rw, network disabled, child creation denied,
  exact `sys.executable`;
- `mcp_stdio_local`: server request supplies workspace/network/subprocess/runtime grants.

All profiles include configured write-protected and read-hidden workspace patterns.
For `code_exec_python`, canonicalize `sys.executable` and derive read-only runtime
roots from `sys.prefix`, `sys.base_prefix`, and `sysconfig.get_paths()`; deduplicate
ancestor/descendant overlaps and omit roots already inside the workspace. These are
built-in runtime roots and do not consume the MCP limit of 16 explicit extra roots.

- [x] **Step 4: Implement selection and initialization**

`SandboxManager.create()` accepts explicit `platform_name` for tests. `auto` selects
Seatbelt for `Darwin`, nsjail for `Linux`, and an unavailable state otherwise.
Unsafe mode constructs `HostUnsafeBackend` only after the settings validator has
proved debug mode. `initialize()` runs probes once and caches probe/profile state,
but does not yet freeze public readiness because registration skips are still being
collected.

Use this keyword-only factory shape so production and tests do not depend on argument
ordering:

```python
@classmethod
def create(
    cls,
    *,
    settings: SandboxSettings,
    debug: bool,
    workspace_root: Path,
    event_bus: EventBus | None = None,
    runner: SandboxProcessRunner | None = None,
    platform_name: str | None = None,
    backend_override: SandboxBackend | None = None,
) -> "SandboxManager": ...
```

Create the manager root with `Path(tempfile.mkdtemp(prefix="multiclaw-sandbox-"))`
and immediately enforce mode `0o700`. Pass this root to environment creation; never
mount the parent host temp directory into a sandbox.

- [x] **Step 5: Implement launch and event behavior**

`build_launch_spec()` validates profile readiness, cwd under workspace, entrypoint,
env grants, and policy. If environment creation succeeds but policy rendering fails,
it removes that launch root before re-raising. `run()` calls the common runner, adds
backend/profile/unsafe metadata, and removes the per-launch private root in `finally`
for success, timeout, cancellation, and pre-spawn failure.

`record_blocked_capability()` is valid only before `finalize_readiness()`.
`finalize_readiness()` creates and caches the one frozen `SandboxReadiness`; later
calls return the same object and later skip mutations fail loudly. Protect startup
event and skipped-capability buffers with `threading.Lock` because MCP connection
policy is evaluated on the manager's background event-loop thread. Drain events only
from the FastAPI lifespan thread.

Unsafe mode records `sandbox.unsafe_fallback_used` once at startup and exactly once
per rendered launch (not once in both `build_launch_spec()` and `run()`).
Auto failures record `sandbox.profile_unavailable` or
`sandbox.registration_skipped`; they never construct a host spec.

- [x] **Step 6: Add reusable test controllers**

`tests/sandbox_fakes.py` contains:

- `ReadyRecordingSandboxController`: builds direct exec-form specs for tests, runs
  through the real `SandboxProcessRunner`, and records requests;
- `UnavailableSandboxController`: all profiles false and every build/run raises
  `SandboxUnavailableError`.

Both fakes implement `finalize_readiness()`, `drain_startup_events()`, and `close()`;
the recording fake removes
its per-request temp roots so cleanup tests exercise the same ownership contract.

These fakes are test-only and are never imported by production modules.

- [x] **Step 7: Run manager and contract tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_sandbox_manager.py \
  tests/test_sandbox_models.py \
  tests/test_sandbox_environment.py \
  tests/test_sandbox_runner.py -q
```

Expected: PASS.

- [x] **Step 8: Commit manager behavior**

```bash
git add src/multiclaw/governance/sandbox tests/sandbox_fakes.py tests/test_sandbox_manager.py
git commit -m "Make sandbox readiness authoritative for local process launch" \
  -m "One runtime manager now selects, probes, caches, and enforces profile readiness while preserving an explicit and noisy debug-only unsafe path." \
  -m "Constraint: auto failures never create host launch specs" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: sandbox manager and core contract tests"
```

### Task 8: Wire readiness, conditional registration, and the health endpoint

**Files:**
- Modify: `src/multiclaw/server.py`
- Modify: `src/multiclaw/auth/middleware.py`
- Modify: `tests/test_server.py`
- Modify: `tests/test_tools.py`

- [x] **Step 1: Write failing server wiring tests**

Add tests that inject ready/unavailable controllers into `create_agent()` and assert
registry contents, MCP filtering order, app state, public health access, and status
codes:

```python
def test_create_agent_omits_risky_tools_when_sandbox_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
    agent = create_agent(sandbox_controller=UnavailableSandboxController(tmp_path))
    names = {tool.name for tool in agent.registry.list_all()}
    assert "shell" not in names
    assert "code_exec" not in names
    assert {"read_file", "web_fetch"} <= names

def test_ready_endpoint_returns_503_for_blocked_sandbox(client, blocked_readiness):
    client.app.state.sandbox_readiness = blocked_readiness
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False
```

- [x] **Step 2: Run tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_server.py tests/test_tools.py -q`

Expected: FAIL because injection, gating, and `/health/ready` do not exist.

- [x] **Step 3: Construct the manager before risky registration**

Change the signature to:

```python
def create_agent(*, sandbox_controller: SandboxController | None = None) -> MultiClawAgent:
```

When none is supplied, construct and initialize `SandboxManager` from settings,
debug flag, workspace, shared event bus, and real runner. Register safe file/web tools
unconditionally. Register `ShellToolBuilder` and `CodeExecToolBuilder` only when their
profiles are ready, passing the controller into each builder.

After MCP filtering/connection has recorded all startup skips, call
`sandbox_controller.finalize_readiness()` exactly once. Store
`runtime_agent.sandbox_controller` and the returned
`runtime_agent.sandbox_readiness` for lifespan use.

- [x] **Step 4: Gate MCP before connection**

Add `sandbox_controller` and `workspace_root` parameters to `_register_mcp_tools()`.
Before `connect_servers()`, partition configs:

- ready stdio: keep;
- unavailable stdio: skip and record server/reason;
- in-process in auto: skip;
- in-process unsafe dev: keep with unsafe event;
- remote: keep and log `transport_remote_unsandboxed=true`.

Preserve callback installation before connect and existing namespace refresh order.

- [x] **Step 5: Publish readiness through FastAPI**

In lifespan:

```python
app.state.sandbox_readiness = agent.sandbox_readiness
for event in agent.sandbox_controller.drain_startup_events():
    await shared_bus.publish(event)
```

Add a public route:

```python
@app.get("/health/ready")
async def health_ready(request: Request):
    readiness = request.app.state.sandbox_readiness
    status = 200 if readiness.ready else 503
    return JSONResponse(readiness.model_dump(mode="json"), status_code=status)
```

Add `"/health/ready"` to `PUBLIC_EXACT`. Redact temp roots, hidden-path matches, and
env values from the response; expose only backend/profile/capability names and reasons.

During lifespan shutdown, stop MCP first so stdio transports remove their private
roots, then call `agent.sandbox_controller.close()` and log any residual-root error.

- [x] **Step 6: Rename scheduler construction**

Construct `CoreToolScheduler(execution_guard=ExecutionGuard(), ...)`. Update any test
factories still using the old keyword.

- [x] **Step 7: Run server/tool tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_server.py \
  tests/test_tools.py \
  tests/test_mcp_integration.py -q
```

Expected: PASS with injected fakes; no unit test requires a native backend.

- [x] **Step 8: Commit fail-closed runtime wiring**

```bash
git add src/multiclaw/server.py src/multiclaw/auth/middleware.py tests/test_server.py tests/test_tools.py tests/test_mcp_integration.py
git commit -m "Keep the service diagnosable while blocking unsafe capabilities" \
  -m "Runtime readiness now controls risky tool and MCP registration before execution and exposes a public deployment gate without crashing the application." \
  -m "Constraint: liveness remains available while readiness is blocked" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: server, tools, and MCP integration tests"
```

### Task 9: Route shell execution through the sandbox controller

**Files:**
- Modify: `src/multiclaw/tools/base.py`
- Modify: `src/multiclaw/tools/scheduler.py`
- Modify: `src/multiclaw/tools/shell.py`
- Modify: `tests/test_shell.py`
- Modify: `tests/test_tools.py`

- [x] **Step 1: Update shell tests to inject the ready recording controller**

Keep the Task 1 characterization assertions unchanged. Add request assertions:

```python
controller = ReadyRecordingSandboxController(workspace)
builder = ShellToolBuilder(str(workspace), sandbox_controller=controller)
result = await builder.build(builder.validate({"command": "printf ok"})).execute()
request = controller.requests[-1]
assert request.mode == "shell_string"
assert request.command == "printf ok"
assert request.profile_name == "shell_workspace"
assert request.cwd == workspace.resolve()
assert result.data == {"exit_code": 0}
```

Add unavailable-controller coverage expecting a tool error whose content says the
sandbox profile is unavailable and contains no command/env secret.

- [x] **Step 2: Run shell tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_shell.py -q`

Expected: FAIL because the builder has no controller parameter and still calls
`create_subprocess_shell`.

- [x] **Step 3: Add internal audit metadata without changing tool data**

Extend `ToolExecutionResult`:

```python
class ToolExecutionResult(BaseModel):
    status: ToolStatus
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    audit: dict[str, Any] = Field(default_factory=dict, exclude=True)
```

Update scheduler audit formatting to prepend only whitelisted internal fields:

```python
def _audit_detail(result: ToolExecutionResult) -> str:
    allowed = {k: result.audit[k] for k in (
        "sandbox_backend", "sandbox_profile", "unsafe_fallback_used"
    ) if k in result.audit}
    prefix = " ".join(f"{k}={allowed[k]}" for k in sorted(allowed))
    return f"{prefix}; {result.content}" if prefix else result.content
```

Assert `result.model_dump()` excludes `audit` and existing `data` assertions remain
unchanged.

- [x] **Step 4: Replace host shell spawning**

Require `sandbox_controller` in `ShellToolBuilder` and `ShellInvocation`. Preserve
validation/safety/cwd/timeout code, then build:

```python
request = SandboxExecRequest(
    tool_name=self.name,
    profile_name="shell_workspace",
    mode="shell_string",
    command=command,
    workspace_root=self.workspace_root,
    cwd=cwd,
    timeout_seconds=timeout,
    correlation_id=uuid.uuid4().hex,
)
exec_result = await self.sandbox_controller.run(request)
```

Delete `_build_env`, direct subprocess creation, and local kill helpers. Format
stdout/stderr/timeout/exit code exactly as before. Map policy/probe/launch errors to
`_error(...)`; add backend/profile/unsafe values to the internal `audit` field.

- [x] **Step 5: Run shell and scheduler tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_shell.py tests/test_tools.py -q
```

Expected: PASS; `rg "create_subprocess_shell" src/multiclaw/tools/shell.py` returns no
matches.

- [x] **Step 6: Commit shell isolation**

```bash
git add src/multiclaw/tools/base.py src/multiclaw/tools/scheduler.py src/multiclaw/tools/shell.py tests/test_shell.py tests/test_tools.py
git commit -m "Put shell compatibility behind an OS-enforced launch boundary" \
  -m "Shell strings keep their current parsing and result contract while process creation, environment, timeout, and cleanup move to the sandbox controller." \
  -m "Constraint: the wrapper is exec-form and the target remains /bin/sh -c" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: shell and scheduler tests"
```

### Task 10: Replace multiprocessing code execution with one sandboxed interpreter

**Files:**
- Create: `src/multiclaw/tools/_code_runner.py`
- Modify: `src/multiclaw/tools/code_exec.py`
- Modify: `tests/test_code_exec.py`

- [x] **Step 1: Write failing runner-protocol and compatibility tests**

Add direct tests for `_code_runner.main()` using monkeypatched stdin and a captured
real stdout protocol stream. Cover success, user stderr, exception, restricted import,
and JSON shape. Add invocation tests using `ReadyRecordingSandboxController` for the
exact argv and result contracts from Task 1.

Amendment note: the approved security deviation replaced the draft trusted JSON
envelope child protocol with a static `python -I -S -c` bootstrap. The parent now
trusts only process outcome plus stdout/stderr, isolated mode intentionally removes
ambient workspace imports, and no multiprocessing/helper tree remains.

```python
@pytest.mark.asyncio
async def test_code_exec_launches_exact_runner_module(tmp_path):
    controller = ReadyRecordingSandboxController(tmp_path)
    builder = CodeExecToolBuilder(tmp_path, sandbox_controller=controller)
    result = await builder.build(CodeExecParams(code="print(3)")).execute()
    request = controller.requests[-1]
    assert request.mode == "exec_argv"
    assert request.argv == (
        sys.executable,
        "-m",
        "multiclaw.tools._code_runner",
        "--restrict-builtins",
    )
    assert result.data == {"success": True}
```

Add malformed JSON, multiple JSON documents, envelope above `MAX_ENVELOPE_BYTES`, and
sandbox launch failure tests; all must return tool status error. Define the protocol
limit as `MAX_ENVELOPE_BYTES = 1_048_576` and enforce it on raw stdout bytes before
UTF-8 decode/JSON parsing.

- [x] **Step 2: Run code-exec tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_code_exec.py -q`

Expected: FAIL because `_code_runner` and controller injection do not exist.

- [x] **Step 3: Implement the child runner**

Move `SAFE_BUILTINS`, `BLOCKED_MODULES`, and restricted import logic into
`_code_runner.py`. The entrypoint:

```python
def main(argv: list[str] | None = None) -> int:
    restrict = "--restrict-builtins" in (argv or sys.argv[1:])
    code = sys.stdin.read()
    captured_stdout = StringIO()
    captured_stderr = StringIO()
    error = ""
    success = False
    with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
        try:
            exec(code, build_globals(restrict))
            success = True
        except BaseException:
            error = traceback.format_exc()
    envelope = {
        "success": success,
        "stdout": captured_stdout.getvalue(),
        "stderr": captured_stderr.getvalue(),
        "error": error,
    }
    sys.__stdout__.write(json.dumps(envelope, ensure_ascii=False))
    return 0
```

Call `raise SystemExit(main())` under `if __name__ == "__main__"`.

- [x] **Step 4: Rewrite `CodeExecInvocation` around the controller**

Require the controller in builder/invocation. Send UTF-8 code as `stdin_bytes`, use
profile `code_exec_python`, exact argv above, workspace cwd, and existing timeout.
Parse exactly one JSON object; reject wrong types/keys, extra non-whitespace bytes,
and envelopes over `MAX_ENVELOPE_BYTES`. Apply the existing 30,000-character
truncation helper to stdout and stderr exactly as today; preserve the existing error
field/content behavior unless the whole envelope crosses the protocol limit.

Preserve result formatting:

- success → `data={"success": True}`;
- Python exception → tool success and `data={"success": False, "error": error}`;
- timeout → existing marker and `data={}`;
- sandbox/protocol error → tool error.

Add internal audit metadata without changing public `data`.

- [x] **Step 5: Prove multiprocessing is gone**

Run:

```bash
rg -n "multiprocessing|Manager\(|Process\(" src/multiclaw/tools/code_exec.py src/multiclaw/tools/_code_runner.py
```

Expected: no matches.

- [x] **Step 6: Run code-exec tests**

Run: `.venv/bin/python -m pytest tests/test_code_exec.py -q`

Expected: PASS, including exact Task 1 contracts.

- [x] **Step 7: Commit code-exec isolation**

```bash
git add src/multiclaw/tools/_code_runner.py src/multiclaw/tools/code_exec.py tests/test_code_exec.py
git commit -m "Remove the unsandboxed helper tree from Python execution" \
  -m "Code execution now uses one exact interpreter and a validated stdin/JSON protocol while retaining restricted builtins and the existing result contract." \
  -m "Rejected: Retain multiprocessing.Manager | it creates an additional unsandboxed listener process" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: tests/test_code_exec.py"
```

### Task 11: Apply sandbox policy to MCP transports

**Files:**
- Modify: `src/multiclaw/mcp/types.py`
- Modify: `src/multiclaw/mcp/config.py`
- Modify: `src/multiclaw/mcp/manager.py`
- Modify: `src/multiclaw/mcp/transport/factory.py`
- Modify: `src/multiclaw/mcp/transport/stdio.py`
- Create: `tests/test_mcp_config.py`
- Modify: `tests/test_mcp_integration.py`
- Modify: `tests/test_mcp_tool_adapter.py`
- Modify: `tests/test_server.py`

- [x] **Step 1: Write failing MCP config and transport tests**

Extend config tests for snake_case and camelCase input aliases. The parsed dataclass
must have:

```python
@dataclass
class StdioServerConfig:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    sandbox_network: Literal["disabled", "inherit"] = "disabled"
    sandbox_workspace: Literal["ro", "rw"] = "ro"
    sandbox_allow_subprocesses: bool = False
    sandbox_env_allowlist: list[str] = field(default_factory=list)
    sandbox_read_only_paths: list[str] = field(default_factory=list)
    transport_type: TransportType = field(default=TransportType.STDIO, init=False)
```

Add factory/transport tests that assert `StdioServerParameters` receives the rendered
wrapper executable, args, canonical cwd, and fully controlled env.

- [x] **Step 2: Run MCP tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_mcp_config.py \
  tests/test_mcp_integration.py \
  tests/test_mcp_tool_adapter.py \
  tests/test_server.py -q
```

Expected: FAIL because the new fields and sandbox-aware factory do not exist.

- [x] **Step 3: Parse explicit security grants**

In `_parse_server_config`, accept both forms:

```python
sandbox_network=data.get("sandboxNetwork", data.get("sandbox_network", "disabled"))
sandbox_workspace=data.get("sandboxWorkspace", data.get("sandbox_workspace", "ro"))
sandbox_allow_subprocesses=data.get(
    "sandboxAllowSubprocesses",
    data.get("sandbox_allow_subprocesses", False),
)
sandbox_env_allowlist=data.get(
    "sandboxEnvAllowlist",
    data.get("sandbox_env_allowlist", []),
)
sandbox_read_only_paths=data.get(
    "sandboxReadOnlyPaths",
    data.get("sandbox_read_only_paths", []),
)
```

Validate types and enum values during parsing. Error logs may contain server/key names,
never env values.

Security amendment note: Tasks 11 and 13 also recorded trusted config provenance,
exact same-key allowlisted env expansion, and atomic same-server/filter winner
selection.

Superseding note (2026-08-08): the final full-branch security/completeness review
found that allowing any `workspace_untrusted` MCP config to auto-connect was a High
trust-boundary flaw. Registration now rejects all `workspace_untrusted` MCP
transports before connect/start, including conservative stdio and literal
HTTP/SSE/WebSocket configs. Operator-managed configs outside the workspace remain
the only startup-connection path.

- [x] **Step 4: Apply the Task 3 MCP override fields in manager policy**

Use the frozen optional fields already introduced in Task 3:

```python
network_mode: Literal["disabled", "inherit"] | None = None
workspace_mode: Literal["ro", "rw"] | None = None
allow_subprocesses: bool | None = None
read_only_paths: tuple[Path, ...] = ()
```

Manager accepts these overrides only for `mcp_stdio_local`. Shell/code requests with
overrides raise `SandboxPolicyError`. Canonicalize extra roots and require them to be
explicitly listed in the server config. Reject more than 16 roots.

- [x] **Step 5: Render the stdio wrapper at the factory boundary**

Change:

```python
def create_transport(
    config: ServerConfig,
    *,
    sandbox_controller: SandboxController,
    workspace_root: Path,
    server_name: str,
) -> BaseTransport:
```

For stdio, resolve cwd, build an exec-argv request from `(command, *args)` and security
grants, then pass the resulting `SandboxedLaunchSpec` to `StdioTransport`. In-process
must already be gated by server registration; add a defensive factory rejection in
`auto`. Remote transports ignore the controller and keep existing constructors.

Resolve an absolute command with `Path.resolve(strict=True)`. Resolve a bare command
with `shutil.which()` only as discovery, then canonicalize it and require the result
to be under a built-in system runtime root or one of that server's explicit
`sandbox_read_only_paths`; never copy the host `PATH` into the child. If request or
transport construction fails after a private root exists, remove it before returning
the sanitized server failure.

Build the controlled MCP PATH from the platform baseline plus only granted executable
directories: add a granted root itself when it is named `bin`, otherwise add its
existing direct `bin/` child. Deduplicate canonical directories while retaining
order. This supports launchers such as `/opt/homebrew/bin/npx` and their
`/usr/bin/env node` shebangs without inheriting unrelated host PATH entries.

- [x] **Step 6: Prevent MCP SDK default env from restoring host values**

The installed SDK merges `get_default_environment()` before the supplied env. In
`StdioTransport.connect()`, blank every SDK-default key first, then overlay the
sandbox spec env:

```python
sdk_defaults = get_default_environment()
controlled_env = {key: "" for key in sdk_defaults}
controlled_env.update(self._launch_spec.env)
params = StdioServerParameters(
    command=self._launch_spec.executable,
    args=list(self._launch_spec.args),
    env=controlled_env,
    cwd=self._launch_spec.cwd,
)
```

Ensure common environment shaping sets synthetic `LOGNAME` as well as USER/HOME/PATH/
SHELL so the SDK merge cannot expose the host identity. Delete `_build_safe_env`.

If `connect()` fails before the context is entered, remove the private root in its
exception path. In `disconnect()`, always exit the SDK context first, then remove
`self._launch_spec.private_root` in `finally`. A cleanup failure is logged with server/
correlation metadata and never causes a second host launch.

- [x] **Step 7: Preserve manager lifecycle and refresh behavior**

Pass controller/workspace/server name from `MCPClientManager._connect_server()` to the
factory. Keep connect/disconnect/tool discovery/circuit breaker/refresh callback order
unchanged. When a stdio policy render fails, mark only that server FAILED with a
sanitized reason; do not affect remote servers.

- [x] **Step 8: Run MCP and server tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_mcp_config.py \
  tests/test_mcp_integration.py \
  tests/test_mcp_tool_adapter.py \
  tests/test_server.py -q
```

Expected: PASS, including stdio connect/disconnect/reconnect and registry refresh.

- [x] **Step 9: Commit MCP isolation**

```bash
git add \
  src/multiclaw/mcp/types.py \
  src/multiclaw/mcp/config.py \
  src/multiclaw/mcp/manager.py \
  src/multiclaw/mcp/transport/factory.py \
  src/multiclaw/mcp/transport/stdio.py \
  tests/test_mcp_config.py \
  tests/test_mcp_integration.py \
  tests/test_mcp_tool_adapter.py \
  tests/test_server.py
git commit -m "Make local MCP privileges explicit at server startup" \
  -m "Stdio servers now launch through rendered sandbox wrappers with typed network, workspace, subprocess, runtime-root, and secret-environment grants while remote transports remain explicitly out of scope." \
  -m "Constraint: the MCP SDK lifecycle is reused rather than reimplemented" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: MCP integration, adapter, and server tests"
```

### Task 12: Add native platform denial tests and release documentation

**Files:**
- Create: `tests/integration/test_sandbox_macos.py`
- Create: `tests/integration/test_sandbox_linux.py`
- Modify: `pyproject.toml`
- Modify: `docs/superpowers/specs/2026-08-03-native-sandbox-backends-design.md`
- Create: `docs/sandbox-deployment.md`

- [x] **Step 1: Register platform markers**

Add this marker configuration to `pyproject.toml`:

```toml
markers = [
  "native_sandbox: requires the matching OS sandbox backend and host-level execution",
  "macos_sandbox: requires Darwin and /usr/bin/sandbox-exec",
  "linux_sandbox: requires Linux and MULTICLAW_NSJAIL_PATH",
]
```

At module scope, mark each native module with both the common and platform marker:

```python
pytestmark = [pytest.mark.native_sandbox, pytest.mark.macos_sandbox]
# Linux uses pytest.mark.linux_sandbox instead of macos_sandbox.
```

Each module skips when `MULTICLAW_RUN_NATIVE_SANDBOX_TESTS != "1"` or the OS does
not match. Once explicitly enabled on the matching OS, a missing/non-executable
backend is `pytest.fail(...)`, not a skip, so the release gate cannot pass silently.

- [x] **Step 2: Implement deterministic parent-host fixtures**

Each native module creates:

- a workspace with allowed output path, `.git/protected`, `.env` secret sentinel;
- an outside sentinel directory;
- a parent TCP listener bound to `127.0.0.1` on an ephemeral port;
- PID recording for child-process/orphan assertions.

No test contacts the public internet.

- [x] **Step 3: Add macOS negative cases**

Use real `SandboxManager`/Seatbelt and assert:

- workspace write succeeds;
- outside write fails;
- `.git` write fails;
- `.env` read fails;
- parent TCP listener connection fails in disabled mode;
- code child creation fails;
- timed-out shell leaves no descendant.

- [x] **Step 4: Add Linux negative cases**

Run the same behavioral matrix through configured nsjail. Also execute a small inside-
jail probe that asserts host home/socket paths are absent from the mount view and the
network namespace cannot reach the parent listener.

- [ ] **Step 5: Run the current-platform native gate**

macOS:

```bash
MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 \
  .venv/bin/python -m pytest -m macos_sandbox -q
```

Linux:

```bash
MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 \
MULTICLAW_NSJAIL_PATH=/usr/bin/nsjail \
  .venv/bin/python -m pytest -m linux_sandbox -q
```

Expected: PASS on the matching prepared host. A missing prerequisite is a release
blocker, not a passing skip, once the environment variable opts into the native gate.
Partial note: the macOS nested parent sandbox gate still failed readiness at
`seatbelt capability check failed: allowed_execution`, so the final native-evidence
commit remains pending and the Linux nsjail gate was not run.

Superseding note (2026-08-08): security review and a
`start_new_session`/`setsid` characterization established an accepted Medium macOS
breakaway-child risk. Task 12 release evidence must therefore describe native orphan
cleanup as limited to same-PGID descendants, must not claim arbitrary breakaway-child
termination, and must still keep the macOS `allowed_execution` failure plus unrun
Linux gate as separate release blockers. `setpgid` and double-fork breakaways remain
out of contract but were not separately characterized.

- [x] **Step 6: Document deployment and migration**

`docs/sandbox-deployment.md` must include:

- macOS and Linux prerequisites/probe behavior;
- config example without real credentials;
- legacy `process` and `docker` migration;
- stdio MCP explicit grant example using environment references such as
  `${SERVICE_TOKEN}` and no literal credentials;
- readiness 200/503 semantics;
- unsafe dev warnings and prohibition in production;
- platform test commands and rollback by deployment/commit.

Link the deployment guide from the approved design. Do not mark implementation
complete in this task because the opposite platform gate may still be outstanding.

Partial note: the docs were committed, but the final native-evidence commit remains
pending until both native gates pass on prepared real hosts.

- [ ] **Step 7: Commit native gates and docs**

```bash
git add \
  pyproject.toml \
  tests/integration/test_sandbox_macos.py \
  tests/integration/test_sandbox_linux.py \
  docs/sandbox-deployment.md \
  docs/superpowers/specs/2026-08-03-native-sandbox-backends-design.md
git commit -m "Require native denial evidence before sandbox release" \
  -m "Platform-gated tests now prove filesystem, network, protected-path, child-process, and orphan behavior on the actual Seatbelt and nsjail environments, with operator migration guidance." \
  -m "Constraint: native tests must run outside a parent sandbox" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: matching platform native sandbox gate" \
  -m "Not-tested: the opposite platform until its release job runs"
```

### Task 13: Run full verification and close the implementation plan

**Files:**
- Modify: `docs/superpowers/plans/2026-08-03-native-sandbox-backends.md` (checkboxes/evidence)
- Modify: `docs/superpowers/specs/2026-08-03-native-sandbox-backends-design.md` (final status)

- [x] **Step 1: Run static and focused checks**

```bash
.venv/bin/python -m compileall -q src/multiclaw
.venv/bin/python -m pytest \
  tests/test_config.py \
  tests/test_governance.py \
  tests/test_sandbox_models.py \
  tests/test_sandbox_environment.py \
  tests/test_sandbox_runner.py \
  tests/test_sandbox_seatbelt.py \
  tests/test_sandbox_nsjail.py \
  tests/test_sandbox_manager.py \
  tests/test_shell.py \
  tests/test_code_exec.py \
  tests/test_mcp_config.py \
  tests/test_mcp_integration.py \
  tests/test_mcp_tool_adapter.py \
  tests/test_server.py \
  tests/test_tools.py -q
```

Expected: PASS.

- [x] **Step 2: Run the non-native full suite**

Run: `.venv/bin/python -m pytest -m "not native_sandbox" -q`

Expected: PASS with zero failures.

- [ ] **Step 3: Run both native release gates**

Run the Task 12 commands on their matching hosts. Record exact pytest counts and
backend versions in the final verification report. Both must pass before default
`auto` is considered releasable.

- [x] **Step 4: Verify security invariants mechanically**

```bash
rg -n "create_subprocess_shell|multiprocessing\.Manager|multiprocessing\.Process" \
  src/multiclaw/tools src/multiclaw/mcp src/multiclaw/governance
rg -n "mode.*off|fallback.*auto|host_unsafe_dev_only" \
  src/multiclaw docs tests
git diff --check
```

Expected:

- no direct shell or multiprocessing-helper launch remains in risky tools;
- no production `off` or automatic fallback exists;
- unsafe references are limited to typed config, explicit backend, tests, and docs;
- diff check exits 0.

- [x] **Step 5: Run credential and response-redaction checks**

Scan only added diff lines so pre-existing config values are not printed:

```bash
git diff -U0 ab58811...HEAD -- \
  src tests config pyproject.toml multiclaw.toml docs/sandbox-deployment.md \
  | rg '^\+[^+]' \
  | rg 'sk-[A-Za-z0-9]|github_pat_[A-Za-z0-9_]|xkeysib-[A-Za-z0-9]|re_[A-Za-z0-9]|Bearer[[:space:]]+[A-Za-z0-9]'
```

Expected: no matches. Then run the health/error redaction tests with known dummy
values and assert those exact dummy values are absent from response bodies, captured
logs, event data, and audit details. Do not print existing repository credentials into
logs or reports.

- [x] **Step 6: Request security and completion review**

Use `security-reviewer` for backend policy/trust-boundary review and `verifier` for
PRD/test-spec evidence. Any Critical or Important finding returns to the responsible
task and repeats focused plus full verification.

Security review after fixes: APPROVE WITH RELEASE BLOCKERS. Prior CRITICAL env
laundering and HIGH workspace self-grant issues were fixed; runner follow-up spec
and quality/security reviews reported 0 Critical/Important/Minor, and the prior
Medium fully-buffered capture risk is closed by bounded per-stream capture with
zero-output overflow handling.

- [ ] **Step 7: Commit plan closeout evidence**

After both native gates and all reviews pass, change the design status to
“implemented and verified on macOS and Linux” and record the exact backend versions
and pytest counts in the plan evidence section.

Superseding note (2026-08-08): even after accepted-risk documentation lands, do not
close Task 13 or mark the design dual-platform release-ready until macOS
`allowed_execution` gating is proven on a real host, Linux native evidence is
recorded, and final review explicitly confirms the accepted Medium breakaway-child
risk remains documented rather than remediated.

### Updated closeout evidence (2026-08-08)

- `compileall` passed.
- Non-native JUnit suite: 568 passed, 0 failures, 0 errors, 0 skipped; 583 total
  tests with 15 `native_sandbox` cases excluded.
- Existing warnings are unchanged: `aiosqlite` closed-event-loop thread warnings
  plus one Starlette `httpx`/`TestClient` deprecation warning.
- Runner contract suite: 25 passed; `asyncio` debug subset: 3 passed; the accepted-risk
  breakaway characterization also passed separately with `PYTHONASYNCIODEBUG=1`;
  waiter leak fix landed and spec/quality-security review reported 0
  Critical/Important/Minor.
- Static risky-launch scan no matches; diff check clean; precise added-line credential
  scan no exact-token matches; broad `re_` rule produced 59 false positives only;
  redaction subset (8 tests) passed.
- Security review after fixes: APPROVE WITH RELEASE BLOCKERS; prior CRITICAL env
  laundering and HIGH workspace self-grant fixed.
- Follow-up security review identified a separate Medium macOS breakaway-child risk;
  the `start_new_session`/`setsid` characterization showed runner timeout still
  cleans same-PGID descendants while the detached child can survive. The
  diagnostic/test teardown—not the runner—detects and precisely cleans that PID.
  `setpgid` and double-fork remain out of contract but were not separately
  characterized. The user accepted this risk for trusted local use, and final
  full-branch review remains pending.
- Final full-branch security/completeness review then found a separate High
  workspace MCP auto-connect issue. This branch remediates it by fail-closing all
  `workspace_untrusted` MCP registration paths, but final rereview is still
  pending.
- Runner capture contract now enforces 128 KiB per stream; overflow clears both
  streams, returns `output_limit_exceeded`, and terminates the process group,
  resolving the prior fully-buffered Medium.
- Lock-only dependency upgrades landed: `mcp` 1.28.1, `starlette` 1.3.1,
  `pydantic-settings` 2.14.2, `cryptography` 50.0.0, `h2` 4.4.1, `hpack` 4.2.0;
  `uv sync --locked --offline` and `uv lock --check` passed, the compatibility
  suite reported 116 passed, and `pip-audit` 2.10.1 found no known
  vulnerabilities.
- Static scan confirmed no `create_subprocess_shell` or multiprocessing helper in
  the governed launch paths; `auto` still has no host fallback and production
  remains off-limits to unsafe mode.
- Native macOS gate in nested parent sandbox failed readiness at
  `seatbelt capability check failed: allowed_execution`.
- Linux nsjail gate not run.
- Final full-branch security/completeness review remains pending with the lead
  agent after this documentation and current-host native-gate attempt; Linux
  real-host evidence remains a separate release gate.
- Release is blocked until both native gates pass on prepared real hosts and backend
  versions/counts are recorded.

```bash
git add \
  docs/superpowers/plans/2026-08-03-native-sandbox-backends.md \
  docs/superpowers/specs/2026-08-03-native-sandbox-backends-design.md
git commit -m "Make native sandbox completion auditable" \
  -m "The execution checklist now records the exact platform, contract, full-suite, and security evidence required to claim the sandbox implementation complete." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: full non-native suite plus macOS and Linux native gates" \
  -m "Not-tested: none within the approved macOS/Linux scope"
```

## Handoff

Recommended execution path: use `superpowers:subagent-driven-development` with one
fresh implementation agent per task and two-stage review after Tasks 5, 6, 8, 10,
11, and 12. Use `superpowers:executing-plans` only when keeping execution in a single
session with explicit checkpoints.

Do not start Task 12's release claim until both native hosts are available. Earlier
tasks may merge behind fail-closed registration, but production rollout remains
blocked until both platform gates pass.
