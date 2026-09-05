import json
import re
from uuid import uuid4

import pytest
from pydantic import ValidationError

from multiclaw import planner
from multiclaw.llm import LLMResponse, ToolCall
from multiclaw.planner import (
    Plan,
    PlanDecisionAction,
    PlanDraft,
    PlanDraftStep,
    Planner,
    PlanningMode,
    PlanningRoute,
    PlanRevisionContext,
    PlanStatus,
    PlanStep,
    PlanStepCompletion,
    PlanStepRunStatus,
    PlanTriggerMode,
    ValidatedPlanDraft,
    ValidatedPlanStep,
)
from multiclaw.planner import validation as planner_validation
from multiclaw.planner.generator import PlanGenerationError, PlanGenerator
from multiclaw.planner.policy import PlanningPolicy, PlanningUnavailableError
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


def test_planner_package_exports():
    assert planner.Plan is Plan
    assert planner.PlanStatus is PlanStatus
    assert planner.PlanStep is PlanStep
    assert planner.Planner is Planner
    assert Planner is PlanGenerator


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


def _validated_plan() -> ValidatedPlanDraft:
    return validate_plan_draft(
        _validation_draft(
            [
                _validation_step("inspect"),
                _validation_step("verify", depends_on=["inspect"]),
            ]
        ),
        max_steps=20,
        max_depth=10,
        max_attempts=2,
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


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(False, id="false"),
        pytest.param(True, id="true"),
        pytest.param(1.0, id="float"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param("1", id="string"),
        pytest.param(None, id="none"),
    ],
)
@pytest.mark.parametrize(
    "argument",
    ["max_steps", "max_depth", "max_attempts", "max_content_bytes"],
)
def test_validation_rejects_invalid_limit_arguments(
    argument: str,
    value: object,
) -> None:
    limits: dict[str, object] = {
        "max_steps": 20,
        "max_depth": 10,
        "max_attempts": 2,
        "max_content_bytes": planner.MAX_PLAN_CONTENT_BYTES,
    }
    limits[argument] = value

    with pytest.raises(
        PlanValidationError,
        match=rf"{argument} must be a positive integer",
    ):
        validate_plan_draft(
            _validation_draft([_validation_step("inspect")]),
            **limits,
        )


def test_nan_cannot_bypass_dependency_depth_hard_cap() -> None:
    steps = [
        _validation_step(
            f"s{index}",
            depends_on=[] if index == 0 else [f"s{index - 1}"],
        )
        for index in range(11)
    ]

    with pytest.raises(PlanValidationError, match="max_depth must be a positive integer"):
        validate_plan_draft(
            _validation_draft(steps),
            max_steps=20,
            max_depth=float("nan"),
            max_attempts=2,
        )


