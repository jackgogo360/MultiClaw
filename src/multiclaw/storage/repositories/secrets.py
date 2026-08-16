from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from multiclaw.secrets.envelope import EncryptedSecretRecord
from multiclaw.storage.dialect import MySQLDialect, SQLiteDialect
from multiclaw.storage.schema import user_secrets
from multiclaw.tenancy.context import TenantContext


Dialect = SQLiteDialect | MySQLDialect


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    secret_id: str
    workspace_id: str | None
    provider_kind: str
    provider_name: str
    secret_name: str
    masked_value: str
    key_version: int
    created_at: int
    updated_at: int
    rotated_at: int | None


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    secret_id: str
    workspace_id: str | None
    provider_kind: str
    provider_name: str
    secret_name: str
    masked_value: str
    record: EncryptedSecretRecord
    created_at: int
    updated_at: int
    rotated_at: int | None


def _validate_name(name: str, value: str, max_length: int) -> str:
    if not value or len(value) > max_length:
        raise ValueError(f"{name} is invalid")
    return value


def _mask_value(secret_name: str) -> str:
    suffix = secret_name[-4:] if len(secret_name) >= 4 else secret_name
    return f"****{suffix}"


@dataclass(slots=True)
class SecretsRepository:
    _conn: AsyncConnection
    _dialect: Dialect
    _context: TenantContext

    @property
    def connection(self) -> AsyncConnection:
        return self._conn

    async def put_encrypted(
        self,
        *,
        secret_id: str,
        provider_kind: str,
        provider_name: str,
        secret_name: str,
        record: EncryptedSecretRecord,
    ) -> SecretMetadata:
        provider_kind = _validate_name("provider_kind", provider_kind, 32)
        provider_name = _validate_name("provider_name", provider_name, 128)
        secret_name = _validate_name("secret_name", secret_name, 128)
        secret_id = _validate_name("secret_id", secret_id, 36)

        existing = await self.get_encrypted(provider_kind, provider_name, secret_name)
        now_ms = self._dialect.db_now_ms()
        values = dict(
            id=secret_id,
            tenant_id=self._context.tenant_id,
            workspace_id=None,
            provider_kind=provider_kind,
            provider_name=provider_name,
            secret_name=secret_name,
            key_provider_name=record.key_provider_name,
            format_version=record.format_version,
            algorithm=record.algorithm,
            key_version=record.key_version,
            nonce=record.nonce,
            ciphertext=record.ciphertext,
            updated_at=now_ms,
        )
        if existing is None:
            await self._conn.execute(
                insert(user_secrets).values(
                    **values,
                    created_at=now_ms,
                    rotated_at=None,
                )
            )
        else:
            await self._conn.execute(
                update(user_secrets)
                .where(
                    user_secrets.c.tenant_id == self._context.tenant_id,
                    user_secrets.c.provider_kind == provider_kind,
                    user_secrets.c.provider_name == provider_name,
                    user_secrets.c.secret_name == secret_name,
                )
                .values(**values)
            )
        metadata = await self.get_metadata(provider_kind, provider_name, secret_name)
        assert metadata is not None
        return metadata

    async def get_metadata(
        self,
        provider_kind: str,
        provider_name: str,
        secret_name: str,
    ) -> SecretMetadata | None:
        row = await self._fetch_row(provider_kind, provider_name, secret_name)
        if row is None:
            return None
        return SecretMetadata(
            secret_id=str(row["id"]),
            workspace_id=row["workspace_id"],
            provider_kind=str(row["provider_kind"]),
            provider_name=str(row["provider_name"]),
            secret_name=str(row["secret_name"]),
            masked_value=_mask_value(str(row["secret_name"])),
            key_version=int(row["key_version"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            rotated_at=int(row["rotated_at"]) if row["rotated_at"] is not None else None,
        )

    async def get_encrypted(
        self,
        provider_kind: str,
        provider_name: str,
        secret_name: str,
    ) -> EncryptedSecret | None:
        row = await self._fetch_row(provider_kind, provider_name, secret_name)
        return None if row is None else self.from_mapping(row)

    async def count_key_versions(self) -> dict[int, int]:
        result = await self._conn.execute(
            select(user_secrets.c.key_version, func.count())
            .where(user_secrets.c.tenant_id == self._context.tenant_id)
            .group_by(user_secrets.c.key_version)
        )
        return {int(version): int(count) for version, count in result.all()}

    async def compare_and_swap_rotation(
        self,
        secret_id: str,
        expected_record: EncryptedSecretRecord,
        new_record: EncryptedSecretRecord,
    ) -> bool:
        result = await self._conn.execute(
            update(user_secrets)
            .where(
                user_secrets.c.tenant_id == self._context.tenant_id,
                user_secrets.c.id == secret_id,
                user_secrets.c.workspace_id.is_(None),
                user_secrets.c.key_version == expected_record.key_version,
                user_secrets.c.nonce == expected_record.nonce,
                user_secrets.c.ciphertext == expected_record.ciphertext,
            )
            .values(
                key_provider_name=new_record.key_provider_name,
                format_version=new_record.format_version,
                algorithm=new_record.algorithm,
                key_version=new_record.key_version,
                nonce=new_record.nonce,
                ciphertext=new_record.ciphertext,
                updated_at=self._dialect.db_now_ms(),
                rotated_at=self._dialect.db_now_ms(),
            )
        )
        return bool(result.rowcount)

    async def _fetch_row(
        self,
        provider_kind: str,
        provider_name: str,
        secret_name: str,
    ):
        result = await self._conn.execute(
            select(user_secrets).where(
                user_secrets.c.tenant_id == self._context.tenant_id,
                user_secrets.c.provider_kind == provider_kind,
                user_secrets.c.provider_name == provider_name,
                user_secrets.c.secret_name == secret_name,
            )
        )
        return result.mappings().first()

    def from_mapping(self, row) -> EncryptedSecret:
        return EncryptedSecret(
            secret_id=str(row["id"]),
            workspace_id=row["workspace_id"],
            provider_kind=str(row["provider_kind"]),
            provider_name=str(row["provider_name"]),
            secret_name=str(row["secret_name"]),
            masked_value=_mask_value(str(row["secret_name"])),
            record=EncryptedSecretRecord(
                key_provider_name=str(row["key_provider_name"]),
                format_version=int(row["format_version"]),
                algorithm=str(row["algorithm"]),
                key_version=int(row["key_version"]),
                nonce=bytes(row["nonce"]),
                ciphertext=bytes(row["ciphertext"]),
            ),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            rotated_at=int(row["rotated_at"]) if row["rotated_at"] is not None else None,
        )
