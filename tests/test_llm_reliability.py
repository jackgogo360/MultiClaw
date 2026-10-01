import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from multiclaw.llm import LLMProviderError, LLMResponseParseError, ModelRouter
from multiclaw.llm.providers import AnthropicAdapter, OpenAIAdapter
from multiclaw.secrets.resolver import ResolvedCredentials, SecretBytes


def settings(**overrides):
    llm = {
        "default_provider": "openai",
        "default_model": "gpt",
        "capability_tags": {"gpt": ["text"], "claude": ["text"]},
        "providers": {
            "openai": {"base_url": "https://openai.example/v1"},
            "anthropic": {"base_url": "https://anthropic.example"},
        },
        "model_providers": {},
        "max_retries": 2,
        "retry_base_seconds": 0,
        "request_timeout_seconds": 60,
        "stream_timeout_seconds": 300,
    }
    llm.update(overrides)
    return SimpleNamespace(llm=SimpleNamespace(**llm))


def credentials(provider="openai"):
    return ResolvedCredentials(
        provider_name=provider,
        source="user",
        base_url="https://provider.example",
        api_key=SecretBytes(b"credential-canary"),
    )


def test_explicit_mapping_selects_each_provider_and_unknown_fails_closed():
    router = ModelRouter(
        settings(
            model_providers={
                "gpt": "openai",
                "claude": "anthropic",
                "broken": "missing",
            }
        )
    )
    assert isinstance(router.get_adapter("gpt"), OpenAIAdapter)
    assert isinstance(router.get_adapter("claude"), AnthropicAdapter)
    assert router.get_adapter("broken") is None


def test_unmapped_models_use_default_provider_not_last_provider():
    assert isinstance(ModelRouter(settings()).get_adapter("gpt"), OpenAIAdapter)


def test_custom_provider_requires_explicit_known_adapter():
    configured = settings(
        providers={
            "custom": {"adapter": "openai", "base_url": "https://custom.example/v1"}
        },
        default_provider="custom",
    )
    assert isinstance(ModelRouter(configured).get_adapter("gpt"), OpenAIAdapter)
    configured.llm.providers["custom"]["adapter"] = "unsupported"
    assert ModelRouter(configured).get_adapter("gpt") is None


def test_anthropic_normalizes_system_tools_and_tool_results_without_mutation():
    messages = [
        {"role": "system", "content": "System instruction"},
        {"role": "user", "content": "Read"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "one",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                },
                {
                    "id": "two",
                    "function": {"name": "read", "arguments": '{"path":"b"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "one", "content": "a result"},
        {"role": "tool", "tool_call_id": "two", "content": "b result"},
    ]
    original = json.dumps(messages)
    body = AnthropicAdapter(base_url="https://anthropic.example/v1/").build_request(
        "claude",
        messages,
        [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read file",
                    "parameters": {"type": "object"},
                },
            }
        ],
        stream=True,
    )
    assert body["url"] == "https://anthropic.example/v1/messages"
    assert body["body"]["stream"] is True
    assert body["body"]["system"] == [{"type": "text", "text": "System instruction"}]
    assert body["body"]["tools"] == [
        {"name": "read", "description": "Read file", "input_schema": {"type": "object"}}
    ]
    assert body["body"]["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "one",
        "name": "read",
        "input": {"path": "a"},
    }
    assert body["body"]["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "one", "content": "a result"},
            {"type": "tool_result", "tool_use_id": "two", "content": "b result"},
        ],
    }
    assert json.dumps(messages) == original


@pytest.mark.parametrize(
    "adapter,raw",
    [
        (
            OpenAIAdapter(),
            {
                "choices": [{"message": {"content": "hi"}}],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                },
            },
        ),
        (
            AnthropicAdapter(),
            {
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "thinking", "thinking": "reason", "signature": "sig"},
                ],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        ),
    ],
)
def test_response_normalizes_usage_and_anthropic_reasoning(adapter, raw):
    response = adapter.parse_response(raw)
    assert response.usage == {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
    if isinstance(adapter, AnthropicAdapter):
        assert response.reasoning_content == "reason"
        assert response.reasoning_blocks == [raw["content"][1]]
        request = adapter.build_request(
            "claude",
            [
                {
                    "role": "assistant",
                    "content": "hi",
                    "reasoning_blocks": response.reasoning_blocks,
                }
            ],
            [],
        )
        assert request["body"]["messages"][0]["content"][0] == raw["content"][1]


class StreamResponse:
    def __init__(self, events=(), failure=None, status=200):
        self.events, self.failure, self.status_code = events, failure, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(
                self.status_code,
                request=httpx.Request(
                    "POST", "https://secret.example/?token=url-canary"
                ),
                text="body-canary",
            )
            response.raise_for_status()

    async def aiter_lines(self):
        for event in self.events:
            yield "data: " + json.dumps(event)
        if self.failure:
            raise self.failure


class Client:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, *args, **kwargs):
        self.calls += 1
        return next(self.responses)

    async def post(self, *args, **kwargs):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


