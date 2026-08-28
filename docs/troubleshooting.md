# 故障排查

按“症状 / 常见原因 / 检查 / 处理”定位问题。生产环境不要通过关闭迁移验证、CSRF、keyring 校验或原生沙箱来绕过故障。

## `db check` 失败或旧 SQLite 文件无法启动

### 症状

`uv run multiclaw db check` 退出 1，或 readiness 返回 `schema_revision` / `schema_integrity`。

### 常见原因

数据库不存在、未执行 Alembic、revision 不是当前 head，或复用了开发早期创建但没有 Alembic revision 的 SQLite 文件。

### 检查

确认 `MULTICLAW_DATABASE__URL` 指向预期文件，运行 `uv run multiclaw db current`，并在备份后检查是否存在 `alembic_version`。

### 处理

开发阶段优先使用新的显式 SQLite 文件并执行 `db upgrade`。生产先做一致性备份与恢复演练，再按发布流程升级；不要手工伪造 revision 或让应用启动时自动迁移。

## 数据库 driver 与 URL 不匹配

### 症状

Settings 启动时报 `database.driver must match database.url`，或 Alembic 拒绝 URL。

### 常见原因

driver 设为 `mysql` 却提供 SQLite scheme，反之亦然；环境变量覆盖了 TOML 的一半配置。

### 检查

同时查看 `MULTICLAW_DATABASE__DRIVER`、`MULTICLAW_DATABASE__URL` 和实际进程环境，确认没有 supervisor 的旧值。

### 处理

SQLite 使用 `sqlite+aiosqlite`，MySQL 使用 `mysql+aiomysql`，两项作为同一变更发布。修改后重新运行 `db upgrade`、`db check`。

## readiness 返回 503

### 症状

`/api/health/live` 为 200，但 `/api/health/ready` 为 503，并返回 `checks_failed`。

### 常见原因

- `db_connectivity` / `backend_version`：连接或版本不支持。
- `schema_revision` / `schema_integrity`：迁移或表/外键不一致。
- `sqlite_foreign_keys`：SQLite connection 未启用外键。
- `workspace_root_permissions`：目录不存在或服务用户缺少 rwx。
- `keyring`：来源、格式、权限或引用版本错误。
- `active_default_workspace_integrity`：active 用户缺少有效默认工作区。
- `mysql_time_zone`、`mysql_isolation`、`mysql_innodb`、`mysql_charset`：MySQL session/schema 不满足契约。

### 检查

读取 `checks_failed`，结合 `~/.multiclaw/logs/multiclaw.log`，在同一进程环境运行 `multiclaw db check`。MySQL 还要检查版本、session time zone/isolation、table engine 与 charset。

### 处理

修复对应外部条件并重新探测。不要把 live 代替 ready，不要删除检查代码，也不要在 readiness handler 内触发迁移。

## JWT 或 keyring 来源缺失/冲突

### 症状

启动报 signing key/keyring “requires exactly one configured source”，JWT key 太短，或 ready 的 `keyring` 失败。

### 常见原因

环境变量与文件来源同时设置、同时为空；JWT 少于 32 字节；keyring 不是合法 base64 JSON、活动版本缺失、key 不是 32 字节，或文件权限过宽/为符号链接。

### 检查

在 supervisor 的真实环境核对四个来源名；检查文件是普通文件且 mode 建议 `0600`；比对数据库引用 key version 与 keyring 版本集合。不要输出 raw key。

### 处理

每种密钥保留恰好一个来源。恢复缺失旧 key version 后再启动；不要用全新随机 key 覆盖生产 keyring 或签名 key。

## 邮件验证码未送达

### 症状

发送接口看似成功但邮箱没有邮件，或接口返回 429/502，无法完成登录。

### 常见原因

活动 provider 处于 mock、选错 provider、API key/发件域未配置、上游拒绝、每日发送限制达到 3 次，或邮件进入垃圾箱。mock 不发送也不显示验证码。

### 检查

核对 `email.provider` 与同一 provider 的 `mock`、`api_key`、`sender_email`；查看不含凭据的 provider 状态和服务日志；确认请求 email 标准化后对应预期邮箱。

### 处理

本地只做健康验证可以保留 mock；交互式登录必须配置真实 Brevo/Resend 并关闭活动 provider mock。429 需等待窗口，不要直接改数据库配额；502 修复 provider 后重新请求，失败发送预留记录会尽力回滚。

## 变更请求返回 CSRF 403

### 症状

GET 正常，POST/PUT/PATCH/DELETE 返回 `CSRF validation failed`。

### 常见原因

缺少/不可信 Origin，未先获取 CSRF cookie，header 与 cookie 不一致，fetch 未携带 credentials，或生产 origin 不在 allowlist。

### 检查

浏览器 Network 中核对 `Origin`、`csrf_token` cookie、`X-CSRF-Token` header 和 cookie credentials；检查 `app.allowed_origins` 是否为 scheme+host+port 的精确 origin。

### 处理

先调用 `/api/auth/csrf`，使用返回 token 发双提交请求；修正 allowlist 和 HTTPS cookie 配置。不要关闭中间件或把 `*` 加入 origin。

## 会话为空或疑似跨租户不可见

