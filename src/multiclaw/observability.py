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

    def clear(self) -> None:
        self.counters.clear()


@dataclass(slots=True)
class TraceEventSink:
    events: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def record(self, name: str, attributes: Mapping[str, object]) -> None:
        self.events.append((name, sanitize_trace_attributes(attributes)))

    def clear(self) -> None:
        self.events.clear()


_BOUND_METRICS = OperationalMetrics()
_BOUND_TRACE_SINK = TraceEventSink()


def bind_observability(
    *,
    metrics: OperationalMetrics | None = None,
    trace_sink: TraceEventSink | None = None,
) -> None:
    global _BOUND_METRICS, _BOUND_TRACE_SINK
    if metrics is not None:
        _BOUND_METRICS = metrics
    if trace_sink is not None:
        _BOUND_TRACE_SINK = trace_sink


def current_metrics() -> OperationalMetrics:
    return _BOUND_METRICS


def current_trace_sink() -> TraceEventSink:
    return _BOUND_TRACE_SINK


def increment_metric(name: str, *, labels: Mapping[str, object] | None = None, value: int = 1) -> int:
    return _BOUND_METRICS.increment(name, labels=labels, value=value)


def record_trace_event(name: str, *, attributes: Mapping[str, object]) -> None:
    _BOUND_TRACE_SINK.record(name, attributes)


def observe_database_error(error: BaseException, *, backend: str, operation: str) -> None:
    message = str(error).lower()
    if "database is locked" in message:
        increment_metric(
            "multiclaw_sqlite_busy_total",
            labels={"backend": backend, "operation": operation, "status": "error", "error_class": "sqlite_busy"},
        )
        record_trace_event(
            "sqlite_busy",
            attributes={"backend": backend, "operation": operation, "error": str(error)},
        )
    if "lock wait timeout exceeded" in message or "lock timeout" in message:
        increment_metric(
            "multiclaw_mysql_lock_timeout_total",
            labels={"backend": backend, "operation": operation, "status": "error", "error_class": "mysql_lock_timeout"},
        )
        record_trace_event(
            "mysql_lock_timeout",
            attributes={"backend": backend, "operation": operation, "error": str(error)},
        )


def sanitize_trace_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    return redact_trace_attributes(attributes)
