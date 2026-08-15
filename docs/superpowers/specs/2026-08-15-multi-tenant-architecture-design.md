# MultiClaw 多租户架构设计（2026-08-15）

## 0. 设计摘要

本规范定义 MultiClaw standalone v1 的多租户安全边界、持久化协议与验收门槛：

- 锁定 v1 `KeyEncryptionProvider` 唯一实现为 `deployment-keyring`。
- 锁定 secret envelope 协议：`format_version=1`、`algorithm='AES-256-GCM'`、AAD 为二进制 length-prefix 编码。
- 锁定删除保留期配置为 `deletion.retention_days`，范围 `0..30`，默认 `7`，仅由部署配置决定。
- 锁定所有持久化时间语义为 **UTC Unix epoch milliseconds BIGINT**，并锁定 SQLite/MySQL 的 `db_now_ms()` SQL 表达式。
- 锁定两后端事务/隔离/锁策略、驱动/依赖、支持版本与 CI gate。

- `user = tenant`
- per-user RuntimePool
- 单 run lease
- 每个 run 内工具串行执行
- 完整 scope FK
- 固定 checkpoint phases
- purge 显式顺序
- 开发阶段不做历史迁移/认领/兼容读写/回填

## 1. 绑定决策

- `tenant_id = users.id`
- 一个用户就是一个租户；v1 不支持组织、成员、邀请、RBAC 或跨用户共享
- 共享逻辑数据库；部署级单选 `sqlite` 或 `mysql`
- 存储采用 `SQLAlchemy 2.x Core async + Alembic async env`
- SQLite URL 固定 `sqlite+aiosqlite`
- MySQL URL 固定 `mysql+aiomysql`
- 加密库固定 Python `cryptography` 的 `AESGCM`
- 新增依赖明确为：`SQLAlchemy`、`Alembic`、`aiomysql`、`cryptography`
- 具体补丁版本由 `uv.lock` 固化，不在设计中留给实现者选择
- 数据模型支持多 workspace，但 v1 每用户仅 1 个默认 workspace
- v1 使用进程内 per-user `RuntimePool`；Agent、EventBus、Scheduler、审批、Skill、Tool Registry 与 MCP 均为用户级有状态对象
- 事件交付必须精确匹配 `(tenant_id, workspace_id, session_id, run_id)`
- Agent continuation 使用结构化 checkpoint 持久化；不序列化 Python 对象
- 每个 run 内工具串行，不同 run 可并发
- 用户 BYOK + 平台 fallback，仅在“用户未配置且部署允许”时生效
- 无应用级超级管理员或 break-glass
- v1 仅实现 `standalone`
- 当前开发阶段允许重建 DB；不做历史数据迁移、认领、兼容读写、回填或 quarantine
- Alembic baseline 建立后仅 forward-only 演进，并经过 production-like backup gate
- 迁移只能通过显式 `multiclaw db upgrade` 执行；API 进程只验证当前 revision 是否为 head
- 账号删除采用立即停用、延迟物理清除；`deletion.retention_days` 范围 `0..30`，默认 `7`

## 2. 当前代码证据

- 认证当前仅恢复 `sub/email`，未建立 tenant/workspace context：`src/multiclaw/auth/router.py:29-38`、`src/multiclaw/auth/middleware.py:12-24`
- 当前会话仍以 `chat_sessions.user_id` 为拥有边界：`src/multiclaw/session/sqlite.py:23-32`、`src/multiclaw/server.py:694-749`
- 当前 `memory_entries.tenant_id` 已存在，但 agent 写入与查询未强制使用：`src/multiclaw/memory/sqlite.py:28-39`、`src/multiclaw/agent/base.py:23-24`、`src/multiclaw/agent/context.py:149-172`、`src/multiclaw/agent/multiclaw.py:615-623`
- 当前为全局 `agent/shared_bus/scheduler/workspace_root`：`src/multiclaw/server.py:430-564`
- 当前 SSE 订阅全局 wildcard：`src/multiclaw/server.py:822-960`
- 当前审批只靠全局 `_pending[request_id]`：`src/multiclaw/tools/scheduler.py:32-40,98-145`
- 现有 canonical path 与 containment 基础可复用：`src/multiclaw/mcp/config.py:316-333`、`src/multiclaw/mcp/transport/factory.py:263-271`

## 3. RALPLAN-DR

### 3.1 Principles

1. 单一协议面：同一类语义只在一个地方表达，不保留运行时可选实现分叉。
2. 完整作用域优先：approval、execution、checkpoint 全部绑定 `(tenant_id, workspace_id, session_id, run_id)`。
3. DB clock 优先：lease、CAS、purge 时间判断全部信任数据库语句内时钟，不信任应用 wall clock。
4. 单 run lease + 串行工具：v1 通过单写主与单活跃工具降低恢复与 split-brain 复杂度。
5. 显式删除优先：tenant-owned FK 默认 `RESTRICT/NO ACTION`，删除顺序由协议明确控制。

### 3.2 Top 3 Drivers

1. 用确定常量消除两后端、两驱动、两种时间源、两类加密实现带来的隐式漂移。
2. 让 approval/execution 在正常重启、runtime 驱逐、审批回流后稳定恢复。
3. 让 CI 与 readiness 能对 engine/version/clock/FK/keyring/config 直接 fail closed。

### 3.3 Fair Options

#### Option A：SQLAlchemy Core async + Alembic + aiosqlite/aiomysql + cryptography AESGCM（Chosen）

优点：

- query/transaction/migration 三层统一
- 明确双方言驱动与 CI gate
- 与单 run lease、显式 CAS、固定 DB clock 协议兼容

缺点：

- 新增依赖与配置门槛

#### Option B：保留驱动/时间/AAD/加密 provider 可选（Rejected）

优点：

- 表面上更灵活

缺点：

- 会把协议确定性推迟到实现期
- CI 与恢复测试容易失真

Rejected：

- 本轮要求消除所有非必要技术可选项

## 4. 核心架构

### 4.1 TenantContext

字段：

- `tenant_id`
- `workspace_id`
- `session_id?`
- `run_id?`
- `request_started_at_ms`

来源：

1. 认证恢复 `users.id/email`
2. 解析 `users.default_workspace_id`
3. 构建 `TenantContext`
4. 注入 API、RuntimePool、TenantUnitOfWork

### 4.2 TenantUnitOfWork

定义：

- 单个 UoW 生命周期内只持有 1 个 async connection handle
- 只持有 1 个 transaction handle
- 所有 repo facade 共用该 connection/transaction
- 跨 repo 状态推进失败即全回滚

接口：

- `async with TenantUnitOfWork(context) as uow`
- `uow.conn`
- `uow.tx`
- `uow.users`
- `uow.workspaces`
- `uow.sessions`
- `uow.memory`
- `uow.agent_runs`
- `uow.approvals`
- `uow.executions`
- `uow.checkpoints`
- `uow.secrets`
- `uow.audit`
- `uow.deletions`

未认证的登录/验证码流程尚无 `tenant_id`，因此不能伪造 TenantContext。它使用受限 `AuthUnitOfWork`，只暴露 `users`、`verification_codes` 和认证速率限制数据；不能访问 session、memory、workspace、secret 或 workflow repository。认证找到用户后，才进入 TenantContext/TenantUnitOfWork。

Auth 与 tenant repository 使用同一个部署所选数据库和 SQLAlchemy engine；MySQL 部署不得保留旁路 SQLite AuthStore。

### 4.3 RuntimePool

Key：`tenant_id`

Handle：

- `runtime_instance_id`
- `workspace_root`
- `agent`
- `event_bus`
- `scheduler`
- `tool_registry`
- `skill_manager`
- `mcp_manager`
- `active_run_count`
- `active_executing_run_count`
- `persisted_awaiting_user_run_count`
- `last_used_at_ms`

策略：

