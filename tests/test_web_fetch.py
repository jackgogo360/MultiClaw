"""Tests for WebFetchTool."""

from __future__ import annotations

import sys
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import ANY

import pytest

from multiclaw.tools.web_fetch import WebFetchToolBuilder


PUBLIC_IPV4 = "93.184.216.34"


class FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def raise_for_status(self) -> None:
        if self.is_error:
            raise RuntimeError(f"http error {self.status_code}")


class FakeClient:
    def __init__(self, responses: dict[str, FakeResponse], raise_on_get: Exception | None = None) -> None:
        self.responses = responses
        self.raise_on_get = raise_on_get
        self.calls: list[tuple[str, bool]] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def get(self, url: str, *, follow_redirects: bool = False) -> FakeResponse:
        self.calls.append((url, follow_redirects))
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.responses[url]


def install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeClient,
) -> None:
    def client_factory(*, timeout, follow_redirects, headers):
        assert timeout == 30.0
        assert follow_redirects is False
        assert headers["User-Agent"]
        return client

    monkeypatch.setitem(
        sys.modules,
        "httpx",
        SimpleNamespace(Client=client_factory),
    )


def install_fake_trafilatura(monkeypatch: pytest.MonkeyPatch, text: str = "Extracted body text") -> None:
    monkeypatch.setitem(
        sys.modules,
        "trafilatura",
        SimpleNamespace(extract=lambda *args, **kwargs: text),
    )


def install_fake_html2text(monkeypatch: pytest.MonkeyPatch, text: str = "Converted markdown") -> None:
    class FakeHTML2Text:
        body_width = 0
        ignore_images = True
        protect_links = True
        unicode_snob = True

        def handle(self, html: str) -> str:
            return text

    monkeypatch.setitem(
        sys.modules,
        "html2text",
        SimpleNamespace(HTML2Text=FakeHTML2Text),
    )


def install_fake_resolver(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    import socket

    def fake_getaddrinfo(host: str, port: int | None, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(f"mock failure for {host}")
        results = []
        for ip in mapping[host]:
            if ":" in ip:
                results.append((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0, 0, 0)))
            else:
                results.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0)))
        return results

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


class FakePlaywrightRequest:
    def __init__(self, url: str | None) -> None:
        self.url = url


class FakePlaywrightRoute:
    def __init__(self, url: str) -> None:
        self.request = FakePlaywrightRequest(url)
        self.actions: list[str] = []

    def continue_(self) -> None:
        self.actions.append("continue")

    def abort(self) -> None:
        self.actions.append("abort")


class FakePage:
    def __init__(
        self,
        context: FakeBrowserContext,
        requests: list[dict[str, object]],
        title: str = "Example",
        html: str = "<title>Example</title><main>Body</main>",
    ) -> None:
        self.context = context
        self.requests = requests
        self._title = title
        self._html = html
        self.goto_calls: list[tuple[str, str, float]] = []

    def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        assert self.context.route_calls == [("**/*", self.context.route_handler)]
        self.goto_calls.append((url, wait_until, timeout))
        for item in self.requests:
            route = FakePlaywrightRoute(item["url"])
            route_request = item.get("route_request", route.request)
            route.request = route_request
            if item.get("explicit_request", False):
                explicit_request = item.get("explicit_request_obj", FakePlaywrightRequest(item["url"]))
                self.context.route_handler(route, explicit_request)
            else:
                self.context.route_handler(route)
            self.context.route_results.append((item["url"], list(route.actions)))
            if item.get("raise_on_abort", False) and route.actions == ["abort"]:
                raise RuntimeError(item.get("raise_message", "navigation blocked"))

    def title(self) -> str:
        return self._title

    def content(self) -> str:
        return self._html


class FakeBrowserContext:
    def __init__(self, requests: list[dict[str, object]]) -> None:
        self.requests = requests
        self.route_calls: list[tuple[str, object]] = []
        self.route_handler = None
        self.route_results: list[tuple[str, list[str]]] = []
        self.browser: FakeBrowser | None = None
        self.page = FakePage(self, requests)

    def route(self, pattern: str, handler) -> None:
        self.route_calls.append((pattern, handler))
        self.route_handler = handler

    def new_page(self) -> FakePage:
        return self.page


