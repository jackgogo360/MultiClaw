import asyncio
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, SecretSettings
from multiclaw.secrets.envelope import EnvelopeFields, SecretEnvelopeService
from multiclaw.secrets.keyring import DeploymentKeyring, SecretKeyringError
from multiclaw.secrets.resolver import (
    ResolvedSecret,
    SecretBytes,
    SecretNotConfiguredError,
    SecretResolver,
    UserSecretInvalidError,
)
from multiclaw.storage import Database
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext


_ORIGINAL_TEST_MYSQL_URL = os.getenv("MULTICLAW_TEST_MYSQL_URL")


def _keyring_payload() -> str:
    return base64.b64encode(
        json.dumps(
            {
                "active_key_version": 3,
                "keys": {
                    "1": base64.b64encode(bytes([7]) * 32).decode("ascii"),
                    "3": base64.b64encode(bytes(range(32))).decode("ascii"),
                },
            }
        ).encode("utf-8")
    ).decode("ascii")


def _sqlite_url(tmp_path: Path, name: str) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / name}"


async def _upgrade_database(database_url: str) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")


async def _create_database(*, driver: str, database_url: str) -> Database:
    if driver == "sqlite":
        await _upgrade_database(database_url)
    return Database.create(DatabaseSettings(driver=driver, url=database_url))


@pytest.fixture(params=("sqlite", "mysql"))
async def secrets_database(request: pytest.FixtureRequest, tmp_path: Path):
    driver = request.param
    if driver == "sqlite":
        database = await _create_database(driver=driver, database_url=_sqlite_url(tmp_path, "secrets.db"))
        try:
            yield database
        finally:
            await database.dispose()
        return

    database_url = _ORIGINAL_TEST_MYSQL_URL or os.getenv("MULTICLAW_TEST_MYSQL_URL")
    if not database_url:
        pytest.skip("MULTICLAW_TEST_MYSQL_URL is not configured")

    admin_database = Database.create(DatabaseSettings(driver="mysql", url=database_url))
    schema_name = f"multiclaw_task13_{uuid4().hex[:12]}"
    temporary_url = make_url(database_url).set(database=schema_name).render_as_string(hide_password=False)

    try:
        async with admin_database.write_transaction() as conn:
            await conn.execute(text(f"CREATE DATABASE `{schema_name}` CHARACTER SET utf8mb4"))
        database = await _create_database(driver="mysql", database_url=temporary_url)
        try:
            yield database
        finally:
            await database.dispose()
    finally:
        async with admin_database.write_transaction() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS `{schema_name}`"))
        await admin_database.dispose()


async def _seed_scope(database: Database, *, slug: str) -> TenantContext:
    tenant_id = str(uuid4())
    workspace_id = str(uuid4())
    async with database.write_transaction() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users (
                    id, email, auth_epoch, default_workspace_id, status,
                    purge_after, created_at, updated_at, disabled_at, purge_requested_at
                )
                VALUES (:tenant_id, :email, 0, NULL, 'active', NULL, 1, 1, NULL, NULL)
                """
            ),
            {"tenant_id": tenant_id, "email": f"{slug}@example.com"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                VALUES (:workspace_id, :tenant_id, :slug, :name, 'active', 1, 1)
                """
            ),
            {"workspace_id": workspace_id, "tenant_id": tenant_id, "slug": slug, "name": slug.title()},
        )
        await conn.execute(
            text("UPDATE users SET default_workspace_id = :workspace_id WHERE id = :tenant_id"),
            {"workspace_id": workspace_id, "tenant_id": tenant_id},
        )
    return TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)


def _resolver(database: Database, *, allow_platform_fallback: bool, platform_lookup=None) -> SecretResolver:
    keyring = DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
    )
    return SecretResolver(
        database=database,
        settings=SecretSettings(allow_platform_fallback=allow_platform_fallback),
        keyring=keyring,
        envelope=SecretEnvelopeService(keyring),
        platform_lookup=platform_lookup,
    )


async def _store_secret(
    database: Database,
    context: TenantContext,
    *,
    provider_kind: str = "llm",
    provider_name: str = "openai",
    secret_name: str = "api_key",
    plaintext: bytes = b"user-secret-value",
    key_version: int | None = None,
) -> str:
    keyring = DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
    )
    envelope = SecretEnvelopeService(keyring, nonce_source=lambda _length: os.urandom(12))
    secret_id = str(uuid4())
    fields = EnvelopeFields(
        tenant_id=context.tenant_id,
        workspace_id=None,
        secret_id=secret_id,
        provider_kind=provider_kind,
        provider_name=provider_name,
        secret_name=secret_name,
    )
    record = envelope.encrypt(plaintext, fields, key_version=key_version)

    async with TenantUnitOfWork(database, context) as uow:
        await uow.secrets.put_encrypted(
            secret_id=secret_id,
            provider_kind=provider_kind,
            provider_name=provider_name,
            secret_name=secret_name,
            record=record,
        )
    return secret_id


