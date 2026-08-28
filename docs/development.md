# 开发指南

## 目录结构

```text
src/multiclaw/          Python 运行时与 FastAPI 服务
├── agent/              Agent 循环、上下文与韧性
├── api/                已认证业务接口
├── auth/               邮箱认证、JWT 与中间件
├── governance/         审批、审计和原生沙箱
├── mcp/                MCP 配置、传输、客户端与工具适配
├── runtime/            租户 RuntimeFactory/RuntimePool
├── secrets/            keyring、envelope、解析与轮换
├── storage/            SQLAlchemy schema、方言、仓储与 UoW
├── tenancy/            TenantContext 与工作区解析
└── workflow/           租约、检查点、审批和恢复
frontend/src/           React 19 / assistant-ui 前端源码
tests/                  pytest 单元与集成测试
alembic/                schema 迁移
docs/                   正式专题文档与设计记录
scripts/                仓库维护脚本
```

## 安装

```bash
uv sync
cd frontend
npm ci
cd ..
```

Python 依赖由 `pyproject.toml` 和 `uv.lock` 固定，前端由 `frontend/package-lock.json` 固定。不要用未记录的全局包掩盖 lockfile 问题。

## 后端热重载

先按[入门指南](getting-started.md)设置 JWT、keyring、邮件模式和新 SQLite URL，再运行：

```bash
uv run multiclaw db upgrade
uv run multiclaw db check
uv run uvicorn multiclaw.server:app --host 127.0.0.1 --port 15800 --reload
```

FastAPI 文档位于 `/docs`，OpenAPI schema 位于 `/openapi.json`。应用日志写入 `~/.multiclaw/logs/multiclaw.log`，按午夜轮转并保留 30 个归档；终端仍会显示 Uvicorn 日志。

应用不会在启动时自动迁移。切换数据库 URL 后重新执行 `db upgrade` 和 `db check`，否则 readiness 会以 `schema_revision` 拒绝放量。

## 前端开发

```bash
cd frontend
npm run dev
```

Vite 在本地提供 HMR，并把 `/api` 代理到 `http://127.0.0.1:15800`。前端必须通过 `src/lib/api.ts`、`src/lib/security.ts` 和既有 store 访问接口，保持 cookie、CSRF 与错误处理一致。

验证前端：

```bash
cd frontend
npm run lint
npm run build
```

build 输出到 `src/multiclaw/static/`，供 FastAPI 根路径与 `/assets` 托管。该目录是生成产物；不要手改 `index-*.js`、`index-*.css` 或其他哈希文件。构建后用 `git status --short` 确认没有意外 bundle 漂移。

## 便利脚本的副作用

`./start.sh` 同时启动后端和前端，但会：

- 对 `15800`、`5173` 上的进程执行强制终止；
- 把两个服务绑定到 `0.0.0.0`；
- 把 PID 写到 `/tmp/multiclaw-*.pid`；
- 在 macOS 尝试打开浏览器；
- 阻塞并跟踪 `~/.multiclaw/logs/multiclaw.log`。

`./stop.sh` 先按 PID 文件，再按端口停止进程。两者只适合已确认端口归属的本地环境，不是生产进程监督工具。

## 配置覆盖

默认读取仓库根目录 `multiclaw.toml`。嵌套环境变量使用双下划线，例如：

```bash
export MULTICLAW_APP__DEBUG=true
export MULTICLAW_DATABASE__DRIVER=sqlite
export MULTICLAW_DATABASE__URL=sqlite+aiosqlite:///data/multiclaw-dev.db
```

环境变量覆盖 TOML，显式传给 `Settings(...)` 的构造参数优先级更高。完整字段、安全分类和特殊密钥来源见[配置参考](configuration.md)。

### MCP 与 skills

- `MULTICLAW_MCP__ENABLED=false` 可在排查时关闭 MCP；`mcp.config_path` 为空时加载仓库默认 `.mcp.json`/相关配置逻辑。
- `MULTICLAW_SKILLS__ENABLED=false` 可关闭 skill discovery；额外目录通过 JSON 数组形式的 `MULTICLAW_SKILLS__EXTRA_DIRS` 传入。
- MCP 配置不会自动把所有 server 连接到所有租户。运行时工厂会按允许的配置和沙箱 readiness 构建租户能力，失败能力会进入 readiness/日志而不是静默扩大权限。

## 调试就绪性

```bash
curl -sS http://127.0.0.1:15800/api/health/live
curl -sS http://127.0.0.1:15800/api/health/ready
```

`live` 只表示进程响应；`ready` 的 `checks_failed` 是定位入口。常见值包括 `db_connectivity`、`backend_version`、`schema_revision`、`sqlite_foreign_keys`、`schema_integrity`、`workspace_root_permissions`、`keyring`、`mysql_time_zone`、`mysql_isolation`、`mysql_innodb` 和 `mysql_charset`。

原生沙箱探测失败可能限制租户工具能力；生产环境不要用 `governance.sandbox.mode=host_unsafe_dev_only` 绕过。该模式只允许与 `app.debug=true` 组合，用于明确接受风险的本地调试。

## 测试与静态检查

```bash
uv run pytest -q
uv run pytest tests/test_server.py -k test_name -q
uv run python scripts/check_docs.py
uv lock --check
cd frontend && npm run lint && npm run build
```

SQLite/default测试应始终执行。MySQL 和原生沙箱用例需要外部条件时可以按已定义 marker/fixture 跳过，不能把未知失败改成无条件 skip。测试矩阵和 release evidence 见[测试指南](testing.md)。

## 编码与提交约定

- Python 使用四空格、类型标注、`snake_case` 函数/模块和 `PascalCase` 类；复用 async UoW 与 Settings 模式。
- TypeScript 使用两空格、strict 类型、`PascalCase` 组件、`camelCase` 函数和 `@/` alias。
- 行为变更先增加回归测试；优先复用既有工具和边界，不新增依赖或无必要抽象。
- 不修改冻结迁移 `alembic/versions/20260815_0001_multi_tenant_baseline.py`。后续 schema 变化必须新增迁移。
- 不手工编辑 `src/multiclaw/static/` 的哈希资源。
- 提交必须遵循 [CONTRIBUTING.md](../CONTRIBUTING.md#分支与提交) 中的 Lore trailer 要求。

