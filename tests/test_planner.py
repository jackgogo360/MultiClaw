import re

import pytest
from pydantic import ValidationError

from multiclaw import planner
from multiclaw.planner import (
    Plan,
    PlanDecisionAction,
    PlanDraft,
    PlanDraftStep,
    Planner,
    PlanningMode,
    PlanningRoute,
    PlanStatus,
    PlanStep,
    PlanStepCompletion,
    PlanStepRunStatus,
    PlanTriggerMode,
    ValidatedPlanDraft,
    ValidatedPlanStep,
)
from multiclaw.planner import validation as planner_validation
from multiclaw.planner.validation import (
    PlanValidationError,
    canonical_plan_bytes,
    plan_content_digest,
    sanitize_plan_text,
    step_definition_digest,
    validate_plan_draft,
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


def validated_plan_draft_payload(
    step_count: int = 1,
    **overrides: object,
) -> dict[str, object]:
    steps = [
        plan_draft_step_payload(
            logical_step_key=f"step-{index}",
            ordinal=index + 1,
        )
        for index in range(step_count)
    ]
    return plan_draft_payload(steps=steps, **overrides)


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


def _validation_step(
    key: str,
    *,
    depends_on: list[str] | None = None,
    title: str | None = None,
    max_attempts: int = 2,
) -> PlanDraftStep:
    return PlanDraftStep(
        logical_step_key=key,
        title=title or key.title(),
        description=f"Execute {key}.",
        expected_outcome=f"{key} is verified.",
        depends_on=depends_on or [],
        max_attempts=max_attempts,
    )


def _validation_draft(steps: list[PlanDraftStep]) -> PlanDraft:
    return PlanDraft(
        objective="Deliver a tested change",
        constraints=["Preserve behavior"],
        generation_reason="Multiple dependent actions are required.",
        steps=steps,
    )


def test_validation_returns_stable_topological_order() -> None:
    draft = _validation_draft(
        [
            _validation_step("publish", depends_on=["test"]),
            _validation_step("lint"),
            _validation_step("test"),
        ]
    )

    validated = validate_plan_draft(
        draft,
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )

    assert [step.logical_step_key for step in validated.steps] == [
        "lint",
        "test",
        "publish",
    ]
    assert [step.ordinal for step in validated.steps] == [1, 2, 3]


def test_validation_preserves_deterministic_order_for_disconnected_dag() -> None:
    draft = _validation_draft(
        [
            _validation_step("package", depends_on=["build"]),
            _validation_step("lint"),
            _validation_step("build"),
            _validation_step("report", depends_on=["lint"]),
        ]
    )

    first = validate_plan_draft(
        draft,
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )
    second = validate_plan_draft(
        draft.model_copy(deep=True),
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )

    assert [step.logical_step_key for step in first.steps] == [
        "lint",
        "build",
        "package",
        "report",
    ]
    assert first == second


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([_validation_step("a", depends_on=["missing"])], "missing dependency"),
        ([_validation_step("a", depends_on=["a"])], "self dependency"),
        (
            [
                _validation_step("a", depends_on=["b"]),
                _validation_step("b", depends_on=["a"]),
            ],
            "cycle",
        ),
        (
            [
                _validation_step("a", depends_on=["b", "b"]),
                _validation_step("b"),
            ],
            "duplicate dependency",
        ),
        ([_validation_step("a"), _validation_step("a")], "duplicate logical_step_key"),
    ],
)
def test_validation_rejects_invalid_graphs(
    steps: list[PlanDraftStep],
    message: str,
) -> None:
    with pytest.raises(PlanValidationError, match=message):
        validate_plan_draft(
            _validation_draft(steps),
            max_steps=20,
            max_depth=10,
            max_attempts=2,
        )


def test_validation_rejects_configured_step_limit() -> None:
    with pytest.raises(PlanValidationError, match="configured step limit"):
        validate_plan_draft(
            _validation_draft([_validation_step("a"), _validation_step("b")]),
            max_steps=1,
            max_depth=10,
            max_attempts=2,
        )


@pytest.mark.parametrize(
    ("max_depth", "chain_length"),
    [(2, 3), (20, 11)],
)
def test_validation_rejects_configured_and_hard_dependency_depth_limits(
    max_depth: int,
    chain_length: int,
) -> None:
    steps = [
        _validation_step(
            f"s{index}",
            depends_on=[] if index == 0 else [f"s{index - 1}"],
        )
        for index in range(chain_length)
    ]

    with pytest.raises(PlanValidationError, match="dependency depth"):
        validate_plan_draft(
            _validation_draft(steps),
            max_steps=20,
            max_depth=max_depth,
            max_attempts=2,
        )


def test_validation_rejects_step_attempts_above_configured_limit() -> None:
    with pytest.raises(PlanValidationError, match="max_attempts"):
        validate_plan_draft(
            _validation_draft([_validation_step("retry", max_attempts=3)]),
            max_steps=20,
            max_depth=10,
            max_attempts=2,
        )


