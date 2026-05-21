"""Tests for WebFetchTool."""
import pytest
from multiclaw.tools.web_fetch import WebFetchParams, WebFetchToolBuilder


class TestWebFetchTool:
    @pytest.mark.asyncio
    async def test_web_fetch_rejects_empty_url(self):
        builder = WebFetchToolBuilder()
        result = await builder.build(
            builder.validate({"url": ""})
        ).execute()
        assert result.status == "error"
        assert "empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_web_fetch_default_mode_is_auto(self):
        builder = WebFetchToolBuilder()
        assert builder.mode == "auto"

    @pytest.mark.asyncio
    async def test_web_fetch_rejects_invalid_mode(self):
        builder = WebFetchToolBuilder()
        result = await builder.build(
            builder.validate({"url": "example.com", "mode": "invalid"})
        ).execute()
        assert result.status == "error"
