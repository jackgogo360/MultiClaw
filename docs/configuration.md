# 配置参考

配置事实源是 [`src/multiclaw/config/settings.py`](../src/multiclaw/config/settings.py)。MultiClaw 默认读取当前工作目录的 `multiclaw.toml`，嵌套环境变量使用 `MULTICLAW_` 前缀和双下划线，例如 TOML `database.url` 对应 `MULTICLAW_DATABASE__URL`。

## 加载顺序

优先级从高到低：

1. 显式传给 `Settings(...)` 的构造参数。
2. `MULTICLAW_` 环境变量。
3. `multiclaw.toml`。
4. Pydantic 模型默认值。

环境变量中的 `true`/`false`、整数以及以 `[`/`{` 开头的合法 JSON 会被转换；其他值保留字符串。列表和映射建议用 JSON：

```bash
export MULTICLAW_APP__ALLOWED_ORIGINS='["https://console.example.com"]'
export MULTICLAW_SKILL__EXTRA_DIRS='["/opt/multiclaw/skills"]'
```

两个环境变量是特殊安全输入，不会复制到普通 Settings 字段：

- `MULTICLAW_AUTH_JWT_SIGNING_KEY` 由认证组件直接加载。
- `MULTICLAW_SECRETS_KEYRING_B64` 由 keyring 组件直接加载。

它们分别与 `auth.jwt_signing_key_file`、`secrets.keyring_file` 构成“恰好一个来源”的互斥关系。

安全分类：**普通**可进入受版本控制的示例；**部署相关**通常应由部署配置提供；**敏感**不得提交真实值；**高风险**会放宽安全边界，仅可在明确场景启用。

## 应用与部署：`app`、`deployment`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `app.name` | `MULTICLAW_APP__NAME` | string / `MultiClaw` | 普通；展示名称 |
| `app.version` | `MULTICLAW_APP__VERSION` | string / `0.1.0` | 普通；应与发布版本一致 |
| `app.debug` | `MULTICLAW_APP__DEBUG` | bool / `false` | 部署相关；生产保持 false |
| `app.allowed_origins` | `MULTICLAW_APP__ALLOWED_ORIGINS` | string[] / localhost、127.0.0.1、testserver 默认集合 | 安全边界；只列可信浏览器 origin，不带路径 |
| `deployment.profile` | `MULTICLAW_DEPLOYMENT__PROFILE` | literal / `standalone`，仅允许该值 | 普通；当前不支持集群 profile |

`allowed_origins` 同时控制 CORS 与变更请求的 Origin/Referer 校验。生产环境必须替换本地默认集合，按实际 HTTPS origin 最小化配置。

## 数据库：`database`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `database.driver` | `MULTICLAW_DATABASE__DRIVER` | `sqlite` 或 `mysql` / `sqlite` | 部署相关 |
| `database.url` | `MULTICLAW_DATABASE__URL` | string / `sqlite+aiosqlite:///data/multiclaw.db` | 敏感；MySQL URL 通常含凭据，应由环境或 secret file 注入 |
| `database.migration_mode` | `MULTICLAW_DATABASE__MIGRATION_MODE` | literal / `validate`，仅允许该值 | 普通；应用只验证，不自动迁移 |
| `database.sqlite_busy_timeout_ms` | `MULTICLAW_DATABASE__SQLITE_BUSY_TIMEOUT_MS` | int / `5000` / `1..60000` | 部署相关；仅 SQLite |

driver 与 URL scheme 必须匹配：SQLite 使用 `sqlite+aiosqlite://`，MySQL 使用 `mysql+aiomysql://`。早期单文件路径写法只保留模型兼容读取能力；新配置统一使用 URL。

