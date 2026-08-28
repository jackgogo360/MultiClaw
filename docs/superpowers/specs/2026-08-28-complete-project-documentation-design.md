# MultiClaw 项目文档体系设计

**日期：** 2026-08-28  
**状态：** 已批准  
**基线分支：** `feature/multi-tenant-implementation`  
**交付分支：** `docs/complete-project-documentation`

## 1. 背景

MultiClaw 已具备 Python 代理运行时、React 管理界面、多租户隔离、持久化工作流、Secret 加密、原生沙箱及 SQLite/MySQL 双存储支持，但仓库缺少根 README 和统一的正式文档入口。现有正式资料仅覆盖部分多租户运维与沙箱部署，前端 README 仍是 Vite 模板，贡献、安全、许可证、版本记录、开发、配置、API 和故障排查说明均不完整。

本设计建立一套面向开发者与贡献者的中文文档体系，使新贡献者能够理解项目边界、完成本地启动、定位模块、运行验证并提交符合规范的改动。部署运维内容作为必要参考保留，但不占用 README 主线。

## 2. 已绑定决策

| 决策项 | 选择 | 说明 |
|---|---|---|
| 文档语言 | 仅中文 | 当前不创建英文镜像或空占位文件；信息架构允许以后增补 |
| 主要读者 | 开发者与贡献者 | 使用者快速体验和部署运维作为次要读者 |
| 信息架构 | README 入口 + 专题文档 + 治理文件 | 不采用单体超长 README，也不引入文档站生成器 |
| 许可证 | Apache License 2.0 | 使用官方许可证文本，提供明确的专利授权条款 |
| 项目状态 | `0.1.0` 开发阶段、尚未正式发布 | 不把规划能力描述为已实现能力 |
| 存储范围 | SQLite 与 MySQL | 单个部署只配置一个后端；不描述双写或历史数据迁移 |
| 部署范围 | 单机部署 | 集群、工作区切换、KMS/Vault 和同一运行内并行工具不在当前范围 |
| 校验方式 | 标准 Markdown + 无第三方依赖的仓库内检查 | 不为文档新增运行时或构建依赖 |

## 3. 目标与非目标

### 3.1 目标

1. 让新开发者从干净检出开始，只依赖 README 和入门指南即可安装依赖、初始化数据库并启动开发环境。
2. 让贡献者能够从架构、目录、配置、API、测试、安全和故障排查文档中找到稳定的事实入口。
3. 让所有正式文档以当前代码、CLI、配置模型、路由和 CI 为事实源，消除模板内容和过时验证状态。
4. 通过自动化检查防止本地链接失效、关键文档缺失、敏感信息进入示例以及旧接口名称重新出现。
5. 为未来增加英文镜像或文档站保留清晰边界，但本轮不承担其维护成本。

### 3.2 非目标

1. 不建立 MkDocs、Docusaurus 或其他文档站发布流水线。
2. 不为每个内部类和函数生成 API 文档。
3. 不把 `docs/superpowers/` 下的设计和实施记录重写为用户手册。
4. 不修改产品行为、数据库模式、冻结迁移或公开 API。
5. 不声称本地完成真实 MySQL、Linux 原生沙箱或浏览器手工验证。
6. 不编写旧数据迁移、集群部署、超级管理员或多工作区切换指南。

## 4. 信息架构

