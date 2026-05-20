import json
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class ToolCall(BaseModel):
    id: str = ""
    name: str
    arguments: dict[str, Any] = {}


class LLMResponse(BaseModel):
    content: str
    role: str = "assistant"
    tool_calls: list[ToolCall] = []
    reasoning_content: str = ""


class ProviderAdapter(ABC):
    def __init__(self, api_key: str = "", base_url: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    def build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool = False,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def parse_response(self, raw: dict[str, Any]) -> LLMResponse: ...

    def parse_stream_chunk(self, raw: dict[str, Any]) -> str | None:
        return None


class OpenAIAdapter(ProviderAdapter):
    def build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return {
            "url": f"{self.base_url}/chat/completions",
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "body": body,
        }

    def parse_response(self, raw: dict[str, Any]) -> LLMResponse:
        choice = raw["choices"][0]["message"]
        tool_calls = []
        if choice.get("tool_calls"):
            for tc in choice["tool_calls"]:
                tool_calls.append(
                    ToolCall(
                        id=tc.get("id", ""),
                        name=tc["function"]["name"],
                        arguments=json.loads(tc["function"]["arguments"]),
                    )
                )
        return LLMResponse(
            content=choice.get("content") or "",
            role=choice.get("role", "assistant"),
            tool_calls=tool_calls,
            reasoning_content=choice.get("reasoning_content", ""),
        )

    def parse_stream_chunk(self, raw: dict[str, Any]) -> str | None:
        choices = raw.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            return delta.get("content")
        return None


class AnthropicAdapter(ProviderAdapter):
    def build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": 4096,
        }
        if tools:
            body["tools"] = tools
        return {
            "url": f"{self.base_url}/v1/messages",
            "headers": {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            "body": body,
        }

    def parse_response(self, raw: dict[str, Any]) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in raw.get("content", []):
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input", {}),
                    )
                )
        return LLMResponse(
            content="\n".join(text_parts),
            role="assistant",
            tool_calls=tool_calls,
        )