## 工作区与运行时：`workspace`、`runtime`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `workspace.root` | `MULTICLAW_WORKSPACE__ROOT` | string / `data/workspaces` | 部署相关；服务用户必须可创建/读写，父目录不应对其他用户开放写入 |
| `runtime.max_resident_tenants` | `MULTICLAW_RUNTIME__MAX_RESIDENT_TENANTS` | int / `32` / `1..1024` | 容量参数 |
| `runtime.idle_ttl_seconds` | `MULTICLAW_RUNTIME__IDLE_TTL_SECONDS` | int / `900` / `>=30` | 容量参数 |
| `runtime.max_concurrent_runs_per_tenant` | `MULTICLAW_RUNTIME__MAX_CONCURRENT_RUNS_PER_TENANT` | int / `2` / `1..32` | 租户配额 |
| `runtime.max_run_seconds` | `MULTICLAW_RUNTIME__MAX_RUN_SECONDS` | int / `1800` / `1..86400` | 每段活动执行的时间上限；审批等待不占用活动执行时间 |
| `runtime.max_run_tokens` | `MULTICLAW_RUNTIME__MAX_RUN_TOKENS` | int / `250000` / `>=1` | 每 run 累计输入和输出额度，恢复后继续累计 |
| `runtime.tenant_daily_token_limit` | `MULTICLAW_RUNTIME__TENANT_DAILY_TOKEN_LIMIT` | int / `0` / `>=0` | UTC 日额度；0 为不限制；会话删除不会返还额度 |
| `runtime.max_queued_messages` | `MULTICLAW_RUNTIME__MAX_QUEUED_MESSAGES` | int / `20` / `1..100` | 会话排队和待消费补充指令上限 |
| `runtime.max_stream_events` | `MULTICLAW_RUNTIME__MAX_STREAM_EVENTS` | int / `10000` / `100..100000` | 会话进度日志保留上限 |

`max_resident_tenants` 是进程内 resident runtime 数，不等于数据库租户数。达到上限且没有可安全回收的 idle runtime 时 API 返回 `503` 和 `Retry-After`。

## 工作流：`workflow`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `workflow.heartbeat_ms` | `MULTICLAW_WORKFLOW__HEARTBEAT_MS` | int / `5000` / `>=1000` | 恢复一致性参数 |
| `workflow.lease_ttl_ms` | `MULTICLAW_WORKFLOW__LEASE_TTL_MS` | int / `20000` / `>=5000` 且至少为 heartbeat 的 3 倍 | 恢复一致性参数 |
| `workflow.max_checkpoint_payload_bytes` | `MULTICLAW_WORKFLOW__MAX_CHECKPOINT_PAYLOAD_BYTES` | int / `262144` / `1024..1048576` | 存储与恢复参数 |

修改 heartbeat/lease 需要同时考虑数据库延迟、最长调度停顿和恢复扫描间隔。租约不是任务超时；过小值会制造错误 fence 丢失。

## Durable Plan：`planning`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `planning.enabled` | `MULTICLAW_PLANNING__ENABLED` | bool / `true` | 功能开关；关闭时显式 Plan 请求安全失败 |
| `planning.default_mode` | `MULTICLAW_PLANNING__DEFAULT_MODE` | `auto`、`always` 或 `never` / `auto` | 发布策略；示例部署保持 `never` 直到双后端门禁完成 |
| `planning.classification_model` | `MULTICLAW_PLANNING__CLASSIFICATION_MODEL` | string / 空 / 最长 255 | 模型路由；空值使用默认模型 |
| `planning.generation_model` | `MULTICLAW_PLANNING__GENERATION_MODEL` | string / 空 / 最长 255 | 模型路由；空值使用默认模型 |
| `planning.max_steps` | `MULTICLAW_PLANNING__MAX_STEPS` | strict int / `20` / `1..20` | 单个 Plan 的步骤上限 |
| `planning.max_dependency_depth` | `MULTICLAW_PLANNING__MAX_DEPENDENCY_DEPTH` | strict int / `10` / `1..10` | DAG 深度上限 |
| `planning.max_revisions` | `MULTICLAW_PLANNING__MAX_REVISIONS` | strict int / `5` / `0..20` | 不可变版本历史上限 |
| `planning.max_step_attempts` | `MULTICLAW_PLANNING__MAX_STEP_ATTEMPTS` | strict int / `2` / `1..20` | 每个步骤的尝试上限 |

Plan 的总 round 预算按 `max_steps * max_step_attempts * agent.max_tool_rounds` 计算；模型请求和新工具执行行在持久化边界预留预算，恢复时从数据库重算。Plan 运行同时计入 `runtime.max_concurrent_runs_per_tenant`，不会引入第二套并发配额。

## Secret 与删除：`secrets`、`deletion`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `secrets.allow_platform_fallback` | `MULTICLAW_SECRETS__ALLOW_PLATFORM_FALLBACK` | bool / `false` | 高风险；打开后允许租户缺少 Secret 时读取部署 provider key |
| `secrets.keyring_file` | `MULTICLAW_SECRETS__KEYRING_FILE` | string / 空 | 敏感路径；与 keyring B64 环境变量二选一 |
| `deletion.retention_days` | `MULTICLAW_DELETION__RETENTION_DAYS` | strict int / `7` / `0..30` | 数据生命周期；`0` 表示立即到期，仍由清除 worker 执行 |

