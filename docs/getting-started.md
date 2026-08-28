# 入门指南

本指南面向第一次参与 MultiClaw 开发的贡献者，使用全新的 SQLite 文件完成安装、迁移、启动与健康检查。MySQL 和生产部署见[部署指南](deployment.md)。

## 前置条件

- Python `>=3.12`
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 与 npm
- Git
- macOS，或已安装 nsjail 的 Linux（原生沙箱要求见[沙箱部署](sandbox-deployment.md)）

## 获取代码并安装依赖

```bash
git clone <repository-url> MultiClaw
cd MultiClaw
uv sync
cd frontend
npm ci
cd ..
```

`uv sync` 使用 `uv.lock` 安装 Python 与开发依赖；`npm ci` 按前端 lockfile 执行可重复安装。

## 创建临时开发密钥

JWT 签名密钥和 Secret keyring 是服务启动的必需输入。下面的值只保存在当前 shell：

```bash
export MULTICLAW_AUTH_JWT_SIGNING_KEY="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export MULTICLAW_SECRETS_KEYRING_B64="$(uv run python -c 'import base64,json,secrets; payload={"active_key_version":1,"keys":{"1":base64.b64encode(secrets.token_bytes(32)).decode()}}; print(base64.b64encode(json.dumps(payload).encode()).decode())')"
```

JWT 密钥必须至少 32 字节。keyring 环境变量是 base64 编码的 JSON，内部活动 key 必须是恰好 32 字节的随机值。

> 以上随机生成方式只适合一次性本地开发。生产环境必须稳定保存密钥、限制读取权限并纳入备份与轮换；重启时丢失 keyring 会导致已有租户 Secret 无法解密。

## 初始化独立 SQLite 数据库

```bash
mkdir -p data
export MULTICLAW_DATABASE__DRIVER=sqlite
export MULTICLAW_DATABASE__URL=sqlite+aiosqlite:///data/multiclaw-dev.db
uv run multiclaw db upgrade
uv run multiclaw db check
```

始终对目标数据库显式执行迁移。若复用早期开发阶段创建、但没有 Alembic revision 的 SQLite 文件，`db check` 会失败；本指南故意选择一个新文件以消除该歧义。

## 配置开发邮件模式

如果当前只需要验证服务、数据库和健康检查，可以启用活动提供商的 mock 开关：

```bash
export MULTICLAW_EMAIL__PROVIDER=brevo
export MULTICLAW_BREVO__MOCK=true
```

mock 模式会保留验证码摘要，但只跳过对外发送；它不会把验证码返回给 API、写入日志或展示在页面。因此 mock 模式**不能完成交互式登录**。

需要登录 Web 界面时，请在当前 shell 配置真实提供商，例如选择 `brevo` 或 `resend`，设置对应的 `api_key`、`sender_email` 和 `sender_name`，并令该提供商的 `mock=false`。完整变量名和生产注意事项见[配置参考](configuration.md#邮件提供商emailbrevoresend)。不要把真实 API key 写入仓库中的 TOML、shell 历史或截图。

## 启动开发服务

终端一（保留上面的环境变量）：

```bash
uv run uvicorn multiclaw.server:app --host 127.0.0.1 --port 15800 --reload
```

终端二：

```bash
cd frontend
npm run dev
```

Vite 在 <http://127.0.0.1:5173> 提供前端，并把 `/api` 代理到 `127.0.0.1:15800`。浏览器认证调用使用 `/api/auth/*`；后端同时保留直接的 `/auth/*` 路由。

## 可选开发脚本

```bash
./start.sh
```

`start.sh` 会强制终止占用 `15800` 和 `5173` 的进程，把后端和前端绑定到 `0.0.0.0`，写入 `/tmp` PID 文件，尝试打开浏览器，并跟踪 `~/.multiclaw/logs/multiclaw.log`。仅在确认端口没有承载其他任务时使用。

> 该脚本会扩大监听范围并强制结束端口进程，只是本地便利工具，不适合作为生产服务管理器，也不应在共享主机上无检查运行。

## 验证健康状态

```bash
curl --fail http://127.0.0.1:15800/api/health/live
curl --fail http://127.0.0.1:15800/api/health/ready
```

存活检查只证明进程能响应。就绪检查还验证数据库连接与版本、Alembic revision、SQLite 外键和完整性、工作区权限以及 keyring 引用；失败时返回 `503` 和 `checks_failed`，不要通过绕过检查来放量。

OpenAPI UI 位于 <http://127.0.0.1:15800/docs>，原始 schema 位于 <http://127.0.0.1:15800/openapi.json>。

## 首次登录

1. 确认已配置真实 Brevo 或 Resend 提供商，并关闭活动提供商的 mock。
2. 打开 <http://127.0.0.1:5173>，输入可收信邮箱并请求验证码。
3. 输入六位验证码。首次验证成功时，后端创建用户及其默认工作区，并设置 HttpOnly 会话 cookie。

发送频率、验证码有效期和失败次数均由服务端限制。收不到验证码时见[故障排查](troubleshooting.md#邮件验证码未送达)。

## 停止服务

在两个前台终端分别按 `Ctrl-C`。如果使用了 `start.sh`，另开终端执行：

```bash
./stop.sh
```

`stop.sh` 先读取 `/tmp` PID 文件，再按端口尝试停止进程；共享主机上同样应先确认目标。

## 下一步

- [开发指南](development.md)：目录、热重载、日志、配置覆盖与调试。
- [架构说明](architecture.md)：理解租户边界、运行时和可恢复工作流。
- [测试指南](testing.md)：运行后端、前端、文档和平台专项门禁。
- [贡献指南](../CONTRIBUTING.md)：准备分支、提交和 Pull Request。
