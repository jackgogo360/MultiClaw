# 参与贡献

感谢你改进 MultiClaw。本项目当前处于 `0.1.0` 开发阶段，接口、配置和数据模型仍可能调整。提交改动前，请先确认问题边界并阅读相关的[架构说明](docs/architecture.md)与[安全模型](docs/security-model.md)。

## 开始之前

- 遵守[贡献者行为准则](CODE_OF_CONDUCT.md)。
- 安全漏洞不要提交公开 Issue，请按[安全策略](SECURITY.md)私密报告。
- 不要提交 API key、访问令牌、私钥、数据库、`.env`、运行日志或真实用户数据。
- 新依赖、数据库迁移、认证授权、沙箱和权限变更需要在 PR 中单独说明理由与风险。

## 开发环境

需要 Python 3.12 或更高版本、[uv](https://docs.astral.sh/uv/)、Node.js 22 和 npm。默认开发数据库是 SQLite；真实 MySQL 合同测试需要 MySQL 8.0.36 或更高的 8.x 版本。

```bash
uv sync
cd frontend
npm ci
cd ..
```

完整启动流程见[快速开始](docs/getting-started.md)，配置覆盖规则见[配置参考](docs/configuration.md)。

## 选择改动范围

- 保持改动聚焦，一个提交解决一个清晰问题。
- 优先复用现有模块、工具和类型，不为局部问题增加新框架。
- 修改行为前先补回归测试；修复缺陷时要证明测试在修复前失败、修复后通过。
- 不要手工编辑 `src/multiclaw/static/` 中的散列资源；运行 `npm run build` 生成它们。
- `alembic/versions/20260815_0001_multi_tenant_baseline.py` 是冻结基线，不得修改。
- 项目尚未正式发布，不为旧开发数据增加迁移或双写路径。

## 分支与提交

从目标基线创建说明性的分支，例如：

```text
feature/scoped-export
fix/readiness-timeout
docs/configuration-reference
```

提交标题描述“为什么需要这项改变”，正文记录约束、取舍和验证。建议使用 Git 原生 trailer：

```text
Prevent stale sessions from crossing tenant boundaries

The session lookup now derives its complete scope from TenantContext.

Constraint: Session identifiers are not globally trusted
Rejected: Filter only in the API layer | repositories would remain unsafe
Confidence: high
Scope-risk: narrow
Directive: Do not add unscoped session repository methods
Tested: Focused tenant isolation tests and full backend suite
Not-tested: Live MySQL when no service is available
```

## 测试要求

至少运行与你的改动直接相关的验证。提交较大功能或安全边界改动时运行完整门禁：

```bash
uv run python scripts/check_docs.py
uv run pytest -q
uv lock --check

cd frontend
npm ci
npm audit
npm run lint
npm run build
```

常用的聚焦命令：

```bash
uv run pytest tests/test_server.py -k test_name -q
uv run pytest tests/test_documentation.py -q
```

MySQL 参数化测试读取 `MULTICLAW_TEST_MYSQL_URL`。不要在 Issue、日志或 PR 描述中粘贴包含凭据的连接串。原生沙箱测试需要真实平台能力，具体命令见[测试指南](docs/testing.md)。

## Pull Request

PR 描述应包含：

1. 用户可见或架构层面的影响。
2. 关键设计选择以及拒绝的替代方案。
3. 配置、迁移、权限或安全变化。
4. 实际执行过的验证命令与结果。
5. 未验证的环境或已知限制。
6. UI 改动的截图或录屏，以及手工浏览器验证说明。

保持 PR 可审阅。不要混入无关格式化、生成物或本地环境文件。

## 代码风格

- Python 使用四空格、类型注解、`snake_case` 函数和 `PascalCase` 类，遵循现有异步与 Unit of Work 模式。
- TypeScript 使用两空格、严格类型、`camelCase` 函数和 `PascalCase` 组件，优先使用 `@/` 导入别名。
- 查询和写入必须从不可变租户上下文派生完整作用域；不得增加绕过作用域的便捷接口。
- 任何 Secret、认证、审计和错误输出都必须经过现有脱敏边界。

## 文档维护

行为、配置、CLI、API 或发布流程变化时，同一 PR 必须更新对应正式文档。文档以当前代码为事实源，不复制历史计划中的旧命令或测试数字。运行 `uv run python scripts/check_docs.py` 检查本地链接、旧接口和敏感信息形态。
