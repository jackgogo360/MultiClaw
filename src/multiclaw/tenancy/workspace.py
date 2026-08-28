from __future__ import annotations

import re
from pathlib import Path

from .context import TenantContext


class InvalidWorkspaceScope(ValueError):
    pass


class WorkspaceContainmentError(ValueError):
    pass


class WorkspaceResolver:
    _SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

    def __init__(self, root: str | Path) -> None:
        resolved_root = Path(root).resolve(strict=True)
        if not resolved_root.is_dir():
            raise NotADirectoryError(f"workspace root must be a directory: {resolved_root}")
        self.root = resolved_root

    def resolve(self, context: TenantContext, *, create: bool = False) -> Path:
        tenant_id = self._validate_segment(context.tenant_id)
        workspace_id = self._validate_segment(context.workspace_id)
        tenant_candidate = self.root / tenant_id
        if create:
            tenant_candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        tenant_root = tenant_candidate.resolve(strict=create)
        self._ensure_contained(tenant_root)

        candidate = tenant_root / workspace_id
        if create:
            candidate.mkdir(exist_ok=True, mode=0o700)
        resolved = candidate.resolve(strict=create)
        self._ensure_contained(resolved)
        return resolved

    def _validate_segment(self, value: str) -> str:
        if not self._SEGMENT.fullmatch(value):
            raise InvalidWorkspaceScope(value)
        return value

    def _ensure_contained(self, path: Path) -> None:
        if not path.is_relative_to(self.root):
            raise WorkspaceContainmentError(str(path))