async def collect(router, creds):
    return [
        event
        async for event in router.stream_completion(
            "claude", [{"role": "user", "content": "prompt-canary"}], credentials=creds
        )
    ]


async def test_anthropic_stream_normalizes_text_tools_reasoning_and_usage():
    events = [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 5, "output_tokens": 1}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reason"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "hello"},
        },
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "call",
                "name": "read",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '"a"}'},
        },
        {"type": "message_delta", "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ]
    client = Client([StreamResponse(events), StreamResponse(events)])
    router = ModelRouter(settings(model_providers={"claude": "anthropic"}))
    with patch("httpx.AsyncClient", return_value=client):
        for _ in range(2):
            creds = credentials("anthropic")
            output = await collect(router, creds)
            assert {"type": "token", "content": "hello"} in output
            assert {"type": "reasoning", "content": "reason"} in output
            assert output[-1]["calls"] == [
                {"id": "call", "name": "read", "arguments": {"path": "a"}}
            ]
            assert output[-1]["reasoning_blocks"] == [
                {"type": "thinking", "thinking": "reason", "signature": "sig"}
            ]
            assert [e for e in output if e["type"] == "usage"][-1]["usage"] == {
                "input_tokens": 5,
                "output_tokens": 3,
                "total_tokens": 8,
            }
            assert creds.api_key.is_zeroized()


@pytest.mark.parametrize(
    "failure",
    [
        429,
        503,
        httpx.ConnectError("transport-canary"),
        httpx.ReadTimeout("timeout-canary"),
    ],
)
async def test_completion_retries_transient_errors_with_bounded_attempts(failure):
    request = httpx.Request("POST", "https://provider.example")
    failed = (
        httpx.Response(failure, request=request)
        if isinstance(failure, int)
        else failure
    )
    success = httpx.Response(
        200, request=request, json={"choices": [{"message": {"content": "ok"}}]}
    )
    client = Client([failed, failed, success])
    creds = credentials()
    with patch("httpx.AsyncClient", return_value=client):
        result = await ModelRouter(settings()).completion("gpt", [], credentials=creds)
    assert result.content == "ok"
    assert client.calls == 3
    assert creds.api_key.is_zeroized()


async def test_stream_retries_only_before_any_event_delivery_and_sanitizes_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="multiclaw.llm.router")
    client = Client(
        [
            StreamResponse(status=503),
            StreamResponse(
                [{"choices": [{"delta": {"content": "hello"}}]}],
                httpx.ReadError("transport-canary"),
            ),
        ]
    )
    creds = credentials()
    output = []
    with (
        patch("httpx.AsyncClient", return_value=client),
        pytest.raises(LLMProviderError) as exc,
    ):
        async for event in ModelRouter(settings()).stream_completion(
            "gpt", [{"role": "user", "content": "prompt-canary"}], credentials=creds
        ):
            output.append(event)
    assert output == [{"type": "token", "content": "hello"}]
    assert client.calls == 2
    assert creds.api_key.is_zeroized()
    assert exc.value.__context__ is None
    for canary in [
        "credential-canary",
        "transport-canary",
        "body-canary",
        "url-canary",
        "prompt-canary",
    ]:
        assert canary not in caplog.text


async def test_invalid_stream_tool_json_fails_instead_of_executing_empty_arguments():
    client = Client(
        [
            StreamResponse(
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call",
                                            "function": {
                                                "name": "read",
                                                "arguments": "{",
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ]
            )
        ]
    )
    creds = credentials()
    with (
        patch("httpx.AsyncClient", return_value=client),
        pytest.raises(LLMResponseParseError),
    ):
        await collect(ModelRouter(settings()), creds)
    assert creds.api_key.is_zeroized()


async def test_stream_request_build_failure_closes_credentials():
    creds = credentials("anthropic")
    with (
        patch.object(
            ModelRouter, "_build_request", side_effect=ValueError("invalid request")
        ),
        pytest.raises(ValueError),
    ):
        await collect(
            ModelRouter(settings(model_providers={"claude": "anthropic"})), creds
        )
    assert creds.api_key.is_zeroized()


@pytest.mark.parametrize("status,attempts", [(401, 1), (429, 3), (503, 3)])
async def test_completion_retry_budget_and_permanent_failures(status, attempts):
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://provider.example"),
        text="body-canary",
    )
    client = Client([response] * 4)
    creds = credentials()
    with (
        patch("httpx.AsyncClient", return_value=client),
        pytest.raises(LLMProviderError) as exc,
    ):
        await ModelRouter(settings()).completion("gpt", [], credentials=creds)
    assert client.calls == attempts
    assert exc.value.__context__ is None
    assert creds.api_key.is_zeroized()


async def test_stream_usage_only_openai_chunk_is_delivered():
    client = Client(
        [
            StreamResponse(
                [
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 4,
                            "completion_tokens": 2,
                            "total_tokens": 6,
                        },
                    }
                ]
            )
        ]
    )
    with patch("httpx.AsyncClient", return_value=client):
        output = await collect(ModelRouter(settings()), credentials())
    assert output == [
        {
            "type": "usage",
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }
    ]


