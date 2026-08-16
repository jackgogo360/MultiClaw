from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.secrets import SecretsRepository
from multiclaw.storage.schema import user_secrets
from multiclaw.tenancy.context import TenantContext

from .envelope import EnvelopeFields, SecretEnvelopeService
from .keyring import DeploymentKeyring


@dataclass(frozen=True, slots=True)
class RotationResult:
    rotated: int = 0
    skipped: int = 0
    failed: int = 0


class SecretRotationService:
    def __init__(
        self,
        *,
        database: Database,
        keyring: DeploymentKeyring,
        envelope: SecretEnvelopeService | None = None,
    ) -> None:
        self._database = database
        self._keyring = keyring
        self._envelope = envelope or SecretEnvelopeService(keyring)

    async def rotate_batch(self, limit: int = 100) -> RotationResult:
        rotated = 0
        skipped = 0
        failed = 0

        async with self._database.write_transaction() as conn:
            rows = (
                await conn.execute(
                    select(user_secrets)
                    .where(user_secrets.c.key_version != self._keyring.active_key_version)
                    .order_by(user_secrets.c.created_at.asc(), user_secrets.c.id.asc())
                    .limit(limit)
                )
            ).mappings().all()

            for row in rows:
                repository = SecretsRepository(
                    conn,
                    self._database.dialect,
                    TenantContext(tenant_id=str(row["tenant_id"]), workspace_id="rotation-scope"),
                )
                encrypted = repository.from_mapping(row)
                try:
                    plaintext = self._envelope.decrypt(
                        encrypted.record,
                        EnvelopeFields(
                            tenant_id=str(row["tenant_id"]),
                            workspace_id=row["workspace_id"],
                            secret_id=str(row["id"]),
                            provider_kind=str(row["provider_kind"]),
                            provider_name=str(row["provider_name"]),
                            secret_name=str(row["secret_name"]),
                        ),
                    )
                    new_record = self._envelope.encrypt(
                        bytes(plaintext),
                        EnvelopeFields(
                            tenant_id=str(row["tenant_id"]),
                            workspace_id=row["workspace_id"],
                            secret_id=str(row["id"]),
                            provider_kind=str(row["provider_kind"]),
                            provider_name=str(row["provider_name"]),
                            secret_name=str(row["secret_name"]),
                        ),
                        key_version=self._keyring.active_key_version,
                    )
                    updated = await repository.compare_and_swap_rotation(
                        encrypted.secret_id,
                        encrypted.record,
                        new_record,
                    )
                    if updated:
                        rotated += 1
                    else:
                        skipped += 1
                except Exception:
                    failed += 1
        return RotationResult(rotated=rotated, skipped=skipped, failed=failed)
