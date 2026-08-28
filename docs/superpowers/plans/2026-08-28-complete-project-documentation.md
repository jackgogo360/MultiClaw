# MultiClaw Complete Project Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一套面向开发者与贡献者的完整中文文档体系，并用无第三方依赖的自动化检查防止链接、接口、配置和安全说明漂移。

**Architecture:** 根 README 只承担项目入口和最短开发路径，详细事实按架构、配置、开发、API、测试、安全、部署和故障排查拆入 `docs/`。治理文件位于仓库根目录；`scripts/check_docs.py` 以只读方式校验正式文档，pytest 锁定检查器行为，CI 在独立 job 中执行文档门禁。

**Tech Stack:** Markdown、Python 3.12 标准库、pytest、GitHub Actions、Mermaid、Apache License 2.0、Contributor Covenant 2.1。

---

## Scope decision

- 文档仅提供中文版本，不创建英文空文件。
- 主要读者是开发者和贡献者；部署运维内容作为必要参考。
- 基于 `feature/multi-tenant-implementation`，不修改生产行为、数据库模式、冻结迁移或依赖。
- 项目状态固定表述为 `0.1.0` 开发阶段、尚未正式发布。
- SQLite 本地流程必须可执行；MySQL 8.0.36 由 CI 验证，本地无服务时如实说明参数化分支跳过。
- 正式健康路径统一为 `/api/health/live` 和 `/api/health/ready`。
- `docs/superpowers/` 是设计和实施记录，不进入新开发者主导航。

## File structure

### Create

- `README.md`：项目入口、状态、快速开始、架构概览和文档导航。
- `LICENSE`：Apache License 2.0 官方文本。
- `CONTRIBUTING.md`：开发、分支、提交、测试、PR 和 Lore trailer 规范。
- `SECURITY.md`：支持范围、漏洞私密报告和敏感信息规则。
- `CODE_OF_CONDUCT.md`：Contributor Covenant 2.1 中文版及执行渠道。
- `CHANGELOG.md`：Keep a Changelog 格式的未发布版本记录。
- `docs/README.md`：正式文档索引。
- `docs/getting-started.md`：从干净检出到 SQLite 开发启动。
- `docs/architecture.md`：组件图、租户请求链路和持久化工作流时序。
- `docs/configuration.md`：所有 Settings 分组和环境变量规则。
- `docs/development.md`：目录、后端、前端、静态构建和调试流程。
- `docs/api.md`：公开路由分组、认证/CSRF、SSE 和状态语义。
- `docs/testing.md`：后端、前端、文档、MySQL 和原生沙箱测试矩阵。
- `docs/deployment.md`：单机发布、数据库、健康门禁、回滚和备份。
- `docs/security-model.md`：信任边界、隔离、加密、MCP、沙箱和删除恢复。
- `docs/troubleshooting.md`：症状、原因、检查和修复路径。
- `scripts/check_docs.py`：无网络、只读的 Markdown/内容检查器。
- `tests/test_documentation.py`：检查器和正式文档契约测试。

### Modify

- `.github/workflows/ci.yml:10-69`：增加独立 `documentation` job。
- `docs/multi-tenant-operations.md:1-88`：重写为中文，保留可执行运维契约。
- `docs/sandbox-deployment.md:1-159`：重写为中文，移除历史机器状态并修正健康路径。
- `frontend/README.md:1-77`：替换 Vite 模板，保留前端独立开发说明并链接根文档。

### Preserve

- `alembic/versions/20260815_0001_multi_tenant_baseline.py`：冻结迁移不得修改。
- `src/multiclaw/static/`：只能由 `npm run build` 重建，不手工编辑。
- `docs/superpowers/` 中除本计划和已批准设计规范外的历史记录：不重写。

## Task 1: Lock the documentation contract with tests

**Files:**
- Create: `tests/test_documentation.py`
- Create: `scripts/check_docs.py`

- [ ] **Step 1: Write the failing documentation contract tests**

Create `tests/test_documentation.py` with four behaviors:

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "docs/README.md",
    "docs/getting-started.md",
    "docs/architecture.md",
    "docs/configuration.md",
    "docs/development.md",
    "docs/api.md",
    "docs/testing.md",
    "docs/deployment.md",
    "docs/security-model.md",
    "docs/troubleshooting.md",
    "docs/multi-tenant-operations.md",
    "docs/sandbox-deployment.md",
    "frontend/README.md",
)