def test_nan_content_limit_cannot_bypass_oversized_plan() -> None:
    oversized = _validation_draft([_validation_step("inspect")]).model_copy(
        update={"objective": "x" * (planner.MAX_PLAN_CONTENT_BYTES + 1)}
    )

    with pytest.raises(
        PlanValidationError,
        match="max_content_bytes must be a positive integer",
    ):
        validate_plan_draft(
            oversized,
            max_steps=20,
            max_depth=10,
            max_attempts=2,
            max_content_bytes=float("nan"),
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


@pytest.mark.parametrize(
    "credential",
    [
        "refresh_token=abc123",
        "access-token: abc123",
        "Authorization=Basic abc123",
        "Authorization: Bearer live-token",
        "Bearer abcdefgh",
        "Bearer live-token",
        "github_pat_abc123xyz",
        "api_key=abc123",
        "password=abc123",
        "sk-abc123xyz",
        "sk_abc123xyz",
        "ghp-abc123xyz",
        "ghp_abc123xyz",
    ],
)
def test_validation_and_sanitizer_share_credential_patterns(
    credential: str,
) -> None:
    draft = _validation_draft(
        [_validation_step("inspect", title=f"Inspect {credential}")]
    )

    with pytest.raises(PlanValidationError, match="credential-shaped content"):
        validate_plan_draft(
            draft,
            max_steps=20,
            max_depth=10,
            max_attempts=2,
        )
    assert sanitize_plan_text(credential) == "[REDACTED]"


@pytest.mark.parametrize(
    ("credential", "secret_fragments"),
    [
        (
            'password="correct horse battery staple"',
            ("correct", "horse", "battery", "staple"),
        ),
        ("secret='alpha beta gamma'", ("alpha", "beta", "gamma")),
        (
            'api_key = "key material with spaces"',
            ("material", "spaces"),
        ),
        (
            'Authorization = "Bearer quoted-secret-xyz opaque-token-987"',
            ("quoted-secret-xyz", "opaque-token-987"),
        ),
        ("Authorization=Basic abc123", ("Basic", "abc123")),
        ("password=abc,def", ("abc", "def")),
        ("secret=alpha;beta", ("alpha", "beta")),
        ("Authorization: Bearer abc,def", ("abc", "def")),
        (
            r'password="correct \"horse\" battery staple"',
            ("correct", "horse", "battery", "staple"),
        ),
        (
            'password="correct horse battery staple',
            ("correct", "horse", "battery", "staple"),
        ),
        ('secret="alpha\nbeta gamma"', ("alpha", "beta", "gamma")),
        (
            'api_key="line-one\r\nline-two material"',
            ("line-one", "line-two", "material"),
        ),
        (
            r"secret='alpha \'beta\' gamma'",
            ("alpha", "beta", "gamma"),
        ),
        ("password=\"trailing secret\\", ("trailing", "secret")),
    ],
)
def test_assignment_sanitization_removes_complete_credential_value(
    credential: str,
    secret_fragments: tuple[str, ...],
) -> None:
    raw_draft = _validation_draft(
        [_validation_step("inspect", title=credential)]
    )
    with pytest.raises(PlanValidationError, match="credential-shaped content"):
        validate_plan_draft(
            raw_draft,
            max_steps=20,
            max_depth=10,
            max_attempts=2,
        )

    sanitized = sanitize_plan_text(credential)
    assert sanitized == "[REDACTED]"
    assert all(fragment not in sanitized for fragment in secret_fragments)

    validated = validate_plan_draft(
        _validation_draft([_validation_step("inspect", title=sanitized)]),
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )
    canonical = canonical_plan_bytes(validated).decode("utf-8")
    assert validated.steps[0].title == sanitized
    assert all(fragment not in canonical for fragment in secret_fragments)


def test_benign_authorization_prose_sanitizes_unchanged() -> None:
    text = "authorization: user consent"

    assert sanitize_plan_text(text) == text


def test_benign_authorization_prose_validates() -> None:
    text = "authorization: user consent"

    validated = validate_plan_draft(
        _validation_draft([_validation_step("inspect", title=text)]),
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )

    assert validated.steps[0].title == text


def test_benign_bearer_prose_validates_and_sanitizes_unchanged() -> None:
    text = "Bearer of the release"
    draft = _validation_draft([_validation_step("inspect", title=text)])

    validated = validate_plan_draft(
        draft,
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )

    assert validated.steps[0].title == text
    assert sanitize_plan_text(text) == text


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


def test_validated_plan_immutability_rejects_field_assignment() -> None:
    validated = _validated_plan()

    with pytest.raises(ValidationError, match="frozen"):
        validated.objective = "Mutated objective"


def test_validated_step_immutability_rejects_field_assignment() -> None:
    validated = _validated_plan()

    with pytest.raises(ValidationError, match="frozen"):
        validated.steps[0].title = "Mutated title"


def test_validated_constraints_immutability_preserves_digest() -> None:
    validated = _validated_plan()
    before = plan_content_digest(validated)

    with pytest.raises(AttributeError):
        validated.constraints.append("New constraint")

    assert plan_content_digest(validated) == before


def test_validated_steps_immutability_preserves_digest() -> None:
    validated = _validated_plan()
    before = plan_content_digest(validated)

    with pytest.raises(AttributeError):
        validated.steps.append(validated.steps[0])

    assert plan_content_digest(validated) == before


def test_validated_dependencies_immutability_rejects_missing_dependency() -> None:
    validated = _validated_plan()
    before = plan_content_digest(validated)

    with pytest.raises(AttributeError):
        validated.steps[1].depends_on.append("missing")

    assert "missing" not in validated.steps[1].depends_on
    assert plan_content_digest(validated) == before


def test_validated_snapshot_serializes_nested_tuples_as_json_arrays() -> None:
    payload = json.loads(canonical_plan_bytes(_validated_plan()))

    assert isinstance(payload["constraints"], list)
    assert isinstance(payload["steps"], list)
    assert isinstance(payload["steps"][0]["depends_on"], list)


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


def plan_draft(objective: str = "Deliver the change") -> PlanDraft:
    return PlanDraft(
        objective=objective,
        constraints=["Preserve behavior"],
        generation_reason="The request has dependent steps.",
        steps=[
            _validation_step("inspect"),
            _validation_step("verify", depends_on=["inspect"]),
        ],
    )


class StubRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def completion(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def invalid_plan_response(kind: str) -> LLMResponse:
    draft = plan_draft()
    if kind == "wrong_function":
        calls = [
            ToolCall(
                id="bad",
                name="read_file",
                arguments={"path": "README.md"},
            )
        ]
    elif kind == "multiple":
        calls = [
            ToolCall(
                id="one",
                name="submit_plan",
                arguments=draft.model_dump(),
            ),
            ToolCall(
                id="two",
                name="submit_plan",
                arguments=draft.model_dump(),
            ),
        ]
    elif kind == "cycle":
        cyclic = plan_draft()
        cyclic.steps = [
            _validation_step("a", depends_on=["b"]),
            _validation_step("b", depends_on=["a"]),
        ]
        calls = [
            ToolCall(
                id="bad",
                name="submit_plan",
                arguments=cyclic.model_dump(),
            )
        ]
    elif kind == "credential":
        draft.steps[0].title = (
            "Use Authorization: " + "Bearer generator-canary"
        )
        calls = [
            ToolCall(
                id="bad",
                name="submit_plan",
                arguments=draft.model_dump(),
            )
        ]
    else:
        raise AssertionError(f"unknown invalid kind: {kind}")
    return LLMResponse(content="", tool_calls=calls)


@pytest.mark.asyncio
async def test_explicit_policy_modes_never_call_model() -> None:
    router = StubRouter([])
    policy = PlanningPolicy(
        router=router,
        default_model="default",
        classification_model="",
    )

    never = await policy.decide("simple", PlanningMode.NEVER)
    always = await policy.decide("complex", PlanningMode.ALWAYS)

    assert never.mode is PlanningRoute.DIRECT
    assert always.mode is PlanningRoute.PLAN
    assert router.calls == []


@pytest.mark.asyncio
async def test_auto_classification_failure_falls_back_direct_without_leak() -> None:
    policy = PlanningPolicy(
        router=StubRouter(
            [RuntimeError("provider unavailable token=private-classifier-canary")]
        ),
        default_model="default",
        classification_model="classifier",
    )

    decision = await policy.decide("request", PlanningMode.AUTO)

    assert decision.mode is PlanningRoute.DIRECT
    assert decision.reason == "automatic classification unavailable"
    assert "private-classifier-canary" not in decision.reason


@pytest.mark.asyncio
async def test_disabled_policy_fails_closed_only_for_explicit_always() -> None:
    router = StubRouter([])
    policy = PlanningPolicy(
        router=router,
        default_model="default",
        classification_model="",
        enabled=False,
    )

    assert (await policy.decide("request", PlanningMode.AUTO)).mode is (
        PlanningRoute.DIRECT
    )
    assert (await policy.decide("request", PlanningMode.NEVER)).mode is (
        PlanningRoute.DIRECT
    )
    with pytest.raises(PlanningUnavailableError):
        await policy.decide("request", PlanningMode.ALWAYS)
    assert router.calls == []


@pytest.mark.asyncio
async def test_classifier_accepts_only_its_bounded_function_schema() -> None:
    valid = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="route",
                name="classify_planning_request",
                arguments={
                    "mode": "plan",
                    "reason": "Multiple dependent changes.",
                },
            )
        ],
    )
    router = StubRouter([valid])

    decision = await PlanningPolicy(
        router=router,
        default_model="default",
        classification_model="classifier",
    ).decide("request", PlanningMode.AUTO)

    assert decision.mode is PlanningRoute.PLAN
    assert len(decision.reason) <= 500
    assert router.calls[0]["model"] == "classifier"
    assert [
        item["function"]["name"] for item in router.calls[0]["tools"]
    ] == ["classify_planning_request"]
    assert router.calls[0]["tools"][0]["function"]["parameters"] == (
        planner.PlanningDecision.model_json_schema()
    )
    assert "read_file" not in str(router.calls[0]["tools"])


