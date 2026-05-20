from multiclaw.planner.models import Plan, PlanStatus, PlanStep


class Planner:
    def create_plan(self, request: str) -> Plan:
        parts = [part.strip() for part in request.split(" and ") if part.strip()]
        steps = [
            PlanStep(
                order=index,
                description=part,
                expected_outcome=f"completed: {part}",
            )
            for index, part in enumerate(parts, start=1)
        ]
        return Plan(steps=steps)

    def approve(self, plan: Plan, reviewer: str) -> Plan:
        plan.status = PlanStatus.APPROVED
        plan.approved_by = reviewer
        return plan

    def summary(self, plan: Plan) -> str:
        return " | ".join(f"{step.order}. {step.description}" for step in plan.steps)
