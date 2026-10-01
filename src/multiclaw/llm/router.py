from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator
from enum import Enum
from typing import TYPE_CHECKING, Protocol, cast

import httpx
from pydantic import ValidationError

logger = logging.getLogger(__name__)

from multiclaw.llm.providers import (
    AnthropicAdapter,
    LLMResponse,
    OpenAIAdapter,
    ProviderAdapter,
    StreamState,
)

if TYPE_CHECKING:
    from multiclaw.config.settings import Settings
    from multiclaw.secrets.resolver import ResolvedCredentials


class LLMProviderError(RuntimeError):
    pass


class LLMResponseParseError(RuntimeError):
    pass


class CompletionRouter(Protocol):
    async def completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse: ...


class CapabilityTag(str, Enum):
    TEXT = "text"
    FUNCTION_CALLING = "function_calling"
    VISION = "vision"
    EXTENDED_THINKING = "extended_thinking"
    STREAMING = "streaming"


_PROVIDER_MAP: dict[str, type[ProviderAdapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "deepseek": OpenAIAdapter,
}


def _truncate(s: str, n: int = 500) -> str:
    return s if len(s) <= n else s[:n] + "...<truncated>"


class ModelRouter:
    supports_output_limit = True

    def __init__(self, settings: Settings, *, credential_resolver=None) -> None:
        self._settings = settings
        self._capability_tags: dict[str, list[str]] = settings.llm.capability_tags
        self._credential_resolver = credential_resolver
        self._provider_configs: dict[str, dict[str, object]] = {}
        self._model_provider: dict[str, str] = dict(
            getattr(settings.llm, "model_providers", {})
        )

        for provider_name, provider_config in settings.llm.providers.items():
            adapter_cls = _PROVIDER_MAP.get(
                provider_config.get("adapter", provider_name)
            )
            if adapter_cls:
                self._provider_configs[provider_name] = {
                    "adapter_cls": adapter_cls,
                    "base_url": provider_config.get("base_url", ""),
                }

    def list_models(self) -> list[str]:
        return list(self._capability_tags)

    def has_capability(self, model: str, capability: CapabilityTag) -> bool:
        tags = self._capability_tags.get(model, [])
        return capability.value in tags

    def route(
        self,
        required: list[CapabilityTag] | None = None,
        preferred: list[CapabilityTag] | None = None,
    ) -> str:
        required = required or []
        candidates = [
            model
            for model, tags in self._capability_tags.items()
            if all(c.value in tags for c in required)
        ]
        if not candidates:
            raise ValueError(
                f"No model found with required capabilities: {[c.value for c in required]}"
            )
        return candidates[0]

    def get_adapter(self, model: str) -> ProviderAdapter | None:
        provider = self._provider_for_model(model)
        config = self._provider_configs.get(provider)
        if not config:
            return None
        adapter_cls = cast(type[ProviderAdapter], config["adapter_cls"])
        return adapter_cls(api_key="", base_url=str(config["base_url"]))

    def _provider_for_model(self, model: str) -> str:
        # Explicit mappings are authoritative, including unknown provider names.
        if model in self._model_provider:
            return self._model_provider[model]
        default = self._settings.llm.default_provider
        if default in self._provider_configs:
            return default
        if len(self._settings.llm.providers) == 1:
            return next(iter(self._settings.llm.providers))
        return default

    def _retryable(self, error: httpx.HTTPError) -> bool:
        if isinstance(error, httpx.HTTPStatusError):
            return (
                error.response.status_code == 429
                or 500 <= error.response.status_code <= 599
            )
        return isinstance(error, (httpx.ConnectError, httpx.TimeoutException))

    async def _retry_delay(self, attempt: int) -> None:
        await asyncio.sleep(
            getattr(self._settings.llm, "retry_base_seconds", 0.25) * 2**attempt
        )

    async def _credentials_for_call(self, provider: str, explicit):
        from multiclaw.secrets.resolver import (
            SecretNotConfiguredError,
            UserSecretInvalidError,
        )

        unavailable = False
        try:
            resolved = await self._resolve_credentials(provider, explicit)
        except (SecretNotConfiguredError, UserSecretInvalidError):
            unavailable = True
        if unavailable:
            raise LLMProviderError("LLM provider unavailable")
        return resolved

    async def completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        credentials: ResolvedCredentials | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        resolved = credentials
        try:
            provider = self._provider_for_model(model)
            adapter = self.get_adapter(model)
            if adapter is None:
                raise LLMProviderError("LLM provider unavailable")
            resolved = await self._credentials_for_call(provider, credentials)
            request = self._build_request(
                adapter,
                resolved,
                model,
                messages,
                tools or [],
                **(
                    {"max_output_tokens": max_output_tokens}
                    if max_output_tokens is not None
                    else {}
                ),
            )
            _log_request(request)
            max_retries = getattr(self._settings.llm, "max_retries", 2)
            timeout = getattr(self._settings.llm, "request_timeout_seconds", 60.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                for attempt in range(max_retries + 1):
                    failed = False
                    retry = False
                    try:
                        response = await client.post(
                            request["url"],
                            headers=request["headers"],
                            json=request["body"],
                        )
                        _log_response(response)
                        response.raise_for_status()
                    except httpx.HTTPError as error:
                        failed = True
                        retry = attempt < max_retries and self._retryable(error)
                    if not failed:
                        break
                    if not retry:
                        raise LLMProviderError("LLM provider unavailable")
                    await self._retry_delay(attempt)
            invalid = False
            try:
                raw = response.json()
                adapter.validate_response_payload(raw)
                parsed = adapter.parse_response(raw)
            except (json.JSONDecodeError, ValidationError):
                invalid = True
            if invalid:
                raise LLMResponseParseError("invalid LLM response")
            logger.info(
                "LLM response: tool_calls=%d reasoning_length=%d text_length=%d",
                len(parsed.tool_calls),
                len(parsed.reasoning_content),
                len(parsed.content),
            )
            return parsed
        finally:
            if resolved is not None:
                resolved.close()

    async def stream_completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        credentials: ResolvedCredentials | None = None,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[dict]:
        """Yield normalized token, reasoning, usage and final tool-call events."""
        resolved = credentials
        delivered = False
        try:
            provider = self._provider_for_model(model)
            adapter = self.get_adapter(model)
            if adapter is None:
                raise LLMProviderError("LLM provider unavailable")
            resolved = await self._credentials_for_call(provider, credentials)
            request = self._build_request(
                adapter,
                resolved,
                model,
                messages,
                tools or [],
                stream=True,
                **(
                    {"max_output_tokens": max_output_tokens}
                    if max_output_tokens is not None
                    else {}
                ),
            )
            _log_request(request)
            max_retries = getattr(self._settings.llm, "max_retries", 2)
            timeout = getattr(self._settings.llm, "stream_timeout_seconds", 300.0)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(timeout, 30.0))
            ) as client:
                for attempt in range(max_retries + 1):
                    state = StreamState()
                    failed = False
                    retry = False
                    stream_error = False
                    malformed = False
                    try:
                        async with client.stream(
                            "POST",
                            request["url"],
                            headers=request["headers"],
                            json=request["body"],
                        ) as response:
                            _log_response(response)
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].lstrip()
                                if data.strip() == "[DONE]":
                                    break
                                if not data:
                                    continue
                                try:
                                    chunk = json.loads(data)
                                    if not isinstance(chunk, dict):
                                        raise TypeError("invalid stream payload")
                                    if chunk.get("type") == "error" or chunk.get(
                                        "error"
                                    ):
                                        stream_error = True
                                        break
                                    events = adapter.parse_stream_events(chunk, state)
                                except (
                                    ValueError,
                                    KeyError,
                                    TypeError,
                                    ValidationError,
                                ):
                                    malformed = True
                                    break
                                for event in events:
                                    delivered = True
                                    yield event
                    except httpx.HTTPError as error:
                        failed = True
                        retry = (
                            not delivered
                            and attempt < max_retries
                            and self._retryable(error)
                        )
                    if malformed:
                        raise LLMResponseParseError("invalid LLM response")
                    if stream_error:
                        raise LLMProviderError("LLM provider unavailable")
                    if failed:
                        if not retry:
                            raise LLMProviderError("LLM provider unavailable")
                        await self._retry_delay(attempt)
                        continue
                    invalid = False
                    try:
                        final_events = state.finish()
                    except (ValueError, ValidationError):
                        invalid = True
                    if invalid:
                        raise LLMResponseParseError("invalid LLM response")
                    for event in final_events:
                        delivered = True
                        yield event
                    break
        finally:
            if resolved is not None:
                resolved.close()

    @staticmethod
    def _extract_stream_delta(chunk: dict) -> dict | None:
        choices = chunk.get("choices", [])
        return choices[0].get("delta") if choices else None

    async def _resolve_credentials(
        self,
        provider_name: str,
        explicit: ResolvedCredentials | None,
    ) -> ResolvedCredentials:
        if explicit is not None:
            return explicit
        if self._credential_resolver is not None:
            resolved = self._credential_resolver(provider_name)
            if inspect.isawaitable(resolved):
                resolved = await resolved
            return resolved
        config = self._provider_configs.get(provider_name)
        if not config:
            raise ValueError(f"No provider config found for '{provider_name}'")
        from multiclaw.secrets.resolver import ResolvedCredentials, SecretBytes

        return ResolvedCredentials(
            provider_name=provider_name,
            source="platform",
            base_url=str(config["base_url"]),
            api_key=SecretBytes(
                str(
                    self._settings.llm.providers.get(provider_name, {}).get(
                        "api_key", ""
                    )
                ).encode("utf-8")
            ),
        )

    @staticmethod
    def _build_request(
        adapter: ProviderAdapter,
        credentials: ResolvedCredentials,
        model: str,
        messages: list[dict],
        tools: list[dict],
        *,
        stream: bool = False,
        max_output_tokens: int | None = None,
    ) -> dict:
        adapter_cls = type(adapter)
        with credentials.api_key.reveal() as api_key:
            configured = adapter_cls(
                api_key=bytes(api_key).decode("utf-8"),
                base_url=credentials.base_url,
            )
            if max_output_tokens is not None:
                return configured.build_request(
                    model,
                    messages,
                    tools,
                    stream=stream,
                    max_output_tokens=max_output_tokens,
                )
            return configured.build_request(model, messages, tools, stream=stream)


# ------------------------------------------------------------------
# request / response logging
# ------------------------------------------------------------------


def _log_request(request: dict) -> None:
    body = request["body"]
    # URLs, message text, tool arguments and response bodies may contain secrets.
    logger.info(
        "LLM request: messages=%d tools=%d stream=%s",
        len(body.get("messages", [])),
        len(body.get("tools", [])),
        body.get("stream", False),
    )


def _log_response(response) -> None:
    status = getattr(response, "status_code", 0)
    logger.info("LLM response <- status=%s", status)
    if isinstance(status, int) and status >= 400:
        logger.error("LLM error response body omitted")


__all__ = [
    "CapabilityTag",
    "CompletionRouter",
    "LLMProviderError",
    "LLMResponseParseError",
    "ModelRouter",
]
