from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from multiclaw.memory import MemoryEntry
from multiclaw.config import Settings
from multiclaw.storage.engine import Database
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import CheckpointPhase, RunLeaseHandle


@dataclass(frozen=True, slots=True)
class PersistedAssistantOutput:
    message_id: str
    output_digest: str
    model_cursor: str


@dataclass(frozen=True, slots=True)
class PersistedToolResult:
    entry_id: str
    result_ref: str
    result_digest: str
    content: str
    tool_call_id: str
    tool_name: str


class ContinuationState(StrEnum):
    COMPLETED = "completed"
    AWAITING_USER = "awaiting_user"
    FAILED_TERMINAL = "failed_terminal"


@dataclass(frozen=True, slots=True)
class ContinuationOutcome:
    state: ContinuationState
    assistant_content: str | None = None
    detail: str = ""


class WorkflowContinuationService:
    def __init__(self, database: Database, *, settings: Settings | None = None) -> None:
        self._database = database
        self._settings = settings or Settings(_config_file="/nonexistent")

    async def persist_assistant_output(
        self,
        *,
        context: TenantContext,
        run_lease_handle: RunLeaseHandle,
        content: str,
        turn_index: int,
    ) -> PersistedAssistantOutput:
        if context.session_id is None:
            raise ValueError("session_id is required for workflow continuation output persistence")

        output_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        async with TenantUnitOfWork(
            self._database,
            context,
            workflow_settings=self._settings.workflow,
        ) as uow:
            entry = await uow.memory.save(
                MemoryEntry(
                    content=content,
                    type="chat_message",
                    role="assistant",
                    session_id=context.session_id,
                    turn_index=turn_index,
                )
            )
            model_cursor = _model_cursor(context.run_id, turn_index, entry.id)
            workflow = WorkflowCoordinator(
                self._database,
                settings=self._settings,
                connection=uow.conn,
            )
            await run_lease_handle.use_current(
                lambda lease: workflow.checkpoint(
                    lease,
                    CheckpointPhase.MODEL_OUTPUT_COMMITTED,
                    {
                        "run_id": context.run_id,
                        "message_id": entry.id,
                        "output_digest": output_digest,
                        "model_cursor": model_cursor,
                        "cursor": model_cursor,
                    },
                )
            )
            await uow.commit()
        return PersistedAssistantOutput(
            message_id=entry.id,
            output_digest=output_digest,
            model_cursor=model_cursor,
        )

    async def persist_tool_result(
        self,
        *,
        repository: MemoryRepository,
        context: TenantContext,
        content: str,
        tool_call_id: str,
        tool_name: str,
        execution_id: str,
        result_status: str,
    ) -> PersistedToolResult:
        if context.session_id is None:
            raise ValueError("session_id is required for workflow tool result persistence")
        entry = await repository.save(
            MemoryEntry(
                content=content,
                type="tool_result",
                role="tool",
                session_id=context.session_id,
                metadata={
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "execution_id": execution_id,
                    "result_status": result_status,
                },
            )
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return PersistedToolResult(
            entry_id=entry.id,
            result_ref=f"memory://{entry.id}",
            result_digest=digest,
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )

    async def load_tool_result(
        self,
        *,
        context: TenantContext,
        result_ref: str,
        expected_digest: str,
        expected_execution_id: str | None = None,
        expected_tool_call_id: str | None = None,
        expected_tool_name: str | None = None,
    ) -> PersistedToolResult:
        match = re.fullmatch(r"memory://([A-Za-z0-9-]{1,64})", result_ref)
        if match is None:
            raise ValueError("unsupported result_ref")
        entry_id = match.group(1)
        async with TenantUnitOfWork(
            self._database,
            context,
            workflow_settings=self._settings.workflow,
        ) as uow:
            entry = await uow.memory.get(entry_id, context.session_id)
        if entry is None:
            raise ValueError("tool result entry not found")
        if entry.type != "tool_result" or entry.role != "tool":
            raise ValueError("tool result entry has invalid type or role")
        tool_call_id = str(entry.metadata.get("tool_call_id", "")).strip()
        tool_name = str(entry.metadata.get("tool_name", "")).strip()
        execution_id = str(entry.metadata.get("execution_id", "")).strip()
        result_status = str(entry.metadata.get("result_status", "")).strip()
        if not tool_call_id or not tool_name or not execution_id or not result_status:
            raise ValueError("tool result metadata is incomplete")
        if expected_execution_id is not None and execution_id != expected_execution_id:
            raise ValueError("tool result execution_id mismatch")
        if expected_tool_call_id is not None and tool_call_id != expected_tool_call_id:
            raise ValueError("tool result tool_call_id mismatch")
        if expected_tool_name is not None and tool_name != expected_tool_name:
            raise ValueError("tool result tool_name mismatch")
        digest = hashlib.sha256(entry.content.encode("utf-8")).hexdigest()
        if digest != expected_digest:
            raise ValueError("tool result digest mismatch")
        return PersistedToolResult(
            entry_id=entry.id,
            result_ref=result_ref,
            result_digest=digest,
            content=entry.content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )


def _model_cursor(run_id: str | None, turn_index: int, message_id: str) -> str:
    scoped_run_id = run_id or "no-run"
    return f"assistant:{scoped_run_id}:{turn_index}:{message_id}"
