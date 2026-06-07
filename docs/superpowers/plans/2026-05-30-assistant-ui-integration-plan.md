# assistant-ui 集成实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 assistant-ui + AI SDK 完全替换当前单文件前端，后端 `POST /chat` SSE 对齐到 AI SDK Data Stream 格式。

**Architecture:** React + Vite + assistant-ui 前端放在独立 `frontend/` 目录，开发时 Vite dev server proxy `/api` 到 FastAPI；生产构建产物拷贝到 `src/multiclaw/static/` 由 FastAPI 直接 serve。后端只改 `server.py` 的 chat 流输出格式和 `/api` 路由前缀，其余不变。

**Tech Stack:** React 19, TypeScript, Vite, Tailwind CSS v4, @assistant-ui/react, @assistant-ui/react-ai-sdk, @ai-sdk/react, ai (v6), Python FastAPI, pytest

---

### Task 1: 搭建前端项目骨架

**Files:**
- Create: `frontend/` 整个目录

- [ ] **Step 1: 创建 Vite + React + TypeScript 项目**

```sh
cd /Users/felix/git/MultiClaw
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
```

Expected: `frontend/` 目录创建成功，`npm install` 通过。

- [ ] **Step 2: 安装 assistant-ui 依赖**

```sh
cd /Users/felix/git/MultiClaw/frontend
npm install @assistant-ui/react @assistant-ui/react-ai-sdk @assistant-ui/react-markdown @ai-sdk/react ai
```

Expected: 所有包安装成功，无版本冲突。

- [ ] **Step 3: 安装 Tailwind CSS v4**

```sh
cd /Users/felix/git/MultiClaw/frontend
npm install -D tailwindcss @tailwindcss/vite
```

- [ ] **Step 4: 配置 Vite**

写入 `frontend/vite.config.ts`:

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
```

- [ ] **Step 5: 写入 Tailwind CSS 入口和主题**

写入 `frontend/src/index.css`:

```css
@import "tailwindcss";

@theme {
  --color-background: #1a1a1c;
  --color-foreground: #ececee;
  --color-surface: #212124;
  --color-elevated: #2a2a2e;
  --color-input: #1e1e21;
  --color-border: #2e2e32;
  --color-accent: #d4a853;
  --color-danger: #e5534b;
  --color-success: #57ab5a;
  --color-muted-foreground: #9d9da6;
}
```

Assistant-ui CLI 脚手架生成时会覆盖此文件，执行 `npx create-assistant-ui init` 后需**重新写入上述内容**。如果脚手架生成了自己的 `globals.css`，则不冲突，手工确保上述 `@theme` 在其中即可。

- [ ] **Step 6: 配置 TypeScript paths**

替换 `frontend/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "isolatedModules": true,
    "moduleDetection": "force",
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": false,
    "noUnusedParameters": false,
    "noFallthroughCasesInSwitch": true,
    "paths": { "@/*": ["./src/*"] }
  },
  "include": ["src"]
}
```

- [ ] **Step 7: 生成 assistant-ui 脚手架组件**

```sh
cd /Users/felix/git/MultiClaw/frontend
npx create-assistant-ui init
```

Expected: 生成 `src/components/assistant-ui/thread.tsx`、`thread-list.tsx` 等文件。

- [ ] **Step 8: 验证骨架可启动**

```sh
cd /Users/felix/git/MultiClaw/frontend
npx vite --host 0.0.0.0
```

Expected: Vite dev server 在 `http://localhost:5173` 启动，无编译错误。

- [ ] **Step 9: Commit**

```bash
cd /Users/felix/git/MultiClaw
git add frontend/
git commit -m "feat: scaffold frontend with Vite + React + assistant-ui"
```

---

### Task 2: 后端 Data Stream 编码器

**Files:**
- Create: `src/multiclaw/stream.py`
- Create: `tests/test_stream.py`

- [ ] **Step 1: 编写测试**

写入 `tests/test_stream.py`:

```python
import json
from multiclaw.stream import DataStreamEncoder


def test_encode_text_delta():
    encoder = DataStreamEncoder()
    assert encoder.text_delta("Hello") == '0:"Hello"\n'


def test_encode_tool_call_start():
    encoder = DataStreamEncoder()
    result = encoder.tool_call_start("call_1", "read_file", {"path": "/x"})
    parsed = json.loads(result[2:])  # skip "2:"
    assert parsed["toolCallId"] == "call_1"
    assert parsed["toolName"] == "read_file"
    assert parsed["args"] == {"path": "/x"}


def test_encode_tool_result():
    encoder = DataStreamEncoder()
    result = encoder.tool_result("call_1", {"content": "ok"})
    parsed = json.loads(result[2:])
    assert parsed["toolCallId"] == "call_1"
    assert parsed["result"] == {"content": "ok"}


def test_encode_tool_result_error():
    encoder = DataStreamEncoder()
    result = encoder.tool_result("call_1", {"content": "err"}, is_error=True)
    parsed = json.loads(result[2:])
    assert parsed["isError"] is True


def test_encode_data_event():
    encoder = DataStreamEncoder()
    result = encoder.data({"type": "reasoning", "data": {"text": "hmm"}})
    parsed = json.loads(result[2:])
    assert parsed == {"type": "reasoning", "data": {"text": "hmm"}}


def test_encode_data_session():
    encoder = DataStreamEncoder()
    result = encoder.data({"type": "session", "data": {"session_id": "s1", "title": "Chat"}})
    parsed = json.loads(result[2:])
    assert parsed["type"] == "session"


def test_encode_finish():
    encoder = DataStreamEncoder()
    assert encoder.finish("stop") == 'f:{"finishReason":"stop"}\n'


def test_encode_finish_tool_calls():
    encoder = DataStreamEncoder()
    result = encoder.finish("tool-calls")
    parsed = json.loads(result[2:])
    assert parsed["finishReason"] == "tool-calls"


def test_encode_error():
    encoder = DataStreamEncoder()
    result = encoder.error("Something went wrong")
    parsed = json.loads(result[2:])
    assert parsed["message"] == "Something went wrong"
```

