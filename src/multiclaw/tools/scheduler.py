import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from multiclaw.config import Settings
from multiclaw.events import Event, EventBus, EventRouter, ScopedEvent
from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.tenancy import TenantContext
from multiclaw.tools.base import (
    ToolBuilder,
    ToolExecutionResult,
    ToolProgressRecorder,
    ToolStatus,
)
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.continuation import WorkflowContinuationService
from multiclaw.workflow.models import (
    ApprovalStatus,
    CheckpointPhase,
    ExecutionRecoveryRecord,
    RecoveryAction,
    ExecutionStatus,
    InvalidTransitionError,
    LeaseConflictError,
    RecoveryStrategy,
    RunLease,
    RunLeaseHandle,
    RunStatus,
    StaleFenceError,
    VersionConflictError,
)

logger = logging.getLogger(__name__)
APPROVAL_TTL_MS = 120_000
MAX_CANONICAL_INPUT_BYTES = 262_144
SECRET_KEY_MARKERS = {"secret", "token", "password", "apikey", "authorization"}


class ExecutionConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _CanonicalToolInput:
    payload_json: str
    payload_hash: str


@dataclass(frozen=True, slots=True)
class _PreparedExecution:
    lease: RunLease
    execution_id: str
    approval_id: str | None
    progress_state: "_ExecutionProgressState"
    input_payload_json: str
    input_hash: str
    recovery_strategy: RecoveryStrategy
    idempotency_key: str | None