@pytest.mark.asyncio
async def test_scoped_secret_repository_crud_and_cross_tenant_isolation(secrets_database: Database) -> None:
    primary = await _seed_scope(secrets_database, slug="alpha")
    secondary = await _seed_scope(secrets_database, slug="beta")
    secret_id = await _store_secret(secrets_database, primary)

    async with TenantUnitOfWork(secrets_database, primary) as uow:
        metadata = await uow.secrets.get_metadata("llm", "openai", "api_key")
        stored = await uow.secrets.get_encrypted("llm", "openai", "api_key")
        counts = await uow.secrets.count_key_versions()

    async with TenantUnitOfWork(secrets_database, secondary) as foreign:
        assert await foreign.secrets.get_metadata("llm", "openai", "api_key") is None
        assert await foreign.secrets.get_encrypted("llm", "openai", "api_key") is None

    assert metadata is not None
    assert metadata.secret_id == secret_id
    assert metadata.workspace_id is None
    assert metadata.masked_value.startswith("****")
    assert stored is not None
    assert stored.secret_id == secret_id
    assert counts == {3: 1}


@pytest.mark.asyncio
async def test_resolver_prefers_user_secret_and_zeroizes_plaintext(secrets_database: Database) -> None:
    context = await _seed_scope(secrets_database, slug="alpha")
    await _store_secret(secrets_database, context, plaintext=b"user-canary-value")
    lookup_calls: list[tuple[str, str, str]] = []

    resolver = _resolver(
        secrets_database,
        allow_platform_fallback=True,
        platform_lookup=lambda provider_kind, provider_name, secret_name: (
            lookup_calls.append((provider_kind, provider_name, secret_name)) or "platform-secret"
        ),
    )

    resolved = await resolver.resolve(context, "llm", "openai", "api_key")

    assert isinstance(resolved, ResolvedSecret)
    assert resolved.source == "user"
    assert resolved.masked_value.startswith("****")
    with resolved.reveal() as plaintext:
        assert bytes(plaintext) == b"user-canary-value"
    assert resolved.secret_bytes.is_zeroized()
    assert lookup_calls == []
    assert "user-canary-value" not in repr(resolved)


@pytest.mark.asyncio
async def test_resolver_fails_closed_on_invalid_user_secret_without_fallback(secrets_database: Database) -> None:
    context = await _seed_scope(secrets_database, slug="alpha")
    await _store_secret(secrets_database, context)
    lookup_calls: list[tuple[str, str, str]] = []
    resolver = _resolver(
        secrets_database,
        allow_platform_fallback=True,
        platform_lookup=lambda provider_kind, provider_name, secret_name: (
            lookup_calls.append((provider_kind, provider_name, secret_name)) or "platform-secret"
        ),
    )

    async with TenantUnitOfWork(secrets_database, context) as uow:
        row = await uow.secrets.get_encrypted("llm", "openai", "api_key")
        assert row is not None
        await uow.secrets.put_encrypted(
            secret_id=row.secret_id,
            provider_kind="llm",
            provider_name="openai",
            secret_name="api_key",
            record=row.record.replace(ciphertext=row.record.ciphertext[:-1] + b"\x00"),
        )

    with pytest.raises(UserSecretInvalidError):
        await resolver.resolve(context, "llm", "openai", "api_key")

    assert lookup_calls == []


@pytest.mark.asyncio
async def test_resolver_uses_platform_fallback_only_when_row_absent(secrets_database: Database) -> None:
    context = await _seed_scope(secrets_database, slug="alpha")
    lookup_calls: list[tuple[str, str, str]] = []
    resolver = _resolver(
        secrets_database,
        allow_platform_fallback=True,
        platform_lookup=lambda provider_kind, provider_name, secret_name: (
            lookup_calls.append((provider_kind, provider_name, secret_name)) or "platform-secret"
        ),
    )

    resolved = await resolver.resolve(context, "llm", "openai", "api_key")

    assert resolved.source == "platform"
    with resolved.reveal() as plaintext:
        assert bytes(plaintext) == b"platform-secret"
    assert lookup_calls == [("llm", "openai", "api_key")]


@pytest.mark.asyncio
async def test_resolver_reports_not_configured_when_absent_and_not_opted_in(
    secrets_database: Database,
) -> None:
    context = await _seed_scope(secrets_database, slug="alpha")
    resolver = _resolver(secrets_database, allow_platform_fallback=False, platform_lookup=lambda *_: "platform")

    with pytest.raises(SecretNotConfiguredError):
        await resolver.resolve(context, "llm", "openai", "api_key")


def test_secret_bytes_repr_masks_and_zeroizes() -> None:
    secret = SecretBytes(b"secret-canary-123")

    with secret.reveal() as plaintext:
        assert bytes(plaintext) == b"secret-canary-123"

    assert secret.is_zeroized()
    assert "secret-canary-123" not in repr(secret)


