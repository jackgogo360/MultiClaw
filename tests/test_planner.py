import pytest
from pydantic import ValidationError

import multiclaw.planner as planner
from multiclaw.planner import (
    Plan,
    PlanDecisionAction,
    PlanDraft,
    PlanDraftStep,
    PlanningMode,
    PlanningRoute,
    PlanStatus,
    PlanStep,
    PlanStepCompletion,
    PlanStepRunStatus,
    PlanTriggerMode,
    Planner,
)


def plan_draft_step_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "logical_step_key": "schema",
        "title": "Create schema",
        "description": "Persist immutable plan definitions.",
        "expected_outcome": "Schema constraints pass on both databases.",
        "depends_on": [],
        "max_attempts": 2,
    }
    payload.update(overrides)
    return payload


def plan_draft_steps(count: int) -> list[dict[str, object]]:
    return [
        plan_draft_step_payload(logical_step_key=f"step-{index}")
        for index in range(count)
    ]


def plan_draft_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "objective": "Ship the durable plan engine",
        "constraints": ["No new dependencies"],
        "generation_reason": "The request spans storage and runtime work.",
        "steps": [plan_draft_step_payload()],
    }
    payload.update(overrides)
    return payload


def plan_step_completion_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "succeeded",
        "summary": "Schema checks passed.",
        "evidence": ["tests/test_migrations.py"],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("enum_type", "expected_values"),
    [
        (PlanningMode, ["auto", "always", "never"]),
        (PlanningRoute, ["direct", "plan"]),
        (PlanStatus, ["awaiting_approval", "approved", "rejected", "archived"]),
        (PlanTriggerMode, ["automatic", "explicit"]),
        (PlanDecisionAction, ["approve", "reject", "revise"]),
        (
            PlanStepRunStatus,
            [
                "pending",
                "running",
                "succeeded",
                "failed_retryable",
                "failed_terminal",
                "cancelled",
            ],
        ),
    ],
)
def test_durable_domain_values_are_stable(enum_type, expected_values) -> None:
    assert [item.value for item in enum_type] == expected_values


def test_durable_domain_models_accept_valid_payloads() -> None:
    draft = PlanDraft.model_validate(plan_draft_payload())
    completion = PlanStepCompletion.model_validate(plan_step_completion_payload())

    assert draft.steps[0].logical_step_key == "schema"
    assert completion.retryable is False


@pytest.mark.parametrize("logical_step_key", ["a", "step_1-2", "z" * 64])
def test_plan_draft_step_accepts_valid_logical_step_keys(logical_step_key) -> None:
    step = PlanDraftStep.model_validate(
        plan_draft_step_payload(logical_step_key=logical_step_key)
    )

    assert step.logical_step_key == logical_step_key


@pytest.mark.parametrize(
    "logical_step_key",
    ["", "UPPERCASE", "contains space", "slash/key", "z" * 65],
)
def test_plan_draft_step_rejects_invalid_logical_step_keys(logical_step_key) -> None:
    with pytest.raises(ValidationError):
        PlanDraftStep.model_validate(
            plan_draft_step_payload(logical_step_key=logical_step_key)
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "x"),
        ("title", "x" * 200),
        ("description", "x"),
        ("description", "x" * 4_000),
        ("expected_outcome", "x"),
        ("expected_outcome", "x" * 2_000),
        ("depends_on", []),
        ("depends_on", ["dependency"] * 20),
    ],
)
def test_plan_draft_step_accepts_text_and_list_boundaries(field, value) -> None:
    step = PlanDraftStep.model_validate(plan_draft_step_payload(**{field: value}))

    assert getattr(step, field) == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "x" * 201),
        ("description", ""),
        ("description", "x" * 4_001),
        ("expected_outcome", ""),
        ("expected_outcome", "x" * 2_001),
        ("depends_on", ["dependency"] * 21),
    ],
)
def test_plan_draft_step_rejects_text_and_list_overflow(field, value) -> None:
    with pytest.raises(ValidationError):
        PlanDraftStep.model_validate(plan_draft_step_payload(**{field: value}))