def test_required_documentation_files_exist_and_are_not_empty() -> None:
    for relative_path in REQUIRED_FILES:
        path = ROOT / relative_path
        assert path.is_file(), relative_path
        assert path.read_text(encoding="utf-8").strip(), relative_path


def test_documentation_checker_passes_repository_documents() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_docs.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_readme_exposes_the_supported_developer_entry_points() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for required_text in (
        "uv sync",
        "multiclaw db upgrade",
        "multiclaw db check",
        "npm ci",
        "npm run build",
        "CONTRIBUTING.md",
        "SECURITY.md",
    ):
        assert required_text in readme


def test_formal_docs_use_current_health_routes() -> None:
    formal_docs = [ROOT / name for name in REQUIRED_FILES if name.endswith(".md")]
    joined = "\n".join(path.read_text(encoding="utf-8") for path in formal_docs)
    assert "/api/health/live" in joined
    assert "/api/health/ready" in joined
    assert "`/health/live`" not in joined
    assert "`/health/ready`" not in joined
```

- [ ] **Step 2: Run the contract and observe the missing-document failure**

Run:

```bash
uv run pytest tests/test_documentation.py -q
```

Expected: failure naming `README.md` before any documentation files are created.

- [ ] **Step 3: Implement the standard-library checker**

Create `scripts/check_docs.py` with these concrete functions:

- `formal_documents(root: Path) -> tuple[Path, ...]` returns the exact Markdown paths from `REQUIRED_FILES` plus root governance Markdown files.
- `github_slug(value: str) -> str` lowercases headings, removes Markdown formatting and punctuation, and joins whitespace with `-` while retaining Chinese characters.
- `heading_anchors(text: str) -> set[str]` returns GitHub-style anchors and applies numeric suffixes to duplicate headings.
- `markdown_links(text: str) -> Iterator[tuple[str, str]]` yields link label and destination from inline Markdown links while excluding images.
- `check_links(root: Path, documents: Iterable[Path]) -> list[str]` verifies local files and optional anchors; external `http`, `https`, `mailto` and absolute site paths are skipped.
- `check_content(root: Path, documents: Iterable[Path]) -> list[str]` rejects unresolved-work markers assembled as `"TO" "DO"`, `"TB" "D"`, `"FIX" "ME"`, the old backticked health routes, removed SQLite repository names, private-key headers and long Brevo-style tokens.
- `check_required_content(root: Path) -> list[str]` enforces README commands, all top-level Settings groups in `docs/configuration.md`, and all public API route groups in `docs/api.md`.
- `main() -> int` prints one `path: issue` line per error and returns `1`; otherwise prints `documentation check passed` and returns `0`.

The script must define its own exact required-file tuple so it remains usable without importing pytest code. It must read UTF-8, stay inside the repository root, avoid network access and never write files.

- [ ] **Step 4: Run focused checker tests**

Run:

```bash
uv run pytest tests/test_documentation.py -q
```

Expected: required-document failures remain, while the checker script itself imports and exits with actionable missing-file messages rather than crashing.

- [ ] **Step 5: Commit the contract**

```bash
git add scripts/check_docs.py tests/test_documentation.py
git commit -m "Keep contributor documentation executable"
```

## Task 2: Add governance and project-lifecycle documents

**Files:**
- Create: `LICENSE`
- Create: `CONTRIBUTING.md`
- Create: `SECURITY.md`
- Create: `CODE_OF_CONDUCT.md`
- Create: `CHANGELOG.md`

- [ ] **Step 1: Add the approved legal and governance documents**

Use the unmodified Apache License 2.0 text in `LICENSE` with the standard January 2004 header and `http://www.apache.org/licenses/` reference.

Write the remaining files with these exact section contracts:

- `CONTRIBUTING.md`: “开始之前”“开发环境”“选择改动范围”“分支与提交”“测试要求”“Pull Request”“代码风格”“文档维护”。Include the exact backend/frontend/doc commands and the repository Lore trailer format.
- `SECURITY.md`: “支持范围”“私密报告漏洞”“报告内容”“响应流程”“敏感信息”“安全设计”。Direct reports to GitHub Security Advisories and prohibit public issues for undisclosed vulnerabilities.
- `CODE_OF_CONDUCT.md`: Contributor Covenant 2.1 Chinese text with scope, enforcement, four consequence levels, attribution URL and repository-maintainer contact through private security or repository channels.
- `CHANGELOG.md`: Keep a Changelog introduction, semantic-versioning statement, an `[未发布]` section, and a `0.1.0` development-baseline entry dated 2026-08-28 covering multi-tenancy, durable workflow, Secret encryption, native sandbox and SQLite/MySQL support.

