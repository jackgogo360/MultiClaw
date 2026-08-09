import asyncio
import logging
import re
import uuid
from typing import Any

from multiclaw.events import Event, EventBus
from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
from multiclaw.tools.base import ToolBuilder, ToolExecutionResult, ToolStatus

logger = logging.getLogger(__name__)


class CoreToolScheduler:
    _AUDIT_ALLOWLIST = (
        "sandbox_backend",
        "sandbox_profile",
        "unsafe_fallback_used",
    )

    def __init__(
        self,
        permission_checker: PermissionChecker,
        execution_guard: ExecutionGuard,
        audit_logger: InMemoryAuditLogger,
        event_bus: EventBus,
    ) -> None:
        self.permission_checker = permission_checker
        self.execution_guard = execution_guard
        self.audit_logger = audit_logger
        self.event_bus = event_bus
        self._pending: dict[str, asyncio.Event] = {}
        self._pending_results: dict[str, bool] = {}

    def resolve_approval(self, request_id: str, approved: bool) -> bool:
        if request_id in self._pending:
            self._pending_results[request_id] = approved
            self._pending[request_id].set()
            return True
        return False

    async def can_run_concurrently(
        self,
        builder: ToolBuilder,
        raw_params: dict[str, Any],
    ) -> bool:
        if not builder.read_only:
            return False

        decision = await self.permission_checker.check(
            builder.name,
            raw_params,
            workspace_root=getattr(builder, "workspace_root", None),
        )
        return decision.allow and not decision.requires_approval

    async def run(
        self,
        builder: ToolBuilder,
        raw_params: dict[str, Any],
    ) -> ToolExecutionResult:
        try:
            await self.event_bus.publish(
                Event(type="tool.scheduled", data={"tool": builder.name})
            )
            await self.event_bus.publish(
                Event(type="tool.validating", data={"tool": builder.name})
            )
            params = builder.validate(raw_params)

            # MCP tools are pre-approved at server-connection time
            from multiclaw.mcp.tool_adapter import MCPToolBuilder as _MCPToolBuilder
            if isinstance(builder, _MCPToolBuilder):
                invocation = builder.build(params)
                try:
                    result = await self.execution_guard.run(invocation.execute)
                except Exception as exc:
                    del exc
                    result = ToolExecutionResult(
                        status=ToolStatus.ERROR,
                        content="tool execution failed",
                    )
                    await self._finalize_terminal_result(
                        builder.name,
                        result,
                        audit_detail="tool execution failed",
                        error_label="tool execution failed",
                    )
                    return result
                await self._finalize_terminal_result(builder.name, result)
                return result

            decision = await self.permission_checker.check(
                builder.name,
                raw_params,
                workspace_root=getattr(builder, "workspace_root", None),
            )
            if decision.requires_approval:
                request_id = uuid.uuid4().hex[:12]
                logger.info(
                    "approval required: tool=%s request_id=%s reason=%s",
                    builder.name, request_id, decision.reason,
                )
                event = asyncio.Event()
                self._pending[request_id] = event

                await self.event_bus.publish(
                    Event(
                        type="tool.awaiting_approval",
                        data={
                            "request_id": request_id,
                            "tool": builder.name,
                            "params": raw_params,
                            "description": builder.approval_description(raw_params),
                        },
                    )
                )
                await self.audit_logger.record(
                    tool_name=builder.name,
                    status=ToolStatus.AWAITING_APPROVAL.value,
                    detail=f"approval required, request_id={request_id}",
                )

                try:
                    await asyncio.wait_for(event.wait(), timeout=120.0)
                except asyncio.TimeoutError:
                    logger.error(
                        "approval timeout: tool=%s request_id=%s",
                        builder.name, request_id,
                    )
                    self._pending.pop(request_id, None)
                    self._pending_results.pop(request_id, None)
                    result = ToolExecutionResult(
                        status=ToolStatus.CANCELLED,
                        content="Approval timed out after 120s.",
                    )
                    await self._finalize_terminal_result(
                        builder.name,
                        result,
                        audit_detail=result.content,
                    )
                    return result
                approved = self._pending_results.pop(request_id, False)
                self._pending.pop(request_id, None)

                if not approved:
                    result = ToolExecutionResult(
                        status=ToolStatus.CANCELLED,
                        content="rejected by user",
                    )
                    await self._finalize_terminal_result(
                        builder.name,
                        result,
                        audit_detail=result.content,
                    )
                    return result

            if not decision.allow:
                await self.event_bus.publish(
                    Event(
                        type="tool.error",
                        data={"tool": builder.name, "error": decision.reason},
                    )
                )
                await self.audit_logger.record(
                    tool_name=builder.name,
                    status=ToolStatus.CANCELLED.value,
                    detail=decision.reason,
                )
                return ToolExecutionResult(
                    status=ToolStatus.CANCELLED,
                    content=decision.reason,
                )

            invocation = builder.build(params)
            invocation.configure_permission(decision.approved_roots)
            await self.event_bus.publish(
                Event(type="tool.executing", data={"tool": builder.name})
            )
            result = await self.execution_guard.run(invocation.execute)
        except Exception as exc:
            del exc
            result = ToolExecutionResult(
                status=ToolStatus.ERROR,
                content="tool execution failed",
            )
            await self._finalize_terminal_result(
                builder.name,
                result,
                audit_detail="tool execution failed",
                error_label="tool execution failed",
            )
            return result

        await self._finalize_terminal_result(builder.name, result)
        return result

    def _audit_detail(self, result: ToolExecutionResult) -> str:
        allowlisted = self._normalized_audit_fields(result.audit)
        if not allowlisted:
            return result.content

        prefix = " ".join(f"{key}={allowlisted[key]}" for key in allowlisted)
        if not result.content:
            return f"[audit] {prefix}"
        return f"[audit] {prefix}\n{result.content}"

    async def _finalize_terminal_result(
        self,
        tool_name: str,
        result: ToolExecutionResult,
        *,
        audit_detail: str | None = None,
        error_label: str | None = None,
    ) -> None:
        await self.audit_logger.record(
            tool_name=tool_name,
            status=result.status.value,
            detail=audit_detail if audit_detail is not None else self._audit_detail(result),
        )
        await self._publish_result_event(
            tool_name,
            result,
            error_label=error_label,
        )

    async def _publish_result_event(
        self,
        tool_name: str,
        result: ToolExecutionResult,
        *,
        error_label: str | None = None,
    ) -> None:
        if result.status == ToolStatus.SUCCESS:
            await self.event_bus.publish(
                Event(type="tool.completed", data={"tool": tool_name})
            )
            return

        resolved_error_label = error_label or (
            "tool returned error"
            if result.status == ToolStatus.ERROR
            else f"tool returned {result.status.value}"
        )
        await self.event_bus.publish(
            Event(type="tool.error", data={"tool": tool_name, "error": resolved_error_label})
        )

    def _normalized_audit_fields(self, audit: dict[str, Any]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key in sorted(self._AUDIT_ALLOWLIST):
            if key not in audit:
                continue
            if key == "unsafe_fallback_used":
                if type(audit[key]) is bool:
                    normalized[key] = "True" if audit[key] else "False"
                continue
            value = audit[key]
            if not isinstance(value, str):
                continue
            safe_value = self._sanitize_audit_token(value)
            if safe_value:
                normalized[key] = safe_value
        return normalized

    def _sanitize_audit_token(self, value: str) -> str:
        token = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
        token = re.sub(r"_+", "_", token).strip("._-")
        if not token:
            return ""
        return token[:80]
