# Repository Guidelines

## Project Structure & Module Organization

MultiClaw combines a Python 3.12 agent runtime with a React frontend. Backend code lives in `src/multiclaw/`; major packages include `agent/`, `tools/`, `llm/`, `memory/`, `session/`, `auth/`, `governance/`, and `mcp/`. Python tests are flat modules under `tests/`. The Vite/React 19 application is in `frontend/src/`, with reusable UI under `components/`. Production frontend output is generated into `src/multiclaw/static/`; do not edit hashed assets there by hand. Configuration examples live in `multiclaw.toml`, `config/`, and `.mcp.json`. Design notes and implementation plans are under `docs/superpowers/`.

## Build, Test, and Development Commands

- `uv sync` installs Python and development dependencies from `uv.lock`.
- `uv run pytest` runs the complete backend test suite.
- `uv run pytest tests/test_server.py -k test_name` runs a focused test.
- `./start.sh` starts FastAPI on port 15800 and Vite on port 5173; `./stop.sh` stops both.
- `cd frontend && npm install` installs frontend dependencies.
- `cd frontend && npm run dev` starts the Vite development server.
- `cd frontend && npm run lint` checks TypeScript and React code with ESLint.
- `cd frontend && npm run build` type-checks and writes the production bundle to `src/multiclaw/static/`.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase` classes, and type annotations in Python. Follow existing async patterns and keep configuration in Pydantic settings models. TypeScript is strict: use two-space indentation, `PascalCase` React components, `camelCase` functions, and the `@/` import alias. Prefer existing stores, primitives, and utilities over new abstractions.

## Testing Guidelines

Pytest and `pytest-asyncio` are configured with automatic async support. Name files `test_<area>.py`, functions `test_<behavior>`, and test classes `Test<Subject>`. Add regression coverage for backend behavior changes. No frontend test runner is configured, so UI changes must at least pass lint and build, with manual browser verification documented in the PR.

## Commit & Pull Request Guidelines

Recent history favors concise Conventional Commit subjects such as `feat: wire MCP into server lifecycle`, followed by explanatory bullets for larger changes. Keep commits focused. Include relevant Lore trailers after a blank line, especially `Tested:`, `Confidence:`, and `Scope-risk:`. Pull requests should explain user-visible and architectural impact, link issues, list verification commands, call out configuration changes, and include screenshots for UI work.

## Security & Configuration Tips

Never commit API keys, tokens, `.env` files, databases, or runtime logs. Treat auth, sandbox, MCP filtering, and permission changes as security-sensitive and test both allowed and denied paths.