- lazy create
- per-tenant create lock
- `active_executing_run_count > 0` 时禁止 eviction
- 仅当 run 已持久化到 `awaiting_user` 且当前无 executing tool 时允许 checkpoint 后 eviction
- 审批返回时如 runtime 不存在，则重建 runtime、先获取 run lease、再按 checkpoint 恢复

### 4.4 EventRouter

- 事件必须包含 `tenant_id/workspace_id/session_id/run_id/event_type/occurred_at`
- SSE 只能订阅当前 tenant runtime 的 EventBus，并在交付前再次精确匹配完整 run scope
- 禁止以全局 wildcard 或单独 `request_id` 作为用户事件隔离边界
- 缺少完整 scope 的事件视为协议错误，不允许消费者猜测归属

### 4.5 WorkspaceResolver

- 将 `(tenant_id, workspace_id)` 稳定映射到 `{workspace_root}/{tenant_id}/{workspace_id}`
- 拒绝绝对路径、`..`、NUL 与非法编码
- Tool、MCP、shell cwd、文件 API 与账号清除必须复用同一个 canonical path / symlink containment 实现
- 删除操作只接受服务端 ID 并重新计算根目录，不接受客户端提供的递归删除路径

### 4.6 SecretResolver

解析顺序固定为：

1. 若用户配置了目标 secret，则解密并使用用户 secret。
2. 若用户 secret 存在但解密、格式或上游认证失败，则明确失败，禁止平台 fallback。
3. 仅当用户未配置且部署允许 `allow_platform_fallback` 时，使用平台默认凭据。
4. 其余情况返回“未配置凭据”。

Secret 明文只能存在于当前调用范围，禁止进入 checkpoint、日志、SSE、审计 detail、metrics、模型上下文或前端持久化状态。

### 4.7 Workflow 与 Deletion Coordinator

- Workflow Coordinator 是 run/approval/execution/checkpoint 状态转移的唯一入口。
- Deletion Coordinator 管理删除请求、受限恢复、Runtime 撤销、文件清除和数据库物理删除。
- HTTP 路由只负责认证、输入校验、调用应用服务和序列化响应，不直接推进跨表状态。

### 4.8 Auth boundary

- JWT signing key 由部署 Secret 注入，不存放在数据库 `auth_config` 表，也不自动生成后写入 SQLite 专用文件。
- JWT 至少携带 `sub/auth_epoch/iat/exp`；受保护请求必须查询或强一致验证当前用户状态与 epoch。
- 登录验证码记录只保存 digest，使用后原子标记，过期数据由短 TTL 任务清除。
- `deletion_recovery` token 使用独立 audience/scope，不能作为普通登录 token 使用。

## 5. 支持版本与 CI 基线

- Python `>= 3.12`
- SQLite `>= 3.35`
- SQLite 合同测试必须基于 file-backed DB，不以 `:memory:` 代替
- MySQL `>= 8.0.36`
- MySQL engine 固定 InnoDB，字符集固定 `utf8mb4`
- CI gate 的 MySQL service 固定 `8.0.36`
- 可选 nightly `8.4`，但不作为 merge gate

每个 PR 两后端都必须运行：

1. empty -> head Alembic upgrade
2. FK check / schema introspection
3. repository contract suite
4. lease / CAS / recovery / purge suite

startup gate：

- engine/version/config 不满足要求即 readiness fail closed
- API 启动时只验证 Alembic revision 等于 head，不自动运行 migration
- schema 落后或高于当前代码支持版本时 readiness fail closed

迁移执行：

- 唯一写路径为显式 `multiclaw db upgrade`
- 升级命令负责执行备份验证、获取迁移锁并运行 `alembic upgrade head`
- 开发脚本可以显式串联该命令，但不能把自动迁移隐藏在 API lifespan 中

当前仍处于开发阶段，首次落地可直接重建数据库并从 empty 升到 baseline head，不设计旧数据认领、双读或回填。Baseline 之后采用 forward-only：失败通过修复 revision 前滚，不把 schema downgrade 作为生产恢复手段。

production-like backup gate：

- SQLite 使用 SQLite backup API 或停写文件快照，随后在独立临时路径打开并执行 `PRAGMA integrity_check` 与 schema revision 检查。
- MySQL 使用一致性逻辑备份或存储快照，并在隔离数据库完成 restore smoke test 与 revision 检查。
- 未产生新鲜备份、备份校验失败或恢复 smoke test 失败时，upgrade 命令必须拒绝迁移。
- 账号清除后的历史备份不逐条修改，按部署文档声明的备份保留周期自然过期。

## 6. 驱动、依赖与事务语义

### 6.1 依赖锁定

- SQLite URL：`sqlite+aiosqlite`
- SQLite driver：`aiosqlite`
- MySQL URL：`mysql+aiomysql`
- MySQL driver：`aiomysql`
- SQLAlchemy：2.x Core async
- Alembic：async env
- 加密：`cryptography` `AESGCM`

### 6.2 SQLite 事务/锁

- write UoW：`BEGIN IMMEDIATE`
- writer 串行
- `busy_timeout` 默认 `5000ms`
- read UoW：deferred snapshot
- SQLite 无 `SELECT FOR UPDATE`
- 依赖：
  - `BEGIN IMMEDIATE`
  - versioned CAS
  - run fencing

### 6.3 MySQL 事务/锁

- InnoDB
- isolation level：`READ COMMITTED`
- 涉及“每 run 最多一个非终态工具”与多行状态推进时：
  - 先 `SELECT agent_runs ... FOR UPDATE`
  - 再在同一 UoW 内更新相关行
- 简单 approval/lease CAS 可直接条件 `UPDATE`

### 6.4 双后端共同规则

- 跨 repo 状态推进必须单 connection + 单 transaction
- 任一步失败全回滚

## 7. 时间语义与 DB Clock

### 7.1 统一时间类型

所有持久化时间字段使用：

- UTC Unix epoch milliseconds
- SQL 类型：`BIGINT`
- 字段名可保留 `*_at`，但语义固定为 epoch ms

包括：

- `created_at`
- `updated_at`
- `finished_at`
- `requested_at`
- `resolved_at`
- `expires_at`
- `lease_expires_at`
- `heartbeat_at`
- `purge_after`

### 7.2 唯一可信时间源

- 所有 lease/CAS/purge 比较只使用语句内 DB clock
- 不信任 app wall clock

### 7.3 StorageDialect.db_now_ms()

SQLite：

```sql
CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)
```

MySQL：

```sql
CAST(UNIX_TIMESTAMP(UTC_TIMESTAMP(3)) * 1000 AS UNSIGNED)
```

MySQL session：

- `time_zone = '+00:00'`

## 8. KeyEncryptionProvider

### 8.1 v1 唯一 provider

v1 运行时只接受：

- `key_provider_name = 'deployment-keyring'`

不允许进入 v1 runtime 的 provider：

- `kms`
- `vault`
- 任何未知 provider name

规则：

- 未知 provider -> readiness fail closed
- 接口可保留未来扩展边界，但 v1 实现唯一

### 8.2 keyring 注入来源 XOR

keyring 只能从且仅从以下二者之一注入：

1. `MULTICLAW_SECRETS_KEYRING_B64`
2. `secrets.keyring_file`

规则：

- 两者同时配置 -> fail closed
- 两者均缺失 -> fail closed

### 8.3 keyring_file 安全要求

- 仅适用于 POSIX
- group/world readable -> fail closed

### 8.4 keyring JSON 固定结构

base64 decode 后得到 JSON：

```json
{
  "active_key_version": 3,
  "keys": {
    "1": "base64-32-byte-key",
    "2": "base64-32-byte-key",
    "3": "base64-32-byte-key"
  }
}
```

启动校验：

- `active_key_version` 必须存在于 `keys`
- 每个 key decode 后必须恰好 `32 bytes`
- 所有已用 `key_version` 必须能在 keyring 找到，否则 readiness fail closed

