# Multi-Tenant Standalone v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a standalone MultiClaw runtime where every authenticated user is an isolated tenant, with one default workspace, SQLite/MySQL deployment-level storage selection, durable workflow recovery, encrypted BYOK secrets, and recoverable delayed account deletion.

**Architecture:** Replace the independent handwritten SQLite stores and process-global agent with one SQLAlchemy Core data plane, scope-bound units of work, and a per-tenant runtime pool. Persist run, approval, execution, and checkpoint state behind fencing-aware workflow services; derive all tenant scope from authenticated identity; expose only precisely scoped API/SSE data. Both database backends implement the same repository contracts and Alembic baseline, while backend-specific transaction/clock behavior stays in one dialect adapter.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x Core async, Alembic async, `sqlite+aiosqlite`, `mysql+aiomysql`, `cryptography` AESGCM, PyJWT, pytest/pytest-asyncio, React 19, TypeScript 6, assistant-ui, Vite.

**Approved design:** `docs/superpowers/specs/2026-08-15-multi-tenant-architecture-design.md`

---

## Scope decision

This remains one implementation plan because partial delivery would create an unsafe state in which the product could appear tenant-aware while runtime, workflow, Secret, or deletion state is still global. Tasks are independently testable and commit-sized, but none authorizes a public multi-tenant claim until Task 18 passes the complete release gate.

The implementation is deliberately limited to:

- `user = tenant`
- one bootstrapped default workspace per user, without workspace switching UI
- no workspace switching or creation API in v1
- one deployment-selected backend: SQLite or MySQL
- standalone process-local runtime pooling
- forward-only baseline for a development-stage product; no legacy row migration or dual-read path
- no application-level superadmin, break-glass route, or cross-user data access path

## Execution constraints

- Create an isolated worktree before source implementation.
- Follow TDD in every task: add the focused failing test, observe the expected failure, add the minimum implementation, observe the pass, then run the listed regression set.
- Run MySQL contract tests against MySQL `>= 8.0.36`; do not replace them with mocks or SQLite.
- Use file-backed SQLite for transaction, migration, foreign-key, and concurrency tests; `:memory:` is allowed only for pure SQL-expression unit tests.
- Keep one connection and one transaction per UoW. Repositories must never call `commit()`, open engines, or accept raw tenant IDs independent of their UoW context.
- Do not retain compatibility reads from `chat_sessions.user_id`, handwritten `CREATE TABLE IF NOT EXISTS`, `AuthStore`, `SqliteSessionStore`, `SqliteMemory`, or `SqliteRepository` after their replacement task.
- Never accept `tenant_id` from an HTTP body, header, path, or query.
- Never serialize Secret plaintext into logs, SSE, audit detail, checkpoint payloads, traces, metrics, or frontend state.
- Preserve existing sandbox containment and MCP trust restrictions while moving them into per-tenant runtimes.
- Every commit must use the repository Lore trailers and list the exact focused tests run.

## File structure

### Create

- `alembic.ini` — Alembic command configuration.
- `alembic/env.py` — async migration environment using the deployment-selected engine.
- `alembic/script.py.mako` — forward-only revision template.
- `alembic/versions/20260815_0001_multi_tenant_baseline.py` — immutable development baseline for all v1 tables and constraints.
- `.github/workflows/ci.yml` — backend SQLite/MySQL matrix plus frontend lint/build gates.
- `src/multiclaw/cli.py` — `multiclaw db upgrade|current|check` commands.
- `src/multiclaw/auth/cleanup.py` — expired verification-code cleanup worker.
- `src/multiclaw/tenancy/__init__.py` — public tenancy contracts.
- `src/multiclaw/tenancy/context.py` — immutable `TenantContext` and request binding.
- `src/multiclaw/tenancy/workspace.py` — default-workspace bootstrap and canonical workspace resolution.
- `src/multiclaw/storage/engine.py` — async engine creation, connection setup, and lifecycle.
- `src/multiclaw/storage/dialect.py` — SQLite/MySQL DB clock and locking semantics.
- `src/multiclaw/storage/schema.py` — SQLAlchemy Core `MetaData` and all table objects.
- `src/multiclaw/storage/uow.py` — `AuthUnitOfWork` and `TenantUnitOfWork`.
- `src/multiclaw/storage/repositories/__init__.py` — scoped repository exports.
- `src/multiclaw/storage/repositories/auth.py` — users and verification-code operations.
- `src/multiclaw/storage/repositories/sessions.py` — workspace/session operations.
- `src/multiclaw/storage/repositories/memory.py` — tenant/workspace/session memory operations.
- `src/multiclaw/storage/repositories/workflow.py` — run, approval, execution, checkpoint, and audit operations.
- `src/multiclaw/storage/repositories/secrets.py` — encrypted Secret row operations.
- `src/multiclaw/storage/repositories/deletions.py` — deletion job and ordered purge operations.
- `src/multiclaw/runtime/__init__.py` — runtime public surface.
- `src/multiclaw/runtime/models.py` — `TenantRuntime` and runtime capacity errors.
- `src/multiclaw/runtime/factory.py` — per-tenant Agent/Skill/Tool/MCP/Sandbox assembly.
- `src/multiclaw/runtime/pool.py` — create locks, quotas, eviction, and shutdown.
- `src/multiclaw/events/router.py` — exact scope subscriptions on tenant-local buses.
- `src/multiclaw/workflow/__init__.py` — workflow public surface.
- `src/multiclaw/workflow/models.py` — typed statuses, checkpoint phases, and transition requests.
- `src/multiclaw/workflow/coordinator.py` — sole state-transition service.
- `src/multiclaw/workflow/recovery.py` — lease takeover, fencing, replay, and block outcomes.
- `src/multiclaw/secrets/__init__.py` — Secret service public surface.
- `src/multiclaw/secrets/keyring.py` — deployment-keyring loader and readiness checks.
- `src/multiclaw/secrets/envelope.py` — AES-256-GCM envelope and fixed length-prefix AAD.
- `src/multiclaw/secrets/resolver.py` — strict user-secret/platform-fallback resolution.
- `src/multiclaw/secrets/rotation.py` — idempotent active-key re-encryption batches.
- `src/multiclaw/deletion/__init__.py` — deletion public surface.
- `src/multiclaw/deletion/service.py` — request, status, and recovery transitions.
- `src/multiclaw/deletion/worker.py` — asynchronous idempotent purge coordinator.
- `src/multiclaw/api/__init__.py` — API package.
- `src/multiclaw/api/dependencies.py` — auth, tenant context, UoW, runtime, and CSRF dependencies.
- `src/multiclaw/api/sessions.py` — scoped session endpoints.
- `src/multiclaw/api/chat.py` — run creation and scoped SSE endpoint.
- `src/multiclaw/api/approvals.py` — persisted approval query/decision endpoints.
- `src/multiclaw/api/secrets.py` — BYOK metadata/write/delete/test endpoints.
- `src/multiclaw/api/account.py` — deletion request/status/recovery endpoints.
- `src/multiclaw/api/health.py` — live/ready endpoints.
- `src/multiclaw/security/__init__.py` — security helper exports.
- `src/multiclaw/security/csrf.py` — origin and CSRF double-submit validation.
- `src/multiclaw/security/redaction.py` — shared recursive Secret redactor.
- `src/multiclaw/observability.py` — redacted trace events and low-cardinality counters.
- `docs/multi-tenant-operations.md` — database, keyring, readiness, rotation, and purge operations.
- `frontend/src/components/settings/SettingsPanel.tsx` — account settings shell.
- `frontend/src/components/settings/SecretSettings.tsx` — masked BYOK management.
- `frontend/src/components/settings/DeletionSettings.tsx` — deletion confirmation/status/recovery UI.
- `frontend/src/lib/security.ts` — CSRF token generation and request header helper.
- `tests/database_fixtures.py` — SQLite/MySQL engine/UoW fixtures shared by contract tests.
- `tests/test_database_config.py`
- `tests/test_storage_engine.py`
- `tests/test_migrations.py`
- `tests/test_schema_contract.py`
- `tests/test_tenant_context.py`
- `tests/test_tenant_uow.py`
- `tests/test_scoped_repositories.py`
- `tests/test_workspace_resolver.py`
- `tests/test_runtime_pool.py`
- `tests/test_runtime_isolation.py`
- `tests/test_event_router.py`
- `tests/test_workflow_state.py`
- `tests/test_workflow_recovery.py`
- `tests/test_scheduler_persistence.py`
- `tests/test_secret_envelope.py`
- `tests/test_secret_resolver.py`
- `tests/test_secret_rotation.py`
- `tests/vectors/secret_envelope_v1.json`
- `tests/test_auth_tenant_boundary.py`
- `tests/test_csrf.py`
- `tests/test_deletion_service.py`
- `tests/test_deletion_worker.py`
- `tests/test_tenant_api.py`
- `tests/test_tenant_sse.py`
- `tests/test_readiness.py`
- `tests/test_secret_redaction.py`
- `tests/integration/test_mysql_contract.py`
- `tests/integration/test_tenant_e2e.py`
- `tests/integration/test_workflow_faults.py`

### Modify

- `pyproject.toml` and `uv.lock` — dependencies and CLI entry point.
- `multiclaw.toml` and `config/multiclaw.toml` — non-secret standalone configuration examples.
- `tests/conftest.py` — clear database/keyring/JWT environment and expose backend fixtures.
- `src/multiclaw/config/settings.py` — typed deployment/database/runtime/workflow/Secret/deletion configuration.
- `src/multiclaw/auth/models.py` — auth epoch, account status, purpose-aware verification requests, and recovery response types.
- `src/multiclaw/auth/middleware.py` — signed token validation plus database-backed account/epoch checks.
- `src/multiclaw/auth/router.py` — digest verification, default-workspace bootstrap, secure cookies, and recovery-token flow.
- `src/multiclaw/session/models.py` — replace `user_id` with tenant/workspace scope and epoch-millisecond timestamps.
- `src/multiclaw/memory/models.py` and `src/multiclaw/memory/protocol.py` — require complete scope on persistent operations.
- `src/multiclaw/agent/base.py`, `context.py`, `multiclaw.py`, and `tool_batch.py` — propagate scope/run IDs, checkpoints, leases, and serial tool execution.
- `src/multiclaw/events/types.py` and `bus.py` — typed scoped events on tenant-local buses.
- `src/multiclaw/tools/base.py`, `registry.py`, and `scheduler.py` — recovery declarations and persisted approval/execution state.
- `src/multiclaw/skills/manager.py` — runtime ownership and deterministic shutdown.
- `src/multiclaw/llm/router.py` — per-call resolved credentials rather than startup-bound plaintext settings.
- `src/multiclaw/mcp/manager.py`, `config.py`, and `transport/factory.py` — tenant workspace/Secret resolution and runtime shutdown.
- `src/multiclaw/governance/audit.py` and `models.py` — persisted, scoped, redacted audit events.
- `src/multiclaw/server.py` — app composition only; remove global agent/event bus and inline business routes.
- `src/multiclaw/stream.py` — scoped run control events.
- `frontend/src/App.tsx` — CSRF-aware chat requests and settings navigation.
- `frontend/src/lib/api.ts` — typed errors, CSRF, approval, Secret, and account APIs.
- `frontend/src/lib/auth-context-store.ts`, `auth-context.tsx`, and `chat-store.ts` — account status and run-aware reset.
- `frontend/src/components/layout/AppLayout.tsx` — settings entry and pending-purge state.
- `frontend/src/components/approval/ApprovalToolUI.tsx` — persisted approval status/retry handling.
- `frontend/src/components/session/SessionProvider.tsx` — strict server-owned session scope.
- `frontend/src/index.css` — settings and destructive-action states.

### Delete after replacement tests pass

- `src/multiclaw/auth/store.py`
- `src/multiclaw/session/sqlite.py`
- `src/multiclaw/memory/sqlite.py`
- `src/multiclaw/storage/repository.py`
- `src/multiclaw/storage/sqlite.py`
- `src/multiclaw/sqlite_utils.py`
- legacy tests whose behavior is fully represented by scoped repository contract tests; preserve unique user-facing assertions by moving them before deletion.

## Planned public contracts

Keep these names and signatures stable across tasks:

```python
@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    workspace_id: str
    session_id: str | None = None
    run_id: str | None = None
    request_started_at_ms: int = 0

    def for_session(self, session_id: str) -> "TenantContext": ...
    def for_run(self, session_id: str, run_id: str) -> "TenantContext": ...


class StorageDialect(Protocol):
    name: Literal["sqlite", "mysql"]
    def db_now_ms(self) -> ColumnElement[int]: ...
    async def begin_write(self, connection: AsyncConnection) -> AsyncTransaction: ...
    async def lock_run(self, connection: AsyncConnection, context: TenantContext) -> None: ...


class AuthUnitOfWork:
    conn: AsyncConnection
    tx: AsyncTransaction
    users: AuthRepository
    verification_codes: AuthRepository
    async def __aenter__(self) -> "AuthUnitOfWork": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...


class TenantUnitOfWork:
    context: TenantContext
    conn: AsyncConnection
    tx: AsyncTransaction
    users: TenantUserRepository
    workspaces: WorkspaceRepository
    sessions: SessionRepository
    memory: MemoryRepository
    runs: WorkflowRepository
    approvals: WorkflowRepository
    executions: WorkflowRepository
    checkpoints: WorkflowRepository
    secrets: SecretRepository
    audit: WorkflowRepository
    deletions: DeletionRepository
    async def __aenter__(self) -> "TenantUnitOfWork": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...


class DeletionUnitOfWork:
    tenant_id: str
    conn: AsyncConnection
    tx: AsyncTransaction
    users: DeletionUserRepository
    deletions: DeletionRepository
    verification_codes: DeletionVerificationRepository
    async def __aenter__(self) -> "DeletionUnitOfWork": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...


@dataclass(slots=True)
class TenantRuntime:
    tenant_id: str
    runtime_instance_id: str
    workspace_root: Path
    agent: MultiClawAgent
    event_bus: EventBus
    event_router: EventRouter
    scheduler: CoreToolScheduler
    registry: ToolRegistry
    skill_manager: SkillManager
    mcp_manager: MCPClientManager | None
    active_run_count: int = 0
    active_executing_run_count: int = 0
    persisted_awaiting_user_run_count: int = 0
    last_used_at_ms: int = 0
    async def close(self) -> None: ...


class RuntimePool:
    async def acquire(self, context: TenantContext) -> TenantRuntime: ...
    async def release(self, tenant_id: str) -> None: ...
    async def evict_idle(self, now_ms: int) -> int: ...
    async def revoke(self, tenant_id: str) -> None: ...
    async def close(self) -> None: ...


class EventScope(BaseModel):
    tenant_id: str
    workspace_id: str
    session_id: str
    run_id: str


class ScopedEvent(EventScope):
    event_type: str
    occurred_at_ms: int
    data: dict[str, Any] = Field(default_factory=dict)


class WorkflowCoordinator:
    async def start_run(self, context: TenantContext, runtime_instance_id: str) -> RunLease: ...
    async def heartbeat(self, lease: RunLease) -> RunLease: ...
    async def checkpoint(self, lease: RunLease, phase: CheckpointPhase, payload: dict[str, Any]) -> None: ...
    async def request_approval(self, lease: RunLease, command: ApprovalCommand) -> ApprovalRecord: ...
    async def decide_approval(self, context: TenantContext, approval_id: str, approved: bool, version: int) -> ApprovalRecord: ...
    async def begin_execution(self, lease: RunLease, command: ExecutionCommand) -> ExecutionRecord: ...
    async def finish_execution(self, lease: RunLease, execution_id: str, result: ExecutionResult) -> ExecutionRecord: ...
    async def finish_run(self, lease: RunLease, status: RunStatus) -> None: ...


class SecretResolver:
    async def resolve(self, context: TenantContext, provider: str, name: str) -> ResolvedSecret: ...


class DeletionService:
    async def request(self, context: TenantContext) -> DeletionStatus: ...
    async def get_status(self, tenant_id: str, recovery_scope: bool = False) -> DeletionStatus: ...
    async def recover(self, tenant_id: str, deletion_job_id: str) -> None: ...
```

## Specification coverage

| Approved requirement | Implementation task | Primary proof |
| --- | --- | --- |
| SQLite/MySQL deployment selection, Core/Alembic, DB clock | Tasks 1-3 | config, dialect, migration, schema contract matrix |
| `user = tenant`, default workspace, scope-bound UoW | Tasks 4-6 | UoW handle/rollback and cross-scope FK tests |
| canonical per-tenant workspace | Task 7 | path traversal/symlink and tenant separation tests |
| per-user Agent/EventBus/Scheduler/Skill/Tool/MCP | Tasks 8-9 | runtime identity, eviction, MCP/Skill isolation tests |
| exact tenant/workspace/session/run SSE filtering | Task 9 | two-user concurrent SSE tests |
| run lease, fencing, CAS, structured checkpoints | Tasks 10-11 | stale-writer and fault-recovery tests |
| serial tools and persisted approvals/executions | Task 12 | one-active-execution and restart approval tests |
| AESGCM deployment-keyring and strict fallback | Task 13 | fixed vector, swapped-row, fallback-zero-call tests |
| digest verification, JWT `auth_epoch`, CSRF | Task 14 | revoked-cookie, purpose, Origin/CSRF tests |
| delayed deletion, recovery, ordered idempotent purge | Task 15 | cancellation race and both-backend purge tests |
| scoped API, readiness, redaction, observability | Task 16 | API 404, schema-head, canary redaction tests |
| Secret/approval/deletion frontend | Task 17 | lint/build plus manual browser checklist |
| dual-backend E2E/fault/release gate | Task 18 | CI matrix, complete backend suite, frontend build |

---


