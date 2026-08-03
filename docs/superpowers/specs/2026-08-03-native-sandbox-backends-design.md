# 原生沙箱后端技术设计

**日期**：2026-08-03

**状态**：已批准，待单独制定实施计划

**范围**：macOS `sandbox-exec` + Seatbelt、Linux nsjail；运行时自动选择；本文件只定义设计，不包含实现

## 决策摘要

MultiClaw 将在真实子进程启动边界执行 OS 级隔离，而不是继续把 Python callable 超时包装称为沙箱：

- macOS 使用 `/usr/bin/sandbox-exec` 和静态 Seatbelt 策略模板。
- Linux 使用 nsjail、namespace、只读挂载、seccomp 与 rlimit。
- `SandboxManager` 由应用运行时创建，并注入 `shell`、`code_exec` 和 MCP stdio 启动路径。
- `CoreToolScheduler` 继续负责参数校验、权限审批、审计和事件顺序，不负责 OS 隔离。
- `auto` 模式严格 fail closed；唯一无沙箱路径是显式 `host_unsafe_dev_only`，且只允许 `app.debug=true`。

## 现状与问题

当前 `ProcessSandbox` 只使用 `asyncio.wait_for` 提供超时，并不限制文件、网络或子进程（`src/multiclaw/governance/sandbox.py:13`）。真正的进程边界分散在：

- `shell`：`asyncio.create_subprocess_shell(...)`（`src/multiclaw/tools/shell.py:86`）。
- `code_exec`：`multiprocessing.Manager()` 和 `multiprocessing.Process(...)`（`src/multiclaw/tools/code_exec.py:99`）。
- MCP stdio：`StdioTransport.connect()` 调用 MCP SDK 启动本地服务（`src/multiclaw/mcp/transport/stdio.py:41`）。

`GovernanceSettings.sandbox_mode` 当前只是未校验字符串，`server.py` 也始终构造同一个 `ProcessSandbox()`（`src/multiclaw/config/settings.py:43`、`src/multiclaw/server.py:278`）。因此现有实现不能提供可信的进程隔离声明。

## 目标与非目标

目标：

- 阻止 shell、Python 代码和本地 MCP 服务越权访问宿主文件、网络、凭据与 socket。
- 保留现有 shell 字符串语义、工具结果格式、审批流程和事件顺序。
- 在后端缺失或策略不可用时阻止危险能力，而不是静默回退宿主执行。
- 允许 macOS 与 Linux 使用不同底层能力，但对上层暴露一致、可测试的公共合同。

非目标：

- 不支持 Windows、容器后端或 microVM。
- 不用本地 OS 沙箱描述远程 HTTP/SSE/WebSocket MCP 的安全性。
- 首版不提供域名级网络白名单；进程网络只支持 `disabled` 或显式 `inherit`。
- 不替代权限审批、Web 工具的 `NetworkPolicy` 或 Python restricted builtins。

## 方案比较

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 调度器包装整个 callable | 改动最小、入口集中 | 无法控制 callable 内部真实子进程 | 拒绝 |
| 运行时 `SandboxManager` + 启动点适配 | 边界正确、复用策略、适配现有架构 | 需要改造三个启动面 | 采用 |
| 独立 runner daemon/service | 单一长期隔离入口 | 协议、部署、恢复和可用性成本过高 | 后续可选 |

采用方案的核心张力是“shell 行为兼容”与“可执行文件完全确定”不能同时满足。设计保留 `/bin/sh -c <raw command>`，固定并验证首个 shell 入口；后续动态命令由受限 `PATH`、只读执行根目录和继承的沙箱策略约束。

## 总体架构

```text
FastAPI startup
  -> SandboxManager (OS selection, config, probes, readiness)
       -> SeatbeltBackend (macOS)
       -> NsJailBackend   (Linux)
  -> ShellToolBuilder -------> SandboxProcessRunner
  -> CodeExecToolBuilder ----> SandboxProcessRunner
  -> MCPClientManager -------> wrapped StdioServerParameters

CoreToolScheduler
  -> validation -> approval -> tool events/audit -> invocation
  -> does not render or own OS sandbox policy
```

`ProcessSandbox` 拆分为：

- `ExecutionGuard`：仅负责进程内 callable 的超时与取消。
- `SandboxManager`：选择后端、验证配置、探测能力、生成启动规格和控制危险降级。
- `SandboxProcessRunner`：执行一次性进程，负责进程组、stdio、超时和 TERM→KILL。
- `SandboxBackend`：把公共策略转译为 Seatbelt 或 nsjail 启动参数。

