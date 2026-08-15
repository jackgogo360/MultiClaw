from dataclasses import dataclass, replace


def _require_non_empty(name: str, value: str | None) -> str:
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    workspace_id: str
    session_id: str | None = None
    run_id: str | None = None
    request_started_at_ms: int = 0

    def __post_init__(self) -> None:
        _require_non_empty("tenant_id", self.tenant_id)
        _require_non_empty("workspace_id", self.workspace_id)
        if self.run_id and not self.session_id:
            raise ValueError("session_id is required when run_id is set")

    def for_session(self, session_id: str) -> "TenantContext":
        return replace(self, session_id=_require_non_empty("session_id", session_id), run_id=None)

    def for_run(self, session_id: str, run_id: str) -> "TenantContext":
        return replace(
            self,
            session_id=_require_non_empty("session_id", session_id),
            run_id=_require_non_empty("run_id", run_id),
        )