def test_secret_bytes_adopts_owned_buffer_without_copy() -> None:
    raw = bytearray(b"owned-secret")
    secret = SecretBytes.adopt(raw)

    with secret.reveal() as plaintext:
        assert bytes(plaintext) == b"owned-secret"

    assert raw == bytearray(b"\x00" * len(raw))


@pytest.mark.asyncio
async def test_resolver_zeroizes_original_decrypt_buffer_on_context_exit(
    secrets_database: Database,
) -> None:
    context = await _seed_scope(secrets_database, slug="alpha")
    await _store_secret(secrets_database, context, plaintext=b"user-canary-value")
    raw = bytearray(b"user-canary-value")

    class _FakeEnvelope:
        def decrypt(self, record, fields):
            del record, fields
            return raw

    resolver = SecretResolver(
        database=secrets_database,
        settings=SecretSettings(allow_platform_fallback=False),
        keyring=DeploymentKeyring.load(
            SecretSettings(),
            environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
        ),
        envelope=_FakeEnvelope(),
    )

    resolved = await resolver.resolve(context, "llm", "openai", "api_key")
    with resolved.reveal() as plaintext:
        assert bytes(plaintext) == b"user-canary-value"

    assert raw == bytearray(b"\x00" * len(raw))


@pytest.mark.asyncio
async def test_resolver_zeroizes_original_decrypt_buffer_on_reveal_exception(
    secrets_database: Database,
) -> None:
    context = await _seed_scope(secrets_database, slug="alpha")
    await _store_secret(secrets_database, context, plaintext=b"user-canary-value")
    raw = bytearray(b"user-canary-value")

    class _FakeEnvelope:
        def decrypt(self, record, fields):
            del record, fields
            return raw

    resolver = SecretResolver(
        database=secrets_database,
        settings=SecretSettings(allow_platform_fallback=False),
        keyring=DeploymentKeyring.load(
            SecretSettings(),
            environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
        ),
        envelope=_FakeEnvelope(),
    )

    resolved = await resolver.resolve(context, "llm", "openai", "api_key")
    with pytest.raises(RuntimeError, match="boom"):
        with resolved.reveal():
            raise RuntimeError("boom")

    assert raw == bytearray(b"\x00" * len(raw))


@pytest.mark.asyncio
async def test_resolve_credentials_concurrent_platform_values_do_not_race(
    secrets_database: Database,
) -> None:
    context = await _seed_scope(secrets_database, slug="alpha")
    keyring = DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload()},
    )
    barrier = asyncio.Event()
    arrivals = 0
    arrival_lock = asyncio.Lock()

    class _RacingResolver(SecretResolver):
        async def resolve(self, context, provider_kind, provider_name, secret_name, **kwargs):
            nonlocal arrivals
            async with arrival_lock:
                arrivals += 1
                if arrivals == 2:
                    barrier.set()
            await barrier.wait()
            return await super().resolve(context, provider_kind, provider_name, secret_name, **kwargs)

    resolver = _RacingResolver(
        database=secrets_database,
        settings=SecretSettings(allow_platform_fallback=True),
        keyring=keyring,
        envelope=SecretEnvelopeService(keyring),
        platform_lookup=None,
    )

    openai_task = resolver.resolve_credentials(
        context,
        provider_name="openai",
        base_url="https://openai.example/v1",
        platform_value="OPENAI_KEY",
    )
    anthropic_task = resolver.resolve_credentials(
        context,
        provider_name="anthropic",
        base_url="https://anthropic.example/v1",
        platform_value="ANTHROPIC_KEY",
    )

    openai_credentials, anthropic_credentials = await asyncio.gather(openai_task, anthropic_task)

    with openai_credentials.api_key.reveal() as plaintext:
        assert bytes(plaintext) == b"OPENAI_KEY"
    with anthropic_credentials.api_key.reveal() as plaintext:
        assert bytes(plaintext) == b"ANTHROPIC_KEY"


@pytest.mark.asyncio
async def test_keyring_referenced_versions_fail_closed_with_repository_counts(
    secrets_database: Database,
) -> None:
    context = await _seed_scope(secrets_database, slug="alpha")
    await _store_secret(secrets_database, context, key_version=1)

    async with TenantUnitOfWork(secrets_database, context) as uow:
        counts = await uow.secrets.count_key_versions()

    keyring = DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": base64.b64encode(
            json.dumps(
                {
                    "active_key_version": 3,
                    "keys": {
                        "3": base64.b64encode(bytes(range(32))).decode("ascii"),
                    },
                }
            ).encode("utf-8")
        ).decode("ascii")},
    )

    with pytest.raises(SecretKeyringError, match="missing referenced key versions"):
        keyring.require_versions(counts)