def test_validation_rejects_raw_credentials_and_oversized_canonical_content() -> None:
    credential = "Authorization: " + "Bearer live-token"
    secret = _validation_draft(
        [_validation_step("inspect", title=f"Use {credential}")]
    )
    with pytest.raises(PlanValidationError, match="credential-shaped"):
        validate_plan_draft(
            secret,
            max_steps=20,
            max_depth=10,
            max_attempts=2,
        )

    oversized = _validation_draft([_validation_step(f"s{i}") for i in range(20)])
    oversized.steps[0].description = "x" * 4_000
    with pytest.raises(PlanValidationError, match="100 bytes"):
        validate_plan_draft(
            oversized,
            max_steps=20,
            max_depth=10,
            max_attempts=2,
            max_content_bytes=100,
        )


def test_credential_scanner_rejects_credential_shaped_mapping_key() -> None:
    # Validated plans are closed models, so exercise the recursive mapping boundary
    # directly without widening the production API for a test-only entry point.
    with pytest.raises(PlanValidationError, match="credential-shaped field"):
        planner_validation._reject_credentials(
            {"metadata": {"api_key": "ordinary-value"}}
        )


def test_sanitize_plan_text_redacts_credentials_only_when_requested() -> None:
    assert sanitize_plan_text("Authorization: Bearer live-token") == "[REDACTED]"
    assert sanitize_plan_text("Inspect the authorization flow") == (
        "Inspect the authorization flow"
    )


def test_canonical_digests_ignore_mapping_order_but_include_definition_changes() -> None:
    draft = _validation_draft(
        [_validation_step("a"), _validation_step("b", depends_on=["a"])]
    )
    validated = validate_plan_draft(
        draft,
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )

    assert canonical_plan_bytes(validated) == canonical_plan_bytes(
        validated.model_copy(deep=True)
    )
    digest = plan_content_digest(validated)
    assert len(digest) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    before = step_definition_digest(validated.steps[0])
    changed = validated.steps[0].model_copy(
        update={"expected_outcome": "A different result."}
    )
    assert step_definition_digest(changed) != before


def test_step_definition_digest_excludes_dependencies_and_ordinal() -> None:
    validated = validate_plan_draft(
        _validation_draft([_validation_step("a"), _validation_step("b")]),
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )
    step = validated.steps[1]

    changed = step.model_copy(update={"depends_on": ["a"], "ordinal": 20})

    assert step_definition_digest(changed) == step_definition_digest(step)


@pytest.mark.parametrize("ordinal", [1, 20])
def test_validated_plan_step_accepts_ordinal_boundaries(ordinal: int) -> None:
    step = ValidatedPlanStep.model_validate(
        plan_draft_step_payload(ordinal=ordinal)
    )

    assert step.ordinal == ordinal


@pytest.mark.parametrize("ordinal", [0, 21])
def test_validated_plan_step_rejects_invalid_ordinals(ordinal: int) -> None:
    with pytest.raises(ValidationError):
        ValidatedPlanStep.model_validate(plan_draft_step_payload(ordinal=ordinal))


def test_validated_plan_step_retains_draft_step_constraints() -> None:
    with pytest.raises(ValidationError):
        ValidatedPlanStep.model_validate(
            plan_draft_step_payload(logical_step_key="UPPERCASE", ordinal=1)
        )


@pytest.mark.parametrize("step_count", [1, 20])
def test_validated_plan_draft_accepts_step_container_boundaries(
    step_count: int,
) -> None:
    draft = ValidatedPlanDraft.model_validate(
        validated_plan_draft_payload(step_count)
    )

    assert len(draft.steps) == step_count


@pytest.mark.parametrize("step_count", [0, 21])
def test_validated_plan_draft_rejects_step_container_overflow(
    step_count: int,
) -> None:
    with pytest.raises(ValidationError):
        ValidatedPlanDraft.model_validate(validated_plan_draft_payload(step_count))


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (
            ValidatedPlanStep,
            plan_draft_step_payload(ordinal=1, unexpected=True),
        ),
        (
            ValidatedPlanDraft,
            validated_plan_draft_payload(unexpected=True),
        ),
    ],
)
def test_validated_plan_models_reject_extra_fields(model_type, payload) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model_type.model_validate(payload)

    assert any(
        error["type"] == "extra_forbidden" and error["loc"] == ("unexpected",)
        for error in exc_info.value.errors()
    )


def test_planner_package_exports_validation_api() -> None:
    assert planner.PlanValidationError is PlanValidationError
    assert planner.canonical_plan_bytes is canonical_plan_bytes
    assert planner.plan_content_digest is plan_content_digest
    assert planner.sanitize_plan_text is sanitize_plan_text
    assert planner.step_definition_digest is step_definition_digest
    assert planner.validate_plan_draft is validate_plan_draft
    assert planner.ValidatedPlanDraft is ValidatedPlanDraft
    assert planner.ValidatedPlanStep is ValidatedPlanStep
