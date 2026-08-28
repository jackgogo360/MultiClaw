# API 概览

本页记录 MultiClaw `0.1.0` 的公开 HTTP 路由、认证边界和流式语义。运行服务后，以 `/docs` 和 `/openapi.json` 中的当前 schema 为请求/响应字段事实源；这里不复制全部 Pydantic schema。

## 基础地址与版本状态

开发后端默认地址为 `http://127.0.0.1:15800`。业务路由位于 `/api`，认证 router 同时暴露两个等价入口：

- `/auth/*`：直接访问后端时可用。
- `/api/auth/*`：同一个 router 的 `/api` 别名，也是前端和 Vite proxy 使用的路径。

新前端集成应使用 `/api/auth/*` 和其他 `/api/*` 路由。当前没有独立 URL 版本前缀；项目尚未正式发布，升级前应检查 changelog 和 OpenAPI diff。

## 认证 cookie 与 CSRF

登录成功后服务设置：

- `token`：HttpOnly、SameSite=Lax、path `/` 的 HS256 JWT，会话 TTL 为 10 天。JWT 绑定 `sub`、email、audience 和 `auth_epoch`；服务端用户状态或 epoch 变化会使旧 token 失效。
- `csrf_token`：可由前端读取、SameSite=Lax、path `/`。所有 `POST`、`PUT`、`PATCH`、`DELETE` 必须把相同值放入 `X-CSRF-Token` header。

除 localhost、127.0.0.1 和 testserver 外，cookie 按请求 hostname 标为 Secure。生产必须通过 HTTPS，并把真实前端 origin 配入 `app.allowed_origins`。

所有非 GET/HEAD/OPTIONS 请求，无论是否为公开认证路由，都需要：

1. `Origin`，或能提取 origin 的 `Referer`；
2. origin 在 allowlist 中；
3. CSRF cookie 与 header 同时存在并以 constant-time 比较相等。

失败统一返回 `403 {"detail":"CSRF validation failed"}`。跨 origin credential 请求还需要浏览器携带 cookie；服务端只为 allowlist origin 返回 credential CORS headers。

## 认证：`/auth` 与 `/api/auth`

下表中的路径同时存在 `/auth/...` 和 `/api/auth/...` 形态。

| 方法与相对路径 | 认证 | 语义 |
|---|---|---|
| `GET /csrf` | 公开 | 生成 CSRF token，设置 `csrf_token` cookie，并返回相同 token |
| `POST /send-code` | 公开 + CSRF | 为标准化 email 创建 15 分钟登录验证码；每 email/用途每天最多 3 次 |
| `POST /verify` | 公开 + CSRF | 消费最新六位登录码；最多 5 次失败后锁定该码；首次成功创建用户和默认工作区并设置会话 cookie |
| `POST /deletion-recovery/send-code` | 公开 + CSRF | 仅为仍处于有效删除恢复窗口的账号发送独立用途验证码 |
| `POST /deletion-recovery/verify` | 公开 + CSRF | 消费删除恢复码，签发只绑定当前 deletion job 的短期 recovery cookie |
| `POST /logout` | 公开 + CSRF | 删除会话 cookie 并轮换 CSRF token |
| `GET /me` | 公开 | 有效会话返回 `email`/`user_id`；匿名返回空字段，不以 401 区分 |

mock 邮件模式只跳过 provider 调用，不返回验证码，因此适合启动验证、不适合交互式登录。验证码按 `login` 与 `deletion_recovery` 用途隔离，不能跨用途使用。

## 会话：`/api/sessions`

全部路由需要有效会话，并自动限制到登录用户的默认工作区。

| 方法与路径 | 额外要求 | 语义 |
|---|---|---|
| `GET /api/sessions?include_archived=false` | 无 | 列出当前作用域会话；默认不含 archived |
| `POST /api/sessions` | CSRF | 创建会话；body 可含 `title`，默认 `New Chat` |
| `PATCH /api/sessions/{session_id}` | CSRF | 重命名当前作用域会话 |
| `POST /api/sessions/{session_id}/archive` | CSRF | 归档会话 |
| `POST /api/sessions/{session_id}/restore` | CSRF | 恢复已归档会话 |
| `DELETE /api/sessions/{session_id}` | CSRF + 5 分钟内近期认证 | 删除会话及其关联数据 |
| `GET /api/sessions/{session_id}/messages?limit=50` | 无 | 读取持久化消息 |
| `GET /api/sessions/{session_id}/pending-approvals` | 无 | 读取当前会话等待中的审批；工具输入已脱敏 |

未知或外租户 session 统一表现为 `404 session not found`。对 archived session 发起新 chat 返回 `409`。

## 聊天与 run 流：`/api/chat`

`POST /api/chat` 需要会话、CSRF，响应为 `text/event-stream`。请求可以提供：

- `message`，或 assistant-ui `messages` 中最后一条用户文本；
- `session_id`，兼容字段 `id`；两者都不提供时创建新 session。

服务在返回流之前完成 session 作用域检查、用户消息持久化、runtime acquire、run lease 与初始 checkpoint 写入。并发超过租户 run 配额返回 `429`；runtime 容量或可用性不足返回 `503`，可能带 `Retry-After`。

