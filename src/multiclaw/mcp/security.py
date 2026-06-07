"""安全模块 — 凭证清理、注入检测"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_CREDENTIAL_PATTERN = re.compile(
    r"(?:ghp_[A-Za-z0-9_]{1,255}|sk-[A-Za-z0-9_]{1,255}|Bearer\s+\S+"
    r"|token=[^\s&,;\"']{1,255}|key=[^\s&,;\"']{1,255}"
    r"|API_KEY=[^\s&,;\"']{1,255}|password=[^\s&,;\"']{1,255}"
    r"|secret=[^\s&,;\"']{1,255})",
    re.IGNORECASE,
)

_INJECTION_PATTERNS = [
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I), "prompt override attempt"),
    (re.compile(r"you\s+are\s+now\s+a", re.I), "identity override attempt"),
    (re.compile(r"system\s*:\s*", re.I), "system prompt injection attempt"),
    (re.compile(r"<\s*(system|human|assistant)\s*>", re.I), "role tag injection attempt"),
    (re.compile(r"do\s+not\s+(tell|inform|mention|reveal)", re.I), "concealment instruction"),
    (re.compile(r"(curl|wget|fetch)\s+https?://", re.I), "network command in description"),
    (re.compile(r"execute\s+(this|the\s+following)\s+command", re.I), "command execution attempt"),
    (re.compile(r"output\s+(the|your)\s+(system|initial)\s+prompt", re.I), "prompt extraction attempt"),
    (re.compile(r"disregard\s+(all|any)\s+(prior|previous)", re.I), "instruction override attempt"),
    (re.compile(r"pretend\s+(you|that)\s+(are|this)", re.I), "role manipulation attempt"),
]


def sanitize_error(text: str) -> str:
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", text)


def scan_tool_description(tool_name: str, description: str) -> list[str]:
    findings = []
    for pattern, label in _INJECTION_PATTERNS:
        if pattern.search(description):
            findings.append(f"[{tool_name}] {label}")
    if findings:
        logger.warning(
            "Potential prompt injection in tool '%s': %s",
            tool_name,
            "; ".join(findings),
        )
    return findings
