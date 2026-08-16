from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import os
import sys
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import func, select

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, Settings
from multiclaw.events import EventRouter, EventScope, ScopedEvent
from multiclaw.memory import MemoryEntry
from multiclaw.secrets.envelope import (
    SECRET_ENVELOPE_ALGORITHM,
    SECRET_ENVELOPE_FORMAT_VERSION,
    EncryptedSecretRecord,
)
from multiclaw.secrets.keyring import KEYRING_PROVIDER_NAME
from multiclaw.storage import Database
from multiclaw.storage.schema import chat_sessions
from multiclaw.session import ChatSession, SessionStatus
from multiclaw.storage.uow import AuthUnitOfWork, TenantUnitOfWork
from multiclaw.tenancy import TenantContext
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


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'tenant-e2e.db'}"


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
    return {"cross_tenant_secret_reads": cross_tenant_secret_reads}


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
    assert metrics["foreign_sse_events"] == 0
    assert metrics["unexpected_session_creations"] == 0