每个成功流的控制顺序以 [`DataStreamEncoder`](../src/multiclaw/stream.py) 为准：

1. `start`。
2. transient `data-session`，包含规范化 session payload。
3. transient `data-run`，包含 `session_id` 和新 `run_id`。客户端必须以这个 run ID 关联后续审批与诊断。
4. `start-step`，随后是文本、推理或工具 chunk。

后续事件可能包括：

- `text-start` / `text-delta` / `text-end`；
- `reasoning-start` / `reasoning-delta` / `reasoning-end`；
- `tool-input-available`、`tool-output-available`、`tool-output-error`；
- `tool-approval-request`；
- transient `data-event`，包含精确 tenant/workspace/session/run 作用域和已脱敏数据；
- `finish-step` 与 `finish`，或 `error`。

等待审批时 `finishReason` 为 `tool-calls`，run 持久化为 `AWAITING_USER`，并不等于 run 完成。正常结束使用 `stop`。客户端断开后应重新读取 session pending approvals 和持久化消息，不应凭本地 UI 推断数据库终态。

## 审批：`/api/approvals`

| 方法与路径 | 要求 | 语义 |
|---|---|---|
| `GET /api/approvals/{approval_id}` | 会话 | 在当前 tenant/workspace 内读取 approval 状态、version、expiry |
| `POST /api/approvals/{approval_id}/decision` | 会话 + CSRF | body 为 `approved` 与当前 `version`；以 CAS 决策 |
| `POST /api/approve` | 会话 + CSRF | 兼容别名，body 还包含 `approval_id`；不进入 OpenAPI，新集成不要使用 |

外租户/不存在返回 `404`，已解决或 version 冲突返回 `409`，过期返回 `410`。客户端必须使用最近读取的 version，不能在冲突后盲目重试旧决策。

## Secret：`/api/secrets`

`provider` path 参数不含冒号时视为 LLM provider，例如 `openai` → kind `llm`、name `openai`；也可以使用 `{kind}:{providerName}`。

| 方法与路径 | 额外要求 | 语义 |
|---|---|---|
| `GET /api/secrets` | 无 | 返回当前租户 metadata：provider kind/name、secret name、masked value、更新时间 |
| `PUT /api/secrets/{provider}/{name}` | CSRF + 近期认证 | body `value`；服务端 envelope 加密后 upsert，仅返回 metadata |
| `DELETE /api/secrets/{provider}/{name}` | CSRF + 近期认证 | 删除当前租户 Secret |
| `POST /api/secrets/{provider}/{name}/test` | CSRF + 近期认证 | 用短生命周期明文验证凭据；只返回 `{ok:true}` |

API 从不返回 plaintext。keyring 不可用时写入/测试返回 `503`；不存在为 `404`；不支持的验证目标或无效凭据为 `422`；上游验证服务不可用为 `503`。

## 账号删除：`/api/account/deletion`

| 方法与路径 | 要求 | 语义 |
|---|---|---|
| `POST /api/account/deletion` | 会话 + CSRF + 5 分钟内近期认证 | 确认无活动 run，调度延迟删除，撤销 runtime 并清除登录 cookie |
| `GET /api/account/deletion` | deletion recovery cookie 或 Bearer token | 返回当前 job 状态与 `purge_after` |
| `POST /api/account/deletion/recover` | recovery auth + CSRF | 在窗口内恢复账号并清除 recovery/session/CSRF cookie |

存在活动 run 时请求删除返回 `409`，detail 为含 `code=ACTIVE_RUNS` 的对象。恢复窗口过期返回 `410`。recovery token 只授权当前 job 的状态与恢复路由，不是普通 API 会话。

## 健康检查：`/api/health`

| 方法与路径 | 认证 | 语义 |
|---|---|---|
| `GET /api/health/live` | 公开 | `200 {"status":"live"}`；仅证明进程可响应 |
| `GET /api/health/ready` | 公开 | 全部门禁通过时 `200`，否则 `503`；返回 `ready`、`status`、`checks_failed` |

readiness 验证数据库连接/版本、Alembic revision、schema 完整性、默认工作区关系、工作区权限和 keyring；MySQL 还检查 UTC、READ-COMMITTED、InnoDB 与字符集。负载均衡器应以 ready 为放量门禁，不能仅使用 live。

## 作用域与错误形态

- 身份来自服务端验证的 cookie，不接受客户端传 tenant/workspace 作为授权依据。
- session/run/approval/Secret 查询都必须匹配当前 tenant/workspace；run 事件继续精确匹配 session/run。
- 普通 FastAPI 错误形态为 `{"detail":"message"}`。少数业务错误的 detail 是结构化对象，例如删除活动 run 冲突。
- validation error 使用 FastAPI/Pydantic `422` 列表；认证失败通常 `401`，账号 pending deletion 或 CSRF 为 `403`。
- scope 隐藏使用 `404`，不要通过错误差异推断其他租户资源。
- runtime 暂不可用为 `503 {"detail":"runtime temporarily unavailable"}` 并带 `Retry-After`。

完整、可机器读取的字段定义始终以运行时 `/openapi.json` 为准；兼容别名 `/api/approve` 不会出现在 schema 中。

