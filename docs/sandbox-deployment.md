# 原生沙箱部署

本文说明 MultiClaw 在 macOS Seatbelt 与 Linux nsjail 上运行 shell、code execution 和本地 stdio MCP 的部署条件、最小权限、测试与残余风险。

## 安全目标

生产使用 `governance.sandbox.mode=auto`。启动 probe 至少验证：

- 被允许的命令可以执行；
- 工作区外写入被拒绝；
- 默认网络被拒绝；
- `.env`/`.env.*` 读取被拒绝；
- `.git` 写入被拒绝；
- 不允许 subprocess 的 profile 无法创建 child。

probe 或具体 profile 不健康时，本地执行能力应被跳过，不允许静默回退到宿主执行。

## macOS 前置条件

- `/usr/bin/sandbox-exec` 存在且可执行。
- 目标宿主可以执行 Seatbelt profile；外层 sandbox 不能干扰验证结果。
- 运行依赖必须能在目标 macOS 架构安装；使用 lockfile 验证，而不是在文档固定某个历史依赖版本。
- native gate 必须在真实目标或等价宿主运行。

Seatbelt profile 限制 filesystem、network 和 process 能力。它不是容器；宿主账户权限、工作区权限和最小 MCP grant 仍是边界的一部分。

## Linux 前置条件

- nsjail 已安装，目标 binary 是普通可执行文件。
- `governance.sandbox.linux.nsjail_path` 指向部署 binary；默认 `/usr/bin/nsjail`。
- kernel 与运行环境允许所需 user/mount/network/pid namespace 和限制能力。
- native test 使用独立的 `MULTICLAW_NSJAIL_PATH` 指定被测 binary。
- Linux gate 还应从 jail 内证明没有非 loopback interface/default route，并且不能访问父进程 listener。

容器内运行 nsjail 可能需要额外 kernel/capability 配置；不能通过授予过宽 host privilege 代替威胁建模。

## 生产配置

```toml
[app]
debug = false

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

自定义 profile/config 目录属于可信部署代码，必须随 release review、只读挂载并避免租户写入。

## stdio MCP 最小授权

workspace-untrusted MCP 配置永不自动连接，包括 stdio 和远程 transport。需要启用的 server 必须放入 operator-managed 配置并经过审阅。

可信 stdio server 的保守默认值：

- `sandbox_network = "disabled"`
- `sandbox_workspace = "ro"`
- `sandbox_allow_subprocesses = false`
- `sandbox_env_allowlist = []`
- `sandbox_read_only_paths = []`

只有 server 确实需要时才逐项扩大。Secret environment 必须同时满足同名 allowlist 和精确 `${VAR}` 引用；不要把 literal credential 写入配置。

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

`network=inherit`、可写 workspace、subprocess 与额外 runtime/read-only roots 都是生产敏感例外。对每个 server 记录用途、数据流、owner 和撤销方式。

## Probe 与流量门禁

启动时 [`RuntimeFactory.probe_startup()`](../src/multiclaw/runtime/factory.py) 创建临时 controller、执行 probe、冻结 `SandboxReadiness` 并保存到应用状态。每个租户 runtime 还会创建自己的 controller，并按 readiness 过滤能力。

当前 `/api/health/ready` **不包含** sandbox readiness；它只检查数据库、schema、workspace 与 keyring。因此部署流水线必须：

1. 在目标平台运行本页 native gate。
2. 检查启动 probe、profiles、skipped capabilities 和 registration audit。
3. 确认生产没有 unsafe fallback。
4. 再结合 `/api/health/ready` 决定放量。

账号清除 worker 只在 startup sandbox readiness 健康时启动。sandbox 不健康而公开 ready 通过仍是不可接受的生产状态，应单独告警。

## 输出、超时与清理

[`SandboxProcessRunner`](../src/multiclaw/governance/sandbox/runner.py) 对 stdout、stderr 各限制 128 KiB：

- 任一 stream 超限即终止原 process group；
- `completion_state=output_limit_exceeded`，标明 stream；
- 两路 captured output 都清空，不返回 partial output；
- 普通 timeout 对原 process group 先 TERM 后 KILL；
- runner 只清理由自己创建的 `proc.wait` waiter，不取消调用方拥有的 waiter。

输出限制同时是可用性和数据泄露控制。调用方不得把原始异常、environment 或完整工作区路径拼回公开错误。

## macOS breakaway child 风险

当前保证不覆盖通过 `setsid`、`setpgid` 或 double-fork 脱离原 PGID 的恶意/异常 child。它们仍继承启动时 Seatbelt profile，因此这不是已知的 Seatbelt host-isolation escape；残余风险是持续占用资源，以及继续访问 profile 已授权的路径、网络或 environment。

风险在以下场景更高：

- `shell_workspace`；
- `sandbox_allow_subprocesses=true` 的 stdio MCP；
- 同时授予 network/environment/workspace write；
- unsafe 开发模式。

运维要求：

- subprocess-enabled 本地 MCP 只运行可信实现；
- timeout audit 不能作为所有任意 child 已清理的证明；
- macOS timeout 后监控残留进程，并在严格限定 PID/owner/correlation 后清理；
- 不以 unsafe 模式规避该风险。

## 不安全开发模式

`host_unsafe_dev_only` 只用于隔离开发机调试：

- 必须同时 `app.debug=true`；
- 在宿主直接执行，本质上不提供生产沙箱保证；
- startup/launch 应记录 unsafe fallback；
- 禁止在生产、共享主机或承载真实租户数据的环境使用。

不存在生产安全的 “off”。原生隔离不可用时，修复部署或停止提供相关能力。

## 原生测试命令

默认排除 native：

```bash
uv run pytest -m "not native_sandbox" -q
```

确认两个模块在未 opt-in 时给出明确 skip：

```bash
uv run pytest tests/integration/test_sandbox_macos.py tests/integration/test_sandbox_linux.py -q -rs
```

macOS gate：

```bash
MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 \
  uv run pytest tests/integration/test_sandbox_macos.py -q -x
```

Linux gate：

```bash
MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 \
MULTICLAW_NSJAIL_PATH=/usr/bin/nsjail \
  uv run pytest tests/integration/test_sandbox_linux.py -q -x
```

opt-in 后缺少 backend 必须失败。保留命令、宿主 OS/架构、binary path/version、通过/失败与限制作为 release evidence；不要写固定历史 pass count。

## 配置升级

- 旧 `governance.sandbox_mode=process` 会发出 deprecated warning 并映射到 `auto`；新配置直接使用 `[governance.sandbox]`。
- `docker` 不受当前 Settings 支持，应从部署配置移除。
- 复核所有 operator-managed stdio server 的 workspace/network/env/subprocess grant。
- 移除任何“native 失败时可回退宿主”的运维假设。

## 回滚

1. startup probe、native gate 或 capability audit 不符合预期时停止放量。
2. 重新部署上一个已知良好 artifact/profile/config。
3. 运行非 native suite、目标平台 native gate、公开 ready 和 capability audit。
4. 全部通过后恢复流量。

禁止通过启用 `host_unsafe_dev_only` 承受回滚压力。

## 当前验证限制

- 本次文档交付环境没有执行真实 Linux nsjail gate；目标 Linux 部署必须自行提供证据。
- macOS breakaway child 清理仍是已记录的生命周期/可用性/工作区完整性风险，没有声称已修复。
- 外层受限执行环境可能让 Seatbelt allowed-execution probe 失败；嵌套失败不能代替真实宿主验证，也不能被描述为真实宿主已通过。
- 公开 readiness 不聚合 sandbox readiness；需保留独立门禁，直到生产代码明确改变该契约。