@pytest.mark.parametrize("method", ["completion", "stream_completion"])
async def test_unknown_mapping_closes_explicit_credentials_without_http(method):
    creds = credentials()
    router = ModelRouter(settings(model_providers={"claude": "missing"}))
    with patch("httpx.AsyncClient") as factory, pytest.raises(LLMProviderError):
        if method == "completion":
            await router.completion("claude", [], credentials=creds)
        else:
            await collect(router, creds)
    factory.assert_not_called()
    assert creds.api_key.is_zeroized()


async def test_stream_consumer_close_releases_credentials():
    client = Client([StreamResponse([{"choices": [{"delta": {"content": "hello"}}]}])])
    creds = credentials()
    with patch("httpx.AsyncClient", return_value=client):
        stream = ModelRouter(settings()).stream_completion("gpt", [], credentials=creds)
        assert (await anext(stream))["content"] == "hello"
        await stream.aclose()
    assert creds.api_key.is_zeroized()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": "secret-canary"},
        },
        {"choices": [{"message": {"content": "hi"}}], "usage": ["secret-canary"]},
    ],
)
async def test_malformed_usage_is_sanitized(payload):
    response = httpx.Response(
        200, request=httpx.Request("POST", "https://provider.example"), json=payload
    )
    with (
        patch("httpx.AsyncClient", return_value=Client([response])),
        pytest.raises(LLMResponseParseError) as exc,
    ):
        await ModelRouter(settings()).completion("gpt", [], credentials=credentials())
    assert exc.value.__context__ is None
    assert "secret-canary" not in str(exc.value)


def test_anthropic_cached_usage_counts_total_input():
    response = AnthropicAdapter().parse_response(
        {
            "content": [],
            "usage": {
                "input_tokens": 5,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 11,
                "output_tokens": 2,
            },
        }
    )
    assert response.usage == {
        "input_tokens": 23,
        "output_tokens": 2,
        "total_tokens": 25,
    }


@pytest.mark.parametrize(
    "block",
    [
        {"type": "tool_use", "input": {}},
        {"type": "thinking", "thinking": ["secret-canary"], "signature": "sig"},
    ],
)
async def test_malformed_anthropic_known_blocks_are_sanitized(block):
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://provider.example"),
        json={"content": [block]},
    )
    with (
        patch("httpx.AsyncClient", return_value=Client([response])),
        pytest.raises(LLMResponseParseError),
    ):
        await ModelRouter(settings(model_providers={"claude": "anthropic"})).completion(
            "claude", [], credentials=credentials("anthropic")
        )


@pytest.mark.parametrize(
    "chunk",
    [
        {"choices": [{"delta": {"content": ["secret-canary"]}}]},
        {"choices": [{"delta": {"reasoning_content": {"secret-canary": "value"}}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": ["secret-canary"],
                                    "arguments": "{}",
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": ["secret-canary"]},
    ],
)
async def test_malformed_stream_deltas_are_sanitized(chunk):
    with (
        patch("httpx.AsyncClient", return_value=Client([StreamResponse([chunk])])),
        pytest.raises(LLMResponseParseError),
    ):
        await collect(ModelRouter(settings()), credentials())


async def test_malformed_anthropic_stream_text_is_sanitized():
    chunk = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": ["secret-canary"]},
    }
    with (
        patch("httpx.AsyncClient", return_value=Client([StreamResponse([chunk])])),
        pytest.raises(LLMResponseParseError),
    ):
        await collect(
            ModelRouter(settings(model_providers={"claude": "anthropic"})),
            credentials("anthropic"),
        )


@pytest.mark.parametrize(
    "adapter,field,limit,expected",
    [
        (OpenAIAdapter(), "max_tokens", 123, 123),
        (AnthropicAdapter(), "max_tokens", 123, 123),
        (AnthropicAdapter(), "max_tokens", 9000, 4096),
    ],
)
def test_adapters_limit_generated_tokens(adapter, field, limit, expected):
    body = adapter.build_request("model", [], [], max_output_tokens=limit)["body"]
    assert body[field] == expected


@pytest.mark.parametrize("method", ["completion", "stream_completion"])
async def test_router_forwards_output_token_limit(method):
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://provider.example"),
        json={"choices": [{"message": {"content": "ok"}}]},
    )
    client = Client([response if method == "completion" else StreamResponse()])
    router = ModelRouter(settings())
    assert router.supports_output_limit is True
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch.object(
            OpenAIAdapter, "build_request", wraps=OpenAIAdapter().build_request
        ) as build,
    ):
        if method == "completion":
            await router.completion(
                "gpt", [], credentials=credentials(), max_output_tokens=123
            )
        else:
            _ = [
                event
                async for event in router.stream_completion(
                    "gpt", [], credentials=credentials(), max_output_tokens=123
                )
            ]
    assert build.call_args.kwargs["max_output_tokens"] == 123


@pytest.mark.parametrize("adapter", [OpenAIAdapter(), AnthropicAdapter()])
def test_output_token_limit_rejects_nonpositive_values(adapter):
    with pytest.raises(ValueError, match="positive"):
        adapter.build_request("model", [], [], max_output_tokens=0)