### Task 1: Lock typed standalone configuration and dependencies

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/multiclaw/config/settings.py`
- Modify: `multiclaw.toml`
- Modify: `config/multiclaw.toml`
- Modify: `tests/conftest.py`
- Create: `tests/test_database_config.py`

- [ ] **Step 1: Write failing configuration and example-safety tests**

Create `tests/test_database_config.py` with exact assertions for the approved defaults and bounds:

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from multiclaw.config import Settings


def test_standalone_database_and_runtime_defaults():
    settings = Settings(_config_file="/nonexistent")

    assert settings.deployment.profile == "standalone"
    assert settings.database.driver == "sqlite"
    assert settings.database.url == "sqlite+aiosqlite:///data/multiclaw.db"
    assert settings.database.migration_mode == "validate"
    assert settings.database.sqlite_busy_timeout_ms == 5000
    assert settings.runtime.max_resident_tenants == 32
    assert settings.runtime.idle_ttl_seconds == 900
    assert settings.runtime.max_concurrent_runs_per_tenant == 2
    assert settings.workflow.heartbeat_ms == 5000
    assert settings.workflow.lease_ttl_ms == 20000
    assert settings.workflow.max_checkpoint_payload_bytes == 262144
    assert settings.deletion.retention_days == 7


@pytest.mark.parametrize("value", [-1, 31, "seven", 1.5, True])
def test_deletion_retention_rejects_out_of_contract_values(value):
    with pytest.raises(ValidationError):
        Settings(_config_file="/nonexistent", deletion={"retention_days": value})


@pytest.mark.parametrize(
    ("driver", "url"),
    [
        ("sqlite", "mysql+aiomysql://db/app"),
        ("mysql", "sqlite+aiosqlite:///app.db"),
    ],
)
def test_database_driver_must_match_url(driver, url):
    with pytest.raises(ValidationError, match="database.driver.*database.url"):
        Settings(
            _config_file="/nonexistent",
            database={"driver": driver, "url": url},
        )


def test_repository_example_configs_contain_no_credential_literals():
    forbidden = ("sk-", "xkeysib-", "re_")
    for path in (Path("multiclaw.toml"), Path("config/multiclaw.toml")):
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), path
```

Update the autouse fixture in `tests/conftest.py` to clear `MULTICLAW_TEST_MYSQL_URL`, `MULTICLAW_SECRETS_KEYRING_B64`, and `MULTICLAW_AUTH_JWT_SIGNING_KEY` in addition to all existing `MULTICLAW_` variables.

- [ ] **Step 2: Run the new tests and observe the expected failure**

Run:

```bash
uv run pytest tests/test_database_config.py -q
```

Expected: FAIL because the new settings groups do not exist, `database.path` is still the active contract, and the example configuration contains credential-shaped literals.

- [ ] **Step 3: Add the approved dependencies and CLI entry point**

Run:

```bash
uv add "SQLAlchemy>=2.0,<3" "Alembic>=1.13,<2" "aiomysql>=0.2,<1" "cryptography>=43,<47"
```

Add the following entry to `pyproject.toml`:

```toml
[project.scripts]
multiclaw = "multiclaw.cli:main"
```

Do not add a CLI framework; Task 3 uses the standard-library `argparse` module.

- [ ] **Step 4: Replace path-only settings with the approved typed contract**

Add these models to `src/multiclaw/config/settings.py` and register each on `Settings` plus `_build_toml_kwargs`:

```python
class DeploymentSettings(BaseModel):
    profile: Literal["standalone"] = "standalone"


class DatabaseSettings(BaseModel):
    driver: Literal["sqlite", "mysql"] = "sqlite"
    url: str = "sqlite+aiosqlite:///data/multiclaw.db"
    migration_mode: Literal["validate"] = "validate"
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=1, le=60000)

    @model_validator(mode="after")
    def validate_driver_url(self) -> "DatabaseSettings":
        expected = {
            "sqlite": "sqlite+aiosqlite://",
            "mysql": "mysql+aiomysql://",
        }[self.driver]
        if not self.url.startswith(expected):
            raise ValueError("database.driver must match database.url")
        return self


class WorkspaceSettings(BaseModel):
    root: str = "data/workspaces"


class RuntimeSettings(BaseModel):
    max_resident_tenants: int = Field(default=32, ge=1, le=1024)
    idle_ttl_seconds: int = Field(default=900, ge=30)
    max_concurrent_runs_per_tenant: int = Field(default=2, ge=1, le=32)


class WorkflowSettings(BaseModel):
    heartbeat_ms: int = Field(default=5000, ge=1000)
    lease_ttl_ms: int = Field(default=20000, ge=5000)
    max_checkpoint_payload_bytes: int = Field(default=262144, ge=1024, le=1048576)

    @model_validator(mode="after")
    def validate_lease_ratio(self) -> "WorkflowSettings":
        if self.lease_ttl_ms < self.heartbeat_ms * 3:
            raise ValueError("workflow.lease_ttl_ms must be at least 3x heartbeat_ms")
        return self


class SecretSettings(BaseModel):
    allow_platform_fallback: bool = False
    keyring_file: str = ""


class DeletionSettings(BaseModel):
    retention_days: int = Field(default=7, ge=0, le=30, strict=True)


class AuthSettings(BaseModel):
    jwt_signing_key_file: str = ""
```

The two environment-only Secret inputs are read explicitly by their loaders as `MULTICLAW_SECRETS_KEYRING_B64` and `MULTICLAW_AUTH_JWT_SIGNING_KEY`; they are intentionally not TOML model fields and must never appear in `model_dump()` output.

Replace all example credential values with empty strings or comments naming the corresponding `MULTICLAW_*` environment variable. Replace `[database] path` with the SQLite URL and add the approved deployment/workspace/runtime/workflow/secrets/deletion blocks. This is configuration sanitation, not credential rotation; operators must rotate any value that was previously exposed.

- [ ] **Step 5: Run focused configuration verification**

Run:

```bash
uv run pytest tests/test_config.py tests/test_database_config.py -q
uv lock --check
```

Expected: both test files PASS and `uv.lock` reports no changes needed.

- [ ] **Step 6: Commit the configuration boundary**

```bash
git add pyproject.toml uv.lock src/multiclaw/config/settings.py \
  multiclaw.toml config/multiclaw.toml tests/conftest.py tests/test_database_config.py
git commit -m "Make deployment storage choices explicit before persistence changes" \
  -m "Replace the path-only SQLite contract with validated standalone SQLite/MySQL settings and remove credential literals from repository configuration examples." \
  -m "Constraint: One deployment selects exactly one async backend" \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/test_config.py tests/test_database_config.py -q; uv lock --check"
```

### Task 2: Establish async engines, transaction semantics, and DB clock

**Files:**
- Create: `src/multiclaw/storage/engine.py`
- Create: `src/multiclaw/storage/dialect.py`
- Modify: `src/multiclaw/storage/__init__.py`
- Create: `tests/database_fixtures.py`
- Create: `tests/test_storage_engine.py`
- Modify: `tests/test_sqlite_pragmas.py`
- Create: `tests/integration/test_mysql_contract.py`

- [ ] **Step 1: Write failing engine and dialect contract tests**

Create a backend fixture that always provides file-backed SQLite and provides MySQL only when `MULTICLAW_TEST_MYSQL_URL` is set:

```python
@pytest.fixture(params=("sqlite", "mysql"))
async def database(request, tmp_path, monkeypatch):
    if request.param == "mysql":
        url = os.getenv("MULTICLAW_TEST_MYSQL_URL")
        if not url:
            pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'contract.db'}"
    db = Database.create(
        DatabaseSettings(driver=request.param, url=url),
    )
    try:
        yield db
    finally:
        await db.dispose()
```

Add tests that prove the same interface on both backends:

```python
@pytest.mark.asyncio
async def test_db_now_ms_is_epoch_milliseconds_and_monotonic(database):
    async with database.connect() as conn:
        first = await conn.scalar(select(database.dialect.db_now_ms()))
        second = await conn.scalar(select(database.dialect.db_now_ms()))
    assert isinstance(first, int)
    assert first <= second
    assert abs(first - int(time.time() * 1000)) < 10_000


@pytest.mark.asyncio
async def test_uow_rollback_is_atomic(database):
    async with database.write_transaction() as conn:
        await conn.execute(text("CREATE TABLE atomic_probe (id INTEGER PRIMARY KEY)"))
    with pytest.raises(RuntimeError):
        async with database.write_transaction() as conn:
            await conn.execute(text("INSERT INTO atomic_probe (id) VALUES (1)"))
            raise RuntimeError("rollback")
    async with database.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM atomic_probe")) == 0
```

Extend `tests/test_sqlite_pragmas.py` to assert `foreign_keys=1`, configured `busy_timeout`, and file-backed WAL on the SQLAlchemy connection. Add MySQL-only assertions for server version `>=8.0.36`, InnoDB, session `READ-COMMITTED`, and `@@session.time_zone='+00:00'`.

- [ ] **Step 2: Run the engine contracts and observe import failure**

```bash
uv run pytest tests/test_storage_engine.py tests/test_sqlite_pragmas.py \
  tests/integration/test_mysql_contract.py -q
```

Expected: FAIL because `Database`, `SQLiteDialect`, and `MySQLDialect` do not exist.

- [ ] **Step 3: Implement backend-specific SQL in one dialect module**

Create `src/multiclaw/storage/dialect.py` with these expressions and lock methods:

```python
class SQLiteDialect:
    name: Literal["sqlite"] = "sqlite"

    def db_now_ms(self) -> ColumnElement[int]:
        return cast(
            func.floor((func.julianday("now") - 2440587.5) * 86400000),
            BigInteger,
        )

    async def begin_write(self, connection: AsyncConnection) -> AsyncTransaction:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        return connection.get_transaction()  # type: ignore[return-value]

    async def lock_run(self, connection: AsyncConnection, context: TenantContext) -> None:
        # BEGIN IMMEDIATE serializes writers; the fencing predicate protects stale owners.
        return None


class MySQLDialect:
    name: Literal["mysql"] = "mysql"

    def db_now_ms(self) -> ColumnElement[int]:
        return cast(func.floor(func.unix_timestamp(func.current_timestamp(6)) * 1000), BigInteger)

    async def begin_write(self, connection: AsyncConnection) -> AsyncTransaction:
        return await connection.begin()

    async def lock_run(self, connection: AsyncConnection, context: TenantContext) -> None:
        await connection.execute(
            select(agent_runs.c.run_id)
            .where(
                agent_runs.c.tenant_id == context.tenant_id,
                agent_runs.c.workspace_id == context.workspace_id,
                agent_runs.c.session_id == context.session_id,
                agent_runs.c.run_id == context.run_id,
            )
            .with_for_update()
        )
```

Use the exact SQLite epoch expression from the approved design. Do not use application time inside lease, CAS, or purge SQL.

- [ ] **Step 4: Implement engine lifecycle and connection initialization**

Create `Database` with a single async engine and these methods:

```python
@dataclass(slots=True)
class Database:
    engine: AsyncEngine
    dialect: SQLiteDialect | MySQLDialect

    @classmethod
    def create(cls, settings: DatabaseSettings) -> "Database":
        engine = create_async_engine(
            settings.url,
            pool_pre_ping=True,
            isolation_level="READ COMMITTED" if settings.driver == "mysql" else None,
        )
        if settings.driver == "sqlite":
            install_sqlite_listeners(engine.sync_engine, settings.sqlite_busy_timeout_ms)
            dialect: SQLiteDialect | MySQLDialect = SQLiteDialect()
        else:
            install_mysql_listeners(engine.sync_engine)
            dialect = MySQLDialect()
        return cls(engine=engine, dialect=dialect)

    def connect(self) -> AsyncContextManager[AsyncConnection]:
        return self.engine.connect()

    @asynccontextmanager
    async def write_transaction(self):
        async with self.engine.connect() as conn:
            tx = await self.dialect.begin_write(conn)
            try:
                yield conn
            except BaseException:
                await tx.rollback()
                raise
            else:
                await tx.commit()

    async def dispose(self) -> None:
        await self.engine.dispose()
```

SQLite connect listeners must run `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=<configured>`, and `PRAGMA journal_mode=WAL`. MySQL connect listeners must run `SET time_zone='+00:00'` and `SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED`. Never add raw SQL conditionals outside this module.

- [ ] **Step 5: Run both-backend engine contracts**

```bash
uv run pytest tests/test_storage_engine.py tests/test_sqlite_pragmas.py -q
MULTICLAW_TEST_MYSQL_URL="$MULTICLAW_TEST_MYSQL_URL" \
  uv run pytest tests/integration/test_mysql_contract.py -q
```

Expected: SQLite tests PASS. In the MySQL-enabled environment, all MySQL engine/version/session tests PASS; a local environment without the variable reports only the documented skips.

- [ ] **Step 6: Commit the storage execution boundary**

```bash
git add src/multiclaw/storage tests/database_fixtures.py tests/test_storage_engine.py \
  tests/test_sqlite_pragmas.py tests/integration/test_mysql_contract.py
git commit -m "Make transaction and clock semantics identical across supported databases" \
  -m "Centralize async engine setup, DB clock expressions, SQLite writer locking, and MySQL row-lock prerequisites before repositories are introduced." \
  -m "Constraint: Lease and purge decisions use database time only" \
  -m "Rejected: Per-repository connection setup | would permit pragma and isolation drift" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: storage engine, SQLite pragma, and MySQL contract suites"
```

### Task 3: Create the immutable Alembic baseline and schema gate

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/20260815_0001_multi_tenant_baseline.py`
- Create: `src/multiclaw/storage/schema.py`
- Create: `src/multiclaw/cli.py`
- Create: `tests/test_migrations.py`
- Create: `tests/test_schema_contract.py`
- Modify: `tests/integration/test_mysql_contract.py`

- [ ] **Step 1: Write failing schema and migration contract tests**

Assert the exact baseline table set and required ownership constraints:

```python
EXPECTED_TABLES = {
    "users",
    "workspaces",
    "chat_sessions",
    "memory_entries",
    "agent_runs",
    "approval_requests",
    "tool_executions",
    "execution_checkpoints",
    "user_secrets",
    "audit_logs",
    "deletion_jobs",
    "verification_codes",
}


@pytest.mark.asyncio
async def test_empty_database_upgrades_to_exact_baseline(database_url):
    await run_alembic_upgrade(database_url, "head")
    async with inspect_async(database_url) as inspector:
        assert set(await inspector.get_table_names()) >= EXPECTED_TABLES
    assert await current_revision(database_url) == "20260815_0001"


@pytest.mark.asyncio
async def test_scope_constraints_reject_cross_workspace_execution(migrated_database):
    seeded = await seed_two_scopes(migrated_database)
    with pytest.raises(IntegrityError):
        async with migrated_database.write_transaction() as conn:
            await conn.execute(
                tool_executions.insert().values(
                    execution_id="exec-cross",
                    tenant_id=seeded.tenant_id,
                    workspace_id=seeded.other_workspace_id,
                    session_id=seeded.session_id,
                    run_id=seeded.run_id,
                    tool_call_id="call-1",
                    tool_name="read_file",
                    tool_kind="native",
                    execution_status="not_started",
                    recovery_strategy="read_only_replay",
                    input_payload_json={},
                    input_hash="0" * 64,
                    schema_version=1,
                    version=1,
                    created_at=1,
                    updated_at=1,
                )
            )
```

Also assert all ID columns use bounded strings, all persisted times are `BIGINT`, tenant-owned foreign keys are `RESTRICT/NO ACTION`, SQLite `PRAGMA foreign_key_check` is empty, MySQL tables use InnoDB, and `users.default_workspace_id` has the composite `(id, default_workspace_id) -> workspaces(tenant_id, workspace_id)` relationship.

- [ ] **Step 2: Run migration tests and observe missing Alembic configuration**

```bash
uv run pytest tests/test_migrations.py tests/test_schema_contract.py -q
```

Expected: FAIL because there is no Alembic environment or Core metadata.

- [ ] **Step 3: Define one Core metadata model for repository queries and autogeneration**

Create `src/multiclaw/storage/schema.py`. Every table listed in `EXPECTED_TABLES` must be a module-level `Table`. Use naming conventions so SQLite and MySQL produce stable constraint names:

```python
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING_CONVENTION)

users = Table(
    "users",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("default_workspace_id", String(36)),
    Column("auth_epoch", BigInteger, nullable=False, server_default="0"),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("purge_requested_at", BigInteger),
    Column("purge_after", BigInteger),
    UniqueConstraint("id", "default_workspace_id", name="users_default_workspace_scope"),
    ForeignKeyConstraint(
        ("id", "default_workspace_id"),
        ("workspaces.tenant_id", "workspaces.id"),
        ondelete="RESTRICT",
        onupdate="RESTRICT",
        name="users_default_workspace_fk",
    ),
    CheckConstraint("status IN ('active','disabled','pending_purge')", name="users_status"),
)

agent_runs = Table(
    "agent_runs",
    metadata,
    Column("run_id", String(36), primary_key=True),
    Column("tenant_id", String(36), nullable=False),
    Column("workspace_id", String(36), nullable=False),
    Column("session_id", String(36), nullable=False),
    Column("run_status", String(32), nullable=False),
    Column("runtime_instance_id", String(128)),
    Column("lease_owner", String(128)),
    Column("fencing_token", BigInteger, nullable=False, server_default="0"),
    Column("lease_expires_at", BigInteger),
    Column("heartbeat_at", BigInteger),
    Column("schema_version", Integer, nullable=False, server_default="1"),
    Column("version", BigInteger, nullable=False, server_default="1"),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("finished_at", BigInteger),
    UniqueConstraint("tenant_id", "workspace_id", "session_id", "run_id"),
    ForeignKeyConstraint(
        ("tenant_id", "workspace_id", "session_id"),
        ("chat_sessions.tenant_id", "chat_sessions.workspace_id", "chat_sessions.id"),
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    ),
)
```

Define the remaining columns, enums-as-checks, unique constraints, full composite foreign keys, optional approval FK, Secret nonce uniqueness, checkpoint payload/hash fields, deletion job uniqueness, and verification-code purpose/digest fields exactly as Sections 9-10 of the approved design. Add the cyclic users/workspaces composite FK in the migration after both tables exist.

- [ ] **Step 4: Configure async, forward-only Alembic and generate the frozen baseline**

`alembic/env.py` must load `Settings`, replace the URL from `-x database_url=...` when supplied by tests/CLI, create an async engine through the same connection hooks, set `target_metadata=metadata`, and enable `compare_type=True` plus `render_as_batch=True` for SQLite.

Generate and inspect the revision:

```bash
uv run alembic -x database_url="sqlite+aiosqlite:///./data/baseline-check.db" \
  revision --autogenerate -m "multi tenant baseline" \
  --rev-id 20260815_0001
uv run alembic -x database_url="sqlite+aiosqlite:///./data/baseline-check.db" upgrade head
```

The checked-in revision must contain explicit `op.create_table`, `op.create_index`, and `op.create_foreign_key` operations; it must not import mutable application metadata. Implement `downgrade()` as:

```python
def downgrade() -> None:
    raise RuntimeError("MultiClaw migrations are forward-only")
```

- [ ] **Step 5: Add explicit database CLI commands**

Create `src/multiclaw/cli.py` with this command shape:

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="multiclaw")
    commands = parser.add_subparsers(dest="command", required=True)
    db = commands.add_parser("db").add_subparsers(dest="db_command", required=True)
    db.add_parser("upgrade")
    db.add_parser("current")
    db.add_parser("check")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    if args.db_command == "upgrade":
        alembic_command.upgrade(alembic_config(settings.database.url), "head")
        return 0
    if args.db_command == "current":
        alembic_command.current(alembic_config(settings.database.url))
        return 0
    return asyncio.run(check_revision_is_head(settings.database.url))
```

