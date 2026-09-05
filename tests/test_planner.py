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
    Planner,
)


def test_durable_domain_values_are_stable() -> None:
    assert [item.value for item in PlanningMode] == ["auto", "always", "never"]
    assert [item.value for item in PlanningRoute] == ["direct", "plan"]
    assert PlanStatus.AWAITING_APPROVAL.value == "awaiting_approval"
    assert PlanDecisionAction.REVISE.value == "revise"
    assert PlanStepRunStatus.FAILED_TERMINAL.value == "failed_terminal"

    draft = PlanDraft(
        objective="Ship the durable plan engine",
        constraints=["No new dependencies"],
        generation_reason="The request spans storage and runtime work.",
        steps=[
            PlanDraftStep(
                logical_step_key="schema",
                title="Create schema",
                description="Persist immutable plan definitions.",
                expected_outcome="Schema constraints pass on both databases.",
                depends_on=[],
                max_attempts=2,
            )
        ],
    )
    completion = PlanStepCompletion(
        status="succeeded",
        summary="Schema checks passed.",
        evidence=["tests/test_migrations.py"],
    )

    assert draft.steps[0].logical_step_key == "schema"
    assert completion.retryable is False


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
