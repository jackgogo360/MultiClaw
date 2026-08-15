import asyncio
import logging
import re
import uuid
from typing import Any

from multiclaw.events import Event, EventBus, EventRouter, ScopedEvent
from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
from multiclaw.tenancy import TenantContext
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
        event_router: EventRouter | None = None,
    ) -> None:
        self.permission_checker = permission_checker
        self.execution_guard = execution_guard
        self.audit_logger = audit_logger
        self.event_bus = event_bus
        self.event_router = event_router
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
        *,
        context: TenantContext | None = None,
        call_id: str | None = None,
    ) -> ToolExecutionResult:
        try:
            await self._publish_event(
                "tool.scheduled",
                self._event_data(builder.name, call_id),
                context=context,
            )
            await self._publish_event(
                "tool.validating",
                self._event_data(builder.name, call_id),
                context=context,
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
                        context=context,
                        call_id=call_id,
                        audit_detail="tool execution failed",
                        error_label="tool execution failed",
                    )
                    return result
                await self._finalize_terminal_result(
                    builder.name,
                    result,
                    context=context,
                    call_id=call_id,
                )
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

                await self._publish_event(
                    "tool.awaiting_approval",
                    {
                        "request_id": request_id,
                        "tool": builder.name,
                        "params": raw_params,
                        "description": builder.approval_description(raw_params),
                        **({"call_id": call_id} if call_id else {}),
                    },
                    context=context,
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
                        context=context,
                        call_id=call_id,
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
                        context=context,
                        call_id=call_id,
                        audit_detail=result.content,
                    )
                    return result

            if not decision.allow:
                await self._publish_event(
                    "tool.error",
                    {
                        "tool": builder.name,
                        "error": decision.reason,
                        **({"call_id": call_id} if call_id else {}),
                    },
                    context=context,
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
            await self._publish_event(
                "tool.executing",
                self._event_data(builder.name, call_id),
                context=context,
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
                context=context,
                call_id=call_id,
                audit_detail="tool execution failed",
                error_label="tool execution failed",
            )
            return result

        await self._finalize_terminal_result(
            builder.name,
            result,
            context=context,
            call_id=call_id,
        )
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
        context: TenantContext | None = None,
        call_id: str | None = None,
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
            context=context,
            call_id=call_id,
            error_label=error_label,
        )

    async def _publish_result_event(
        self,
        tool_name: str,
        result: ToolExecutionResult,
        *,
        context: TenantContext | None = None,
        call_id: str | None = None,
        error_label: str | None = None,
    ) -> None:
        if result.status == ToolStatus.SUCCESS:
            await self._publish_event(
                "tool.completed",
                self._event_data(tool_name, call_id),
                context=context,
            )
            return

        resolved_error_label = error_label or (
            "tool returned error"
            if result.status == ToolStatus.ERROR
            else f"tool returned {result.status.value}"
        )
        await self._publish_event(
            "tool.error",
            {
                "tool": tool_name,
                "error": resolved_error_label,
                **({"call_id": call_id} if call_id else {}),
            },
            context=context,
        )

    @staticmethod
    def _event_data(tool_name: str, call_id: str | None) -> dict[str, Any]:
        data = {"tool": tool_name}
        if call_id:
            data["call_id"] = call_id
        return data

    async def _publish_event(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        context: TenantContext | None,
    ) -> None:
        await self.event_bus.publish(Event(type=event_type, data=data))
        if (
            self.event_router is not None
            and context is not None
            and context.session_id is not None
            and context.run_id is not None
        ):
            await self.event_router.publish(ScopedEvent.from_context(context, event_type, data))

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
