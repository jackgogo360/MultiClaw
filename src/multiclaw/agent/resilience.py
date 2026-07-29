from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
import hashlib
import math
import json
from typing import Any


class ResilienceAction(str, Enum):
    CONTINUE = "continue"
    REFLECT = "reflect"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class ResilienceDecision:
    action: ResilienceAction
    reason: str = ""


def fingerprint_calls(calls: list[Any]) -> str:
    normalized_calls = [
        {key: value for key, value in call.items() if key != "id"}
        if isinstance(call, dict)
        else call
        for call in calls
    ]
    return _fingerprint(normalized_calls)


def fingerprint_results(results: list[Any]) -> str:
    return _fingerprint(results)


class ResilienceController:
    def __init__(self, repeat_limit: int, max_reflections: int) -> None:
        if repeat_limit < 1:
            raise ValueError("repeat_limit must be at least 1")
        if max_reflections < 0:
            raise ValueError("max_reflections must be non-negative")

        self.repeat_limit = repeat_limit
        self.max_reflections = max_reflections
        self.reflections_used = 0
        self._last_call_fingerprint: str | None = None
        self._last_result_fingerprint: str | None = None
        self._call_repeats = 0
        self._result_repeats = 0

    def observe_calls(self, calls: list[Any]) -> ResilienceDecision:
        fingerprint = fingerprint_calls(calls)
        self._call_repeats = self._next_repeats(
            self._last_call_fingerprint,
            fingerprint,
            self._call_repeats,
        )
        self._last_call_fingerprint = fingerprint
        if self._call_repeats >= self.repeat_limit:
            return self._decision(
                f"Detected repeated tool call {self._call_repeats} times consecutively."
            )
        return ResilienceDecision(ResilienceAction.CONTINUE)

    def observe_results(self, results: list[Any]) -> ResilienceDecision:
        fingerprint = fingerprint_results(results)
        self._result_repeats = self._next_repeats(
            self._last_result_fingerprint,
            fingerprint,
            self._result_repeats,
        )
        self._last_result_fingerprint = fingerprint
        if self._result_repeats >= self.repeat_limit:
            return self._decision(
                f"Detected repeated tool result {self._result_repeats} times consecutively."
            )
        return ResilienceDecision(ResilienceAction.CONTINUE)

    def mark_reflection_used(self) -> None:
        """Consume reflection budget only after a reflect step actually ran."""
        self.reflections_used = min(
            self.reflections_used + 1,
            self.max_reflections,
        )

    def _decision(self, reason: str) -> ResilienceDecision:
        """Propose reflect/terminate only; callers must mark successful reflection use."""
        action = (
            ResilienceAction.REFLECT
            if self.reflections_used < self.max_reflections
            else ResilienceAction.TERMINATE
        )
        return ResilienceDecision(action=action, reason=reason)

    @staticmethod
    def _next_repeats(
        previous_fingerprint: str | None,
        current_fingerprint: str,
        repeats: int,
    ) -> int:
        if previous_fingerprint == current_fingerprint:
            return repeats + 1
        return 1


def _fingerprint(value: list[Any]) -> str:
    normalized = _normalize_value(value)
    payload = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("Non-finite floats are not supported in resilience fingerprints")
        return value

    if isinstance(value, datetime):
        return {"__type__": "datetime", "value": value.isoformat()}

    if isinstance(value, date):
        return {"__type__": "date", "value": value.isoformat()}

    if isinstance(value, time):
        return {"__type__": "time", "value": value.isoformat()}

    if isinstance(value, Enum):
        return {
            "__type__": "enum",
            "class": f"{value.__class__.__module__}.{value.__class__.__qualname__}",
            "value": _normalize_value(value.value),
        }

    if isinstance(value, dict):
        return _normalize_mapping(value)

    if isinstance(value, list):
        return [_normalize_value(item) for item in value]

    if isinstance(value, tuple):
        return {"__type__": "tuple", "items": [_normalize_value(item) for item in value]}

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return {
            "__type__": "model",
            "class": f"{value.__class__.__module__}.{value.__class__.__qualname__}",
            "value": _normalize_value(model_dump(mode="python")),
        }

    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__type__": "dataclass",
            "class": f"{value.__class__.__module__}.{value.__class__.__qualname__}",
            "value": {
                field.name: _normalize_value(getattr(value, field.name))
                for field in fields(value)
            },
        }

    raise TypeError(
        f"Unsupported value type for resilience fingerprint: {value.__class__.__name__}"
    )


def _normalize_mapping(value: dict[Any, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("Resilience fingerprint dict keys must be strings")
        normalized[key] = _normalize_value(item)
    return normalized
