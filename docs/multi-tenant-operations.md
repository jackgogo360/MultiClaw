# 多租户运维手册

本文说明 MultiClaw 当前单机部署的数据库发布、密钥、健康门禁、清除器和轮换操作。部署拓扑见[部署指南](deployment.md)，字段见[配置参考](configuration.md)，边界见[安全模型](security-model.md)，具体故障见[故障排查](troubleshooting.md)。

## 部署输入

- 一个部署只配置一个 `database.driver` 和一个 `database.url`。
- `sqlite` 必须使用 `sqlite+aiosqlite` URL；`mysql` 必须使用 `mysql+aiomysql` URL。
- 不在同一进程中同时配置两个后端，不做双写，也不把另一数据库当作应用内热备。
- 数据库凭据、JWT key、Secret keyring 和邮件 API key 由部署 secret 渠道提供，不写入变更记录、日志或仓库。

## 数据库发布流程

在启动或重新引入 API 流量前：

1. 对生产同等数据执行一致性备份并验证恢复。
2. 设置目标部署的数据库环境。
3. 运行 `uv run multiclaw db upgrade`。
4. 运行 `uv run multiclaw db check`。
5. 启动 API，验证 live、ready 和独立原生沙箱门禁。
6. 只有全部必需门禁通过后才放量。

API 不自动执行迁移。`/api/health/ready` 对 schema drift fail closed，但它不是迁移 hook。当前产品未对外发布，不计划历史租户回填或旧产品数据迁移；新的 schema 变更仍必须新增 Alembic revision，不能改写冻结基线。

## JWT 签名密钥

必须恰好选择一个来源：

- `MULTICLAW_AUTH_JWT_SIGNING_KEY`；
- `auth.jwt_signing_key_file`。

key material 至少 32 字节。文件必须由运维方管理、拒绝符号链接、权限不包含 group/other 位，且位于租户不可控制的位置。更换 key 会使既有 session/recovery token 失效，并改变验证码摘要派生；轮换前应明确用户影响。

## Secret keyring

必须恰好选择一个来源：

- `MULTICLAW_SECRETS_KEYRING_B64`；
- `secrets.keyring_file`。

keyring 必须保留一个活动版本和数据库仍引用的全部旧版本。部署变更记录可以写 active version、保留版本集合和引用计数，但不能写 raw key、token 或真实文件路径。

keyring 文件采用与 JWT 文件相同的 operator-managed、owner-only 策略。数据库备份必须与对应 keyring 版本集合一起纳入恢复演练。

## 健康与流量门禁

- `/api/health/live` 只证明进程响应。
- `/api/health/ready` 检查数据库连接/版本、Alembic head、schema/外键完整性、工作区权限、active 默认工作区关系，以及 keyring 加载和引用版本。
- MySQL 还要求 Community major 8、最低 `8.0.36`、InnoDB、`utf8mb4`、UTC-compatible session time zone 和 `READ COMMITTED`。

ready 失败时移出流量并调查，不要原地自动迁移或关闭门禁。

当前公开 ready 不包含 sandbox readiness。部署必须额外运行目标平台 native gate，并核对 startup probe/registration skipped。ready=200 不能替代沙箱证据。

## 清除 worker

单机 lifespan 在数据库连接成功后启动 workflow recovery 和认证清理 worker；账号清除 worker 仅在启动 sandbox readiness 健康时启动。

- worker 以可取消的 batch polling loop 运行；`retention_days=0` 仍通过异步 worker 清除。
- 监控低基数 `multiclaw_purge_retry_total`。
- 停滞时检查 deletion job 的 `status`、`worker_id`、`lease_expires_at`、`heartbeat_at`、`attempt_count` 和 `last_error`，同时检查阻塞活动、数据库和工作区权限。
- 不手工乱序级联删除租户表。作用域清除路径维护删除顺序、lease 和 fencing。
- purge 事件、备份和 incident notes 不包含邮箱、真实路径或 Secret。

如果 sandbox startup readiness 不健康，清除 worker 不会启动，即使公开 `/api/health/ready` 仍可能通过。必须对此设置单独部署告警。

## Secret key 轮换

当前没有产品化 rotation CLI。轮换只能在受控维护环境中，由 code-reviewed 一次性脚本或内部维护服务调用 [`SecretRotationService.rotate_batch()`](../src/multiclaw/secrets/rotation.py)：

1. 把新 key version 加入 keyring。
2. 将新版本设为 active，保留所有旧版本。
3. 部署包含新旧版本的 keyring，并确认 readiness。
4. 以受控批次执行 rotation，记录每批 `rotated`、`skipped`、`failed`。
5. 任一 batch 失败时停止，保留旧版本，调查 CAS 冲突或 envelope 问题。
6. 统计数据库 key version 引用并完成备份恢复验证。
7. 只有旧版本引用为零时才从后续 keyring 删除。

轮换脚本不得输出 plaintext、nonce/ciphertext 全量或 raw key。

## SQLite 运维

- 数据库文件放在持久化存储，服务用户独占写权限。
- 文件型连接启用 WAL、foreign keys、busy timeout 和 `synchronous=NORMAL`。
- 使用 SQLite backup API、停止写入后的拷贝或保证一致性的 volume snapshot。
- 不把普通活动写入期间的文件复制视为可恢复备份。
- 容量、锁等待或备份窗口超出单机 SQLite 能力时，评估在发布前选择 MySQL；当前不提供产品内在线迁移工具。

## MySQL 运维

- 使用 MySQL Community major 8，最低 `8.0.36`；MariaDB/Percona 不在当前支持范围。
- 所有表使用 InnoDB，database/table charset 使用 `utf8mb4`。
- session time zone 保持 `+00:00`/UTC-compatible，transaction isolation 为 `READ COMMITTED`。
- 连接用户只授予应用 schema 所需权限；迁移账户可以与运行账户分离。
- 备份与 point-in-time recovery 由 MySQL 运维体系提供，并定期在隔离实例演练。

## 事故处置原则

- 先阻断新流量，再保留日志、指标、数据库与 keyring 版本证据。
- 不在工单中复制用户内容、Secret、认证 header、数据库 URL 或真实工作区路径。
- stale fence、scope rejection、approval conflict 和 purge retry 都应按作用域 ID/错误类调查，不用全局数据导出定位。
- 需要人工脚本时先 code review、备份、dry-run/只读统计，并限定 tenant/job/batch 作用域。

## 当前非目标

- 历史产品数据迁移或租户回填；
- SQLite/MySQL 双写与在线切换；
- 工作区切换器；
- 多副本集群；
- KMS/Vault 集成；
- superadmin 流程；
- 同一 run 内的变更工具并行执行。