The API lifecycle must never call the `upgrade` branch; readiness only calls `check_revision_is_head`.

- [ ] **Step 6: Run empty-to-head and introspection verification on both backends**

```bash
uv run pytest tests/test_migrations.py tests/test_schema_contract.py -q
MULTICLAW_TEST_MYSQL_URL="$MULTICLAW_TEST_MYSQL_URL" \
  uv run pytest tests/integration/test_mysql_contract.py -q
uv run multiclaw db check
```

Expected: empty SQLite and MySQL databases upgrade to revision `20260815_0001`, schema/FK introspection passes, and `db check` exits `0` only at head.

- [ ] **Step 7: Commit the baseline**

```bash
git add alembic.ini alembic src/multiclaw/cli.py src/multiclaw/storage/schema.py \
  tests/test_migrations.py tests/test_schema_contract.py tests/integration/test_mysql_contract.py
git commit -m "Give every tenant-owned row one enforceable database scope" \
  -m "Create the immutable Core/Alembic baseline, complete composite foreign keys, and explicit migration CLI for both supported databases." \
  -m "Constraint: Development data is reset; no legacy migration or dual-read path" \
  -m "Rejected: API startup migrations | readiness must fail closed instead" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Directive: Never edit revision 20260815_0001 after another revision exists" \
  -m "Tested: migration, schema contract, MySQL contract, and db revision checks"
```

### Task 4: Bind authenticated users to one scoped UoW and default workspace

**Files:**
- Create: `src/multiclaw/tenancy/__init__.py`
- Create: `src/multiclaw/tenancy/context.py`
- Create: `src/multiclaw/storage/uow.py`
- Create: `src/multiclaw/storage/repositories/__init__.py`
- Create: `src/multiclaw/storage/repositories/auth.py`
- Modify: `src/multiclaw/auth/models.py`
- Create: `tests/test_tenant_context.py`
- Create: `tests/test_tenant_uow.py`

- [ ] **Step 1: Write failing immutable-context and UoW ownership tests**

```python
def test_tenant_context_derives_narrower_scope_without_mutation():
    root = TenantContext("tenant-a", "workspace-a", request_started_at_ms=123)
    session = root.for_session("session-a")
    run = session.for_run("session-a", "run-a")

    assert root.session_id is None and root.run_id is None
    assert session.session_id == "session-a" and session.run_id is None
    assert run.session_id == "session-a" and run.run_id == "run-a"
    with pytest.raises(FrozenInstanceError):
        run.tenant_id = "tenant-b"


@pytest.mark.asyncio
async def test_tenant_uow_repositories_share_one_connection_and_transaction(database):
    context = await seed_tenant_context(database)
    async with TenantUnitOfWork(database, context) as uow:
        handles = {
            id(repo.connection)
            for repo in (
                uow.users,
                uow.workspaces,
            )
        }
        assert handles == {id(uow.conn)}
        assert uow.tx.is_active


@pytest.mark.asyncio
async def test_cross_repository_failure_rolls_back_default_workspace_bootstrap(database):
    with pytest.raises(BootstrapProbeError):
        async with AuthUnitOfWork(database) as uow:
            await uow.users.create_user_with_default_workspace(
                email="rollback@example.com",
                fail_after_workspace=True,
            )
    async with AuthUnitOfWork(database) as uow:
        assert await uow.users.find_by_email("rollback@example.com") is None
```

Add an access-surface test proving `AuthUnitOfWork` has only `users` and `verification_codes` repositories and has no `sessions`, `memory`, `secrets`, or workflow properties.

- [ ] **Step 2: Run the focused tests and observe missing contracts**

```bash
uv run pytest tests/test_tenant_context.py tests/test_tenant_uow.py -q
```

Expected: FAIL on imports for `TenantContext`, `AuthUnitOfWork`, and `TenantUnitOfWork`.

- [ ] **Step 3: Implement immutable scope derivation**

Create `src/multiclaw/tenancy/context.py`:

```python
@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    workspace_id: str
    session_id: str | None = None
    run_id: str | None = None
    request_started_at_ms: int = 0

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.workspace_id:
            raise ValueError("tenant_id and workspace_id are required")
        if self.run_id is not None and self.session_id is None:
            raise ValueError("run scope requires session scope")

    def for_session(self, session_id: str) -> "TenantContext":
        if not session_id:
            raise ValueError("session_id is required")
        return replace(self, session_id=session_id, run_id=None)

    def for_run(self, session_id: str, run_id: str) -> "TenantContext":
        if not session_id or not run_id:
            raise ValueError("session_id and run_id are required")
        return replace(self, session_id=session_id, run_id=run_id)
```

Do not add a method that changes `tenant_id` or `workspace_id`.

- [ ] **Step 4: Implement restricted Auth UoW and scope-bound Tenant UoW**

Both UoWs must acquire one connection, begin through `database.dialect.begin_write`, construct repositories with that exact connection, and commit/rollback only in `__aexit__`:

```python
class TenantUnitOfWork:
    def __init__(self, database: Database, context: TenantContext) -> None:
        self.database = database
        self.context = context

    async def __aenter__(self) -> "TenantUnitOfWork":
        self.conn = await self.database.engine.connect()
        self.tx = await self.database.dialect.begin_write(self.conn)
        self.users = TenantUserRepository(self.conn, self.context)
        self.workspaces = WorkspaceRepository(self.conn, self.context)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                await self.tx.commit()
            else:
                await self.tx.rollback()
        finally:
            await self.conn.close()
```

At this task boundary `TenantUnitOfWork` exposes only tenant users/workspaces. Task 5 adds sessions/memory, Task 10 adds workflow/audit, Task 13 adds Secrets, and Task 15 adds deletion repositories; every extension must reuse the existing `self.conn` and `self.tx`. `AuthUnitOfWork` follows the same lifecycle but creates only `AuthRepository` aliases for users and verification codes. Task 15 adds a separate recovery-token-authorized `DeletionUnitOfWork`, scoped by tenant ID and limited to users, deletion jobs, verification-code cleanup, and ordered purge; it does not expose sessions, memory, Secrets, or workflow execution. Repository constructors store the UoW connection privately; no repository exposes `commit`, `rollback`, engine creation, or connection replacement.

- [ ] **Step 5: Implement atomic user/default-workspace bootstrap**

`AuthRepository.create_user_with_default_workspace` must use DB clock expressions and the exact sequence:

```python
async def create_user_with_default_workspace(self, email: str) -> UserRecord:
    tenant_id = str(uuid4())
    workspace_id = str(uuid4())
    now = self._dialect.db_now_ms()
    await self._conn.execute(
        users.insert().values(
            id=tenant_id,
            email=email,
            status="active",
            default_workspace_id=None,
            auth_epoch=0,
            created_at=now,
            updated_at=now,
        )
    )
    await self._conn.execute(
        workspaces.insert().values(
            id=workspace_id,
            tenant_id=tenant_id,
            slug="default",
            name="Default",
            created_at=now,
            updated_at=now,
        )
    )
    await self._conn.execute(
        users.update()
        .where(users.c.id == tenant_id)
        .values(default_workspace_id=workspace_id, updated_at=now)
    )
    row = await self._conn.execute(
        select(users).where(
            users.c.id == tenant_id,
            users.c.default_workspace_id == workspace_id,
        )
    )
    return UserRecord.from_row(row.one())
```

Use backend-neutral insert handling for the login race: catch the unique-email `IntegrityError`, roll back to a nested savepoint, then select the existing user. Never use SQLite-only `INSERT OR IGNORE`.

- [ ] **Step 6: Run the UoW and bootstrap contract on SQLite/MySQL**

```bash
uv run pytest tests/test_tenant_context.py tests/test_tenant_uow.py -q
MULTICLAW_TEST_MYSQL_URL="$MULTICLAW_TEST_MYSQL_URL" \
  uv run pytest tests/test_tenant_uow.py -q
```

Expected: immutable scope, one-handle, rollback, concurrent bootstrap, and active/default-workspace assertions PASS on both backends.

- [ ] **Step 7: Commit the tenant transaction boundary**

```bash
git add src/multiclaw/tenancy src/multiclaw/storage/uow.py \
  src/multiclaw/storage/repositories src/multiclaw/auth/models.py \
  tests/test_tenant_context.py tests/test_tenant_uow.py
git commit -m "Prevent tenant work from escaping one authenticated transaction" \
  -m "Introduce immutable tenant context, restricted unauthenticated UoW access, and atomic default-workspace bootstrap on the shared deployment database." \
  -m "Constraint: Unauthenticated email flows cannot construct TenantContext" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Directive: Repositories must not open or commit their own connections" \
  -m "Tested: tenant context and UoW contract suites on SQLite and MySQL"
```

### Task 5: Replace session and memory stores with fully scoped repositories

**Files:**
- Create: `src/multiclaw/storage/repositories/sessions.py`
- Create: `src/multiclaw/storage/repositories/memory.py`
- Modify: `src/multiclaw/storage/uow.py`
- Modify: `src/multiclaw/session/models.py`
- Modify: `src/multiclaw/session/__init__.py`
- Modify: `src/multiclaw/memory/models.py`
- Modify: `src/multiclaw/memory/protocol.py`
- Modify: `src/multiclaw/memory/__init__.py`
- Modify: `src/multiclaw/agent/base.py`
- Modify: `src/multiclaw/agent/context.py`
- Modify: `src/multiclaw/agent/multiclaw.py`
- Create: `tests/test_scoped_repositories.py`
- Modify: `tests/test_session.py`
- Modify: `tests/test_session_delete_and_messages.py`
- Modify: `tests/test_memory.py`
- Modify: `tests/test_context.py`
- Delete: `src/multiclaw/session/sqlite.py`
- Delete: `src/multiclaw/memory/sqlite.py`

- [ ] **Step 1: Write failing cross-tenant and cross-workspace repository tests**

```python
@pytest.mark.asyncio
async def test_session_repository_never_returns_other_scope(database, seeded_scopes):
    async with TenantUnitOfWork(database, seeded_scopes.tenant_a) as uow:
        own = await uow.sessions.create("Own")
    async with TenantUnitOfWork(database, seeded_scopes.tenant_b) as uow:
        assert await uow.sessions.get(own.id) is None
        assert await uow.sessions.list(include_archived=True) == []


@pytest.mark.asyncio
async def test_memory_requires_session_inside_uow_scope(database, seeded_scopes):
    foreign = seeded_scopes.tenant_b.for_session(seeded_scopes.session_b)
    with pytest.raises(IntegrityError):
        async with TenantUnitOfWork(database, seeded_scopes.tenant_a) as uow:
            await uow.memory.save(
                MemoryEntry(
                    content="must not cross",
                    type="chat_message",
                    session_id=foreign.session_id,
                    role="user",
                    turn_index=1,
                )
            )


@pytest.mark.asyncio
async def test_repository_update_predicate_contains_full_scope(database, seeded_scopes):
    async with TenantUnitOfWork(database, seeded_scopes.tenant_a) as uow:
        count = await uow.sessions.rename(seeded_scopes.session_b, "stolen")
        assert count == 0
```

Add contract cases for archive/restore/delete, message ordering/limit, memory search/recent/context, and transaction rollback. Remove tests that depend on empty `tenant_id`, empty `user_id`, handwritten table creation, or `:memory:` persistence behavior.

- [ ] **Step 2: Run repository tests and observe the old unscoped behavior**

```bash
uv run pytest tests/test_scoped_repositories.py tests/test_session.py \
  tests/test_session_delete_and_messages.py tests/test_memory.py tests/test_context.py -q
```

Expected: FAIL because the new repositories and scope-required model fields do not exist.

- [ ] **Step 3: Implement repository predicates that always derive scope from context**

Every session query must start from the same predicate builder:

```python
class SessionRepository:
    def __init__(self, connection: AsyncConnection, context: TenantContext) -> None:
        self._connection = connection
        self._context = context

    def _scope(self) -> tuple[ColumnElement[bool], ...]:
        return (
            chat_sessions.c.tenant_id == self._context.tenant_id,
            chat_sessions.c.workspace_id == self._context.workspace_id,
        )

    async def get(self, session_id: str) -> ChatSession | None:
        row = await self._connection.execute(
            select(chat_sessions).where(*self._scope(), chat_sessions.c.id == session_id)
        )
        value = row.mappings().one_or_none()
        return ChatSession.model_validate(value) if value else None

    async def rename(self, session_id: str, title: str) -> int:
        result = await self._connection.execute(
            chat_sessions.update()
            .where(*self._scope(), chat_sessions.c.id == session_id)
            .values(title=validate_title(title), updated_at=self._dialect.db_now_ms())
        )
        return result.rowcount
```

`MemoryRepository` must derive tenant/workspace from context and accept only `session_id`, never caller-supplied tenant/workspace values. `MemoryEntry.session_id` is `str | None`: chat messages require the current non-empty session, while workspace-long-term memory uses `NULL`. Recent chat queries match only the current session; relevant retrieval matches `(session_id = current_session_id OR session_id IS NULL)` and still includes tenant/workspace predicates. In-memory test memory may retain a separate test-only implementation but must not be wired into production.

Extend `TenantUnitOfWork.__aenter__` with `self.sessions = SessionRepository(self.conn, self.context, self.database.dialect)` and `self.memory = MemoryRepository(self.conn, self.context, self.database.dialect)`; extend the one-handle test to include both repositories.

- [ ] **Step 4: Propagate context through agent memory reads and writes**

Change agent entry points to require `TenantContext`:

```python
async def handle_message_stream(
    self,
    message: str,
    *,
    context: TenantContext,
) -> AsyncIterator[dict[str, Any]]:
    ...


async def _save_chat_msg(
    self,
    context: TenantContext,
    role: Literal["user", "assistant"],
    content: str,
    turn_index: int,
) -> None:
    if context.session_id is None:
        raise ValueError("chat persistence requires session scope")
    await self.memory.save(
        context,
        MemoryEntry(
            content=content,
            type="chat_message",
            session_id=context.session_id,
            role=role,
            turn_index=turn_index,
        ),
    )
```

Update `ContextBuilder` to call recent/query with the full context. Remove any default empty tenant or session values from production model constructors.

- [ ] **Step 5: Delete handwritten session/memory SQL after all callers move**

Delete `src/multiclaw/session/sqlite.py` and `src/multiclaw/memory/sqlite.py`. Update package exports so importing either legacy class fails, and add an assertion to `tests/test_scoped_repositories.py` that production packages export only scoped repository-backed protocols/models.

- [ ] **Step 6: Run scoped repository and agent context regressions**

```bash
uv run pytest tests/test_scoped_repositories.py tests/test_session.py \
  tests/test_session_delete_and_messages.py tests/test_memory.py tests/test_context.py \
  tests/test_agent.py -q
```

Expected: all scoped CRUD, message ordering, memory retrieval, and agent context tests PASS without the legacy store modules.

- [ ] **Step 7: Commit the scoped persistence cutover**

```bash
git add src/multiclaw/session src/multiclaw/memory src/multiclaw/agent \
  src/multiclaw/storage/repositories tests/test_scoped_repositories.py \
  tests/test_session.py tests/test_session_delete_and_messages.py tests/test_memory.py \
  tests/test_context.py tests/test_agent.py
git commit -m "Make session and memory ownership impossible to omit" \
  -m "Replace independent SQLite stores with UoW repositories whose predicates and foreign keys always include tenant and workspace scope." \
  -m "Rejected: Optional tenant filters | omission recreates cross-tenant access paths" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: scoped repository, session, memory, context, and agent suites"
```

### Task 6: Resolve TenantContext at the authenticated request boundary

**Files:**
- Modify: `src/multiclaw/auth/middleware.py`
- Create: `src/multiclaw/api/__init__.py`
- Create: `src/multiclaw/api/dependencies.py`
- Modify: `src/multiclaw/server.py`
- Create: `tests/test_auth_tenant_boundary.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing identity-source and strict-session tests**

```python
def test_request_tenant_ignores_spoofed_headers_and_body(client, user_a_cookie):
    response = client.post(
        "/api/sessions",
        cookies=user_a_cookie,
        headers={"X-Tenant-Id": "tenant-b", "X-Workspace-Id": "workspace-b"},
        json={"title": "Owned", "tenant_id": "tenant-b", "workspace_id": "workspace-b"},
    )
    assert response.status_code == 200
    assert response.json()["tenant_id"] == user_a_cookie.tenant_id
    assert response.json()["workspace_id"] == user_a_cookie.workspace_id


def test_foreign_session_id_is_404_and_does_not_create_session(client, two_users):
    before = client.get("/api/sessions", cookies=two_users.a.cookie).json()
    response = client.post(
        "/api/chat",
        cookies=two_users.a.cookie,
        json={"message": "hello", "session_id": two_users.b.session_id},
    )
    after = client.get("/api/sessions", cookies=two_users.a.cookie).json()
    assert response.status_code == 404
    assert after == before
```

Add tests for inactive/pending-purge users, active users with null/invalid default workspace, and no-token requests. The request context must never be built before auth state and current `auth_epoch` are verified.

- [ ] **Step 2: Run auth-boundary tests and observe spoof/silent-create failures**

```bash
uv run pytest tests/test_auth_tenant_boundary.py tests/test_server.py -q
```

Expected: FAIL because middleware restores only JWT claims, and `/api/chat` silently creates a session for a foreign ID.

- [ ] **Step 3: Add database-backed authentication and tenant dependencies**

Create dependencies with explicit order:

```python
async def current_user(request: Request) -> AuthenticatedUser:
    user = getattr(request.state, "authenticated_user", None)
    if user is None:
        raise HTTPException(401, "Unauthorized")
    return user


async def tenant_context(
    request: Request,
    user: AuthenticatedUser = Depends(current_user),
) -> TenantContext:
    if user.status != "active" or user.default_workspace_id is None:
        raise HTTPException(403, "Account unavailable")
    return TenantContext(
        tenant_id=user.id,
        workspace_id=user.default_workspace_id,
        request_started_at_ms=request.state.request_started_at_ms,
    )


async def tenant_uow(
    request: Request,
    context: TenantContext = Depends(tenant_context),
) -> AsyncIterator[TenantUnitOfWork]:
    async with TenantUnitOfWork(request.app.state.database, context) as uow:
        yield uow
