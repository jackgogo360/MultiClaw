from dataclasses import dataclass


@dataclass(frozen=True)
class ContextBuildReport:
    limit_tokens: int
    reserved_response_tokens: int
    used_tokens_by_level: dict[str, int]
    dropped_by_level: dict[str, int]


@dataclass(frozen=True)
class ContextBuildResult:
    messages: list[dict]
    report: ContextBuildReport


def estimate_tokens(text: str) -> int:
    return 0 if not text else (len(text) + 3) // 4
