from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools.base import ToolExecutionResult, ToolInvocation
from multiclaw.workflow.models import RecoveryStrategy
from multiclaw.tools._common import (
    DEFAULT_FIND_DIR_DEPTH,
    MAX_FIND_DIR_RESULTS,
    VCS_DIRS,
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _policy_for_invocation,
    _resolve_path,
    _success,
)


class FindDirParams(BaseModel):
    pattern: str
    path: str | None = None
    max_depth: int = Field(default=DEFAULT_FIND_DIR_DEPTH, ge=0)


class FindDirInvocation(ToolInvocation[FindDirParams]):
    def __init__(self, name: str, params: FindDirParams, workspace_root: Path, policy: PathPolicy) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.policy = policy

    async def execute(self) -> ToolExecutionResult:
        search_dir = self.workspace_root if self.params.path is None else _resolve_path(self.params.path, self.workspace_root)
        policy = _policy_for_invocation(self.policy, self)

        error = policy.validate_path(search_dir)
        if error:
            return _error(error)
        if not search_dir.is_dir():
            return _error(f"Not a directory: {search_dir}")

        matches: list[str] = []
        truncated = False

        def walk(current: Path, depth: int) -> None:
            nonlocal truncated
            if depth > self.params.max_depth or len(matches) >= MAX_FIND_DIR_RESULTS:
                return
            try:
                items = sorted(current.iterdir(), key=lambda item: item.name.lower())
            except PermissionError:
                return
            for item in items:
                if not item.is_dir():
                    continue
                if item.name in VCS_DIRS or item.name.startswith("."):
                    continue
                if fnmatch(item.name, self.params.pattern):
                    if len(matches) >= MAX_FIND_DIR_RESULTS:
                        truncated = True
                        return
                    matches.append(str(item.relative_to(search_dir)))
                    if len(matches) >= MAX_FIND_DIR_RESULTS:
                        continue
                walk(item, depth + 1)

        walk(search_dir, 0)

        if not matches:
            return _success(f"No directories matching '{self.params.pattern}' found.")
        header = f"Found {len(matches)} directory(ies) matching '{self.params.pattern}'"
        if truncated:
            header += f" (limited to {MAX_FIND_DIR_RESULTS})"
        return _success("\n".join([header, *matches[:MAX_FIND_DIR_RESULTS]]))


class FindDirToolBuilder(WorkspaceToolBuilder):
    name = "find_dir"
    description = "Find directories by name pattern within the workspace."
    parameters_schema = FindDirParams
    read_only = True
    recovery_strategy = RecoveryStrategy.READ_ONLY_REPLAY

    def validate(self, params: dict) -> FindDirParams:
        return FindDirParams(**params)

    def build(self, params: FindDirParams) -> ToolInvocation[FindDirParams]:
        return FindDirInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
        )
