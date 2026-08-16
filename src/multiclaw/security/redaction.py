from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REDACTED = "[REDACTED]"
BINARY_REDACTED = "[BINARY REDACTED]"
REDACTED_PATH = "[REDACTED PATH]"
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
    re.compile(r"(?:(?:[A-Za-z]:)?[\\/][^\s]+)+"),
)
_PRIVATE_PATH_MARKERS = (
    str(Path.home()),
    tempfile.gettempdir(),
    "/private/",
    "/tmp/",
    "\\Users\\",
    "\\Temp\\",
)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text):
                redacted[key_text] = REDACTED
            else:
                redacted[key_text] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        if len(value) == 2 and isinstance(value[0], str) and _SECRET_KEY_RE.search(value[0]):
            return (value[0], REDACTED)
        return tuple(redact(item) for item in value)
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
    return {str(key): redact(value) for key, value in attributes.items()}


def _redact_string(value: str) -> str:
    result = value
    for pattern, replacement in _STRING_PATTERNS:
        result = pattern.sub(replacement, result)
    if any(marker in result for marker in _PRIVATE_PATH_MARKERS) or "/." in result or "\\." in result:
        for pattern in _PATH_PATTERNS:
            result = pattern.sub(REDACTED_PATH, result)
    return result
