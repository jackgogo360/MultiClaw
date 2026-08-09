"""WebFetch tool — fetch web pages with automatic mode selection."""

from __future__ import annotations

import logging
import re
from enum import Enum
from pathlib import Path
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field

from multiclaw.tools._common import WorkspaceToolBuilder, _error, _success
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation
from multiclaw.tools.network_policy import NetworkPolicy, NetworkPolicyError

logger = logging.getLogger(__name__)


class FetchMode(str, Enum):
    LIGHT = "light"
    MARKDOWN = "markdown"
    BROWSER = "browser"
    AUTO = "auto"


SPA_INDICATORS = {
    "react", "angular", "vue", "next", "nuxt", "svelte", "gatsby",
    "vercel", "netlify", "cloudflare-pages",
}

BROWSER_DOMAINS = {
    "twitter.com", "x.com", "instagram.com", "facebook.com",
    "linkedin.com", "reddit.com", "medium.com",
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}


class WebFetchParams(BaseModel):
    url: str
    mode: str = Field(default="auto")


class WebFetchInvocation(ToolInvocation[WebFetchParams]):
    def __init__(self, name: str, params: WebFetchParams,
                 mode: FetchMode, timeout: float,
                 network_policy: NetworkPolicy | None = None) -> None:
        super().__init__(name=name, params=params)
        self.default_mode = mode
        self.timeout = timeout
        self.network_policy = network_policy or NetworkPolicy()
        self._browser_blocked_any = False

    async def execute(self) -> ToolExecutionResult:
        url = self.params.url
        if not url or not url.strip():
            return _error("URL is empty")

        try:
            mode = FetchMode(self.params.mode)
        except ValueError:
            return _error(f"Unknown fetch mode: '{self.params.mode}'. Valid: light, markdown, browser, auto")
        try:
            url = self.network_policy.validate_url(url)
            if mode == FetchMode.AUTO:
                return self._auto_fetch(url)
            elif mode == FetchMode.LIGHT:
                return self._light_fetch(url)
            elif mode == FetchMode.MARKDOWN:
                return self._markdown_fetch(url)
            elif mode == FetchMode.BROWSER:
                return self._browser_fetch(url)
            else:
                return _error(f"Unknown mode: {mode}")
        except NetworkPolicyError as e:
            return _error(f"Blocked network target: {e}")

    def _auto_fetch(self, url: str) -> ToolExecutionResult:
        if self._needs_browser(url):
            result = self._browser_fetch(url)
            if result.status == "success":
                return result
        result = self._light_fetch(url)
        if result.status == "error":
            result = self._markdown_fetch(url)
            if result.status == "error":
                return self._browser_fetch(url)
            return result
        if self._content_looks_empty(result.content):
            browser_result = self._browser_fetch(url)
            if browser_result.status == "success" and len(browser_result.content) > len(result.content):
                return browser_result
        return result

    def _needs_browser(self, url: str) -> bool:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")
        if domain in BROWSER_DOMAINS:
            return True
        path = parsed.path.lower()
        if any(f"/{spa}" in path or f".{spa}" in path for spa in SPA_INDICATORS):
            return True
        if "#/" in url or "#!" in url:
            return True
        return False

    def _content_looks_empty(self, content: str) -> bool:
        if not content:
            return True
        text = content.strip()
        if len(text) < 200:
            return True
        if len(text.split()) < 30:
            return True
        return False

    def _request_with_redirects(self, client, url: str) -> tuple[str, object]:
        current_url = self.network_policy.validate_url(url)
        redirects = 0
        while True:
            resp = client.get(current_url, follow_redirects=False)
            if resp.status_code not in {301, 302, 303, 307, 308}:
                return current_url, resp
            location = resp.headers.get("Location")
            if not location:
                return current_url, resp
            if redirects >= 5:
                raise NetworkPolicyError("too many redirects")
            current_url = self.network_policy.validate_url(urljoin(current_url, location))
            redirects += 1

    def _light_fetch(self, url: str) -> ToolExecutionResult:
        try:
            import httpx
        except ImportError:
            return _error("httpx not installed: pip install httpx")
        try:
            import trafilatura
        except ImportError:
            return _error("trafilatura not installed: pip install trafilatura")
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, headers=DEFAULT_HEADERS) as client:
                final_url, resp = self._request_with_redirects(client, url)
                if resp.is_error:
                    logger.error("WebFetch light error %s for %s: %s", resp.status_code, final_url, resp.text[:1000])
                resp.raise_for_status()
                html = resp.text[:5_000_000]
                text = trafilatura.extract(html, include_links=True, include_tables=True,
                                            include_comments=False, favor_recall=True)
                if not text:
                    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()[:50000]
                title = ""
                match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if match:
                    title = match.group(1).strip()[:200]
                content = f"Fetched: {final_url} (mode=light)\nTitle: {title}\n\n{text or ''}"
                return _success(content, data={"url": final_url, "mode": "light", "title": title})
        except NetworkPolicyError:
            raise
        except Exception as e:
            return _error(f"Light fetch error: {e}")

    def _markdown_fetch(self, url: str) -> ToolExecutionResult:
        try:
            import httpx
        except ImportError:
            return _error("httpx not installed: pip install httpx")
        try:
            import html2text
        except ImportError:
            return _error("html2text not installed: pip install html2text")
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, headers=DEFAULT_HEADERS) as client:
                final_url, resp = self._request_with_redirects(client, url)
                if resp.is_error:
                    logger.error("WebFetch markdown error %s for %s: %s", resp.status_code, final_url, resp.text[:1000])
                resp.raise_for_status()
                html = resp.text[:5_000_000]
                html = re.sub(r"<nav[^>]*>.*?</nav>", "", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<footer[^>]*>.*?</footer>", "", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
                converter = html2text.HTML2Text()
                converter.body_width = 0
                converter.ignore_images = True
                converter.protect_links = True
                converter.unicode_snob = True
                markdown = converter.handle(html).strip()
                title = ""
                match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if match:
                    title = match.group(1).strip()[:200]
                content = f"Fetched: {final_url} (mode=markdown)\nTitle: {title}\n\n{markdown}"
                return _success(content, data={"url": final_url, "mode": "markdown", "title": title})
        except NetworkPolicyError:
            raise
        except Exception as e:
            return _error(f"Markdown fetch error: {e}")

    def _browser_fetch(self, url: str) -> ToolExecutionResult:
        url = self.network_policy.validate_url(url)
        self._browser_blocked_any = False
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return _error("playwright not installed: pip install playwright && playwright install chromium")
        browser = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=DEFAULT_HEADERS["User-Agent"],
                    service_workers="block",
                )
                route_web_socket = getattr(context, "route_web_socket", None)
                if not callable(route_web_socket):
                    raise NetworkPolicyError(
                        "browser runtime cannot enforce WebSocket policy"
                    )
                route_web_socket("**/*", self._block_playwright_websocket)
                context.route("**/*", self._make_playwright_route_guard())
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
                title = page.title()
                html = page.content()
                try:
                    import html2text
                    converter = html2text.HTML2Text()
                    converter.body_width = 0
                    converter.ignore_images = True
                    converter.unicode_snob = True
                    text = converter.handle(html).strip()
                except ImportError:
                    text = re.sub(r"<[^>]+>", " ", html)
                    text = re.sub(r"\s+", " ", text).strip()
                content = f"Fetched: {url} (mode=browser)\nTitle: {title}\n\n{text}"
                return _success(content, data={"url": url, "mode": "browser", "title": title})
        except Exception as e:
            if self._browser_blocked_any:
                return _error("Browser fetch error: blocked network target")
            return _error(f"Browser fetch error: {e}")
        finally:
            if browser is not None:
                browser.close()

    def _make_playwright_route_guard(self):
        def guard(route, request=None) -> None:
            try:
                request_obj = request or getattr(route, "request", None)
                request_url = self._extract_playwright_request_url(request_obj)
                self.network_policy.validate_url(request_url)
            except NetworkPolicyError:
                self._browser_blocked_any = True
                route.abort()
                return
            route.continue_()

        return guard

    @staticmethod
    def _block_playwright_websocket(web_socket_route) -> None:
        web_socket_route.close(
            code=1008,
            reason="WebSocket disabled by network policy",
        )

    def _extract_playwright_request_url(self, request) -> str:
        if request is None:
            raise NetworkPolicyError("blocked network target")
        request_url = getattr(request, "url", None)
        if callable(request_url):
            request_url = request_url()
        if not isinstance(request_url, str) or not request_url:
            raise NetworkPolicyError("blocked network target")
        return request_url


class WebFetchToolBuilder(WorkspaceToolBuilder):
    name = "web_fetch"
    description = "Fetch a web page and extract content. Modes: light, markdown, browser, auto."
    parameters_schema = WebFetchParams
    read_only = True

    def __init__(self, workspace_root: str | Path | None = None, policy=None,
                 mode: str = "auto", timeout: float = 30.0,
                 allow_private_networks: bool = False) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.mode = mode
        self.timeout = timeout
        self.allow_private_networks = allow_private_networks

    def validate(self, params: dict) -> WebFetchParams:
        return WebFetchParams(**params)

    def build(self, params: WebFetchParams) -> ToolInvocation[WebFetchParams]:
        return WebFetchInvocation(name=self.name, params=params,
                                  mode=FetchMode(self.mode),
                                  timeout=self.timeout,
                                  network_policy=NetworkPolicy(
                                      allow_private_networks=self.allow_private_networks
                                  ))