- [ ] **Step 2: 运行测试，确认失败**

```sh
cd /Users/felix/git/MultiClaw && python -m pytest tests/test_stream.py -v
```

Expected: FAIL，`ModuleNotFoundError: No module named 'multiclaw.stream'`

- [ ] **Step 3: 实现 DataStreamEncoder**

写入 `src/multiclaw/stream.py`:

```python
"""AI SDK Data Stream format encoder.

Wire format:
  <type_code>:<payload>\n

Type codes:
  0 = text delta (raw string, not JSON)
  2 = tool call start
  9 = tool result
  d = data (generic key-value)
  e = error
  f = finish
"""

import json


class DataStreamEncoder:
    """Encode AI SDK Data Stream protocol lines."""

    @staticmethod
    def text_delta(text: str) -> str:
        return f'0:"{text}"\n'

    @staticmethod
    def tool_call_start(tool_call_id: str, tool_name: str, args: dict) -> str:
        payload = json.dumps(
            {"toolCallId": tool_call_id, "toolName": tool_name, "args": args},
            ensure_ascii=False,
        )
        return f"2:{payload}\n"

    @staticmethod
    def tool_result(tool_call_id: str, result: dict, is_error: bool = False) -> str:
        payload = json.dumps(
            {
                "toolCallId": tool_call_id,
                "result": result,
                "isError": is_error,
            },
            ensure_ascii=False,
        )
        return f"9:{payload}\n"

    @staticmethod
    def data(obj: dict) -> str:
        payload = json.dumps(obj, ensure_ascii=False)
        return f"d:{payload}\n"

    @staticmethod
    def finish(reason: str) -> str:
        payload = json.dumps({"finishReason": reason})
        return f"f:{payload}\n"

    @staticmethod
    def error(message: str) -> str:
        payload = json.dumps({"message": message})
        return f"e:{payload}\n"
```

- [ ] **Step 4: 运行测试**

```sh
cd /Users/felix/git/MultiClaw && python -m pytest tests/test_stream.py -v
```

Expected: 所有 8 个测试 PASS。

- [ ] **Step 5: Commit**

```bash
cd /Users/felix/git/MultiClaw
git add src/multiclaw/stream.py tests/test_stream.py
git commit -m "feat: add AI SDK Data Stream encoder"
```

---

### Task 3: 后端 server.py chat 协议改造

**Files:**
- Modify: `src/multiclaw/server.py` (整个 `event_stream` 函数)

- [ ] **Step 1: 阅读当前 event_stream 代码**

确认理解当前 `server.py:303-431` 的 SSE 流逻辑。关键改动点：
- 移除 `data: ...\n\n` 的 SSE 包装，改为 Data Stream 行编码
- `Content-Type` 保持 `text/event-stream`（Data Stream 兼容 SSE）
- token 事件用 `DataStreamEncoder.text_delta()`
- tool_call 事件用 `DataStreamEncoder.tool_call_start()`
- tool_result 事件用 `DataStreamEncoder.tool_result()`
- approval_required 用 data 事件（带 `requires-action` 标记）再加上一个 tool-call start
- done 事件用 `DataStreamEncoder.finish()`
- error 事件用 `DataStreamEncoder.error()`
- session 事件用 `DataStreamEncoder.data()`
- reasoning 事件用 `DataStreamEncoder.data()`

- [ ] **Step 2: 修改 server.py 导入**

在 `server.py` 顶部导入区，添加 `DataStreamEncoder` 和 `uuid4`:

```python
from uuid import uuid4
from multiclaw.stream import DataStreamEncoder
```

- [ ] **Step 3: 修改 Content-Type 响应头**

找到 `server.py:431`:
```python
return StreamingResponse(event_stream(), media_type="text/event-stream")
```

改为:
```python
return StreamingResponse(
    event_stream(),
    media_type="text/event-stream",
    headers={"X-Vercel-AI-Data-Stream": "v1"},
)
```

- [ ] **Step 4: 重写 event_stream 协程**

替换 `server.py:303-429` 的整个 `event_stream` 函数体。

将当前逻辑替换为以下版本（保留 token queue / event queue / bus 机制，只改编码层）：