### 8.5 轮换规则

1. 将新 key 加入 keyring
2. 将其设为 `active_key_version`
3. 后台逐行重加密
4. 仅当旧版本引用数为 `0` 时才允许从 keyring 移除

## 9. Secret Envelope Protocol

### 9.1 user_secrets schema

字段：

- `id CHAR(36)`
- `tenant_id CHAR(36)`
- `workspace_id CHAR(36) NULL`
- `provider_kind VARCHAR(32)`
- `provider_name VARCHAR(128)`
- `secret_name VARCHAR(128)`
- `key_provider_name VARCHAR(128)` 固定为 `deployment-keyring`
- `format_version INTEGER` 固定为 `1`
- `algorithm VARCHAR(32)` 固定为 `AES-256-GCM`
- `key_version INTEGER`
- `nonce LargeBinary(12)`，由 SQLAlchemy 在 SQLite/MySQL 映射为固定协议的 12-byte 二进制值
- `ciphertext LargeBinary`，内容为 ciphertext 与尾随 16-byte authentication tag
- `created_at BIGINT`
- `updated_at BIGINT`
- `rotated_at BIGINT NULL`

约束：

- `PRIMARY KEY (id)`
- `UNIQUE (tenant_id, provider_kind, provider_name, secret_name)`
- `UNIQUE (key_provider_name, key_version, nonce)`
- `FOREIGN KEY (tenant_id) REFERENCES users(id) ON DELETE RESTRICT ON UPDATE RESTRICT`
- `FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT`

语义：

- v1 只暴露用户级 secret，`workspace_id` 保持 `NULL`

### 9.2 固定加密常量

- `format_version = 1`
- `algorithm = 'AES-256-GCM'`
- key length = `32 bytes`
- nonce = `12 bytes` CSPRNG
- tag = `16 bytes`，作为 `AESGCM` ciphertext 尾部
- 底层库固定 Python `cryptography` 的 `AESGCM`

### 9.3 固定 AAD 编码

不使用 canonical JSON。

AAD 编码固定为二进制 length-prefix：

1. domain prefix ASCII：

```text
multiclaw.secret-envelope.v1\0
```

2. 后接固定顺序字段：

- `tenant_id`
- `workspace_id(nullable)`
- `secret_id`
- `provider_kind`
- `provider_name`
- `secret_name`
- `key_provider_name`
- `key_version(decimal ASCII)`
- `format_version(decimal ASCII)`
- `algorithm`

3. 每字段编码：

- 4-byte unsigned big-endian length
- UTF-8 bytes
- `NULL` 用长度 `0xFFFFFFFF`

要求：

- 解密前后都校验 AAD
- ciphertext 行交换必须失败
- 固定测试向量文件作为后续实现计划产物，但协议本身已锁定

## 10. 数据模型

### 10.1 类型与长度原则

- 主 ID：`CHAR(36)` UUID 字符串
- `tool_call_id`：`VARCHAR(128)`
- `runtime_instance_id` / `lease_owner`：`VARCHAR(128)`
- status / algorithm / provider_kind：`VARCHAR(32)` 或 `VARCHAR(64)`
- 所有时间字段：`BIGINT`
- 结构化 payload 使用 SQLAlchemy `Text().with_variant(MEDIUMTEXT(), "mysql")`，SQLite 映射为 `TEXT`、MySQL 映射为 `MEDIUMTEXT`
- 可能超过 16 MiB 的消息正文不直接写入 workflow payload；通过 workspace/object 引用传递

### 10.2 users

字段：

- `id CHAR(36)` PK，兼作 `tenant_id`
- `email VARCHAR(320)`
- `auth_epoch BIGINT NOT NULL DEFAULT 0`
- `default_workspace_id CHAR(36) NULL`
- `status VARCHAR(32)` = `active | disabled | pending_purge`
- `purge_after BIGINT NULL`
- `created_at BIGINT`
- `updated_at BIGINT`
- `disabled_at BIGINT NULL`
- `purge_requested_at BIGINT NULL`

约束：

- `UNIQUE (id, default_workspace_id)`
- `FOREIGN KEY (id, default_workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT`

运行时不变量：

- `status='active' AND default_workspace_id IS NULL` -> readiness fail closed
- `status='pending_purge' AND default_workspace_id IS NULL` -> 合法
- JWT 必须携带签发时的 `auth_epoch`；所有受保护请求同时验证用户为 `active` 且 token epoch 等于数据库值

### 10.3 workspaces

字段：

- `id CHAR(36)`
- `tenant_id CHAR(36)`
- `slug VARCHAR(64)`
- `name VARCHAR(255)`
- `status VARCHAR(32)`
- `created_at BIGINT`
- `updated_at BIGINT`

约束：

- `PRIMARY KEY (id)`
- `UNIQUE (tenant_id, id)`
- `UNIQUE (tenant_id, slug)`
- `FOREIGN KEY (tenant_id) REFERENCES users(id) ON DELETE RESTRICT ON UPDATE RESTRICT`

### 10.4 chat_sessions

字段：

- `id CHAR(36)`
- `tenant_id CHAR(36)`
- `workspace_id CHAR(36)`
- `title VARCHAR(255)`
- `status VARCHAR(32)`
- `created_at BIGINT`
- `updated_at BIGINT`
- `last_message_at BIGINT NULL`
- `metadata_json` 使用结构化 payload 类型

约束：

- `PRIMARY KEY (id)`
- `UNIQUE (tenant_id, id)`
- `UNIQUE (tenant_id, workspace_id, id)`
- `FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT`

### 10.5 memory_entries

字段：

- `id CHAR(36)`
- `tenant_id CHAR(36)`
- `workspace_id CHAR(36)`
- `session_id CHAR(36) NULL`
- `content` 使用结构化 payload 类型
- `type VARCHAR(64)`
- `role VARCHAR(32)`
- `turn_index INTEGER`
- `created_at BIGINT`
- `metadata_json` 使用结构化 payload 类型

约束：

- `PRIMARY KEY (id)`
- `UNIQUE (tenant_id, id)`
- `UNIQUE (tenant_id, workspace_id, id)`
- `FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT`
- `FOREIGN KEY (tenant_id, workspace_id, session_id) REFERENCES chat_sessions(tenant_id, workspace_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT`

查询语义：

- recent chat：仅 `session_id = current_session_id`
- relevant retrieval：`session_id = current_session_id OR session_id IS NULL`
- `session_id IS NULL` 的长期记忆永不跨 workspace 返回

### 10.6 agent_runs

字段：

- `run_id CHAR(36)`
- `tenant_id CHAR(36)`
- `workspace_id CHAR(36)`
- `session_id CHAR(36)`
- `run_status VARCHAR(32)`
- `runtime_instance_id VARCHAR(128)`
- `lease_owner VARCHAR(128)`
- `fencing_token BIGINT`
- `lease_expires_at BIGINT`
- `heartbeat_at BIGINT`
- `schema_version INTEGER`
- `version BIGINT`
- `created_at BIGINT`
- `updated_at BIGINT`
- `finished_at BIGINT NULL`

约束：

- `PRIMARY KEY (run_id)`
- `UNIQUE (tenant_id, run_id)`
- `UNIQUE (tenant_id, workspace_id, session_id, run_id)`
- `FOREIGN KEY (tenant_id, workspace_id, session_id) REFERENCES chat_sessions(tenant_id, workspace_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT`

`run_status`：

- `running`
- `awaiting_user`
- `resuming`
- `completed`
- `failed_terminal`
- `blocked_incompatible`
- `blocked_corrupt`
- `cancelled`

### 10.7 approval_requests

字段：

- `approval_id CHAR(36)`
- `tenant_id CHAR(36)`
- `workspace_id CHAR(36)`
- `session_id CHAR(36)`
- `run_id CHAR(36)`
- `tool_call_id VARCHAR(128)`
- `approval_status VARCHAR(32)`
- `requested_at BIGINT`
- `resolved_at BIGINT NULL`
- `expires_at BIGINT`
- `version BIGINT`

