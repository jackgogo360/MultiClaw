# Native Sandbox Deployment Guide

This guide covers production deployment and rollback for MultiClaw's native sandbox backends.

## Prerequisites

### macOS

- `/usr/bin/sandbox-exec` must exist and be executable.
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

Status as of August 5, 2026:

- Default collection and default skip behavior are verified in this branch.
- Non-native verification remains required before release.
- macOS native verification remains a release gate and must pass on a supported macOS host outside any interfering parent sandbox.
- Linux native verification remains a release gate and has not been validated in this environment because `nsjail` is not available here.
- Both native gates remain pending until they pass in real host environments with the reviewed MCP restrictions enabled.
- The Linux native gate is designed to fail if it cannot inspect `/proc/net/route` or enumerate interfaces from inside the jail; it must not silently pass on parent-loopback denial alone.
- The current nested-macOS characterization failed at readiness with `probe_reason='seatbelt capability check failed: allowed_execution'` and all native profile readiness values false.
- A prior nested-macOS characterization had reached Seatbelt profile execution and returned `-6`; treat both outcomes as environment constraints, not as evidence to loosen the sandbox policy.

Release should stay blocked until both native platform gates pass in their real host environments.
