from copy import deepcopy

from pydantic import BaseModel, ValidationError

from multiclaw.llm import CompletionRouter, LLMProviderError, LLMResponseParseError
from multiclaw.planner.models import (
    PlanningDecision,
    PlanningMode,
    PlanningRoute,
)
from multiclaw.planner.validation import sanitize_plan_text


class PlanningUnavailableError(RuntimeError):
    pass


class _ClassificationResponseError(ValueError):
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
        router: CompletionRouter,
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
            safe_request = sanitize_plan_text(request)
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
                    {"role": "user", "content": safe_request},
                ],
                tools=[deepcopy(CLASSIFY_SCHEMA)],
            )
            tool_calls = getattr(response, "tool_calls", None)
            if not isinstance(tool_calls, list) or len(tool_calls) != 1:
                raise _ClassificationResponseError
            call = tool_calls[0]
            if getattr(call, "name", None) != "classify_planning_request":
                raise _ClassificationResponseError
            decision = PlanningDecision.model_validate(
                getattr(call, "arguments", None)
            )
            return PlanningDecision(
                mode=decision.mode,
                reason=sanitize_plan_text(decision.reason),
            )
        except (
            LLMProviderError,
            LLMResponseParseError,
            _ClassificationResponseError,
            ValidationError,
        ):
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