```python
async def event_stream():
    logger.info("SSE stream started, message=%r, session=%r", req.message[:80], session.id)
    enc = DataStreamEncoder()

    # Emit session info via data event
    yield enc.data({
        "type": "session",
        "data": {"session_id": session.id, "title": session.title},
    })

    token_queue: asyncio.Queue[dict] = asyncio.Queue()
    event_queue: asyncio.Queue[Event] = asyncio.Queue()

    async def collector(event: Event):
        await event_queue.put(event)

    sub_id = shared_bus.subscribe("*", collector)

    async def run_stream():
        try:
            async for item in agent.handle_message_stream(req.message, session_id=session.id):
                await token_queue.put(item)
        except Exception as exc:
            logger.exception("stream error")
            msg = _friendly_error(exc)
            await token_queue.put({"type": "error", "content": msg})

    stream_task = asyncio.create_task(run_stream())

    try:
        while True:
            token_count = 0
            while True:
                try:
                    item = token_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                token_count += 1
                if item["type"] == "token":
                    yield enc.text_delta(item["content"])
                elif item["type"] == "done":
                    logger.info("stream done, tokens=%d, content_len=%d", token_count, len(item.get("content", "")))
                    finish_reason = "stop"
                    # If there were tool calls pending, mark accordingly
                    yield enc.finish(finish_reason)
                    return
                elif item["type"] == "error":
                    logger.error("stream error: %s", item["content"])
                    yield enc.error(item["content"])
                    return
                elif item["type"] == "tool_call":
                    tool_call_id = item.get("call_id") or uuid4().hex
                    yield enc.tool_call_start(
                        tool_call_id,
                        item["name"],
                        item.get("arguments", {}),
                    )
                elif item["type"] == "tool_result":
                    tool_call_id = item.get("call_id", "")
                    yield enc.tool_result(
                        tool_call_id,
                        {"content": item.get("content", "")},
                        is_error=item.get("is_error", False),
                    )
                elif item["type"] == "reasoning":
                    yield enc.data({"type": "reasoning", "data": {"text": item["content"]}})
                else:
                    # Forward unknown types as data events
                    yield enc.data(item)

            # Drain bus events
            while not event_queue.empty():
                evt = event_queue.get_nowait()
                if evt.type == "tool.awaiting_approval":
                    logger.info(
                        "yield approval_required: request_id=%s tool=%s",
                        evt.data.get("request_id"), evt.data.get("tool"),
                    )
                    tool_call_id = evt.data.get("call_id") or uuid4().hex
                    yield enc.tool_call_start(
                        tool_call_id,
                        evt.data.get("tool", ""),
                        evt.data.get("params", {}),
                    )
                    yield enc.data({
                        "type": "approval_required",
                        "data": {
                            "request_id": evt.data.get("request_id", ""),
                            "tool_call_id": tool_call_id,
                            "tool": evt.data.get("tool", ""),
                            "params": evt.data.get("params", {}),
                            "description": evt.data.get("description", ""),
                        },
                    })
                else:
                    yield enc.data({"type": "state", "data": {"state": evt.type}})

            if stream_task.done():
                exc = stream_task.exception()
                if exc:
                    logger.exception("stream task crashed")
                    yield enc.error(str(exc))
                return

            await asyncio.sleep(0.02)
    finally:
        stream_task.cancel()
        shared_bus.unsubscribe(sub_id)
        logger.info("SSE stream ended")
```

- [ ] **Step 5: 完整检查 server.py**

确保 `server.py` 中不再有旧的 `yield ("data: " + json.dumps(...) + "\n\n")` 的格式（除 `DataStreamEncoder` 调用的行外）。

```sh
cd /Users/felix/git/MultiClaw && grep -n 'data:.*json.dumps' src/multiclaw/server.py
```

Expected: 无输出。

- [ ] **Step 6: 更新 chat 相关测试**

`tests/test_server.py` 中 `test_chat_without_session_emits_session_event` 以及其他以 `test_chat` 开头的测试，原来检查旧的 `data: {"type":"session"...}\n\n` SSE 格式。改为检查 Data Stream 格式：行以 `d:` 开头，JSON 中包含 `type: "session"`。

更新测试断言示例：

```python
# 旧
assert '"type":"session"' in line
# 新
assert line.startswith('d:')
assert '"type":"session"' in line
```

```sh
cd /Users/felix/git/MultiClaw && python -m pytest tests/test_server.py -v -k "chat" 
```

Expected: 所有 chat 相关测试 PASS。

- [ ] **Step 7: Commit**

```bash
cd /Users/felix/git/MultiClaw
git add src/multiclaw/server.py tests/test_server.py
git commit -m "feat: migrate POST /chat to AI SDK Data Stream format"
```

---

### Task 4: 后端 /api 路由前缀和 Auth 中间件适配

**Files:**
- Modify: `src/multiclaw/server.py`
- Modify: `src/multiclaw/auth/middleware.py`

- [ ] **Step 1: 在 server.py 中给后端 API 加前缀**

当前 `app = FastAPI(title="MultiClaw", lifespan=lifespan)` 后直接注册路由，且 `@app.get("/")` 等装饰器是无前缀的。

在 `server.py` 中，创建子 application 处理 API 路由，保持 `/` 和 `/multiclaw.png` 在根路径：

```python
# 在 server.py 顶部添加
from fastapi import FastAPI, APIRouter

# 替换 app 初始化部分：
app = FastAPI(title="MultiClaw", lifespan=lifespan)
api = APIRouter(prefix="/api")

# 将所有 @app.post(...) 等装饰器改为 @api.post(...)
# 保持 @app.get("/") 和 @app.get("/multiclaw.png") 不变
# 在最后添加:
app.include_router(api)
```

具体改造方式 — 找到 server.py 中除 `/` 和 `/multiclaw.png` 外的全部路由装饰器，将 `@app.` 改为 `@api.`：

```python
# 原有:
@app.post("/approve")
@app.get("/sessions")
@app.post("/sessions")
@app.patch("/sessions/{session_id}")
@app.post("/sessions/{session_id}/archive")
@app.post("/sessions/{session_id}/restore")
@app.delete("/sessions/{session_id}")
@app.get("/sessions/{session_id}/messages")
@app.post("/chat")

# 改为:
@api.post("/approve")
@api.get("/sessions")
@api.post("/sessions")
@api.patch("/sessions/{session_id}")
@api.post("/sessions/{session_id}/archive")
@api.post("/sessions/{session_id}/restore")
@api.delete("/sessions/{session_id}")
@api.get("/sessions/{session_id}/messages")
@api.post("/chat")
```

将 `app.add_middleware(AuthMiddleware)` 改为 `app.add_middleware(AuthMiddleware)`（不变，中间件作用于整个 app）。

