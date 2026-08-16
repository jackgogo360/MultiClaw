from __future__ import annotations

import inspect
from contextlib import contextmanager
from dataclasses import dataclass

from multiclaw.config.settings import SecretSettings
from multiclaw.storage.engine import Database
from multiclaw.tenancy.context import TenantContext

from .envelope import EnvelopeFields, SecretEnvelopeError, SecretEnvelopeService
from .keyring import DeploymentKeyring


class SecretNotConfiguredError(RuntimeError):
    pass


class UserSecretInvalidError(RuntimeError):
    pass


class SecretBytes:
    def __init__(self, value: bytes | bytearray) -> None:
        self._buffer = bytearray(value)
        self._closed = False

    @contextmanager
    def reveal(self):
        if self._closed:
            raise RuntimeError("secret bytes are no longer available")
        try:
            yield memoryview(self._buffer)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._closed = True

    def is_zeroized(self) -> bool:
        return self._closed and all(value == 0 for value in self._buffer)

    def __repr__(self) -> str:
        return "<SecretBytes masked>"


@dataclass(slots=True)
class ResolvedSecret:
    provider_kind: str
    provider_name: str
    secret_name: str
    source: str
    masked_value: str
    secret_bytes: SecretBytes

    def reveal(self):
        return self.secret_bytes.reveal()

    def close(self) -> None:
        self.secret_bytes.close()

    def __repr__(self) -> str:
        return (
            f"ResolvedSecret(provider_kind={self.provider_kind!r}, "
            f"provider_name={self.provider_name!r}, secret_name={self.secret_name!r}, "
            f"source={self.source!r}, masked_value={self.masked_value!r})"
        )


@dataclass(slots=True)
class ResolvedCredentials:
    provider_name: str
    source: str
    base_url: str
    api_key: SecretBytes

    def close(self) -> None:
        self.api_key.close()


class SecretResolver:
    def __init__(
        self,
        *,
        database: Database,
        settings: SecretSettings,
        keyring: DeploymentKeyring,
        envelope: SecretEnvelopeService | None = None,
        platform_lookup=None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._keyring = keyring
        self._envelope = envelope or SecretEnvelopeService(keyring)
        self._platform_lookup = platform_lookup

    async def resolve(
        self,
        context: TenantContext,
        provider_kind: str,
        provider_name: str,
        secret_name: str,
    ) -> ResolvedSecret:
        from multiclaw.storage.uow import TenantUnitOfWork

        async with TenantUnitOfWork(self._database, context) as uow:
            row = await uow.secrets.get_encrypted(provider_kind, provider_name, secret_name)
        if row is not None:
            try:
                plaintext = self._envelope.decrypt(
                    row.record,
                    EnvelopeFields(
                        tenant_id=context.tenant_id,
                        workspace_id=row.workspace_id,
                        secret_id=row.secret_id,
                        provider_kind=provider_kind,
                        provider_name=provider_name,
                        secret_name=secret_name,
                    ),
                )
            except SecretEnvelopeError as exc:
                raise UserSecretInvalidError("user secret is invalid") from exc
            return ResolvedSecret(
                provider_kind=provider_kind,
                provider_name=provider_name,
                secret_name=secret_name,
                source="user",
                masked_value=row.masked_value,
                secret_bytes=SecretBytes(plaintext),
            )

        if not self._settings.allow_platform_fallback:
            raise SecretNotConfiguredError("secret is not configured")
        fallback = await self._lookup_platform_secret(provider_kind, provider_name, secret_name)
        if not fallback:
            raise SecretNotConfiguredError("secret is not configured")
        return ResolvedSecret(
            provider_kind=provider_kind,
            provider_name=provider_name,
            secret_name=secret_name,
            source="platform",
            masked_value=_mask_value(secret_name),
            secret_bytes=SecretBytes(fallback.encode("utf-8")),
        )

    async def resolve_credentials(
        self,
        context: TenantContext,
        *,
        provider_name: str,
        base_url: str,
        platform_value: str = "",
    ) -> ResolvedCredentials:
        previous_lookup = self._platform_lookup
        if platform_value and previous_lookup is None:
            self._platform_lookup = lambda *_args: platform_value
        try:
            resolved = await self.resolve(context, "llm", provider_name, "api_key")
        finally:
            self._platform_lookup = previous_lookup
        return ResolvedCredentials(
            provider_name=provider_name,
            source=resolved.source,
            base_url=base_url,
            api_key=resolved.secret_bytes,
        )

    async def resolve_reference(self, context: TenantContext, reference: str) -> ResolvedSecret:
        provider_kind, provider_name, secret_name = parse_secret_reference(reference)
        return await self.resolve(context, provider_kind, provider_name, secret_name)

    async def _lookup_platform_secret(
        self,
        provider_kind: str,
        provider_name: str,
        secret_name: str,
    ) -> str | None:
        if self._platform_lookup is None:
            return None
        value = self._platform_lookup(provider_kind, provider_name, secret_name)
        if inspect.isawaitable(value):
            value = await value
        if value is None:
            return None
        return str(value)


def parse_secret_reference(reference: str) -> tuple[str, str, str]:
    if not reference.startswith("secret://"):
        raise SecretNotConfiguredError("invalid secret reference")
    parts = reference[len("secret://") :].split("/")
    if len(parts) != 3 or not all(parts):
        raise SecretNotConfiguredError("invalid secret reference")
    return parts[0], parts[1], parts[2]


def _mask_value(secret_name: str) -> str:
    suffix = secret_name[-4:] if len(secret_name) >= 4 else secret_name
    return f"****{suffix}"
