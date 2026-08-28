import shutil
from pathlib import Path

from pydantic import BaseModel

from multiclaw.tools.base import ToolExecutionResult, ToolInvocation, ToolStatus
from multiclaw.workflow.models import RecoveryStrategy
from multiclaw.tools._common import (
    MAX_GLOB_RESULTS,
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _policy_for_invocation,
    _resolve_path,
    _run_command,
    _success,
)


class GlobParams(BaseModel):
    pattern: str
    path: str | None = None


class GlobInvocation(ToolInvocation[GlobParams]):
    def __init__(self, name: str, params: GlobParams, workspace_root: Path, policy: PathPolicy) -> None:
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

        if shutil.which("rg"):
            result = self._execute_ripgrep(search_dir)
            if result.status == ToolStatus.SUCCESS:
                return result
        return self._execute_python(search_dir)

    def _execute_ripgrep(self, search_dir: Path) -> ToolExecutionResult:
        code, stdout, stderr = _run_command(
            [
                "rg",
                "--files",
                "--glob",
                self.params.pattern,
                "--sort=modified",
                "--hidden",
                "--glob",
                "!.git/*",
                ".",
            ],
            cwd=search_dir,
        )
        if code not in (0, 1):
            return _error(f"ripgrep error: {stderr.strip()}")
        files = [line.lstrip("./") for line in stdout.splitlines() if line.strip()]
        return self._format(files)

    def _execute_python(self, search_dir: Path) -> ToolExecutionResult:
        pattern = self.params.pattern
        if not pattern.startswith("**/") and "/" not in pattern:
            pattern = f"**/{pattern}"
        matches: list[tuple[float, str]] = []
        for path in search_dir.glob(pattern):
            if path.is_file() and ".git" not in path.parts:
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = 0
                matches.append((mtime, str(path.relative_to(search_dir))))
        matches.sort(key=lambda item: item[0], reverse=True)
        return self._format([path for _, path in matches])

    def _format(self, files: list[str]) -> ToolExecutionResult:
        if not files:
            return _success("No files matched.")
        truncated = len(files) > MAX_GLOB_RESULTS
        files = files[:MAX_GLOB_RESULTS]
        header = f"Found {len(files)} file(s)"
        if truncated:
            header += f" (truncated to {MAX_GLOB_RESULTS}, use a more specific pattern)"
        return _success("\n".join([header, *files]))


class GlobToolBuilder(WorkspaceToolBuilder):
    name = "glob"
    description = "Find files by glob pattern within the workspace."
    parameters_schema = GlobParams
    read_only = True
    recovery_strategy = RecoveryStrategy.READ_ONLY_REPLAY

    def validate(self, params: dict) -> GlobParams:
        return GlobParams(**params)

    def build(self, params: GlobParams) -> ToolInvocation[GlobParams]:
        return GlobInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
        )