最后在 server.py 末尾添加 `app.include_router(api)`。

- [ ] **Step 2: 更新 Auth 中间件中 public 路径**

`middleware.py:7-8`:

```python
PUBLIC_PREFIXES = ("/auth/",)
PUBLIC_EXACT = {"/multiclaw.png"}
```

因为 auth router 挂载在 `/auth` 下（在 `auth/router.py:18` 中定义了 `router = APIRouter(prefix="/auth")`），需要确保 middleware 能正确处理 `/auth/` 和 `/api/auth/` 两种前缀：

实际上 auth router 在 `server.py` 中通过 `app.include_router(auth_router)` 注册，前缀由 auth_router 自己定义。如果改成 `api.include_router(auth_router)`，那么 auth 路径会变成 `/api/auth/`。

需要决定：auth 路由是要 `/auth/` 还是 `/api/auth/`？

保持 auth 在根路径 `/auth/`（不挂到 api 下），这样 middleware 的公网路径不变。只需要把 `app.include_router(auth_router)` 保留在 app 层级即可。

所以改动点仅是：
- 将 session/chat/approve 相关路由挂到 `api` router（前缀 `/api`）
- auth router 保持挂到 `app`（前缀 `/auth`）
- middleware 不变

确认 `server.py` 结尾：
```python
app.add_middleware(AuthMiddleware)
app.include_router(auth_router)  # 保持 /auth/ 前缀
app.include_router(api)           # /api/ 前缀
```

- [ ] **Step 3: 更新 frontend 中的 API 路径**

后续 Task 5-6 会配置，暂不处理。

- [ ] **Step 4: 验证现有测试**

```sh
cd /Users/felix/git/MultiClaw && python -m pytest tests/test_server.py -v
```

因为改动了路由前缀，测试会失败。需要更新测试中的路径。

- [ ] **Step 5: 更新测试路径**

`tests/test_server.py` 中所有请求路径加 `/api` 前缀（auth 除外）：

全局替换规则：
- `"/sessions"` → `"/api/sessions"`
- `"/chat"` → `"/api/chat"`
- `"/approve"` → `"/api/approve"`
- `/auth/` 不变

在 `tests/test_server.py` 中执行替换：

```python
# 示例: 原
client.post("/sessions", json={"title": "Alpha"})
# 改为
client.post("/api/sessions", json={"title": "Alpha"})
```

- [ ] **Step 6: 运行测试确认通过**

```sh
cd /Users/felix/git/MultiClaw && python -m pytest tests/test_server.py -v
```

Expected: 所有测试 PASS。

- [ ] **Step 7: Commit**

```bash
cd /Users/felix/git/MultiClaw
git add src/multiclaw/server.py tests/test_server.py
git commit -m "feat: add /api route prefix for backend endpoints"
```

---

### Task 5: 前端基础布局和 Auth

**Files:**
- Create: `frontend/src/lib/constants.ts`
- Create: `frontend/src/lib/api.ts`
- Create: `frontend/src/lib/auth-context.tsx`
- Create: `frontend/src/components/login/LoginOverlay.tsx`
- Create: `frontend/src/components/layout/AppLayout.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/main.tsx`

- [ ] **Step 1: 写入 constants.ts**

写入 `frontend/src/lib/constants.ts`:

```ts
export const API_BASE = "/api";
```

- [ ] **Step 2: 写入 api.ts**

写入 `frontend/src/lib/api.ts`:

```ts
import { API_BASE } from "./constants";

export interface Session {
  id: string;
  title: string;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
}

export interface Message {
  role: "user" | "assistant";
  content: string;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });
  if (res.status === 401) {
    throw new AuthError();
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export class AuthError extends Error {
  constructor() {
    super("Unauthorized");
  }
}

export const authApi = {
  me: () => request<{ email?: string; user_id?: string }>("/auth/me"),
  sendCode: (email: string) =>
    request<{}>("/auth/send-code", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  verify: (email: string, code: string) =>
    request<{}>("/auth/verify", {
      method: "POST",
      body: JSON.stringify({ email, code }),
    }),
  logout: () => request<{}>("/auth/logout", { method: "POST" }),
};

export const sessionApi = {
  list: () => request<Session[]>("/sessions"),
  create: () => request<Session>("/sessions", { method: "POST", body: JSON.stringify({ title: "New Chat" }) }),
  del: (id: string) => request<{ ok: boolean }>(`/sessions/${id}`, { method: "DELETE" }),
  messages: (id: string) => request<Message[]>(`/sessions/${id}/messages`),
};

export const approveApi = {
  submit: (requestId: string, approved: boolean) =>
    request<{ ok: boolean }>("/approve", {
      method: "POST",
      body: JSON.stringify({ request_id: requestId, approved }),
    }),
};
```

- [ ] **Step 3: 写入 AuthContext**

写入 `frontend/src/lib/auth-context.tsx`:

```tsx
import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";
import { authApi, AuthError } from "./api";

interface AuthState {
  email: string | null;
  userId: string | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  login: (email: string, code: string) => Promise<void>;
  sendCode: (email: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [email, setEmail] = useState<string | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    authApi
      .me()
      .then((data) => {
        setEmail(data.email ?? null);
        setUserId(data.user_id ?? null);
      })
      .catch(() => {})
      .finally(() => setIsLoading(false));
  }, []);

  const sendCode = useCallback(async (emailAddr: string) => {
    await authApi.sendCode(emailAddr);
  }, []);

  const login = useCallback(async (emailAddr: string, code: string) => {
    await authApi.verify(emailAddr, code);
    setEmail(emailAddr);
    const data = await authApi.me();
    setUserId(data.user_id ?? null);
  }, []);

  const logout = useCallback(async () => {
    await authApi.logout();
    setEmail(null);
    setUserId(null);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        email,
        userId,
        isLoading,
        isAuthenticated: email !== null,
        login,
        sendCode,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
```

