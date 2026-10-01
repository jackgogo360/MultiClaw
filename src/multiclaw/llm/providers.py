import json
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ProviderResponseModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class _UsageEnvelope(_ProviderResponseModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)


class _OpenAIFunctionEnvelope(_ProviderResponseModel):
    name: str
    arguments: str


class _OpenAIToolCallEnvelope(_ProviderResponseModel):
    id: str = ""
    function: _OpenAIFunctionEnvelope


class _OpenAIMessageEnvelope(_ProviderResponseModel):
    content: str | None = None
    role: str = "assistant"
    tool_calls: list[_OpenAIToolCallEnvelope] | None = None
    reasoning_content: str = ""


class _OpenAIChoiceEnvelope(_ProviderResponseModel):
    message: _OpenAIMessageEnvelope


class _OpenAIResponseEnvelope(_ProviderResponseModel):
    choices: list[_OpenAIChoiceEnvelope] = Field(min_length=1)
    usage: _UsageEnvelope | None = None


class _OpenAIStreamFunction(_ProviderResponseModel):
    name: str | None = None
    arguments: str | None = None


class _OpenAIStreamToolCall(_ProviderResponseModel):
    index: int = Field(default=0, ge=0)
    id: str | None = None
    function: _OpenAIStreamFunction | None = None


class _OpenAIStreamDelta(_ProviderResponseModel):
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[_OpenAIStreamToolCall] | None = None


class _OpenAIStreamChoice(_ProviderResponseModel):
    delta: _OpenAIStreamDelta = Field(default_factory=_OpenAIStreamDelta)


class _OpenAIStreamEnvelope(_ProviderResponseModel):
    choices: list[_OpenAIStreamChoice] = Field(default_factory=list)
    usage: _UsageEnvelope | None = None


class _AnthropicContentBlockEnvelope(_ProviderResponseModel):
    type: str
    text: str | None = None
    id: str = ""
    name: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    thinking: str | None = None
    signature: str | None = None
    data: str | None = None

    @model_validator(mode="after")
    def validate_known_block(self) -> "_AnthropicContentBlockEnvelope":
        if self.type == "text" and self.text is None:
            raise ValueError("text content is required")
        if self.type == "tool_use" and (not self.id or not self.name):
            raise ValueError("tool id and name are required")
        if self.type == "thinking" and (
            self.thinking is None or self.signature is None
        ):
            raise ValueError("thinking content and signature are required")
        if self.type == "redacted_thinking" and self.data is None:
            raise ValueError("redacted thinking data is required")
        return self


class _AnthropicResponseEnvelope(_ProviderResponseModel):
    content: list[_AnthropicContentBlockEnvelope]
    usage: _UsageEnvelope | None = None


class _AnthropicStreamDelta(_ProviderResponseModel):
    type: str = ""
    text: str | None = None
    thinking: str | None = None
    partial_json: str | None = None
    signature: str | None = None


class _AnthropicStreamMessage(_ProviderResponseModel):
    usage: _UsageEnvelope | None = None


class _AnthropicStreamEnvelope(_ProviderResponseModel):
    type: str
    index: int | None = Field(default=None, ge=0)
    delta: _AnthropicStreamDelta | None = None
    content_block: _AnthropicContentBlockEnvelope | None = None
    message: _AnthropicStreamMessage | None = None
    usage: _UsageEnvelope | None = None


class ToolCall(BaseModel):
    id: str = ""
    name: str
    arguments: dict[str, Any] = {}


class LLMResponse(BaseModel):
    content: str
    role: str = "assistant"
    tool_calls: list[ToolCall] = []
    reasoning_content: str = ""
    reasoning_blocks: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)


def normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
    usage = _UsageEnvelope.model_validate(raw, strict=True)
    input_tokens = (
        usage.input_tokens if usage.input_tokens is not None else usage.prompt_tokens
    )
    output_tokens = (
        usage.output_tokens
        if usage.output_tokens is not None
        else usage.completion_tokens
    )
    # Cached Anthropic input is reported separately from uncached input.
    input_tokens += usage.cache_creation_input_tokens + usage.cache_read_input_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.total_tokens
        if usage.total_tokens is not None
        else input_tokens + output_tokens,
    }


