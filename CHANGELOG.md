# 变更日志

本文件记录 MultiClaw 的重要变化。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

项目当前处于开发阶段，以下条目用于描述代码基线，不代表已经发布稳定版本。

## [未发布]

### 新增

- 面向开发者与贡献者的完整中文文档体系。
- 自动化文档链接、内容和安全形态检查。

## [0.1.0] - 2026-08-28

### 新增

- Python 代理运行时、工具系统、MCP 集成和 React 管理界面。
- 基于 TenantContext、作用域 Unit of Work 和数据库约束的多租户隔离。
- 持久化运行、lease、fencing、checkpoint、审批、串行工具执行和确定性恢复。
- 用户 BYOK Secret 的 AESGCM envelope、keyring 轮换和严格 fallback 策略。
- 邮箱验证码认证、CSRF、auth epoch 和可恢复的延迟账户删除。
- macOS Seatbelt 与 Linux nsjail 原生沙箱后端。
- SQLite 与 MySQL 双存储、Alembic 前向迁移和双后端 CI 矩阵。

### 安全

- 精确租户/工作区/会话/运行事件路由与 SSE 隔离。
- Secret 脱敏、MCP 信任边界、工具审批和 fail-closed readiness。

### 状态

- `0.1.0` 是开发基线，尚未正式发布。
- 当前仅支持单机部署；不支持旧数据迁移、集群、工作区切换或同一运行内并行工具。