- [ ] **Step 4: 写入 LoginOverlay**

写入 `frontend/src/components/login/LoginOverlay.tsx`:

```tsx
import { useState, useRef, useEffect } from "react";
import { useAuth } from "@/lib/auth-context";

export function LoginOverlay() {
  const { isAuthenticated, isLoading, sendCode, login } = useAuth();
  const [step, setStep] = useState<"email" | "code">("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState(["", "", "", "", "", ""]);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [resendSeconds, setResendSeconds] = useState(0);
  const inputRefs = useRef<(HTMLInputElement | null)[]>([]);

  useEffect(() => {
    if (resendSeconds <= 0) return;
    const t = setInterval(() => setResendSeconds((s) => s - 1), 1000);
    return () => clearInterval(t);
  }, [resendSeconds]);

  if (isLoading) return null;
  if (isAuthenticated) return null;

  const handleSendCode = async () => {
    setError("");
    if (!email.includes("@")) {
      setError("Please enter a valid email");
      return;
    }
    setSending(true);
    try {
      await sendCode(email);
      setStep("code");
      setResendSeconds(60);
      setTimeout(() => inputRefs.current[0]?.focus(), 100);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSending(false);
    }
  };

  const handleCodeInput = (idx: number, value: string) => {
    const digit = value.replace(/\D/g, "").slice(-1);
    const next = [...code];
    next[idx] = digit;
    setCode(next);
    if (digit && idx < 5) inputRefs.current[idx + 1]?.focus();
  };

  const handleCodeKeyDown = (idx: number, e: React.KeyboardEvent) => {
    if (e.key === "Backspace" && !code[idx] && idx > 0) {
      inputRefs.current[idx - 1]?.focus();
    }
    if (e.key === "Enter") handleVerify();
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    e.preventDefault();
    const pasted = e.clipboardData.getData("text").replace(/\D/g, "").slice(0, 6);
    const next = [...code];
    pasted.split("").forEach((d, i) => (next[i] = d));
    setCode(next);
    if (pasted.length === 6) {
      setTimeout(() => handleVerifyWith(next.join("")), 50);
    }
  };

  const handleVerify = () => handleVerifyWith(code.join(""));

  const handleVerifyWith = async (codeStr: string) => {
    if (codeStr.length !== 6) {
      setError("Please enter the 6-digit code");
      return;
    }
    setVerifying(true);
    setError("");
    try {
      await login(email, codeStr);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setVerifying(false);
    }
  };

  const handleResend = async () => {
    if (resendSeconds > 0) return;
    setError("");
    try {
      await sendCode(email);
      setResendSeconds(60);
      setCode(["", "", "", "", "", ""]);
      inputRefs.current[0]?.focus();
    } catch (e: any) {
      setError(e.message);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="w-[380px] max-w-[90vw] rounded-xl border border-border bg-surface p-8 shadow-lg">
        <h2 className="mb-1 text-center font-serif text-xl font-medium">MultiClaw</h2>
        <p className="mb-5 text-center text-xs uppercase tracking-wider text-muted-foreground">
          Sign in to continue
        </p>

        {step === "email" ? (
          <>
            <label className="mb-1 block text-sm text-muted-foreground">Email</label>
            <input
              type="email"
              className="mb-3 w-full rounded-lg border border-border bg-input px-3 py-2.5 text-sm outline-none focus:border-accent"
              placeholder="hello@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSendCode()}
              autoFocus
            />
            <button
              className="w-full rounded-lg bg-accent py-2.5 text-sm font-medium text-black hover:brightness-110 disabled:opacity-40"
              onClick={handleSendCode}
              disabled={sending}
            >
              {sending ? "Sending..." : "Send Code"}
            </button>
            {error && <p className="mt-3 text-center text-xs text-danger">{error}</p>}
          </>
        ) : (
          <>
            <p className="mb-3 text-sm text-muted-foreground">
              Sent to <strong>{email}</strong>
            </p>
            <div className="mb-4 flex justify-center gap-2" onPaste={handlePaste}>
              {code.map((d, i) => (
                <input
                  key={i}
                  ref={(el) => { inputRefs.current[i] = el; }}
                  className="h-[54px] w-[44px] rounded-lg border border-border bg-input text-center font-mono text-2xl outline-none focus:border-accent"
                  maxLength={1}
                  inputMode="numeric"
                  pattern="[0-9]"
                  value={d}
                  onChange={(e) => handleCodeInput(i, e.target.value)}
                  onKeyDown={(e) => handleCodeKeyDown(i, e)}
                />
              ))}
            </div>
            <button
              className="w-full rounded-lg bg-accent py-2.5 text-sm font-medium text-black hover:brightness-110 disabled:opacity-40"
              onClick={handleVerify}
              disabled={verifying}
            >
              {verifying ? "Verifying..." : "Verify"}
            </button>
            {error && <p className="mt-3 text-center text-xs text-danger">{error}</p>}
            <p className="mt-3 text-center text-xs text-muted-foreground">
              <span
                className={resendSeconds > 0 ? "cursor-not-allowed opacity-50" : "cursor-pointer text-accent hover:underline"}
                onClick={handleResend}
              >
                {resendSeconds > 0 ? `Resend code (${resendSeconds}s)` : "Resend code"}
              </span>
            </p>
            <p
              className="mt-2 cursor-pointer text-center text-xs text-muted-foreground underline hover:text-accent"
              onClick={() => { setStep("email"); setError(""); }}
            >
              &larr; Use a different email
            </p>
          </>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: 写入 AppLayout**

写入 `frontend/src/components/layout/AppLayout.tsx`:

```tsx
import { type ReactNode } from "react";
import { useAuth } from "@/lib/auth-context";

