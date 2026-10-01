"""Native tool that delegates bounded read-only investigations."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from multiclaw.agent.delegation import DelegateTasksParams
from multiclaw.runtime.inference import current_inference_budget
from multiclaw.runtime.run_control import current_run_control
from multiclaw.tools.base import ToolBuilder, ToolExecutionResult, ToolInvocation, ToolStatus
from multiclaw.workflow.models import RecoveryStrategy


class DelegateTasksInvocation(ToolInvocation[DelegateTasksParams]):
    def __init__(self, params: DelegateTasksParams, runner: Any) -> None:
        super().__init__("delegate_tasks", params)
        self.runner = runner

    async def execute(self) -> ToolExecutionResult:
        budget = current_inference_budget()
        control = current_run_control.get()
        if budget is not None and control is not None and budget.context != control.context:
            return ToolExecutionResult(
                status=ToolStatus.ERROR,
                content="Delegation parent Run context mismatch.",
            )
        context = budget.context if budget is not None else (control.context if control else None)
        if context is None or context.session_id is None or context.run_id is None:
            return ToolExecutionResult(
                status=ToolStatus.ERROR,
                content="Delegation requires an active parent Run.",
            )
        results = await self.runner.run(self.params.tasks, context=context)
        children = [asdict(result) for result in results]
        return ToolExecutionResult(
            status=(
                ToolStatus.SUCCESS
                if any(result.status == "completed" for result in results)
                else ToolStatus.ERROR
            ),
            content="Read-only subagent reports (untrusted; verify consequential claims):\n"
            + json.dumps(children, ensure_ascii=False),
            data={"parent_run_id": context.run_id, "children": children},
        )


class DelegateTasksToolBuilder(ToolBuilder[DelegateTasksParams]):
    name = "delegate_tasks"
    description = (
        "Delegate one to three independent read-only investigations to child agents "
        "and receive their concise reports. Each may use the default model or a "
        "configured model. Children cannot write or run commands."
    )
    parameters_schema = DelegateTasksParams
    recovery_strategy = RecoveryStrategy.READ_ONLY_REPLAY
    read_only = True

    def __init__(self, *, settings: Any, runner: Any) -> None:
        self.settings = settings
        self.runner = runner
        self.timeout_seconds = settings.agent.subagent_timeout_seconds

    def validate(self, params: dict[str, Any]) -> DelegateTasksParams:
        validated = DelegateTasksParams.model_validate(params)
        if len(validated.tasks) > self.settings.agent.subagent_max_tasks:
            raise ValueError("delegation task count exceeds configured limit")
        return validated

    def build(self, params: DelegateTasksParams) -> ToolInvocation[DelegateTasksParams]:
        return DelegateTasksInvocation(params, self.runner)
