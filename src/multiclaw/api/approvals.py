from __future__ import annotations

from pydantic import BaseModel

from multiclaw.workflow.models import ApprovalRecord


class ApprovalDecisionRequest(BaseModel):
    approval_id: str
    approved: bool
    version: int


class ApprovalResponse(BaseModel):
    approval_id: str
    status: str
    version: int
    expires_at: int
    resolved_at: int | None = None

    @classmethod
    def from_record(cls, record: ApprovalRecord) -> "ApprovalResponse":
        return cls(
            approval_id=record.approval_id,
            status=record.status.value,
            version=record.version,
            expires_at=record.expires_at,
            resolved_at=record.resolved_at,
        )
