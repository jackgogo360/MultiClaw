from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence

from multiclaw.planner.models import (
    PlanDraft,
    ValidatedPlanDraft,
    ValidatedPlanStep,
)

MAX_PLAN_CONTENT_BYTES = 262_144
_AUTHORIZATION_NAME = r"authorization"
_SECRET_ASSIGNMENT_NAME_PATTERN = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
)
_SECRET_NAME_PATTERN = (
    rf"(?:{_AUTHORIZATION_NAME}|{_SECRET_ASSIGNMENT_NAME_PATTERN})"
)
_QUOTED_ASSIGNMENT_VALUE_PATTERN = r'''(?:"[^"\r\n]*"|'[^'\r\n]*')'''
_UNQUOTED_ASSIGNMENT_VALUE_PATTERN = r"\S+"
_ASSIGNMENT_VALUE_PATTERN = (
    rf"(?:{_QUOTED_ASSIGNMENT_VALUE_PATTERN}|{_UNQUOTED_ASSIGNMENT_VALUE_PATTERN})"
)
_AUTHORIZATION_SCHEME_PATTERN = r"(?:basic|bearer|digest|token|negotiate)"
_AUTHORIZATION_VALUE_PATTERN = (
    rf"(?:{_QUOTED_ASSIGNMENT_VALUE_PATTERN}|"
    rf"{_AUTHORIZATION_SCHEME_PATTERN}\s+{_ASSIGNMENT_VALUE_PATTERN})"
)
_AUTHORIZATION_ASSIGNMENT_PATTERN = (
    rf"{_AUTHORIZATION_NAME}\s*[:=]\s*{_AUTHORIZATION_VALUE_PATTERN}"
)
_SECRET_ASSIGNMENT_PATTERN = (
    rf"{_SECRET_ASSIGNMENT_NAME_PATTERN}\s*[:=]\s*{_ASSIGNMENT_VALUE_PATTERN}"
)
_BEARER_TOKEN_CHAR_PATTERN = r"[A-Za-z0-9._~+/=-]"
_STANDALONE_BEARER_PATTERN = (
    rf"\bbearer\s+(?={_BEARER_TOKEN_CHAR_PATTERN}{{8,}}"
    rf"(?!{_BEARER_TOKEN_CHAR_PATTERN}))"
    rf"{_BEARER_TOKEN_CHAR_PATTERN}+"
)
_TOKEN_PREFIX_PATTERN = r"\b(?:sk[-_]|ghp[-_]|github_pat_)[A-Za-z0-9_-]+"

_SECRET_KEY = re.compile(_SECRET_NAME_PATTERN, re.IGNORECASE)
_SECRET_VALUE = re.compile(
    rf"(?:{_AUTHORIZATION_ASSIGNMENT_PATTERN}|{_SECRET_ASSIGNMENT_PATTERN}|"
    rf"{_STANDALONE_BEARER_PATTERN}|{_TOKEN_PREFIX_PATTERN})",
    re.IGNORECASE,
)


class PlanValidationError(ValueError):
    pass


def _require_positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise PlanValidationError(f"{name} must be a positive integer")
    return value


def _reject_credentials(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                raise PlanValidationError("Plan contains credential-shaped field")
            _reject_credentials(item)
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            _reject_credentials(item)
        return
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        raise PlanValidationError("Plan contains credential-shaped content")


def sanitize_plan_text(value: str) -> str:
    return _SECRET_VALUE.sub("[REDACTED]", value)


def validate_plan_draft(
    draft: PlanDraft,
    *,
    max_steps: int,
    max_depth: int,
    max_attempts: int,
    max_content_bytes: int = MAX_PLAN_CONTENT_BYTES,
) -> ValidatedPlanDraft:
    max_steps = _require_positive_int(max_steps, name="max_steps")
    max_depth = _require_positive_int(max_depth, name="max_depth")
    max_attempts = _require_positive_int(max_attempts, name="max_attempts")
    max_content_bytes = _require_positive_int(
        max_content_bytes,
        name="max_content_bytes",
    )

    if len(draft.steps) > min(max_steps, 20):
        raise PlanValidationError("Plan exceeds configured step limit")

    keys = [step.logical_step_key for step in draft.steps]
    if len(keys) != len(set(keys)):
        raise PlanValidationError("duplicate logical_step_key")

    by_key = {step.logical_step_key: step for step in draft.steps}
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {key: 0 for key in keys}
    depth = {key: 1 for key in keys}
    original = {key: index for index, key in enumerate(keys)}

    for step in draft.steps:
        if step.max_attempts > max_attempts:
            raise PlanValidationError("step max_attempts exceeds configured limit")
        if len(step.depends_on) != len(set(step.depends_on)):
            raise PlanValidationError("duplicate dependency")
        for dependency in step.depends_on:
            if dependency == step.logical_step_key:
                raise PlanValidationError("self dependency")
            if dependency not in by_key:
                raise PlanValidationError(f"missing dependency {dependency}")
            outgoing[dependency].append(step.logical_step_key)
            indegree[step.logical_step_key] += 1

    ready = sorted(
        (key for key, count in indegree.items() if count == 0),
        key=original.__getitem__,
    )
    ordered: list[str] = []
    while ready:
        key = ready.pop(0)
        ordered.append(key)
        for child in sorted(outgoing[key], key=original.__getitem__):
            depth[child] = max(depth[child], depth[key] + 1)
            if depth[child] > min(max_depth, 10):
                raise PlanValidationError("dependency depth exceeds configured limit")
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=original.__getitem__)

    if len(ordered) != len(keys):
        raise PlanValidationError("Plan dependency cycle")

    validated = ValidatedPlanDraft(
        objective=draft.objective,
        constraints=tuple(draft.constraints),
        generation_reason=draft.generation_reason,
        steps=tuple(
            ValidatedPlanStep(
                **by_key[key].model_dump(exclude={"depends_on"}),
                depends_on=tuple(by_key[key].depends_on),
                ordinal=index,
            )
            for index, key in enumerate(ordered, start=1)
        ),
    )
    _reject_credentials(validated.model_dump(mode="json"))

    encoded = canonical_plan_bytes(validated)
    content_limit = min(max_content_bytes, MAX_PLAN_CONTENT_BYTES)
    if len(encoded) > content_limit:
        raise PlanValidationError(f"Plan content exceeds {content_limit} bytes")
    return validated


def canonical_plan_bytes(plan: ValidatedPlanDraft) -> bytes:
    return json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def plan_content_digest(plan: ValidatedPlanDraft) -> str:
    return hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()


def step_definition_digest(step: ValidatedPlanStep) -> str:
    value = {
        "logical_step_key": step.logical_step_key,
        "title": step.title,
        "description": step.description,
        "expected_outcome": step.expected_outcome,
        "max_attempts": step.max_attempts,
    }
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
