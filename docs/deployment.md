# 部署指南

## 支持拓扑

MultiClaw `0.1.0` 只支持单机 `standalone`：一个 FastAPI 进程连接一个 SQLite 或 MySQL 数据库，并在进程内管理 RuntimePool、workflow recovery、认证清理和账号清除 worker。React 静态 bundle 由同一 FastAPI 进程托管。

不支持多副本、滚动多实例、共享事件总线、跨进程 run lease owner 协调或数据库双写。即使数据库是远程 MySQL，也不要同时启动多个应用副本处理同一部署。

## 生产前置条件

- Python `>=3.12` 与由 `uv.lock` 安装的依赖。
- 已构建的前端 `src/multiclaw/static/`。
- 二选一：持久化 SQLite volume，或 Oracle MySQL 主版本 8（最低 `8.0.36`、InnoDB、`utf8mb4`；支持 commercial 版本标识）。
- 受控工作区根目录，服务用户拥有读/写/执行权限，其他非受信用户不能写入。
- 稳定 JWT 签名密钥、版本化 Secret keyring、真实邮件 provider 凭据。
- macOS Seatbelt 或 Linux nsjail 原生沙箱，并在真实宿主机完成对应测试。
- HTTPS reverse proxy；前端 origin 与 `app.allowed_origins` 精确一致。

## 构建发布产物

```bash
uv sync --locked
cd frontend
npm ci
npm run lint
npm run build
cd ..
uv run python scripts/check_docs.py
uv run pytest -q
```

不要在生产主机临时手改生成 bundle。应从已验证 commit 构建或分发不可变产物，并记录 Python、Node、数据库与 commit 信息。

## 配置与密钥输入

普通部署参数可以放入运维管理的 TOML；凭据由进程监督器、容器 secret 或权限受限文件注入。至少配置：

- `app.debug=false` 与实际 HTTPS `app.allowed_origins`。
- `deployment.profile=standalone`。
- `database.driver`、`database.url`、workspace root 和运行时容量。
- JWT key 的环境变量或文件来源，恰好一个。
- Secret keyring 的 base64 环境变量或 JSON 文件来源，恰好一个。
- 活动邮件 provider 的 API key 与已验证发件地址。
- sandbox `mode=auto` 和对应原生后端路径/profile。

完整字段见[配置参考](configuration.md)，安全边界见[安全模型](security-model.md)。

## 数据库选择

### SQLite

```bash
export MULTICLAW_DATABASE__DRIVER=sqlite
export MULTICLAW_DATABASE__URL=sqlite+aiosqlite:////var/lib/multiclaw/multiclaw.db
```

数据库与工作区应位于持久化存储。应用会为文件型 SQLite 启用 foreign keys、busy timeout、WAL 和 `synchronous=NORMAL`。文件备份必须使用能保证一致性的 SQLite backup、停止写入后的副本或平台一致性快照；普通热复制不构成可靠备份。

### MySQL

```bash
export MULTICLAW_DATABASE__DRIVER=mysql
export MULTICLAW_DATABASE__URL="<mysql-url-from-secret-store>"
```

注入值必须使用 `mysql+aiomysql` scheme。服务连接会设置 session time zone `+00:00` 和 `READ COMMITTED`；readiness 还要求 Oracle MySQL 主版本 8、版本不低于 `8.0.36`、所有表为 InnoDB、数据库/表为 `utf8mb4`。commercial 版本标识可接受，MariaDB 和 Percona 当前不在支持范围。

## 备份与恢复演练

每次发布前：

1. 对生产同等规模的数据执行一致性备份。
2. 把备份恢复到隔离数据库或隔离 volume。
3. 对恢复副本运行当前或待发布代码的 `multiclaw db check`，并执行关键只读检查。
4. 记录备份时间、恢复耗时、revision、keyring 版本集合和验证结论；记录中不包含凭据或用户数据。

数据库备份必须与 keyring 备份配对。只有 ciphertext 而没有仍被引用的 key 版本无法恢复 Secret；只有 keyring 而没有数据库也不能恢复租户元数据。

## 发布顺序

对目标数据库显式执行：

```bash
uv run multiclaw db upgrade
uv run multiclaw db check
```

两条命令都成功后再启动 API。应用不会自动迁移，readiness 失败也不是触发在线迁移的信号。

推荐启动形态（由 systemd、launchd 或容器 supervisor 管理）：

```bash
uv run uvicorn multiclaw.server:app --host 127.0.0.1 --port 15800
```

reverse proxy 终止 TLS、限制请求大小/超时、保留 SSE streaming，并把可信客户端转发到应用。不要使用 `start.sh` 管理生产进程。

## 健康与放量

```bash
curl --fail http://127.0.0.1:15800/api/health/live
curl --fail http://127.0.0.1:15800/api/health/ready
```

- live 仅作为进程存活探测。
- ready 是数据库/schema/workspace/keyring 流量门禁；非 200 时不得放量。
- 当前公开 ready payload **不包含沙箱 readiness**。部署流水线必须在放量前单独执行对应原生沙箱测试，并核对启动日志/审计中的 probe 与被跳过能力。不能仅凭 ready=200 推断 shell、code_exec 或 stdio MCP 已安全注册。

启动期间会执行 sandbox probe。probe 不健康时危险本地能力不会回退到宿主执行；账号清除 worker 也不会启动。该限制必须纳入运维告警与部署验收。

## 静态前端

`npm run build` 将 `index.html` 与哈希资源写入 `src/multiclaw/static/`。FastAPI 在 `/` 返回 index，在 `/assets` 托管 bundle，并在 `/multiclaw.png` 返回项目图像。

部署验证至少包括：

- `/` 返回当前 release 的 HTML；
- HTML 引用的哈希资源均为 200；
- 浏览器通过 `/api` 调用同源后端，cookie 与 CSRF 正常；
- reverse proxy 不缓冲或截断 `/api/chat` SSE。

## 监控

日志位于 `~/.multiclaw/logs/multiclaw.log`，按日轮转。集中采集时继续执行凭据和路径脱敏。重点监控：

- readiness 状态与 `checks_failed`；
- runtime capacity/unavailable 和 `Retry-After`；
- stale fence、scope FK rejection、approval recovery；
- purge retry 和 deletion job 租约；
- keyring 引用缺失与 rotation batch 结果；
- sandbox probe、registration skipped、unsafe fallback（生产出现即告警）。

## 回滚

代码与 schema 发布前准备上一个已知良好 artifact 和恢复方案：

1. readiness 或原生门禁失败时不要放量，停止新 artifact。
2. 若迁移仍兼容旧代码，重新部署上一个 artifact，并重跑其 `db check` 与健康门禁。
3. 若 schema 已不兼容，不要盲目 downgrade；按已演练步骤停止写入、恢复数据库与匹配 keyring，再部署旧 artifact。
4. 重跑后端、前端、数据库和对应原生沙箱验证后再恢复流量。

当前冻结基线迁移是 forward-only 起点，不提供历史产品数据迁移承诺。回滚依赖发布前备份/恢复演练，而不是在故障现场临时设计逆向迁移。

## 不支持的操作

- 同一数据库启动多个应用副本或蓝绿两套并行处理流量。
- SQLite/MySQL 双写、在线切换或自动复制应用数据。
- 在启动 hook 中自动运行 Alembic。
- 丢弃仍被数据库引用的 keyring 旧版本。
- 以 `host_unsafe_dev_only` 绕过生产沙箱失败。
- 把 `./start.sh`、开发 Vite server 或 mock 邮件作为生产服务。