约束：

- `PRIMARY KEY (approval_id)`
- `UNIQUE (tenant_id, approval_id)`
- `UNIQUE (tenant_id, workspace_id, session_id, run_id, approval_id)`
- `UNIQUE (tenant_id, workspace_id, session_id, run_id, tool_call_id)`
- `FOREIGN KEY (tenant_id, workspace_id, session_id, run_id) REFERENCES agent_runs(tenant_id, workspace_id, session_id, run_id) ON DELETE RESTRICT ON UPDATE RESTRICT`

`approval_status`：

- `awaiting_user`
- `approved`
- `rejected`
- `expired`

### 10.8 tool_executions

字段：

- `execution_id CHAR(36)`
- `tenant_id CHAR(36)`
- `workspace_id CHAR(36)`
- `session_id CHAR(36)`
- `run_id CHAR(36)`
- `approval_id CHAR(36) NULL`
- `tool_call_id VARCHAR(128)`
- `tool_name VARCHAR(128)`
- `tool_kind VARCHAR(64)`
- `execution_status VARCHAR(32)`
- `recovery_strategy VARCHAR(32)`
- `idempotency_key VARCHAR(128) NULL`
- `input_payload_json` 使用结构化 payload 类型
- `input_hash VARCHAR(64)`
- `external_request_id VARCHAR(255) NULL`
- `result_ref VARCHAR(255) NULL`
- `result_digest VARCHAR(64) NULL`
- `schema_version INTEGER`
- `version BIGINT`
- `created_at BIGINT`
- `updated_at BIGINT`
- `finished_at BIGINT NULL`

约束：

- `PRIMARY KEY (execution_id)`
- `UNIQUE (tenant_id, execution_id)`
- `UNIQUE (tenant_id, workspace_id, session_id, run_id, execution_id)`
- `UNIQUE (tenant_id, workspace_id, session_id, run_id, tool_call_id)`
- `FOREIGN KEY (tenant_id, workspace_id, session_id, run_id) REFERENCES agent_runs(tenant_id, workspace_id, session_id, run_id) ON DELETE RESTRICT ON UPDATE RESTRICT`
- `FOREIGN KEY (tenant_id, workspace_id, session_id, run_id, approval_id) REFERENCES approval_requests(tenant_id, workspace_id, session_id, run_id, approval_id) ON DELETE RESTRICT ON UPDATE RESTRICT`

optional composite FK 语义：

- `approval_id IS NULL` 时，整条 optional approval FK 不生效
- `tenant_id/workspace_id/session_id/run_id` 仍始终 `NOT NULL`

`execution_status`：

- `not_started`
- `replaying`
- `executing`
- `succeeded`
- `failed_retryable`
- `failed_terminal`
- `uncertain`
- `blocked_incompatible`
- `blocked_corrupt`

`recovery_strategy`：

- `read_only_replay`
- `idempotent_retry`
- `manual_uncertain`

输入与结果语义：

- `input_payload_json` 是通过 schema 验证后的不可变工具输入，最大 `256 KiB`；更大的内容必须通过 workspace 文件或受作用域约束的对象引用传递。
- 输入中只允许 secret 引用，禁止复制 secret 明文；`input_hash` 覆盖 canonical input payload。
- `external_request_id`、`idempotency_key`、`result_ref` 与 `result_digest` 用于崩溃后判断远端副作用和恢复结果。

### 10.9 execution_checkpoints

字段：

- `checkpoint_id CHAR(36)`
- `tenant_id CHAR(36)`
- `workspace_id CHAR(36)`
- `session_id CHAR(36)`
- `run_id CHAR(36)`
- `approval_id CHAR(36) NULL`
- `execution_id CHAR(36) NULL`
- `phase VARCHAR(64)`
- `checkpoint_seq BIGINT`
- `payload_json` 使用结构化 payload 类型
- `payload_hash VARCHAR(64)`
- `schema_version INTEGER`
- `created_at BIGINT`

约束：

- `PRIMARY KEY (checkpoint_id)`
- `UNIQUE (tenant_id, checkpoint_id)`
- `UNIQUE (tenant_id, workspace_id, session_id, run_id, checkpoint_seq)`
- `FOREIGN KEY (tenant_id, workspace_id, session_id, run_id) REFERENCES agent_runs(tenant_id, workspace_id, session_id, run_id) ON DELETE RESTRICT ON UPDATE RESTRICT`
- `FOREIGN KEY (tenant_id, workspace_id, session_id, run_id, approval_id) REFERENCES approval_requests(tenant_id, workspace_id, session_id, run_id, approval_id) ON DELETE RESTRICT ON UPDATE RESTRICT`
- `FOREIGN KEY (tenant_id, workspace_id, session_id, run_id, execution_id) REFERENCES tool_executions(tenant_id, workspace_id, session_id, run_id, execution_id) ON DELETE RESTRICT ON UPDATE RESTRICT`

optional composite FK 语义：

- `approval_id IS NULL` 或 `execution_id IS NULL` 时，对应整条 optional FK 不生效
- `tenant_id/workspace_id/session_id/run_id` 仍始终 `NOT NULL`

### 10.10 audit_logs

字段：

- `audit_id CHAR(36)`
- `tenant_id CHAR(36)`
- `workspace_id CHAR(36)`
- `session_id CHAR(36) NULL`
- `run_id CHAR(36) NULL`
- `approval_id CHAR(36) NULL`
- `execution_id CHAR(36) NULL`
- `event_type VARCHAR(64)`
- `status VARCHAR(32)`
- `tool_name VARCHAR(128) NULL`
- `detail_redacted TEXT`
- `created_at BIGINT`

约束：

- `PRIMARY KEY (audit_id)`
- `UNIQUE (tenant_id, audit_id)`
- `FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces(tenant_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT`
- `FOREIGN KEY (tenant_id, workspace_id, session_id) REFERENCES chat_sessions(tenant_id, workspace_id, id) ON DELETE RESTRICT ON UPDATE RESTRICT`
- `FOREIGN KEY (tenant_id, workspace_id, session_id, run_id) REFERENCES agent_runs(tenant_id, workspace_id, session_id, run_id) ON DELETE RESTRICT ON UPDATE RESTRICT`
- `FOREIGN KEY (tenant_id, workspace_id, session_id, run_id, approval_id) REFERENCES approval_requests(tenant_id, workspace_id, session_id, run_id, approval_id) ON DELETE RESTRICT ON UPDATE RESTRICT`
- `FOREIGN KEY (tenant_id, workspace_id, session_id, run_id, execution_id) REFERENCES tool_executions(tenant_id, workspace_id, session_id, run_id, execution_id) ON DELETE RESTRICT ON UPDATE RESTRICT`

### 10.11 deletion_jobs

字段：

- `job_id CHAR(36)`
- `tenant_id CHAR(36)`
- `status VARCHAR(32)`
- `purge_after BIGINT`
- `requested_at BIGINT`
- `started_at BIGINT NULL`
- `worker_id VARCHAR(128) NULL`
- `lease_expires_at BIGINT NULL`
- `heartbeat_at BIGINT NULL`
- `fencing_token BIGINT NOT NULL DEFAULT 0`
- `version BIGINT NOT NULL DEFAULT 0`
- `attempt_count INTEGER`
- `last_error TEXT NULL`

约束：

- `PRIMARY KEY (job_id)`
- `UNIQUE (tenant_id, job_id)`
- `FOREIGN KEY (tenant_id) REFERENCES users(id) ON DELETE RESTRICT ON UPDATE RESTRICT`

`status`：

- `scheduled`
- `running`

### 10.12 verification_codes

邮箱验证码在用户创建前就可能存在，因此属于全局认证控制数据，不带 `tenant_id`：

