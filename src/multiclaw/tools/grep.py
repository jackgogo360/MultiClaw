import re
import shutil
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools.base import ToolExecutionResult, ToolInvocation, ToolStatus
from multiclaw.tools._common import (
    MAX_GREP_LINE_LENGTH,
    MAX_GREP_RESULTS,
    VCS_DIRS,
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _expand_include,
    _policy_for_invocation,
    _resolve_path,
    _run_command,
    _success,
)


class GrepParams(BaseModel):
    pattern: str
    path: str | None = None
    include: str | None = None
    context: int = Field(default=0, ge=0)
    case_sensitive: bool = False
    output_mode: str = "content"


class GrepInvocation(ToolInvocation[GrepParams]):
    def __init__(self, name: str, params: GrepParams, workspace_root: Path, policy: PathPolicy) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.policy = policy

    async def execute(self) -> ToolExecutionResult:
        search_dir = self.workspace_root if self.params.path is None else _resolve_path(self.params.path, self.workspace_root)
        policy = _policy_for_invocation(self.policy, self)

        error = policy.validate_path(search_dir)
        if error:
            return _error(error)
        if not search_dir.exists():
            return _error(f"Path not found: {search_dir}")

        if shutil.which("rg"):
            result = self._execute_ripgrep(search_dir)
            if result.status == ToolStatus.SUCCESS:
                return result
        if shutil.which("grep"):
            result = self._execute_grep(search_dir)
            if result.status == ToolStatus.SUCCESS:
                return result
        return self._execute_python(search_dir)

    def _execute_ripgrep(self, search_dir: Path) -> ToolExecutionResult:
        args = ["rg", "--hidden", "--max-columns", str(MAX_GREP_LINE_LENGTH)]
        for vcs_dir in VCS_DIRS:
            args.extend(["--glob", f"!{vcs_dir}/*"])
        if not self.params.case_sensitive:
            args.append("-i")
        if self.params.output_mode == "files_with_matches":
            args.append("-l")
            args.append("--sort=modified")
        elif self.params.output_mode == "count":
            args.append("-c")
        else:
            args.append("-n")
            if self.params.context > 0:
                args.extend(["-C", str(self.params.context)])
        if self.params.include:
            for include_pattern in _expand_include(self.params.include):
                args.extend(["--glob", include_pattern])
        args.extend(["--", self.params.pattern, "."])

        code, stdout, stderr = _run_command(args, cwd=search_dir)
        if code == 2:
            return _error(f"ripgrep error: {stderr.strip()}")
        if code == 1:
            return _success("No matches found.")
        lines = [line.lstrip("./") for line in stdout.splitlines() if line.strip()]
        return self._format_lines(lines)

    def _execute_grep(self, search_dir: Path) -> ToolExecutionResult:
        args = ["grep", "-r", "-n", "-H", "-E", "-I"]
        if not self.params.case_sensitive:
            args.append("-i")
        if self.params.output_mode == "files_with_matches":
            args.append("-l")
        elif self.params.output_mode == "count":
            args.append("-c")
        if self.params.context > 0 and self.params.output_mode == "content":
            args.extend(["-C", str(self.params.context)])
        for vcs_dir in VCS_DIRS:
            args.extend(["--exclude-dir", vcs_dir])
        if self.params.include:
            for include_pattern in _expand_include(self.params.include):
                args.extend(["--include", include_pattern])
        args.extend(["--", self.params.pattern, "."])

        code, stdout, stderr = _run_command(args, cwd=search_dir)
        if code == 2:
            return _error(f"grep error: {stderr.strip()}")
        if code == 1:
            return _success("No matches found.")
        lines = [line.lstrip("./") for line in stdout.splitlines() if line.strip()]
        return self._format_lines(lines)

    def _execute_python(self, search_dir: Path) -> ToolExecutionResult:
        flags = 0 if self.params.case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(self.params.pattern, flags)
        except re.error as exc:
            return _error(f"Invalid regex: {exc}")

        if self.params.output_mode == "count":
            counts: list[str] = []
            for file_path in self._walk_files(search_dir):
                if self.params.include and not self._matches_include(file_path.name):
                    continue
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                count = len(regex.findall(content))
                if count > 0:
                    counts.append(f"{file_path.relative_to(search_dir)}:{count}")
            if not counts:
                return _success("No matches found.")
            return self._format_lines(counts)

        matches: list[str] = []
        files_with_matches: list[str] = []
        for file_path in self._walk_files(search_dir):
            if self.params.include and not self._matches_include(file_path.name):
                continue
            try:
                lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            relative = str(file_path.relative_to(search_dir))
            file_has_match = False
            for line_number, line in enumerate(lines, 1):
                if regex.search(line):
                    file_has_match = True
                    if self.params.output_mode == "files_with_matches":
                        files_with_matches.append(relative)
                        break
                    matches.append(f"{relative}:{line_number}:{line[:MAX_GREP_LINE_LENGTH]}")
                    if len(matches) >= MAX_GREP_RESULTS:
                        return self._format_lines(matches)
            if self.params.output_mode == "files_with_matches" and file_has_match and len(files_with_matches) >= MAX_GREP_RESULTS:
                break

        if self.params.output_mode == "files_with_matches":
            if not files_with_matches:
                return _success("No matches found.")
            return self._format_lines(files_with_matches)
        if not matches:
            return _success("No matches found.")
        return self._format_lines(matches)

    def _walk_files(self, search_dir: Path):
        for root, dirs, files in search_dir.walk():
            dirs[:] = [dir_name for dir_name in dirs if dir_name not in VCS_DIRS and not dir_name.startswith(".")]
            for file_name in sorted(files):
                yield root / file_name

    def _matches_include(self, filename: str) -> bool:
        if self.params.include is None:
            return True
        return any(fnmatch(filename, pattern) for pattern in _expand_include(self.params.include))

    def _format_lines(self, lines: list[str]) -> ToolExecutionResult:
        if not lines:
            return _success("No matches found.")
        truncated = len(lines) > MAX_GREP_RESULTS
        lines = lines[:MAX_GREP_RESULTS]
        if self.params.output_mode == "files_with_matches":
            header = f"Found {len(lines)} file(s) with matches"
        elif self.params.output_mode == "count":
            header = f"{len(lines)} file(s) with matches"
        else:
            header = f"Found {len(lines)} match(es)"
        if truncated:
            header += f" (limited to {MAX_GREP_RESULTS})"
        return _success("\n".join([header, *lines]))


class GrepToolBuilder(WorkspaceToolBuilder):
    name = "grep"
    description = "Search file contents in the workspace."
    parameters_schema = GrepParams

    def validate(self, params: dict) -> GrepParams:
        return GrepParams(**params)

    def build(self, params: GrepParams) -> ToolInvocation[GrepParams]:
        return GrepInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
        )
