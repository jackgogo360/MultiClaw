from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from multiclaw.llm import (
    CompletionRouter,
    LLMProviderError,
    LLMResponse,
    LLMResponseParseError,
)
from multiclaw.planner.models import (
    PlanDraft,
    PlanRevisionContext,
    ValidatedPlanDraft,
)
from multiclaw.planner.policy import function_schema
from multiclaw.planner.validation import (
    PlanValidationError,
    sanitize_plan_text,
    validate_plan_draft,
)


class PlanGenerationError(RuntimeError):
    pass


class _StructuredResponseError(PlanValidationError):
    def __init__(self, location: str) -> None:
        super().__init__("invalid structured response")
        self.location = location


def _submit_plan_schema() -> dict[str, object]:
    return function_schema(
        "submit_plan",
        "Submit a bounded dependency-aware plan for validation.",
        PlanDraft,
    )


async def _request_plan(
    router: CompletionRouter,
    *,
    model: str,
    messages: list[dict[str, str]],
) -> LLMResponse:
    return await router.completion(
        model=model,
        messages=messages,
        tools=[_submit_plan_schema()],
    )


def _sanitize_prompt_value(value: object) -> object:
    if isinstance(value, str):
        return sanitize_plan_text(value)
    if isinstance(value, list):
        return [_sanitize_prompt_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_prompt_value(item) for key, item in value.items()}
    return value


def _initial_messages(
    objective: str,
    revision: PlanRevisionContext | None,
) -> list[dict[str, str]]:
    system = {
        "role": "system",
        "content": (
            "Create a bounded dependency-aware plan. Return exactly one "
            "submit_plan function call. Do not execute the objective."
        ),
    }
    user_content = f"Requested objective:\n{objective}"
    if revision is not None:
        revision_payload = _sanitize_prompt_value(revision.model_dump(mode="json"))
        user_content += "\nCredential-redacted revision context:\n" + json.dumps(
            revision_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return [system, {"role": "user", "content": user_content}]


def _validation_locations(error: Exception) -> list[str]:
    if isinstance(error, ValidationError):
        locations = []
        for detail in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )[:8]:
            location = ".".join(
                sanitize_plan_text(str(part))[:64] for part in detail["loc"]
            )
            locations.append(location[:256] or "plan")
        return locations or ["plan"]
    if isinstance(error, _StructuredResponseError):
        return [error.location[:256]]
    return ["plan"]


def _repair_message(error: Exception) -> dict[str, str]:
    error_class = (
        "ValidationError"
        if isinstance(error, ValidationError)
        else "PlanValidationError"
    )
    locations = ",".join(_validation_locations(error))
    return {
        "role": "system",
        "content": (
            f"Invalid structured response; error_class={error_class}; "
            f"field_locations={locations}. Return exactly one valid "
            "submit_plan function call."
        ),
    }


def _validate_response(
    response: Any,
    *,
    safe_objective: str,
    max_steps: int,
    max_depth: int,
    max_attempts: int,
) -> tuple[ValidatedPlanDraft | None, dict[str, str] | None]:
    try:
        validated = PlanGenerator._validated_response(
            response,
            safe_objective=safe_objective,
            max_steps=max_steps,
            max_depth=max_depth,
            max_attempts=max_attempts,
        )
    except (ValidationError, PlanValidationError) as error:
        return None, _repair_message(error)
    return validated, None


class PlanGenerator:
    def __init__(
        self,
        router: CompletionRouter | None = None,
        *,
        default_model: str = "",
        generation_model: str = "",
    ) -> None:
        self._router = router
        self._default_model = default_model
        self._generation_model = generation_model

    async def generate(
        self,
        objective: str,
        revision: PlanRevisionContext | None = None,
        max_steps: int = 20,
        max_depth: int = 10,
        max_attempts: int = 2,
        reserve_round: Callable[[int], Awaitable[None]] | None = None,
    ) -> ValidatedPlanDraft:
        if self._router is None:
            raise PlanGenerationError("plan generation unavailable")
        safe_objective = sanitize_plan_text(objective)
        if not safe_objective.strip() or len(safe_objective) > 16_000:
            raise PlanGenerationError("invalid planning objective")
        messages = _initial_messages(safe_objective, revision)

        for attempt in range(2):
            if reserve_round is not None:
                await reserve_round(attempt)
            provider_unavailable = False
            response_invalid = False
            try:
                response = await _request_plan(
                    self._router,
                    model=self._generation_model or self._default_model,
                    messages=messages,
                )
            except LLMProviderError:
                provider_unavailable = True
            except LLMResponseParseError:
                response_invalid = True

            if provider_unavailable:
                raise PlanGenerationError("plan generation unavailable") from None

            validated: ValidatedPlanDraft | None
            repair: dict[str, str] | None
            if response_invalid:
                validated = None
                repair = _repair_message(_StructuredResponseError("response"))
            else:
                validated, repair = _validate_response(
                    response,
                    safe_objective=safe_objective,
                    max_steps=max_steps,
                    max_depth=max_depth,
                    max_attempts=max_attempts,
                )
            if validated is not None:
                return validated
            if attempt == 0:
                assert repair is not None
                messages = [*messages, repair]
                continue
            raise PlanGenerationError("two invalid structured responses") from None

        raise AssertionError("unreachable")

    @staticmethod
    def _validated_response(
        response: Any,
        *,
        safe_objective: str,
        max_steps: int,
        max_depth: int,
        max_attempts: int,
    ) -> ValidatedPlanDraft:
        tool_calls = getattr(response, "tool_calls", None)
        if not isinstance(tool_calls, list):
            raise _StructuredResponseError("response")
        if len(tool_calls) != 1:
            raise _StructuredResponseError("tool_calls")
        call = tool_calls[0]
        if getattr(call, "name", None) != "submit_plan":
            raise _StructuredResponseError("tool_calls.0.name")

        draft = PlanDraft.model_validate(getattr(call, "arguments", None))
        draft = draft.model_copy(update={"objective": safe_objective})
        return validate_plan_draft(
            draft,
            max_steps=max_steps,
            max_depth=max_depth,
            max_attempts=max_attempts,
        )


__all__ = [
    "PlanGenerationError",
    "PlanGenerator",
]