```text
README.md                       项目入口、状态、能力、快速开始和导航
LICENSE                         Apache License 2.0 官方文本
CONTRIBUTING.md                 开发、分支、提交、测试和 PR 规范
SECURITY.md                     支持范围、漏洞报告和敏感信息规则
CODE_OF_CONDUCT.md              Contributor Covenant 2.1
CHANGELOG.md                    未发布版本和后续版本变更记录

docs/
├── README.md                   正式文档总索引
├── getting-started.md          环境准备、安装、初始化和首次启动
├── architecture.md             组件、多租户、工作流、事件和 Secret 架构
├── configuration.md            TOML、环境变量、默认值和安全属性
├── development.md              目录、开发流程、后端和前端调试
├── api.md                      认证、会话、聊天、审批、Secret 和健康接口
├── testing.md                  后端、前端、双数据库和原生沙箱测试矩阵
├── deployment.md               单机部署、数据库发布、健康检查和回滚
├── security-model.md           信任边界、租户隔离、CSRF、加密和沙箱
├── troubleshooting.md          启动、数据库、就绪性、邮件、MCP 和沙箱问题
├── multi-tenant-operations.md  中文多租户运维手册
└── sandbox-deployment.md       中文原生沙箱部署与平台限制
```

`docs/superpowers/` 保留为内部设计与实施记录。`docs/README.md` 可以索引其中的架构设计，但不把逐步实施计划放入新开发者的主导航路径。

## 5. 根 README 契约

根 README 控制在能够快速浏览的长度，按以下顺序组织：

1. 项目 Logo、名称、一句话定位和开发阶段警示。
2. 已实现能力摘要，包括代理运行时、工具、MCP、持久化工作流、多租户、BYOK Secret、沙箱和双数据库。
3. 支持范围表，明确 Python、Node、SQLite/MySQL、macOS/Linux 和单机部署边界。
4. 最短本地开发路径，包括依赖安装、密钥生成、数据库升级、后端/前端启动和访问地址。
5. 小型 Mermaid 架构概览，链接到完整架构文档。
6. 文档导航，按“开始、开发、接口、安全、部署、运维”分组。
7. 验证命令、贡献入口、安全报告入口、许可证和当前限制。

README 不重复配置表、完整端点表、沙箱策略细节或运维处置步骤。这些内容只在专题文档维护，并从 README 链接。

## 6. 专题文档契约

### 6.1 入门与开发

- `getting-started.md` 提供可复制执行的 SQLite 开发流程，区分必需步骤、可选集成和生产禁忌。示例只生成临时开发密钥，不包含真实凭据。
- `development.md` 解释 Python/React 目录、服务生命周期、静态资源构建、配置加载顺序、日志位置和常见调试入口。
- `CONTRIBUTING.md` 固化小提交、行为锁定、分支命名、测试要求、Lore commit trailer、PR 描述和 UI 截图要求。

### 6.2 架构与安全

- `architecture.md` 使用组件图和请求/运行时序图解释 FastAPI、TenantContext、TenantUnitOfWork、RuntimePool、EventRouter、WorkflowCoordinator、SecretResolver 和前端之间的关系。
- `security-model.md` 以信任边界和不变量为中心，覆盖认证、CSRF、租户作用域、Secret envelope、日志脱敏、MCP 授权、沙箱、删除恢复和已接受风险。
- `SECURITY.md` 只描述公开支持策略和漏洞报告流程；私密漏洞通过 GitHub Security Advisories 报告，不要求公开披露或发送到个人邮箱。

### 6.3 接口与配置

- `configuration.md` 从 Pydantic Settings 模型生成手工维护的分组参考，记录 TOML 路径、环境变量写法、类型、默认值、约束和安全级别。
- `api.md` 记录公开路由分组、认证/CSRF 要求、主要请求响应语义、SSE 行为、状态码和运行时 OpenAPI 入口，不复制所有 Pydantic schema。
- API 文档只列入 schema 中公开的端点；兼容别名标记为兼容用途，不作为新集成首选。

### 6.4 测试、部署与故障排查

- `testing.md` 区分默认测试、真实 MySQL 参数化分支、前端 lint/build、文档检查以及 macOS/Linux 原生沙箱门禁，明确本地缺少外部条件时的跳过语义。
- `deployment.md` 固化“备份与恢复验证 → `db upgrade` → `db check` → 启动 → readiness 放量”的单机发布顺序。
- `multi-tenant-operations.md` 转为中文，保留数据库、密钥、清除器、轮换和后端限制，去除无法复现的历史验证数字。
- `sandbox-deployment.md` 转为中文，修正健康路径为 `/api/health/*`，将平台限制与可执行验证命令分离，不把历史机器状态写成长期事实。
- `troubleshooting.md` 使用“症状 → 原因 → 检查 → 修复”结构，避免建议关闭就绪性、安全校验或生产沙箱。

