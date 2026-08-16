from __future__ import annotations

import asyncio
import base64
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
import sys
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, SecretSettings, Settings
from multiclaw.events import EventRouter, EventScope, ScopedEvent
from multiclaw.memory import MemoryEntry
from multiclaw.secrets.envelope import (
    EnvelopeFields,
    SECRET_ENVELOPE_ALGORITHM,
    SECRET_ENVELOPE_FORMAT_VERSION,
    EncryptedSecretRecord,
    SecretEnvelopeService,
)
from multiclaw.secrets.keyring import DeploymentKeyring, KEYRING_PROVIDER_NAME
from multiclaw.secrets.resolver import SecretResolver, UserSecretInvalidError
from multiclaw.storage import Database
from multiclaw.storage.schema import chat_sessions, deletion_jobs, memory_entries, users, workspaces
from multiclaw.session import ChatSession, SessionStatus
from multiclaw.storage.uow import AuthUnitOfWork, DeletionUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext, WorkspaceResolver
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import (
    ApprovalStatus,
    ExecutionStatus,
    RecoveryStrategy,
    RunLease,
    VersionConflictError,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database_fixtures import _ORIGINAL_TEST_MYSQL_URL

TEST_JWT_SIGNING_KEY = "test-jwt-signing-key-material-1234567890"


class SessionMessage(TypedDict):
    role: str
    content: str
    created_at: int


@dataclass(frozen=True, slots=True)
class TenantState:
    base_context: TenantContext
    session_context: TenantContext
    run_context: TenantContext
    lease: RunLease
    session_id: str
    session_title: str
    session_status: SessionStatus
    last_message_at: int | None
    messages: list[SessionMessage]


@dataclass(frozen=True, slots=True)
class TenantScopes:
    alpha: TenantState
    beta: TenantState


@dataclass(frozen=True, slots=True)
class DeletionTenantState:
    base_context: TenantContext
    session_context: TenantContext
    session_id: str
    session_title: str
    session_status: SessionStatus
    last_message_at: int | None
    messages: list[SessionMessage]
    workspace: Path
    marker_file: Path
    marker_contents: str


class _TrackingRuntimePool:
    def __init__(self) -> None:
        self.revoked: list[str] = []

    async def revoke(self, tenant_id: str) -> None:
        self.revoked.append(tenant_id)


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'tenant-e2e.db'}"


def _keyring_payload() -> str:
    return base64.b64encode(
        json.dumps(
            {
                "active_key_version": 3,
                "keys": {
                    "3": base64.b64encode(bytes(range(32))).decode("ascii"),
                },
            }
        ).encode("utf-8")
    ).decode("ascii")


async def _upgrade_database(url: str) -> Database:
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=url), "head")
    driver = "mysql" if url.startswith("mysql+aiomysql://") else "sqlite"
    return Database.create(DatabaseSettings(driver=driver, url=url))


async def _seed_user(database: Database, email_prefix: str) -> TenantContext:
    async with AuthUnitOfWork(database) as uow:
        user = await uow.users.create_user_with_default_workspace(
            f"{email_prefix}-{uuid4().hex[:8]}@example.com"
        )
        assert user.default_workspace_id is not None
        return TenantContext(user.id, user.default_workspace_id)


async def _load_auth_user(database: Database, tenant_id: str):
    async with AuthUnitOfWork(database, read_only=True) as uow:
        return await uow.users.get_by_id(tenant_id)


async def _load_deletion_user(database: Database, tenant_id: str):
    async with DeletionUnitOfWork(database, tenant_id, read_only=True) as uow:
        return await uow.users.get_current()


async def _load_current_deletion_job(database: Database, tenant_id: str):
    async with DeletionUnitOfWork(database, tenant_id, read_only=True) as uow:
        return await uow.deletions.get_current()


def _build_request(*, request_started_at_ms: int):
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/api/sessions", "headers": []})
    request.state.request_started_at_ms = request_started_at_ms
    return request


def _make_cookie(*, user_id: str, email: str, auth_epoch: int) -> dict[str, str]:
    import time

    import jwt

    now = int(time.time())
    return {
        "token": jwt.encode(
            {
                "sub": user_id,
                "email": email,
                "aud": "multiclaw-api",
                "iat": now,
                "exp": now + 86_400,
                "auth_epoch": auth_epoch,
            },
            TEST_JWT_SIGNING_KEY,
            algorithm="HS256",
        )
    }


