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
_SECRET_KEY = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+\S+|bearer\s+\S+|"
    r"(?:api[_-]?key|password|secret)\s*[:=]\s*\S+|\b(?:sk|ghp)[_-][A-Za-z0-9_-]+)"
)


class PlanValidationError(ValueError):
    pass


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
        constraints=draft.constraints,
        generation_reason=draft.generation_reason,
        steps=[
            ValidatedPlanStep(**by_key[key].model_dump(), ordinal=index)
            for index, key in enumerate(ordered, start=1)
        ],
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