- [ ] **Step 2: Verify governance content and secret hygiene**

Run:

```bash
uv run pytest tests/test_documentation.py::test_required_documentation_files_exist_and_are_not_empty -q
rg -n 'xkeysib-[A-Za-z0-9_-]{20,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,}' LICENSE CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md
```

Expected: the pytest still fails only for remaining content documents; the secret scan returns no matches.

- [ ] **Step 3: Commit governance documents**

```bash
git add LICENSE CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md
git commit -m "Give contributors explicit project governance"
```

## Task 3: Build the README, documentation index, and first-run path

**Files:**
- Create: `README.md`
- Create: `docs/README.md`
- Create: `docs/getting-started.md`
- Modify: `frontend/README.md`

- [ ] **Step 1: Write the root README**

Use this section order and content boundary:

1. `# MultiClaw` with `multiclaw.png`, a one-sentence description and a blockquote warning that `0.1.0` is unreleased development software.
2. “核心能力” covering agent/tool/MCP, tenant isolation, durable workflow/approval, encrypted BYOK Secret, native sandbox and SQLite/MySQL.
3. “支持范围” table with Python `>=3.12`, Node `22` for CI, SQLite, MySQL `8.0.36+` major version 8, macOS Seatbelt, Linux nsjail and standalone deployment.
4. “快速开始” with `uv sync`, `npm ci`, safe local JWT/keyring generation, `multiclaw db upgrade`, `multiclaw db check`, backend and frontend commands, and URLs `5173`, `15800`, `/docs`, `/api/health/live`, `/api/health/ready`.
5. “架构概览” Mermaid flow linking browser → FastAPI/auth → TenantContext → TenantUnitOfWork/RuntimePool → workflow/events/secrets.
6. “文档导航”“验证”“参与贡献”“安全”“许可证”“当前限制”。

The quick start must state that `start.sh` is a convenience command that binds both services to `0.0.0.0`, terminates processes already using its ports and tails the backend log; it is not a production service manager.

- [ ] **Step 2: Write the documentation index and getting-started guide**

`docs/README.md` groups links into:

- 入门：getting started and root README.
- 开发：development, architecture, configuration, API, testing and contributing.
- 安全：security model, security policy and sandbox deployment.
- 部署运维：deployment, multi-tenant operations and troubleshooting.
- 设计记录：approved multi-tenant architecture and documentation design only.

`docs/getting-started.md` must include prerequisites, clone/install, secure temporary local secrets, SQLite initialization, two-terminal startup, optional `start.sh`, first login with mock email provider, health checks, shutdown and next links. Production warnings must be adjacent to unsafe development examples.

- [ ] **Step 3: Replace the Vite template README**

`frontend/README.md` must cover the React 19/Vite 8 purpose, `npm ci`, `npm run dev`, `npm run lint`, `npm run build`, output path `src/multiclaw/static/`, proxy/API expectations, component/store layout and a link back to `../README.md` plus `../docs/development.md`.

- [ ] **Step 4: Run entry-point contract checks**

```bash
uv run pytest tests/test_documentation.py -q
uv run python scripts/check_docs.py
```

Expected: failures are limited to topic documents not yet created; README entry-point assertions pass.

- [ ] **Step 5: Commit the entry path**

```bash
git add README.md docs/README.md docs/getting-started.md frontend/README.md
git commit -m "Make the contributor path discoverable"
```

## Task 4: Document architecture, configuration, development, and API contracts

**Files:**
- Create: `docs/architecture.md`
- Create: `docs/configuration.md`
- Create: `docs/development.md`
- Create: `docs/api.md`

- [ ] **Step 1: Write the architecture guide from production boundaries**

Include:

- Scope and standalone non-goals.
- Repository/module map.
- Mermaid component diagram.
- Authenticated request sequence through auth middleware, TenantContext, UoW and repository.
- RuntimePool identity, capacity, eviction and shutdown.
- Exact-scope EventRouter and SSE behavior.
- Workflow lease/fencing/CAS, checkpoints, approvals, serial tools and recovery.
- Secret envelope/keyring/fallback behavior.
- Delayed deletion and purge worker.
- SQLite/MySQL dialect boundary and Alembic ownership.

