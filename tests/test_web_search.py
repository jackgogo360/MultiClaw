"""Tests for WebSearchTool."""
import pytest
from multiclaw.tools.web_search import WebSearchParams, WebSearchToolBuilder


class TestWebSearchTool:
    @pytest.mark.asyncio
    async def test_web_search_rejects_empty_query(self):
        builder = WebSearchToolBuilder()
        result = await builder.build(
            builder.validate({"query": ""})
        ).execute()
        assert result.status == "error"
        assert "empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_web_search_returns_error_for_unknown_engine(self):
        builder = WebSearchToolBuilder()
        result = await builder.build(
            builder.validate({"query": "test", "engine": "nonexistent"})
        ).execute()
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_web_search_default_engine_is_duckduckgo(self):
        builder = WebSearchToolBuilder()
        assert builder.engine == "duckduckgo"

    @pytest.mark.asyncio
    async def test_web_search_accepts_all_known_engines(self):
        for engine in ["duckduckgo", "bing", "baidu"]:
            builder = WebSearchToolBuilder(engine=engine)
            assert builder.engine == engine
