# 安全模型

本文描述 MultiClaw `0.1.0` 已实现的信任边界、不变量和已知限制。漏洞报告流程见 [SECURITY.md](../SECURITY.md)；这里不是合规认证或绝对安全保证。

## 资产

- 租户身份、邮箱、默认工作区和账号删除状态。
- 会话消息、memory、run、execution、checkpoint 与 approval。
- 租户 BYOK Secret 明文、ciphertext、key metadata 和部署 keyring。
- 租户工作区文件、MCP 配置和工具输出。
- JWT 签名 key、邮件 provider key、数据库凭据与平台 LLM key。
- 审计事件、日志、健康状态和备份。

## 信任边界

**可信运维输入：** release artifact、TOML、环境变量、JWT/keyring 文件、数据库管理员、operator-managed MCP 配置、原生沙箱 profile 和 reverse proxy。

**不可信输入：** tenant/user HTTP 请求、邮箱地址与验证码尝试、会话消息、模型输出、工具参数/输出、租户工作区文件、workspace-untrusted MCP 配置、远程 MCP 响应和上游 provider 错误。

模型输出不是授权来源。Agent 请求执行工具时仍经过参数校验、permission/approval、作用域、Secret 解析和 sandbox 边界。

## 认证与会话

- 邮箱验证码只保存带用途和 email 的 HMAC 摘要，15 分钟过期；login 与 deletion recovery 隔离。
- 每 email/用途每天最多发码 3 次；最新 code 累计 5 次失败后锁定，不能回退到旧 code。
- 登录 JWT 使用 HS256、固定 audience、10 天 expiry 和 `auth_epoch`。每次请求回查当前用户；删除/撤销状态或 epoch 变化会使旧 cookie 失效。
- `token` 是 HttpOnly、SameSite=Lax；生产 hostname 标记 Secure。允许 origin 不得包含通配符。
- 需要近期认证的删除 session、Secret 变更和账号删除，要求 JWT `iat` 距请求时间不超过 5 分钟。

JWT signing key 同时影响 session、recovery token 和验证码摘要，必须至少 32 字节并稳定保存。环境与文件来源必须恰好一个。

## CSRF 与浏览器边界

所有非 GET/HEAD/OPTIONS 请求都必须通过：

1. allowlist 中的 Origin 或 Referer origin；
2. `csrf_token` cookie；
3. 相同的 `X-CSRF-Token` header。

比较使用 constant-time token match。CORS 只对允许 origin 返回 credential headers。CSRF 防护不替代 XSS 防护；前端仍应避免执行模型或工具提供的任意 HTML/脚本。

## 多租户隔离

认证用户 ID 作为 `tenant_id`，服务端读取其 active `default_workspace_id`；客户端不能通过提交 tenant/workspace 字段改变授权作用域。

[`TenantContext`](../src/multiclaw/tenancy/context.py) 从 tenant/workspace 逐级收窄到 session/run。Session、memory、workflow、approval、Secret 和 deletion 仓储都将作用域列加入查询/更新条件；数据库 schema 还使用复合外键和唯一约束维持父子关系。

跨租户资源通常返回 `404`，减少存在性泄露。每次 run 事件以完整四元组精确路由，不允许通配符。隔离仍依赖所有新仓储和 API 使用 UoW/Context；绕过仓储执行未作用域 SQL 会破坏该不变量，因此属于安全敏感改动。

## Secret 加密与解析

租户 Secret 使用 AES-256-GCM envelope：

- 随机 12 字节 nonce；
- 版本化 32 字节部署 key；
- 固定 AAD 前缀和长度编码字段；
- AAD 绑定 tenant、workspace、secret ID、provider kind/name、secret name、key provider/version、格式和算法。

复制 ciphertext 到另一个租户、名称或 key version 会使认证标签校验失败。API 只返回 masked metadata，不返回明文；日志/事件/工具输入通过递归 redaction 和公开错误清洗。

默认 `secrets.allow_platform_fallback=false`。租户缺少 BYOK 时 fail closed，不会静默使用部署 provider key。显式打开回退会改变租户计费和数据边界，应视为高风险部署决策。

解密后的 bytearray 在 reveal 结束时清零，runtime 关闭也关闭 Secret handles。这是缩短驻留时间的 best-effort，不能保证解释器、依赖库或操作系统副本已经物理擦除。

## Keyring 与轮换

keyring 环境变量是 base64 JSON；文件来源必须拒绝符号链接并限制为 owner-only。活动 key 和所有仍被数据库引用的旧 key 必须同时存在，否则 readiness 返回 `keyring` 失败。

