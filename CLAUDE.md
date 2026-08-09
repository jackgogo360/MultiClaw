# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development commands

```bash
# Start both backend and frontend in dev mode
./start.sh              # Backend on :15800, frontend Vite on :5173, opens browser

# Stop everything
./stop.sh

# Backend only
uv run uvicorn multiclaw.server:app --host 0.0.0.0 --port 15800 --reload

# Frontend only (from frontend/)
cd frontend && npm run dev      # Vite dev server on :5173, proxies /api to :15800
cd frontend && npm run build    # TypeScript check + production build → ../src/multiclaw/static/
cd frontend && npm run lint     # ESLint

# Python tests (run from repo root)
uv run pytest                    # All tests
uv run pytest tests/test_server.py -k "test_name"   # Single test
uv run pytest tests/ -x          # Stop on first failure
uv run pytest tests/ --lf        # Last failed only
```

## Architecture overview

### Two-process dev setup

- **Backend**: Python FastAPI server (`src/multiclaw/server.py`, port 15800) — agent runtime, SSE streaming, session/auth APIs
- **Frontend**: Vite + React 19 + TypeScript (`frontend/`, port 5173) — assistant-ui chat UI
- In dev, Vite proxies `/api` to the backend. In production, Vite builds into `src/multiclaw/static/` and FastAPI serves it as static files alongside the API on a single port.

### Assistant-UI integration (the critical wiring)

The frontend uses three `@assistant-ui` packages: `@assistant-ui/react` (primitives), `@assistant-ui/react-ai-sdk` (AI SDK transport bridge), and `@assistant-ui/react-markdown` (markdown rendering).

**Transport → Backend bridge** (`frontend/src/App.tsx:22-70`):
- `AssistantChatTransport` is configured with `api: "/api/chat"` and `credentials: "include"` (JWT cookie auth).
- `prepareSendMessagesRequest` extracts the latest user message text from assistant-ui's message array and sends it as `{ message, session_id }` — not the full message history. The backend does its own conversation history management.
- `onData` listens for `data-session` transient parts from the SSE stream and syncs session metadata into `sessionStore`.

**SSE stream protocol** (`src/multiclaw/stream.py`):
- The backend emits Vercel AI SDK Data Stream v1 SSE events (`X-Vercel-AI-Data-Stream: v1` header).
- `DataStreamEncoder` maps internal events to AI SDK wire format: `text-start/delta/end`, `reasoning-start/delta/end`, `tool-input-available`, `tool-output-available`, `tool-approval-request`, `data-*` for custom payloads.
- The `/api/chat` handler (`server.py:323-498`) orchestrates: session resolution, SSE event loop with token queue + event bus subscription, part lifecycle management (open/close text and reasoning parts around tool calls).

**Custom assistant-ui components** (`frontend/src/components/assistant-ui/`):
- `thread.tsx` — `ThreadPrimitive.Root`, `MessagePrimitive.Parts` with `assistantMessagePartsComponents` for assistant messages (user messages render plain text). Custom send button label states and `ComposerPrimitive` styling.
- `thread-list.tsx` — `ThreadListPrimitive` sidebar with thread CRUD.
- `ToolGroup.tsx` — Collapsible `<details>` wrapper for grouped tool call parts, showing tool name + count summary and status.
- `tool-fallback.tsx` — `tools.Override` on `MessagePrimitive.Parts`; renders `ApprovalToolUI` when `status.type === "requires-action"`, otherwise shows a compact tool call card.
- `MarkdownText.tsx` — Wraps `MarkdownTextPrimitive` with custom `.aui-md` CSS styling.

**Session management** is split between two systems:
1. assistant-ui's internal thread runtime (`useChatRuntime`) — manages the message list displayed in the current thread view.
2. `sessionStore` (`frontend/src/lib/session-store.ts`) — custom external store that tracks sessions list, current session ID, and hydration state. `SessionProvider` (`components/session/SessionProvider.tsx`) bridges these: loading session messages from the backend API into assistant-ui's thread via `thread.reset()`, and reacting to "new chat" by resetting the thread.

### Other key backend modules

| Module | Purpose |
|--------|---------|
| `src/multiclaw/agent/` | Core agent loop, tool orchestration |
| `src/multiclaw/tools/` | Tool implementations (file ops, shell, web, etc.) |
| `src/multiclaw/llm/` | LLM provider routing (OpenAI, Anthropic, DeepSeek) |
| `src/multiclaw/memory/` | SQLite-backed conversation memory |
| `src/multiclaw/session/` | SQLite session store (CRUD, messages) |
| `src/multiclaw/auth/` | JWT cookie auth, email verification (Resend/Brevo) |
| `src/multiclaw/governance/` | Permission checker, sandbox, audit logging |
| `src/multiclaw/events/` | Internal event bus (used for tool approval events in SSE loop) |
| `src/multiclaw/config/` | Settings from `multiclaw.toml` via pydantic-settings |
| `src/multiclaw/skills/` | Skill manager for loading agent skills |

### Auth flow

JWT in httpOnly cookie. `AuthMiddleware` skips `/auth/`, `/api/auth/`, `/`, and `/multiclaw.png`. All other routes return 401 without a valid token. Frontend `AuthProvider` calls `/api/auth/me` on mount to check session, renders `LoginOverlay` until authenticated.

### Styling

Tailwind CSS v4 with `@tailwindcss/vite`. Dark theme defined as CSS custom properties in `@theme` block (`frontend/src/index.css`). No Tailwind config file — v4 uses CSS-first configuration.