@pytest.mark.parametrize(
    "tool_calls",
    [
        [],
        [ToolCall(id="wrong", name="read_file", arguments={})],
        [
            ToolCall(
                id="one",
                name="classify_planning_request",
                arguments={"mode": "plan", "reason": "First"},
            ),
            ToolCall(
                id="two",
                name="classify_planning_request",
                arguments={"mode": "direct", "reason": "Second"},
            ),
        ],
        [
            ToolCall(
                id="invalid",
                name="classify_planning_request",
                arguments={"mode": "plan", "reason": "x" * 501},
            )
        ],
    ],
)
@pytest.mark.asyncio
async def test_classifier_invalid_structured_output_falls_back_direct(
    tool_calls: list[ToolCall],
) -> None:
    router = StubRouter([LLMResponse(content="", tool_calls=tool_calls)])

    decision = await PlanningPolicy(
        router=router,
        default_model="default",
        classification_model="",
    ).decide("request", PlanningMode.AUTO)

    assert decision == planner.PlanningDecision(
        mode=PlanningRoute.DIRECT,
        reason="automatic classification unavailable",
    )
    assert router.calls[0]["model"] == "default"


@pytest.mark.asyncio
async def test_generator_passes_only_submit_plan_schema_and_validates_payload() -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="plan",
                name="submit_plan",
                arguments=plan_draft().model_dump(),
            )
        ],
    )
    router = StubRouter([response])

    generated = await PlanGenerator(
        router,
        default_model="default",
        generation_model="planner",
    ).generate(
        objective="Deliver the change",
        revision=None,
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )

    assert generated.objective == "Deliver the change"
    assert [step.logical_step_key for step in generated.steps] == [
        "inspect",
        "verify",
    ]
    assert router.calls[0]["model"] == "planner"
    assert [
        tool["function"]["name"] for tool in router.calls[0]["tools"]
    ] == ["submit_plan"]
    assert router.calls[0]["tools"][0]["function"]["parameters"] == (
        PlanDraft.model_json_schema()
    )
    assert "read_file" not in str(router.calls[0]["tools"])


