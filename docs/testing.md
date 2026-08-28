# 测试指南

MultiClaw 的默认质量门禁覆盖 Python、SQLite、可选 MySQL 分支、文档和 React 前端。原生沙箱测试是平台相关的显式 opt-in 门禁；缺少真实后端时应清楚记录 skip，而不是声称已经验证。

## 后端默认套件

```bash
uv run pytest -q
```

`pytest-asyncio` 使用自动 async 模式。默认命令始终执行普通单元/集成测试和 SQLite 分支；带 `sqlite`/`mysql` 参数的 fixture 在没有 `MULTICLAW_TEST_MYSQL_URL` 时只跳过 MySQL 参数，SQLite 参数仍必须通过。

聚焦运行示例：

```bash
uv run pytest tests/test_server.py -k health -q
uv run pytest tests/integration/test_tenant_e2e.py -q
uv run pytest tests/integration/test_workflow_faults.py -q
```

不要用无条件 skip 掩盖 SQLite 或普通平台失败。新增行为测试应命名为 `test_<behavior>`，优先验证真实边界和结果，而不是只断言 mock 调用次数。

## 文档门禁

```bash
uv run python scripts/check_docs.py
uv run pytest tests/test_documentation.py -q
```

检查器不访问网络、不写文件，验证正式文档清单、相对链接/标题锚点、关键 README 命令、Settings/API 覆盖、旧接口、未决标记和疑似凭据。修改路由、配置或开发命令时必须同步修改对应文档。

## 前端门禁

```bash
cd frontend
NPM_CONFIG_REGISTRY=https://registry.npmjs.org npm ci
NPM_CONFIG_REGISTRY=https://registry.npmjs.org npm audit
npm run lint
npm run build
cd ..
```

`npm ci` 验证 lockfile 可重复安装；`npm audit` 是发布前依赖风险证据；lint 检查 React/TypeScript；build 同时运行 TypeScript project build，并重建 `src/multiclaw/static/`。

构建后执行 `git status --short`。没有前端源码变化时，静态 bundle 不应出现 diff。仓库当前没有浏览器自动化测试 runner；UI 行为变化还需记录手工浏览器路径和截图。

## MySQL 8 测试

CI 使用 MySQL `8.0.36` service，并在 `sqlite`/`mysql` matrix 中分别执行：

```bash
uv run multiclaw db upgrade
uv run multiclaw db check
uv run pytest -q
```

本地 MySQL 需要：

1. 独立测试数据库，字符集为 `utf8mb4`，服务版本为 MySQL Community 8 且不低于 `8.0.36`。
2. 一个由本地 secret 管理或当前 shell 注入的 `mysql+aiomysql` URL；不要把用户名和密码写进文档、Git 或命令记录。
3. 将该 URL 同时提供给 `MULTICLAW_TEST_MYSQL_URL`。若需要把整个应用配置到 MySQL，再同步设置 `MULTICLAW_DATABASE__DRIVER=mysql` 和 `MULTICLAW_DATABASE__URL`。

```bash
export MULTICLAW_TEST_MYSQL_URL="<mysql-test-url-from-secret-store>"
uv run pytest tests/integration/test_tenant_e2e.py tests/integration/test_workflow_faults.py -q -rs
```

`-rs` 会显示 skip 原因。没有 URL 时看到 MySQL 参数 `skipped` 是预期，但只能报告“本地未执行 MySQL”；不能把 CI 历史结果冒充本次本地实测。

## 原生沙箱测试

pytest markers：

- `native_sandbox`：所有真实原生后端测试。
- `macos_sandbox`：macOS Seatbelt。
- `linux_sandbox`：Linux nsjail。

普通环境排除原生用例：

```bash
uv run pytest -m "not native_sandbox" -q
```

确认原生模块在未 opt-in 时按原因跳过：

```bash
uv run pytest tests/integration/test_sandbox_macos.py tests/integration/test_sandbox_linux.py -q -rs
```

真实 macOS 主机：

```bash
MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 \
  uv run pytest tests/integration/test_sandbox_macos.py -q -x
```

真实 Linux 主机：

```bash
MULTICLAW_RUN_NATIVE_SANDBOX_TESTS=1 \
MULTICLAW_NSJAIL_PATH=/usr/bin/nsjail \
  uv run pytest tests/integration/test_sandbox_linux.py -q -x
```

设置 opt-in 后，缺少 `/usr/bin/sandbox-exec` 或指定 nsjail 可执行文件属于失败，不是 skip。嵌套在另一层受限沙箱中的结果不能替代真实宿主机证据。平台先决条件和残余风险见[沙箱部署](sandbox-deployment.md)。

## 静态与仓库检查

```bash
uv lock --check
git diff --check
git status --short
```

安全敏感改动还应运行针对性凭据扫描、允许/拒绝路径测试和依赖审计。不要把真实 key、数据库 URL 或个人邮箱写入测试输出和 PR。

## CI 矩阵

| Job / 分支 | 环境 | 证明内容 |
|---|---|---|
| documentation | Ubuntu + Python 3.12 | 正式文档检查与文档契约测试 |
| backend / sqlite | Ubuntu + Python 3.12 | Alembic、SQLite 与默认后端套件 |
| backend / mysql | Ubuntu + Python 3.12 + MySQL 8.0.36 | Alembic、MySQL 方言和参数化分支 |
| frontend | Ubuntu + Node.js 22 | lockfile 安装、lint、TypeScript/build |
| native sandbox | 对应真实宿主机，当前不在通用 CI job | Seatbelt/nsjail 内核级行为；发布方单独保留证据 |

## 发布证据规则

提交或 PR 不应只写“测试通过”，而应记录：

- 实际命令、退出状态和关键通过/跳过摘要；
- 数据库后端、Python/Node 主版本和原生宿主平台；
- 未执行的 MySQL、浏览器或原生门禁及原因；
- `npm audit` 的 Critical/High 状态；
- 构建后是否产生预期外静态资源 diff；
- 冻结迁移 hash 是否保持不变（涉及多租户基线时）。

失败时继续修复并重跑相关门禁；不要用删除断言、扩大 skip 或关闭安全检查换取绿灯。