@dataclass
class StreamState:
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    reasoning_content: str = ""
    reasoning_blocks: dict[int, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)

    def finish(self) -> list[dict[str, Any]]:
        if not self.tool_calls:
            return []
        calls = []
        for index in sorted(self.tool_calls):
            entry = self.tool_calls[index]
            arguments = (
                json.loads(entry["arguments"])
                if entry["arguments"]
                else entry.get("input", {})
            )
            if not isinstance(arguments, dict) or not entry["name"]:
                raise ValueError("invalid tool call")
            calls.append(
                {"id": entry["id"], "name": entry["name"], "arguments": arguments}
            )
        event = {
            "type": "tool_calls",
            "calls": calls,
            "reasoning_content": self.reasoning_content,
        }
        if self.reasoning_blocks:
            event["reasoning_blocks"] = [
                self.reasoning_blocks[i] for i in sorted(self.reasoning_blocks)
            ]
        return [event]


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
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def parse_response(self, raw: dict[str, Any]) -> LLMResponse: ...

    def validate_response_payload(self, raw: object) -> None:
        _ProviderResponseModel.model_validate(raw, strict=True)

    def parse_stream_chunk(self, raw: dict[str, Any]) -> str | None:
        return None

    def parse_stream_events(
        self, raw: dict[str, Any], state: StreamState
    ) -> list[dict[str, Any]]:
        _OpenAIStreamEnvelope.model_validate(raw, strict=True)
        events = []
        if raw.get("usage"):
            events.append({"type": "usage", "usage": normalize_usage(raw["usage"])})
        choices = raw.get("choices", [])
        delta = choices[0].get("delta", {}) if choices else {}
        if delta.get("reasoning_content"):
            state.reasoning_content += delta["reasoning_content"]
            events.append({"type": "reasoning", "content": delta["reasoning_content"]})
        if delta.get("content"):
            events.append({"type": "token", "content": delta["content"]})
        for tool in delta.get("tool_calls") or []:
            entry = state.tool_calls.setdefault(
                tool.get("index", 0), {"id": "", "name": "", "arguments": ""}
            )
            if tool.get("id"):
                entry["id"] = tool["id"]
            function = tool.get("function") or {}
            if function.get("name"):
                entry["name"] = function["name"]
            entry["arguments"] += function.get("arguments") or ""
        return events


class OpenAIAdapter(ProviderAdapter):
    def build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool = False,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if max_output_tokens is not None:
            if max_output_tokens <= 0:
                raise ValueError("max_output_tokens must be positive")
            body["max_tokens"] = max_output_tokens
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return {
            "url": f"{self.base_url.rstrip('/')}/chat/completions",
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
            usage=normalize_usage(raw["usage"]) if raw.get("usage") else {},
        )

    def validate_response_payload(self, raw: object) -> None:
        _OpenAIResponseEnvelope.model_validate(raw, strict=True)

    def parse_stream_chunk(self, raw: dict[str, Any]) -> str | None:
        choices = raw.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            return delta.get("content")
        return None