### 症状

`/api/sessions` 为空，已知 ID 返回 404，或登录另一个账号后看不到原账号数据。

### 常见原因

使用了不同用户/数据库、session 已归档、默认工作区变化，或资源属于另一个 tenant。跨租户 404 是设计行为。

### 检查

调用 `/api/auth/me` 确认用户，检查 `include_archived=true`，确认服务实际数据库 URL。运维诊断只比较脱敏 ID 和作用域列，不把其他租户数据返回给调用方。

### 处理

切换到正确账号/数据库或恢复 archived session。不要放宽仓储 scope、直接修改 tenant ID 或用全局查询补偿 UI。

## SSE 中断或审批一直等待

### 症状

聊天流提前断开、页面停止输出，run 保持等待，或审批按钮发生 409/410。

### 常见原因

代理缓冲/超时、客户端断网、runtime 暂不可用、lease fence 丢失；审批 version 过期、已被其他请求解决或已经超时。

### 检查

确认 reverse proxy 对 `/api/chat` 不缓冲 SSE；记录流中的 `data-run` ID；重新读取 session messages、pending approvals 和 approval 当前 version；查看 stale fence/runtime 指标。

### 处理

以数据库状态为准恢复 UI。使用最新 approval version 重新决策；410 不能复用。不要把断流直接标记 completed，也不要自动重试结果不确定的非幂等工具。

## MCP 配置没有自动连接

### 症状

配置存在但 runtime 中没有对应 MCP 工具，启动事件显示 registration skipped。

### 常见原因

`mcp.enabled=false`、路径错误、配置被标记 `workspace_untrusted`、stdio sandbox profile 不可用，或请求了未授权 network/workspace/subprocess/env grant。

### 检查

核对 `mcp.config_path` 的信任来源、startup audit、sandbox probe 和 server 的 `config_trust`。workspace-untrusted 配置无论 transport 都不会自动连接。

### 处理

把需要启用的配置移到 operator-managed 位置，逐项授予最小权限并修复原生沙箱。不要通过改成 unsafe mode、允许全部环境变量或扩大工作区写权限解决。

## 沙箱能力不可用

### 症状

服务可启动且公开 ready 可能为 200，但 shell、code_exec 或 stdio MCP 未注册，startup readiness/audit 显示 probe/profile 不健康。

### 常见原因

Seatbelt/nsjail 不存在或不可执行、嵌套沙箱干扰、profile 目录错误、Linux namespace 能力不足，或 startup probe 的 deny/allow 矩阵失败。

### 检查

在真实目标宿主运行[原生测试](sandbox-deployment.md#原生测试命令)，查看 `app.state.sandbox_readiness` 对应启动日志/审计与 skipped capabilities。公开 health 当前不包含该状态。

### 处理

修复宿主原生后端和 profile，重启并重跑 native gate。生产不得启用 `host_unsafe_dev_only`；probe 不健康还会阻止账号清除 worker 启动，应立即告警。

## 前端代理、端口或静态资源异常

### 症状

Vite 页面请求 404/网络错误，5173/15800 端口冲突，或生产页面引用不存在的哈希资源。

### 常见原因

后端没有监听 127.0.0.1:15800、`start.sh` 杀掉了既有进程、Vite proxy 路径被改动、bundle 未构建或 HTML 与 assets 来自不同 release。

### 检查

分别访问后端 live、Vite 页面和 `/assets`；检查 `frontend/vite.config.ts`、进程端口和 `git status`。生产检查 HTML 引用的每个哈希资源。

### 处理

开发分别启动两个终端并保留 `/api` proxy；生产重新从同一 commit 执行 `npm ci && npm run build` 并整体部署 static。不要手改哈希文件；共享端口不要使用 `start.sh`。

## 清除任务重试或停滞

### 症状

账号超过 `purge_after` 仍未清除，`multiclaw_purge_retry_total` 增长，job attempt/last_error 更新。

### 常见原因

startup sandbox readiness 不健康导致 deletion worker 未启动、worker lease/heartbeat 异常、仍有阻塞活动、数据库锁或工作区删除失败。

### 检查

核对 deletion job 的 status、worker_id、lease_expires_at、heartbeat_at、attempt_count、last_error；检查 sandbox startup readiness、数据库与工作区权限。

### 处理

先恢复安全 worker 条件，让带 fencing 的 worker 重试。不要手工级联删除租户表、跳过顺序或把 retention 改为负值；需要人工维护时使用受审阅、可恢复的运维路径。

## Secret 轮换失败

### 症状

rotation batch 的 failed/skipped 非零，或移除旧 key 后 ready 失败、租户 Secret 无法解密。

### 常见原因

CAS 冲突、ciphertext/AAD 损坏、新 keyring 不完整、旧 key 过早删除，或维护脚本没有保留批次结果。

### 检查

停止轮换，记录脱敏的 rotated/skipped/failed；统计数据库 key version 引用，确认新旧 key 都在 keyring；对失败记录只检查 metadata，不输出明文。

### 处理

恢复所有仍被引用的旧 key，修复脚本或冲突后按批次继续。只有引用计数为零并完成备份恢复验证后才能删除旧版本。
