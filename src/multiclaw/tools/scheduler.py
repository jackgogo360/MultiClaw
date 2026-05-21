import asyncio
import uuid
from typing import Any

from multiclaw.events import Event, EventBus
from multiclaw.governance import InMemoryAuditLogger, PermissionChecker, ProcessSandbox
from multiclaw.tools.base import ToolBuilder, ToolExecutionResult, ToolStatus


class CoreToolScheduler:
    def __init__(
        self,
        permission_checker: PermissionChecker,
        sandbox: ProcessSandbox,
        audit_logger: InMemoryAuditLogger,
        event_bus: EventBus,
    ) -> None:
        self.permission_checker = permission_checker
        self.sandbox = sandbox
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

            decision = await self.permission_checker.check(
                builder.name,
                raw_params,
                workspace_root=getattr(builder, "workspace_root", None),
            )
            if decision.requires_approval:
                request_id = uuid.uuid4().hex[:12]
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

                await event.wait()
                approved = self._pending_results.pop(request_id, False)
                self._pending.pop(request_id, None)

                if not approved:
                    await self.audit_logger.record(
                        tool_name=builder.name,
                        status=ToolStatus.CANCELLED.value,
                        detail="rejected by user",
                    )
                    return ToolExecutionResult(
                        status=ToolStatus.CANCELLED,
                        content="rejected by user",
                    )

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
            result = await self.sandbox.run(invocation.execute)
        except Exception as exc:
            error_text = str(exc)
            await self.audit_logger.record(
                tool_name=builder.name,
                status=ToolStatus.ERROR.value,
                detail=error_text,
            )
            await self.event_bus.publish(
                Event(type="tool.error", data={"tool": builder.name, "error": error_text})
            )
            return ToolExecutionResult(status=ToolStatus.ERROR, content=error_text)

        await self.audit_logger.record(
            tool_name=builder.name,
            status=ToolStatus.SUCCESS.value,
            detail=result.content,
        )
        await self.event_bus.publish(
            Event(type="tool.completed", data={"tool": builder.name})
        )
        return result