```

Middleware may decode signature/expiry, but it must load the current user from `AuthUnitOfWork` and require claim `auth_epoch == users.auth_epoch`. Store the typed user on request state; never trust JWT email, workspace, or status as current database state.

- [ ] **Step 4: Rewire the existing session/chat boundary without adding new workflow behavior**

Replace inline `user["id"]` checks with `TenantContext` and UoW repositories. For supplied `session_id`, use:

```python
session = await uow.sessions.get(requested_session_id)
if session is None:
    raise HTTPException(status_code=404, detail="session not found")
if session.status == SessionStatus.ARCHIVED:
    raise HTTPException(status_code=409, detail="session is archived")
```

Create a new session only when both `req.session_id` and compatibility alias `req.id` are absent. Do not expose an API to create or select another workspace.

- [ ] **Step 5: Run auth/session boundary regressions**

```bash
uv run pytest tests/test_auth_tenant_boundary.py tests/test_server.py \
  tests/test_chat_request_compat.py tests/test_auth_route_aliases.py -q
```

Expected: spoofed scope has no effect, foreign/missing session IDs return indistinguishable `404`, invalid IDs create no rows, and existing request-shape aliases continue to work.

- [ ] **Step 6: Commit the request scope boundary**

```bash
git add src/multiclaw/auth/middleware.py src/multiclaw/api src/multiclaw/server.py \
  tests/test_auth_tenant_boundary.py tests/test_server.py tests/test_chat_request_compat.py \
  tests/test_auth_route_aliases.py
git commit -m "Derive every request scope from current authenticated account state" \
  -m "Resolve tenant and default workspace after database-backed identity validation and reject foreign session identifiers without side effects." \
  -m "Constraint: HTTP clients cannot select tenant or workspace in v1" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: auth tenant boundary, server, and chat compatibility suites"
```

### Task 7: Isolate tenant workspace paths with one canonical resolver

**Files:**
- Create: `src/multiclaw/tenancy/workspace.py`
- Modify: `src/multiclaw/mcp/config.py`
- Modify: `src/multiclaw/mcp/transport/factory.py`
- Modify: `src/multiclaw/governance/sandbox/manager.py`
- Create: `tests/test_workspace_resolver.py`
- Modify: `tests/test_mcp_config.py`
- Modify: `tests/test_sandbox_manager.py`

- [ ] **Step 1: Write failing canonical-path and containment tests**

```python
def test_workspace_resolver_maps_each_scope_to_distinct_directory(tmp_path):
    resolver = WorkspaceResolver(tmp_path)
    a = resolver.resolve(TenantContext("tenant-a", "workspace-a"))
    b = resolver.resolve(TenantContext("tenant-b", "workspace-b"))
    assert a == (tmp_path / "tenant-a" / "workspace-a").resolve()
    assert b == (tmp_path / "tenant-b" / "workspace-b").resolve()
    assert a != b


@pytest.mark.parametrize("value", ["../escape", "/absolute", "x\x00y", "a/b"])
def test_workspace_resolver_rejects_non_identifier_segments(tmp_path, value):
    resolver = WorkspaceResolver(tmp_path)
    with pytest.raises(InvalidWorkspaceScope):
        resolver.resolve(TenantContext(value, "workspace"))


def test_resolver_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "tenant-a").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceContainmentError):
        WorkspaceResolver(root).resolve(TenantContext("tenant-a", "workspace-a"))
```

Add tests proving MCP config, shell cwd, file tools, and deletion path calculation receive the exact same resolved root object.

- [ ] **Step 2: Run path tests and observe missing resolver**

```bash
uv run pytest tests/test_workspace_resolver.py tests/test_mcp_config.py \
  tests/test_sandbox_manager.py -q
```

Expected: FAIL because workspace canonicalization remains distributed across server/MCP/sandbox code.

- [ ] **Step 3: Implement the single resolver and server-ID-only deletion path**

```python
class WorkspaceResolver:
    _SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve(strict=True)

    def resolve(self, context: TenantContext, *, create: bool = False) -> Path:
        for value in (context.tenant_id, context.workspace_id):
            if not self._SEGMENT.fullmatch(value):
                raise InvalidWorkspaceScope(value)
        tenant_candidate = self.root / context.tenant_id
        if create:
            tenant_candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        tenant_root = tenant_candidate.resolve(strict=create)
        if not tenant_root.is_relative_to(self.root):
            raise WorkspaceContainmentError(str(tenant_root))
        candidate = tenant_root / context.workspace_id
        if create:
            candidate.mkdir(exist_ok=True, mode=0o700)
        resolved = candidate.resolve(strict=create)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceContainmentError(str(resolved))
        return resolved
```

For UUIDs stored with hyphens, the pattern accepts the canonical value. Do not accept a user-provided filesystem path in deletion, MCP, tool, or shell APIs; these consumers receive only `Path` from `WorkspaceResolver`.

- [ ] **Step 4: Replace duplicate workspace-root calculations**

Inject the resolver into sandbox/runtime/MCP assembly. Reuse existing MCP config symlink/canonicalization checks inside the resolved tenant root; do not loosen `config_trust` or stdio containment. Remove `Path.cwd()`/config-parent fallback from tenant runtime creation.

- [ ] **Step 5: Run containment and MCP/sandbox regression tests**

```bash
uv run pytest tests/test_workspace_resolver.py tests/test_mcp_config.py \
  tests/test_mcp_integration.py tests/test_sandbox_manager.py tests/test_shell.py -q
```

Expected: traversal/symlink cases fail closed and existing MCP/sandbox restrictions remain PASS.

- [ ] **Step 6: Commit the workspace boundary**

```bash
git add src/multiclaw/tenancy/workspace.py src/multiclaw/mcp/config.py \
  src/multiclaw/mcp/transport/factory.py src/multiclaw/governance/sandbox/manager.py \
  tests/test_workspace_resolver.py tests/test_mcp_config.py tests/test_sandbox_manager.py
git commit -m "Keep every filesystem capability inside its tenant workspace" \
  -m "Centralize tenant/workspace path derivation and reuse it for tools, sandbox, MCP, and future purge operations." \
  -m "Constraint: Recursive deletion accepts server IDs, never client paths" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: workspace, MCP, sandbox, and shell containment suites"
```

### Task 8: Replace the process-global agent with a bounded per-tenant RuntimePool

**Files:**
- Create: `src/multiclaw/runtime/__init__.py`
- Create: `src/multiclaw/runtime/models.py`
- Create: `src/multiclaw/runtime/factory.py`
- Create: `src/multiclaw/runtime/pool.py`
- Modify: `src/multiclaw/skills/manager.py`
- Modify: `src/multiclaw/mcp/manager.py`
- Modify: `src/multiclaw/tools/registry.py`
- Modify: `src/multiclaw/server.py`
- Create: `tests/test_runtime_pool.py`
- Create: `tests/test_runtime_isolation.py`
- Modify: `tests/test_skills.py`
- Modify: `tests/test_mcp_integration.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Write failing runtime identity, create-lock, quota, and eviction tests**

```python
@pytest.mark.asyncio
async def test_same_tenant_reuses_runtime_and_different_tenants_never_share_state(pool, contexts):
    a1, a2 = await asyncio.gather(
        pool.acquire(contexts.a),
        pool.acquire(contexts.a),
    )
    b = await pool.acquire(contexts.b)
    assert a1 is a2
    assert a1 is not b
    assert a1.agent is not b.agent
    assert a1.event_bus is not b.event_bus
    assert a1.event_router is not b.event_router
    assert a1.scheduler is not b.scheduler
    assert a1.registry is not b.registry
    assert a1.skill_manager is not b.skill_manager
    assert a1.mcp_manager is not b.mcp_manager
    assert a1.workspace_root != b.workspace_root


@pytest.mark.asyncio
async def test_pool_evicts_only_safe_idle_runtime(pool, contexts, clock):
    idle = await pool.acquire(contexts.a)
    active = await pool.acquire(contexts.b)
    active.active_executing_run_count = 1
    clock.advance(pool.idle_ttl_ms + 1)
    assert await pool.evict_idle(clock.now_ms()) == 1
    assert await pool.peek(contexts.a.tenant_id) is None
    assert await pool.peek(contexts.b.tenant_id) is active


@pytest.mark.asyncio
async def test_pool_returns_capacity_error_when_no_runtime_is_evictable(pool_at_capacity, contexts):
    resident = await pool_at_capacity.acquire(contexts.a)
    resident.active_executing_run_count = 1
    with pytest.raises(RuntimeCapacityError) as error:
        await pool_at_capacity.acquire(contexts.b)
    assert error.value.retry_after_seconds >= 1
```

Add tests for awaiting-user checkpoint-safe eviction, tenant revocation, MCP shutdown, Skill state separation, and idempotent pool close. The persisted per-tenant concurrent-run quota is enforced transactionally by `WorkflowCoordinator.start_run` in Task 10; RuntimePool counters are eviction/capacity signals, not the authoritative quota.

- [ ] **Step 2: Run runtime tests and observe the global-singleton failure**

```bash
uv run pytest tests/test_runtime_pool.py tests/test_runtime_isolation.py \
  tests/test_skills.py tests/test_mcp_integration.py tests/test_tools.py -q
```

Expected: FAIL because `server.create_agent()` constructs one process-global object graph.

- [ ] **Step 3: Extract runtime-owned assembly from `server.py`**

Move tool/MCP/skill/agent construction into an injected factory:

```python
class RuntimeFactory:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        workspace_resolver: WorkspaceResolver,
        secret_resolver: SecretResolver | None = None,
    ) -> None: ...

    async def create(self, context: TenantContext) -> TenantRuntime:
        workspace_root = self.workspace_resolver.resolve(context, create=True)
        event_bus = EventBus(tenant_id=context.tenant_id)
        event_router = EventRouter()
        skill_manager = SkillManager(project_root=workspace_root, max_active=self.settings.skill.max_active)
        registry = self._build_registry(workspace_root, event_bus)
        mcp_manager = self._build_mcp_manager(workspace_root, event_bus, registry)
        scheduler = self._build_scheduler(context, event_bus)
        agent = self._build_agent(context, registry, scheduler, event_bus, skill_manager)
        return TenantRuntime(
            tenant_id=context.tenant_id,
            runtime_instance_id=str(uuid4()),
            workspace_root=workspace_root,
            agent=agent,
            event_bus=event_bus,
            event_router=event_router,
            scheduler=scheduler,
            registry=registry,
            skill_manager=skill_manager,
            mcp_manager=mcp_manager,
            last_used_at_ms=self.clock.now_ms(),
        )
```

The process may share immutable settings, `Database`, migration status, and provider class definitions. It must not share any object listed on `TenantRuntime`.

- [ ] **Step 4: Implement create locks, quotas, safe eviction, and deterministic close**

```python
async def acquire(self, context: TenantContext) -> TenantRuntime:
    lock = self._create_locks.setdefault(context.tenant_id, asyncio.Lock())
    async with lock:
        runtime = self._runtimes.get(context.tenant_id)
        if runtime is None:
            await self._ensure_capacity()
            runtime = await self._factory.create(context)
            self._runtimes[context.tenant_id] = runtime
        runtime.last_used_at_ms = self._clock.now_ms()
        return runtime


async def revoke(self, tenant_id: str) -> None:
    lock = self._create_locks.setdefault(tenant_id, asyncio.Lock())
    async with lock:
        runtime = self._runtimes.pop(tenant_id, None)
        if runtime is not None:
            await runtime.close()
```

`TenantRuntime.close()` stops MCP connections, deactivates Skill state, closes sandbox-owned resources, clears any Secret handles, and is idempotent. `_ensure_capacity()` may evict only runtimes with `active_executing_run_count == 0` and either no active runs or all active runs persisted at `awaiting_user` with no executing tool.

- [ ] **Step 5: Rewire application lifecycle to own Database and RuntimePool**

`lifespan()` creates shared infrastructure once, sets `app.state.database`, `app.state.runtime_pool`, `app.state.workspace_resolver`, and closes pool before database disposal. Remove module globals `agent` and `shared_bus`; tests must assert those names are absent. Sandbox startup readiness becomes an immutable capability template or a runtime factory probe, not a mutable shared controller.

- [ ] **Step 6: Run runtime and existing isolation regressions**

```bash
uv run pytest tests/test_runtime_pool.py tests/test_runtime_isolation.py \
  tests/test_skills.py tests/test_mcp_integration.py tests/test_tools.py \
  tests/test_server.py -q
```

Expected: create-lock produces one runtime, users share no mutable runtime state, safe eviction rules hold, and server lifecycle closes every resident runtime.

- [ ] **Step 7: Commit the runtime isolation boundary**

```bash
git add src/multiclaw/runtime src/multiclaw/skills/manager.py \
  src/multiclaw/mcp/manager.py src/multiclaw/tools/registry.py src/multiclaw/server.py \
  tests/test_runtime_pool.py tests/test_runtime_isolation.py tests/test_skills.py \
  tests/test_mcp_integration.py tests/test_tools.py tests/test_server.py
git commit -m "Stop mutable agent state from crossing tenant boundaries" \
  -m "Move Agent, EventBus, Scheduler, Skill, Tool, MCP, sandbox, and workspace state into a quota-aware per-tenant runtime pool." \
  -m "Constraint: v1 pool is standalone and process-local" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Directive: Do not reintroduce module-global runtime components" \
  -m "Tested: runtime pool, runtime isolation, Skill, MCP, Tool, and server suites"
```

### Task 9: Route events and SSE by exact tenant/workspace/session/run scope

**Files:**
- Modify: `src/multiclaw/events/types.py`
- Modify: `src/multiclaw/events/bus.py`
- Create: `src/multiclaw/events/router.py`
- Modify: `src/multiclaw/events/__init__.py`
- Modify: `src/multiclaw/stream.py`
- Create: `src/multiclaw/api/chat.py`
- Modify: `src/multiclaw/agent/base.py`
- Modify: `src/multiclaw/agent/multiclaw.py`
- Modify: `src/multiclaw/tools/scheduler.py`
- Modify: `src/multiclaw/server.py`
- Create: `tests/test_event_router.py`
- Create: `tests/test_tenant_sse.py`
- Modify: `tests/test_events.py`
- Modify: `tests/test_stream.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing exact-match event and concurrent SSE tests**

```python
@pytest.mark.asyncio
async def test_event_router_delivers_only_exact_run_scope():
    router = EventRouter()
    target = EventScope(tenant_id="t", workspace_id="w", session_id="s", run_id="r")
    seen: list[ScopedEvent] = []

    async def collect(event: ScopedEvent) -> None:
        seen.append(event)

    subscription = router.subscribe(target, collect)
    for scope in (
        target,
        EventScope(tenant_id="other", workspace_id="w", session_id="s", run_id="r"),
        EventScope(tenant_id="t", workspace_id="other", session_id="s", run_id="r"),
        EventScope(tenant_id="t", workspace_id="w", session_id="other", run_id="r"),
        EventScope(tenant_id="t", workspace_id="w", session_id="s", run_id="other"),
    ):
        await router.publish(ScopedEvent.from_scope(scope, "tool.completed", {}))
    subscription.close()
    assert [(event.tenant_id, event.workspace_id, event.session_id, event.run_id) for event in seen] == [
        ("t", "w", "s", "r")
    ]


def test_two_concurrent_sse_streams_never_receive_each_others_run_events(app, two_users):
    a, b = start_concurrent_chats(app, two_users)
    assert a.run_id != b.run_id
    assert all(event["run_id"] == a.run_id for event in a.scoped_events)
    assert all(event["run_id"] == b.run_id for event in b.scoped_events)
    assert not set(a.tool_call_ids) & set(b.tool_call_ids)
```

Add negative tests for missing scope fields, wildcard subscription attempts, subscriptions closed on disconnect, and events emitted after the wrong run finishes.

- [ ] **Step 2: Run event/SSE tests and observe global wildcard leakage**

```bash
uv run pytest tests/test_event_router.py tests/test_tenant_sse.py \
  tests/test_events.py tests/test_stream.py tests/test_server.py -q
```

Expected: FAIL because current `Event` lacks scope and `/api/chat` subscribes to global `shared_bus` wildcard.

- [ ] **Step 3: Make complete scope mandatory on user-visible events**

```python
class EventScope(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=36)
    workspace_id: str = Field(min_length=1, max_length=36)
    session_id: str = Field(min_length=1, max_length=36)
    run_id: str = Field(min_length=1, max_length=36)

    @classmethod
    def from_context(cls, context: TenantContext) -> "EventScope":
        if context.session_id is None or context.run_id is None:
            raise ValueError("event scope requires session and run")
        return cls(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            run_id=context.run_id,
        )


class ScopedEvent(EventScope):
    event_type: str = Field(min_length=1, max_length=128)
    occurred_at_ms: int
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_scope(cls, scope: EventScope, event_type: str, data: dict[str, Any]):
        return cls(
            **scope.model_dump(),
            event_type=event_type,
            occurred_at_ms=int(time.time() * 1000),
            data=data,
        )

    @classmethod
    def from_context(cls, context: TenantContext, event_type: str, data: dict[str, Any]):
        if context.session_id is None or context.run_id is None:
            raise ValueError("user-visible events require complete run scope")
        return cls(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            session_id=context.session_id,
            run_id=context.run_id,
            event_type=event_type,
            occurred_at_ms=int(time.time() * 1000),
            data=data,
        )
```

Runtime-local diagnostic events that occur before a session/run exists use a separate `RuntimeEvent` type and never enter user SSE.

- [ ] **Step 4: Implement exact-key subscriptions and remove wildcard user delivery**

```python
ScopeKey = tuple[str, str, str, str]


class EventRouter:
    def __init__(self) -> None:
        self._handlers: dict[ScopeKey, dict[str, Handler]] = defaultdict(dict)

    def subscribe(self, scope: EventScope, handler: Handler) -> Subscription:
        key = (scope.tenant_id, scope.workspace_id, scope.session_id, scope.run_id)
        subscription_id = str(uuid4())
        self._handlers[key][subscription_id] = handler
        return Subscription(lambda: self._unsubscribe(key, subscription_id))

    async def publish(self, event: ScopedEvent) -> None:
        key = (event.tenant_id, event.workspace_id, event.session_id, event.run_id)
        for handler in tuple(self._handlers.get(key, {}).values()):
            await handler(event)
```

Do not provide `"*"`, tenant-only, session-only, or request-ID subscription overloads.

- [ ] **Step 5: Rebuild chat SSE around a persisted run ID and scoped subscription**

The endpoint creates `run_id` before streaming, derives `run_context`, and subscribes exactly once:

```python
run_id = str(uuid4())
run_context = context.for_run(session.id, run_id)
runtime = await request.app.state.runtime_pool.acquire(run_context)
subscription = runtime.event_router.subscribe(EventScope.from_context(run_context), collector)