Every component name must link to its current production module.

- [ ] **Step 2: Write the complete configuration reference**

For each top-level group `app`, `deployment`, `database`, `workspace`, `runtime`, `workflow`, `secrets`, `deletion`, `llm`, `memory`, `governance`, `tools`, `agent`, `skills`, `auth`, `email`, `brevo`, `resend`, and `mcp`, record:

- TOML key.
- Matching `MULTICLAW_` environment-variable form with `__` nesting.
- Type/default and validation bounds.
- Whether the value is safe for config, must come from environment/file, or is production-sensitive.

Explain precedence: explicit constructor arguments override config-derived data; environment variables override TOML; `MULTICLAW_SECRETS_KEYRING_B64` and `MULTICLAW_AUTH_JWT_SIGNING_KEY` are loaded by their security components rather than copied into Settings fields.

- [ ] **Step 3: Write the development guide**

Cover repository layout, `uv sync`, backend reload server, frontend Vite server, generated static assets, start/stop script side effects, logging, config overrides, MCP/skill toggles, focused tests, formatting expectations, debugging readiness and avoiding edits to hashed assets or frozen migration.

- [ ] **Step 4: Write the API overview**

Document:

- `/auth`: CSRF, send/verify code, deletion-recovery code, logout and current user.
- `/api/sessions`: list/create/update/archive/restore/delete/messages and exact run event stream.
- `/api/chat`: authenticated SSE chat and first run-control event.
- `/api/approvals`: scoped lookup and decision; `/api/approve` as a compatibility alias only.
- `/api/secrets`: list, upsert, delete and test without returning plaintext.
- `/api/account/deletion`: request, status and recover.
- `/api/health/live` and `/api/health/ready`.

State cookie, Origin, double-submit CSRF, tenant-scope and error-shape rules. Link to runtime `/docs` and `/openapi.json` instead of duplicating full schemas.

- [ ] **Step 5: Run source-of-truth checks**

```bash
uv run pytest tests/test_documentation.py -q
uv run python scripts/check_docs.py
rg -n '`/health/(live|ready)`|aiosqlite\.connect|SqliteRepository|database\.path' README.md docs frontend/README.md
```

Expected: documentation tests and checker pass except for remaining operations documents; the legacy scan returns no matches.

- [ ] **Step 6: Commit developer reference documents**

```bash
git add docs/architecture.md docs/configuration.md docs/development.md docs/api.md
git commit -m "Expose the implemented contributor contracts"
```

## Task 5: Complete testing, security, deployment, operations, and support guides

**Files:**
- Create: `docs/testing.md`
- Create: `docs/deployment.md`
- Create: `docs/security-model.md`
- Create: `docs/troubleshooting.md`
- Modify: `docs/multi-tenant-operations.md`
- Modify: `docs/sandbox-deployment.md`

- [ ] **Step 1: Write testing and deployment guides**

`docs/testing.md` must include the default full suite, focused pytest, document checker, frontend install/audit/lint/build, MySQL URL and CI matrix, native sandbox opt-in markers, expected skip behavior and release evidence rules.

`docs/deployment.md` must include standalone topology, production prerequisites, config/secret inputs, SQLite and MySQL examples without credentials, backup/restore rehearsal, explicit `db upgrade`/`db check`, API startup, liveness/readiness, static frontend serving, rollback and unsupported cluster behavior.

- [ ] **Step 2: Write the security model**

Describe assets, trusted operator inputs, untrusted tenants/workspaces/MCP configs/model output, authentication and CSRF, tenant scoping, database constraints, Secret AESGCM envelope and fixed AAD, no silent platform fallback, redaction, exact event routing, tool approval/recovery, native sandbox and deletion lifecycle. Clearly label macOS breakaway-child cleanup and unexecuted Linux native verification as limitations rather than fixed guarantees.

- [ ] **Step 3: Rewrite the multi-tenant operations guide in Chinese**

Preserve and update database release, JWT/keyring, health gates, purge worker, rotation, backend notes and v1 non-goals. Add links to deployment/configuration/security/troubleshooting. Remove historical release-gate wording that implies this guide is temporary.

- [ ] **Step 4: Rewrite the sandbox deployment guide in Chinese**

