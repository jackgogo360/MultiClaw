import re
import shutil
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools.base import ToolBuilder, ToolExecutionResult, ToolInvocation, ToolStatus
from multiclaw.tools._common import (
    DEFAULT_FIND_DIR_DEPTH,
    MAX_FIND_DIR_RESULTS,
    MAX_GLOB_RESULTS,
    MAX_GREP_LINE_LENGTH,
    MAX_GREP_RESULTS,
    MAX_LIST_DIR_ENTRIES,
    MAX_LIST_DIR_DEPTH,
    VCS_DIRS,
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _expand_include,
    _human_size,
    _policy_for_invocation,
    _resolve_path,
    _run_command,
    _success,
)

from multiclaw.tools.read_file import ReadFileToolBuilder

from multiclaw.tools.write_file import WriteFileToolBuilder

from multiclaw.tools.edit_file import EditFileToolBuilder, UndoEditToolBuilder


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

    def validate(self, params: dict) -> GlobParams:
        return GlobParams(**params)

    def build(self, params: GlobParams) -> ToolInvocation[GlobParams]:
        return GlobInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
        )


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

    def validate(self, params: dict) -> FindDirParams:
        return FindDirParams(**params)

    def build(self, params: FindDirParams) -> ToolInvocation[FindDirParams]:
        return FindDirInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
        )
