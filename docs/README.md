# MultiClaw 文档

这里是 MultiClaw 的正式中文文档入口。文档描述当前 `0.1.0` 开发基线；`docs/superpowers/` 中的其他文件是设计和实施记录，不应替代当前代码与本索引下的使用说明。

## 入门

- [项目 README](../README.md)：能力、支持范围和最短开发路径。
- [入门指南](getting-started.md)：从干净检出到 SQLite 启动、健康检查和登录准备。

## 开发

- [开发指南](development.md)：目录、后端、前端、静态资源和调试流程。
- [架构说明](architecture.md)：多租户请求链路、运行时、工作流、事件和 Secret。
- [配置参考](configuration.md)：TOML、环境变量、默认值和安全属性。
- [API 概览](api.md)：认证、会话、聊天、审批、Secret、删除和健康接口。
- [测试指南](testing.md)：后端、前端、文档、MySQL 和原生沙箱门禁。
- [贡献指南](../CONTRIBUTING.md)：提交、测试、Lore trailer 和 Pull Request 规范。

## 安全

- [安全模型](security-model.md)：资产、信任边界、隔离、加密、审批和删除生命周期。
- [安全政策](../SECURITY.md)：支持范围和私密漏洞报告流程。
- [原生沙箱部署](sandbox-deployment.md)：macOS Seatbelt、Linux nsjail 与已知限制。

## 部署运维

- [部署指南](deployment.md)：单机发布、数据库、健康门禁、备份和回滚。
- [多租户运维](multi-tenant-operations.md)：迁移、keyring、清除器与轮换操作。
- [故障排查](troubleshooting.md)：按症状定位启动、数据库、邮件、SSE、MCP 和沙箱问题。

## 设计记录

以下两份已批准设计用于解释关键决策，实施事实仍以当前生产模块和正式文档为准：

- [多租户架构设计](superpowers/specs/2026-08-15-multi-tenant-architecture-design.md)
- [项目文档体系设计](superpowers/specs/2026-08-28-complete-project-documentation-design.md)

发现文档与代码不一致时，请按[贡献指南](../CONTRIBUTING.md#文档维护)修正文档，并运行 `uv run python scripts/check_docs.py`。