export function AppLayout({
  sidebar,
  children,
}: {
  sidebar: ReactNode;
  children: ReactNode;
}) {
  const { email, logout } = useAuth();

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background">
      {/* Sidebar */}
      <aside className="flex w-[260px] shrink-0 flex-col border-r border-border bg-surface">
        {sidebar}
        {/* Footer */}
        <div className="shrink-0 border-t border-border p-3">
          <button
            className="w-full rounded-lg border border-dashed border-border py-2.5 text-sm text-muted-foreground hover:border-accent hover:text-accent"
            onClick={() => {/* handled by SessionProvider */}}
          >
            + New Chat
          </button>
          {email && (
            <div className="mt-2 flex items-center justify-between px-1">
              <span className="truncate text-xs text-muted-foreground">{email}</span>
              <button
                className="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-danger/10 hover:text-danger"
                onClick={logout}
              >
                Sign out
              </button>
            </div>
          )}
        </div>
      </aside>

      {/* Main area */}
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {children}
      </main>
    </div>
  );
}
```

- [ ] **Step 6: 更新 App.tsx**

写入 `frontend/src/App.tsx`:

```tsx
import { AuthProvider } from "@/lib/auth-context";
import { LoginOverlay } from "@/components/login/LoginOverlay";
import { AppLayout } from "@/components/layout/AppLayout";

function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <LoginOverlay />
      {children}
    </AuthProvider>
  );
}

export default function App() {
  return (
    <AppShell>
      {/* Placeholder — Task 6 fills in chat runtime */}
      <div className="flex h-full items-center justify-center text-muted-foreground">
        Loading...
      </div>
    </AppShell>
  );
}
```

- [ ] **Step 7: 更新 main.tsx**

写入 `frontend/src/main.tsx`:

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
```

- [ ] **Step 8: 更新 index.html**

确保 `frontend/index.html` 中 `<body>` 内只有一个 `<div id="root"></div>`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>MultiClaw</title>
    <link rel="icon" href="/multiclaw.png" type="image/png" />
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 9: Commit**

```bash
cd /Users/felix/git/MultiClaw
git add frontend/src/
git commit -m "feat: add base layout, auth context, and login overlay"
```

---

### Task 6: 聊天核心串通

**Files:**
- Modify: `frontend/src/App.tsx`
- Create: `frontend/src/components/chat/ChatView.tsx`
- Modify: `frontend/src/components/assistant-ui/thread.tsx` (from scaffold)

- [ ] **Step 1: 在 App.tsx 中接入 AssistantRuntimeProvider + useChatRuntime**

写入 `frontend/src/App.tsx`:

```tsx
import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useChatRuntime, AssistantChatTransport } from "@assistant-ui/react-ai-sdk";
import { AuthProvider, useAuth } from "@/lib/auth-context";
import { LoginOverlay } from "@/components/login/LoginOverlay";
import { AppLayout } from "@/components/layout/AppLayout";
import { ChatView } from "@/components/chat/ChatView";
import { API_BASE } from "@/lib/constants";

function ChatApp() {
  const { isAuthenticated, isLoading } = useAuth();

  const transport = new AssistantChatTransport({
    api: `${API_BASE}/chat`,
  });

  const runtime = useChatRuntime({ transport });

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (!isAuthenticated) {
    return <LoginOverlay />;
  }

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <AppLayout sidebar={<div className="flex-1 overflow-y-auto p-2">{/* Task 7: SessionList */}</div>}>
        <ChatView />
      </AppLayout>
    </AssistantRuntimeProvider>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <ChatApp />
    </AuthProvider>
  );
}
```

- [ ] **Step 2: 写入 ChatView**

写入 `frontend/src/components/chat/ChatView.tsx`:

```tsx
import { Thread } from "@/components/assistant-ui/thread";

export function ChatView() {
  return (
    <div className="flex h-full flex-col">
      <Thread />
    </div>
  );
}
```

- [ ] **Step 3: 检查脚手架生成的 thread.tsx**

读取 `frontend/src/components/assistant-ui/thread.tsx`，确认 `Thread` 组件被正确导出。如果 scaffold 生成的是默认实现，可以直接用；如果有 AI SDK 特定的 transport 参数，根据实际内容调整。

关键检查点：
- `Thread` 组件内部使用了 `<ThreadPrimitive.Root>`, `<ThreadPrimitive.Messages>`, `<ComposerPrimitive.Root>` 等
- 消息渲染使用了 `MessagePrimitive`
- 工具回调使用了 `ToolFallback`

此步骤为读取和确认，不写代码。

- [ ] **Step 4: 启动后端和前端联调**

在两个终端分别：

```sh
# Terminal 1: 启动后端
cd /Users/felix/git/MultiClaw && python -m uvicorn multiclaw.server:app --reload --host 0.0.0.0 --port 8000

# Terminal 2: 启动前端
cd /Users/felix/git/MultiClaw/frontend && npx vite --host 0.0.0.0
```

Expected: 浏览器访问 `http://localhost:5173`，登录后看到 assistant-ui 的聊天界面。发送消息后能收到 AI SDK Data Stream 格式的响应。

- [ ] **Step 5: 验证 text、tool-call、reasoning 端到端**