## 启动合同

`SandboxExecRequest` 包含：

- `tool_name`、`profile_name`、`correlation_id`
- `mode = shell_string | exec_argv`
- 二选一的 `command` 或 `argv`
- `cwd`、`stdin_bytes`、`timeout_seconds`、`env_overrides`
- 可选 `mcp_server_name`

`SandboxManager.build_launch_spec()` 返回纯 exec-form 的 `SandboxedLaunchSpec`：`executable`、`args`、`cwd`、`env`、`stdin_bytes`、后端和 profile 元数据。用户输入不会被拼接成沙箱 wrapper 的第二层 shell 字符串。

在 `shell_string` 模式中，目标 argv 固定为 `/bin/sh -c <raw command>`。在 `exec_argv` 模式中不经过 shell。

一次性进程启动后开始计时；超时先向进程组发送 `SIGTERM`，等待 2 秒，再发送 `SIGKILL`。stdout/stderr 按字节独立捕获，由调用方以 UTF-8 replacement 解码并保持现有截断行为。

MCP stdio 是长生命周期进程：MultiClaw 只把沙箱 wrapper 的 command/args/cwd/env 传给 MCP SDK 的 `StdioServerParameters`，继续使用 `stdio_client` 的 JSON-RPC、关闭 stdin、TERM 和 KILL 流程，不重写 MCP 协议。

## 策略模型

### 公共环境与文件规则

- 每次启动创建私有 `TMPDIR` 和空 `HOME=<TMPDIR>/home`，不继承宿主 home 或共享 `/tmp`。
- macOS 默认 `PATH=/usr/bin:/bin:/usr/sbin:/sbin`；Linux 默认 `/usr/bin:/bin`。
- 仅保留 `LANG`、`LC_ALL` 和必要时的 `TERM`；注入合成的 `USER`、`SHELL`、`HOME`、`TMPDIR`、`PATH`。
- `XDG_CONFIG_HOME`、`XDG_CACHE_HOME`、`XDG_DATA_HOME` 重定向到私有 home。
- 默认拒绝 secret-shaped 环境变量；只有服务器配置中的显式安全授权可以传入。
- 宿主 home、SSH/GPG agent、Docker/container socket、宿主临时目录均不进入可读视图。
- `.git` 默认可读但禁止写、改名、删除和 chmod/chown。
- `.env`、`.env.*` 默认从沙箱读视图隐藏；若后端无法证明隐藏能力，对要求该规则的 profile 判为不可用。

### Profile

| Profile | 工作区 | 网络 | 子进程 | 入口 |
| --- | --- | --- | --- | --- |
| `shell_workspace` | rw；`.git` 写保护 | 默认 disabled | 允许，后代继承沙箱 | 固定 `/bin/sh` |
| `code_exec_python` | rw；运行时根只读 | disabled | 必须拒绝创建 | 精确 Python 路径 |
| `mcp_stdio_local` | 每服务 ro/rw，默认 ro | 每服务 disabled/inherit，默认 disabled | 每服务显式授权，默认 false | 精确服务 launcher |

所有入口先 canonicalize，再和 profile 入口允许集比较；解析失败或越界时拒绝启动。shell 后代命令不做虚假的预解析，而是受沙箱文件视图和受限 `PATH` 控制。

## 平台策略转译

### macOS：Seatbelt

- 使用随应用发布、经过审查的静态 SBPL 模板。
- 动态路径通过 `sandbox-exec -D KEY=VALUE` 传入；禁止把用户路径或命令插值进 SBPL 文本。
- 默认拒绝文件、网络和进程能力，再按 profile 放行 canonical runtime roots、工作区、私有 tmp/home 和必要进程操作。
- 启动规格形态为 `sandbox-exec <profile args> -- <target argv>`。
- 行为探测必须证明：允许的无害命令可运行、工作区外写入被拒绝、默认网络被拒绝、`code_exec` 后代进程创建被拒绝。

### Linux：nsjail

- 从应用自有模板生成每 profile 的 protobuf 配置；动态值作为数据序列化，不拼接 shell。
- 使用 user、mount、PID、IPC、UTS 和 network namespace，丢弃 capabilities，并启用 `no_new_privs`。
- 运行时根只读挂载；工作区按 profile ro/rw；私有 tmp/home 使用 tmpfs 或专用 bind；工作区 rw 时将 `.git` 覆盖为只读。
- 默认网络使用无宿主接口的独立 namespace。
- 使用 seccomp/rlimit 实现公共约束；cgroup 是可选加强项，不是公共语义前提。
- 探测必须验证 user namespace、挂载、seccomp、允许命令、拒绝写入、拒绝网络和 `code_exec` 后代进程拒绝。

