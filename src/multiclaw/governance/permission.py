from collections.abc import Iterable
from pathlib import Path

from multiclaw.governance.models import PermissionDecision


class PermissionChecker:
    def __init__(self, guarded_tools: Iterable[str] | None = None):
        self._guarded_tools = {
            self._canonicalize_tool_name(tool_name) for tool_name in (guarded_tools or ())
        }

    async def check(
        self,
        tool_name: str,
        raw_params: dict | None = None,
        workspace_root: str | Path | None = None,
    ) -> PermissionDecision:
        approved_roots = self._collect_external_roots(raw_params or {}, workspace_root)
        if approved_roots:
            return PermissionDecision(
                allow=True,
                requires_approval=True,
                reason="external_path_requires_approval",
                approved_roots=approved_roots,
            )

        if self._canonicalize_tool_name(tool_name) in self._guarded_tools:
            return PermissionDecision(
                allow=True,
                requires_approval=True,
                reason="approval_required",
            )

        return PermissionDecision(
            allow=True,
            requires_approval=False,
            reason="allowed",
        )

    @staticmethod
    def _canonicalize_tool_name(tool_name: str) -> str:
        return tool_name.strip().casefold()

    @staticmethod
    def _collect_external_roots(
        raw_params: dict,
        workspace_root: str | Path | None,
    ) -> list[str]:
        if workspace_root is None:
            return []

        workspace = Path(workspace_root).resolve()
        roots: list[str] = []
        for key in ("file_path", "dir_path", "path"):
            value = raw_params.get(key)
            if not isinstance(value, str) or not value:
                continue
            resolved = Path(value).resolve() if Path(value).is_absolute() else (workspace / value).resolve()
            try:
                resolved.relative_to(workspace)
            except ValueError:
                roots.append(str(resolved))
        return roots