Preserve macOS Seatbelt and Linux nsjail prerequisites, least-privilege MCP grants, runner output limits, accepted breakaway-child risk, unsafe development mode and native test commands. Replace all `/health/*` references with `/api/health/*`. Remove fixed package versions, old pass counts, named-machine details and time-sensitive final review claims.

- [ ] **Step 5: Write troubleshooting by symptom**

Cover at least:

- `db check` failure or pre-Alembic SQLite file.
- driver/URL mismatch.
- readiness `503` for schema, foreign keys, keyring, workspace, MySQL or sandbox.
- missing JWT/keyring sources.
- email code not delivered in mock/real providers.
- CSRF `403`.
- empty/cross-tenant sessions.
- SSE disconnect or approval awaiting state.
- MCP config not auto-connecting.
- frontend proxy/port/static-asset issues.
- purge retries and key-rotation failures.

Each item uses “症状 / 常见原因 / 检查 / 处理” and never recommends disabling production safety gates.

- [ ] **Step 6: Run the complete documentation gate**

```bash
uv run pytest tests/test_documentation.py -q
uv run python scripts/check_docs.py
rg -n 'xkeysib-[A-Za-z0-9_-]{20,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,}' README.md CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md docs frontend/README.md
```

Expected: tests and checker pass; secret scan returns no matches.

- [ ] **Step 7: Commit operational and security documents**

```bash
git add docs/testing.md docs/deployment.md docs/security-model.md docs/troubleshooting.md docs/multi-tenant-operations.md docs/sandbox-deployment.md
git commit -m "Make deployment and security limits operable"
```

## Task 6: Gate documentation in CI and complete release verification

**Files:**
- Modify: `.github/workflows/ci.yml:10-69`
- Modify: `scripts/check_docs.py`
- Modify: `tests/test_documentation.py`
- Modify: documentation files only when verification exposes inaccuracies

- [ ] **Step 1: Add the independent CI job**

Add before `backend`:

```yaml
  documentation:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          python-version: "3.12"
      - run: uv sync --locked
      - run: uv run python scripts/check_docs.py
      - run: uv run pytest tests/test_documentation.py -q
```

- [ ] **Step 2: Verify documentation and YAML syntax inputs**

```bash
uv run python scripts/check_docs.py
uv run pytest tests/test_documentation.py -q
uv lock --check
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 3: Run the full backend suite**

```bash
uv run pytest -q
```

Expected: all available SQLite/default tests pass; MySQL and native platform tests may skip only when their documented external inputs are unavailable.

- [ ] **Step 4: Run the full frontend gate**

```bash
cd frontend
NPM_CONFIG_REGISTRY=https://registry.npmjs.org npm ci
NPM_CONFIG_REGISTRY=https://registry.npmjs.org npm audit
npm run lint
npm run build
cd ..
```

Expected: install, audit, lint and build exit `0`; rebuilding static assets leaves no diff.

- [ ] **Step 5: Run final repository scans**

```bash
rg -n 'TO''DO|TB''D|FI''XME|待''定|待''确认' README.md CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md docs frontend/README.md scripts/check_docs.py tests/test_documentation.py
rg -n '`/health/(live|ready)`|aiosqlite\.connect|SqliteRepository|database\.path' README.md docs frontend/README.md
rg -n 'xkeysib-[A-Za-z0-9_-]{20,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,}' README.md CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md docs frontend/README.md
git hash-object alembic/versions/20260815_0001_multi_tenant_baseline.py
git status --short
```

Expected: content scans return no matches; migration hash remains `a32d7b5595a455c857bfe6bb2a0b031d0cd222f7`; status shows only the intended CI change before the final commit.

- [ ] **Step 6: Commit the CI gate**

```bash
git add .github/workflows/ci.yml scripts/check_docs.py tests/test_documentation.py
git commit -m "Prevent formal documentation from drifting"
```

- [ ] **Step 7: Request independent specification and quality review**

The reviewer must compare the final tree to `docs/superpowers/specs/2026-08-28-complete-project-documentation-design.md`, verify commands and links from source, report issues by severity, and explicitly state local MySQL/native/browser limitations.

- [ ] **Step 8: Finish with a clean-tree verification**

```bash
git status --short --branch
git log --oneline --decorate -8
```

Expected: branch `docs/complete-project-documentation`, clean worktree, documentation commits present and no unrelated production changes.