Linux 可提供更强资源限制，但公共 API 只承诺两个后端都能由运行时行为探测证明的能力。平台负向集成测试是发布门禁；运行时 readiness 以当前主机探测结果为准。

## 配置设计

新配置使用嵌套结构：

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

模式只有：

- `auto`：按 OS 选择后端，任一探测、策略渲染或启动失败都不得回退宿主执行。
- `host_unsafe_dev_only`：只在 `app.debug=true` 时有效；否则配置硬错误。启用时启动和每次危险执行都记录高危日志与事件。

兼容迁移：

- 旧值 `process` 映射为 `auto` 并输出精确弃用警告。
- 旧值 `docker` 直接配置失败，因为仓库没有 Docker 后端。
- 不提供生产 `off`。

stdio MCP 增加：`cwd`（省略时工作区根）、`sandbox_network=disabled|inherit`、`sandbox_workspace=ro|rw`、`sandbox_allow_subprocesses`。网络继承、工作区写入、子进程和 secret-shaped env 都是显式安全授权，启动时记录服务器名和授权类型。

## Fail-closed 与 readiness

启动时只创建一次不可变 `SandboxReadiness`，存入 `app.state.sandbox_readiness`，并由新增 `/health/ready` 返回后端、probe、各 profile、跳过能力和 unsafe fallback 状态。

后端不可用时：

- 服务 liveness 保持正常，便于登录和查看诊断。
- readiness 返回失败，阻止生产流量或部署继续推进。
- 在注册阶段跳过 `shell` 和 `code_exec`。
- 在调用 `MCPClientManager.connect_servers(...)` 前过滤 stdio MCP；不启动、不注册。
- 远程 HTTP/SSE/WS MCP 可继续注册，但日志明确标注“无本地 OS 沙箱边界”。
- in-process MCP 在 `auto` 中禁止，只能在 `host_unsafe_dev_only` 中启用。

不存在“探测失败后自动改为宿主进程”的路径。

## 各执行面改造

### Shell

`ShellInvocation` 继续接收原始字符串，安全检查、cwd 校验、输出格式和截断保持不变。它改为提交 `mode=shell_string` 请求，由 runner 执行沙箱 wrapper；必须用回归测试锁定 pipes、redirects、quoting、globbing、env assignment、stderr、exit code、cwd 和进程组清理。

### Code execution

移除 `multiprocessing.Manager()` 和 `multiprocessing.Process()`。新流程只启动一个精确 Python interpreter：

1. 父进程将 UTF-8 代码写入 child stdin；不生成源码文件，也不支持交互 stdin。
2. runner 默认继续使用 `restrict_builtins=True`，以 `StringIO` 捕获用户 stdout/stderr。
3. runner 捕获异常并在 stdout 输出单个 JSON envelope：`success`、`stdout`、`stderr`、`error`。
4. 父进程校验 envelope，应用现有 30,000 字符截断并还原结果。

结果兼容要求：

- 成功：tool status `success`，`data={"success": true}`。
- Python 异常：tool status `success`，保留 traceback，`data={"success": false, "error": "..."}`。
- 超时：tool status `success`，保留现有 timeout marker 和可用的截断 stdout，`data={}`。
- probe、策略渲染、入口验证、启动或 envelope 错误：tool status `error`。

### MCP

| Transport | 行为 |
| --- | --- |
| stdio | 在 transport 启动点使用 wrapper；后端不可用时启动前跳过 |
| in-process | `auto` 禁止；只允许 unsafe dev 模式 |
| HTTP/SSE/WS | 可用，但明确不属于本地 OS 沙箱范围 |

MCP tool call 仍经过现有 scheduler。stdio 沙箱事件发生在服务器启动时，而不是错误地归因到后续某次工具调用。

## 错误、事件与审计

错误类型至少区分：后端缺失、probe 失败、旧模式无效、profile 渲染失败、入口不允许、cwd 越界、策略拒绝、exec 前启动失败、超时。

新增事件：

- `sandbox.probed`
- `sandbox.profile_unavailable`
- `sandbox.registration_skipped`
- `sandbox.launch_failed`
- `sandbox.unsafe_fallback_used`