### Secret keyring 来源

必须恰好选择一个来源：

1. `MULTICLAW_SECRETS_KEYRING_B64`：base64 编码的 UTF-8 JSON。
2. `secrets.keyring_file`：原始 JSON 文件；必须是普通文件、拒绝符号链接、权限不得包含 group/other 位（建议 `0600`）。

JSON 合约：

```json
{
  "active_key_version": 1,
  "keys": {
    "1": "<32-byte-key-in-base64>"
  }
}
```

顶层只能有这两个字段。版本是正整数，key 解码后必须恰好 32 字节，活动版本必须存在。轮换时先添加新版本并切换 active，完成数据库重加密并验证引用计数后才能移除旧版本。

## 模型路由：`llm`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `llm.default_provider` | `MULTICLAW_LLM__DEFAULT_PROVIDER` | string / `openai` | 普通；必须对应 providers 或租户 Secret provider |
| `llm.default_model` | `MULTICLAW_LLM__DEFAULT_MODEL` | string / `gpt-4o` | 普通 |
| `llm.providers` | `MULTICLAW_LLM__PROVIDERS` | map<string,map<string,string>> / `{}` | `base_url` 部署相关，`api_key` 敏感；整个映射可用 JSON |
| `llm.capability_tags` | `MULTICLAW_LLM__CAPABILITY_TAGS` | map<string,string[]> / `{}` | 普通；声明模型能力标签 |
| `llm.model_providers` | `MULTICLAW_LLM__MODEL_PROVIDERS` | map<string,string> / `{}` | 明确绑定模型与 provider；未绑定模型使用 default_provider |
| `llm.max_retries` | `MULTICLAW_LLM__MAX_RETRIES` | int / `2` / `0..5` | 瞬时上游失败的额外尝试次数 |
| `llm.retry_base_seconds` | `MULTICLAW_LLM__RETRY_BASE_SECONDS` | float / `0.25` / `0..30` | 指数退避起点 |
| `llm.request_timeout_seconds` | `MULTICLAW_LLM__REQUEST_TIMEOUT_SECONDS` | float / `60` / `0<value<=600` | 非流式请求超时 |
| `llm.stream_timeout_seconds` | `MULTICLAW_LLM__STREAM_TIMEOUT_SECONDS` | float / `300` / `0<value<=3600` | 流式 HTTP 超时，另受 run 时间上限限制 |
| `llm.token_prices` | `MULTICLAW_LLM__TOKEN_PRICES` | map<string,map<string,float>> / `{}` | 可选模型单价，按每百万 Token 的 input_per_million/output_per_million 估算金额 |

嵌套 provider 也可按路径覆盖，例如 `MULTICLAW_LLM__PROVIDERS__OPENAI__BASE_URL`。配置中的 provider API key 是部署级回退来源；租户 BYOK 应通过 `/api/secrets` 存储。默认 `allow_platform_fallback=false` 时两者不会静默混用。

自定义 provider 配置 `adapter="openai"` 或 `adapter="anthropic"`，并通过 `model_providers` 显式绑定模型。未知 provider/adapter 安全失败。只有 429、5xx 和连接/超时错误会重试；已向调用方交付流事件后不会自动重试，工具副作用也不会由模型 HTTP 重试重放。

用量优先采用 provider 报告；缺失时标记为估计。上下文和预留额度采用确定性估算，并非模型专用 tokenizer。金额只在配置单价后提供，缺失价格不会显示成零费用。部署方应按自己的账单配置价格。

## 上下文与记忆：`memory`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `memory.short_term_limit` | `MULTICLAW_MEMORY__SHORT_TERM_LIMIT` | int / `100` / 无额外模型边界 | 普通 |
| `memory.context_window_limit` | `MULTICLAW_MEMORY__CONTEXT_WINDOW_LIMIT` | int / `128000` / 无额外模型边界 | 模型容量参数 |
| `memory.recent_turns` | `MULTICLAW_MEMORY__RECENT_TURNS` | int / `2` / 无额外模型边界 | 上下文策略 |
| `memory.context_history_ratio` | `MULTICLAW_MEMORY__CONTEXT_HISTORY_RATIO` | float / `0.5` / 无额外模型边界 | 上下文策略 |
| `memory.include_legacy_memory_in_retrieval` | `MULTICLAW_MEMORY__INCLUDE_LEGACY_MEMORY_IN_RETRIEVAL` | bool / `false` | 兼容策略 |
| `memory.progressive_context_enabled` | `MULTICLAW_MEMORY__PROGRESSIVE_CONTEXT_ENABLED` | bool / `false` | 实验性策略 |
| `memory.context_response_reserve_tokens` | `MULTICLAW_MEMORY__CONTEXT_RESPONSE_RESERVE_TOKENS` | int / `4096` / `>=256` | 响应预算 |
| `memory.context_l1_ratio` | `MULTICLAW_MEMORY__CONTEXT_L1_RATIO` | float / `0.6` / `0<value<1` | 上下文预算 |

