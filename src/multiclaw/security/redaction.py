from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


REDACTED = "[REDACTED]"
BINARY_REDACTED = "[BINARY REDACTED]"
REDACTED_PATH = "[REDACTED PATH]"
CIRCULAR = "[CIRCULAR]"
_TRACE_FORBIDDEN_KEYS = {
    "tenant_id",
    "workspace_id",
    "session_id",
    "run_id",
    "request_id",
    "email",
    "provider_name",
    "path",
}
_SECRET_KEY_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
)
_STRING_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+\S+"), "Authorization: [REDACTED]"),
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)\b([A-Za-z_]*(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)[A-Za-z_]*)\b\s*[:=]\s*\S+"
        ),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"\b(sk|ghp)_[A-Za-z0-9_\-]+\b"), REDACTED),
)
_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9:.])/(?!/)[^\s,;\"']+"),
    re.compile(r"(?<![A-Za-z0-9])\.\.?/[^\s,;\"']+"),
    re.compile(r"\b[A-Za-z]:\\[^\s,;\"']+"),
    re.compile(r"(?<![A-Za-z0-9])\\\\[^\s,;\"']+"),
)


def redact(value: Any) -> Any:
    return _redact(value, seen=set())


def _redact(value: Any, *, seen: set[int]) -> Any:
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return CIRCULAR
        seen.add(identity)
        redacted: dict[str, Any] = {}
        try:
            for key, item in value.items():
                key_text = str(key)
                if _SECRET_KEY_RE.search(key_text):
                    redacted[key_text] = REDACTED
                else:
                    redacted[key_text] = _redact(item, seen=seen)
            return redacted
        finally:
            seen.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in seen:
            return CIRCULAR
        seen.add(identity)
        try:
            return [_redact(item, seen=seen) for item in value]
        finally:
            seen.remove(identity)
    if isinstance(value, tuple):
        identity = id(value)
        if identity in seen:
            return CIRCULAR
        if len(value) == 2 and isinstance(value[0], str) and _SECRET_KEY_RE.search(value[0]):
            return (value[0], REDACTED)
        seen.add(identity)
        try:
            return tuple(_redact(item, seen=seen) for item in value)
        finally:
            seen.remove(identity)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        identity = id(value)
        if identity in seen:
            return CIRCULAR
        seen.add(identity)
        try:
            return [_redact(item, seen=seen) for item in value]
        finally:
            seen.remove(identity)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return BINARY_REDACTED
    if isinstance(value, str):
        return _redact_string(value)
    return value


def public_error_message(exc: BaseException) -> str:
    message = _redact_string(str(exc))
    lowered = message.lower()
    if any(token in lowered for token in ("database", "sqlite", "mysql", "alembic")):
        return "service temporarily unavailable"
    if any(token in lowered for token in ("secret", "authorization", "token", "password", "keyring")):
        return "request could not be completed"
    if any(token in lowered for token in ("mcp", "filesystem", "workspace", "path")):
        return "request could not be completed"
    if any(token in lowered for token in ("timeout", "timed out")):
        return "request timed out"
    return "request could not be completed"


def redact_trace_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in attributes.items():
        key_text = str(key)
        if key_text in _TRACE_FORBIDDEN_KEYS or _SECRET_KEY_RE.search(key_text):
            sanitized[key_text] = REDACTED
        else:
            sanitized[key_text] = redact(value)
    return sanitized


def _redact_string(value: str) -> str:
    result = value
    for pattern, replacement in _STRING_PATTERNS:
        result = pattern.sub(replacement, result)
    for pattern in _PATH_PATTERNS:
        result = pattern.sub(lambda match: _replace_path_match(match, result), result)
    return result


def _replace_path_match(match: re.Match[str], source: str) -> str:
    if match.start() >= 3 and source[match.start() - 3 : match.start()] == "://":
        return match.group(0)
    if match.start() >= 2 and source[match.start() - 1] == "/" and source[match.start() - 2] == ":":
        return match.group(0)
    return REDACTED_PATH