async def event_stream():
    try:
        yield encoder.start()
        yield encoder.data_part(
            "data-run",
            {"session_id": session.id, "run_id": run_id},
            transient=True,
        )
        async for item in runtime.agent.handle_message_stream(message, context=run_context):
            yield encode_agent_item(encoder, item)
    finally:
        subscription.close()
        await request.app.state.runtime_pool.release(context.tenant_id)
```

All scheduler/agent events use `ScopedEvent.from_context`. Persisted terminal state must commit before the SSE finish event is emitted; Task 11 supplies that persistence.

- [ ] **Step 6: Run concurrent SSE and event regressions**

```bash
uv run pytest tests/test_event_router.py tests/test_tenant_sse.py \
  tests/test_events.py tests/test_stream.py tests/test_server.py \
  tests/test_agent_stream_tool_ids.py -q
```

Expected: exact-match cases PASS, two concurrent users observe zero foreign events, and stream tool IDs remain stable.

- [ ] **Step 7: Commit exact event routing**

```bash
git add src/multiclaw/events src/multiclaw/stream.py src/multiclaw/api/chat.py \
  src/multiclaw/agent src/multiclaw/tools/scheduler.py src/multiclaw/server.py \
  tests/test_event_router.py tests/test_tenant_sse.py tests/test_events.py \
  tests/test_stream.py tests/test_server.py tests/test_agent_stream_tool_ids.py
git commit -m "Prevent live run events from reaching neighboring sessions" \
  -m "Require complete tenant/workspace/session/run scope and replace wildcard SSE subscriptions with exact-key routing." \
  -m "Rejected: Filter after wildcard delivery | accidental consumers would remain unsafe" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: event router, concurrent tenant SSE, stream, server, and agent tool-ID suites"
```

### Task 10: Enforce run leases, fencing, and versioned workflow transitions

**Files:**
- Create: `src/multiclaw/workflow/__init__.py`
- Create: `src/multiclaw/workflow/models.py`
- Create: `src/multiclaw/storage/repositories/workflow.py`
- Create: `src/multiclaw/workflow/coordinator.py`
- Modify: `src/multiclaw/storage/uow.py`
- Modify: `src/multiclaw/api/chat.py`
- Create: `tests/test_workflow_state.py`
- Modify: `tests/integration/test_mysql_contract.py`

- [ ] **Step 1: Write failing legal-state, CAS, and stale-fencing tests**

```python
@pytest.mark.asyncio
async def test_stale_runtime_cannot_write_after_lease_takeover(database, run_context):
    first = await coordinator(database).start_run(run_context, "runtime-1")
    await expire_lease_with_db_clock(database, run_context)
    second = await coordinator(database).acquire_run(run_context, "runtime-2")
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(StaleFenceError):
        await coordinator(database).heartbeat(first)


@pytest.mark.asyncio
async def test_run_cannot_complete_with_nonterminal_execution(database, run_context):
    lease = await coordinator(database).start_run(run_context, "runtime")
    await seed_execution(database, lease, status="executing")
    with pytest.raises(InvalidTransitionError):
        await coordinator(database).finish_run(lease, RunStatus.COMPLETED)


@pytest.mark.asyncio
async def test_version_cas_allows_exactly_one_concurrent_approval_decision(database, approval):
    results = await asyncio.gather(
        decide(database, approval, approved=True, version=approval.version),
        decide(database, approval, approved=False, version=approval.version),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ApprovalRecord) for result in results) == 1
    assert sum(isinstance(result, VersionConflictError) for result in results) == 1
```

Parameterize all cases over SQLite and MySQL. Add cases for expired lease takeover, heartbeat only by current fence, terminal-state immutability, approval expiry, optional approval FK, and `active + NULL default_workspace_id` readiness failure.

Add a concurrent `start_run` test at `runtime.max_concurrent_runs_per_tenant`: the DB-serialized count of `running|awaiting_user|resuming` runs allows exactly the configured number and maps the next attempt to `TenantRunQuotaError`/HTTP `429`. Runtime activity counters are updated only after the corresponding run transaction commits and are reconciled from persisted state when a runtime is recreated.

Add an application-clock-skew test that monkeypatches Python wall time by `+24h` and `-24h` while holding database time constant; lease expiry/takeover results must remain identical because all predicates and expiry calculations use `db_now_ms()` inside SQL.

- [ ] **Step 2: Run workflow tests and observe missing state machine**

```bash
uv run pytest tests/test_workflow_state.py tests/integration/test_mysql_contract.py -q
```

Expected: FAIL because workflow repositories and coordinator do not exist.

- [ ] **Step 3: Define typed states and transition commands**

Use string enums whose values exactly match the schema checks:

```python
class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"
    BLOCKED_CORRUPT = "blocked_corrupt"
    BLOCKED_INCOMPATIBLE = "blocked_incompatible"


class ApprovalStatus(StrEnum):
    AWAITING_USER = "awaiting_user"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class RunLease:
    context: TenantContext
    lease_owner: str
    fencing_token: int
    version: int
    lease_expires_at: int
```

Define execution statuses/recovery strategies exactly as approved. Encode allowed transitions in constant maps and reject any source/target pair not present before executing SQL.

The run transition map is exactly `running -> awaiting_user`, `awaiting_user -> resuming`, `resuming -> running`, `running -> completed`, and `running|resuming -> failed_terminal|blocked_incompatible|blocked_corrupt|cancelled`. Approval transitions are exactly `awaiting_user -> approved|rejected|expired`; do not invent a `pending` or `cancelled` approval state.

- [ ] **Step 4: Implement every mutation as one DB-clock CAS statement**

The common fence predicate must be reused by heartbeat, checkpoint, execution, and run writes:

```python
def current_lease_predicate(lease: RunLease, dialect: StorageDialect):
    return and_(
        agent_runs.c.tenant_id == lease.context.tenant_id,
        agent_runs.c.workspace_id == lease.context.workspace_id,
        agent_runs.c.session_id == lease.context.session_id,
        agent_runs.c.run_id == lease.context.run_id,
        agent_runs.c.lease_owner == lease.lease_owner,
        agent_runs.c.fencing_token == lease.fencing_token,
        agent_runs.c.version == lease.version,
        agent_runs.c.lease_expires_at > dialect.db_now_ms(),
    )
```

Lease takeover must lock the run row through `StorageDialect.lock_run`, require `lease_expires_at <= db_now_ms()`, increment `fencing_token` and `version`, set the new runtime, and compute new expiry inside the same SQL statement/UoW. A `rowcount != 1` maps to `LeaseConflictError` or `StaleFenceError`, never a blind retry.

- [ ] **Step 5: Make WorkflowCoordinator the sole transition facade**

HTTP routes, agent code, and scheduler must call coordinator methods. Make repository mutation methods package-private and require a `RunLease` for run/execution/checkpoint writes. `decide_approval` accepts authenticated `TenantContext`, approval ID, decision, and version; it only records the decision and never executes a tool directly.

Update `api/chat.py` so the run row and initial lease are committed through `WorkflowCoordinator.start_run(run_context, runtime.runtime_instance_id)` before the SSE start/data-run events. Pass the resulting `RunLease` into `agent.handle_message_stream`; if the HTTP client disconnects, cancel only in-process streaming, then transition/persist the run according to its last committed checkpoint instead of deleting the run row.

- [ ] **Step 6: Run both-backend CAS/locking verification**

```bash
uv run pytest tests/test_workflow_state.py tests/test_tenant_sse.py -q
MULTICLAW_TEST_MYSQL_URL="$MULTICLAW_TEST_MYSQL_URL" \
  uv run pytest tests/test_workflow_state.py tests/integration/test_mysql_contract.py -q
```

Expected: exactly one writer wins each race, stale fencing writes are zero, completed runs have zero nonterminal executions, and MySQL `FOR UPDATE` behavior is observed.

- [ ] **Step 7: Commit durable workflow ownership**

```bash
git add src/multiclaw/workflow src/multiclaw/storage/repositories/workflow.py \
  src/multiclaw/storage/uow.py src/multiclaw/api/chat.py tests/test_workflow_state.py \
  tests/integration/test_mysql_contract.py
git commit -m "Keep one durable writer responsible for each run" \
  -m "Add DB-clock leases, fencing tokens, legal transitions, and version CAS so stale runtimes cannot mutate recovered work." \
  -m "Constraint: SQLite serializes writers; MySQL locks run rows under READ COMMITTED" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Directive: Every workflow write must include full scope and current fencing token" \
  -m "Tested: workflow state and both-backend locking/CAS suites"
```

### Task 11: Persist structured checkpoints and deterministic recovery

**Files:**
- Create: `src/multiclaw/workflow/recovery.py`
- Modify: `src/multiclaw/workflow/models.py`
- Modify: `src/multiclaw/workflow/coordinator.py`
- Modify: `src/multiclaw/storage/repositories/workflow.py`
- Modify: `src/multiclaw/agent/multiclaw.py`
- Modify: `src/multiclaw/agent/resilience.py`
- Create: `tests/test_workflow_recovery.py`
- Create: `tests/integration/test_workflow_faults.py`

- [ ] **Step 1: Write failing checkpoint validation and crash-window tests**

```python
@pytest.mark.asyncio
async def test_checkpoint_hash_mismatch_blocks_run(database, lease):
    checkpoint = await write_valid_checkpoint(database, lease, phase="model_output_committed")
    await corrupt_payload_without_hash_update(database, checkpoint.checkpoint_id)
    outcome = await RecoveryService(database).recover(lease.context, "runtime-2")
    assert outcome.status == RunStatus.BLOCKED_CORRUPT
    assert outcome.executions_started == 0


@pytest.mark.asyncio
async def test_old_fence_cannot_write_run_only_checkpoint(database, expired_lease):
    new_lease = await takeover(database, expired_lease.context)
    with pytest.raises(StaleFenceError):
        await coordinator(database).checkpoint(
            expired_lease,
            CheckpointPhase.MODEL_OUTPUT_COMMITTED,
            {
                "run_id": expired_lease.context.run_id,
                "message_id": "m1",
                "output_digest": "a" * 64,
                "model_cursor": "cursor-1",
                "cursor": "cursor-1",
            },
        )
    assert await checkpoint_count(database, new_lease.context, expired_lease.fencing_token) == 0
```

Fault-injection cases must cover crash after run creation, after model output commit, before approval event, after approval CAS, before tool call, after remote side effect/before result commit, after result commit/before SSE terminal event, and after DB purge commit/before worker acknowledgement.

- [ ] **Step 2: Run recovery tests and observe missing phase protocol**

```bash
uv run pytest tests/test_workflow_recovery.py tests/integration/test_workflow_faults.py -q
```

Expected: FAIL because checkpoints are not validated or replayed.

- [ ] **Step 3: Define fixed checkpoint phases and payload models**

```python
class CheckpointPhase(StrEnum):
    RUN_STARTED = "run_started"
    MODEL_OUTPUT_COMMITTED = "model_output_committed"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTION_DISPATCHING = "execution_dispatching"
    EXECUTION_RESULT_OBSERVED = "execution_result_observed"
    RUN_TERMINAL = "run_terminal"


PHASE_PAYLOADS: dict[CheckpointPhase, type[BaseModel]] = {
    CheckpointPhase.RUN_STARTED: RunStartedPayload,
    CheckpointPhase.MODEL_OUTPUT_COMMITTED: ModelOutputPayload,
    CheckpointPhase.AWAITING_APPROVAL: AwaitingApprovalPayload,
    CheckpointPhase.EXECUTION_DISPATCHING: ExecutionDispatchingPayload,
    CheckpointPhase.EXECUTION_RESULT_OBSERVED: ExecutionResultObservedPayload,
    CheckpointPhase.RUN_TERMINAL: RunTerminalPayload,
}
```

Define the payload models with the exact required fields and cursor semantics:

```python
class CheckpointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1


class RunStartedPayload(CheckpointPayload):
    tenant_id: str
    workspace_id: str
    session_id: str
    run_id: str
    started_at_ms: int
    model_cursor: str
    next_step: Literal["model_inference"] = "model_inference"
    cursor: str


class ModelOutputPayload(CheckpointPayload):
    run_id: str
    message_id: str
    output_digest: str
    model_cursor: str
    next_step: Literal["tool_plan_or_terminal"] = "tool_plan_or_terminal"
    cursor: str


class AwaitingApprovalPayload(CheckpointPayload):
    run_id: str
    approval_id: str
    tool_call_id: str
    approval_expires_at_ms: int
    resume_cursor: str
    next_step: Literal["approval_resolution"] = "approval_resolution"
    cursor: str


class ExecutionDispatchingPayload(CheckpointPayload):
    run_id: str
    execution_id: str
    tool_call_id: str
    recovery_strategy: RecoveryStrategy
    input_hash: str
    input_ref: str
    idempotency_key: str | None = None
    dispatch_cursor: str
    next_step: Literal["execution_observation"] = "execution_observation"
    cursor: str

    @model_validator(mode="after")
    def require_idempotency_key_for_retry(self):
        if self.recovery_strategy == RecoveryStrategy.IDEMPOTENT_RETRY and not self.idempotency_key:
            raise ValueError("idempotent_retry requires idempotency_key")
        return self


class ExecutionResultObservedPayload(CheckpointPayload):
    run_id: str
    execution_id: str
    result_status: ExecutionStatus
    result_digest: str
    result_ref: str
    external_request_id: str | None = None
    resume_cursor: str
    next_step: Literal["continue_or_terminal"] = "continue_or_terminal"
    cursor: str


class RunTerminalPayload(CheckpointPayload):
    run_id: str
    terminal_status: RunStatus
    finished_at_ms: int
    final_digest: str
    next_step: None = None
    cursor: None = None
```

Validators require `cursor == model_cursor`, `cursor == resume_cursor`, or `cursor == dispatch_cursor` for the applicable phase. Each bounded ID/reference/digest field has an explicit maximum length. Canonicalize with sorted compact JSON, compute SHA-256, enforce configured maximum bytes, and reject keys matching `secret`, `token`, `password`, `api_key`, or `authorization` recursively.

- [ ] **Step 4: Write checkpoint and related state in the same UoW**

Coordinator methods must persist domain state first and checkpoint second before committing. For example, model output persistence writes the assistant memory row plus `MODEL_OUTPUT_COMMITTED` checkpoint in one `TenantUnitOfWork`; terminal SSE is sent only after that UoW exits successfully.

```python
async def checkpoint(self, lease, phase, payload):
    model = PHASE_PAYLOADS[phase].model_validate(payload)
    encoded = canonical_json(model.model_dump(mode="json"))
    reject_secret_fields(model.model_dump(mode="python"))
    if len(encoded) > self.settings.max_checkpoint_payload_bytes:
        raise CheckpointTooLargeError(len(encoded))
    await self.repository.insert_checkpoint(
        lease=lease,
        phase=phase.value,
        schema_version=1,
        payload_json=encoded.decode(),
        payload_hash=sha256(encoded).hexdigest(),
    )
```

- [ ] **Step 5: Implement recovery classification without Python-object deserialization**

Recovery loads the latest committed checkpoint under full scope, validates version/phase/schema/hash/payload, obtains a new run lease, and returns one explicit action: resume model, await user, replay read-only, retry idempotent, mark manual uncertain, terminal no-op, blocked corrupt, or blocked incompatible. It must never unpickle/import dynamically named classes or reconstruct a prior runtime object graph.

- [ ] **Step 6: Run deterministic recovery and fault-injection verification**

```bash
uv run pytest tests/test_workflow_recovery.py tests/integration/test_workflow_faults.py \
  tests/test_agent_resilience.py -q
```

Expected: every crash window converges to exactly one documented outcome, corrupt/incompatible checkpoints start no tools, and terminal SSE follows committed terminal state.

- [ ] **Step 7: Commit the continuation protocol**

```bash
git add src/multiclaw/workflow src/multiclaw/storage/repositories/workflow.py \
  src/multiclaw/agent/multiclaw.py src/multiclaw/agent/resilience.py \
  tests/test_workflow_recovery.py tests/integration/test_workflow_faults.py \
  tests/test_agent_resilience.py
git commit -m "Make interrupted runs recover from data instead of process memory" \
  -m "Persist fixed, hashed, Secret-free checkpoint phases and classify every crash window before resuming work under a new fence." \
  -m "Rejected: Python object serialization | incompatible and unsafe across deployments" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: workflow recovery, fault injection, and agent resilience suites"
```

### Task 12: Persist approvals and serial tool execution with recovery metadata

**Files:**
- Modify: `src/multiclaw/tools/base.py`
- Modify: `src/multiclaw/tools/scheduler.py`
- Modify: `src/multiclaw/agent/tool_batch.py`
- Modify: `src/multiclaw/agent/multiclaw.py`
- Modify: `src/multiclaw/workflow/coordinator.py`
- Modify: `src/multiclaw/workflow/recovery.py`
- Create: `src/multiclaw/api/approvals.py`
- Modify: `src/multiclaw/server.py`
- Create: `tests/test_scheduler_persistence.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_tool_batch.py`
- Modify: `tests/test_agent_stream_tool_ids.py`

- [ ] **Step 1: Write failing serial-execution, restart-approval, and uncertain-outcome tests**

```python
@pytest.mark.asyncio
async def test_same_run_never_executes_two_tools_concurrently(scheduler, lease, recording_tool):
    first, second = await asyncio.gather(
        scheduler.run(lease, recording_tool("first"), {}),
        scheduler.run(lease, recording_tool("second"), {}),
        return_exceptions=True,
    )
    assert recording_tool.max_concurrency == 1
    assert sum(isinstance(value, ExecutionConflictError) for value in (first, second)) == 1


@pytest.mark.asyncio
async def test_approval_survives_runtime_eviction(app, pending_approval):
    await app.state.runtime_pool.revoke(pending_approval.tenant_id)
    response = await decide_approval_api(
        app,
        pending_approval.owner_cookie,
        pending_approval.approval_id,
        approved=True,
        version=pending_approval.version,
    )
    assert response.status_code == 200
    assert await persisted_approval_status(app, pending_approval) == "approved"
    assert await execution_count(app, pending_approval) == 0


@pytest.mark.asyncio
async def test_non_idempotent_crash_after_dispatch_becomes_uncertain(faulting_scheduler, lease):
    await faulting_scheduler.crash_after_external_dispatch(lease, non_idempotent_tool())
    outcome = await recover(lease.context)
    assert outcome.execution_status == "uncertain"
    assert outcome.external_calls_after_recovery == 0
