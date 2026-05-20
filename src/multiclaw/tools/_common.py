import subprocess
from dataclasses import dataclass, field
from difflib import unified_diff
from fnmatch import fnmatch
from pathlib import Path

from multiclaw.tools.base import ToolBuilder, ToolExecutionResult, ToolInvocation, ToolStatus

MAX_READ_LINES_DEFAULT = 2000
STREAM_THRESHOLD = 10 * 1024 * 1024
MAX_GLOB_RESULTS = 100
MAX_GREP_RESULTS = 100
MAX_GREP_LINE_LENGTH = 500
MAX_LIST_DIR_ENTRIES = 500
MAX_LIST_DIR_DEPTH = 2
MAX_FIND_DIR_RESULTS = 100
DEFAULT_FIND_DIR_DEPTH = 5
VCS_DIRS = {".git", ".svn", ".hg"}


def _success(content: str, data: dict | None = None) -> ToolExecutionResult:
    return ToolExecutionResult(
        status=ToolStatus.SUCCESS,
        content=content,
        data=data or {},
    )


def _error(content: str, data: dict | None = None) -> ToolExecutionResult:
    return ToolExecutionResult(
        status=ToolStatus.ERROR,
        content=content,
        data=data or {},
    )


@dataclass
class PathPolicy:
    workspace_root: Path
    deny_patterns: list[str] = field(default_factory=list)
    allow_outside_workspace: bool = False
    approved_roots: list[Path] = field(default_factory=list)

    def validate_read(self, path: Path) -> str | None:
        resolved = path.resolve()
        if self._is_device_path(resolved):
            return f"Cannot read device file: {resolved}"
        return self._validate_common(resolved)

    def validate_write(self, path: Path) -> str | None:
        resolved = path.resolve()
        error = self._validate_common(resolved)
        if error:
            return error
        if self._is_dangerous_path(resolved):
            return f"Refusing to write to dangerous path: {resolved}"
        return None

    def validate_path(self, path: Path) -> str | None:
        return self._validate_common(path.resolve())

    def _validate_common(self, resolved: Path) -> str | None:
        if (
            not self.allow_outside_workspace
            and not self._is_within_workspace(resolved)
            and not self._is_within_approved_roots(resolved)
        ):
            return f"Path outside workspace: {resolved}"
        if self._matches_deny(resolved):
            return f"Path denied by policy: {resolved}"
        return None

    def _is_within_workspace(self, resolved: Path) -> bool:
        try:
            resolved.relative_to(self.workspace_root.resolve())
            return True
        except ValueError:
            return False

    def _matches_deny(self, path: Path) -> bool:
        path_str = str(path)
        return any(fnmatch(path_str, pattern) for pattern in self.deny_patterns)

    def _is_within_approved_roots(self, resolved: Path) -> bool:
        for root in self.approved_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    def _is_device_path(self, path: Path) -> bool:
        return str(path) in {"/dev/zero", "/dev/random", "/dev/urandom", "/dev/stdin", "/dev/null"}

    def _is_dangerous_path(self, path: Path) -> bool:
        return any(part in {".git", ".env", ".bashrc", ".zshrc", ".ssh"} for part in path.parts)


def _resolve_path(path_text: str, workspace_root: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    return (workspace_root / path).resolve()


def _detect_binary(path: Path, sample_size: int = 4096) -> bool:
    try:
        chunk = path.read_bytes()[:sample_size]
    except OSError:
        return False
    if not chunk:
        return False
    if chunk.startswith(b"\xff\xfe\x00\x00") or chunk.startswith(b"\x00\x00\xfe\xff"):
        return False
    if chunk.startswith(b"\xff\xfe") or chunk.startswith(b"\xfe\xff"):
        return False
    if chunk.startswith(b"\xef\xbb\xbf"):
        return False
    return (chunk.count(b"\x00") / len(chunk)) > 0.05


def _levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        return _levenshtein(right, left)
    if len(right) == 0:
        return len(left)
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left):
        current = [left_index + 1]
        for right_index, right_char in enumerate(right):
            insertions = previous[right_index + 1] + 1
            deletions = current[right_index] + 1
            substitutions = previous[right_index] + (left_char != right_char)
            current.append(min(insertions, deletions, substitutions))
        previous = current
    return previous[-1]


def _run_command(
    cmd: list[str],
    cwd: Path,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"


def _policy_for_invocation(policy: PathPolicy, invocation: ToolInvocation) -> PathPolicy:
    return PathPolicy(
        workspace_root=policy.workspace_root,
        deny_patterns=list(policy.deny_patterns),
        allow_outside_workspace=policy.allow_outside_workspace,
        approved_roots=list(invocation.approved_roots),
    )


def _expand_include(include: str) -> list[str]:
    if "{" in include and "}" in include:
        prefix, rest = include.split("{", 1)
        alternatives, suffix = rest.split("}", 1)
        return [prefix + option + suffix for option in alternatives.split(",")]
    return [include]


def _truncate_diff(diff_text: str, max_lines: int = 50) -> str:
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return diff_text
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more diff lines)"


def _generate_diff(old: str, new: str, filename: str) -> str:
    diff = unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=filename,
        tofile=filename,
        n=3,
    )
    return _truncate_diff("".join(diff))


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


class WorkspaceToolBuilder(ToolBuilder):
    def __init__(self, workspace_root: str | Path | None = None, policy: PathPolicy | None = None) -> None:
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self.policy = policy or PathPolicy(workspace_root=self.workspace_root)