没有显式 Pydantic 边界的数值仍应保持正值和业务合理范围；配置模型不会替部署方推断模型真实 context window。

生产运行时在每次模型调用前统一计入消息、工具 schema 与响应预留，无论初始历史是否启用 progressive 策略。超限历史按完整工具交换压缩；目标、指令和最新用户输入保留。模型摘要失败或摘要请求本身过大时使用本地摘要。大工具输出保存到当前租户工作区 `.multiclaw/tool-results/`，模型获得有界预览及 `read_file` 路径。项目指令仅加载工作区内的 `AGENTS.md`、`CLAUDE.md` 和已访问目录的适用规则，不读取宿主机父目录或跟随符号链接。

## 治理与沙箱：`governance`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `governance.audit_enabled` | `MULTICLAW_GOVERNANCE__AUDIT_ENABLED` | bool / `true` | 安全审计；生产保持开启 |
| `governance.sandbox.mode` | `MULTICLAW_GOVERNANCE__SANDBOX__MODE` | `auto` 或 `host_unsafe_dev_only` / `auto` | 高风险；unsafe 模式要求 `app.debug=true` |
| `governance.sandbox.backend_probe_on_startup` | `MULTICLAW_GOVERNANCE__SANDBOX__BACKEND_PROBE_ON_STARTUP` | bool / `true` | 部署门禁 |
| `governance.sandbox.unsafe_fallback_requires_debug` | `MULTICLAW_GOVERNANCE__SANDBOX__UNSAFE_FALLBACK_REQUIRES_DEBUG` | literal `true` / `true` | 固定保护，不可关闭 |
| `governance.sandbox.write_protected_workspace_paths` | `MULTICLAW_GOVERNANCE__SANDBOX__WRITE_PROTECTED_WORKSPACE_PATHS` | string[] / `[".git"]` | 安全边界 |
| `governance.sandbox.read_hidden_workspace_paths` | `MULTICLAW_GOVERNANCE__SANDBOX__READ_HIDDEN_WORKSPACE_PATHS` | string[] / `[".env", ".env.*"]` | 安全边界 |
| `governance.sandbox.profiles.shell` | `MULTICLAW_GOVERNANCE__SANDBOX__PROFILES__SHELL` | string / `shell_workspace` | profile 名 |
| `governance.sandbox.profiles.code_exec` | `MULTICLAW_GOVERNANCE__SANDBOX__PROFILES__CODE_EXEC` | string / `code_exec_python` | profile 名 |
| `governance.sandbox.profiles.mcp_stdio` | `MULTICLAW_GOVERNANCE__SANDBOX__PROFILES__MCP_STDIO` | string / `mcp_stdio_local` | profile 名 |
| `governance.sandbox.macos.seatbelt_profile_dir` | `MULTICLAW_GOVERNANCE__SANDBOX__MACOS__SEATBELT_PROFILE_DIR` | string / 空 | 部署相关；自定义 Seatbelt profile 目录 |
| `governance.sandbox.linux.nsjail_path` | `MULTICLAW_GOVERNANCE__SANDBOX__LINUX__NSJAIL_PATH` | string / `/usr/bin/nsjail` | 部署相关 |
| `governance.sandbox.linux.nsjail_config_dir` | `MULTICLAW_GOVERNANCE__SANDBOX__LINUX__NSJAIL_CONFIG_DIR` | string / 空 | 部署相关；自定义 nsjail config 目录 |

`host_unsafe_dev_only` 在宿主机直接执行本应隔离的能力，只能用于隔离开发机上的明确调试。生产必须保留 `auto` 并满足[沙箱部署](sandbox-deployment.md)的原生后端要求。