- `id CHAR(36)`
- `email VARCHAR(320)`
- `code_digest VARCHAR(128)`
- `purpose VARCHAR(32)`，例如 `login | deletion_recovery`
- `expires_at BIGINT`
- `used_at BIGINT NULL`
- `created_at BIGINT`

规则：

- 不保存验证码明文。
- `deletion_recovery` 验证码只能换取短期、单用途恢复 token，不能直接创建普通应用会话。
- 清除用户时按该用户 email 删除尚未过期的验证码记录；其他验证码按短 TTL 定期清理。

## 11. 串行工具不变量

v1 明确：

- **每个 run 内工具串行执行**
- **不同 run 可并发**

per-tool-call 合法性：

- 同一 run 可存在历史 `succeeded/failed_terminal/expired/rejected` records
- 同一 run 当前最多一个非终态 approval/execution
- 同一 run 可出现“历史 succeeded execution + 当前 awaiting approval/not_started execution”

run 终态约束：

- `agent_runs.run_status='completed'` 前，所有 `tool_executions` 必须 terminal：
  - `succeeded`
  - `failed_terminal`
  - `uncertain`
  - `blocked_incompatible`
  - `blocked_corrupt`

## 12. 状态组合与 CAS

### 12.1 合法组合

| run_status | 当前 approval_status | 当前 execution_status | 合法性 |
| --- | --- | --- | --- |
| `running` | 无 | 无 | 合法 |
| `running` | `awaiting_user` | `not_started` | 合法 |
| `awaiting_user` | `awaiting_user` | `not_started` | 合法 |
| `resuming` | `approved` | `replaying` | 合法 |
| `running` | `approved` | `executing` | 合法 |
| `running` | `approved` | `uncertain` | 合法 |
| `completed` | `approved/rejected/expired/无` | 全部 terminal | 合法 |

非法示例：

- 同一 run 同时两个 `awaiting_user`
- 同一 run 同时两个 `executing`
- `awaiting_user + executing`
- `completed + 存在非终态 execution`

### 12.2 run lease 常量

仅 `agent_runs` 保存：

- `lease_owner`
- `fencing_token`
- `lease_expires_at`
- `heartbeat_at`
- `version`

默认值：

- heartbeat cadence：`5000ms`
- lease TTL：`20000ms`
- 抢占阈值：`lease_expires_at <= db_now_ms()`

### 12.3 Approval CAS

`approval_status` 转移：

- `awaiting_user -> approved`
- `awaiting_user -> rejected`
- `awaiting_user -> expired`

approval API：

- 仅做 tenant-scoped approval CAS
- runtime 已释放时不直接推进 execution
- 恢复者必须先拿 run lease 再推进 execution

伪 SQL：

```sql
UPDATE approval_requests
SET approval_status = :next,
    resolved_at = DB_NOW_MS,
    version = version + 1
WHERE tenant_id = :tenant_id
  AND workspace_id = :workspace_id
  AND session_id = :session_id
  AND run_id = :run_id
  AND approval_id = :approval_id
  AND approval_status = 'awaiting_user'
  AND version = :expected_version;
```

### 12.4 Run CAS

`run_status` 转移：

- `running -> awaiting_user`
- `awaiting_user -> resuming`
- `resuming -> running`
- `running -> completed`
- `running/resuming -> failed_terminal`
- `running/resuming -> blocked_incompatible`
- `running/resuming -> blocked_corrupt`
- `running/resuming -> cancelled`

伪 SQL：

```sql
UPDATE agent_runs
SET run_status = :next,
    version = version + 1,
    heartbeat_at = DB_NOW_MS,
    updated_at = DB_NOW_MS
WHERE tenant_id = :tenant_id
  AND workspace_id = :workspace_id
  AND session_id = :session_id
  AND run_id = :run_id
  AND run_status = :expected_status
  AND version = :expected_version
  AND lease_owner = :lease_owner
  AND fencing_token = :fencing_token
  AND lease_expires_at > DB_NOW_MS;
```

### 12.5 Execution CAS

`execution_status` 转移：

- `not_started -> replaying`
- `not_started|replaying -> executing`
- `executing -> succeeded`
- `executing -> failed_retryable`
- `executing -> failed_terminal`
- `executing -> uncertain`
- `not_started|replaying|executing -> blocked_incompatible`
- `not_started|replaying|executing -> blocked_corrupt`

所有 execution 推进必须：

- 在同一 `TenantUnitOfWork` 事务
- 通过 `EXISTS` 或 `SELECT agent_runs ... FOR UPDATE` / SQLite `BEGIN IMMEDIATE` + 读取 `agent_runs`
- 校验：
  - run lease 有效
  - `lease_owner`
  - `fencing_token`
  - `agent_runs.version`
  - `tool_executions.version`
  - `lease_expires_at > DB_NOW_MS`

伪 SQL：

```sql
UPDATE tool_executions
SET execution_status = :next,
    version = version + 1,
    updated_at = DB_NOW_MS
WHERE tenant_id = :tenant_id
  AND workspace_id = :workspace_id
  AND session_id = :session_id
  AND run_id = :run_id
  AND execution_id = :execution_id
  AND execution_status = :expected_execution_status
  AND version = :expected_execution_version
  AND EXISTS (
    SELECT 1
    FROM agent_runs
    WHERE tenant_id = :tenant_id
      AND workspace_id = :workspace_id
      AND session_id = :session_id
      AND run_id = :run_id
      AND lease_owner = :lease_owner
      AND fencing_token = :fencing_token
      AND version = :expected_run_version
      AND lease_expires_at > DB_NOW_MS
  );
```

### 12.6 Lease 抢占

规则：

1. 新 runtime 仅可抢占已过期 run lease
2. 抢占成功时：
   - `lease_owner = new_runtime_instance_id`
   - `fencing_token = old_fencing_token + 1`
   - `lease_expires_at = DB_NOW_MS + 20000`
   - `heartbeat_at = DB_NOW_MS`
   - `version = version + 1`
3. 旧 runtime 此后对 run/execution/checkpoint 的所有写入都失败

## 13. Checkpoint Protocol

### 13.1 分类

- run-only checkpoint：`execution_id IS NULL`
- execution-level checkpoint：`execution_id IS NOT NULL`

### 13.2 Guard

run-only checkpoint：

- 验证 `agent_runs` 的 lease/fencing/version/DB time

execution-level checkpoint：

- 验证 `agent_runs` 的 lease/fencing/version/DB time
- 验证同 scope execution 的 `version` 与 `execution_status`

### 13.3 固定 phase schema

`run_started`

- required fields:
  - `tenant_id`
  - `workspace_id`
  - `session_id`
  - `run_id`
  - `started_at_ms`
  - `model_cursor`
- `next_step = "model_inference"`
- `cursor = model_cursor`

`model_output_committed`

- required fields:
  - `run_id`
  - `message_id`
  - `output_digest`
  - `model_cursor`
- `next_step = "tool_plan_or_terminal"`
- `cursor = model_cursor`

`awaiting_approval`

- required fields:
  - `run_id`
  - `approval_id`
  - `tool_call_id`
  - `approval_expires_at_ms`
  - `resume_cursor`
- `next_step = "approval_resolution"`
- `cursor = resume_cursor`

`execution_dispatching`

- required fields:
  - `run_id`
  - `execution_id`
  - `tool_call_id`
  - `recovery_strategy`
  - `input_hash`
  - `input_ref`，指向 `tool_executions.input_payload_json`
  - `idempotency_key`，仅 `idempotent_retry` 必填
  - `dispatch_cursor`
- `next_step = "execution_observation"`
- `cursor = dispatch_cursor`

`execution_result_observed`

- required fields:
  - `run_id`
  - `execution_id`
  - `result_status`
  - `result_digest`
  - `result_ref`
  - `external_request_id`，远端系统提供时必填
  - `resume_cursor`
- `next_step = "continue_or_terminal"`
- `cursor = resume_cursor`

