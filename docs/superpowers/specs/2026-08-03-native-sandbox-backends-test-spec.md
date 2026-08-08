# Test Specification: Native Sandbox Backends

Date: 2026-08-03
Status: approved design coverage; implementation pending
PRD: `docs/superpowers/specs/2026-08-03-native-sandbox-backends-prd.md`
Execution-gate mirror: `.omx/plans/test-spec-native-sandbox-backends.md`

## Quality strategy

The suite is split into deterministic contract tests, backend-rendering tests,
runtime integration tests with fake backends, and native negative tests on the
matching OS. Ordinary tests must not require nsjail or Seatbelt. Native tests run
outside any parent agent/container sandbox because nested sandbox restrictions can
produce false failures.

## Test locations

- `tests/test_config.py` — typed config and legacy migration
- `tests/test_governance.py` — `ExecutionGuard` rename and public exports
- `tests/test_sandbox_models.py` — request/result/readiness invariants
- `tests/test_sandbox_environment.py` — env, XDG, private home/tmp, secret grants
- `tests/test_sandbox_runner.py` — process groups, stdio, timeout, cleanup
- `tests/test_sandbox_seatbelt.py` — SBPL args/render/probe interpretation
- `tests/test_sandbox_nsjail.py` — config render/mount/seccomp/probe interpretation
- `tests/test_sandbox_manager.py` — OS selection, fail-closed, unsafe gating/events
- `tests/test_shell.py` — compatibility plus manager integration
- `tests/test_code_exec.py` — JSON envelope and result compatibility
- `tests/test_mcp_config.py` — stdio security-grant parsing and aliases
- `tests/test_mcp_integration.py` — transport matrix and lifecycle integration
- `tests/test_server.py` — conditional registration and `/health/ready`
- `tests/test_tools.py` — scheduler guard rename and registry behavior
- `tests/integration/test_sandbox_macos.py` — real Seatbelt negative tests
- `tests/integration/test_sandbox_linux.py` — real nsjail negative tests

## Markers and execution

