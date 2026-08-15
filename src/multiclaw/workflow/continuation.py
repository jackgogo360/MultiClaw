from __future__ import annotations

import hashlib
from dataclasses import dataclass

from multiclaw.config import Settings
from multiclaw.memory import MemoryEntry
from multiclaw.storage.engine import Database
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy import TenantContext
from multiclaw.workflow.coordinator import WorkflowCoordinator
from multiclaw.workflow.models import CheckpointPhase, RunLeaseHandle


@dataclass(frozen=True, slots=True)
class PersistedAssistantOutput:
    message_id: str
    output_digest: str
    model_cursor: str


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


def _model_cursor(run_id: str | None, turn_index: int, message_id: str) -> str:
    scoped_run_id = run_id or "no-run"
    return f"assistant:{scoped_run_id}:{turn_index}:{message_id}"
