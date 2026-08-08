# Native Sandbox Deployment Guide

This guide covers production deployment and rollback for MultiClaw's native sandbox backends.

## Prerequisites

### macOS

- `/usr/bin/sandbox-exec` must exist and be executable.
- `cryptography==50.0.0` does not publish an Intel macOS `x86_64` wheel.
- Intel macOS source builds for `cryptography==50.0.0` require a Rust toolchain plus OpenSSL development headers and libraries.
- Current validation on this machine succeeded only with a temporary Rust + OpenSSL setup; no system Rust installation is present.
- Native verification must run on a supported macOS host, not inside another parent sandbox that interferes with Seatbelt policy execution.
- The startup probe must prove:
  - allowed execution works
  - outside-workspace writes are denied
  - network is denied by default
  - `.env` reads are denied
  - `.git` writes are denied
  - `code_exec` child creation is denied

### Linux

- `nsjail` must be installed and executable.
- Set `MULTICLAW_NSJAIL_PATH` to the exact deployed binary path for native verification.
- The host kernel must support the namespaces and restrictions required by the configured nsjail profile.
- The startup probe must prove the same deny-path matrix as macOS before readiness can become healthy.
- The Linux native gate additionally proves from inside the jailed process view that no non-loopback interfaces and no default route are visible while parent-listener access is denied.

## Configuration

Use `auto` in production. Do not use `host_unsafe_dev_only` outside local debugging.

```toml
[governance.sandbox]
mode = "auto"
backend_probe_on_startup = true
unsafe_fallback_requires_debug = true
write_protected_workspace_paths = [".git"]
read_hidden_workspace_paths = [".env", ".env.*"]

[governance.sandbox.profiles]
shell = "shell_workspace"
code_exec = "code_exec_python"
mcp_stdio = "mcp_stdio_local"

[governance.sandbox.macos]
seatbelt_profile_dir = ""

[governance.sandbox.linux]
nsjail_path = "/usr/bin/nsjail"
nsjail_config_dir = ""
```

## Stdio MCP Grants

Grant extra stdio MCP access explicitly and minimally. Never embed literal credentials in config files.

- High-privilege local MCP settings must come from a trusted operator-managed config outside the workspace.
- Workspace `.mcp.json` files are conservative-only:
  - `sandbox_network = "disabled"`
  - `sandbox_workspace = "ro"`
  - `sandbox_allow_subprocesses = false`
  - `sandbox_env_allowlist = []`
  - `sandbox_read_only_paths = []`
- Workspace configs cannot use `${...}` expansion anywhere, including remote MCP URLs, headers, or OAuth fields.
- Secret env expansion is allowed only as an exact same-key reference with an exact allowlist entry, for example `API_TOKEN = "${API_TOKEN}"` together with `sandbox_env_allowlist = ["API_TOKEN"]`.

```toml
[mcp.servers.example_stdio]
transport = "stdio"
command = "/usr/bin/env"
args = ["bash", "-lc", "exec ./run-example-mcp"]
cwd = "."
sandbox_workspace = "ro"
sandbox_network = "inherit"
sandbox_allow_subprocesses = false
sandbox_env_allowlist = ["SERVICE_TOKEN"]
sandbox_read_only_paths = ["/opt/example-mcp"]
env = { SERVICE_TOKEN = "${SERVICE_TOKEN}" }
```

`sandbox_network="inherit"`, writable workspaces, subprocess grants, and extra runtime roots are explicit security exceptions. Review each one as production-sensitive.

## Readiness And Probe Semantics

- `/health/ready` returns `200` only when the selected backend is available, probe evidence passes, and required sandbox profiles are ready.
- `/health/ready` returns `503` when native sandbox proof is incomplete or blocked.
- Liveness can remain healthy while readiness is `503`; this is intentional so operators can inspect diagnostics without exposing unsafe execution.
- In `auto`, failed probes block local stdio sandboxed capabilities instead of falling back to host execution.

## Runner Output And Cleanup Semantics

- `stdout` and `stderr` are each capped at 128 KiB.
- If either stream exceeds that cap, the runner terminates the full process group, sets `completion_state=output_limit_exceeded`, clears both captured streams, and returns no partial output.
- The runner cleans up only the `proc.wait` waiter it created itself; it does not cancel waiters owned by the caller.

## Migration Notes

Legacy modes map as follows:

- `process` is deprecated and migrates to `auto`.
- `docker` is no longer supported and must be removed before deployment.
- There is no production-safe equivalent of "off". If native isolation is unavailable, keep readiness blocked and fix the deployment.

Migration checklist:

1. Replace legacy `governance.sandbox_mode` usage with `[governance.sandbox]`.
2. Confirm production config uses `mode = "auto"`.
3. Verify local stdio MCP servers declare only the minimum workspace/network/env grants they need.
4. Remove any operational assumption that host fallback is acceptable in production.

## Unsafe Development Mode

`host_unsafe_dev_only` is for local debugging only.

- It requires `app.debug = true`.
- It is prohibited in production.
- It records unsafe fallback usage at startup and launch time.
- It must not be used to "work around" a failing native release gate.

## Native Test Commands

Default non-native validation:

```bash
uv run pytest -m "not native_sandbox"
```

Default collection of native modules should skip them cleanly unless they are explicitly opted in:

```bash
uv run pytest tests/integration/test_sandbox_macos.py tests/integration/test_sandbox_linux.py -rs
```

macOS native gate:

```bash
MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 uv run pytest tests/integration/test_sandbox_macos.py -q -x
```

Linux native gate:

```bash
MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 MULTICLAW_NSJAIL_PATH=/usr/bin/nsjail \
  uv run pytest tests/integration/test_sandbox_linux.py -q -x
```

## Rollback

Rollback is deployment-based and commit-based:

1. Redeploy the last known-good release artifact if readiness blocks production rollout.
2. Revert or cherry-pick back to the last known-good commit if configuration or policy changes caused the regression.
3. Re-run the non-native suite and the matching native gate before re-promoting the rollback.

Do not bypass rollback pressure by enabling `host_unsafe_dev_only` in production.

## Known Verification Status

Status as of August 7, 2026:

- Non-native JUnit verification recorded 567 passed, 0 failures, 0 errors, and 0 skipped, for 582 total tests with 15 native-gated cases excluded.
- `python -m compileall` passed.
- Runner coverage passed with 24 tests, and the asyncio debug subset passed with 3 tests.
- Two-phase review completed with 0 Critical, 0 Important, and 0 Minor findings.
- Lock-only dependency upgrades were verified exactly at `mcp==1.28.1`, `starlette==1.3.1`, `pydantic-settings==2.14.2`, `cryptography==50.0.0`, `h2==4.4.1`, and `hpack==4.2.0`.
- `uv sync --locked --offline` and `uv lock --check` both passed.
- Compatibility verification passed with 116 tests.
- `pip-audit==2.10.1` reported no known vulnerabilities.
- Static scanning found no `create_subprocess_shell`, no multiprocessing helper usage, no `auto` host fallback, and no production `off` mode.
- Exact long-token scanning found no matches; a broader `re_` rule still reports 59 identifier or dummy Bearer false positives.
- The redaction subset passed with 8 tests.
- Remaining warnings are pre-existing `aiosqlite` closed-event-loop thread warnings plus one Starlette `httpx`/`TestClient` deprecation warning.
- macOS nested gating still fails at readiness with `probe_reason='seatbelt capability check failed: allowed_execution'`.
- The Linux native gate was not executed in this environment.
- Release remains blocked until both real-host native gates pass with the reviewed MCP restrictions enabled.

Release should stay blocked until both native platform gates pass in their real host environments.