`run_terminal`

- required fields:
  - `run_id`
  - `terminal_status`
  - `finished_at_ms`
  - `final_digest`
- `next_step = null`
- `cursor = null`

### 13.4 Secret 禁令

- checkpoint payload 不得包含 secret 明文
- 仅允许摘要、ID、cursor、digest、status、bounded metadata
- 工具输入通过 `tool_executions` 中的不可变 payload/ref 恢复；checkpoint 只保存引用和 hash

### 13.5 恢复校验

恢复前必须验证：

- `schema_version`
- `phase`
- required fields 完整
- `payload_hash` 匹配

失败处理：

- 缺字段 / hash 不匹配 -> `blocked_corrupt`
- 版本不支持 -> `blocked_incompatible`

## 14. 首次 default workspace bootstrap

单事务顺序：

1. 创建 `users(id, email, default_workspace_id=NULL, status='active')`
2. 创建 `workspaces(id, tenant_id=id, slug='default', ...)`
3. 更新 `users.default_workspace_id = workspaces.id`
4. 重新读取验证 `(id, default_workspace_id)` 命中 `workspaces(tenant_id, id)`
5. 提交

readiness：

- `active + NULL default_workspace_id` fail closed
- `pending_purge + NULL default_workspace_id` 合法

Alembic DDL 顺序：

1. create `users`（先不加复合 FK）
2. create `workspaces`
3. add `users(id, default_workspace_id) -> workspaces(tenant_id, id)` 复合 FK
4. SQLite 使用 Alembic batch/DDL rebuild 明确处理

## 15. 删除与 retention

### 15.1 retention 配置

配置键：

- `deletion.retention_days`

规则：

- Pydantic integer
- 允许 `0..30`
- 默认 `7`
- 越界或非整数 -> startup fail closed
- API 不允许逐请求覆盖

删除请求 UoW：

- `purge_after = db_now_ms() + retention_days * 86400000`
- 后续配置变化不回改已有 job
- `retention_days = 0` 表示事务提交后立即具备异步 purge 资格
- 不在请求内同步递归删除

### 15.2 删除请求与状态

发起删除前必须进行近期重新认证。当前产品使用邮箱验证码，因此删除确认通过新的登录验证码完成，不引入密码语义。

删除请求 UoW 必须：

1. 锁定 `users` 行并验证当前为 `active`。
2. 若存在 `replaying/executing` 工具或有效 run lease，返回 `409 ACTIVE_RUNS`；用户必须先等待或取消 run。
3. 将等待审批的 run/approval 取消或过期。
4. 写入 `status='pending_purge'`、`purge_requested_at`、`purge_after` 和唯一 `deletion_job(status='scheduled')`。
5. `auth_epoch = auth_epoch + 1`，使所有既有 JWT 失效。
6. 提交后关闭并移除用户 Runtime、MCP 连接和内存 secret handles。

`pending_purge + NULL default_workspace_id` 是合法清除中间态；到 `purge_after` 前，普通登录、session 和 run API 均不可用。重复删除请求返回现有状态和相同 `purge_after`，不能创建第二个活跃 job。

### 15.3 保留期内恢复

延迟删除必须提供纠正误操作的恢复路径：

1. 用户通过专用邮箱验证码流程证明对原邮箱的控制权。
2. 服务签发短期、单用途 `deletion_recovery` token；该 token 只能读取删除状态和取消删除，不能访问聊天或 Secret。
3. 取消接口在同一 UoW 中锁定 `users` 与 `deletion_jobs`。
4. 仅当 `users.status='pending_purge'`、job 为 `scheduled` 且 `db_now_ms() < purge_after` 时允许恢复。
5. 删除 job、清空删除字段、恢复 `status='active'`，再次增加 `auth_epoch`，然后要求用户重新登录。

清除器通过 CAS 将 job 从 `scheduled` 改为 `running`；取消与清除锁定同一记录，因此只有一方成功。job 已为 `running` 时禁止恢复。`retention_days=0` 不承诺恢复窗口。

### 15.4 文件与数据对象范围

- workspace 文件
- `execution_checkpoints`
- `audit_logs`
- `tool_executions`
- `approval_requests`
- `agent_runs`
- `memory_entries`
- `chat_sessions`
- `user_secrets`
- `workspaces`
- `deletion_jobs`
- `users`
- 与该用户 email 关联且尚未过期的 `verification_codes`

### 15.5 可执行顺序

1. 通过 DB clock 选择 `scheduled AND purge_after <= db_now_ms()` 的 job，并以 version CAS 抢占为 `running`。
2. 关闭/撤销 runtime，确认不存在有效 run lease，清除 in-memory secret handles。
3. 根据服务端 tenant/workspace ID 重新计算目录，执行 canonical path 与 symlink containment；禁止使用客户端路径。
4. 幂等删除 workspace 文件；目标不存在视为成功。
5. 单 DB 事务内保存待删 email，并：
   - `UPDATE users SET default_workspace_id=NULL WHERE status='pending_purge'`
   - 按叶到根删除：
     - `execution_checkpoints`
     - `audit_logs`
     - `tool_executions`
     - `approval_requests`
     - `agent_runs`
     - `memory_entries`
     - `chat_sessions`
     - `user_secrets`
     - `workspaces`
     - 与用户 email 关联的 `verification_codes`
     - `deletion_jobs`
     - `users`

重试语义：

- 文件已删：继续
- DB 事务失败：整步可重试
- DB 已删但 ack 前崩溃：若用户/job 均不存在则视为完成
- job 长时间停留在 `running`：清除器根据 DB heartbeat/lease 规则重新取得执行权并从文件阶段幂等重试

FK 策略：

- 所有 tenant-owned ownership FK 默认 `ON DELETE RESTRICT/NO ACTION`
- 不依赖 cascade，避免 SQLite/MySQL cycle/cascade 差异

## 16. Cluster

v1 不实现任何分布式代码。

仅保留不变量：

- 同一 `tenant_id` 同时最多一个可写 runtime owner
- run lease + fencing_token 决定唯一写主
- workspace root 对 `(tenant_id, workspace_id)` 稳定映射

## 17. Pre-mortem

### 场景 1：Lease Split-Brain

- 原因：旧 runtime 心跳延迟，新 runtime 抢占成功后旧 runtime 仍尝试写
- 缓解：所有 run/execution/checkpoint 写都比较 run lease + fencing_token + DB time + version

### 场景 2：Purge 半执行重试

- 原因：文件已删、DB 事务未提交即崩溃
- 缓解：文件删除幂等；DB 可重试；用户/job 均不存在即视为完成

### 场景 3：同用户双 session 事件串流

- 原因：未按 run 精确过滤
- 缓解：按 `tenant_id/workspace_id/session_id/run_id` 精确匹配

### 场景 4：非幂等工具副作用窗口崩溃

- 原因：远端副作用已发出，本地未能确认
- 缓解：`manual_uncertain`

### 场景 5：active 用户缺 default workspace

- 原因：bootstrap 中断、复合 FK 失效或 key engine gate 错配
- 缓解：readiness fail closed

### 场景 6：用户 Secret 错误时静默消耗平台凭据

- 原因：把“用户配置错误”错误解释为“用户未配置”
- 缓解：SecretResolver 只在记录不存在时允许 fallback；解密、格式或上游认证失败均显式报错

### 场景 7：删除恢复与清除器竞态

- 原因：恢复请求和到期 purge 同时推进同一 job
- 缓解：锁定同一 user/job；恢复仅接受 `scheduled` 且未到期，purge 以 version CAS 抢占为 `running`

## 18. 测试矩阵

### 18.1 Unit