@asynccontextmanager
async def _real_auth_client(database: Database):
    from fastapi import FastAPI
    import httpx

    from multiclaw.api.sessions import router as sessions_router
    from multiclaw.auth.middleware import AuthMiddleware
    from multiclaw.auth.models import build_auth_runtime

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(sessions_router)
    app.state.database = database
    app.state.settings = Settings(_config_file="/nonexistent")
    app.state.auth = build_auth_runtime(
        app.state.settings,
        environ={"MULTICLAW_AUTH_JWT_SIGNING_KEY": TEST_JWT_SIGNING_KEY},
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _coordinator(database: Database) -> WorkflowCoordinator:
    return WorkflowCoordinator(database, settings=Settings(_config_file="/nonexistent"))


async def _build_tenant_state(database: Database, label: str) -> TenantState:
    base_context = await _seed_user(database, f"tenant-e2e-{label}")
    async with TenantUnitOfWork(database, base_context) as uow:
        session = await uow.sessions.create()

    session_context = base_context.for_session(session.id)
    async with TenantUnitOfWork(database, session_context) as uow:
        await uow.memory.save(
            MemoryEntry(
                content=f"{label} question for private session",
                type="chat_message",
                role="user",
                turn_index=1,
            )
        )
        await uow.memory.save(
            MemoryEntry(
                content=f"{label} answer for private session",
                type="chat_message",
                role="assistant",
                turn_index=2,
            )
        )
        touched = await uow.sessions.touch_message(session.id, f"{label} question for private session")
        renamed = await uow.sessions.rename(session.id, f"{label.title()} locked session")
        fetched = await uow.sessions.get(session.id)
        listed = await uow.sessions.list()
        messages = await uow.sessions.get_messages(session.id)

    assert touched is not None
    assert renamed is not None
    assert fetched is not None
    assert [row.id for row in listed] == [session.id]
    assert [message["role"] for message in messages] == ["user", "assistant"]

    run_context = base_context.for_run(session.id, str(uuid4()))
    lease = await _coordinator(database).start_run(run_context, runtime_instance_id=f"{label}-runtime")

    return TenantState(
        base_context=base_context,
        session_context=session_context,
        run_context=run_context,
        lease=lease,
        session_id=session.id,
        session_title=fetched.title,
        session_status=fetched.status,
        last_message_at=fetched.last_message_at,
        messages=messages,
    )


async def _build_scopes(database: Database) -> TenantScopes:
    return TenantScopes(
        alpha=await _build_tenant_state(database, "alpha"),
        beta=await _build_tenant_state(database, "beta"),
    )


async def _build_deletion_tenant_state(
    database: Database,
    workspace_resolver: WorkspaceResolver,
    label: str,
) -> DeletionTenantState:
    base_context = await _seed_user(database, f"tenant-deletion-purge-{label}")
    async with TenantUnitOfWork(database, base_context) as uow:
        session = await uow.sessions.create()

    session_context = base_context.for_session(session.id)
    async with TenantUnitOfWork(database, session_context) as uow:
        await uow.memory.save(
            MemoryEntry(
                content=f"{label} user message",
                type="chat_message",
                role="user",
                turn_index=1,
            )
        )
        await uow.memory.save(
            MemoryEntry(
                content=f"{label} assistant reply",
                type="chat_message",
                role="assistant",
                turn_index=2,
            )
        )
        session_snapshot = await uow.sessions.get(session.id)
        messages = await uow.sessions.get_messages(session.id)

    assert session_snapshot is not None
    assert [message["role"] for message in messages] == ["user", "assistant"]

    workspace = workspace_resolver.resolve(base_context, create=True)
    marker_file = workspace / "marker.txt"
    marker_contents = f"{label}-workspace-marker-{uuid4().hex}"
    marker_file.write_text(marker_contents, encoding="utf-8")

    return DeletionTenantState(
        base_context=base_context,
        session_context=session_context,
        session_id=session.id,
        session_title=session_snapshot.title,
        session_status=session_snapshot.status,
        last_message_at=session_snapshot.last_message_at,
        messages=messages,
        workspace=workspace,
        marker_file=marker_file,
        marker_contents=marker_contents,
    )


async def _count_deletion_scope_rows(
    database: Database,
    state: DeletionTenantState,
) -> dict[str, int]:
    async with database.connect() as conn:
        user_count = await conn.scalar(
            select(func.count()).select_from(users).where(users.c.id == state.base_context.tenant_id)
        )
        job_count = await conn.scalar(
            select(func.count())
            .select_from(deletion_jobs)
            .where(deletion_jobs.c.tenant_id == state.base_context.tenant_id)
        )
        workspace_count = await conn.scalar(
            select(func.count())
            .select_from(workspaces)
            .where(
                workspaces.c.tenant_id == state.base_context.tenant_id,
                workspaces.c.id == state.base_context.workspace_id,
            )
        )
        session_count = await conn.scalar(
            select(func.count())
            .select_from(chat_sessions)
            .where(
                chat_sessions.c.tenant_id == state.base_context.tenant_id,
                chat_sessions.c.workspace_id == state.base_context.workspace_id,
                chat_sessions.c.id == state.session_id,
            )
        )
        memory_count = await conn.scalar(
            select(func.count())
            .select_from(memory_entries)
            .where(
                memory_entries.c.tenant_id == state.base_context.tenant_id,
                memory_entries.c.workspace_id == state.base_context.workspace_id,
                memory_entries.c.session_id == state.session_id,
            )
        )

    return {
        "users": int(user_count or 0),
        "deletion_jobs": int(job_count or 0),
        "workspaces": int(workspace_count or 0),
        "sessions": int(session_count or 0),
        "memory_entries": int(memory_count or 0),
    }


async def _count_test_sessions(database: Database, scopes: TenantScopes) -> int:
    # Shared CI schemas may retain unrelated rows from other tests; scope counts to
    # the random tenant ids created by this test so historical or parallel tenants
    # cannot affect this aggregate.
    tenant_ids = (
        scopes.alpha.base_context.tenant_id,
        scopes.beta.base_context.tenant_id,
    )
    async with database.write_transaction() as conn:
        total = await conn.scalar(
            select(func.count())
            .select_from(chat_sessions)
            .where(chat_sessions.c.tenant_id.in_(tenant_ids))
        )
    return int(total or 0)


async def _count_scoped_sessions(database: Database, context: TenantContext) -> int:
    async with database.write_transaction() as conn:
        total = await conn.scalar(
            select(func.count())
            .select_from(chat_sessions)
            .where(
                chat_sessions.c.tenant_id == context.tenant_id,
                chat_sessions.c.workspace_id == context.workspace_id,
            )
        )
    return int(total or 0)


async def _load_session_snapshot(
    database: Database,
    context: TenantContext,
    session_id: str,
) -> tuple[ChatSession | None, list[SessionMessage]]:
    async with TenantUnitOfWork(database, context) as uow:
        session = await uow.sessions.get(session_id)
        messages = await uow.sessions.get_messages(session_id)
    return session, messages


async def _collect_session_metrics(
    database: Database,
    scopes: TenantScopes,
) -> dict[str, int]:
    alpha = scopes.alpha
    beta = scopes.beta
    total_before = await _count_test_sessions(database, scopes)
    alpha_before = await _count_scoped_sessions(database, alpha.session_context)
    beta_before = await _count_scoped_sessions(database, beta.session_context)

    async with TenantUnitOfWork(database, alpha.session_context) as uow:
        foreign_session = await uow.sessions.get(beta.session_id)
        foreign_list = await uow.sessions.list(include_archived=True)
        foreign_touch = await uow.sessions.touch_message(beta.session_id, "foreign touch should fail")
        foreign_rename = await uow.sessions.rename(beta.session_id, "foreign rename should fail")
        foreign_messages = await uow.sessions.get_messages(beta.session_id)
        await uow.sessions.delete(beta.session_id)

    total_after = await _count_test_sessions(database, scopes)
    alpha_after = await _count_scoped_sessions(database, alpha.session_context)
    beta_after = await _count_scoped_sessions(database, beta.session_context)
    beta_session_after, beta_messages_after = await _load_session_snapshot(
        database,
        beta.session_context,
        beta.session_id,
    )

    assert beta_session_after is not None
    assert beta_session_after.title == beta.session_title
    assert beta_session_after.status == beta.session_status
    assert beta_session_after.last_message_at == beta.last_message_at
    assert beta_messages_after == beta.messages
    assert alpha_before == alpha_after
    assert beta_before == beta_after

    count_deltas = (
        total_after - total_before,
        alpha_after - alpha_before,
        beta_after - beta_before,
    )
    cross_tenant_reads = sum(
        (
            int(foreign_session is not None),
            int(any(row.id == beta.session_id for row in foreign_list)),
            int(bool(foreign_messages)),
        )
    )
    cross_tenant_writes = sum(
        (
            int(foreign_touch is not None),
            int(foreign_rename is not None),
            int(beta_session_after is None),
            int(beta_messages_after != beta.messages),
        )
    )
    unexpected_session_creations = sum(max(delta, 0) for delta in count_deltas)

    return {
        "cross_tenant_reads": cross_tenant_reads,
        "cross_tenant_writes": cross_tenant_writes,
        "unexpected_session_creations": unexpected_session_creations,
    }


async def _collect_event_metrics(scopes: TenantScopes) -> dict[str, int]:
    router = EventRouter()
    alpha_run_context = scopes.alpha.run_context
    beta_run_context = scopes.beta.run_context

    alpha_scope = EventScope.from_context(alpha_run_context)
    beta_scope = EventScope.from_context(beta_run_context)
    received: dict[str, list[tuple[str, str]]] = {"alpha": [], "beta": []}

    async def collect_alpha(event: ScopedEvent) -> None:
        await asyncio.sleep(0)
        received["alpha"].append((event.event_type, event.run_id))

    async def collect_beta(event: ScopedEvent) -> None:
        await asyncio.sleep(0)
        received["beta"].append((event.event_type, event.run_id))

    alpha_subscription = router.subscribe(alpha_scope, collect_alpha)
    beta_subscription = router.subscribe(beta_scope, collect_beta)
    try:
        await asyncio.gather(
            router.publish(
                ScopedEvent.from_scope(alpha_scope, "tenant.alpha.started", {"run_id": alpha_run_context.run_id})
            ),
            router.publish(
                ScopedEvent.from_scope(beta_scope, "tenant.beta.started", {"run_id": beta_run_context.run_id})
            ),
            router.publish(
                ScopedEvent.from_scope(alpha_scope, "tenant.alpha.finished", {"run_id": alpha_run_context.run_id})
            ),
            router.publish(
                ScopedEvent.from_scope(beta_scope, "tenant.beta.finished", {"run_id": beta_run_context.run_id})
            ),
        )
    finally:
        alpha_subscription.close()
        beta_subscription.close()

    expected_alpha = Counter(
        {
            ("tenant.alpha.started", alpha_run_context.run_id): 1,
            ("tenant.alpha.finished", alpha_run_context.run_id): 1,
        }
    )
    expected_beta = Counter(
        {
            ("tenant.beta.started", beta_run_context.run_id): 1,
            ("tenant.beta.finished", beta_run_context.run_id): 1,
        }
    )
    assert Counter(received["alpha"]) == expected_alpha
    assert Counter(received["beta"]) == expected_beta

    foreign_sse_events = sum(
        count
        for event_key, count in Counter(received["alpha"]).items()
        if event_key not in expected_alpha
    ) + sum(
        count
        for event_key, count in Counter(received["beta"]).items()
        if event_key not in expected_beta
    )
    return {"foreign_sse_events": foreign_sse_events}


async def _collect_approval_metrics(
    database: Database,
    scopes: TenantScopes,
) -> dict[str, int]:
    coordinator = _coordinator(database)
    alpha_run_context = scopes.alpha.run_context
    beta_run_context = scopes.beta.run_context
    alpha_lease = scopes.alpha.lease
    beta_lease = scopes.beta.lease

    alpha_approval = await coordinator.create_approval(
        alpha_lease,
        approval_id=str(uuid4()),
        tool_call_id="alpha-tool-call",
        expires_at=database.dialect.db_now_ms() + 60_000,
    )
    beta_approval = await coordinator.create_approval(
        beta_lease,
        approval_id=str(uuid4()),
        tool_call_id="beta-tool-call",
        expires_at=database.dialect.db_now_ms() + 60_000,
    )
    alpha_execution = await coordinator.create_execution(
        alpha_lease,
        execution_id=str(uuid4()),
        approval_id=alpha_approval.approval_id,
        tool_call_id="alpha-tool-call",
        tool_name="echo",
        tool_kind="builtin",
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        idempotency_key=None,
        input_payload_json='{"tenant":"alpha"}',
        input_hash="a" * 64,
        status=ExecutionStatus.NOT_STARTED,
    )
    beta_execution = await coordinator.create_execution(
        beta_lease,
        execution_id=str(uuid4()),
        approval_id=beta_approval.approval_id,
        tool_call_id="beta-tool-call",
        tool_name="echo",
        tool_kind="builtin",
        recovery_strategy=RecoveryStrategy.READ_ONLY_REPLAY,
        idempotency_key=None,
        input_payload_json='{"tenant":"beta"}',
        input_hash="b" * 64,
        status=ExecutionStatus.NOT_STARTED,
    )

    assert alpha_execution is not None
    assert beta_execution is not None

    own_beta_approval = await coordinator.get_approval(beta_run_context, beta_approval.approval_id)
    own_beta_execution = await coordinator.get_execution_by_approval_id(
        beta_run_context,
        beta_approval.approval_id,
    )
    assert own_beta_approval is not None
    assert own_beta_execution is not None
    assert own_beta_approval.status is ApprovalStatus.AWAITING_USER
    assert own_beta_approval.version == 1

    foreign_beta_approval = await coordinator.get_approval(alpha_run_context, beta_approval.approval_id)
    foreign_beta_execution = await coordinator.get_execution_by_approval_id(
        alpha_run_context,
        beta_approval.approval_id,
    )
    unexpected_decisions = 0
    try:
        await coordinator.decide_approval(
            alpha_run_context,
            beta_approval.approval_id,
            approved=False,
            version=beta_approval.version,
        )
        unexpected_decisions += 1
    except VersionConflictError as exc:
        assert str(exc) == "approval record not found"

    beta_approval_after = await coordinator.get_approval(beta_run_context, beta_approval.approval_id)
    assert beta_approval_after is not None
    assert beta_approval_after.status is ApprovalStatus.AWAITING_USER
    assert beta_approval_after.version == beta_approval.version
    assert beta_approval_after.resolved_at is None
    assert foreign_beta_execution is None

    cross_tenant_approval_decisions = sum(
        (
            int(foreign_beta_approval is not None),
            unexpected_decisions,
        )
    )
    return {"cross_tenant_approval_decisions": cross_tenant_approval_decisions}


async def _collect_secret_metrics(
    database: Database,
    scopes: TenantScopes,
) -> dict[str, int]:
    alpha_base_context = scopes.alpha.base_context
    beta_base_context = scopes.beta.base_context

    alpha_secret_name = "alpha_api_key"
    beta_secret_name = "beta_api_key"

    alpha_record = EncryptedSecretRecord(
        key_provider_name=KEYRING_PROVIDER_NAME,
        format_version=SECRET_ENVELOPE_FORMAT_VERSION,
        algorithm=SECRET_ENVELOPE_ALGORITHM,
        key_version=1,
        nonce=b"alpha-nonce1",
        ciphertext=b"alpha-ciphertext-01",
    )
    beta_record = EncryptedSecretRecord(
        key_provider_name=KEYRING_PROVIDER_NAME,
        format_version=SECRET_ENVELOPE_FORMAT_VERSION,
        algorithm=SECRET_ENVELOPE_ALGORITHM,
        key_version=1,
        nonce=b"beta-nonce-2",
        ciphertext=b"beta-ciphertext-02",
    )

    async with TenantUnitOfWork(database, alpha_base_context) as uow:
        alpha_metadata = await uow.secrets.put_encrypted(
            secret_id=str(uuid4()),
            provider_kind="llm",
            provider_name="openai",
            secret_name=alpha_secret_name,
            record=alpha_record,
        )
    async with TenantUnitOfWork(database, beta_base_context) as uow:
        beta_metadata = await uow.secrets.put_encrypted(
            secret_id=str(uuid4()),
            provider_kind="llm",
            provider_name="openai",
            secret_name=beta_secret_name,
            record=beta_record,
        )
        beta_secret = await uow.secrets.get_encrypted("llm", "openai", beta_secret_name)

    assert alpha_metadata.secret_name == alpha_secret_name
    assert beta_metadata.secret_name == beta_secret_name
    assert beta_secret is not None
    assert beta_secret.record.ciphertext == beta_record.ciphertext

    async with TenantUnitOfWork(database, alpha_base_context) as uow:
        foreign_metadata = await uow.secrets.get_metadata("llm", "openai", beta_secret_name)
        foreign_secret = await uow.secrets.get_encrypted("llm", "openai", beta_secret_name)
        visible_metadata = await uow.secrets.list_metadata()

    async with TenantUnitOfWork(database, beta_base_context) as uow:
        beta_secret_after = await uow.secrets.get_encrypted("llm", "openai", beta_secret_name)
        beta_list_after = await uow.secrets.list_metadata()

    assert beta_secret_after is not None
    assert beta_secret_after.secret_id == beta_metadata.secret_id
    assert beta_secret_after.record.ciphertext == beta_record.ciphertext
    assert any(entry.secret_id == beta_metadata.secret_id for entry in beta_list_after)

    cross_tenant_secret_reads = sum(
        (
            int(foreign_metadata is not None),
            int(foreign_secret is not None),
            int(any(entry.secret_id == beta_metadata.secret_id for entry in visible_metadata)),
        )
    )

    invalid_secret_name = "alpha_invalid_api_key"
    keyring = DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
    )
    envelope = SecretEnvelopeService(keyring, nonce_source=lambda _length: bytes(range(12)))
    invalid_secret_id = str(uuid4())
    invalid_record = envelope.encrypt(
        b"alpha-invalid-secret",
        EnvelopeFields(
            tenant_id=alpha_base_context.tenant_id,
            workspace_id=None,
            secret_id=invalid_secret_id,
            provider_kind="llm",
            provider_name="openai",
            secret_name=invalid_secret_name,
        ),
    )

    async with TenantUnitOfWork(database, alpha_base_context) as uow:
        await uow.secrets.put_encrypted(
            secret_id=invalid_secret_id,
            provider_kind="llm",
            provider_name="openai",
            secret_name=invalid_secret_name,
            record=invalid_record,
        )
        invalid_secret = await uow.secrets.get_encrypted("llm", "openai", invalid_secret_name)
        assert invalid_secret is not None
        tampered_ciphertext = (
            invalid_secret.record.ciphertext[:-1]
            + bytes([invalid_secret.record.ciphertext[-1] ^ 0x01])
        )
        assert tampered_ciphertext != invalid_secret.record.ciphertext
        await uow.secrets.put_encrypted(
            secret_id=invalid_secret.secret_id,
            provider_kind="llm",
            provider_name="openai",
            secret_name=invalid_secret_name,
            record=invalid_secret.record.replace(
                ciphertext=tampered_ciphertext
            ),
        )

    platform_lookup_calls: list[tuple[str, str, str]] = []
    resolver = SecretResolver(
        database=database,
        settings=SecretSettings(allow_platform_fallback=True),
        keyring=keyring,
        envelope=SecretEnvelopeService(keyring),
        platform_lookup=lambda provider_kind, provider_name, secret_name: (
            platform_lookup_calls.append((provider_kind, provider_name, secret_name))
            or "platform-secret"
        ),
    )

    try:
        resolved = await resolver.resolve(alpha_base_context, "llm", "openai", invalid_secret_name)
    except UserSecretInvalidError:
        resolved = None
    else:
        resolved.close()

    assert resolved is None
    return {
        "cross_tenant_secret_reads": cross_tenant_secret_reads,
        "platform_fallback_calls": len(platform_lookup_calls),
    }


