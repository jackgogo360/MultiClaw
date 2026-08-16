import asyncio
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text

from multiclaw.cli import alembic_config
from multiclaw.config.settings import DatabaseSettings, SecretSettings
from multiclaw.secrets.envelope import EnvelopeFields, SecretEnvelopeService
from multiclaw.secrets.keyring import DeploymentKeyring, SecretKeyringError
from multiclaw.secrets.rotation import SecretRotationService
from multiclaw.secrets.resolver import SecretResolver
from multiclaw.storage import Database
from multiclaw.storage.repositories.secrets import SecretsRepository
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy.context import TenantContext


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'secret-rotation.db'}"


async def _upgrade_database(database_url: str) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config(database_url=database_url), "head")


@pytest.fixture
async def rotation_database(tmp_path: Path):
    database_url = _sqlite_url(tmp_path)
    await _upgrade_database(database_url)
    database = Database.create(DatabaseSettings(driver="sqlite", url=database_url))
    try:
        yield database
    finally:
        await database.dispose()


async def _seed_scope(database: Database) -> TenantContext:
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
            {"tenant_id": tenant_id, "email": "tenant@example.com"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO workspaces (id, tenant_id, slug, name, status, created_at, updated_at)
                VALUES (:workspace_id, :tenant_id, 'default', 'Default', 'active', 1, 1)
                """
            ),
            {"tenant_id": tenant_id, "workspace_id": workspace_id},
        )
        await conn.execute(
            text("UPDATE users SET default_workspace_id = :workspace_id WHERE id = :tenant_id"),
            {"tenant_id": tenant_id, "workspace_id": workspace_id},
        )
    return TenantContext(tenant_id=tenant_id, workspace_id=workspace_id)


def _keyring_payload(include_old: bool = True) -> str:
    keys = {"3": base64.b64encode(bytes(range(32))).decode("ascii")}
    if include_old:
        keys["1"] = base64.b64encode(bytes([7]) * 32).decode("ascii")
    return base64.b64encode(
        json.dumps({"active_key_version": 3, "keys": keys}).encode("utf-8")
    ).decode("ascii")


def _keyring(include_old: bool = True) -> DeploymentKeyring:
    return DeploymentKeyring.load(
        SecretSettings(),
        environ={"MULTICLAW_SECRETS_KEYRING_B64": _keyring_payload(include_old=include_old)},
    )


async def _store_old_secret(database: Database, context: TenantContext, *, name: str) -> str:
    keyring = _keyring()
    envelope = SecretEnvelopeService(keyring, nonce_source=lambda _length: os.urandom(12))
    secret_id = str(uuid4())
    record = envelope.encrypt(
        f"value-for-{name}".encode("utf-8"),
        EnvelopeFields(
            tenant_id=context.tenant_id,
            workspace_id=None,
            secret_id=secret_id,
            provider_kind="llm",
            provider_name="openai",
            secret_name=name,
        ),
        key_version=1,
    )

    async with TenantUnitOfWork(database, context) as uow:
        await uow.secrets.put_encrypted(
            secret_id=secret_id,
            provider_kind="llm",
            provider_name="openai",
            secret_name=name,
            record=record,
        )
    return secret_id


@pytest.mark.asyncio
async def test_rotation_reencrypts_old_key_versions_with_new_nonce(rotation_database: Database) -> None:
    context = await _seed_scope(rotation_database)
    await _store_old_secret(rotation_database, context, name="api_key")

    resolver = SecretResolver(
        database=rotation_database,
        settings=SecretSettings(),
        keyring=_keyring(),
        envelope=SecretEnvelopeService(_keyring()),
    )
    before = await resolver.resolve(context, "llm", "openai", "api_key")
    with before.reveal() as plaintext:
        assert bytes(plaintext) == b"value-for-api_key"

    async with TenantUnitOfWork(rotation_database, context) as uow:
        row_before = await uow.secrets.get_encrypted("llm", "openai", "api_key")
        counts_before = await uow.secrets.count_key_versions()

    service = SecretRotationService(
        database=rotation_database,
        keyring=_keyring(),
        envelope=SecretEnvelopeService(_keyring(), nonce_source=lambda _length: os.urandom(12)),
    )
    result = await service.rotate_batch(limit=100)

    async with TenantUnitOfWork(rotation_database, context) as uow:
        row_after = await uow.secrets.get_encrypted("llm", "openai", "api_key")
        counts_after = await uow.secrets.count_key_versions()

    assert result.rotated == 1
    assert row_before is not None and row_after is not None
    assert row_before.record.key_version == 1
    assert row_after.record.key_version == 3
    assert row_after.record.nonce != row_before.record.nonce
    assert counts_before == {1: 1}
    assert counts_after == {3: 1}


@pytest.mark.asyncio
async def test_rotation_is_idempotent_and_recovers_on_retry(rotation_database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    context = await _seed_scope(rotation_database)
    await _store_old_secret(rotation_database, context, name="api_key")
    await _store_old_secret(rotation_database, context, name="secondary_key")

    envelope = SecretEnvelopeService(_keyring(), nonce_source=lambda _length: os.urandom(12))
    service = SecretRotationService(
        database=rotation_database,
        keyring=_keyring(),
        envelope=envelope,
    )

    original_encrypt = envelope.encrypt
    seen = {"count": 0}

    def flaky_encrypt(*args, **kwargs):
        seen["count"] += 1
        if seen["count"] == 2:
            raise RuntimeError("boom")
        return original_encrypt(*args, **kwargs)

    monkeypatch.setattr(envelope, "encrypt", flaky_encrypt)
    first = await service.rotate_batch(limit=100)
    monkeypatch.setattr(envelope, "encrypt", original_encrypt)
    second = await service.rotate_batch(limit=100)
    third = await service.rotate_batch(limit=100)

    assert first.rotated == 1
    assert first.failed == 1
    assert second.rotated == 1
    assert third.rotated == 0


@pytest.mark.asyncio
async def test_rotation_skips_cas_conflicts_without_corrupting_rows(
    rotation_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await _seed_scope(rotation_database)
    await _store_old_secret(rotation_database, context, name="api_key")

    original = SecretsRepository.compare_and_swap_rotation
    calls = {"count": 0}

    async def conflict_once(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return False
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(SecretsRepository, "compare_and_swap_rotation", conflict_once)

    service = SecretRotationService(
        database=rotation_database,
        keyring=_keyring(),
        envelope=SecretEnvelopeService(_keyring(), nonce_source=lambda _length: os.urandom(12)),
    )

    first = await service.rotate_batch(limit=100)
    second = await service.rotate_batch(limit=100)

    assert first.skipped == 1
    assert second.rotated == 1


@pytest.mark.asyncio
async def test_rotation_reference_counts_gate_old_key_removal(rotation_database: Database) -> None:
    context = await _seed_scope(rotation_database)
    await _store_old_secret(rotation_database, context, name="api_key")

    async with TenantUnitOfWork(rotation_database, context) as uow:
        counts_before = await uow.secrets.count_key_versions()

    with pytest.raises(SecretKeyringError, match="missing referenced key versions"):
        _keyring(include_old=False).require_versions(counts_before)

    service = SecretRotationService(
        database=rotation_database,
        keyring=_keyring(),
        envelope=SecretEnvelopeService(_keyring(), nonce_source=lambda _length: os.urandom(12)),
    )
    await service.rotate_batch(limit=100)

    async with TenantUnitOfWork(rotation_database, context) as uow:
        counts_after = await uow.secrets.count_key_versions()

    _keyring(include_old=False).require_versions(counts_after)