- TenantContext 只从认证身份构建，客户端 tenant/workspace 覆盖被拒绝
- 任一 TenantUnitOfWork 生命周期只使用一个 connection/transaction handle
- approval/execution 合法组合矩阵
- 同 tenant 跨 scope optional FK 拒绝
- run-only checkpoint fencing
- 每 run 最多一个非终态工具
- 旧 runtime fencing 写全失败
- `pending_purge + NULL` 合法，`active + NULL` fail
- phase payload 缺字段 -> `blocked_corrupt`
- `AES-256-GCM` / AAD 固定测试向量
- keyring source XOR
- unknown provider readiness fail
- key rotation gate：已用旧 key_version 引用数非 0 时不得移除
- retention 边界：`-1/31/non-int` fail startup，`0/7/30` 通过
- DB clock vs app clock skew：人为扭曲 app wall clock 不影响 lease/purge 结果
- SecretResolver：记录不存在时可 fallback；记录存在但错误时禁止 fallback
- `auth_epoch` token 失效与 `deletion_recovery` token 的 audience/scope 限制
- WorkspaceResolver 的绝对路径、`..`、symlink containment 与删除根校验

### 18.2 Integration

- SQLite/MySQL 同一 Core + Alembic 契约测试
- checkpoint 复合归属与 optional composite FK
- purge 循环 FK 顺序在两库都成立
- SQLite app/test/Alembic 三处 `foreign_keys=ON`
- SQLite migration 后 `foreign_key_check` 通过
- awaiting_user checkpoint 后 eviction，再审批恢复
- SQLite concurrent CAS under `BEGIN IMMEDIATE`
- MySQL `SELECT ... FOR UPDATE` 行锁与非终态工具不变量
- 显式 `multiclaw db upgrade` 可从 empty 到 head；API 在 schema 落后时只 fail readiness，绝不自动升级
- 删除请求与清除器/恢复请求竞态只有一方成功；旧 JWT 在 `auth_epoch` 增加后全部失效
- 用户 Secret 失效时平台凭据调用数为 0；删除用户 Secret 后仅在部署允许时恢复 fallback

### 18.3 E2E

- 两个用户并发执行 session/chat/approval/secret API，跨租户成功访问数为 0
- 正常重启后 approval 可继续，不残留 `uncertain/blocked_*`
- 同 run 串行工具不变量
- 同用户双 session 不串 SSE
- 非目标 `run_id` 的 SSE 误投递数为 0
- 越权或不存在 `session_id` 返回 404，且不会静默创建新 session
- 等待审批的 runtime 被驱逐后批准并恢复成功
- `manual_uncertain` 工具自动重放次数为 0
- 删除请求在 active execution 存在时返回 409；保留期内受限恢复成功，purge 开始后恢复被拒绝
- purge 完成后用户、job、workspace 文件、DB 行全部消失
- 两后端 CI matrix：empty->head Alembic、FK/schema introspection、repository contract、lease/CAS/recovery/purge 全通过

### 18.4 Observability

- split-brain 抢占记录可见
- `blocked_incompatible` / `blocked_corrupt` 可审计
- purge 重试次数仅保留匿名低基数聚合
- readiness 对 engine/version/provider/keyring/config 失败原因有脱敏结构化日志
- metrics labels 不含 tenant/session/run/request/email/path 等高基数字段
- secret canary 在日志、SSE、checkpoint、audit detail、trace 与前端状态中的命中数为 0

### 18.5 API、前端与安全

- 所有状态修改端点验证 CSRF 与 origin；credentialed CORS wildcard 被拒绝
- Secret API 永不返回明文或平台默认凭据
- 删除恢复 token 不能访问 chat/session/secret API
- 前端执行 ESLint、TypeScript production build，并人工验证审批恢复、Secret 设置和删除恢复流程

## 19. 可测验收标准

1. 同一 run 同时最多一个非终态 approval/execution；违反尝试必须被应用逻辑或 DB 契约拒绝。
2. `agent_runs.run_status='completed'` 时，该 run 下非终态 `tool_executions` 数必须为 `0`。
3. 跨同 tenant 不同 workspace/session/run 的 optional composite FK 错绑必须被 DB 拒绝。
4. run-only checkpoint 在旧 fencing_token 下写入成功数必须为 `0`。
5. 旧 runtime 在 lease 被抢占后，对 run/execution/checkpoint 的任何写入成功数必须为 `0`。
6. `pending_purge + NULL default_workspace_id` 自检不得失败；`active + NULL default_workspace_id` 自检必须失败。
7. purge 顺序在 SQLite/MySQL 上都能完成，无 cascade 依赖。
8. SQLite 的 app/test/Alembic 三类连接都必须启用 `foreign_keys=ON`；migration 后 `foreign_key_check` 必须通过。
9. phase payload 缺必需字段时，恢复结果必须为 `blocked_corrupt`。
10. `AES-256-GCM`、32-byte key、12-byte nonce、16-byte tag 与 AAD 固定测试向量必须全部通过；ciphertext 行交换必须失败。
11. keyring source 必须满足 XOR；同时配置或均缺失都必须 startup fail。
12. 未知 `key_provider_name`、缺失已用 `key_version`、POSIX keyring 文件 group/world readable 都必须 readiness fail。
13. `deletion.retention_days` 仅允许 `0..30` 整数；越界或非整数必须 startup fail；`0` 只能触发异步 purge 资格，不得在请求内同步递归删除。
14. 人为制造 app clock 偏斜时，lease/CAS/purge 结果不得偏离 DB clock 预期。
15. SQLite 并发 CAS 与 MySQL `FOR UPDATE` 锁行为都必须通过 CI gate。
16. 每个 PR 必须通过两后端 CI matrix：Alembic upgrade、FK/schema introspection、repository contract、lease/CAS/recovery/purge。
17. 当前开发阶段设计中不得要求历史认领、兼容读写或回填迁移。
18. 任一 UoW 生命周期内 repository 使用的 connection handle 数必须为 `1`，跨 repository 失败必须整体回滚。
19. 两用户并发 E2E 中跨租户读、写、审批、Secret 访问成功数必须为 `0`；非目标 SSE `run_id` 事件数必须为 `0`。
20. 用户 Secret 存在但解密/格式/上游认证失败时，平台 fallback 调用数必须为 `0`。
21. 删除请求提交后旧 JWT 成功访问数必须为 `0`；恢复 token 对非恢复端点成功访问数必须为 `0`。
22. API 在数据库不位于 Alembic head 时必须 readiness fail，且自动执行 migration 的次数必须为 `0`。
23. Secret canary 在日志、SSE、checkpoint、audit detail、trace 与前端状态中的命中数必须为 `0`。
24. 越权或不存在 session ID 均返回 `404`；该请求造成的新 session 数必须为 `0`。
25. 前端 lint 与 production build 必须通过，并记录审批恢复、Secret 设置和删除恢复的人工浏览器验证。

## 20. API 与安全契约

### 20.1 作用域规则

- `tenant_id` 永远来自认证身份，不接受请求体、header 或 query 覆盖。
- v1 的 `workspace_id` 来自 `users.default_workspace_id`，不开放创建或切换 API。
- 客户端提交 `session_id/run_id/approval_id` 时，服务端必须验证完整 scope。
- 外租户资源与不存在资源统一返回 `404`，防止 ID 枚举。
- 非法或越权 `session_id` 不再静默创建新 session；只有未提交 `session_id` 时才创建。

### 20.2 端点

| Endpoint | Contract |
| --- | --- |
| `POST /api/chat` | 验证或创建 session，创建不可复用 `run_id`，以 SSE 返回精确 run scope 事件 |
| `GET /api/sessions` | 仅返回当前 tenant/default workspace 的 session |
| `GET /api/sessions/{id}/messages` | 完整 scope 验证后返回消息 |
| `GET /api/approvals/{id}` | 返回当前用户可见审批状态 |
| `POST /api/approvals/{id}/decision` | 通过 version CAS 提交 approve/reject；不直接执行工具 |
| `GET /api/secrets` | 只返回元数据和遮蔽值 |
| `PUT/DELETE /api/secrets/{provider}/{name}` | 写入或删除用户 secret，不返回明文 |
| `POST /api/secrets/{provider}/{name}/test` | 在当前调用范围测试凭据并全面脱敏 |
| `POST /api/account/deletion` | 近期重新认证后发起延迟删除 |
| `GET /api/account/deletion` | 受限返回状态和 `purge_after` |
| `POST /api/account/deletion/recover` | 使用单用途恢复 token 取消尚未开始的清除 |
| `GET /api/health/live` | 仅检查进程存活 |
| `GET /api/health/ready` | 验证 DB/schema/FK/keyring/default workspace 等不变量 |