@dataclass(slots=True)
class _ExecutionProgressState:
    run_lease_handle: RunLeaseHandle
    execution_id: str
    execution_version: int
    external_request_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _ExecutionProgressRecorder(ToolProgressRecorder):
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        state: _ExecutionProgressState,
    ) -> None:
        self._database = database
        self._settings = settings
        self._state = state

    async def record_external_request_id(self, external_request_id: str) -> None:
        external_request_id = external_request_id.strip()
        if not external_request_id or len(external_request_id) > 255:
            raise ValueError("external_request_id must be 1-255 characters")

        async with self._state.lock:
            if self._state.external_request_id is not None:
                if self._state.external_request_id != external_request_id:
                    raise ValueError("external_request_id conflict")
                return

            async with self._database.write_transaction() as conn:
                workflow = WorkflowCoordinator(
                    self._database,
                    settings=self._settings,
                    connection=conn,
                )
                updated = await self._state.run_lease_handle.use_current(
                    lambda lease: workflow.record_external_request_id(
                        lease,
                        self._state.execution_id,
                        expected_status=ExecutionStatus.EXECUTING,
                        expected_version=self._state.execution_version,
                        external_request_id=external_request_id,
                    )
                )
            self._state.execution_version = updated.version
            self._state.external_request_id = external_request_id


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
        database: Database | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.permission_checker = permission_checker
        self.execution_guard = execution_guard
        self.audit_logger = audit_logger
        self.event_bus = event_bus
        self.event_router = event_router
        self.database = database
        self.settings = settings or Settings(_config_file="/nonexistent")

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
        run_lease_handle: RunLeaseHandle | None = None,
    ) -> ToolExecutionResult:
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
        if (
            self.database is None
            or context is None
            or context.session_id is None
            or context.run_id is None
            or run_lease_handle is None
        ):
            return await self._run_ephemeral(
                builder,
                raw_params,
                params=params,
                context=context,
                call_id=call_id,
            )

        try:
            canonical_input = self._canonicalize_input(params.model_dump(mode="json"))
            recovery_metadata = builder.recovery_metadata(params)
            decision = await self.permission_checker.check(
                builder.name,
                raw_params,
                workspace_root=getattr(builder, "workspace_root", None),
            )
            if decision.requires_approval:
                approval = await self._persist_approval_request(
                    builder,
                    canonical_input=canonical_input,
                    recovery_strategy=recovery_metadata.recovery_strategy,
                    idempotency_key=recovery_metadata.idempotency_key,
                    context=context,
                    call_id=call_id,
                    run_lease_handle=run_lease_handle,
                )
                await self._publish_event(
                    "tool.awaiting_approval",
                    {
                        "approval_id": approval.approval_id,
                        "version": approval.version,
                        "tool": builder.name,
                        "description": self._safe_approval_description(builder, raw_params),
                        **({"call_id": call_id} if call_id else {}),
                    },
                    context=context,
                )
                await self.audit_logger.record(
                    tool_name=builder.name,
                    status=ToolStatus.AWAITING_APPROVAL.value,
                    detail=f"approval required, approval_id={approval.approval_id}",
                )
                return ToolExecutionResult(
                    status=ToolStatus.AWAITING_APPROVAL,
                    content="approval required",
                    data={
                        "approval_id": approval.approval_id,
                        "version": approval.version,
                        "expires_at_ms": approval.expires_at,
                        "tool_call_id": call_id or approval.approval_id,
                    },
                )

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
            prepared = await self._prepare_execution(
                builder=builder,
                context=context,
                call_id=call_id,
                run_lease_handle=run_lease_handle,
                invocation=invocation,
                canonical_input=canonical_input,
                recovery_strategy=recovery_metadata.recovery_strategy,
                idempotency_key=recovery_metadata.idempotency_key,
            )
            await self._publish_event(
                "tool.executing",
                self._event_data(builder.name, call_id),
                context=context,
            )
            try:
                result = await self.execution_guard.run(invocation.execute)
            except Exception:
                result = ToolExecutionResult(
                    status=ToolStatus.ERROR,
                    content="tool execution failed",
                )
                terminal_status = (
                    ExecutionStatus.UNCERTAIN
                    if prepared.recovery_strategy is RecoveryStrategy.MANUAL_UNCERTAIN
                    else ExecutionStatus.FAILED_TERMINAL
                )
                await self._persist_execution_result(
                    prepared,
                    tool_name=builder.name,
                    context=context,
                    call_id=call_id,
                    result=result,
                    terminal_status=terminal_status,
                    run_lease_handle=run_lease_handle,
                )
                return result
        except (ExecutionConflictError, StaleFenceError, VersionConflictError, LeaseConflictError):
            raise
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

        await self._persist_execution_result(
            prepared,
            tool_name=builder.name,
            context=context,
            call_id=call_id,
            result=result,
        )
        return result

    async def _run_ephemeral(
        self,
        builder: ToolBuilder,
        raw_params: dict[str, Any],
        *,
        params,
        context: TenantContext | None,
        call_id: str | None,
    ) -> ToolExecutionResult:
        try:
            decision = await self.permission_checker.check(
                builder.name,
                raw_params,
                workspace_root=getattr(builder, "workspace_root", None),
            )
            if decision.requires_approval:
                approval_id = uuid.uuid4().hex
                await self.audit_logger.record(
                    tool_name=builder.name,
                    status=ToolStatus.AWAITING_APPROVAL.value,
                    detail=f"approval required, approval_id={approval_id}",
                )
                await self._publish_event(
                    "tool.awaiting_approval",
                    {
                        "approval_id": approval_id,
                        "tool": builder.name,
                        "description": self._safe_approval_description(builder, raw_params),
                        **({"call_id": call_id} if call_id else {}),
                    },
                    context=context,
                )
                return ToolExecutionResult(
                    status=ToolStatus.AWAITING_APPROVAL,
                    content="approval required",
                )
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
        except Exception:
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

    async def _persist_approval_request(
        self,
        builder: ToolBuilder,
        *,
        canonical_input: _CanonicalToolInput,
        recovery_strategy: RecoveryStrategy,
        idempotency_key: str | None,
        context: TenantContext,
        call_id: str | None,
        run_lease_handle: RunLeaseHandle,
    ):
        lease = await run_lease_handle.current()
        approval_id = str(uuid.uuid4())
        execution_id = str(uuid.uuid4())
        tool_call_id = call_id or approval_id
        async with self.database.write_transaction() as conn:
            workflow = WorkflowCoordinator(self.database, settings=self.settings, connection=conn)
            transitioned = await workflow.transition_run(lease, RunStatus.AWAITING_USER)
            approval = await workflow.create_approval(
                transitioned,
                approval_id=approval_id,
                tool_call_id=tool_call_id,
                expires_at=self.database.dialect.db_now_ms() + APPROVAL_TTL_MS,
            )
            created = await workflow.create_execution(
                transitioned,
                execution_id=execution_id,
                approval_id=approval.approval_id,
                tool_call_id=tool_call_id,
                tool_name=builder.name,
                tool_kind=getattr(builder, "tool_kind", "native"),
                recovery_strategy=recovery_strategy,
                idempotency_key=idempotency_key,
                input_payload_json=canonical_input.payload_json,
                input_hash=canonical_input.payload_hash,
                status=ExecutionStatus.NOT_STARTED,
            )
            if created is None:
                raise ExecutionConflictError("another execution is already active for this run")
            approval_cursor = self._approval_cursor(context.run_id, call_id or approval.approval_id)
            await workflow.checkpoint(
                transitioned,
                CheckpointPhase.AWAITING_APPROVAL,
                {
                    "run_id": context.run_id,
                    "approval_id": approval.approval_id,
                    "tool_call_id": tool_call_id,
                    "approval_expires_at_ms": approval.expires_at,
                    "resume_cursor": approval_cursor,
                    "cursor": approval_cursor,
                },
                approval_id=approval.approval_id,
            )
        await run_lease_handle.replace(transitioned)
        return approval

    async def _prepare_execution(
        self,
        *,
        builder: ToolBuilder,
        context: TenantContext,
        call_id: str | None,
        run_lease_handle: RunLeaseHandle,
        invocation,
        canonical_input: _CanonicalToolInput,
        recovery_strategy: RecoveryStrategy,
        idempotency_key: str | None,
    ) -> _PreparedExecution:
        lease = await run_lease_handle.current()
        execution_id = str(uuid.uuid4())
        tool_call_id = call_id or execution_id
        async with self.database.write_transaction() as conn:
            workflow = WorkflowCoordinator(self.database, settings=self.settings, connection=conn)
            created = await workflow.create_execution(
                lease,
                execution_id=execution_id,
                approval_id=None,
                tool_call_id=tool_call_id,
                tool_name=builder.name,
                tool_kind=getattr(builder, "tool_kind", "native"),
                recovery_strategy=recovery_strategy,
                idempotency_key=idempotency_key,
                input_payload_json=canonical_input.payload_json,
                input_hash=canonical_input.payload_hash,
            )
            if created is None:
                raise ExecutionConflictError("another execution is already active for this run")
            await workflow.transition_execution(
                lease,
                execution_id,
                expected_status=ExecutionStatus.NOT_STARTED,
                expected_version=created.version,
                target=ExecutionStatus.EXECUTING,
            )
            dispatch_cursor = self._dispatch_cursor(context.run_id, tool_call_id)
            await workflow.checkpoint(
                lease,
                CheckpointPhase.EXECUTION_DISPATCHING,
                {
                    "run_id": context.run_id,
                    "execution_id": execution_id,
                    "tool_call_id": tool_call_id,
                    "recovery_strategy": recovery_strategy.value,
                    "input_hash": canonical_input.payload_hash,
                    "input_ref": f"tool_execution:{execution_id}:input_payload_json",
                    "idempotency_key": idempotency_key,
                    "dispatch_cursor": dispatch_cursor,
                    "cursor": dispatch_cursor,
                },
                execution_id=execution_id,
                execution_expected_status=ExecutionStatus.EXECUTING,
                execution_expected_version=created.version + 1,
            )
        progress_state = _ExecutionProgressState(
            run_lease_handle=run_lease_handle,
            execution_id=execution_id,
            execution_version=created.version + 1,
        )
        invocation.configure_progress(
            _ExecutionProgressRecorder(
                database=self.database,
                settings=self.settings,
                state=progress_state,
            )
        )
        return _PreparedExecution(
            lease=lease,
            execution_id=execution_id,
            approval_id=None,
            progress_state=progress_state,
            input_payload_json=canonical_input.payload_json,
            input_hash=canonical_input.payload_hash,
            recovery_strategy=recovery_strategy,
            idempotency_key=idempotency_key,
        )

    async def _persist_execution_result(
        self,
        prepared: _PreparedExecution,
        *,
        tool_name: str,
        context: TenantContext,
        call_id: str | None,
        result: ToolExecutionResult,
        run_lease_handle: RunLeaseHandle | None = None,
        terminal_status: ExecutionStatus | None = None,
    ) -> None:
        resolved_status = terminal_status or self._terminal_execution_status(result)
        recorded_external_request_id = prepared.progress_state.external_request_id
        if (
            recorded_external_request_id is not None
            and result.external_request_id is not None
            and recorded_external_request_id != result.external_request_id
        ):
            raise ValueError("external_request_id conflict")
        external_request_id = result.external_request_id or recorded_external_request_id
        async with self.database.write_transaction() as conn:
            workflow = WorkflowCoordinator(self.database, settings=self.settings, connection=conn)
            continuation = WorkflowContinuationService(self.database, settings=self.settings)
            persisted_result = await continuation.persist_tool_result(
                repository=MemoryRepository(conn, context, self.database.dialect),
                context=context,
                content=result.content,
                tool_call_id=call_id or prepared.execution_id,
                tool_name=tool_name,
                execution_id=prepared.execution_id,
                result_status=resolved_status.value,
            )
            updated = await workflow.complete_execution(
                prepared.lease,
                prepared.execution_id,
                expected_status=ExecutionStatus.EXECUTING,
                expected_version=prepared.progress_state.execution_version,
                target_status=resolved_status,
                external_request_id=external_request_id,
                result_ref=persisted_result.result_ref,
                result_digest=persisted_result.result_digest,
            )
            prepared.progress_state.execution_version = updated.version
            prepared.progress_state.external_request_id = updated.external_request_id
            resume_cursor = self._result_cursor(context.run_id, call_id or prepared.execution_id)
            await workflow.checkpoint(
                prepared.lease,
                CheckpointPhase.EXECUTION_RESULT_OBSERVED,
                {
                    "run_id": context.run_id,
                    "execution_id": prepared.execution_id,
                    "result_status": resolved_status.value,
                    "result_digest": persisted_result.result_digest,
                    "result_ref": persisted_result.result_ref,
                    "external_request_id": updated.external_request_id,
                    "resume_cursor": resume_cursor,
                    "cursor": resume_cursor,
                },
                approval_id=prepared.approval_id,
                execution_id=prepared.execution_id,
                execution_expected_status=resolved_status,
                execution_expected_version=updated.version,
            )
        if run_lease_handle is not None:
            await run_lease_handle.replace(prepared.lease)
        await self._finalize_terminal_result(
            tool_name,
            result,
            context=context,
            call_id=call_id,
            audit_detail="tool execution failed" if result.status is ToolStatus.ERROR else None,
            error_label="tool execution failed" if result.status is ToolStatus.ERROR else None,
        )

    async def recover_execution(
        self,
        *,
        builder: ToolBuilder,
        context: TenantContext,
        execution: ExecutionRecoveryRecord,
        action: RecoveryAction,
        run_lease_handle: RunLeaseHandle,
        force_execute: bool = False,
    ) -> ToolExecutionResult:
        try:
            params_data = json.loads(execution.input_payload_json)
            if not isinstance(params_data, dict):
                raise ValueError("persisted input payload must decode to an object")
            canonical = self._canonicalize_input(params_data)
            if canonical.payload_hash != execution.input_hash:
                return await self._block_execution(
                    context=context,
                    execution=execution,
                    run_lease_handle=run_lease_handle,
                    status=ExecutionStatus.BLOCKED_CORRUPT,
                    detail="input hash mismatch",
                )
            if builder.name != execution.tool_name:
                return await self._block_execution(
                    context=context,
                    execution=execution,
                    run_lease_handle=run_lease_handle,
                    status=ExecutionStatus.BLOCKED_INCOMPATIBLE,
                    detail="tool name mismatch",
                )
            if getattr(builder, "tool_kind", "native") != execution.tool_kind:
                return await self._block_execution(
                    context=context,
                    execution=execution,
                    run_lease_handle=run_lease_handle,
                    status=ExecutionStatus.BLOCKED_INCOMPATIBLE,
                    detail="tool kind mismatch",
                )
            params = builder.validate(params_data)
            metadata = builder.recovery_metadata(params)
            if metadata.recovery_strategy is not execution.recovery_strategy:
                return await self._block_execution(
                    context=context,
                    execution=execution,
                    run_lease_handle=run_lease_handle,
                    status=ExecutionStatus.BLOCKED_INCOMPATIBLE,
                    detail="recovery strategy mismatch",
                )
            if metadata.idempotency_key != execution.idempotency_key:
                return await self._block_execution(
                    context=context,
                    execution=execution,
                    run_lease_handle=run_lease_handle,
                    status=ExecutionStatus.BLOCKED_CORRUPT,
                    detail="idempotency key mismatch",
                )
            decision = await self.permission_checker.check(
                builder.name,
                params_data,
                workspace_root=getattr(builder, "workspace_root", None),
            )
            if not decision.allow:
                return await self._block_execution(
                    context=context,
                    execution=execution,
                    run_lease_handle=run_lease_handle,
                    status=ExecutionStatus.BLOCKED_INCOMPATIBLE,
                    detail=decision.reason,
                )
            if decision.requires_approval:
                if execution.approval_id is None:
                    return await self._block_execution(
                        context=context,
                        execution=execution,
                        run_lease_handle=run_lease_handle,
                        status=ExecutionStatus.BLOCKED_INCOMPATIBLE,
                        detail="approval required under current policy",
                    )
                approval = await WorkflowCoordinator(
                    self.database,
                    settings=self.settings,
                ).get_approval(context, execution.approval_id)
                if approval is None or approval.status is not ApprovalStatus.APPROVED:
                    return await self._block_execution(
                        context=context,
                        execution=execution,
                        run_lease_handle=run_lease_handle,
                        status=ExecutionStatus.BLOCKED_INCOMPATIBLE,
                        detail="approval not currently approved",
                    )

            if action is RecoveryAction.MARK_MANUAL_UNCERTAIN and not force_execute:
                return await self._mark_uncertain_execution(
                    context=context,
                    execution=execution,
                    run_lease_handle=run_lease_handle,
                )

            invocation = builder.build(params)
            progress_state = _ExecutionProgressState(
                run_lease_handle=run_lease_handle,
                execution_id=execution.execution_id,
                execution_version=execution.version,
                external_request_id=execution.external_request_id,
            )
            invocation.configure_progress(
                _ExecutionProgressRecorder(
                    database=self.database,
                    settings=self.settings,
                    state=progress_state,
                )
            )
            invocation.configure_permission(decision.approved_roots)
            prepared = await self._prepare_recovery_execution(
                context=context,
                execution=execution,
                canonical_input=canonical,
                progress_state=progress_state,
                run_lease_handle=run_lease_handle,
            )
            result = await self.execution_guard.run(invocation.execute)
            await self._persist_execution_result(
                prepared,
                tool_name=builder.name,
                context=context,
                call_id=execution.tool_call_id,
                result=result,
                run_lease_handle=run_lease_handle,
            )
            return result
        except (ExecutionConflictError, asyncio.CancelledError, StaleFenceError, VersionConflictError, LeaseConflictError):
            raise
        except Exception:
            result = ToolExecutionResult(status=ToolStatus.ERROR, content="tool execution failed")
            terminal_status = (
                ExecutionStatus.UNCERTAIN
                if execution.recovery_strategy is RecoveryStrategy.MANUAL_UNCERTAIN
                else ExecutionStatus.FAILED_TERMINAL
            )
            prepared = _PreparedExecution(
                lease=await run_lease_handle.current(),
                execution_id=execution.execution_id,
                approval_id=execution.approval_id,
                progress_state=_ExecutionProgressState(
                    run_lease_handle=run_lease_handle,
                    execution_id=execution.execution_id,
                    execution_version=execution.version,
                    external_request_id=execution.external_request_id,
                ),
                input_payload_json=execution.input_payload_json,
                input_hash=execution.input_hash,
                recovery_strategy=execution.recovery_strategy,
                idempotency_key=execution.idempotency_key,
            )
            await self._persist_execution_result(
                prepared,
                tool_name=builder.name,
                context=context,
                call_id=execution.tool_call_id,
                result=result,
                run_lease_handle=run_lease_handle,
                terminal_status=terminal_status,
            )
            return result

    async def _prepare_recovery_execution(
        self,
        *,
        context: TenantContext,
        execution: ExecutionRecoveryRecord,
        canonical_input: _CanonicalToolInput,
        progress_state: _ExecutionProgressState,
        run_lease_handle: RunLeaseHandle,
    ) -> _PreparedExecution:
        current_execution = execution
        async with self.database.write_transaction() as conn:
            workflow = WorkflowCoordinator(self.database, settings=self.settings, connection=conn)
            if execution.status is ExecutionStatus.NOT_STARTED:
                current_execution = await workflow.get_execution_recovery(context, execution.execution_id)
                assert current_execution is not None
                if execution.recovery_strategy is RecoveryStrategy.READ_ONLY_REPLAY:
                    await workflow.transition_execution(
                        await run_lease_handle.current(),
                        execution.execution_id,
                        expected_status=ExecutionStatus.NOT_STARTED,
                        expected_version=current_execution.version,
                        target=ExecutionStatus.REPLAYING,
                    )
                    replaying = await workflow.get_execution_recovery(context, execution.execution_id)
                    assert replaying is not None
                    dispatch_version = replaying.version
                    expected_status = ExecutionStatus.REPLAYING
                    await workflow.checkpoint(
                        await run_lease_handle.current(),
                        CheckpointPhase.EXECUTION_DISPATCHING,
                        {
                            "run_id": context.run_id,
                            "execution_id": execution.execution_id,
                            "tool_call_id": execution.tool_call_id,
                            "recovery_strategy": execution.recovery_strategy.value,
                            "input_hash": canonical_input.payload_hash,
                            "input_ref": execution.input_ref,
                            "idempotency_key": execution.idempotency_key,
                            "dispatch_cursor": self._dispatch_cursor(context.run_id, execution.tool_call_id),
                            "cursor": self._dispatch_cursor(context.run_id, execution.tool_call_id),
                        },
                        approval_id=execution.approval_id,
                        execution_id=execution.execution_id,
                        execution_expected_status=expected_status,
                        execution_expected_version=dispatch_version,
                    )
                    await workflow.transition_execution(
                        await run_lease_handle.current(),
                        execution.execution_id,
                        expected_status=ExecutionStatus.REPLAYING,
                        expected_version=dispatch_version,
                        target=ExecutionStatus.EXECUTING,
                    )
                else:
                    await workflow.transition_execution(
                        await run_lease_handle.current(),
                        execution.execution_id,
                        expected_status=ExecutionStatus.NOT_STARTED,
                        expected_version=current_execution.version,
                        target=ExecutionStatus.EXECUTING,
                    )
                    dispatch_version = current_execution.version + 1
                    await workflow.checkpoint(
                        await run_lease_handle.current(),
                        CheckpointPhase.EXECUTION_DISPATCHING,
                        {
                            "run_id": context.run_id,
                            "execution_id": execution.execution_id,
                            "tool_call_id": execution.tool_call_id,
                            "recovery_strategy": execution.recovery_strategy.value,
                            "input_hash": canonical_input.payload_hash,
                            "input_ref": execution.input_ref,
                            "idempotency_key": execution.idempotency_key,
                            "dispatch_cursor": self._dispatch_cursor(context.run_id, execution.tool_call_id),
                            "cursor": self._dispatch_cursor(context.run_id, execution.tool_call_id),
                        },
                        approval_id=execution.approval_id,
                        execution_id=execution.execution_id,
                        execution_expected_status=ExecutionStatus.EXECUTING,
                        execution_expected_version=dispatch_version,
                    )
                refreshed = await workflow.get_execution_recovery(context, execution.execution_id)
                assert refreshed is not None
                current_execution = refreshed

        progress_state.execution_version = current_execution.version
        progress_state.external_request_id = current_execution.external_request_id
        return _PreparedExecution(
            lease=await run_lease_handle.current(),
            execution_id=current_execution.execution_id,
            approval_id=current_execution.approval_id,
            progress_state=progress_state,
            input_payload_json=current_execution.input_payload_json,
            input_hash=current_execution.input_hash,
            recovery_strategy=current_execution.recovery_strategy,
            idempotency_key=current_execution.idempotency_key,
        )

    async def _mark_uncertain_execution(
        self,
        *,
        context: TenantContext,
        execution: ExecutionRecoveryRecord,
        run_lease_handle: RunLeaseHandle,
    ) -> ToolExecutionResult:
        prepared = _PreparedExecution(
            lease=await run_lease_handle.current(),
            execution_id=execution.execution_id,
            approval_id=execution.approval_id,
            progress_state=_ExecutionProgressState(
                run_lease_handle=run_lease_handle,
                execution_id=execution.execution_id,
                execution_version=execution.version,
                external_request_id=execution.external_request_id,
            ),
            input_payload_json=execution.input_payload_json,
            input_hash=execution.input_hash,
            recovery_strategy=execution.recovery_strategy,
            idempotency_key=execution.idempotency_key,
        )
        result = ToolExecutionResult(
            status=ToolStatus.ERROR,
            content="tool execution failed",
            external_request_id=execution.external_request_id,
            result_ref=execution.result_ref or f"tool_execution:{execution.execution_id}:uncertain",
            result_digest=execution.result_digest
            or hashlib.sha256(b"uncertain").hexdigest(),
        )
        await self._persist_execution_result(
            prepared,
            tool_name=execution.tool_name,
            context=context,
            call_id=execution.tool_call_id,
            result=result,
            run_lease_handle=run_lease_handle,
            terminal_status=ExecutionStatus.UNCERTAIN,
        )
        return result

    async def _block_execution(
        self,
        *,
        context: TenantContext,
        execution: ExecutionRecoveryRecord,
        run_lease_handle: RunLeaseHandle,
        status: ExecutionStatus,
        detail: str,
    ) -> ToolExecutionResult:
        result = ToolExecutionResult(
            status=ToolStatus.ERROR,
            content=detail,
            external_request_id=execution.external_request_id,
        )
        lease = await run_lease_handle.current()
        async with self.database.write_transaction() as conn:
            workflow = WorkflowCoordinator(self.database, settings=self.settings, connection=conn)
            continuation = WorkflowContinuationService(self.database, settings=self.settings)
            persisted_result = await continuation.persist_tool_result(
                repository=MemoryRepository(conn, context, self.database.dialect),
                context=context,
                content=detail,
                tool_call_id=execution.tool_call_id,
                tool_name=execution.tool_name,
                execution_id=execution.execution_id,
                result_status=status.value,
            )
            updated = await workflow.complete_execution(
                lease,
                execution.execution_id,
                expected_status=execution.status,
                expected_version=execution.version,
                target_status=status,
                external_request_id=execution.external_request_id,
                result_ref=persisted_result.result_ref,
                result_digest=persisted_result.result_digest,
            )
            await workflow.checkpoint(
                lease,
                CheckpointPhase.EXECUTION_RESULT_OBSERVED,
                {
                    "run_id": context.run_id,
                    "execution_id": execution.execution_id,
                    "result_status": status.value,
                    "result_digest": persisted_result.result_digest,
                    "result_ref": persisted_result.result_ref,
                    "external_request_id": updated.external_request_id,
                    "resume_cursor": self._result_cursor(context.run_id, execution.tool_call_id),
                    "cursor": self._result_cursor(context.run_id, execution.tool_call_id),
                },
                approval_id=execution.approval_id,
                execution_id=execution.execution_id,
                execution_expected_status=status,
                execution_expected_version=updated.version,
            )
        await self._finalize_terminal_result(
            execution.tool_name,
            result,
            context=context,
            call_id=execution.tool_call_id,
            audit_detail=detail,
            error_label=detail,
        )
        return result

    @staticmethod
    def _approval_cursor(run_id: str | None, tool_call_id: str) -> str:
        return f"approval:{run_id or 'no-run'}:{tool_call_id}"

    @staticmethod
    def _dispatch_cursor(run_id: str | None, tool_call_id: str) -> str:
        return f"dispatch:{run_id or 'no-run'}:{tool_call_id}"

    @staticmethod
    def _result_cursor(run_id: str | None, tool_call_id: str) -> str:
        return f"result:{run_id or 'no-run'}:{tool_call_id}"

    @staticmethod
    def _terminal_execution_status(result: ToolExecutionResult) -> ExecutionStatus:
        if result.status is ToolStatus.SUCCESS:
            return ExecutionStatus.SUCCEEDED
        if result.status is ToolStatus.ERROR:
            return ExecutionStatus.FAILED_TERMINAL
        return ExecutionStatus.FAILED_TERMINAL

    def _safe_approval_description(
        self,
        builder: ToolBuilder,
        raw_params: dict[str, Any],
    ) -> str:
        return builder.approval_description(self._redact_secret_values(raw_params))

    def _canonicalize_input(self, value: dict[str, Any]) -> _CanonicalToolInput:
        self._reject_secret_fields(value)
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_CANONICAL_INPUT_BYTES:
            raise ValueError("tool input exceeds canonical payload size limit")
        return _CanonicalToolInput(
            payload_json=encoded.decode("utf-8"),
            payload_hash=hashlib.sha256(encoded).hexdigest(),
        )

    def _reject_secret_fields(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
                if any(marker in normalized for marker in SECRET_KEY_MARKERS):
                    raise ValueError(f"tool input contains forbidden secret field {key!r}")
                self._reject_secret_fields(item)
            return
        if isinstance(value, list):
            for item in value:
                self._reject_secret_fields(item)
            return
        if isinstance(value, tuple):
            for item in value:
                self._reject_secret_fields(item)

    def _redact_secret_values(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
                if any(marker in normalized for marker in SECRET_KEY_MARKERS):
                    redacted[key] = "[REDACTED]"
                else:
                    redacted[key] = self._redact_secret_values(item)
            return redacted
        if isinstance(value, list):
            return [self._redact_secret_values(item) for item in value]
        if isinstance(value, tuple):
            return [self._redact_secret_values(item) for item in value]
        return value

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
