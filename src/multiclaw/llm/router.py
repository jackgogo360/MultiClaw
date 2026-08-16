import asyncio
import json
import logging
from collections.abc import AsyncIterator
from enum import Enum
import inspect

import httpx

logger = logging.getLogger(__name__)

from multiclaw.config.settings import Settings
from multiclaw.llm.providers import (
    LLMResponse,
    ProviderAdapter,
    OpenAIAdapter,
    AnthropicAdapter,
)
from multiclaw.secrets.resolver import ResolvedCredentials, SecretBytes


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
    def __init__(self, settings: Settings, *, credential_resolver=None) -> None:
        self._settings = settings
        self._capability_tags: dict[str, list[str]] = settings.llm.capability_tags
        self._credential_resolver = credential_resolver
        self._provider_configs: dict[str, dict[str, object]] = {}
        self._model_provider: dict[str, str] = {}

        for provider_name, provider_config in settings.llm.providers.items():
            adapter_cls = _PROVIDER_MAP.get(provider_name)
            if adapter_cls:
                self._provider_configs[provider_name] = {
                    "adapter_cls": adapter_cls,
                    "base_url": provider_config.get("base_url", ""),
                }
                for model in self._capability_tags:
                    self._model_provider[model] = provider_name

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
        provider = self._model_provider.get(model) or self._settings.llm.default_provider
        config = self._provider_configs.get(provider)
        if not config:
            return None
        adapter_cls = config["adapter_cls"]
        return adapter_cls(api_key="", base_url=str(config["base_url"]))

    # ------------------------------------------------------------------
    # non-streaming
    # ------------------------------------------------------------------

    async def completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        credentials: ResolvedCredentials | None = None,
    ) -> LLMResponse:
        provider = self._model_provider.get(model) or self._settings.llm.default_provider
        adapter = self.get_adapter(model)
        if adapter is None:
            raise ValueError(f"No adapter found for model '{model}'")
        resolved = await self._resolve_credentials(provider, credentials)
        try:
            request = self._build_request(adapter, resolved, model, messages, tools or [])
            _log_request(request)

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    request["url"],
                    headers=request["headers"],
                    json=request["body"],
                )
            _log_response(response)
            response.raise_for_status()
            parsed = adapter.parse_response(response.json())
            logger.info(
                "LLM response: content=%s tool_calls=%s reasoning=%d",
                _truncate(parsed.content, 300),
                [tc.name for tc in parsed.tool_calls],
                len(parsed.reasoning_content),
            )
            return parsed
        finally:
            resolved.close()

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------

    async def stream_completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        credentials: ResolvedCredentials | None = None,
    ) -> AsyncIterator[dict]:
        """Stream LLM response, yielding {'type':'token','content':...} or
        {'type':'tool_calls','calls':[...]} at the end."""
        provider = self._model_provider.get(model) or self._settings.llm.default_provider
        adapter = self.get_adapter(model)
        if adapter is None:
            raise ValueError(f"No adapter found for model '{model}'")
        resolved = await self._resolve_credentials(provider, credentials)
        request = self._build_request(adapter, resolved, model, messages, tools or [], stream=True)
        _log_request(request)

        token_count = 0
        tool_calls_acc: dict[int, dict] = {}
        reasoning_content = ""
        full_text = ""

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
                async with client.stream(
                    "POST", request["url"], headers=request["headers"], json=request["body"]
                ) as response:
                    status = getattr(response, "status_code", 0)
                    logger.info("response status=%s", status)
                    if isinstance(status, int) and status >= 400:
                        try:
                            body = await response.aread()
                            logger.error("LLM error response body: %s", _truncate(body.decode(errors="replace"), 2000))
                        except Exception:
                            pass
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        delta = self._extract_stream_delta(chunk)
                        if delta is None:
                            continue

                        if delta.get("reasoning_content"):
                            rc = delta["reasoning_content"]
                            reasoning_content += rc
                            yield {"type": "reasoning", "content": rc}

                        if delta.get("content"):
                            token_count += 1
                            full_text += delta["content"]
                            yield {"type": "token", "content": delta["content"]}

                        for tc_delta in delta.get("tool_calls") or []:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": "",
                                    "name": "",
                                    "arguments": "",
                                }
                            entry = tool_calls_acc[idx]
                            if tc_delta.get("id"):
                                entry["id"] = tc_delta["id"]
                            if tc_delta.get("function", {}).get("name"):
                                entry["name"] = tc_delta["function"]["name"]
                            if tc_delta.get("function", {}).get("arguments"):
                                entry["arguments"] += tc_delta["function"]["arguments"]
        finally:
            resolved.close()

        logger.info(
            "stream done: tokens=%d tool_calls=%d reasoning=%d text_len=%d",
            token_count, len(tool_calls_acc), len(reasoning_content), len(full_text),
        )
        if full_text:
            logger.info("LLM response text: %s", _truncate(full_text, 1000))
        if reasoning_content:
            logger.info("LLM reasoning: %s", _truncate(reasoning_content, 500))

        if tool_calls_acc:
            calls = []
            for idx in sorted(tool_calls_acc):
                entry = tool_calls_acc[idx]
                try:
                    args = json.loads(entry["arguments"]) if entry["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}
                calls.append(
                    {"id": entry["id"], "name": entry["name"], "arguments": args}
                )
            logger.info("LLM tool_calls: %s", json.dumps(calls, ensure_ascii=False))
            yield {"type": "tool_calls", "calls": calls, "reasoning_content": reasoning_content}

    @staticmethod
    def _extract_stream_delta(chunk: dict) -> dict | None:
        choices = chunk.get("choices", [])
        if not choices:
            return None
        return choices[0].get("delta")

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
        return ResolvedCredentials(
            provider_name=provider_name,
            source="platform",
            base_url=str(config["base_url"]),
            api_key=SecretBytes(
                str(
                    self._settings.llm.providers.get(provider_name, {}).get("api_key", "")
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
    ) -> dict:
        adapter_cls = type(adapter)
        with credentials.api_key.reveal() as api_key:
            configured = adapter_cls(
                api_key=bytes(api_key).decode("utf-8"),
                base_url=credentials.base_url,
            )
            return configured.build_request(model, messages, tools, stream=stream)


# ------------------------------------------------------------------
# request / response logging
# ------------------------------------------------------------------

def _log_request(request: dict) -> None:
    body = request["body"]
    msgs = body.get("messages", [])
    tools = body.get("tools", [])
    logger.info(
        "LLM request -> %s model=%s messages=%d tools=%d stream=%s",
        request["url"],
        body.get("model", "?"),
        len(msgs),
        len(tools),
        body.get("stream", False),
    )
    # Log role summary for each message
    for i, msg in enumerate(msgs):
        role = msg.get("role", "?")
        content = msg.get("content")
        tc = msg.get("tool_calls")
        tc_id = msg.get("tool_call_id")
        if tc:
            names = [t.get("function", {}).get("name", "?") for t in tc]
            logger.info("  msg[%d] role=%s tool_calls=%s", i, role, names)
        elif tc_id:
            logger.info("  msg[%d] role=%s tool_call_id=%s content=%s", i, role, tc_id, _truncate(str(content), 200))
        else:
            logger.info("  msg[%d] role=%s content=%s", i, role, _truncate(str(content), 200))
    if tools:
        tool_names = [t.get("function", {}).get("name", "?") for t in tools]
        logger.info("  tools sent: %s", tool_names)
    logger.debug("  full body: %s", _truncate(json.dumps(body, ensure_ascii=False), 4000))


def _log_response(response) -> None:
    status = getattr(response, "status_code", 0)
    logger.info("LLM response <- status=%s", status)
    if isinstance(status, int) and status >= 400:
        try:
            body = response.text
        except Exception:
            body = "<unavailable>"
        logger.error("LLM error <- body: %s", _truncate(str(body), 2000))