class AnthropicAdapter(ProviderAdapter):
    @staticmethod
    def _content_blocks(content: Any) -> list[dict[str, Any]]:
        if content is None or content == "":
            return []
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        return deepcopy(content)

    def build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool = False,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        normalized_messages: list[dict[str, Any]] = []
        system: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]
            if role in {"system", "developer"}:
                system.extend(self._content_blocks(message.get("content")))
                continue
            if role == "tool":
                role = "user"
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": message["tool_call_id"],
                        "content": message.get("content") or "",
                    }
                ]
            else:
                blocks = self._content_blocks(message.get("content"))
                if role == "assistant":
                    blocks = deepcopy(message.get("reasoning_blocks", [])) + blocks
                    for tool in message.get("tool_calls") or []:
                        function = tool["function"]
                        arguments = function["arguments"]
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                        if not isinstance(arguments, dict):
                            raise TypeError("invalid tool arguments")
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tool["id"],
                                "name": function["name"],
                                "input": arguments,
                            }
                        )
            if normalized_messages and normalized_messages[-1]["role"] == role:
                normalized_messages[-1]["content"].extend(blocks)
            else:
                normalized_messages.append({"role": role, "content": blocks})
        body: dict[str, Any] = {
            "model": model,
            "messages": normalized_messages,
            "max_tokens": min(4096, max_output_tokens)
            if max_output_tokens is not None
            else 4096,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = []
            for tool in tools:
                if tool.get("type") == "function":
                    function = tool["function"]
                    normalized = {
                        "name": function["name"],
                        "input_schema": deepcopy(
                            function.get("parameters", {"type": "object"})
                        ),
                    }
                    if "description" in function:
                        normalized["description"] = function["description"]
                    body["tools"].append(normalized)
                else:
                    body["tools"].append(deepcopy(tool))
        if stream:
            body["stream"] = True
        base_url = self.base_url.rstrip("/")
        return {
            "url": f"{base_url}/messages"
            if base_url.endswith("/v1")
            else f"{base_url}/v1/messages",
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
        reasoning_blocks = []
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
            elif block["type"] in {"thinking", "redacted_thinking"}:
                reasoning_blocks.append(deepcopy(block))
        return LLMResponse(
            content="\n".join(text_parts),
            role="assistant",
            tool_calls=tool_calls,
            reasoning_content="".join(
                block.get("thinking", "") for block in reasoning_blocks
            ),
            reasoning_blocks=reasoning_blocks,
            usage=normalize_usage(raw["usage"]) if raw.get("usage") else {},
        )

    def validate_response_payload(self, raw: object) -> None:
        _AnthropicResponseEnvelope.model_validate(raw, strict=True)

    def parse_stream_events(
        self, raw: dict[str, Any], state: StreamState
    ) -> list[dict[str, Any]]:
        _AnthropicStreamEnvelope.model_validate(raw, strict=True)
        events = []
        event_type = raw.get("type")
        if event_type in {"message_start", "message_delta"}:
            usage = (
                raw.get("message", {}).get("usage")
                if event_type == "message_start"
                else raw.get("usage")
            )
            if usage:
                state.usage.update(usage)
                events.append({"type": "usage", "usage": normalize_usage(state.usage)})
        elif event_type == "content_block_start":
            block = raw["content_block"]
            index = raw["index"]
            if block["type"] == "tool_use":
                state.tool_calls[index] = {
                    "id": block["id"],
                    "name": block["name"],
                    "arguments": "",
                    "input": block.get("input", {}),
                }
            elif block["type"] in {"thinking", "redacted_thinking"}:
                state.reasoning_blocks[index] = deepcopy(block)
                if block.get("thinking"):
                    state.reasoning_content += block["thinking"]
                    events.append({"type": "reasoning", "content": block["thinking"]})
            elif block["type"] == "text" and block.get("text"):
                events.append({"type": "token", "content": block["text"]})
        elif event_type == "content_block_delta":
            delta = raw["delta"]
            index = raw["index"]
            if delta["type"] == "text_delta" and delta.get("text"):
                events.append({"type": "token", "content": delta["text"]})
            elif delta["type"] == "input_json_delta":
                state.tool_calls[index]["arguments"] += delta["partial_json"]
            elif delta["type"] == "thinking_delta":
                text = delta["thinking"]
                state.reasoning_content += text
                state.reasoning_blocks[index]["thinking"] += text
                if text:
                    events.append({"type": "reasoning", "content": text})
            elif delta["type"] == "signature_delta":
                state.reasoning_blocks[index]["signature"] = (
                    state.reasoning_blocks[index].get("signature", "")
                    + delta["signature"]
                )
        return events
