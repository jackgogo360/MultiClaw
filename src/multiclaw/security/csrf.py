from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from fastapi import Request

from multiclaw.auth.models import CSRF_COOKIE_NAME, CSRF_HEADER_NAME


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(cookie_token: str | None, header_token: str | None) -> bool:
    if not cookie_token or not header_token:
        return False
    return secrets.compare_digest(cookie_token, header_token)


def extract_request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin:
        return _normalize_origin(origin)
    referer = request.headers.get("referer")
    if not referer:
        return None
    parsed = urlsplit(referer)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def csrf_failure_reason(request: Request, allowed_origins: set[str] | frozenset[str]) -> str | None:
    origin = extract_request_origin(request)
    if origin is None:
        return "missing origin"
    if origin not in allowed_origins:
        return "untrusted origin"
    if not request.cookies.get(CSRF_COOKIE_NAME):
        return "missing csrf cookie"
    if not request.headers.get(CSRF_HEADER_NAME):
        return "missing csrf header"
    if not tokens_match(
        request.cookies.get(CSRF_COOKIE_NAME),
        request.headers.get(CSRF_HEADER_NAME),
    ):
        return "csrf mismatch"
    return None


def _normalize_origin(value: str) -> str | None:
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
