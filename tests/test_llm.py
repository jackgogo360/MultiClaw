import pytest
from unittest.mock import AsyncMock, Mock, patch

from multiclaw.config.settings import Settings
from multiclaw.llm.providers import ProviderAdapter, OpenAIAdapter, AnthropicAdapter
from multiclaw.llm.router import ModelRouter, CapabilityTag
from multiclaw.secrets.resolver import ResolvedCredentials, SecretBytes


class TestProviderAdapters:
    def test_openai_adapter_formats_request(self):
        adapter = OpenAIAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
        request = adapter.build_request(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )

        assert request["url"] == "https://api.openai.com/v1/chat/completions"
        assert request["headers"]["Authorization"] == "Bearer sk-test"
        assert request["headers"]["Content-Type"] == "application/json"
        assert request["body"]["model"] == "gpt-4o-mini"
        assert request["body"]["messages"] == [{"role": "user", "content": "hello"}]

    def test_anthropic_adapter_formats_request(self):
        adapter = AnthropicAdapter(api_key="ant-test", base_url="https://api.anthropic.com")
        request = adapter.build_request(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
        )

        assert request["url"] == "https://api.anthropic.com/v1/messages"
        assert request["headers"]["x-api-key"] == "ant-test"
        assert request["headers"]["anthropic-version"] == "2023-06-01"
        assert request["body"]["model"] == "claude-sonnet-4-6"

    def test_openai_adapter_parses_response(self):
        adapter = OpenAIAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
        raw = {
            "choices": [{"message": {"role": "assistant", "content": "hi there"}}],
        }
        result = adapter.parse_response(raw)

        assert result.content == "hi there"
        assert result.role == "assistant"
        assert result.tool_calls == []

    def test_openai_adapter_parses_tool_call_response(self):
        adapter = OpenAIAdapter(api_key="sk-test", base_url="https://api.openai.com/v1")
        raw = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "function": {"name": "read_file", "arguments": '{"path": "/tmp/x"}'},
                            }
                        ],
                    }
                }
            ],
        }
        result = adapter.parse_response(raw)

        assert result.content == ""
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"
        assert result.tool_calls[0].arguments == {"path": "/tmp/x"}

    def test_anthropic_adapter_parses_response(self):
        adapter = AnthropicAdapter(api_key="ant-test", base_url="https://api.anthropic.com")
        raw = {
            "content": [{"type": "text", "text": "hello from claude"}],
        }
        result = adapter.parse_response(raw)

        assert result.content == "hello from claude"
        assert result.role == "assistant"


class TestModelRouter:
    @pytest.fixture
    def router(self, test_config_path):
        settings = Settings(_config_file=str(test_config_path))
        return ModelRouter(settings)

    def test_router_loads_capability_tags(self, router):
        tags = router.list_models()
        assert "gpt-4o-mini" in tags
        assert router.has_capability("gpt-4o-mini", CapabilityTag.TEXT)

    def test_route_selects_model_with_required_capability(self, router):
        model = router.route(required=[CapabilityTag.TEXT])

        assert model in ("gpt-4o-mini",)

    def test_route_returns_default_when_no_constraints(self, router):
        model = router.route()

        assert model == "gpt-4o-mini"

    def test_route_raises_when_no_model_has_capability(self, router):
        with pytest.raises(ValueError, match="No model found"):
            router.route(required=[CapabilityTag.VISION])

    def test_get_adapter_returns_correct_provider(self, router):
        adapter = router.get_adapter("gpt-4o-mini")

        assert isinstance(adapter, OpenAIAdapter)

    @pytest.mark.asyncio
    async def test_completion_makes_http_call_and_parses_response(self, router):
        mock_http_response = Mock()
        mock_http_response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "hi from openai"}}]
        }
        mock_http_response.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_http_response

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await router.completion(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hello"}],
            )

        assert result.content == "hi from openai"
        assert result.role == "assistant"
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_completion_uses_per_call_credentials_and_zeroizes(self, router):
        mock_http_response = Mock()
        mock_http_response.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "tenant secret ok"}}]
        }
        mock_http_response.raise_for_status = Mock()

        captured: dict[str, dict] = {}

        async def fake_post(url, *, headers, json):
            captured["request"] = {"url": url, "headers": headers, "body": json}
            return mock_http_response

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.side_effect = fake_post
        credentials = ResolvedCredentials(
            provider_name="openai",
            source="user",
            base_url="https://tenant.example/v1",
            api_key=SecretBytes(b"tenant-secret-key"),
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await router.completion(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hello"}],
                credentials=credentials,
            )

        assert result.content == "tenant secret ok"
        assert captured["request"]["url"] == "https://tenant.example/v1/chat/completions"
        assert captured["request"]["headers"]["Authorization"] == "Bearer tenant-secret-key"
        assert credentials.api_key.is_zeroized()
        assert "tenant-secret-key" not in repr(router.__dict__)

    @pytest.mark.asyncio
    async def test_stream_completion_uses_per_call_credentials_and_zeroizes(self, router):
        chunks = [
            'data: {"choices":[{"delta":{"content":"hello"}}]}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "data: [DONE]",
        ]
        captured: dict[str, dict] = {}

        class _StreamResponse:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                for chunk in chunks:
                    yield chunk

            async def aread(self):
                return b""

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url, *, headers, json):
                captured["request"] = {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "body": json,
                }
                return _StreamResponse()

        credentials = ResolvedCredentials(
            provider_name="openai",
            source="user",
            base_url="https://tenant.example/v1",
            api_key=SecretBytes(b"tenant-stream-key"),
        )

        with patch("httpx.AsyncClient", return_value=_Client()):
            events = [
                event
                async for event in router.stream_completion(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "hello"}],
                    credentials=credentials,
                )
            ]

        assert [event["content"] for event in events if event["type"] == "token"] == ["hello", " world"]
        assert captured["request"]["headers"]["Authorization"] == "Bearer tenant-stream-key"
        assert credentials.api_key.is_zeroized()


class TestCapabilityTag:
    def test_capability_tags_are_strings(self):
        assert CapabilityTag.TEXT == "text"
        assert CapabilityTag.FUNCTION_CALLING == "function_calling"
        assert CapabilityTag.VISION == "vision"
        assert CapabilityTag.EXTENDED_THINKING == "extended_thinking"