```

Add tests that immutable input payload/hash, `external_request_id`, `idempotency_key`, `result_ref`, and `result_digest` survive restart; cross-tenant approval IDs return `404`; stale approval versions return `409`; expired approvals return `410`.

Add a positive concurrency test proving tools from two different run IDs may overlap up to the configured per-tenant run limit while the same-run maximum remains one.

- [ ] **Step 2: Run scheduler tests and observe in-memory approval/concurrency failures**

```bash
uv run pytest tests/test_scheduler_persistence.py tests/test_tools.py \
  tests/test_tool_batch.py tests/test_agent_stream_tool_ids.py -q
```

Expected: FAIL because approvals use `_pending`, read-only calls can run in parallel, and tool inputs/results are not durably classified.

- [ ] **Step 3: Require recovery declarations on every ToolBuilder**

```python
class ToolBuilder(ABC, Generic[P]):
    tool_kind: ClassVar[Literal["native", "mcp"]] = "native"
    recovery_strategy: ClassVar[RecoveryStrategy]
    idempotency_key_field: ClassVar[str | None] = None

    def recovery_metadata(self, params: P) -> ToolRecoveryMetadata:
        key = getattr(params, self.idempotency_key_field) if self.idempotency_key_field else None
        return ToolRecoveryMetadata(
            tool_kind=self.tool_kind,
            recovery_strategy=self.recovery_strategy,
            idempotency_key=key,
        )
```

Declare filesystem reads/searches as `read_only_replay`; writes without proven provider idempotency as `manual_uncertain`; operations with a stable externally honored idempotency key as `idempotent_retry`. Startup/runtime factory must reject a registered tool without an explicit declaration.

- [ ] **Step 4: Replace `ToolBatchExecutor` concurrency with strict per-run sequencing**

Remove the read-only parallel branch and process calls in input order:

```python
async def execute_batch(self, lease: RunLease, calls: Sequence[ToolCall]) -> list[ToolResult]:
    results: list[ToolResult] = []
    for call in calls:
        results.append(await self.scheduler.run(lease, call))
    return results
```

The database unique/CAS contract remains authoritative; the Python loop is not the only guard.

- [ ] **Step 5: Make scheduler operations repository-backed**

`CoreToolScheduler.run` now receives `RunLease` and follows: validate/canonicalize immutable input; create approval if required and return `AWAITING_APPROVAL` without holding a coroutine; otherwise create one `not_started` execution; CAS to `executing`; persist external request ID as soon as known; execute; persist result reference/digest and terminal status; emit scoped events after commits.

Canonical input is compact sorted-key JSON encoded as UTF-8, limited to `262144` bytes, and hashed with SHA-256 before insertion. Payloads above the limit fail validation before approval/execution rows are created and must use a scoped workspace/object reference. Recursive Secret plaintext keys are rejected; only Secret references may appear.

Preserve the current approval window as the protocol constant `APPROVAL_TTL_MS = 120000`; compute `expires_at = db_now_ms() + APPROVAL_TTL_MS` in the approval insert. Expiration is a DB-clock CAS from `awaiting_user` to `expired`, not an in-memory `asyncio.wait_for` timeout.

Remove `_pending`, `_pending_results`, timeout waits, and `resolve_approval`. Approval API calls only:

```python
record = await coordinator.decide_approval(
    context=context,
    approval_id=approval_id,
    approved=body.approved,
    version=body.version,
)
return ApprovalResponse.from_record(record)
```

The recovery worker, not the HTTP request, reacquires runtime/run lease and advances approved work.

Extend `workflow/recovery.py` with `WorkflowRecoveryWorker.run_once()`: select approved/expired-waiting and expired-lease candidate runs in bounded DB-clock order; for each candidate load the user's default workspace, acquire its runtime, acquire/take over the run lease, CAS `awaiting_user -> resuming`, then invoke `RecoveryService`. Lifespan owns one cancellable standalone worker loop and awaits it on shutdown. The test fixture calls `run_once()` directly so restart behavior is deterministic and never depends on sleep.

- [ ] **Step 6: Run persisted scheduler and approval regressions**

```bash
uv run pytest tests/test_scheduler_persistence.py tests/test_tools.py \
  tests/test_tool_batch.py tests/test_agent_stream_tool_ids.py \
  tests/test_workflow_recovery.py -q
```

Expected: one active tool per run, approvals survive restart, approval HTTP never executes tools directly, and all recovery strategies produce their exact outcome.

- [ ] **Step 7: Commit durable tool semantics**

```bash
git add src/multiclaw/tools src/multiclaw/agent src/multiclaw/workflow \
  src/multiclaw/api/approvals.py src/multiclaw/server.py \
  tests/test_scheduler_persistence.py tests/test_tools.py tests/test_tool_batch.py \
  tests/test_agent_stream_tool_ids.py tests/test_workflow_recovery.py
git commit -m "Prevent approvals and tool side effects from disappearing with a runtime" \
  -m "Persist approval/execution state, serialize tools within each run, and classify crash recovery from immutable input and external effect metadata." \
  -m "Constraint: Non-idempotent ambiguous dispatch is manual uncertain, never automatic retry" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: persisted scheduler, Tool, serial batch, stream ID, and workflow recovery suites"
```

### Task 13: Encrypt user BYOK Secrets and enforce strict fallback

**Files:**
- Create: `src/multiclaw/secrets/__init__.py`
- Create: `src/multiclaw/secrets/keyring.py`
- Create: `src/multiclaw/secrets/envelope.py`
- Create: `src/multiclaw/secrets/resolver.py`
- Create: `src/multiclaw/secrets/rotation.py`
- Create: `src/multiclaw/storage/repositories/secrets.py`
- Modify: `src/multiclaw/storage/uow.py`
- Modify: `src/multiclaw/config/settings.py`
- Modify: `src/multiclaw/llm/router.py`
- Modify: `src/multiclaw/mcp/manager.py`
- Modify: `src/multiclaw/runtime/factory.py`
- Create: `tests/test_secret_envelope.py`
- Create: `tests/test_secret_resolver.py`
- Create: `tests/test_secret_rotation.py`
- Create: `tests/vectors/secret_envelope_v1.json`
- Modify: `tests/test_llm.py`
- Modify: `tests/test_mcp_integration.py`

- [ ] **Step 1: Write failing keyring, fixed-vector, row-swap, and fallback tests**

```python
def test_fixed_envelope_vector_is_stable():
    vector = json.loads(Path("tests/vectors/secret_envelope_v1.json").read_text())
    fields = EnvelopeFields(
        tenant_id="tenant-a",
        workspace_id=None,
        secret_id="secret-a",
        provider_kind="llm",
        provider_name="openai",
        secret_name="api_key",
        key_provider_name="deployment-keyring",
        key_version=3,
        format_version=1,
        algorithm="AES-256-GCM",
    )
    aad = build_aad(fields)
    assert aad.hex() == vector["aad_hex"]
    ciphertext = AESGCM(base64.b64decode(vector["key_b64"])).encrypt(
        bytes.fromhex(vector["nonce_hex"]),
        bytes.fromhex(vector["plaintext_hex"]),
        aad,
    )
    assert ciphertext.hex() == vector["ciphertext_with_tag_hex"]


def test_ciphertext_cannot_be_moved_to_another_secret_row(envelope, fields, monkeypatch):
    monkeypatch.setattr(os, "urandom", lambda length: b"\x01" * length)
    encrypted = envelope.encrypt(fields, b"secret-value")
    moved = replace(fields, secret_id="secret-b")
    with pytest.raises(InvalidTag):
        envelope.decrypt(moved, encrypted)


@pytest.mark.asyncio
async def test_broken_user_secret_never_falls_back_to_platform(resolver, calls, context):
    await store_invalid_ciphertext(context, provider="openai", name="api_key")
    with pytest.raises(UserSecretInvalidError):
        await resolver.resolve(context, "openai", "api_key")
    assert calls.platform_fallback == 0


@pytest.mark.asyncio
async def test_platform_fallback_requires_absent_user_secret_and_deployment_opt_in(
    resolver_with_fallback, context
):
    value = await resolver_with_fallback.resolve(context, "openai", "api_key")
    assert value.source == "platform"
```

Add tests for both/neither keyring source, base64/JSON validation, non-32-byte keys, unknown provider, file mode group/world readable, missing active/used key version, 12-byte nonce, 16-byte tag, nonce uniqueness collision, masked metadata output, and Secret canaries absent from exception strings.

Create `tests/vectors/secret_envelope_v1.json` with the reviewed non-secret interoperability vector:

```json
{
  "key_b64": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
  "nonce_hex": "000102030405060708090a0b",
  "plaintext_hex": "746573742d7365637265742d76616c7565",
  "aad_hex": "6d756c7469636c61772e7365637265742d656e76656c6f70652e7631000000000874656e616e742d61ffffffff000000087365637265742d61000000036c6c6d000000066f70656e6169000000076170695f6b6579000000126465706c6f796d656e742d6b657972696e67000000013300000001310000000b4145532d3235362d47434d",
  "ciphertext_with_tag_hex": "3367a56fe896a778ff24e3a6c7881418e6fefe9b30454bf5716530532311188e4f"
}
```

- [ ] **Step 2: Run Secret tests and observe missing provider/envelope failures**

```bash
uv run pytest tests/test_secret_envelope.py tests/test_secret_resolver.py \
  tests/test_secret_rotation.py \
  tests/test_llm.py tests/test_mcp_integration.py -q
```

Expected: FAIL because the keyring, envelope, and per-call credential resolver do not exist.

- [ ] **Step 3: Implement the only v1 key provider and XOR loading gate**

`DeploymentKeyring.load` reads exactly one of `MULTICLAW_SECRETS_KEYRING_B64` or `secrets.keyring_file`:

```python
@dataclass(frozen=True, slots=True)
class DeploymentKeyring:
    active_key_version: int
    keys: Mapping[int, bytes]
    provider_name: Literal["deployment-keyring"] = "deployment-keyring"

    @classmethod
    def load(cls, settings: SecretSettings, environ: Mapping[str, str] = os.environ):
        encoded = environ.get("MULTICLAW_SECRETS_KEYRING_B64", "")
        path = settings.keyring_file
        if bool(encoded) == bool(path):
            raise KeyringConfigurationError("configure exactly one keyring source")
        if path:
            mode = Path(path).stat().st_mode
            if os.name == "posix" and mode & (stat.S_IRGRP | stat.S_IROTH):
                raise KeyringPermissionError(path)
            payload = Path(path).read_bytes()
        else:
            payload = base64.b64decode(encoded, validate=True)
        raw = json.loads(payload)
        keys = {int(version): base64.b64decode(value, validate=True) for version, value in raw["keys"].items()}
        if any(len(value) != 32 for value in keys.values()):
            raise KeyringConfigurationError("every key must decode to 32 bytes")
        active = int(raw["active_key_version"])
        if active not in keys:
            raise KeyringConfigurationError("active key version is missing")
        return cls(active_key_version=active, keys=MappingProxyType(keys))
```

File content is the fixed JSON object from design Section 8.4; the environment value is base64 of those JSON bytes. Query distinct stored `key_version` values during readiness and reject removal while references remain.

- [ ] **Step 4: Implement fixed binary AAD and AESGCM envelope**

```python
DOMAIN = b"multiclaw.secret-envelope.v1\0"
NULL_LENGTH = (0xFFFFFFFF).to_bytes(4, "big")


def encode_field(value: str | None) -> bytes:
    if value is None:
        return NULL_LENGTH
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(4, "big") + encoded


def build_aad(fields: EnvelopeFields) -> bytes:
    ordered = (
        fields.tenant_id,
        fields.workspace_id,
        fields.secret_id,
        fields.provider_kind,
        fields.provider_name,
        fields.secret_name,
        fields.key_provider_name,
        str(fields.key_version),
        str(fields.format_version),
        fields.algorithm,
    )
    return DOMAIN + b"".join(encode_field(value) for value in ordered)


def encrypt(self, fields: EnvelopeFields, plaintext: bytes) -> EncryptedSecret:
    nonce = os.urandom(12)
    ciphertext = AESGCM(self.keyring.keys[fields.key_version]).encrypt(
        nonce,
        plaintext,
        build_aad(fields),
    )
    return EncryptedSecret(nonce=nonce, ciphertext=ciphertext)
```

`AESGCM.encrypt` output contains the trailing 16-byte tag. Enforce provider `deployment-keyring`, format `1`, algorithm `AES-256-GCM`, 12-byte nonce, ciphertext at least 16 bytes, and full row-field AAD before decrypting.

- [ ] **Step 5: Implement scoped Secret repository and resolver state machine**

Repository writes create the `secret_id` before encryption, write user-level rows with `workspace_id=NULL`, and return metadata only. Resolver logic must be structurally exhaustive:

```python
class SecretBytes:
    def __init__(self, value: bytes) -> None:
        self._value = bytearray(value)

    def reveal(self) -> bytes:
        return bytes(self._value)

    def clear(self) -> None:
        for index in range(len(self._value)):
            self._value[index] = 0

    def __enter__(self) -> "SecretBytes":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.clear()


async def resolve(self, context, provider, name):
    row = await self.repository.get_metadata_and_ciphertext(context, provider, name)
    if row is not None:
        try:
            plaintext = self.envelope.decrypt(EnvelopeFields.from_row(row), row.ciphertext)
        except Exception as exc:
            raise UserSecretInvalidError(provider, name) from exc
        return ResolvedSecret(value=SecretBytes(plaintext), source="user")
    if self.settings.allow_platform_fallback:
        platform = self.platform_provider.get(provider, name)
        if platform is not None:
            return ResolvedSecret(value=platform, source="platform")
    raise SecretNotConfiguredError(provider, name)
```

Callers use `with resolved.value as secret:` and pass `secret.reveal()` only to the immediate provider invocation; `finally` clears the mutable buffer. Do not catch upstream authentication failures and retry with platform credentials; once `source="user"`, the call outcome remains tied to that source.

- [ ] **Step 6: Re-encrypt old key versions in idempotent batches**

Create `SecretRotationService.rotate_batch(limit=100)`. It selects scoped rows whose `key_version != active_key_version`, decrypts with the old key/AAD, constructs new AAD with the active version, generates a new nonce, and updates ciphertext/key version/nonce/`rotated_at` through a version or `updated_at` CAS. Each row commits independently or in a bounded batch so a crash can safely resume.

```python
async def rotate_batch(self, limit: int = 100) -> RotationResult:
    rows = await self.repository.list_for_rotation(
        active_key_version=self.keyring.active_key_version,
        limit=limit,
    )
    rotated = 0
    for row in rows:
        plaintext = self.envelope.decrypt(EnvelopeFields.from_row(row), row.ciphertext)
        target = replace(
            EnvelopeFields.from_row(row),
            key_version=self.keyring.active_key_version,
        )
        encrypted = self.envelope.encrypt(target, plaintext)
        rotated += await self.repository.cas_rotate(
            row=row,
            target=target,
            encrypted=encrypted,
        )
    return RotationResult(scanned=len(rows), rotated=rotated)
```

`tests/test_secret_rotation.py` proves crash/retry convergence, row readability before/after rotation, new nonces, old-version reference count reaching zero, and readiness failure if a referenced old version is removed too early. No API endpoint exposes key rotation; it is an operator/background service.

- [ ] **Step 7: Resolve LLM and MCP credentials per invocation**

Stop constructing provider adapters with plaintext startup settings. `ModelRouter.completion` and `stream_completion` accept a `ResolvedCredentials` argument and create/cache only non-secret adapter configuration. MCP environment/header Secret references resolve within the current tenant call and are cleared when the invocation/runtime ends. Assertions must prove resolved plaintext is not retained in router/manager attributes.

- [ ] **Step 8: Run encryption and integration regressions**

```bash
uv run pytest tests/test_secret_envelope.py tests/test_secret_resolver.py \
  tests/test_secret_rotation.py \
  tests/test_llm.py tests/test_mcp_integration.py tests/test_runtime_isolation.py -q
```

Expected: fixed vector and tamper tests PASS, all strict fallback branches are covered, and two runtimes cannot observe or retain each other's credentials.

- [ ] **Step 9: Commit the BYOK boundary**

```bash
git add src/multiclaw/secrets src/multiclaw/storage/repositories/secrets.py \
  src/multiclaw/storage/uow.py src/multiclaw/config/settings.py \
  src/multiclaw/llm/router.py src/multiclaw/mcp/manager.py \
  src/multiclaw/runtime/factory.py tests/test_secret_envelope.py \
  tests/test_secret_resolver.py tests/vectors/secret_envelope_v1.json \
  tests/test_secret_rotation.py tests/test_llm.py tests/test_mcp_integration.py
git commit -m "Keep user credentials decryptable only inside their own call" \
  -m "Add the fixed deployment-keyring AESGCM envelope, scoped Secret repository, and strict user-secret/platform-fallback state machine." \
  -m "Constraint: v1 accepts only deployment-keyring and length-prefixed AAD" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Directive: Provider authentication failures must never trigger platform fallback" \
  -m "Tested: Secret envelope, resolver, rotation, LLM, MCP, and runtime isolation suites"
```

### Task 14: Replace legacy auth storage with digest codes, auth epochs, and CSRF

**Files:**
- Modify: `src/multiclaw/auth/models.py`
- Modify: `src/multiclaw/auth/middleware.py`
- Modify: `src/multiclaw/auth/router.py`
- Create: `src/multiclaw/auth/cleanup.py`
- Delete: `src/multiclaw/auth/store.py`
- Modify: `src/multiclaw/storage/repositories/auth.py`
- Create: `src/multiclaw/security/csrf.py`
- Create: `src/multiclaw/security/__init__.py`
- Modify: `src/multiclaw/server.py`
- Modify: `tests/test_auth_tenant_boundary.py`
- Create: `tests/test_csrf.py`
- Modify: `tests/test_auth_email_sender.py`
- Modify: `tests/test_auth_route_aliases.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing digest, epoch-revocation, purpose, and CSRF tests**

```python
@pytest.mark.asyncio
async def test_verification_code_table_never_contains_plaintext(database, auth_service):
    await auth_service.send_code("user@example.com", purpose="login", forced_code="654321")
    async with database.connect() as conn:
        row = (await conn.execute(select(verification_codes))).mappings().one()
    assert row["code_digest"] != "654321"
    assert "code" not in row
    assert row["purpose"] == "login"


def test_incremented_auth_epoch_revokes_existing_cookie(client, active_user):
    cookie = login_cookie(client, active_user)
    increment_auth_epoch(client.app, active_user.id)
    assert client.get("/api/sessions", cookies=cookie).status_code == 401


def test_recovery_token_cannot_access_normal_api(client, pending_purge_user):
    token = issue_recovery_token(client, pending_purge_user)
    assert client.get("/api/sessions", cookies={"recovery_token": token}).status_code in {401, 403}
    assert client.get("/api/account/deletion", cookies={"recovery_token": token}).status_code == 200


@pytest.mark.parametrize("missing", ["origin", "csrf_cookie", "csrf_header"])
def test_state_change_requires_origin_and_matching_csrf(client, logged_in_user, missing):
    request = valid_csrf_request(logged_in_user).without(missing)
    assert client.post("/api/sessions", **request).status_code == 403
```