## 7. 事实源与防漂移规则

| 文档内容 | 唯一事实源 |
|---|---|
| Python 包、版本约束、CLI 入口 | `pyproject.toml`、`uv.lock`、`multiclaw --help` |
| 前端依赖与命令 | `frontend/package.json`、`frontend/package-lock.json` |
| 配置项与环境变量 | `src/multiclaw/config/settings.py`、示例 TOML |
| HTTP 路由 | `src/multiclaw/api/`、`src/multiclaw/auth/router.py`、`src/multiclaw/server.py` |
| 数据库支持与发布命令 | `src/multiclaw/storage/`、`src/multiclaw/cli.py`、Alembic 配置 |
| 架构边界 | 对应生产模块及已批准的多租户架构设计 |
| 测试能力 | `tests/`、pytest markers、`.github/workflows/ci.yml` |
| 开发启动方式 | `start.sh`、`stop.sh`、Vite 和 Uvicorn 配置 |

正式文档不得使用历史实施计划中的旧命令或测试数字覆盖当前代码事实。版本相关状态必须写明适用版本或改写成可重复执行的命令。

## 8. 文档校验设计

新增一个仅使用 Python 标准库的文档检查入口，并由 CI 调用。检查范围包括：

1. 正式文档清单存在且非空。
2. Markdown 相对文件链接指向存在的仓库文件。
3. 文档内本地标题锚点可以解析，忽略外部 URL、邮件链接和纯页面内特殊协议。
4. 正式文档不包含未决标记、旧健康路径、已删除的 SQLite 直连 API 或疑似长密钥。
5. README 包含当前 CLI、数据库升级、测试、前端构建和安全报告入口。
6. `configuration.md` 覆盖顶层 Settings 分组；`api.md` 覆盖公开路由分组。

检查脚本不得访问网络，也不得修改文件。它应返回非零退出码并打印具体文件与问题位置，便于本地和 CI 定位。

## 9. 安全与法律要求

1. 所有示例使用占位域名、空邮箱或运行时生成的随机值。
2. 文档扫描覆盖 API key、Bearer token、私钥头、数据库密码和历史 Brevo key 形态。
3. 不把开发用 `host_unsafe_dev_only` 描述为生产替代方案。
4. 不建议在应用启动时自动执行 Alembic 升级。
5. Apache License 2.0 使用未经改写的官方正文。
6. Contributor Covenant 2.1 保留来源与版本链接；执行联系方式使用仓库维护渠道，不写个人邮箱。

## 10. 验证与验收

交付前必须完成：

1. 文档检查入口通过。
2. 所有 Markdown 相对链接和锚点通过检查。
3. 快速开始中的安全命令在临时目录或隔离环境中验证。
4. `uv run pytest -q` 通过；无真实 MySQL URL 时明确记录跳过项。
5. `npm run lint` 与 `npm run build` 通过，且构建后工作树无差异。
6. 当前树敏感信息扫描、未决标记扫描和 `git diff --check` 通过。
7. 正式文档与 CLI、Settings、路由和 CI 逐项交叉核对。
8. 独立规范审阅和质量审阅无未解决的高优先级问题。

最终成果必须满足：新开发者可以从 README 到入门、开发、测试和贡献路径连续导航；高级贡献者可以找到架构、安全、配置、API、部署与故障排查的稳定入口；所有已知平台限制和未验证条件均被如实披露。

## 11. 交付边界

实施在 `docs/complete-project-documentation` 分支的独立 worktree 中完成。文档设计、实施计划和最终文档分别形成可审阅提交。除文档检查及其 CI 接线外，不修改生产行为、数据模型或依赖。
