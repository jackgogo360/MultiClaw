"""WebFetch tool — fetch web pages with automatic mode selection."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from multiclaw.tools._common import WorkspaceToolBuilder, _error, _success
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation


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
                 mode: FetchMode, timeout: float) -> None:
        super().__init__(name=name, params=params)
        self.default_mode = mode
        self.timeout = timeout

    async def execute(self) -> ToolExecutionResult:
        url = self.params.url
        if not url or not url.strip():
            return _error("URL is empty")

        url = self._normalize_url(url)

        try:
            mode = FetchMode(self.params.mode)
        except ValueError:
            return _error(f"Unknown fetch mode: '{self.params.mode}'. Valid: light, markdown, browser, auto")

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

    def _normalize_url(self, url: str) -> str:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

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
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=DEFAULT_HEADERS) as client:
                resp = client.get(url)
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
                content = f"Fetched: {url} (mode=light)\nTitle: {title}\n\n{text or ''}"
                return _success(content, data={"url": url, "mode": "light", "title": title})
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
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=DEFAULT_HEADERS) as client:
                resp = client.get(url)
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
                content = f"Fetched: {url} (mode=markdown)\nTitle: {title}\n\n{markdown}"
                return _success(content, data={"url": url, "mode": "markdown", "title": title})
        except Exception as e:
            return _error(f"Markdown fetch error: {e}")

    def _browser_fetch(self, url: str) -> ToolExecutionResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return _error("playwright not installed: pip install playwright && playwright install chromium")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
                title = page.title()
                html = page.content()
                browser.close()
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
            return _error(f"Browser fetch error: {e}")


class WebFetchToolBuilder(WorkspaceToolBuilder):
    name = "web_fetch"
    description = "Fetch a web page and extract content. Modes: light, markdown, browser, auto."
    parameters_schema = WebFetchParams

    def __init__(self, workspace_root: str | Path | None = None, policy=None,
                 mode: str = "auto", timeout: float = 30.0) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.mode = mode
        self.timeout = timeout

    def validate(self, params: dict) -> WebFetchParams:
        return WebFetchParams(**params)

    def build(self, params: WebFetchParams) -> ToolInvocation[WebFetchParams]:
        return WebFetchInvocation(name=self.name, params=params,
                                  mode=FetchMode(self.mode),
                                  timeout=self.timeout)
