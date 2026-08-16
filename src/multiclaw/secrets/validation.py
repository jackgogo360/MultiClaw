from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from multiclaw.config import Settings
from multiclaw.secrets.resolver import SecretNotConfiguredError, SecretResolver
from multiclaw.tenancy import TenantContext


class UnsupportedSecretValidationTargetError(RuntimeError):
    pass


class InvalidSecretCredentialsError(RuntimeError):
    pass


class SecretCredentialServiceUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SecretValidationResult:
    ok: bool


class SecretCredentialTester:
    def __init__(
        self,
        *,
        resolver: SecretResolver,
        settings: Settings,
        client_factory: Callable[..., Any] = httpx.AsyncClient,
    ) -> None:
        self._resolver = resolver
        self._settings = settings
        self._client_factory = client_factory

    async def validate(
        self,
        context: TenantContext,
        provider_kind: str,
        provider_name: str,
        secret_name: str,
    ) -> SecretValidationResult:
        if provider_kind != "llm" or secret_name != "api_key":
            raise UnsupportedSecretValidationTargetError("unsupported secret validation target")
        if provider_name not in {"openai", "deepseek", "anthropic"}:
            raise UnsupportedSecretValidationTargetError("unsupported secret validation target")

        provider_config = self._settings.llm.providers.get(provider_name, {})
        base_url = str(provider_config.get("base_url", "") or "").strip()
        if not base_url:
            raise SecretCredentialServiceUnavailableError("secret validation unavailable")

        resolved = await self._resolver.resolve(context, provider_kind, provider_name, secret_name)
        try:
            with resolved.reveal() as secret_view:
                api_key = bytes(secret_view).decode("utf-8")
                url, headers = _validation_request(provider_name, base_url, api_key)
                try:
                    async with self._client_factory(timeout=15.0, follow_redirects=False) as client:
                        response = await client.get(url, headers=headers)
                except (asyncio.TimeoutError, httpx.RequestError) as exc:
                    raise SecretCredentialServiceUnavailableError("secret validation unavailable") from exc
        finally:
            resolved.close()

        if response.status_code in {401, 403}:
            raise InvalidSecretCredentialsError("invalid secret credentials")
        if 200 <= response.status_code < 300:
            return SecretValidationResult(ok=True)
        raise SecretCredentialServiceUnavailableError("secret validation unavailable")


def _validation_request(provider_name: str, base_url: str, api_key: str) -> tuple[str, dict[str, str]]:
    normalized = base_url.rstrip("/")
    if provider_name in {"openai", "deepseek"}:
        return (
            f"{normalized}/models",
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
    return (
        f"{normalized}/v1/models",
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        },
    )
