"""WebSearch tool — unified web search with engine fallback."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools._common import WorkspaceToolBuilder, _error, _success
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation

logger = logging.getLogger(__name__)

DEFAULT_ENGINE = "duckduckgo"
DEFAULT_MAX_RESULTS = 5


class WebSearchParams(BaseModel):
    query: str
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=5)
    engine: str | None = None


class WebSearchInvocation(ToolInvocation[WebSearchParams]):
    def __init__(self, name: str, params: WebSearchParams,
                 default_engine: str, fallback_engines: list[str],
                 lang: str, region: str) -> None:
        super().__init__(name=name, params=params)
        self.default_engine = default_engine
        self.fallback_engines = fallback_engines
        self.lang = lang
        self.region = region
        self._instances: dict[str, object] = {}

    async def execute(self) -> ToolExecutionResult:
        query = self.params.query
        if not query or not query.strip():
            return _error("Query cannot be empty")

        target_engine = self.params.engine or self.default_engine
        order = [target_engine] + [e for e in self.fallback_engines if e != target_engine]

        errors: list[str] = []
        for eng_name in order:
            instance = self._get_engine(eng_name)
            if instance is None:
                errors.append(f"{eng_name}: unknown engine")
                continue
            try:
                resp = instance.search(query, max_results=self.params.max_results)
                if resp.get("error"):
                    errors.append(f"{eng_name}: {resp['error']}")
                    continue
                if resp.get("results"):
                    lines = [f"Search results for '{query}' ({resp['engine']}):"]
                    for r in resp["results"]:
                        lines.append(f"\n{r['position']}. {r['title']}")
                        lines.append(f"   {r['url']}")
                        if r.get("snippet"):
                            lines.append(f"   {r['snippet'][:200]}")
                    return _success(
                        "\n".join(lines),
                        data={"query": query, "engine": resp["engine"],
                              "results": resp["results"]},
                    )
            except Exception as exc:
                errors.append(f"{eng_name}: {exc}")

        err_detail = "; ".join(errors) if errors else "no engines available"
        return _error(f"All engines failed for query '{query}': {err_detail}")

    def _get_engine(self, name: str):
        if name in self._instances:
            return self._instances[name]
        if name == "duckduckgo":
            inst = _DuckDuckGoEngine(region=self.region)
        elif name == "bing":
            inst = _BingEngine(lang=self.lang)
        elif name == "baidu":
            inst = _BaiduEngine()
        else:
            return None
        self._instances[name] = inst
        return inst


class _DuckDuckGoEngine:
    def __init__(self, region: str = "wt-wt", safesearch: str = "moderate"):
        self.region = region
        self.safesearch = safesearch

    def search(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> dict:
        DDGS = None
        try:
            from ddgs import DDGS  # new package name
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # legacy
            except ImportError:
                return {"engine": "duckduckgo", "results": [],
                        "error": "ddgs not installed: pip install ddgs"}
        try:
            results = []
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=max_results, region=self.region,
                                     safesearch=self.safesearch))
                for i, item in enumerate(raw):
                    if isinstance(item, dict):
                        results.append({
                            "title": item.get("title", ""),
                            "url": item.get("href", item.get("url", "")),
                            "snippet": item.get("body", item.get("description", "")),
                            "source": "duckduckgo", "position": i + 1,
                        })
            if not results:
                logger.warning("duckduckgo returned 0 results for query=%r raw_items=%d",
                               query, len(raw))
            return {"engine": "duckduckgo", "results": results, "error": ""}
        except Exception as e:
            logger.warning("duckduckgo search error for query=%r: %s", query, e)
            return {"engine": "duckduckgo", "results": [], "error": str(e)}


class _BingEngine:
    def __init__(self, lang: str = "en"):
        self.lang = lang

    def search(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> dict:
        try:
            import httpx
        except ImportError:
            return {"engine": "bing", "results": [],
                    "error": "httpx not installed: pip install httpx"}
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": self.lang,
            }
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                resp = client.get("https://www.bing.com/search",
                                  params={"q": query, "count": max_results}, headers=headers)
                resp.raise_for_status()
                results = []
                snippet_pattern = re.compile(
                    r'<li class="b_algo".*?<h2><a[^>]*href="([^"]*)"[^>]*>(.*?)</a></h2>.*?<p[^>]*>(.*?)</p>',
                    re.DOTALL)
                for i, (url, title, snippet) in enumerate(snippet_pattern.findall(resp.text)[:max_results]):
                    title = re.sub(r"<[^>]+>", "", title).strip()
                    snippet = re.sub(r"<[^>]+>", "", snippet).strip()
                    results.append({"title": title, "url": url, "snippet": snippet,
                                    "source": "bing", "position": i + 1})
                if not results:
                    # Log HTML sample to diagnose regex mismatch
                    body_match = re.search(
                        r'<ol[^>]*id="b_results"[^>]*>(.*?)</ol>', resp.text, re.DOTALL
                    )
                    if body_match:
                        logger.warning("bing regex matched 0 results. b_results sample: %s",
                                       body_match.group(0)[:1000])
                    else:
                        logger.warning("bing regex matched 0 results, no b_results found. url=%s html_snippet=%s",
                                       str(resp.url), resp.text[:1000])
                return {"engine": "bing", "results": results, "error": ""}
        except Exception as e:
            return {"engine": "bing", "results": [], "error": str(e)}


class _BaiduEngine:
    def search(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> dict:
        try:
            import httpx
        except ImportError:
            return {"engine": "baidu", "results": [],
                    "error": "httpx not installed: pip install httpx"}
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            }
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                resp = client.get("https://www.baidu.com/s",
                                  params={"wd": query, "rn": max_results}, headers=headers)
                resp.raise_for_status()
                # Check if Baidu redirected to CAPTCHA
                if "wappass" in str(resp.url) or "captcha" in str(resp.url):
                    logger.warning("baidu redirected to captcha: %s -> %s", query, str(resp.url))
                    return {"engine": "baidu", "results": [],
                            "error": f"Baidu CAPTCHA block, redirected to {resp.url}"}
                results = []
                snippet_pattern = re.compile(
                    r'<div[^>]*class="[^"]*result[^"]*".*?<h3[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?<span[^>]*class="[^"]*content-right_[^"]*"[^>]*>(.*?)</span>',
                    re.DOTALL)
                for i, (url, title, snippet) in enumerate(
                    snippet_pattern.findall(resp.text)[:max_results]):
                    title = re.sub(r"<[^>]+>", "", title).strip()
                    snippet = re.sub(r"<[^>]+>", "", snippet).strip()
                    results.append({"title": title, "url": url, "snippet": snippet,
                                    "source": "baidu", "position": i + 1})
                if not results:
                    logger.warning("baidu regex matched 0 results. url=%s html_snippet=%s",
                                   str(resp.url), resp.text[:1000])
                return {"engine": "baidu", "results": results, "error": ""}
        except Exception as e:
            return {"engine": "baidu", "results": [], "error": str(e)}


class WebSearchToolBuilder(WorkspaceToolBuilder):
    name = "web_search"
    description = "Search the web with engine fallback. Engines: duckduckgo, bing, baidu."
    parameters_schema = WebSearchParams

    def __init__(self, workspace_root: str | Path | None = None, policy=None,
                 engine: str = DEFAULT_ENGINE,
                 fallback_engines: list[str] | None = None,
                 lang: str = "en", region: str = "wt-wt") -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.engine = engine
        self.fallback_engines = fallback_engines or [
            e for e in ["duckduckgo", "bing", "baidu"] if e != engine
        ]
        self.lang = lang
        self.region = region

    def validate(self, params: dict) -> WebSearchParams:
        return WebSearchParams(**params)

    def build(self, params: WebSearchParams) -> ToolInvocation[WebSearchParams]:
        return WebSearchInvocation(name=self.name, params=params,
                                   default_engine=self.engine,
                                   fallback_engines=self.fallback_engines,
                                   lang=self.lang, region=self.region)