在聊天中输入测试消息，观察：
- 文本流式输出：`0:"..."` 格式被正确解析和渲染
- 工具调用：`2:{...}` 被正确渲染为 tool card
- 推理：data 事件 `d:{"type":"reasoning",...}` 被正确处理

使用浏览器 DevTools Network 标签，检查 `/api/chat` 的响应体是否符合 Data Stream 格式。

- [ ] **Step 6: Commit**

```bash
cd /Users/felix/git/MultiClaw
git add frontend/src/
git commit -m "feat: wire up chat runtime with AI SDK Data Stream transport"
```

---

### Task 7: Session 管理

**Files:**
- Create: `frontend/src/components/session/SessionProvider.tsx`
- Create: `frontend/src/components/session/SessionList.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: 写入 SessionProvider**

写入 `frontend/src/components/session/SessionProvider.tsx`:

```tsx
import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";
import { useAui } from "@assistant-ui/react";
import { sessionApi, type Session, type Message } from "@/lib/api";

interface SessionContextValue {
  sessions: Session[];
  currentId: string | null;
  switchSession: (id: string) => Promise<void>;
  createSession: () => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function useSessions() {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSessions must be used within SessionProvider");
  return ctx;
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const api = useAui();

  const loadSessions = useCallback(async () => {
    try {
      const list = await sessionApi.list();
      setSessions(list);
    } catch {}
  }, []);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const switchSession = useCallback(async (id: string) => {
    setCurrentId(id);
    try {
      const messages = await sessionApi.messages(id);
      // Populate thread with message history
      const thread = api.thread();
      for (const msg of messages) {
        thread.append({
          role: msg.role as "user" | "assistant",
          content: [{ type: "text", text: msg.content }],
        });
      }
    } catch {}
  }, [api]);

  const createSession = useCallback(async () => {
    try {
      const session = await sessionApi.create();
      setCurrentId(session.id);
      await loadSessions();
    } catch {}
  }, [loadSessions]);

  const deleteSession = useCallback(async (id: string) => {
    try {
      await sessionApi.del(id);
      if (currentId === id) setCurrentId(null);
      await loadSessions();
    } catch {}
  }, [currentId, loadSessions]);

  return (
    <SessionContext.Provider
      value={{ sessions, currentId, switchSession, createSession, deleteSession }}
    >
      {children}
    </SessionContext.Provider>
  );
}
```

- [ ] **Step 2: 写入 SessionList**

写入 `frontend/src/components/session/SessionList.tsx`:

```tsx
import { useSessions } from "./SessionProvider";

export function SessionList() {
  const { sessions, currentId, switchSession, deleteSession } = useSessions();

  return (
    <div className="space-y-0.5">
      {sessions.map((s) => (
        <div
          key={s.id}
          className={`group flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm transition-colors ${
            s.id === currentId
              ? "bg-accent/10 text-accent"
              : "text-muted-foreground hover:bg-elevated hover:text-foreground"
          }`}
          onClick={() => switchSession(s.id)}
        >
          <span className="flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-md bg-elevated text-[10px]">
            {s.id === currentId ? "●" : "○"}
          </span>
          <span className="flex-1 truncate">{s.title}</span>
          <button
            className="hidden rounded p-0.5 text-muted-foreground hover:bg-danger/10 hover:text-danger group-hover:block"
            onClick={(e) => {
              e.stopPropagation();
              if (confirm("Delete this conversation?")) deleteSession(s.id);
            }}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 3: 集成到 App.tsx**

更新 `frontend/src/App.tsx` 中的 `ChatApp` 组件，在 `AssistantRuntimeProvider` 内包裹 `SessionProvider`，并用 `SessionList` 替换 sidebar 占位：

`App.tsx` 中，将 sidebar 的占位 div 改为：

```tsx
import { SessionProvider } from "@/components/session/SessionProvider";
import { SessionList } from "@/components/session/SessionList";

// 在 AssistantRuntimeProvider 内包裹 SessionProvider:
<AssistantRuntimeProvider runtime={runtime}>
  <SessionProvider>
    <AppLayout sidebar={<SessionList />}>
      <ChatView />
    </AppLayout>
  </SessionProvider>
</AssistantRuntimeProvider>
```

注意：`SessionProvider` 必须在 `AssistantRuntimeProvider` 内部（因为要使用 `useAui()`）。

- [ ] **Step 4: Commit**

```bash
cd /Users/felix/git/MultiClaw
git add frontend/src/
git commit -m "feat: add session list with CRUD sync to backend"
```

---

### Task 8: 工具审批

**Files:**
- Create: `frontend/src/components/approval/ApprovalToolUI.tsx`
- Modify: `frontend/src/components/assistant-ui/thread.tsx` (添加 ToolFallback)

- [ ] **Step 1: 写入 ApprovalToolUI**

写入 `frontend/src/components/approval/ApprovalToolUI.tsx`:

```tsx
import { useState } from "react";
import { type ToolUIProps } from "@assistant-ui/react";
import { approveApi } from "@/lib/api";

export function ApprovalToolUI({
  toolCallId,
  toolName,
  args,
  status,
}: ToolUIProps) {
  const [resolution, setResolution] = useState<string | null>(null);

  const handleApprove = async () => {
    try {
      await approveApi.submit(toolCallId, true);
      setResolution("approved");
    } catch {}
  };

  const handleReject = async () => {
    try {
      await approveApi.submit(toolCallId, false);
      setResolution("rejected");
    } catch {}
  };

  if (status !== "requires-action") {
    return null; // Use default tool rendering for non-approval cases
  }

  return (
    <div className="my-2 max-w-md rounded-lg border border-accent/20 bg-surface p-4 shadow-sm">
      <div className="mb-2 flex items-start gap-3">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-accent/30 bg-accent/10 text-accent">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
        </div>
        <div className="min-w-0 flex-1">
          <span className="block text-[10px] font-semibold uppercase tracking-wider text-accent/80">
            Approval Required
          </span>
          <span className="font-mono text-sm font-medium tracking-tight">
            {toolName}
          </span>
        </div>
      </div>

      <details className="mb-3 rounded border border-border">
        <summary className="cursor-pointer px-3 py-1.5 text-xs text-muted-foreground bg-elevated select-none">
          Raw params
        </summary>
        <pre className="max-h-[120px] overflow-y-auto bg-background p-3 font-mono text-xs text-muted-foreground whitespace-pre-wrap break-all">
          {JSON.stringify(args, null, 2)}
        </pre>
      </details>

      {resolution ? (
        <div
          className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium ${
            resolution === "approved"
              ? "bg-success/10 text-success border border-success/25"
              : "bg-danger/10 text-danger border border-danger/25"
          }`}
        >
          {resolution === "approved" ? "✓ Approved" : "✗ Rejected"}
        </div>
      ) : (
        <div className="flex gap-2">
          <button
            className="flex-1 rounded-lg bg-accent py-2 text-sm font-medium text-black hover:brightness-110"
            onClick={handleApprove}
          >
            Approve
          </button>
          <button
            className="flex-1 rounded-lg border border-border bg-elevated py-2 text-sm font-medium text-muted-foreground hover:bg-danger/10 hover:text-danger hover:border-danger"
            onClick={handleReject}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: 在 thread.tsx 中注册 ToolFallback**

读取 `frontend/src/components/assistant-ui/thread.tsx`（scaffold 生成的文件），找到 `<MessagePrimitive.If>` 或 `<ActionBarPrimitive.ToolFallback>` 相关位置。

Assistant-ui 的 Thread 组件默认如果消息中有 `tool-call` 类型的 part，会自动渲染。需要通过 `makeAssistantToolUI` 注册审批工具的自定义 UI。

在 `thread.tsx` 或新建的文件 `tool-fallback.tsx` 中：

```tsx
// frontend/src/components/assistant-ui/tool-fallback.tsx
"use client";

import { makeAssistantToolUI } from "@assistant-ui/react";
import { ApprovalToolUI } from "@/components/approval/ApprovalToolUI";

export const ApprovalTool = makeAssistantToolUI({
  toolName: "", // Empty = catch-all for tools requiring action
  render: ApprovalToolUI,
});
```

然后在 Thread 组件的 `tools` prop 中注册：

```tsx
// 在 thread.tsx 的 Thread 组件中添加:
import { ApprovalTool } from "./tool-fallback";

// <ThreadPrimitive.Root tools={[ApprovalTool]}>
```

如果 scaffold 生成的 `thread.tsx` 结构不同，在 `ThreadPrimitive.Root` 层级添加 `tools` prop 即可。

- [ ] **Step 3: Commit**

```bash
cd /Users/felix/git/MultiClaw
git add frontend/src/
git commit -m "feat: add tool approval UI for requires-action tools"
```

---

### Task 9: 构建集成和清理

**Files:**
- Modify: `frontend/vite.config.ts` (调整 build 输出)
- Modify: `src/multiclaw/server.py` (调整 static 路径)
- Delete: `src/multiclaw/static/index.html` (旧前端)

- [ ] **Step 1: 配置 Vite build 输出到后端 static 目录**

修改 `frontend/vite.config.ts`，添加 `build.outDir`:

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "../src/multiclaw/static",
    emptyOutDir: true,
  },
});
```

- [ ] **Step 2: 更新 server.py 中 static 路径**

`server.py:438-441` 当前从 static 目录读取 `index.html`。构建后 Vite 产物直接放在 `static/` 下，包括 `index.html`、`assets/` 等。

确认 `server.py` 中：
```python
_HTML_PATH = Path(__file__).parent / "static" / "index.html"
_CHAT_HTML = _HTML_PATH.read_text()
```

这行不需要改动 — Vite 构建的 `index.html` 会放在同一位置。

- [ ] **Step 3: 确保 FastAPI 也 serve Vite 构建出的 JS/CSS assets**

当前 server.py 中 `@app.get("/", response_class=HTMLResponse)` 只返回 HTML。Vite 构建会在 `static/` 下生成 `assets/` 目录。需要挂载静态文件服务：

在 `server.py` 末尾添加：

```python
from fastapi.staticfiles import StaticFiles

# Mount static files (Vite build output)
app.mount("/assets", StaticFiles(directory=str(Path(__file__).parent / "static" / "assets")), name="assets")
```

- [ ] **Step 4: 构建前端**

```sh
cd /Users/felix/git/MultiClaw/frontend
npm run build
```

Expected: 构建产出生成了 `src/multiclaw/static/index.html` 和 `src/multiclaw/static/assets/`。

- [ ] **Step 5: 验证生产模式**

```sh
cd /Users/felix/git/MultiClaw
python -m uvicorn multiclaw.server:app --host 0.0.0.0 --port 8000
```

浏览器访问 `http://localhost:8000`，确认：
- 登录页正常显示
- 登录后聊天界面正常
- 发送消息后端正确响应
- 浏览器 DevTools 无 JS 错误

- [ ] **Step 6: 删除旧前端**

```sh
cd /Users/felix/git/MultiClaw
git rm src/multiclaw/static/index.html
```

- [ ] **Step 7: Commit**

```bash
cd /Users/felix/git/MultiClaw
git add frontend/vite.config.ts src/multiclaw/server.py src/multiclaw/static/
git commit -m "feat: integrate Vite build output with FastAPI static serving"
```