@pytest.mark.parametrize("max_attempts", [1, 20])
def test_plan_draft_step_accepts_max_attempt_boundaries(max_attempts) -> None:
    step = PlanDraftStep.model_validate(
        plan_draft_step_payload(max_attempts=max_attempts)
    )

    assert step.max_attempts == max_attempts


@pytest.mark.parametrize("max_attempts", [0, 21, 1.0, "1", True])
def test_plan_draft_step_rejects_invalid_max_attempts(max_attempts) -> None:
    with pytest.raises(ValidationError):
        PlanDraftStep.model_validate(
            plan_draft_step_payload(max_attempts=max_attempts)
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("objective", "x"),
        ("objective", "x" * 16_000),
        ("constraints", []),
        ("constraints", ["x"]),
        ("constraints", ["x" * 1_000] * 20),
        ("generation_reason", "x"),
        ("generation_reason", "x" * 1_000),
        ("steps", plan_draft_steps(1)),
        ("steps", plan_draft_steps(20)),
    ],
)
def test_plan_draft_accepts_text_and_list_boundaries(field, value) -> None:
    draft = PlanDraft.model_validate(plan_draft_payload(**{field: value}))

    actual = getattr(draft, field)
    if field == "steps":
        assert len(actual) == len(value)
    else:
        assert actual == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("objective", ""),
        ("objective", "x" * 16_001),
        ("constraints", [""]),
        ("constraints", ["x" * 1_001]),
        ("constraints", ["x"] * 21),
        ("generation_reason", ""),
        ("generation_reason", "x" * 1_001),
        ("steps", []),
        ("steps", plan_draft_steps(21)),
    ],
)
def test_plan_draft_rejects_text_and_list_overflow(field, value) -> None:
    with pytest.raises(ValidationError):
        PlanDraft.model_validate(plan_draft_payload(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", "x"),
        ("summary", "x" * 4_000),
        ("evidence", []),
        ("evidence", ["x"]),
        ("evidence", ["x" * 2_000] * 20),
    ],
)
def test_plan_step_completion_accepts_text_and_list_boundaries(field, value) -> None:
    completion = PlanStepCompletion.model_validate(
        plan_step_completion_payload(**{field: value})
    )

    assert getattr(completion, field) == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", ""),
        ("summary", "x" * 4_001),
        ("evidence", [""]),
        ("evidence", ["x" * 2_001]),
        ("evidence", ["x"] * 21),
    ],
)
def test_plan_step_completion_rejects_text_and_list_overflow(field, value) -> None:
    with pytest.raises(ValidationError):
        PlanStepCompletion.model_validate(
            plan_step_completion_payload(**{field: value})
        )


@pytest.mark.parametrize("status", ["pending", "failed_retryable", "cancelled"])
def test_plan_step_completion_rejects_invalid_status(status) -> None:
    with pytest.raises(ValidationError):
        PlanStepCompletion.model_validate(plan_step_completion_payload(status=status))


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (PlanDraftStep, plan_draft_step_payload(unexpected=True)),
        (PlanDraft, plan_draft_payload(unexpected=True)),
        (PlanStepCompletion, plan_step_completion_payload(unexpected=True)),
    ],
)
def test_durable_domain_models_reject_extra_fields(model_type, payload) -> None:
    with pytest.raises(ValidationError):
        model_type.model_validate(payload)


def test_create_plan_returns_single_draft_step():
    created = Planner().create_plan("summarize the latest note")

    assert created.status is PlanStatus.DRAFT
    assert len(created.steps) == 1
    assert created.steps[0].description == "summarize the latest note"


def test_create_plan_splits_request_on_and():
    created = Planner().create_plan("collect facts and summarize findings")

    assert [step.description for step in created.steps] == [
        "collect facts",
        "summarize findings",
    ]


def test_approve_sets_status_and_reviewer():
    planner_instance = Planner()
    plan = planner_instance.create_plan("draft answer")

    approved = planner_instance.approve(plan, reviewer="user-1")

    assert approved.status is PlanStatus.APPROVED
    assert approved.approved_by == "user-1"


def test_planner_package_exports():
    assert planner.Plan is Plan
    assert planner.PlanStatus is PlanStatus
    assert planner.PlanStep is PlanStep
    assert planner.Planner is Planner
