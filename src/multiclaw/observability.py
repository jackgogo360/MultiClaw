from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from multiclaw.security.redaction import redact_trace_attributes


class InvalidMetricLabelError(ValueError):
    pass


@dataclass(slots=True)
class OperationalMetrics:
    _ALLOWED_LABELS = frozenset(
        {
            "backend",
            "profile",
            "operation",
            "status",
            "error_class",
            "recovery_strategy",
        }
    )
    _FORBIDDEN_LABELS = frozenset(
        {
            "tenant_id",
            "workspace_id",
            "session_id",
            "run_id",
            "request_id",
            "email",
            "provider_name",
            "path",
        }
    )

    counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = field(default_factory=dict)

    def increment(self, name: str, *, labels: Mapping[str, object] | None = None, value: int = 1) -> int:
        normalized = self._normalize_labels(labels or {})
        key = (name, tuple(sorted(normalized.items())))
        self.counters[key] = self.counters.get(key, 0) + value
        return self.counters[key]

    def _normalize_labels(self, labels: Mapping[str, object]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, raw_value in labels.items():
            if key in self._FORBIDDEN_LABELS or key not in self._ALLOWED_LABELS:
                raise InvalidMetricLabelError(f"invalid metric label: {key}")
            normalized[key] = str(raw_value)
        return normalized


def sanitize_trace_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    return redact_trace_attributes(attributes)