async def _collect_tenant_isolation_metrics(database: Database) -> dict[str, int]:
    scopes = await _build_scopes(database)
    session_metrics = await _collect_session_metrics(database, scopes)
    event_metrics = await _collect_event_metrics(scopes)
    approval_metrics = await _collect_approval_metrics(database, scopes)
    secret_metrics = await _collect_secret_metrics(database, scopes)
    return {
        "cross_tenant_reads": session_metrics["cross_tenant_reads"],
        "cross_tenant_writes": session_metrics["cross_tenant_writes"],
        "cross_tenant_approval_decisions": approval_metrics["cross_tenant_approval_decisions"],
        "cross_tenant_secret_reads": secret_metrics["cross_tenant_secret_reads"],
        "platform_fallback_calls": secret_metrics["platform_fallback_calls"],
        "foreign_sse_events": event_metrics["foreign_sse_events"],
        "unexpected_session_creations": session_metrics["unexpected_session_creations"],
    }


@pytest.fixture(params=("sqlite", "mysql"))
async def tenant_isolation_database(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "mysql":
        url = _ORIGINAL_TEST_MYSQL_URL or os.getenv("MULTICLAW_TEST_MYSQL_URL")
        if not url:
            pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")
    else:
        url = _sqlite_url(tmp_path)

    database = await _upgrade_database(url)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_real_tenant_aggregate_isolation_gate(tenant_isolation_database: Database) -> None:
    metrics = await _collect_tenant_isolation_metrics(tenant_isolation_database)

    assert metrics["cross_tenant_reads"] == 0
    assert metrics["cross_tenant_writes"] == 0
    assert metrics["cross_tenant_approval_decisions"] == 0
    assert metrics["cross_tenant_secret_reads"] == 0
    assert metrics["platform_fallback_calls"] == 0
    assert metrics["foreign_sse_events"] == 0
    assert metrics["unexpected_session_creations"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_days", [1, 7], ids=["retention-1-day", "retention-7-days"])
async def test_real_deletion_recovery_blocks_ordinary_tenant_access(
    tenant_isolation_database: Database,
    retention_days: int,
) -> None:
    from multiclaw.api.dependencies import tenant_context
    from multiclaw.deletion.service import DeletionService

    base_context = await _seed_user(tenant_isolation_database, f"tenant-deletion-{retention_days}")
    async with AuthUnitOfWork(tenant_isolation_database) as uow:
        baseline_user = await uow.users.get_by_id(base_context.tenant_id)

    assert baseline_user is not None
    assert baseline_user.status == "active"
    assert baseline_user.auth_epoch == 0

    runtime_pool = _TrackingRuntimePool()
    service = DeletionService(
        database=tenant_isolation_database,
        runtime_pool=runtime_pool,
        settings=Settings(
            _config_file="/nonexistent",
            deletion={"retention_days": retention_days},
        ),
    )

    request_started_at_ms = 123_456_789
    scheduled = await service.request(base_context)

    pending_user = await _load_auth_user(tenant_isolation_database, base_context.tenant_id)
    pending_deletion_user = await _load_deletion_user(tenant_isolation_database, base_context.tenant_id)
    pending_job = await _load_current_deletion_job(tenant_isolation_database, base_context.tenant_id)

    assert pending_user is not None
    assert pending_deletion_user is not None
    assert pending_job is not None
    assert scheduled.status == "scheduled"
    assert scheduled.job_id == pending_job.job_id
    assert scheduled.requested_at == pending_job.requested_at
    assert scheduled.purge_after == pending_job.purge_after
    assert scheduled.purge_after > scheduled.requested_at
    assert pending_job.status == "scheduled"
    assert pending_user.status == "pending_purge"
    assert pending_user.auth_epoch == baseline_user.auth_epoch + 1
    assert pending_deletion_user.status == "pending_purge"
    assert pending_deletion_user.auth_epoch == baseline_user.auth_epoch + 1
    assert pending_deletion_user.purge_requested_at == pending_job.requested_at
    assert pending_deletion_user.purge_after == pending_job.purge_after
    assert runtime_pool.revoked == [base_context.tenant_id]

    pending_cookie = _make_cookie(
        user_id=base_context.tenant_id,
        email=pending_user.email,
        auth_epoch=pending_user.auth_epoch,
    )
    async with _real_auth_client(tenant_isolation_database) as client:
        client.cookies.update(pending_cookie)
        blocked = await client.get("/api/sessions")

    assert blocked.status_code == 403
    assert blocked.json() == {"detail": "Account pending deletion"}

    await service.recover(base_context, scheduled.job_id)

    restored_user = await _load_auth_user(tenant_isolation_database, base_context.tenant_id)
    restored_deletion_user = await _load_deletion_user(tenant_isolation_database, base_context.tenant_id)
    restored_job = await _load_current_deletion_job(tenant_isolation_database, base_context.tenant_id)

    assert restored_user is not None
    assert restored_deletion_user is not None
    assert restored_job is None
    assert restored_user.status == "active"
    assert restored_user.auth_epoch == baseline_user.auth_epoch + 2
    assert restored_deletion_user.status == "active"
    assert restored_deletion_user.auth_epoch == baseline_user.auth_epoch + 2
    assert restored_deletion_user.purge_requested_at is None
    assert restored_deletion_user.purge_after is None
    assert runtime_pool.revoked == [base_context.tenant_id, base_context.tenant_id]

    restored_request = _build_request(
        request_started_at_ms=request_started_at_ms,
    )
    restored_context = await tenant_context(restored_request, restored_user)

    assert restored_context.tenant_id == base_context.tenant_id
    assert restored_context.workspace_id == base_context.workspace_id
    assert restored_context.request_started_at_ms == request_started_at_ms


@pytest.mark.asyncio
async def test_real_deletion_purge_removes_only_target_tenant_and_closes_recovery_when_running(
    tenant_isolation_database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.deletion.service import DeletionService, RecoveryWindowClosedError
    from multiclaw.deletion.worker import DeletionWorker

    workspace_root = tmp_path / "deletion-workspaces"
    workspace_root.mkdir()
    workspace_resolver = WorkspaceResolver(workspace_root)
    target = await _build_deletion_tenant_state(
        tenant_isolation_database,
        workspace_resolver,
        "target",
    )
    survivor = await _build_deletion_tenant_state(
        tenant_isolation_database,
        workspace_resolver,
        "survivor",
    )

    runtime_pool = _TrackingRuntimePool()
    service = DeletionService(
        database=tenant_isolation_database,
        runtime_pool=runtime_pool,
        settings=Settings(
            _config_file="/nonexistent",
            deletion={"retention_days": 0},
        ),
    )

    scheduled = await service.request(target.base_context)

    pending_job = await _load_current_deletion_job(
        tenant_isolation_database,
        target.base_context.tenant_id,
    )
    pending_user = await _load_auth_user(
        tenant_isolation_database,
        target.base_context.tenant_id,
    )
    pending_deletion_user = await _load_deletion_user(
        tenant_isolation_database,
        target.base_context.tenant_id,
    )
    survivor_user = await _load_auth_user(
        tenant_isolation_database,
        survivor.base_context.tenant_id,
    )
    survivor_job = await _load_current_deletion_job(
        tenant_isolation_database,
        survivor.base_context.tenant_id,
    )
    target_counts_before_worker = await _count_deletion_scope_rows(
        tenant_isolation_database,
        target,
    )
    survivor_counts_before_worker = await _count_deletion_scope_rows(
        tenant_isolation_database,
        survivor,
    )

    assert pending_job is not None
    assert pending_user is not None
    assert pending_deletion_user is not None
    assert survivor_user is not None
    assert scheduled.status == "scheduled"
    assert scheduled.job_id == pending_job.job_id
    assert scheduled.purge_after == scheduled.requested_at
    assert pending_job.status == "scheduled"
    assert pending_user.status == "pending_purge"
    assert pending_deletion_user.status == "pending_purge"
    assert target_counts_before_worker == {
        "users": 1,
        "deletion_jobs": 1,
        "workspaces": 1,
        "sessions": 1,
        "memory_entries": 2,
    }
    assert survivor_counts_before_worker == {
        "users": 1,
        "deletion_jobs": 0,
        "workspaces": 1,
        "sessions": 1,
        "memory_entries": 2,
    }
    assert target.workspace.exists()
    assert target.marker_file.exists()
    assert target.marker_file.read_text(encoding="utf-8") == target.marker_contents
    assert survivor.workspace.exists()
    assert survivor.marker_file.exists()
    assert survivor.marker_file.read_text(encoding="utf-8") == survivor.marker_contents
    assert survivor_user.status == "active"
    assert survivor_job is None
    assert runtime_pool.revoked == [target.base_context.tenant_id]

    worker = DeletionWorker(
        database=tenant_isolation_database,
        runtime_pool=runtime_pool,
        workspace_resolver=workspace_resolver,
        settings=Settings(
            _config_file="/nonexistent",
            deletion={"retention_days": 0},
        ),
        worker_id="worker-tenant-e2e-purge",
    )
    entered_purge = asyncio.Event()
    release_purge = asyncio.Event()
    original_purge_job = worker._purge_job

    async def paused_purge_job(job):
        entered_purge.set()
        await release_purge.wait()
        return await original_purge_job(job)

    monkeypatch.setattr(worker, "_purge_job", paused_purge_job)
    batch_task = asyncio.create_task(worker.run_batch(batch_size=10))
    await asyncio.wait_for(entered_purge.wait(), timeout=5)

    running_job = await _load_current_deletion_job(
        tenant_isolation_database,
        target.base_context.tenant_id,
    )
    assert running_job is not None
    assert running_job.job_id == scheduled.job_id
    assert running_job.status == "running"

    with pytest.raises(RecoveryWindowClosedError):
        await service.recover(target.base_context, scheduled.job_id)

    release_purge.set()
    batch = await asyncio.wait_for(batch_task, timeout=10)

    target_user_after = await _load_auth_user(
        tenant_isolation_database,
        target.base_context.tenant_id,
    )
    target_deletion_user_after = await _load_deletion_user(
        tenant_isolation_database,
        target.base_context.tenant_id,
    )
    target_job_after = await _load_current_deletion_job(
        tenant_isolation_database,
        target.base_context.tenant_id,
    )
    survivor_user_after = await _load_auth_user(
        tenant_isolation_database,
        survivor.base_context.tenant_id,
    )
    survivor_job_after = await _load_current_deletion_job(
        tenant_isolation_database,
        survivor.base_context.tenant_id,
    )
    survivor_session_after, survivor_messages_after = await _load_session_snapshot(
        tenant_isolation_database,
        survivor.session_context,
        survivor.session_id,
    )
    target_counts_after = await _count_deletion_scope_rows(
        tenant_isolation_database,
        target,
    )
    survivor_counts_after = await _count_deletion_scope_rows(
        tenant_isolation_database,
        survivor,
    )

    assert batch.claimed == 1
    assert batch.completed == 1
    assert batch.failed == 0
    assert target_user_after is None
    assert target_deletion_user_after is None
    assert target_job_after is None
    assert target_counts_after == {
        "users": 0,
        "deletion_jobs": 0,
        "workspaces": 0,
        "sessions": 0,
        "memory_entries": 0,
    }
    assert not target.marker_file.exists()
    assert not target.workspace.exists()
    assert survivor_user_after is not None
    assert survivor_user_after.status == "active"
    assert survivor_job_after is None
    assert survivor_session_after is not None
    assert survivor_session_after.title == survivor.session_title
    assert survivor_session_after.status == survivor.session_status
    assert survivor_session_after.last_message_at == survivor.last_message_at
    assert survivor_messages_after == survivor.messages
    assert survivor_counts_after == {
        "users": 1,
        "deletion_jobs": 0,
        "workspaces": 1,
        "sessions": 1,
        "memory_entries": 2,
    }
    assert survivor.marker_file.exists()
    assert survivor.marker_file.read_text(encoding="utf-8") == survivor.marker_contents
    assert runtime_pool.revoked == [
        target.base_context.tenant_id,
        target.base_context.tenant_id,
    ]

    with pytest.raises(RecoveryWindowClosedError):
        await service.recover(target.base_context, scheduled.job_id)