轮换顺序是先加新 key、切 active、批量 CAS 重加密、确认引用计数归零、最后删旧 key。当前没有产品化 CLI；受控一次性维护脚本必须直接使用 `SecretRotationService`，每批记录 rotated/skipped/failed，并在失败时保留旧 key。

## 事件、日志与脱敏

[`EventRouter`](../src/multiclaw/events/router.py) 只向精确 `(tenant, workspace, session, run)` 订阅者投递，且为每个 handler 深拷贝 payload。SSE 编码前再执行 redaction。

安全日志和 audit 不应包含 Secret、Authorization、邮箱、完整路径或模型返回的 provider 错误细节。脱敏降低意外暴露，但不是将任意敏感对象安全写日志的许可；新数据结构仍需增加针对性测试。

## 工具、审批与恢复

- 工具注册由 runtime、permission checker 与 sandbox readiness 共同决定。
- 变更或高风险操作创建持久化 approval，客户端以当前 version 做 CAS 决策；过期、重复或外作用域决策失败。
- run lease owner、fencing token、version 与 expiry 阻止过期 owner 写入。
- checkpoint hash/兼容性异常 fail closed；结果不确定的非幂等工具不会自动重试。
- 恢复事件和工具参数对外前脱敏；审批 payload 读取同样脱敏。

可恢复工作流减少重复副作用，但无法替第三方系统提供事务。工具作者必须正确声明幂等/恢复策略，并在 uncertain 场景提供人工处置。

## MCP 信任

operator-managed MCP 配置是可信部署输入，但仍必须最小化网络、工作区、子进程、环境和只读路径授权。`workspace_untrusted` 配置永不自动连接，无论是 stdio 还是远程 transport；需要启用的 server 必须迁移到运维控制位置并经过审阅。

workspace-untrusted 配置禁止 `${...}` 展开。可信 stdio 配置只有在 environment key 与 allowlist 精确对应时才能注入 secret。远程 MCP 响应、工具 schema 和错误仍是不可信数据。

## 原生沙箱

生产 `mode=auto` 根据平台选择 macOS Seatbelt 或 Linux nsjail。probe 验证允许执行以及拒绝工作区外写、网络、`.env` 读取、`.git` 写入和不允许的 child creation。未通过的本地执行能力不会自动回退到宿主机。

shell、code_exec 和 stdio MCP 使用不同 profile；workspace/network/subprocess/env/read-only-path grant 必须显式最小化。runner 对 stdout/stderr 各限制 128 KiB，超限时终止原 process group、丢弃两路输出并返回 `output_limit_exceeded`。

`host_unsafe_dev_only` 只允许与 debug 组合，但它仍在宿主执行代码；配置模型限制不是生产隔离，生产不得启用。

## 删除与恢复

账号删除先检查活动 run，再将用户置为 `pending_purge`、撤销 runtime、提高授权边界并创建延迟 job。保留窗口内只允许独立 recovery token 查询/恢复当前 job；token 绑定 job、用途、expiry 和用户状态。

到期清除由带 worker lease/fencing 的批处理执行，遵循显式删除顺序。保留天数为 0 仍是异步 worker 路径。备份中的数据不由在线 purge 自动删除，备份保留与销毁必须由部署政策单独覆盖。

## 已知限制与接受风险

- **公开 readiness 不覆盖沙箱：** `/api/health/ready` 当前检查数据库/schema/workspace/keyring，但不读取 `sandbox_readiness`。部署必须单独执行原生 gate 并审查 probe；ready=200 不能证明本地工具已安全可用。
- **macOS breakaway child：** runner 能 TERM/KILL 原 process group，但不保证清理通过 `setsid`、`setpgid` 或 double-fork 脱离 PGID 的恶意/异常子进程。它仍继承 Seatbelt profile，因此主要残余风险是资源占用与已授权路径/网络持续访问。超时后需运维监控残留进程。
- **Linux 原生证据：** 本次文档交付环境没有真实 nsjail 服务，不能声称已执行 Linux native gate；部署方必须在目标宿主运行测试。
- **单进程：** EventRouter 和 RuntimePool 在内存中，不提供集群隔离/协调保证。
- **默认工作区：** 一个 active 用户当前只使用一个默认工作区，没有 UI/权限模型支持切换。
- **密钥服务：** keyring 是部署文件/环境输入，没有 KMS/Vault 的审计、自动轮换或硬件边界。

这些限制不得通过扩大 MCP grant、关闭 audit、启用 unsafe sandbox 或绕过 readiness 来“修复”。