@pytest.mark.asyncio
async def test_generator_repairs_once_then_fails_closed_without_raw_output() -> None:
    canary = "Authorization: Bearer repair-output-canary"
    invalid = LLMResponse(content=f"not structured {canary}", tool_calls=[])
    router = StubRouter([invalid, invalid])
    generator = PlanGenerator(
        router,
        default_model="default",
        generation_model="",
    )

    with pytest.raises(
        PlanGenerationError,
        match="two invalid structured responses",
    ) as exc_info:
        await generator.generate(
            objective="Deliver",
            revision=None,
            max_steps=20,
            max_depth=10,
            max_attempts=2,
        )

    assert len(router.calls) == 2
    assert all(
        [tool["function"]["name"] for tool in call["tools"]]
        == ["submit_plan"]
        for call in router.calls
    )
    first_messages = router.calls[0]["messages"]
    second_messages = router.calls[1]["messages"]
    assert len(second_messages) == len(first_messages) + 1
    assert second_messages[-1]["role"] == "system"
    assert canary not in str(second_messages)
    assert "not structured" not in str(second_messages)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.asyncio
async def test_generator_redacts_credential_shaped_repair_locations() -> None:
    canary = "Authorization: Bearer field-location-canary"
    invalid_payload = plan_draft().model_dump()
    invalid_payload[canary] = True
    invalid = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="invalid",
                name="submit_plan",
                arguments=invalid_payload,
            )
        ],
    )
    valid = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="valid",
                name="submit_plan",
                arguments=plan_draft().model_dump(),
            )
        ],
    )
    router = StubRouter([invalid, valid])

    await PlanGenerator(
        router,
        default_model="default",
        generation_model="planner",
    ).generate(objective="Deliver")

    repair = str(router.calls[1]["messages"][-1])
    assert "field-location-canary" not in repair
    assert "[REDACTED]" in repair