SSE 首个控制事件返回 `session_id` 与 `run_id`；后续事件全部带完整 scope。消息/checkpoint/终态必须先提交数据库，才能发送声明成功的最终事件。

### 20.3 错误语义

| HTTP | 场景 |
| --- | --- |
| `401` | 未认证或 JWT `auth_epoch` 已失效 |
| `403` | `pending_purge` 用户尝试访问非恢复端点 |
| `404` | 资源不存在或不属于当前完整 scope |
| `409` | CAS 冲突、非法状态转移、存在活跃 run |
| `410` | Approval 已过期或删除恢复窗口已关闭 |
| `422` | 请求或工具参数校验失败 |
| `429` | 当前用户并发/配额超限 |
| `503` | Runtime 无安全容量、DB/schema 暂不可用 |

### 20.4 Cookie 与 CSRF

- Cookie 使用 `HttpOnly`、`Secure` 和部署适用的 `SameSite`。
- 所有状态修改接口校验 `Origin/Referer` 和 CSRF token。
- CORS 不允许带凭据的任意 origin 通配符。
- Approval、Secret、删除和恢复端点必须通过 CSRF；Secret 修改与删除要求近期重新认证。

## 21. 配置契约

```toml
[deployment]
profile = "standalone"

[database]
driver = "sqlite"               # sqlite | mysql
url = "sqlite+aiosqlite:///..."
migration_mode = "validate"     # API 只验证，不升级
sqlite_busy_timeout_ms = 5000

[auth]
jwt_signing_key_file = "/run/secrets/multiclaw-jwt-key"

[workspace]
root = "./data/workspaces"

[runtime]
max_resident_tenants = 32
idle_ttl_seconds = 900
max_concurrent_runs_per_tenant = 2

[workflow]
heartbeat_ms = 5000
lease_ttl_ms = 20000
max_checkpoint_payload_bytes = 262144

[secrets]
allow_platform_fallback = true
keyring_file = "/run/secrets/multiclaw-keyring.json"

[deletion]
retention_days = 7
```

- JWT signing key、MySQL DSN、平台 API key 和 keyring 不写入普通 TOML，应通过环境变量或只读 Secret 文件注入。
- 达到 tenant 并发上限返回 `429`。
- 达到 resident runtime 上限时先驱逐已 checkpoint 的 idle runtime；无安全驱逐对象则返回带 `Retry-After` 的 `503`。
- 任何配额都不能通过终止持有有效 lease 的 runtime 实现。

## 22. Readiness 与可观测性

### 22.1 Readiness

接收流量前必须验证：

- 数据库可连接，类型/版本/engine/transaction 配置满足本规范。
- Alembic revision 恰好等于 head。
- SQLite app/test/Alembic connection 均启用 FK；MySQL 使用 InnoDB/UTC/READ COMMITTED。
- `active` 用户均有合法默认 workspace，不存在 scope FK 损坏。
- keyring 来源满足 XOR；所有已用 key version 存在；provider 名已知。
- workspace 根目录存在并满足权限要求。

Liveness 不执行上述依赖检查，避免依赖故障触发无意义重启循环。

### 22.2 Logs、traces 与 metrics

高基数字段只进入脱敏后的结构化日志、trace/span 和删除前的应用审计：

- `tenant_id/workspace_id/session_id/run_id`
- `approval_id/execution_id/runtime_instance_id/fencing_token`

metrics label 只允许低基数维度，例如 `backend/profile/operation/status/error_class/recovery_strategy`。禁止把 tenant、session、run、request、email、provider name 或路径作为 label。

必须监控：scope FK 拒绝、旧 fencing 写入、`uncertain`、`blocked_*`、runtime 容量、approval 恢复、purge 重试、migration revision、keyring 缺失、SQLite busy 与 MySQL lock timeout。所有输出通过统一 secret redactor。

## 23. 交付边界与顺序

### 23.1 推荐顺序

1. SQLAlchemy engine、Alembic baseline、Core schema、双方言 CI 与 DB clock。
2. TenantContext、TenantUnitOfWork、复合 FK 和默认 workspace bootstrap。
3. Session/Memory repository 改造，删除 `chat_sessions.user_id`。
4. per-user RuntimePool、EventRouter 与 WorkspaceResolver。
5. run/approval/execution/checkpoint、lease、fencing 与故障恢复。
6. BYOK、deployment keyring、严格 fallback 与轮换。
7. `auth_epoch`、删除恢复、retention 与幂等 purge。
8. 前端 Secret/审批恢复/删除界面、故障注入和全链路安全验收。

不能在 UoW、完整 scope FK 和运行时隔离完成前对外宣称支持多租户。

### 23.2 v1 非目标

- 组织、成员、邀请、RBAC 与跨用户共享
- 多 workspace 创建/切换 UI
- 同实例按用户选择数据库、SQLite/MySQL 双写
- cluster、分布式 runtime、远程 workspace
- KMS/Vault provider
- 应用级 superadmin/break-glass
- 同一 run 内并行工具
- 历史开发数据迁移
- API 进程自动执行 migration
- 从不可变历史备份逐条清除用户数据

## 24. ADR

### Decision

采用 `user=tenant`、默认 workspace、scope-bound UoW、`SQLAlchemy 2.x Core async + Alembic async env + sqlite+aiosqlite/mysql+aiomysql + cryptography AESGCM` 的 standalone 多租户架构；使用 per-user RuntimePool、结构化 continuation、单 run lease 与串行工具。Secret provider 在 v1 固定为 `deployment-keyring`；时间统一为 DB clock epoch ms；迁移通过显式命令执行；账号采用可恢复的延迟物理清除。

### Drivers

- 需要消除 driver/AAD/provider/time/transaction 的实现歧义。
- 需要使 CI 与 readiness 能对版本、engine、FK、keyring、clock、lease 协议直接给出 fail-closed 结论。
- 需要保持 V5 已批准的恢复、purge、scope FK 协议不被新的技术可选项稀释。

### Alternatives

- 可选 key provider：Rejected
- canonical JSON AAD：Rejected
- app wall clock 参与 lease/purge 判断：Rejected
- execution 独立 lease：Rejected
- 同 run 多工具并行：Rejected
- 原生双驱动 + 自研迁移：Rejected
- ORM：Rejected

### Why chosen

- 这些常量一旦在设计期锁死，后续实现和 CI 才能验证同一协议，而不是验证一组可变实现。
- 单 run lease + DB clock + AESGCM + Core/Alembic 是 v1 足够严格且可移植的最小稳定集。

### Consequences

- v1 不接受其他加密 provider、其他 AAD 编码、其他时间源、其他 ORM/driver 选择。
- 实现阶段必须先完成 keyring gate、DB clock helper、SQLite FK gate、MySQL row lock gate 与固定测试向量。
- 实现阶段必须引入 `auth_epoch` 并让所有受保护请求检查数据库中的账号状态，不能只验证 JWT 签名。
- 未来若扩展 KMS/Vault/其他编码，必须作为新 ADR 和新 schema/compat protocol 进入，不得隐式兼容。

### Follow-ups

- 明确 native tool 与 MCP tool 的 `tool_kind/recovery_strategy/idempotency_key` 声明接口
- 明确固定测试向量文件在仓库中的组织位置
- 明确开发期 reset/bootstrap 命令的运维入口