class FakeBrowser:
    def __init__(self, context: FakeBrowserContext) -> None:
        self.context = context
        self.closed = False
        self.new_context_kwargs: dict[str, object] | None = None

    def new_context(self, **kwargs) -> FakeBrowserContext:
        self.new_context_kwargs = kwargs
        return self.context

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.launch_kwargs: list[dict[str, object]] = []

    def launch(self, **kwargs) -> FakeBrowser:
        self.launch_kwargs.append(kwargs)
        return self.browser


class FakePlaywrightManager:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium

    def __enter__(self) -> FakePlaywrightManager:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    *,
    requests: list[dict[str, object]],
) -> FakeBrowserContext:
    context = FakeBrowserContext(requests)
    browser = FakeBrowser(context)
    context.browser = browser
    chromium = FakeChromium(browser)

    def sync_playwright():
        return FakePlaywrightManager(chromium)

    playwright_module = ModuleType("playwright")
    sync_api_module = ModuleType("playwright.sync_api")
    sync_api_module.sync_playwright = sync_playwright
    playwright_module.sync_api = sync_api_module
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api_module)
    return context


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

    @pytest.mark.asyncio
    async def test_light_fetch_blocks_private_initial_url_before_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = FakeClient({})
        install_fake_httpx(monkeypatch, client)
        install_fake_trafilatura(monkeypatch)
        install_fake_resolver(monkeypatch, {"internal.example": ["10.0.0.7"]})
        builder = WebFetchToolBuilder(mode="light")

        result = await builder.build(
            builder.validate({"url": "https://internal.example/data", "mode": "light"})
        ).execute()

        assert result.status == "error"
        assert "blocked network target" in result.content.lower()
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_markdown_fetch_blocks_private_initial_url_before_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = FakeClient({})
        install_fake_httpx(monkeypatch, client)
        install_fake_html2text(monkeypatch)
        install_fake_resolver(monkeypatch, {"internal.example": ["10.0.0.7"]})
        builder = WebFetchToolBuilder(mode="markdown")

        result = await builder.build(
            builder.validate({"url": "https://internal.example/data", "mode": "markdown"})
        ).execute()

        assert result.status == "error"
        assert "blocked network target" in result.content.lower()
        assert client.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "location",
        [
            "https://private.example/secret",
            "https://user:pass@example.com/secret",
            "file:///etc/passwd",
        ],
    )
    async def test_light_fetch_blocks_unsafe_redirect_target_before_second_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        location: str,
    ) -> None:
        client = FakeClient(
            {
                "https://public.example/start": FakeResponse(
                    status_code=302,
                    headers={"Location": location},
                ),
            }
        )
        install_fake_httpx(monkeypatch, client)
        install_fake_trafilatura(monkeypatch)
        install_fake_resolver(
            monkeypatch,
            {
                "public.example": [PUBLIC_IPV4],
                "private.example": ["10.0.0.7"],
            },
        )
        builder = WebFetchToolBuilder(mode="light")

        result = await builder.build(
            builder.validate({"url": "https://public.example/start", "mode": "light"})
        ).execute()

        assert result.status == "error"
        assert "blocked network target" in result.content.lower()
        assert client.calls == [("https://public.example/start", False)]

    @pytest.mark.asyncio
    async def test_light_fetch_follows_relative_public_redirects_with_redirects_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = FakeClient(
            {
                "https://public.example/start": FakeResponse(
                    status_code=302,
                    headers={"Location": "/next"},
                ),
                "https://public.example/next": FakeResponse(
                    status_code=200,
                    text="<title>Example</title><main>Body</main>",
                ),
            }
        )
        install_fake_httpx(monkeypatch, client)
        install_fake_trafilatura(monkeypatch, text="Extracted body text")
        install_fake_resolver(monkeypatch, {"public.example": [PUBLIC_IPV4]})
        builder = WebFetchToolBuilder(mode="light")

        result = await builder.build(
            builder.validate({"url": "https://public.example/start", "mode": "light"})
        ).execute()

        assert result.status == "success"
        assert "Fetched: https://public.example/next (mode=light)" in result.content
        assert client.calls == [
            ("https://public.example/start", False),
            ("https://public.example/next", False),
        ]

    @pytest.mark.asyncio
    async def test_markdown_fetch_enforces_redirect_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        responses = {
            "https://public.example/start": FakeResponse(status_code=302, headers={"Location": "/one"}),
            "https://public.example/one": FakeResponse(status_code=302, headers={"Location": "/two"}),
            "https://public.example/two": FakeResponse(status_code=302, headers={"Location": "/three"}),
            "https://public.example/three": FakeResponse(status_code=302, headers={"Location": "/four"}),
            "https://public.example/four": FakeResponse(status_code=302, headers={"Location": "/five"}),
            "https://public.example/five": FakeResponse(status_code=302, headers={"Location": "/six"}),
        }
        client = FakeClient(responses)
        install_fake_httpx(monkeypatch, client)
        install_fake_html2text(monkeypatch)
        install_fake_resolver(monkeypatch, {"public.example": [PUBLIC_IPV4]})
        builder = WebFetchToolBuilder(mode="markdown")

        result = await builder.build(
            builder.validate({"url": "https://public.example/start", "mode": "markdown"})
        ).execute()

        assert result.status == "error"
        assert "too many redirects" in result.content.lower()
        assert client.calls == [
            ("https://public.example/start", False),
            ("https://public.example/one", False),
            ("https://public.example/two", False),
            ("https://public.example/three", False),
            ("https://public.example/four", False),
            ("https://public.example/five", False),
        ]

    @pytest.mark.asyncio
    async def test_browser_fetch_validates_initial_target_before_playwright_import(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        install_fake_resolver(monkeypatch, {"internal.example": ["10.0.0.7"]})
        builder = WebFetchToolBuilder(mode="browser")

        result = await builder.build(
            builder.validate({"url": "https://internal.example/data", "mode": "browser"})
        ).execute()

        assert result.status == "error"
        assert "blocked network target" in result.content.lower()

    @pytest.mark.asyncio
    async def test_browser_fetch_installs_route_guard_before_goto_and_blocks_service_workers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context = install_fake_playwright(
            monkeypatch,
            requests=[
                {"url": "https://public.example/start", "explicit_request": True},
                {"url": "https://public.example/app.js"},
            ],
        )
        install_fake_html2text(monkeypatch, text="Rendered body")
        install_fake_resolver(monkeypatch, {"public.example": [PUBLIC_IPV4]})
        builder = WebFetchToolBuilder(mode="browser")

        result = await builder.build(
            builder.validate({"url": "https://public.example/start", "mode": "browser"})
        ).execute()

        assert result.status == "success"
        assert context.route_calls
        assert context.route_results == [
            ("https://public.example/start", ["continue"]),
            ("https://public.example/app.js", ["continue"]),
        ]
        assert context.browser is not None
        assert context.browser.new_context_kwargs == {
            "user_agent": ANY,
            "service_workers": "block",
        }
        assert context.page.goto_calls == [
            ("https://public.example/start", "networkidle", 30000.0)
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "blocked_url",
        [
            "https://private.example/redirect",
            "https://user:pass@example.com/secret",
            "file:///etc/passwd",
        ],
    )
    async def test_browser_fetch_aborts_blocked_main_frame_requests(
        self,
        monkeypatch: pytest.MonkeyPatch,
        blocked_url: str,
    ) -> None:
        context = install_fake_playwright(
            monkeypatch,
            requests=[
                {"url": "https://public.example/start"},
                {
                    "url": blocked_url,
                    "raise_on_abort": True,
                    "raise_message": f"navigation blocked for {blocked_url} via 10.0.0.7",
                },
            ],
        )
        install_fake_html2text(monkeypatch, text="Rendered body")
        install_fake_resolver(
            monkeypatch,
            {
                "public.example": [PUBLIC_IPV4],
                "private.example": ["10.0.0.7"],
                "example.com": [PUBLIC_IPV4],
            },
        )
        builder = WebFetchToolBuilder(mode="browser")

        result = await builder.build(
            builder.validate({"url": "https://public.example/start", "mode": "browser"})
        ).execute()

        assert result.status == "error"
        assert result.content == "Browser fetch error: blocked network target"
        assert blocked_url not in result.content
        assert "10.0.0.7" not in result.content
        assert context.route_results[-1] == (blocked_url, ["abort"])

    @pytest.mark.asyncio
    async def test_browser_fetch_aborts_private_subresource_requests(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context = install_fake_playwright(
            monkeypatch,
            requests=[
                {"url": "https://public.example/start"},
                {"url": "https://private.example/app.js"},
            ],
        )
        install_fake_html2text(monkeypatch, text="Rendered body")
        install_fake_resolver(
            monkeypatch,
            {
                "public.example": [PUBLIC_IPV4],
                "private.example": ["10.0.0.7"],
            },
        )
        builder = WebFetchToolBuilder(mode="browser")

        result = await builder.build(
            builder.validate({"url": "https://public.example/start", "mode": "browser"})
        ).execute()

        assert result.status == "success"
        assert context.route_results == [
            ("https://public.example/start", ["continue"]),
            ("https://private.example/app.js", ["abort"]),
        ]

    @pytest.mark.asyncio
    async def test_browser_fetch_aborts_requests_with_missing_request_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context = install_fake_playwright(
            monkeypatch,
            requests=[
                {"url": "https://public.example/start"},
                {
                    "url": "invalid-request",
                    "route_request": SimpleNamespace(url=None),
                },
            ],
        )
        install_fake_html2text(monkeypatch, text="Rendered body")
        install_fake_resolver(monkeypatch, {"public.example": [PUBLIC_IPV4]})
        builder = WebFetchToolBuilder(mode="browser")

        result = await builder.build(
            builder.validate({"url": "https://public.example/start", "mode": "browser"})
        ).execute()

        assert result.status == "success"
        assert context.route_results == [
            ("https://public.example/start", ["continue"]),
            ("invalid-request", ["abort"]),
        ]

    @pytest.mark.asyncio
    async def test_browser_fetch_allow_private_networks_permits_private_requests(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context = install_fake_playwright(
            monkeypatch,
            requests=[
                {"url": "https://internal.example/start"},
                {"url": "https://internal.example/app.js"},
            ],
        )
        install_fake_html2text(monkeypatch, text="Rendered body")
        install_fake_resolver(monkeypatch, {"internal.example": ["10.0.0.7"]})
        builder = WebFetchToolBuilder(mode="browser", allow_private_networks=True)

        result = await builder.build(
            builder.validate({"url": "https://internal.example/start", "mode": "browser"})
        ).execute()

        assert result.status == "success"
        assert context.route_results == [
            ("https://internal.example/start", ["continue"]),
            ("https://internal.example/app.js", ["continue"]),
        ]

    @pytest.mark.asyncio
    async def test_auto_mode_browser_path_cannot_skip_route_guard(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context = install_fake_playwright(
            monkeypatch,
            requests=[
                {"url": "https://x.com/start"},
                {"url": "https://private.example/tracker.js"},
            ],
        )
        install_fake_html2text(monkeypatch, text="Rendered body")
        install_fake_resolver(
            monkeypatch,
            {
                "x.com": [PUBLIC_IPV4],
                "private.example": ["10.0.0.7"],
            },
        )
        builder = WebFetchToolBuilder(mode="browser")

        result = await builder.build(
            builder.validate({"url": "https://x.com/start", "mode": "auto"})
        ).execute()

        assert result.status == "success"
        assert context.route_results == [
            ("https://x.com/start", ["continue"]),
            ("https://private.example/tracker.js", ["abort"]),
        ]