@pytest.mark.parametrize(
    "invalid_kind",
    ["wrong_function", "multiple", "cycle", "credential"],
)
@pytest.mark.asyncio
async def test_generator_rejects_every_invalid_structured_shape(
    invalid_kind: str,
) -> None:
    invalid = invalid_plan_response(invalid_kind)
    router = StubRouter([invalid, invalid])
    generator = PlanGenerator(
        router,
        default_model="default",
        generation_model="planner",
    )

    with pytest.raises(
        PlanGenerationError,
        match="two invalid structured responses",
    ):
        await generator.generate(
            objective="Deliver",
            revision=None,
            max_steps=20,
            max_depth=10,
            max_attempts=2,
        )

    assert len(router.calls) == 2
    assert all(
        [tool["function"]["name"] for tool in call["tools"]]
        == ["submit_plan"]
        for call in router.calls
    )


@pytest.mark.asyncio
async def test_generator_owns_objective_and_redacts_revision_context() -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="plan",
                name="submit_plan",
                arguments=plan_draft(
                    objective="Model substituted objective"
                ).model_dump(),
            )
        ],
    )
    router = StubRouter([response])
    canary = "Authorization: " + "Bearer revision-canary"
    filesystem_path = "/workspace/project/docs/recovery.md"
    revision = PlanRevisionContext(
        plan_id=str(uuid4()),
        parent_version=1,
        feedback=f"Retry {filesystem_path} without {canary}",
        failed_step_key="verify",
        failed_error=f"Failure at {filesystem_path}: {canary}",
        completed=[],
    )

    generated = await PlanGenerator(
        router,
        default_model="default",
        generation_model="planner",
    ).generate(
        objective=f"Deliver while removing {canary}",
        revision=revision,
        max_steps=20,
        max_depth=10,
        max_attempts=2,
    )

    assert generated.objective == "Deliver while removing [REDACTED]"
    messages = str(router.calls[0]["messages"])
    assert canary not in messages
    assert "revision-canary" not in messages
    assert "[REDACTED]" in messages
    assert filesystem_path in messages


@pytest.mark.asyncio
async def test_generator_provider_failure_is_bounded_and_does_not_leak() -> None:
    router = StubRouter(
        [RuntimeError("provider unavailable Authorization: Bearer provider-canary")]
    )

    with pytest.raises(PlanGenerationError) as exc_info:
        await PlanGenerator(
            router,
            default_model="default",
            generation_model="planner",
        ).generate(
            objective="Deliver",
            revision=None,
            max_steps=20,
            max_depth=10,
            max_attempts=2,
        )

    assert str(exc_info.value) == "plan generation unavailable"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_plan_revision_context_is_strict() -> None:
    with pytest.raises(ValidationError):
        PlanRevisionContext.model_validate(
            {
                "plan_id": str(uuid4()),
                "parent_version": 1,
                "feedback": None,
                "failed_step_key": None,
                "failed_error": None,
                "completed": [],
                "unexpected": True,
            }
        )
