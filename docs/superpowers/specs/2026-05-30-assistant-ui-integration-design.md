# assistant-ui 集成设计方案

## 目标

用 assistant-ui（基于 Vercel AI SDK）完全替换当前 1415 行 `index.html` 单文件前端，保留现有 FastAPI 后端，将 `POST /chat` SSE 协议对齐到 AI SDK Data Stream 格式。

## 架构

```
MultiClaw/
├── src/multiclaw/          ← FastAPI 后端
│   ├── server.py           ← 改造: chat SSE → AI SDK Data Stream
│   ├── auth/               ← 不变
│   ├── session/            ← 不变
│   ├── agent/              ← 不变
│   └── tools/              ← 不变
│
├── frontend/               ← 新增: React + Vite + assistant-ui
│   ├── src/
│   │   ├── App.tsx                    ← AssistantRuntimeProvider 入口
│   │   ├── main.tsx                   ← ReactDOM entry
│   │   ├── components/
│   │   │   ├── login/                 ← 自定义 React 组件
│   │   │   │   ├── LoginOverlay.tsx
│   │   │   │   └── AuthContext.tsx
│   │   │   ├── session/
│   │   │   │   ├── SessionList.tsx    ← 基于 ThreadListPrimitive
│   │   │   │   └── SessionProvider.tsx ← 同步后端 /sessions CRUD
│   │   │   ├── approval/
│   │   │   │   └── ApprovalToolUI.tsx ← 渲染 requires-action 工具
│   │   │   └── layout/
│   │   │       └── AppLayout.tsx      ← 侧边栏 + 主区域布局
│   │   └── lib/
│   │       ├── api.ts                 ← fetch 封装
│   │       └── constants.ts
│   ├── vite.config.ts
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   └── package.json
│
├── multiclaw.toml
└── pyproject.toml
```

- 前端放独立 `frontend/` 目录，Vite 构建
- 开发时 Vite dev server 独立运行，`/api` proxy 到 FastAPI
- 生产构建产物拷贝到 `src/multiclaw/static/`，FastAPI 继续 serve SPA

## 后端改造

### 总则

- 框架、目录结构不动
- 改动集中在 `server.py` 中 `POST /chat` 的 SSE 输出格式
- 统一加 `/api` 路由前缀

### 端点清单

| 端点 | 改动 |
|------|------|
| `POST /api/chat` | SSE 输出改为 AI SDK Data Stream 格式 |
| `GET /api/sessions` | 不变 |
| `POST /api/sessions` | 不变 |
| `DELETE /api/sessions/{id}` | 不变 |
| `GET /api/sessions/{id}/messages` | 不变 |
| `POST /api/approve` | 不变 |
| `GET /api/auth/me` | 不变 |
| `POST /api/auth/send-code` | 不变 |
| `POST /api/auth/verify` | 不变 |
| `POST /api/auth/logout` | 不变 |

### POST /chat 协议映射

| 当前 SSE 事件 | AI SDK Data Stream | 说明 |
|--------------|-------------------|------|
| `{"type":"token","content":"..."}` | `0:"text..."` | 文本增量 chunk |
| `{"type":"reasoning","content":"..."}` | `d:{"type":"reasoning","data":...}` | 推理，走 data 通道 |
| `{"type":"tool_call","name":"...","arguments":...}` | `9:{"toolCallId":"...","toolName":"...","args":...}` | 工具调用开始 |
| `{"type":"tool_result","name":"...","content":"..."}` | `a:{"toolCallId":"...","result":"..."}` | 工具结果 |
| `{"type":"approval_required",...}` | `9:{"toolCallId":"...","status":"requires-action",...}` | 待审批工具 |
| `{"type":"done","content":"..."}` | `f:{"finishReason":"stop"}` | 完成 |
| `{"type":"error","content":"..."}` | `e:{"message":"..."}` | 错误 |
| `{"type":"session",...}` | `d:{"type":"session",...}` | session info，走 data 通道 |

### 后端需新增

1. **toolCallId** — 每次 tool call 分配唯一 ID（当前没有）
2. **Data Stream 行编码器** — 简单的 `prefix + JSON` 行输出器（Python 侧实现，工作量小）
3. **requires-action 流程** — tool call 需审批时标记 `status="requires-action"`，前端调 `/api/approve` 后，后端在后续流中补发 tool call result

## 前端集成

### 依赖

```json
{
  "@assistant-ui/react": "latest",
  "@assistant-ui/react-ai-sdk": "latest",
  "@assistant-ui/react-markdown": "latest",
  "@ai-sdk/react": "latest",
  "ai": "latest",
  "react": "^19",
  "react-dom": "^19"
}
```

构建：Vite + Tailwind CSS v4 + TypeScript

### Runtime 配置

```tsx
import { useChatRuntime, AssistantChatTransport } from "@assistant-ui/react-ai-sdk";

const runtime = useChatRuntime({
  transport: new AssistantChatTransport({ api: "/api/chat" }),
});
```

`AssistantChatTransport` 自动处理 AI SDK Data Stream 协议，前端无需手动写协议适配。

### 三个自定义模块

**1. 登录**
- `AuthContext` — 管理认证状态，`GET /api/auth/me` 检查登录态
- `LoginOverlay` — Email 输入 → 验证码 → 自动消失
- 拦截 401 弹出登录

**2. Session（Thread）列表**
- 基于 `ThreadListPrimitive`，嫁接后端 session CRUD
- `SessionProvider` 负责：初始化从 `GET /api/sessions` 加载 → 切换线程加载历史消息 → 新建/删除同步后端

**3. 工具审批**
- 自定义 `ToolFallback` 组件，`status === "requires-action"` 时渲染审批面板
- 用户操作后调 `POST /api/approve`，后端补发 tool result

## 初始化步骤

```sh
# 1. 创建 Vite + React + TypeScript 项目
npm create vite@latest frontend -- --template react-ts

# 2. 安装依赖
cd frontend
npm install @assistant-ui/react @assistant-ui/react-ai-sdk @assistant-ui/react-markdown @ai-sdk/react ai

# 3. 安装 Tailwind
npm install -D tailwindcss @tailwindcss/vite

# 4. 生成 assistant-ui 脚手架组件
npx create-assistant-ui init
```

## 不在本次范围

- Session 改名（`PATCH /sessions/{id}`）、归档（`POST .../archive`）、恢复（`POST .../restore`）—— assistant-ui ThreadList 无原生对应概念，首版不实现
- 移动端响应式优化 —— 首版聚焦桌面端

## 实施顺序

1. **搭建前端骨架** — Vite + React + TS + Tailwind + assistant-ui 包安装 + CLI 脚手架
2. **后端 chat 协议改造** — `POST /chat` SSE → AI SDK Data Stream；加 toolCallId；加 `/api` 前缀
3. **聊天核心串通** — 配置 transport，验证文本流、tool call、reasoning 端到端
4. **Session 管理** — `SessionProvider` 对接 `/sessions` CRUD，集成 ThreadList
5. **工具审批** — 自定义 ApprovalToolUI，对接 requires-action + `/approve`
6. **登录 & 收尾** — LoginOverlay + AuthContext；build 产物集成到 FastAPI static；清理旧文件
