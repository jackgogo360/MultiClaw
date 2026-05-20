import multiclaw.planner as planner
from multiclaw.planner import Plan, PlanStatus, PlanStep, Planner


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
