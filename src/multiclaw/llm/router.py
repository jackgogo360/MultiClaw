from enum import Enum

from multiclaw.config.settings import Settings
from multiclaw.llm.providers import (
    LLMResponse,
    ProviderAdapter,
    OpenAIAdapter,
    AnthropicAdapter,
)


class CapabilityTag(str, Enum):
    TEXT = "text"
    FUNCTION_CALLING = "function_calling"
    VISION = "vision"
    EXTENDED_THINKING = "extended_thinking"


_PROVIDER_MAP: dict[str, type[ProviderAdapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
}


class ModelRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._capability_tags: dict[str, list[str]] = settings.llm.capability_tags
        self._adapters: dict[str, ProviderAdapter] = {}
        self._model_provider: dict[str, str] = {}

        for provider_name, provider_config in settings.llm.providers.items():
            adapter_cls = _PROVIDER_MAP.get(provider_name)
            if adapter_cls:
                adapter = adapter_cls(
                    api_key=provider_config.get("api_key", ""),
                    base_url=provider_config.get("base_url", ""),
                )
                self._adapters[provider_name] = adapter
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
        return self._adapters.get(provider)

    def completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            content='{"action": "mock_response", "message": "This is a mock LLM response"}',
            role="assistant",
        )