## 工具与 Agent：`tools`、`agent`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `tools.parallel_read_only_enabled` | `MULTICLAW_TOOLS__PARALLEL_READ_ONLY_ENABLED` | bool / `false` | 调度策略；只影响符合只读条件的工具 |
| `tools.parallel_max_concurrency` | `MULTICLAW_TOOLS__PARALLEL_MAX_CONCURRENCY` | int / `4` / `1..16` | 容量与上游限流参数 |
| `tools.web_fetch_allow_private_networks` | `MULTICLAW_TOOLS__WEB_FETCH_ALLOW_PRIVATE_NETWORKS` | bool / `false` | 高风险 SSRF 边界；生产通常保持 false |
| `agent.max_tool_rounds` | `MULTICLAW_AGENT__MAX_TOOL_ROUNDS` | int / `10` / 无额外模型边界 | 运行成本限制 |
| `agent.resilience_enabled` | `MULTICLAW_AGENT__RESILIENCE_ENABLED` | bool / `false` | 行为策略 |
| `agent.no_progress_repeat_limit` | `MULTICLAW_AGENT__NO_PROGRESS_REPEAT_LIMIT` | int / `3` / `2..10` | 韧性策略 |
| `agent.reflection_max_attempts` | `MULTICLAW_AGENT__REFLECTION_MAX_ATTEMPTS` | int / `1` / `0..3` | 韧性策略 |
| `agent.subagents_enabled` | `MULTICLAW_AGENT__SUBAGENTS_ENABLED` | bool / `false` | 启用只读 `delegate_tasks`；默认关闭以避免额外模型调用 |
| `agent.subagent_max_tasks` | `MULTICLAW_AGENT__SUBAGENT_MAX_TASKS` | int / `3` / `1..3` | 每次委派的子任务上限；子任务并行执行 |
| `agent.subagent_max_rounds` | `MULTICLAW_AGENT__SUBAGENT_MAX_ROUNDS` | int / `3` / `1..10` | 每个子任务的工具轮次上限；必要时另有一次无工具总结调用 |
| `agent.subagent_max_tokens` | `MULTICLAW_AGENT__SUBAGENT_MAX_TOKENS` | int / `20000` / `1024..100000` | 每个子任务的估算 Token 上限，仍受父 Run 总额度约束 |
| `agent.subagent_timeout_seconds` | `MULTICLAW_AGENT__SUBAGENT_TIMEOUT_SECONDS` | int / `180` / `1..1800` | 单次委派工具最长执行时间，仍受父 Run 截止时间约束 |
| `agent.system_prompt` | `MULTICLAW_AGENT__SYSTEM_PROMPT` | string / 代码内置默认 prompt | 安全敏感行为输入；变更需评估工具与数据泄露风险 |

只读并行不允许把变更工具、需要审批的工具或同 run 恢复序列并行化。`web_fetch_allow_private_networks=true` 会放开内网目标，必须由独立网络边界和审计支撑。

启用子 Agent 后，`delegate_tasks` 只向子任务开放内置 `read_file`、`glob`、`grep`、`list_dir` 和 `find_dir`；不开放网络、写入、Shell、代码执行、MCP 或再次委派。子任务可选默认模型或 `llm.model_providers` 中显式配置的模型。子任务继承父 Run 的租户作用域、取消和总 Token 额度，结果按输入顺序返回。崩溃恢复可重新执行这一只读委派，可能重复消耗模型 Token；子任务尚无独立持久化 Run。

## 协作：collaboration

独立子任务与 Agent Team 使用下面的 `collaboration` 开关；上述 `delegate_tasks` 是旧的只读工具调用。

| 字段 | 默认值 / 范围 | 用途 |
|---|---|---|
| `collaboration.enabled` | `false` | 开启独立子 Agent 与团队 |
| `collaboration.max_workers` | `3` / `1..8` | 后台成员并发槽位，仍受租户 Run 配额限制 |
| `collaboration.max_jobs_per_session` | `100` / `1..500` | 会话累计分派上限 |
| `collaboration.max_job_tokens` | `20000` / `1024..100000` | 每个分派的 Token 分配与上限 |
| `collaboration.max_job_seconds` | `300` / `1..1800` | 每段活动执行最长时间 |
| `collaboration.max_job_rounds` | `10` / `1..50` | 成员工具轮次上限 |
| `collaboration.max_team_tokens` | `250000` / `20000..1000000` | 团队累计分派 Token 分配上限 |

对应环境变量使用 `MULTICLAW_COLLABORATION__` 加大写字段名。行为、审批与 Git 写入边界见[协作指南](collaboration.md)。日志目录可用 `MULTICLAW_LOG_DIR` 指定，默认在用户的 `.multiclaw/logs`。