Add tests for wrong-purpose codes, atomic one-time use, rate limiting, current account status, JWT audience/iat/exp/epoch, secure cookie flags in production, constant-time CSRF comparison, untrusted Origin/Referer, and CORS credential wildcard rejection.

Add an async cleanup test that inserts expired and live login/recovery codes, runs `AuthCleanupWorker.run_once()`, and asserts only `expires_at <= db_now_ms()` rows are deleted.

- [ ] **Step 2: Run auth/security tests and observe legacy failures**

```bash
uv run pytest tests/test_auth_tenant_boundary.py tests/test_csrf.py \
  tests/test_auth_email_sender.py tests/test_auth_route_aliases.py tests/test_server.py -q
```

Expected: FAIL because codes/JWT secret are stored in legacy SQLite tables, JWT lacks `auth_epoch`, and state-changing APIs have no CSRF validation.

- [ ] **Step 3: Load the JWT deployment Secret and derive a code-digest key**

Load exactly one configured JWT source (`MULTICLAW_AUTH_JWT_SIGNING_KEY` or `auth.jwt_signing_key_file`), require at least 32 random bytes, and never persist or log it. Derive an HMAC key with domain separation:

```python
def verification_digest(signing_key: bytes, purpose: str, email: str, code: str) -> str:
    digest_key = hmac.new(
        signing_key,
        b"multiclaw.verification-code-key.v1",
        hashlib.sha256,
    ).digest()
    message = b"\0".join((purpose.encode(), email.lower().encode(), code.encode()))
    return hmac.new(digest_key, message, hashlib.sha256).hexdigest()
```

Compare with `hmac.compare_digest`. `verification_codes` rows store only this digest, purpose, expiry, used timestamp, and creation time. Perform find/compare/mark-used in one `AuthUnitOfWork` and use DB clock for expiry.

Even mock email mode must not log or return the six-digit code. Tests inject the forced code directly into the auth service and assert it has zero matches in captured logs/responses; developer convenience cannot bypass the Secret redactor.

- [ ] **Step 4: Issue audience-specific JWTs and validate current database state**

Normal session claims:

```python
{
    "sub": user.id,
    "email": user.email,
    "auth_epoch": user.auth_epoch,
    "aud": "multiclaw-api",
    "iat": now,
    "exp": now + timedelta(days=10),
}
```

Define `DELETION_RECOVERY_TOKEN_TTL_SECONDS = 600`. Deletion recovery claims use `aud="multiclaw-deletion-recovery"`, `purpose="deletion_recovery"`, `job_id`, and that 10-minute expiry; they are stored in a separate cookie or submitted as Bearer token only to account deletion status/recovery endpoints. Middleware decodes only the normal audience and then loads the user from the shared deployment DB, requiring `status='active'` and equal epoch. Pending-purge handling is added in Task 15.

Define `RECENT_AUTH_MAX_AGE_SECONDS = 300`. The typed auth dependency retains the verified JWT `iat`; Secret mutation/test requires `db_now_ms()/1000 - iat <= 300`. Account deletion always requires a fresh email-code verification, which issues a new normal JWT before the deletion request. Tests cover the exact 300-second boundary and reject application-clock skew by comparing against DB time.

- [ ] **Step 5: Add Origin and double-submit CSRF validation**

```python
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def require_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin") or origin_from_referer(request.headers.get("referer"))
    if origin not in request.app.state.allowed_origins:
        raise HTTPException(403, "CSRF origin rejected")
    cookie = request.cookies.get("csrf_token", "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(403, "CSRF token rejected")
```

`GET /auth/csrf` is public and creates a random 32-byte URL-safe value, sets a Secure/SameSite cookie that JavaScript may read, and returns the same token. Normal auth cookie remains HttpOnly. Apply both Origin/Referer and matching CSRF token checks to pre-login send/verify, logout, approvals, Secrets, account deletion/recovery, session mutations, and chat POST; rotate the CSRF token after verify and logout.

- [ ] **Step 6: Delete AuthStore and remove the SQLite side channel**

Switch all auth router calls to `AuthUnitOfWork` and delete `src/multiclaw/auth/store.py`, its `auth_config` table creation, session-column migration, and generated JWT secret behavior. MySQL deployments must contain no SQLite auth connection or file. Add a test that scans application startup state for only one `Database` engine.

Create `AuthCleanupWorker.run_once()` using `verification_codes.delete().where(verification_codes.c.expires_at <= dialect.db_now_ms())`. Lifespan starts one cancellable periodic loop after readiness succeeds and awaits it during shutdown; the worker owns no separate engine and opens `AuthUnitOfWork` for each bounded pass.

- [ ] **Step 7: Run auth, CSRF, and server regressions**

```bash
uv run pytest tests/test_auth_tenant_boundary.py tests/test_csrf.py \
  tests/test_auth_email_sender.py tests/test_auth_route_aliases.py \
  tests/test_server.py tests/test_tenant_uow.py -q
```

Expected: plaintext code/JWT storage is absent, old cookies fail after epoch change, recovery tokens are scope-limited, all state mutations enforce CSRF, and the auth side database is gone.

- [ ] **Step 8: Commit the authentication boundary**

```bash
git add src/multiclaw/auth src/multiclaw/security src/multiclaw/storage/repositories/auth.py \
  src/multiclaw/server.py tests/test_auth_tenant_boundary.py tests/test_csrf.py \
  tests/test_auth_email_sender.py tests/test_auth_route_aliases.py tests/test_server.py
git commit -m "Make account revocation immediate and authentication storage backend-neutral" \
  -m "Move auth into the shared UoW, store only purpose-bound code digests, validate auth epochs, and enforce Origin plus CSRF on state changes." \
  -m "Rejected: JWT-only account state | deletion and disablement would leave old sessions valid" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Directive: Recovery tokens are never accepted by normal API dependencies" \
  -m "Tested: auth boundary, CSRF, email, route alias, server, and UoW suites"
```

### Task 15: Implement recoverable delayed account deletion and idempotent purge

**Files:**
- Create: `src/multiclaw/deletion/__init__.py`
- Create: `src/multiclaw/deletion/service.py`
- Create: `src/multiclaw/deletion/worker.py`
- Create: `src/multiclaw/storage/repositories/deletions.py`
- Modify: `src/multiclaw/storage/uow.py`
- Create: `src/multiclaw/api/account.py`
- Modify: `src/multiclaw/auth/router.py`
- Modify: `src/multiclaw/auth/middleware.py`
- Modify: `src/multiclaw/runtime/pool.py`
- Modify: `src/multiclaw/server.py`
- Create: `tests/test_deletion_service.py`
- Create: `tests/test_deletion_worker.py`
- Modify: `tests/test_auth_tenant_boundary.py`
- Modify: `tests/test_workspace_resolver.py`

- [ ] **Step 1: Write failing request/recovery race and purge-order tests**

```python
@pytest.mark.asyncio
async def test_deletion_request_disables_account_and_revokes_runtime(app, active_user):
    status = await deletion_service(app).request(active_user.context)
    assert status.status == "scheduled"
    assert status.purge_after == status.requested_at + app.state.settings.deletion.retention_days * 86400000
    assert await user_status(app, active_user.id) == "pending_purge"
    assert await auth_epoch(app, active_user.id) == active_user.auth_epoch + 1
    assert await app.state.runtime_pool.peek(active_user.id) is None


@pytest.mark.asyncio
async def test_recovery_and_worker_claim_cannot_both_win(database, pending_job):
    recovered, claimed = await asyncio.gather(
        recover_job(database, pending_job),
        claim_job(database, pending_job),
        return_exceptions=True,
    )
    assert sum(value is True for value in (recovered, claimed)) == 1


@pytest.mark.asyncio
async def test_purge_deletes_leaf_to_root_without_cascade(database, populated_tenant):
    worker = DeletionWorker(database, populated_tenant.workspace_resolver)
    await worker.purge_due_jobs()
    assert await tenant_row_counts(database, populated_tenant.id) == {
        table: 0 for table in TENANT_PURGE_TABLES
    }
```

Add tests for recent reauthentication, active runs returning `409`, duplicate request idempotency, retention `0` asynchronous eligibility, recovery strictly before `purge_after`, no recovery after job `running`, file-missing retry, filesystem deletion failure leaving all DB rows intact, DB rollback, crash after DB commit, stale worker lease takeover, and verification-code cleanup by retained email.

Patch application wall time in both directions during request/recovery/claim tests and assert `purge_after` eligibility remains controlled exclusively by the database clock.

- [ ] **Step 2: Run deletion tests and observe missing state transitions**

```bash
uv run pytest tests/test_deletion_service.py tests/test_deletion_worker.py \
  tests/test_auth_tenant_boundary.py tests/test_workspace_resolver.py -q
```

Expected: FAIL because there is no deletion service/worker and account state does not drive runtime revocation.

- [ ] **Step 3: Implement request and recovery as locked DB-clock transitions**

Deletion request locks the user row, rejects replaying/executing tools or a valid run lease, expires waiting approvals, inserts one scheduled job, sets `pending_purge`, computes `purge_after` from DB time, increments `auth_epoch`, then revokes runtime after commit.

```python
async def recover(self, tenant_id: str, job_id: str) -> None:
    async with DeletionUnitOfWork(self.database, tenant_id) as uow:
        job = await uow.deletions.lock_job(job_id)
        if (
            job.status != "scheduled"
            or await uow.users.db_now_ms() >= job.purge_after
        ):
            raise RecoveryWindowClosedError(job_id)
        await uow.users.restore_pending_user(expected_job_version=job.version)
```

Restore clears purge fields, deletes the scheduled job, sets `active`, increments epoch again, and requires a new normal login. The recovery token can query status and invoke this method only.

- [ ] **Step 4: Implement worker claim, heartbeat, and idempotent filesystem phase**

Claim only `scheduled AND purge_after <= db_now_ms()` via version CAS, set `running`, worker ID, lease, heartbeat, increment fence/attempt. Revoke runtime, confirm no valid run lease, calculate every directory from server IDs through `WorkspaceResolver`, and treat an already-missing directory as success. Never pass a database or client path to recursive deletion.

Lifespan owns one cancellable standalone deletion-worker loop and awaits it at shutdown. Unit/integration tests invoke `purge_due_jobs()` directly; production polling uses bounded batches and cancellation-aware event waiting, not an uninterruptible sleep.

- [ ] **Step 5: Implement the exact leaf-to-root database transaction**

Within one write UoW retain the email locally, null the default workspace for `pending_purge`, then delete in this order:

```python
PURGE_ORDER = (
    execution_checkpoints,
    audit_logs,
    tool_executions,
    approval_requests,
    agent_runs,
    memory_entries,
    chat_sessions,
    user_secrets,
    workspaces,
)

for table in PURGE_ORDER:
    await conn.execute(table.delete().where(table.c.tenant_id == tenant_id))
await conn.execute(
    verification_codes.delete().where(
        verification_codes.c.email == retained_email,
        verification_codes.c.expires_at > dialect.db_now_ms(),
    )
)
await conn.execute(deletion_jobs.delete().where(deletion_jobs.c.tenant_id == tenant_id))
await conn.execute(users.delete().where(users.c.id == tenant_id))
```

If user/job are absent on retry after an acknowledgement crash, report completed. A long-running job can be reclaimed only after its DB-clock lease expires and resumes from the idempotent file phase.

- [ ] **Step 6: Wire account endpoints and pending-purge middleware**

Add request/status/recover routes from the approved design. Normal middleware returns `403` for `pending_purge`; recovery authentication bypasses only status and recover routes. Deletion request requires a recently verified login-code timestamp and CSRF. `retention_days=0` returns scheduled status and lets the background worker claim after commit; it never recursively deletes in the HTTP handler.

- [ ] **Step 7: Run deletion contracts on both backends**

```bash
uv run pytest tests/test_deletion_service.py tests/test_deletion_worker.py \
  tests/test_auth_tenant_boundary.py tests/test_workspace_resolver.py -q
MULTICLAW_TEST_MYSQL_URL="$MULTICLAW_TEST_MYSQL_URL" \
  uv run pytest tests/test_deletion_service.py tests/test_deletion_worker.py -q
```

Expected: request/recovery/claim races have one winner, purge succeeds without cascades on both backends, retries converge, and old JWT/runtime access is zero.

- [ ] **Step 8: Commit deletion lifecycle**

```bash
git add src/multiclaw/deletion src/multiclaw/storage/repositories/deletions.py \
  src/multiclaw/storage/uow.py src/multiclaw/api/account.py src/multiclaw/auth \
  src/multiclaw/runtime/pool.py src/multiclaw/server.py \
  tests/test_deletion_service.py tests/test_deletion_worker.py \
  tests/test_auth_tenant_boundary.py tests/test_workspace_resolver.py
git commit -m "Let users reverse account deletion until physical purge begins" \
  -m "Add immediate access revocation, bounded recovery, DB-clock worker claims, and explicit idempotent file/database purge ordering." \
  -m "Constraint: retention is deployment-owned 0..30 days and purge is always asynchronous" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Directive: Do not replace explicit purge order with cascade deletion" \
  -m "Tested: deletion service, worker, auth boundary, and workspace tests on SQLite/MySQL"
```

### Task 16: Complete the scoped API, readiness, redaction, and observability gates

