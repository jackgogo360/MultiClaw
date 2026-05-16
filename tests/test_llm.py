import pytest
from multiclaw.config.settings import Settings
from multiclaw.llm.providers import ProviderAdapter, OpenAIAdapter, AnthropicAdapter
from multiclaw.llm.router import ModelRouter, CapabilityTag


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
        assert adapter.api_key == "test-key"

    def test_completion_returns_mock_response(self, router):
        result = router.completion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hello"}],
        )

        assert result.content != ""
        assert result.role == "assistant"


class TestCapabilityTag:
    def test_capability_tags_are_strings(self):
        assert CapabilityTag.TEXT == "text"
        assert CapabilityTag.FUNCTION_CALLING == "function_calling"
        assert CapabilityTag.VISION == "vision"
        assert CapabilityTag.EXTENDED_THINKING == "extended_thinking"