## Skills：`skills`

TOML 分组名使用复数 `skills`，但 Settings 字段名和环境变量路径使用单数 `skill`。这是当前加载契约，不能把两种形式互换。

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `skills.enabled` | `MULTICLAW_SKILL__ENABLED` | bool / `true` | 功能开关 |
| `skills.max_active` | `MULTICLAW_SKILL__MAX_ACTIVE` | int / `5` / 无额外模型边界 | 运行容量 |
| `skills.extra_dirs` | `MULTICLAW_SKILL__EXTRA_DIRS` | string[] / `[]` | 信任边界；目录内容可影响 Agent 行为 |
| `skills.user_dir` | `MULTICLAW_SKILL__USER_DIR` | string / 空 | 信任边界；用户 skill 根目录 |

额外 skill 目录必须由可信运维方控制，不能指向租户可任意写入的共享位置。

## 认证：`auth`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `auth.jwt_signing_key_file` | `MULTICLAW_AUTH__JWT_SIGNING_KEY_FILE` | string / 空 | 敏感路径；与直接密钥环境变量二选一 |

JWT 签名密钥必须恰好选择一个来源：

1. `MULTICLAW_AUTH_JWT_SIGNING_KEY`：原始字符串，UTF-8 编码后至少 32 字节。
2. `auth.jwt_signing_key_file`：原始 key bytes 文件；必须是普通文件、拒绝符号链接、权限不得包含 group/other 位，建议 `0600`。

密钥用于会话 JWT、删除恢复 JWT 和验证码摘要派生。无计划轮换会使现有会话失效；不要每次启动重新生成生产密钥。

## 邮件提供商：`email`、`brevo`、`resend`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `email.provider` | `MULTICLAW_EMAIL__PROVIDER` | `brevo` 或 `resend` / `brevo` | 选择活动提供商 |
| `brevo.api_key` | `MULTICLAW_BREVO__API_KEY` | string / 空 | 敏感；生产从 secret 环境注入 |
| `brevo.sender_email` | `MULTICLAW_BREVO__SENDER_EMAIL` | string / 空 | 部署相关；需在提供商验证 |
| `brevo.sender_name` | `MULTICLAW_BREVO__SENDER_NAME` | string / `MultiClaw` | 普通 |
| `brevo.mock` | `MULTICLAW_BREVO__MOCK` | bool / `false` | 仅开发；只跳过发送，不显示验证码 |
| `resend.api_key` | `MULTICLAW_RESEND__API_KEY` | string / 空 | 敏感；生产从 secret 环境注入 |
| `resend.sender_email` | `MULTICLAW_RESEND__SENDER_EMAIL` | string / 空 | 部署相关；需在提供商验证 |
| `resend.sender_name` | `MULTICLAW_RESEND__SENDER_NAME` | string / `MultiClaw` | 普通 |
| `resend.mock` | `MULTICLAW_RESEND__MOCK` | bool / `false` | 仅开发；只跳过发送，不显示验证码 |

只有活动 provider 的 mock 开关生效。mock 模式仍会创建验证码摘要和消耗发送配额，但不发送、返回或记录验证码，因此不能完成交互式登录。真实 provider 失败时发送接口返回 `502`，并尽力删除本次预留验证码。

## MCP：`mcp`

| TOML key | 环境变量 | 类型 / 默认值 / 约束 | 分类与说明 |
|---|---|---|---|
| `mcp.enabled` | `MULTICLAW_MCP__ENABLED` | bool / `true` | 功能开关 |
| `mcp.config_path` | `MULTICLAW_MCP__CONFIG_PATH` | string / 空 | 信任边界；外部配置路径 |

MCP 配置可启动本地进程或连接远程服务，必须视为代码和网络权限配置。stdio server 还受原生沙箱与最小授权控制；不要把租户可写配置直接提升为部署级 MCP 配置。

## 最小生产检查清单

- 用独立 secret 管理渠道提供数据库凭据、JWT key、keyring 和邮件 API key。
- `app.debug=false`、sandbox `mode=auto`、audit 开启、private network fetch 关闭。
- `allowed_origins` 只包含真实 HTTPS 前端 origin。
- 数据库 driver/URL 匹配，并在启动前执行 `db upgrade` 与 `db check`。
- keyring 包含数据库所有在用版本，工作区根目录和 key 文件权限通过 readiness。
- 不在 TOML、Git、日志或工单中保存真实凭据。