**Files:**
- Create: `src/multiclaw/api/sessions.py`
- Modify: `src/multiclaw/api/chat.py`
- Modify: `src/multiclaw/api/approvals.py`
- Create: `src/multiclaw/api/secrets.py`
- Modify: `src/multiclaw/api/account.py`
- Create: `src/multiclaw/api/health.py`
- Modify: `src/multiclaw/api/dependencies.py`
- Create: `src/multiclaw/security/redaction.py`
- Create: `src/multiclaw/observability.py`
- Modify: `src/multiclaw/governance/audit.py`
- Modify: `src/multiclaw/governance/models.py`
- Modify: `src/multiclaw/server.py`
- Create: `tests/test_tenant_api.py`
- Create: `tests/test_readiness.py`
- Create: `tests/test_secret_redaction.py`
- Modify: `tests/test_request_logging.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing endpoint, readiness, and canary-redaction tests**

```python
def test_foreign_and_missing_resources_are_indistinguishable(client, two_users):
    foreign = client.get(
        f"/api/sessions/{two_users.b.session_id}/messages",
        cookies=two_users.a.cookie,
    )
    missing = client.get(
        "/api/sessions/00000000-0000-0000-0000-000000000000/messages",
        cookies=two_users.a.cookie,
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_api_never_runs_migrations_when_revision_is_behind(app_factory, database_url):
    app = app_factory(database_url=database_url, revision="base")
    with TestClient(app) as client:
        response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert migration_upgrade_call_count(app) == 0


def test_secret_canary_is_absent_from_all_outputs(app, caplog, secret_canary):
    exercise_secret_failure_paths(app, secret_canary)
    assert secret_canary not in caplog.text
    assert secret_canary not in collected_sse(app)
    assert secret_canary not in collected_checkpoints(app)
    assert secret_canary not in collected_audit_details(app)
    assert secret_canary not in collected_trace_events(app)
```

Add endpoint matrix tests for all approved routes/status codes, Secret metadata-only responses, recent-reauth enforcement on Secret mutation/test and deletion, strict tenant scope, approval version conflicts, session creation only without ID, `429` tenant run quota, `503` runtime capacity with `Retry-After`, and liveness independence from DB failure.

Add `test_application_exposes_no_superadmin_or_break_glass_route`, which inspects `app.routes`, requires no application route containing `/admin`, `/superadmin`, or `/break-glass`, and proves ordinary authentication cannot enumerate another tenant through any service dependency.

- [ ] **Step 2: Run API/readiness/redaction tests and observe incomplete gates**

```bash
uv run pytest tests/test_tenant_api.py tests/test_readiness.py \
  tests/test_secret_redaction.py tests/test_request_logging.py tests/test_server.py -q
```

Expected: FAIL because routes remain partly inline, readiness covers only sandbox state, and redaction is local to server error helpers.

- [ ] **Step 3: Centralize recursive Secret redaction**

Create one redactor used by logs, SSE errors, audit detail, checkpoints, and trace attributes:

```python
SECRET_KEYS = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if SECRET_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return "[BINARY REDACTED]"
    if isinstance(value, str):
        return redact_credential_patterns(redact_private_paths(value))
    return value
```

Exceptions exposed to clients map to typed public error codes/messages; never serialize `str(exc)` for Secret, database, MCP, or filesystem failures.

Replace `InMemoryAuditLogger` in production runtime assembly with `ScopedAuditLogger`. Its `record(workflow_repository, context, event_type, status, tool_name, detail)` receives the caller's current UoW-bound repository, applies `redact(detail)`, and inserts `audit_logs` with available session/run/approval/execution IDs in the same transaction. It never opens or commits a connection. Test-only in-memory audit remains available only to isolated scheduler unit tests; runtime factory must never select it.

- [ ] **Step 4: Split business endpoints out of `server.py`**

`server.py` must be limited to logging setup, `create_app`, lifespan/shared infrastructure, middleware, router inclusion, and static assets. Each router depends on typed services/UoWs and contains no SQL or runtime factory construction.

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="MultiClaw", lifespan=build_lifespan(settings))
    app.add_middleware(AuthMiddleware)
    app.include_router(auth_router)
    app.include_router(auth_router, prefix="/api")
    app.include_router(session_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(approval_router, prefix="/api")
    app.include_router(secret_router, prefix="/api")
    app.include_router(account_router, prefix="/api")
    app.include_router(health_router, prefix="/api")
    return app


app = create_app()
```

Keep compatibility auth aliases only where existing frontend/tests require them. Replace `/api/approve` with the approved `/api/approvals/{id}/decision`; if one release of aliasing is necessary internally, route it through the same scoped service and mark it private/deprecated without different behavior.

- [ ] **Step 5: Implement fail-closed readiness and independent liveness**

Readiness checks, in order, are database connectivity/backend version, Alembic head, SQLite FK pragmas/MySQL InnoDB+UTC+isolation, schema/FK integrity, active default workspaces, keyring XOR/provider/used versions, and workspace root permissions. `active + NULL/invalid default_workspace_id` fails; `pending_purge + NULL default_workspace_id` is valid. Return a redacted low-cardinality list of failed check names, never DSNs/paths/IDs.

```python
@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    result = await request.app.state.readiness.check()
    return JSONResponse(
        redact(result.public_dict()),
        status_code=200 if result.ready else 503,
    )
```

Do not make liveness query DB/keyring/workspace dependencies.

Expose only `/api/health/live` and `/api/health/ready` as unauthenticated health routes in middleware; their payloads stay redacted. Remove or compatibility-redirect the old `/health/ready` route so there is one readiness implementation.

- [ ] **Step 6: Add bounded observability without tenant IDs in metric labels**

Implement `OperationalMetrics` with an allowlist:

```python
ALLOWED_METRIC_LABELS = {
    "backend",
    "profile",
    "operation",
    "status",
    "error_class",
    "recovery_strategy",
}
FORBIDDEN_METRIC_LABELS = {
    "tenant_id", "workspace_id", "session_id", "run_id", "request_id",
    "email", "provider_name", "path",
}


def increment(self, name: str, **labels: str) -> None:
    if set(labels) - ALLOWED_METRIC_LABELS or set(labels) & FORBIDDEN_METRIC_LABELS:
        raise InvalidMetricLabelError(set(labels))
    self._counters[(name, tuple(sorted(labels.items())))] += 1
```

Structured logs/trace events may include opaque scope IDs only after redaction and must never include email, Secret values, or raw paths. Instrument scope-FK rejection, stale fence, uncertain/blocked recovery, runtime capacity, approval recovery, purge retry, migration revision, keyring failure, SQLite busy, and MySQL lock timeout.

- [ ] **Step 7: Run API and security boundary verification**

```bash
uv run pytest tests/test_tenant_api.py tests/test_readiness.py \
  tests/test_secret_redaction.py tests/test_request_logging.py tests/test_server.py \
  tests/test_csrf.py tests/test_auth_tenant_boundary.py -q
```

Expected: route matrix, `404` non-enumeration, no automatic migration, liveness/readiness split, label rejection, and Secret canary-zero assertions PASS.

- [ ] **Step 8: Commit the external boundary**

```bash
git add src/multiclaw/api src/multiclaw/security/redaction.py \
  src/multiclaw/observability.py src/multiclaw/governance src/multiclaw/server.py \
  tests/test_tenant_api.py tests/test_readiness.py tests/test_secret_redaction.py \
  tests/test_request_logging.py tests/test_server.py
git commit -m "Expose tenant behavior only through scoped and fail-closed interfaces" \
  -m "Split API services, enforce indistinguishable resource lookup, validate readiness invariants, and centralize redacted low-cardinality observability." \
  -m "Constraint: API validates schema head and never upgrades it" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Tested: tenant API, readiness, redaction, request logging, server, CSRF, and auth suites"
```

### Task 17: Add run-aware approval, Secret, and deletion frontend flows

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Create: `frontend/src/lib/security.ts`
- Modify: `frontend/src/lib/auth-context-store.ts`
- Modify: `frontend/src/lib/auth-context.tsx`
- Modify: `frontend/src/lib/chat-store.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/layout/AppLayout.tsx`
- Modify: `frontend/src/components/approval/ApprovalToolUI.tsx`
- Modify: `frontend/src/components/session/SessionProvider.tsx`
- Create: `frontend/src/components/settings/SettingsPanel.tsx`
- Create: `frontend/src/components/settings/SecretSettings.tsx`
- Create: `frontend/src/components/settings/DeletionSettings.tsx`
- Modify: `frontend/src/index.css`
- Modify: `tests/test_frontend_debug.py`
- Modify: `tests/test_frontend_welcome.py`

- [ ] **Step 1: Add failing static contract tests for frontend security/state**

Because no frontend test runner is configured, extend the existing source-inspection tests to require:

```python
def test_frontend_api_attaches_csrf_to_mutations():
    api = Path("frontend/src/lib/api.ts").read_text()
    security = Path("frontend/src/lib/security.ts").read_text()
    assert '"X-CSRF-Token"' in api
    assert "ensureCsrfToken" in security


def test_frontend_never_models_secret_plaintext_in_persistent_state():
    sources = "\n".join(
        path.read_text()
        for path in Path("frontend/src").rglob("*.ts*")
    )
    assert "secretPlaintext:" not in sources
    assert "localStorage.setItem" not in Path("frontend/src/components/settings/SecretSettings.tsx").read_text()


def test_approval_api_uses_scoped_persisted_endpoint():
    api = Path("frontend/src/lib/api.ts").read_text()
    assert "/approvals/${approvalId}/decision" in api
    assert "version" in api
```

- [ ] **Step 2: Run frontend contract/lint and observe missing UI/API behavior**

```bash
uv run pytest tests/test_frontend_debug.py tests/test_frontend_welcome.py -q
cd frontend && npm run lint
```

Expected: source-contract tests FAIL because CSRF/settings/persisted approvals are absent; existing lint remains the baseline.

- [ ] **Step 3: Make the API client CSRF-aware with typed HTTP errors**

```typescript
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly retryAfter?: number,
  ) {
    super(message);
  }

  static async fromResponse(response: Response): Promise<ApiError> {
    const body = await response.json().catch(() => ({ code: "REQUEST_FAILED", detail: "Request failed" }));
    const retryAfter = Number(response.headers.get("Retry-After"));
    return new ApiError(
      response.status,
      body.code ?? "REQUEST_FAILED",
      body.detail ?? `HTTP ${response.status}`,
      Number.isFinite(retryAfter) ? retryAfter : undefined,
    );
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = options.method ?? "GET";
  const csrfToken = isMutation(method) ? await ensureCsrfToken() : undefined;
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) throw await ApiError.fromResponse(response);
  return response.json() as Promise<T>;
}
```

`ensureCsrfToken()` fetches `/auth/csrf` once per session, reads the returned token, keeps it only in module memory, and refreshes after login/logout or a `403 CSRF` response. Do not persist tokens or Secret values in local/session storage.

- [ ] **Step 4: Add typed Secret and account APIs**

```typescript
export interface SecretMetadata {
  providerKind: string;
  providerName: string;
  secretName: string;
  maskedValue: string;
  updatedAt: number;
}

export const secretApi = {
  list: () => request<SecretMetadata[]>("/secrets"),
  put: (provider: string, name: string, value: string) =>
    request<SecretMetadata>(`/secrets/${provider}/${name}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),
  remove: (provider: string, name: string) =>
    request<void>(`/secrets/${provider}/${name}`, { method: "DELETE" }),
  test: (provider: string, name: string) =>
    request<{ ok: boolean }>(`/secrets/${provider}/${name}/test`, { method: "POST" }),
};
```

Add deletion request/status/recover types and approval decision `{ approved, version }`. API responses never include ciphertext, key version, nonce, or plaintext.

- [ ] **Step 5: Implement settings and recovery UX without workspace switching**

`SettingsPanel` opens from the account footer. `SecretSettings` uses a transient password input, clears it in `finally`, displays masked metadata, and requires recent reauthentication for mutation. `DeletionSettings` explains the configured purge date, requires an email-code confirmation, disables ordinary navigation after `pending_purge`, and offers recovery only while server status says `scheduled` and before `purge_after`.

`ApprovalToolUI` submits persisted approval ID/version, handles `409` by refetching, renders expired `410`, and survives reload by querying status. `SessionProvider` resets server-derived session/run state on login/logout/account-state changes; it never adds tenant/workspace selectors.

- [ ] **Step 6: Parse the first run control event and retain exact run scope**

Update chat request/stream handling so `data-run` captures both IDs and ignores any later scoped event whose `session_id/run_id` does not match the active stream. The client check is defense in depth; backend routing remains authoritative.

- [ ] **Step 7: Run frontend static checks and production build**

```bash
uv run pytest tests/test_frontend_debug.py tests/test_frontend_welcome.py -q
cd frontend && npm run lint
cd frontend && npm run build
```

Expected: source contracts PASS, ESLint reports zero errors, TypeScript/Vite build succeeds, and generated assets are written only by Vite into `src/multiclaw/static/`.

- [ ] **Step 8: Perform and record manual browser verification**

Using two browser profiles and one backend instance, verify:

1. each user sees only their sessions and live run events;
2. approval can be left pending, backend restarted, page reloaded, and then decided;
3. Secret list is masked, set/test/delete works, and failed user credentials do not consume platform credentials;
4. deletion immediately ends ordinary access, recovery works before purge claim, and a claimed job cannot be recovered;
5. no workspace creation/switching UI is visible.

Record date, browser, backend, database backend, and result in the commit body/PR evidence; do not add screenshots containing email or Secret values.

- [ ] **Step 9: Commit the tenant account UI**

```bash
git add frontend/src src/multiclaw/static tests/test_frontend_debug.py tests/test_frontend_welcome.py
git commit -m "Let users manage durable tenant state without exposing its scope or secrets" \
  -m "Add CSRF-aware persisted approvals, masked BYOK settings, deletion recovery, and run-aware stream state without a workspace-switching surface." \
  -m "Constraint: Frontend has no test runner; lint, build, static contracts, and browser evidence are required" \
  -m "Confidence: medium" \
  -m "Scope-risk: moderate" \
  -m "Tested: frontend source contracts, npm lint, npm build, and documented browser scenarios"
```

### Task 18: Prove dual-backend isolation, remove legacy storage, and gate release

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `tests/integration/test_tenant_e2e.py`
- Expand: `tests/integration/test_workflow_faults.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_storage.py`
- Delete: `src/multiclaw/storage/repository.py`
- Delete: `src/multiclaw/storage/sqlite.py`
- Delete: `src/multiclaw/sqlite_utils.py`
- Modify: `src/multiclaw/storage/__init__.py`
- Create: `docs/multi-tenant-operations.md`

- [ ] **Step 1: Write the final two-user E2E and failure-count assertions**

```python
@pytest.mark.asyncio
async def test_two_users_have_zero_cross_tenant_successes(running_app, two_users):
    results = await exercise_concurrent_tenants(running_app, two_users)
    assert results.cross_tenant_reads == 0
    assert results.cross_tenant_writes == 0
    assert results.cross_tenant_approval_decisions == 0
    assert results.cross_tenant_secret_reads == 0
    assert results.foreign_sse_events == 0
    assert results.unexpected_session_creations == 0


@pytest.mark.asyncio
async def test_stale_and_faulted_workflow_writes_have_zero_successes(running_app):
    results = await inject_all_approved_fault_windows(running_app)
    assert results.stale_fence_writes == 0
    assert results.automatic_non_idempotent_retries == 0
    assert results.corrupt_checkpoint_tool_starts == 0
    assert results.completed_runs_with_active_execution == 0
```

Parameterize E2E and fault suites over file-backed SQLite and the MySQL service. Include account deletion/recovery/purge and platform-fallback zero-call assertions.

- [ ] **Step 2: Run the new release tests and observe remaining gaps**

```bash
uv run pytest tests/integration/test_tenant_e2e.py \
  tests/integration/test_workflow_faults.py -q
```

Expected: any still-unwired path fails before the legacy cleanup/CI gate is accepted.

- [ ] **Step 3: Delete generic and SQLite-only persistence escape hatches**

Move any unique generic repository tests into `tests/test_scoped_repositories.py`, change `tests/test_storage.py` into an import-surface test for `Database`, UoWs, schema, and scoped repositories, then delete `Repository`, `SqliteRepository`, `SqliteConfig`, and `sqlite_utils`. Verify:

```python
def test_storage_public_surface_has_no_unscoped_repository():
    import multiclaw.storage as storage

    assert not hasattr(storage, "Repository")
    assert not hasattr(storage, "SqliteRepository")
    assert hasattr(storage, "Database")
    assert hasattr(storage, "TenantUnitOfWork")
```

Run `rg 'aiosqlite.connect|CREATE TABLE IF NOT EXISTS|database\.path|chat_sessions\.user_id|shared_bus|global agent' src/multiclaw` and require zero production matches except `aiosqlite` inside SQLAlchemy dependency metadata, which is outside `src/`.

- [ ] **Step 4: Add the SQLite/MySQL CI matrix and frontend gate**

Create `.github/workflows/ci.yml` with separate jobs:

```yaml
jobs:
  backend:
    strategy:
      fail-fast: false
      matrix:
        backend: [sqlite, mysql]
    services:
      mysql:
        image: mysql:8.0.36
        env:
          MYSQL_DATABASE: multiclaw_test
          MYSQL_USER: multiclaw
          MYSQL_PASSWORD: multiclaw_test
          MYSQL_ROOT_PASSWORD: root_test
        ports: ["3306:3306"]
        options: >-
          --health-cmd="mysqladmin ping -h localhost -uroot -proot_test"
          --health-interval=5s --health-timeout=5s --health-retries=20
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --locked
      - run: uv run multiclaw db upgrade
      - run: uv run pytest -q
        env:
          MULTICLAW_DATABASE__DRIVER: ${{ matrix.backend }}
          MULTICLAW_DATABASE__URL: ${{ matrix.backend == 'mysql' && 'mysql+aiomysql://multiclaw:multiclaw_test@127.0.0.1:3306/multiclaw_test' || 'sqlite+aiosqlite:///./data/ci.db' }}
          MULTICLAW_TEST_MYSQL_URL: ${{ matrix.backend == 'mysql' && 'mysql+aiomysql://multiclaw:multiclaw_test@127.0.0.1:3306/multiclaw_test' || '' }}

  frontend:
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 22, cache: npm, cache-dependency-path: frontend/package-lock.json }
      - run: npm ci
        working-directory: frontend
      - run: npm run lint
        working-directory: frontend
      - run: npm run build
        working-directory: frontend
```

GitHub Actions does not allow dynamic omission of a service by matrix expression in every syntax version; if the SQLite matrix leg still starts MySQL, accept the harmless service startup but keep the application configured for SQLite. Store CI-only passwords literally only inside the isolated workflow service; no production Secret values enter repository files.

- [ ] **Step 5: Document explicit operations and non-goals**

Document:

- configure exactly one database URL/backend;
- inject JWT signing key and exactly one keyring source;
- run `multiclaw db upgrade` before API startup/deploy;
- interpret `/api/health/live` versus `/api/health/ready`;
- back up a production-like database before future forward-only upgrades;
- run/monitor purge worker and key rotation;
- no legacy data migration, dual-write, workspace switcher, cluster, KMS/Vault, superadmin, or same-run parallel tools in v1.

Do not include DSNs, tokens, emails, or real filesystem paths in the document.

- [ ] **Step 6: Run the complete release gate locally**

```bash
git diff --check
uv lock --check
uv run multiclaw db upgrade
uv run multiclaw db check
uv run pytest -q
MULTICLAW_DATABASE__DRIVER=mysql \
MULTICLAW_DATABASE__URL="$MULTICLAW_TEST_MYSQL_URL" \
MULTICLAW_TEST_MYSQL_URL="$MULTICLAW_TEST_MYSQL_URL" \
  uv run pytest -q
cd frontend && npm run lint
cd frontend && npm run build
placeholder_pattern='TO''DO|TB''D|FI''XME|待''定|待''确认'
rg -n "$placeholder_pattern" \
  docs/superpowers/specs/2026-08-15-multi-tenant-architecture-design.md \
  docs/superpowers/plans/2026-08-15-multi-tenant-implementation.md \
  src/multiclaw frontend/src tests && exit 1 || true
rg -n 'aiosqlite\.connect|CREATE TABLE IF NOT EXISTS|database\.path|chat_sessions\.user_id|shared_bus|global agent' \
  src/multiclaw && exit 1 || true
```

Expected: diff/lock/revision checks succeed, complete SQLite and MySQL suites have zero failures, frontend lint/build pass, placeholder scan is empty, and no legacy persistence/runtime escape-hatch pattern remains.

- [ ] **Step 7: Request independent code and security review**

Use the repository review workflows after implementation, with reviewers explicitly checking:

- every repository predicate/full composite FK;
- UoW single-connection ownership;
- JWT epoch/recovery audience/CSRF;
- AESGCM/AAD/keyring and plaintext lifetime;
- stale fencing/CAS and non-idempotent recovery;
- SSE exact scope;
- deletion race/purge order;
- SQLite/MySQL behavior parity;
- absence of app-level superadmin or bypass repository.

Resolve all critical/high findings and rerun Step 6 from the beginning.

- [ ] **Step 8: Commit the release gate and cleanup**

```bash
git add .github/workflows/ci.yml tests src/multiclaw/storage \
  docs/multi-tenant-operations.md
git commit -m "Make tenant isolation a release property instead of an implementation claim" \
  -m "Add dual-backend E2E/fault gates, remove legacy persistence escape hatches, and document explicit migration/key/deletion operations." \
  -m "Constraint: Public multi-tenant support requires the complete SQLite/MySQL and frontend matrix" \
  -m "Confidence: high" \
  -m "Scope-risk: broad" \
  -m "Directive: Do not merge future tenant-aware paths without both-backend isolation coverage" \
  -m "Tested: complete backend suites on SQLite/MySQL, migration checks, frontend lint/build, placeholder and legacy scans"
```

---

## Final acceptance checklist

- [ ] Exactly one backend is configured and its Alembic revision equals head.
- [ ] Every active user has one valid default workspace; no API/UI exposes workspace switching.
- [ ] Every tenant UoW owns one connection/transaction and every repository derives scope from its context.
- [ ] Cross-tenant and cross-workspace foreign-key inserts fail on SQLite and MySQL.
- [ ] No process-global Agent/EventBus/Scheduler/Skill/Tool/MCP mutable state remains.
- [ ] SSE delivers zero events outside the exact tenant/workspace/session/run scope.
- [ ] Old fencing tokens produce zero successful run/execution/checkpoint writes.
- [ ] Same-run tool concurrency never exceeds one; non-idempotent uncertain effects never auto-retry.
- [ ] Checkpoints are structured, hashed, versioned, size-bounded, and Secret-free.
- [ ] User Secret failure produces zero platform fallback calls; row swaps fail AESGCM authentication.
- [ ] Existing JWTs stop working immediately after disable/delete/recovery epoch changes.
- [ ] Recovery tokens cannot access ordinary APIs and lose validity once purge starts or recovery completes.
- [ ] Purge is asynchronous, retryable, explicit leaf-to-root, and succeeds on both backends without cascade.
- [ ] API never runs Alembic upgrade and readiness fails closed when schema/config/keyring/FK invariants fail.
- [ ] Secret canaries have zero matches in logs, SSE, checkpoints, audit details, traces, metrics, and frontend state.
- [ ] Backend full suite passes on SQLite and MySQL; frontend lint/build and manual browser verification pass.
- [ ] Independent code/security review has no unresolved critical or high findings.