现有审计模型只有 `tool_name`、`status` 和 `detail` 等基础字段，首版把 backend/profile/fallback 编码进 `detail`，不在本设计中扩展审计 schema。结构化日志包含 `tool`、`transport`、`sandbox_backend`、`sandbox_profile`、`sandbox_mode`、`unsafe_fallback_used`、`correlation_id` 和可选 `mcp_server_name`。

原生工具事件顺序保持：`tool.scheduled` → `tool.validating` → permission/approval → `tool.executing` → sandbox launch → audit success/error → `tool.completed`/`tool.error`。

## 实施顺序

1. 在 `config/settings.py` 增加 typed settings 和旧值迁移，更新仓库配置与配置测试。
2. 建立 `governance/sandbox/` 的模型、manager、runner、后端、模板、probe 和错误类型；保留独立 `ExecutionGuard`。
3. 在 `server.py` 创建 manager/readiness，先过滤危险能力，再注册工具和连接 MCP，并增加 `/health/ready`。
4. 迁移 `tools/shell.py`，先锁定行为兼容测试，再接入沙箱 runner。
5. 迁移 `tools/code_exec.py` 到单 interpreter JSON-envelope 协议。
6. 扩展 MCP config/types/factory/stdio transport，加入三类 transport 的门禁。
7. 完成两个平台的负向集成测试、事件顺序测试、部署说明和回滚说明后再启用默认 `auto`。

完整实施任务与文件分工记录在 `.omx/plans/2026-08-03-native-sandbox-backends.md`。

## 验证与验收

单元测试覆盖 OS 选择、请求互斥校验、profile 渲染、env scrub、路径规则、旧值迁移、unsafe fallback 门禁。

集成测试覆盖：

- shell 工作区外写入和默认网络被拒绝。
- shell 字符串语义、输出、超时和进程组行为不回退。
- code_exec 单子进程、JSON envelope、结果格式、子进程拒绝和超时。
- MCP stdio 的 cwd、env、network、workspace、connect/disconnect/reconnect/tool refresh。
- in-process MCP 在 `auto` 中拒绝。

端到端与可观测性覆盖：

- 后端缺失时 liveness 正常、readiness 失败、危险工具缺席、stdio MCP 跳过、远程 MCP 可用。
- approval 与 audit/event 顺序保持。
- 成功 probe、失败 probe、跳过注册、启动失败和 unsafe fallback 都产生预期事件与日志。
- 用户错误不泄漏 secret env 或宿主路径细节。

发布验收条件：

1. macOS 和 Linux 均通过工作区外写入拒绝、默认网络拒绝和 code_exec 后代进程拒绝测试。
2. 任一 runtime probe 失败时不出现宿主执行。
3. shell 行为兼容矩阵全部通过。
4. code_exec 保持当前结果结构且不再使用 multiprocessing helper。
5. stdio/in-process/remote MCP 行为符合 transport 矩阵。
6. `host_unsafe_dev_only` 的 debug 门禁和逐次高危告警可验证。

## 预演失败场景

1. **系统升级使 backend 不可用**：行为 probe 阻止危险能力注册，readiness 失败，绝不回退宿主执行。
2. **profile 过严破坏合法命令或 MCP**：runtime-root 探测、shell 兼容矩阵和明确拒绝诊断阻止操作者盲目开启 unsafe fallback。
3. **超时/断开留下孤儿进程**：一次性进程组 TERM→KILL 测试和 MCP connect/disconnect/reconnect 后的进程清点作为发布门禁。

## ADR

**Decision**：采用运行时拥有、启动点执行的 `SandboxManager`；macOS 使用 Seatbelt，Linux 使用 nsjail；默认 fail closed。

**Drivers**：当前 `ProcessSandbox` 不隔离；真实边界分散；必须兼容现有调度、审批和 shell 行为；服务需要在危险能力被禁用时仍能提供诊断。

**Alternatives considered**：拒绝 scheduler callable wrapper；暂缓外部 runner service。

**Why chosen**：这是在当前架构中能够覆盖真实启动边界、又不引入新守护进程协议的最小可信方案。

**Consequences**：`code_exec` 需要实质改造；MCP 按 transport 分类；生产无普通 `off`；shell 只能保证固定入口和受限执行视图，不能预声明所有动态后代命令。

**Follow-ups**：批准本规格后另行编写实施 PRD 与 test spec；实现完成后由安全审查和两个平台的负向测试共同验收。
