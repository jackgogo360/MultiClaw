from typing import Any

from pydantic import BaseModel

from multiclaw.planner.models import (
    PlanningDecision,
    PlanningMode,
    PlanningRoute,
)


class PlanningUnavailableError(RuntimeError):
    pass


def function_schema(
    name: str,
    description: str,
    model: type[BaseModel],
) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": model.model_json_schema(),
        },
    }


CLASSIFY_SCHEMA = function_schema(
    "classify_planning_request",
    "Choose whether this request needs a multi-step plan.",
    PlanningDecision,
)


class PlanningPolicy:
    def __init__(
        self,
        router: Any,
        *,
        default_model: str,
        classification_model: str,
        enabled: bool = True,
    ) -> None:
        self._router = router
        self._default_model = default_model
        self._classification_model = classification_model
        self._enabled = enabled

    async def decide(
        self,
        request: str,
        mode: PlanningMode,
    ) -> PlanningDecision:
        if mode is PlanningMode.NEVER:
            return PlanningDecision(
                mode=PlanningRoute.DIRECT,
                reason="planning explicitly disabled",
            )
        if mode is PlanningMode.ALWAYS:
            if not self._enabled:
                raise PlanningUnavailableError("planning is disabled")
            return PlanningDecision(
                mode=PlanningRoute.PLAN,
                reason="planning explicitly requested",
            )
        if not self._enabled:
            return PlanningDecision(
                mode=PlanningRoute.DIRECT,
                reason="planning is disabled",
            )

        try:
            response = await self._router.completion(
                model=self._classification_model or self._default_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classify whether the request requires a dependent "
                            "multi-step plan. Return exactly one "
                            "classify_planning_request function call."
                        ),
                    },
                    {"role": "user", "content": request},
                ],
                tools=[CLASSIFY_SCHEMA],
            )
            if len(response.tool_calls) != 1:
                raise ValueError("classification requires exactly one tool call")
            call = response.tool_calls[0]
            if call.name != "classify_planning_request":
                raise ValueError("unexpected classification function")
            return PlanningDecision.model_validate(call.arguments)
        except Exception:  # noqa: BLE001 - classification must fail open
            return PlanningDecision(
                mode=PlanningRoute.DIRECT,
                reason="automatic classification unavailable",
            )


__all__ = [
    "CLASSIFY_SCHEMA",
    "PlanningPolicy",
    "PlanningUnavailableError",
    "function_schema",
]
