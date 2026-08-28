# MultiClaw 前端

该目录是 MultiClaw 的 React 19 + TypeScript + Vite 8 Web 界面，负责邮箱认证、会话列表、聊天流、工具审批、租户 Secret 与账号删除恢复。项目总览和后端启动方式见[根 README](../README.md)。

## 安装与开发

```bash
npm ci
npm run dev
```

开发服务器默认使用 Vite 端口 `5173`。`vite.config.ts` 将 `/api` 代理到 `http://127.0.0.1:15800`，因此应先在该地址启动后端。前端的 `API_BASE` 固定为 `/api`；认证请求会落到 `/api/auth/*`，不要在组件中硬编码另一个后端主机。

## 质量门禁

```bash
npm run lint
npm run build
```

`npm run build` 先执行 TypeScript project build，再由 Vite 将生产资源写入 `../src/multiclaw/static/`。该目录是生成产物：不要手工编辑哈希文件；源码变化后通过构建命令整体重建，并检查 Git diff 是否只包含预期输出。

可用脚本：

| 命令 | 用途 |
|---|---|
| `npm run dev` | 启动 Vite 开发服务器和热更新 |
| `npm run lint` | 运行 ESLint |
| `npm run build` | TypeScript 检查并生成后端托管的静态资源 |
| `npm run preview` | 本地预览已经生成的 Vite bundle |

## 目录约定

- `src/components/assistant-ui/`：assistant-ui 消息、线程和工具渲染。
- `src/components/chat/`：聊天视图。
- `src/components/session/`：会话加载、切换和列表。
- `src/components/approval/`：工具审批交互。
- `src/components/login/`：邮箱验证码登录。
- `src/components/settings/`：Secret 和账号删除设置。
- `src/lib/api.ts`：HTTP 请求、CSRF 和响应错误边界。
- `src/lib/auth-context.tsx`：认证状态生命周期。
- `src/lib/session-store.ts`、`src/lib/chat-store.ts`：会话与聊天状态。

优先复用现有组件、store 和 `@/` 路径别名。认证请求必须携带 cookie；变更请求通过 `src/lib/security.ts` 获取并发送双提交 CSRF token。

更多后端/前端联调、日志、静态资源和提交要求见[开发指南](../docs/development.md)与[贡献指南](../CONTRIBUTING.md)。
