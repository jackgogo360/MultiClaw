from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from multiclaw.tools._common import (
    MAX_LIST_DIR_ENTRIES,
    MAX_LIST_DIR_DEPTH,
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _human_size,
    _policy_for_invocation,
    _resolve_path,
    _success,
)
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation


class ListDirParams(BaseModel):
    dir_path: str = "."
    recursive: bool = False


class ListDirInvocation(ToolInvocation[ListDirParams]):
    def __init__(self, name: str, params: ListDirParams, workspace_root: Path, policy: PathPolicy) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.policy = policy

    async def execute(self) -> ToolExecutionResult:
        target = _resolve_path(self.params.dir_path, self.workspace_root)
        policy = _policy_for_invocation(self.policy, self)

        error = policy.validate_path(target)
        if error:
            return _error(error)
        if not target.exists():
            return _error(f"Directory not found: {target}")
        if not target.is_dir():
            return _error(f"Not a directory: {target}")

        entries, truncated = self._list_recursive(target) if self.params.recursive else self._list_flat(target)
        return self._format_output(entries, target, truncated)

    def _list_flat(self, target: Path) -> tuple[list[dict], bool]:
        entries: list[dict] = []
        for item in sorted(target.iterdir(), key=self._sort_key):
            if item.name.startswith("."):
                continue
            entries.append(self._make_entry(item, target))
            if len(entries) >= MAX_LIST_DIR_ENTRIES:
                return entries, True
        return entries, False

    def _list_recursive(self, target: Path) -> tuple[list[dict], bool]:
        entries: list[dict] = []

        def walk(current: Path, depth: int) -> None:
            if depth > MAX_LIST_DIR_DEPTH or len(entries) >= MAX_LIST_DIR_ENTRIES:
                return
            for item in sorted(current.iterdir(), key=self._sort_key):
                if item.name.startswith("."):
                    continue
                entries.append(self._make_entry(item, target))
                if len(entries) >= MAX_LIST_DIR_ENTRIES:
                    return
                if item.is_dir():
                    walk(item, depth + 1)

        walk(target, 0)
        truncated = len(entries) >= MAX_LIST_DIR_ENTRIES
        return entries[:MAX_LIST_DIR_ENTRIES], truncated

    def _make_entry(self, item: Path, root: Path) -> dict:
        try:
            stat = item.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            size = stat.st_size if item.is_file() else 0
        except OSError:
            mtime = datetime.fromtimestamp(0, tz=timezone.utc)
            size = 0
        return {
            "path": str(item.relative_to(root)),
            "is_directory": item.is_dir(),
            "size": size,
            "modified": mtime.strftime("%Y-%m-%d %H:%M"),
        }

    def _sort_key(self, item: Path) -> tuple[int, str]:
        return (0 if item.is_dir() else 1, item.name.lower())

    def _format_output(self, entries: list[dict], target: Path, truncated: bool) -> ToolExecutionResult:
        if not entries:
            return _success(f"Directory is empty: {target}")
        lines = []
        for entry in entries:
            if entry["is_directory"]:
                lines.append(f"[DIR]  {entry['path']}")
            else:
                lines.append(f"       {entry['path']}  ({_human_size(entry['size'])})")
        header = f"{len(entries)} entries in {target}"
        if truncated:
            header += f" (truncated to {MAX_LIST_DIR_ENTRIES})"
        return _success(
            "\n".join([header, *lines]),
            data={
                "path": str(target),
                "count": str(len(entries)),
                "entries": entries,
            },
        )


class ListDirToolBuilder(WorkspaceToolBuilder):
    name = "list_dir"
    description = "List a directory in flat or recursive mode."
    parameters_schema = ListDirParams

    def validate(self, params: dict) -> ListDirParams:
        return ListDirParams(**params)

    def build(self, params: ListDirParams) -> ToolInvocation[ListDirParams]:
        return ListDirInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
        )