Add pytest markers:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
  "native_sandbox: requires the matching OS sandbox backend and host-level execution",
  "macos_sandbox: requires Darwin and /usr/bin/sandbox-exec",
  "linux_sandbox: requires Linux and MULTICLAW_NSJAIL_PATH",
]
```

Commands:

- Fast contract suite:
  `.venv/bin/python -m pytest -m "not native_sandbox" -q`
- macOS native gate:
  `MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 .venv/bin/python -m pytest -m macos_sandbox -q`
- Linux native gate:
  `MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 MULTICLAW_NSJAIL_PATH=/usr/bin/nsjail .venv/bin/python -m pytest -m linux_sandbox -q`
- Full release suite on each platform:
  `.venv/bin/python -m pytest -q`

## Contract cases

### Configuration

| ID | Case | Expected |
| --- | --- | --- |
| CFG-01 | no sandbox section | typed defaults resolve to `auto` |
| CFG-02 | legacy `sandbox_mode="process"` | effective mode `auto`; one deprecation warning |
| CFG-03 | legacy `sandbox_mode="docker"` | `ValidationError` names unsupported mode |
| CFG-04 | `host_unsafe_dev_only`, debug false | hard validation failure |
| CFG-05 | unsafe mode, debug true | accepted and marked unsafe |
| CFG-06 | invalid network/workspace values | field-specific validation failure |
| CFG-07 | env and config precedence | `MULTICLAW_GOVERNANCE__SANDBOX__...` wins |
| CFG-08 | legacy and nested sandbox config together | hard validation failure |
| CFG-09 | `unsafe_fallback_requires_debug=false` | hard validation failure |
| CFG-10 | `auto` with startup probe disabled | readiness blocked; risky capabilities omitted |

### Models

| ID | Case | Expected |
| --- | --- | --- |
| MOD-01 | shell request has command only | valid |
| MOD-02 | exec request has argv only | valid |
| MOD-03 | command and argv both present/absent | validation failure |
| MOD-04 | cwd outside workspace | typed policy error before backend call |
| MOD-05 | relative/cyclic entrypoint resolution | canonicalization failure |
| MOD-06 | readiness serialization | stable backend/profile/skipped/unsafe fields |
| MOD-07 | readiness finalization | skips may be recorded before one-time freeze; later mutation fails |

### Environment and paths

| ID | Case | Expected |
| --- | --- | --- |
| ENV-01 | host env contains token/password/cloud keys | absent from launch env |
| ENV-02 | allowed LANG/LC_ALL | preserved |
| ENV-03 | HOME/TMPDIR/XDG | unique private paths below manager temp root |
| ENV-04 | host agent/socket variables | absent |
| ENV-05 | MCP secret key not in allowlist | policy error with key name, never value |
| ENV-06 | MCP secret key in allowlist | value included; audit/log output redacted |
| ENV-07 | `.git` and `.env*` rules | distinct write-protected/read-hidden sets |
| ENV-08 | explicit runtime root outside allowed grant scope | fail closed |
| ENV-09 | more than 16 explicit runtime roots | fail closed before backend rendering |
| ENV-10 | hostile correlation ID | private root remains a generated child of manager root |
| ENV-11 | override runtime-owned env keys | policy error even when key is allowlisted |

### Process runner

| ID | Case | Expected |
| --- | --- | --- |
| RUN-01 | stdout and stderr | byte streams captured independently |
| RUN-02 | normal exit | real exit code and no timeout |
| RUN-03 | signal exit | signal metadata populated |
| RUN-04 | timeout with cooperative child | TERM ends process group |
| RUN-05 | timeout with TERM-ignoring child | KILL after two-second grace |
| RUN-06 | child forks descendant that stays in the original PGID | no descendant remaining in the original process group after timeout |
| RUN-07 | spawn raises OSError | typed launch error, no success result |
| RUN-08 | stdin payload | fully delivered, then closed |
| RUN-09 | parent task cancellation | process group terminated and no descendant remaining in the original process group |
| RUN-10 | manager success, timeout, cancellation, and pre-spawn failure | per-launch private root removed |

## Backend rendering cases

### Seatbelt unit tests

- SB-01: target wrapper is exec-form and shell mode ends with
  `-- /bin/sh -c <raw command>`.
- SB-02: workspace/tmp/home/runtime roots are passed through `-D` values; raw command
  never appears inside profile text.
- SB-03: disabled network profile contains deny rules and no broad network allow.
- SB-04: `.git` write deny and `.env*` read deny are represented separately.
- SB-05: probe output maps allowed/denied checks into per-capability readiness.
- SB-06: missing binary, non-zero probe, or incomplete capability proof is unavailable.

### nsjail unit tests

- NS-01: user/mount/PID/IPC/UTS namespaces, `no_new_privs`, capability drop present.
- NS-02: network-disabled config creates isolated network namespace.
- NS-03: workspace mode, runtime roots, private tmp/home, `.git` overlay, hidden paths
  render as distinct mounts.
- NS-04: code profile contains child-process seccomp/rlimit policy.
- NS-05: dynamic paths are protobuf-text escaped and never shell-interpreted.
- NS-06: missing binary/kernel feature/probe proof is unavailable.

Backend tests compare normalized structures or exact stable fragments; they do not
snapshot volatile correlation IDs or temp paths.

## Tool compatibility cases

### Shell

| ID | Command | Assertion |
| --- | --- | --- |
| SH-01 | `printf 'a\\nb\\n' | tail -n 1` | pipe output contains `b` |
| SH-02 | `printf value > out.txt && cat out.txt` | redirect works inside workspace |
| SH-03 | `name='a b'; printf '%s' "$name"` | quoting preserved |
| SH-04 | `touch a.py b.py; printf '%s\\n' *.py` | globbing preserved |
| SH-05 | `VALUE=ok sh -c 'printf %s "$VALUE"'` | env assignment preserved |
| SH-06 | cwd subdirectory | `pwd` equals canonical requested cwd |
| SH-07 | stdout + stderr + exit 7 | labels and `data.exit_code=7` preserved |
| SH-08 | oversized stdout/stderr | current 30,000-char truncation contract preserved |
| SH-09 | outside-workspace write | denied on native backends |
| SH-10 | local host TCP listener | connection denied when network disabled |
| SH-11 | timeout/fork tree within the original PGID | timeout marker and no same-PGID descendants remaining after timeout |

### Code execution

| ID | Case | Assertion |
| --- | --- | --- |
| PY-01 | `print(3)` | success data true and stdout `3` |
| PY-02 | user stderr | stderr label preserved |
| PY-03 | `raise ValueError('bad')` | tool success, data success false, traceback error |
| PY-04 | restricted import | current ImportError contract preserved |
| PY-05 | infinite loop | timeout marker and `data={}` |
| PY-06 | malformed runner envelope | tool error |
| PY-07 | envelope above limit | tool error after explicit size validation |
| PY-08 | attempted subprocess | denied; no descendant process |
| PY-09 | attempted outside write/network | denied on both native backends |
| PY-10 | stdout/stderr/error truncation | matches existing caller formatting |

## MCP cases

| ID | Case | Expected |
| --- | --- | --- |
| MCP-01 | stdio defaults | workspace ro, network off, subprocess off |
| MCP-02 | explicit cwd | canonical cwd passed to `StdioServerParameters` |
| MCP-03 | network/workspace/subprocess grants | reflected in request and startup security log |
| MCP-04 | secret env missing allowlist | server state FAILED/skipped without value leak |
| MCP-05 | backend unavailable | stdio filtered before `connect_servers()` |
| MCP-05a | `workspace_untrusted` stdio defaults | skipped before `connect_servers()` with sanitized `sandbox.registration_skipped` evidence |
| MCP-05b | `workspace_untrusted` HTTP/SSE/WS literal config | skipped before `connect_servers()` with no URL/header leakage |
| MCP-06 | in-process in auto | rejected before factory creates transport |
| MCP-07 | in-process unsafe debug | allowed with unsafe evidence |
| MCP-08 | trusted HTTP/SSE/WS without backend | still created; marked remote-unsandboxed |
| MCP-09 | wrapped stdio connect/disconnect | existing SDK context enter/exit used once |
| MCP-10 | reconnect/tool refresh | registry namespace updates as today |
| MCP-11 | MCP SDK default env contains host HOME/PATH/USER/LOGNAME | controlled sandbox values or blanks override every SDK-default key |
| MCP-12 | stdio disconnect and failed connect | SDK context closes before private root removal; no residual root |
| MCP-13 | bare/absolute stdio command | canonical entrypoint is within built-in or explicitly granted runtime roots; controlled PATH contains only granted bin dirs |

## Server, events, and audit

- SRV-01: ready backend registers shell/code-exec before agent is returned.
- SRV-02: failed backend omits both and lists them in readiness.
- SRV-03: `/health/ready` is public through `AuthMiddleware`.
- SRV-04: ready response is HTTP 200; blocked response is HTTP 503.
- SRV-05: response contains no absolute secret paths or env values.
- SRV-06: `workspace_untrusted` MCP registration is fail-closed before any
  transport-specific connect/start branch and records sanitized skip evidence.
- EVT-01: native success order remains scheduled, validating, executing, audit success,
  completed.
- EVT-02: approval order remains awaiting-approval audit before executing.
- EVT-03: launch failure produces audit error then `tool.error`.
- EVT-04: stdio startup events identify transport/server and do not appear as tool-call
  sandbox launches.
- EVT-05: unsafe startup and each unsafe launch emit `sandbox.unsafe_fallback_used`.

## Native negative tests

Each native test creates a workspace and a separate sentinel directory owned by the
test parent. It starts a local TCP listener on the parent host for deterministic
network denial and records child PIDs for process-group cleanup checks.
Tests never contact the public internet.

Accepted risk / out-of-contract behavior:

- Native orphan semantics cover the original process group only.
- macOS breakaway children that escape the original PGID via `setsid`, `setpgid`, or
  double-fork patterns are explicitly out of contract for forced cleanup.
- The characterization test reproduces a `start_new_session`/`setsid` survivor after
  runner timeout. `setpgid` and double-fork remain out of contract but are not
  separately characterized.
- That reproduced survivor is a documented accepted-risk result, not by itself a
  test failure against RUN-06, RUN-09, or SH-11.
- The recorded breakaway PID is diagnostic/test-harness state, not runner contract.
- Test teardown must detect any surviving breakaway PID, precisely clean it up, and
  confirm no residual process remains so tests can prove the launched task and
  same-PGID descendants were terminated.

macOS gate proves:

- Seatbelt binary and templates execute.
- outside write, `.git` write, `.env` read, parent TCP listener access, and code child
  creation are denied.
- allowed workspace write and Python runtime reads succeed.
- same-PGID timeout descendants are cleaned up; breakaway descendants are documented
  as accepted out of contract.

Linux gate proves the same behavior through nsjail and additionally verifies the
expected namespace/mount view from inside the jail.

## Release gates

- Contract suite has zero failures.
- Relevant baseline remains at least the pre-change `90 passed`, with new tests added.
- Full repository suite passes.
- macOS native suite passes on a supported macOS host.
- Linux native suite passes on a Linux host with the deployed nsjail version and
  required kernel features.
- `git diff --check`, compileall, and credential scan pass.
- A verifier confirms every PRD acceptance criterion has test evidence.

## Failure triage

- Rendering/unit failure: implementation defect; do not change policy defaults to pass.
- Native probe failure: backend unavailable; keep readiness blocked.
- Compatibility failure: repair runtime roots/env/capture while preserving deny rules.
- Environment-only nested-sandbox failure: rerun on the native host gate; never waive
  the native release result based on an inner sandbox run.
