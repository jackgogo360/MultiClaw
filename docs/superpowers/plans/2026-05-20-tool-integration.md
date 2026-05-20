# Tool Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Shell, CodeExec, WebFetch, WebSearch tools from agent-code reference implementations into MultiClaw, adapting them to the ToolBuilder/ToolInvocation pattern. Also split existing `builtin.py` into one-file-per-tool.

**Architecture:** Extract shared utilities from `builtin.py` into `_common.py`. Each tool lives in its own file under `src/multiclaw/tools/`. Four new tools are adapted from the agent-code dataclass pattern to the project's Pydantic ToolBuilder pattern. Existing tool logic is preserved exactly — only moved.

**Tech Stack:** Python 3.12, Pydantic, asyncio, multiprocessing, httpx (optional), duckduckgo-search (optional), playwright (optional), trafilatura (optional), html2text (optional)

---

### Task 1: Extract `_common.py` from `builtin.py`

**Files:**
- Create: `src/multiclaw/tools/_common.py`
- Modify: `src/multiclaw/tools/builtin.py`

- [ ] **Step 1: Create `_common.py` with all shared utilities**

Move all shared constants, helpers, and the `WorkspaceToolBuilder` base class into `_common.py`:

```python
"""Shared utilities for builtin tools."""

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import unified_diff
from fnmatch import fnmatch
from pathlib import Path

from multiclaw.tools.base import ToolBuilder, ToolExecutionResult, ToolInvocation, ToolStatus

# --- constants ---

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

# --- result helpers ---


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


# --- path policy ---


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


# --- path helpers ---


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


# --- base class for workspace tools ---


class WorkspaceToolBuilder(ToolBuilder):
    def __init__(self, workspace_root: str | Path | None = None, policy: PathPolicy | None = None) -> None:
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self.policy = policy or PathPolicy(workspace_root=self.workspace_root)
```

- [ ] **Step 2: Rewrite `builtin.py` to import from `_common.py`**

Replace the top section of `builtin.py` (lines 1-208) — all constants, helpers, PathPolicy, and WorkspaceToolBuilder — with a single import block:

```python
from multiclaw.tools._common import (
    DEFAULT_FIND_DIR_DEPTH,
    MAX_FIND_DIR_RESULTS,
    MAX_GLOB_RESULTS,
    MAX_GREP_LINE_LENGTH,
    MAX_GREP_RESULTS,
    MAX_LIST_DIR_ENTRIES,
    MAX_LIST_DIR_DEPTH,
    MAX_READ_LINES_DEFAULT,
    STREAM_THRESHOLD,
    VCS_DIRS,
    PathPolicy,
    WorkspaceToolBuilder,
    _detect_binary,
    _error,
    _expand_include,
    _generate_diff,
    _human_size,
    _levenshtein,
    _policy_for_invocation,
    _resolve_path,
    _run_command,
    _success,
    _truncate_diff,
)
```

Remove all the original constant/helper/PathPolicy/WorkspaceToolBuilder definitions (lines 1-208).

- [ ] **Step 3: Run existing tests to verify nothing broke**

Run: `pytest tests/test_tools.py -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/multiclaw/tools/_common.py src/multiclaw/tools/builtin.py
git commit -m "refactor: extract shared utilities from builtin.py into _common.py"
```

---

### Task 2: Split `read_file.py` from `builtin.py`

**Files:**
- Create: `src/multiclaw/tools/read_file.py`
- Modify: `src/multiclaw/tools/builtin.py`

- [ ] **Step 1: Create `read_file.py`**

Move `ReadFileParams`, `ReadFileInvocation`, `ReadFileToolBuilder` classes from `builtin.py` into `read_file.py`. All logic preserved exactly.

```python
"""ReadFile tool — read files with line ranges and encoding detection."""

from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools._common import (
    MAX_READ_LINES_DEFAULT,
    STREAM_THRESHOLD,
    PathPolicy,
    WorkspaceToolBuilder,
    _detect_binary,
    _error,
    _levenshtein,
    _policy_for_invocation,
    _resolve_path,
    _success,
)
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation


class ReadFileParams(BaseModel):
    file_path: str
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=MAX_READ_LINES_DEFAULT, ge=1)


class ReadFileInvocation(ToolInvocation[ReadFileParams]):
    def __init__(
        self,
        name: str,
        params: ReadFileParams,
        workspace_root: Path,
        policy: PathPolicy,
        read_state: dict[str, float],
    ) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.policy = policy
        self.read_state = read_state

    async def execute(self) -> ToolExecutionResult:
        path = _resolve_path(self.params.file_path, self.workspace_root)
        policy = _policy_for_invocation(self.policy, self)

        error = policy.validate_read(path)
        if error:
            return _error(error)
        if not path.exists():
            message = f"File not found: {path}"
            suggestion = self._suggest_similar(path)
            if suggestion:
                message += f"\nDid you mean: {suggestion}?"
            return _error(message)
        if not path.is_file():
            return _error(f"Not a regular file: {path}")
        if _detect_binary(path):
            return _error(
                f"Cannot read binary file: {path}\n"
                "Use appropriate tools for binary file formats."
            )

        try:
            file_size = path.stat().st_size
            if file_size >= STREAM_THRESHOLD:
                content, total_lines = self._read_streaming(path)
            else:
                content, total_lines = self._read_standard(path)
        except OSError as exc:
            return _error(f"Error reading file: {exc}")

        self.read_state[str(path)] = path.stat().st_mtime
        shown_lines = max(0, len(content.splitlines()))
        end_line = self.params.offset + shown_lines - 1
        header = f"[{path}] Lines {self.params.offset}-{end_line} of {total_lines}"
        if end_line < total_lines:
            header += f" (use offset={end_line + 1} to continue)"

        return _success(
            f"{header}\n{content}",
            data={
                "path": str(path),
                "total_lines": str(total_lines),
                "shown_lines": f"{self.params.offset}-{end_line}",
            },
        )

    def _read_standard(self, path: Path) -> tuple[str, int]:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = self._read_with_fallback_encoding(path)
        all_lines = text.splitlines(keepends=True)
        return self._slice_lines(all_lines)

    def _read_streaming(self, path: Path) -> tuple[str, int]:
        lines: list[str] = []
        start_idx = self.params.offset - 1
        total = 0

        try:
            with path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    total += 1
                    if index >= start_idx and len(lines) < self.params.limit:
                        lines.append(line)
        except UnicodeDecodeError:
            with path.open("r", encoding=self._detect_text_encoding(path), errors="replace") as handle:
                for index, line in enumerate(handle):
                    total += 1
                    if index >= start_idx and len(lines) < self.params.limit:
                        lines.append(line)

        return self._add_line_numbers(lines), total

    def _slice_lines(self, all_lines: list[str]) -> tuple[str, int]:
        total = len(all_lines)
        start_idx = self.params.offset - 1
        if start_idx >= total:
            return "", total
        end_idx = min(total, start_idx + self.params.limit)
        return self._add_line_numbers(all_lines[start_idx:end_idx]), total

    def _add_line_numbers(self, lines: list[str]) -> str:
        width = len(str(self.params.offset + len(lines)))
        output = []
        for index, line in enumerate(lines):
            line_no = self.params.offset + index
            output.append(f"{line_no:>{width}}\t{line.rstrip()}")
        return "\n".join(output)

    def _read_with_fallback_encoding(self, path: Path) -> str:
        encoding = self._detect_text_encoding(path)
        return path.read_text(encoding=encoding, errors="replace")

    def _detect_text_encoding(self, path: Path) -> str:
        raw = path.read_bytes()
        if raw.startswith(b"\xff\xfe\x00\x00") or raw.startswith(b"\x00\x00\xfe\xff"):
            return "utf-32"
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            return "utf-16"
        if raw.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        return "latin-1"

    def _suggest_similar(self, path: Path) -> str | None:
        parent = path.parent
        if not parent.exists():
            return None
        name = path.name.lower()
        try:
            candidates = [child.name for child in parent.iterdir() if child.is_file()]
        except OSError:
            return None
        for candidate in candidates:
            if candidate.lower() == name or _levenshtein(candidate.lower(), name) <= 2:
                return str(parent / candidate)
        return None


class ReadFileToolBuilder(WorkspaceToolBuilder):
    name = "read_file"
    description = "Read a file from the workspace with 1-based line ranges."
    parameters_schema = ReadFileParams

    def __init__(self, workspace_root: str | Path | None = None, policy: PathPolicy | None = None) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self._read_state: dict[str, float] = {}

    def validate(self, params: dict) -> ReadFileParams:
        return ReadFileParams(**params)

    def build(self, params: ReadFileParams) -> ToolInvocation[ReadFileParams]:
        return ReadFileInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
            read_state=self._read_state,
        )

    def has_been_read(self, file_path: str) -> bool:
        return str(_resolve_path(file_path, self.workspace_root)) in self._read_state

    def get_read_mtime(self, file_path: str) -> float | None:
        return self._read_state.get(str(_resolve_path(file_path, self.workspace_root)))
```

- [ ] **Step 2: Remove `ReadFileParams`, `ReadFileInvocation`, `ReadFileToolBuilder` from `builtin.py`**

Delete those three classes from `builtin.py`.

- [ ] **Step 3: Run tests to verify**

Run: `pytest tests/test_tools.py -v -k "ReadFile or read_file or test_file" --no-header -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/multiclaw/tools/read_file.py src/multiclaw/tools/builtin.py
git commit -m "refactor: extract read_file tool into own file"
```

---

### Task 3: Split `write_file.py` from `builtin.py`

**Files:**
- Create: `src/multiclaw/tools/write_file.py`
- Modify: `src/multiclaw/tools/builtin.py`

- [ ] **Step 1: Create `write_file.py`**

Move `WriteFileParams`, `WriteFileInvocation`, `WriteFileToolBuilder` from `builtin.py`.

```python
"""WriteFile tool — create or overwrite files with atomic writes."""

import os
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel

from multiclaw.tools._common import (
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _generate_diff,
    _policy_for_invocation,
    _resolve_path,
    _success,
)
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation


class WriteFileParams(BaseModel):
    file_path: str
    content: str


class WriteFileInvocation(ToolInvocation[WriteFileParams]):
    def __init__(
        self,
        name: str,
        params: WriteFileParams,
        workspace_root: Path,
        policy: PathPolicy,
        read_builder,
        require_read_before_write: bool,
    ) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.policy = policy
        self.read_builder = read_builder
        self.require_read_before_write = require_read_before_write

    async def execute(self) -> ToolExecutionResult:
        path = _resolve_path(self.params.file_path, self.workspace_root)
        policy = _policy_for_invocation(self.policy, self)

        error = policy.validate_write(path)
        if error:
            return _error(error)

        placeholder = self._detect_placeholder(self.params.content)
        if placeholder:
            return _error(
                f"Content contains omission placeholder: '{placeholder}'\n"
                "Please provide the complete file content."
            )

        is_new = not path.exists()
        if not is_new and self.require_read_before_write and self.read_builder is not None:
            if not self.read_builder.has_been_read(str(path)):
                return _error("File has not been read yet. Read it first before writing to it.")
            saved_mtime = self.read_builder.get_read_mtime(str(path))
            current_mtime = path.stat().st_mtime
            if saved_mtime is not None and current_mtime > saved_mtime:
                return _error("File has been modified since last read. Read it again to see the current content.")

        content = self.params.content.replace("\r\n", "\n")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if is_new:
                path.write_text(content, encoding="utf-8")
                return _success(f"Created new file: {path}", data={"path": str(path), "created": "true"})

            old_content = path.read_text(encoding="utf-8")
            self._atomic_write(path, content)
            diff = _generate_diff(old_content, content, str(path))
            return _success(
                f"Updated file: {path}\n{diff}",
                data={"path": str(path), "created": "false"},
            )
        except OSError as exc:
            return _error(f"Error writing file: {exc}")

    def _atomic_write(self, path: Path, content: str) -> None:
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".write_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            shutil.move(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _detect_placeholder(self, content: str) -> str | None:
        indicators = [
            "... rest of",
            "// ...",
            "# ...",
            "/* ... */",
            "... remaining",
            "... other methods",
            "... (unchanged)",
            "[rest of the code]",
            "[remaining code]",
        ]
        for line in content.splitlines():
            lowered = line.strip().lower()
            if any(indicator in lowered for indicator in indicators):
                return line.strip()
        return None


class WriteFileToolBuilder(WorkspaceToolBuilder):
    name = "write_file"
    description = "Write full file content to the workspace. Existing files must be read first."
    parameters_schema = WriteFileParams

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        read_builder=None,
        policy: PathPolicy | None = None,
        require_read_before_write: bool = True,
    ) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.read_builder = read_builder
        self.require_read_before_write = require_read_before_write

    def validate(self, params: dict) -> WriteFileParams:
        return WriteFileParams(**params)

    def build(self, params: WriteFileParams) -> ToolInvocation[WriteFileParams]:
        return WriteFileInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
            read_builder=self.read_builder,
            require_read_before_write=self.require_read_before_write,
        )
```

- [ ] **Step 2: Remove `WriteFileParams`, `WriteFileInvocation`, `WriteFileToolBuilder` from `builtin.py`**

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_tools.py -v -k "write_file" --no-header -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/multiclaw/tools/write_file.py src/multiclaw/tools/builtin.py
git commit -m "refactor: extract write_file tool into own file"
```

---

### Task 4: Split `edit_file.py` from `builtin.py`

**Files:**
- Create: `src/multiclaw/tools/edit_file.py`
- Modify: `src/multiclaw/tools/builtin.py`

- [ ] **Step 1: Create `edit_file.py`**

Move `EditFileParams`, `EditFileInvocation`, `EditFileToolBuilder`, `UndoEditParams`, `UndoEditInvocation`, `UndoEditToolBuilder` from `builtin.py`. Contains both EditFile and UndoEdit since they're tightly coupled.

```python
"""EditFile and UndoEdit tools — precise string replacement with fuzzy matching."""

import re
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools._common import (
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _generate_diff,
    _policy_for_invocation,
    _resolve_path,
    _success,
)
from multiclaw.tools.base import ToolBuilder, ToolExecutionResult, ToolInvocation, ToolStatus


class EditFileParams(BaseModel):
    file_path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class EditFileInvocation(ToolInvocation[EditFileParams]):
    def __init__(
        self,
        name: str,
        params: EditFileParams,
        workspace_root: Path,
        policy: PathPolicy,
        history: dict[str, list[str]],
        max_history: int,
    ) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.policy = policy
        self.history = history
        self.max_history = max_history

    async def execute(self) -> ToolExecutionResult:
        path = _resolve_path(self.params.file_path, self.workspace_root)
        policy = _policy_for_invocation(self.policy, self)

        error = policy.validate_write(path)
        if error:
            return _error(error)

        if self.params.old_string == "" and not path.exists():
            return self._create_file(path, self.params.new_string)

        if not path.exists():
            return _error(f"File not found: {path}")
        if self.params.old_string == self.params.new_string:
            return _error("old_string and new_string are identical.")

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _error("Cannot edit binary or non-UTF-8 file.")
        except OSError as exc:
            return _error(f"Error reading file: {exc}")

        replacement = self._find_and_replace(content)
        if replacement.status == ToolStatus.ERROR:
            return replacement

        new_content = replacement.data["new_content"]
        self._push_history(str(path), content)
        try:
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return _error(f"Error writing file: {exc}")

        diff = _generate_diff(content, new_content, str(path))
        return _success(
            f"Edited {path}\n{diff}",
            data={"path": str(path)},
        )

    def _create_file(self, path: Path, content: str) -> ToolExecutionResult:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return _success(f"Created new file: {path}", data={"path": str(path)})
        except OSError as exc:
            return _error(f"Error creating file: {exc}")

    def _find_and_replace(self, content: str) -> ToolExecutionResult:
        strategies = [
            ("exact", self._match_exact),
            ("trimmed", self._match_line_trimmed),
            ("whitespace_normalized", self._match_whitespace_normalized),
            ("indentation_flexible", self._match_indentation_flexible),
        ]

        for strategy_name, matcher in strategies:
            matches = matcher(content, self.params.old_string)
            if not matches:
                continue
            if self.params.replace_all:
                updated = content
                for start, end in reversed(matches):
                    updated = updated[:start] + self.params.new_string + updated[end:]
                return _success("", data={"new_content": updated})
            if len(matches) == 1:
                start, end = matches[0]
                updated = content[:start] + self.params.new_string + content[end:]
                return _success("", data={"new_content": updated})
            locations = self._describe_match_locations(content, matches)
            return _error(
                f"Found {len(matches)} matches (strategy: {strategy_name}). "
                "Provide more context to uniquely identify the target.\n"
                f"Matches at:\n{locations}"
            )

        return _error(
            "No match found for old_string in the file.\n"
            "Ensure the text exactly matches what's in the file, including whitespace and indentation."
        )

    def _match_exact(self, content: str, needle: str) -> list[tuple[int, int]]:
        matches: list[tuple[int, int]] = []
        start = 0
        while True:
            found = content.find(needle, start)
            if found == -1:
                return matches
            matches.append((found, found + len(needle)))
            start = found + 1

    def _match_line_trimmed(self, content: str, needle: str) -> list[tuple[int, int]]:
        needle_lines = [line.strip() for line in needle.splitlines()]
        content_lines = content.splitlines(keepends=True)
        matches: list[tuple[int, int]] = []
        for index in range(len(content_lines) - len(needle_lines) + 1):
            window = content_lines[index : index + len(needle_lines)]
            if [line.strip() for line in window] == needle_lines:
                start = sum(len(line) for line in content_lines[:index])
                end = start + sum(len(line) for line in window)
                matches.append((start, end))
        return matches

    def _match_whitespace_normalized(self, content: str, needle: str) -> list[tuple[int, int]]:
        def normalize(text: str) -> str:
            return re.sub(r"\s+", " ", text).strip()

        normalized_needle = normalize(needle)
        if not normalized_needle:
            return []
        content_lines = content.splitlines(keepends=True)
        needle_line_count = len(needle.splitlines())
        matches: list[tuple[int, int]] = []
        for index in range(len(content_lines) - needle_line_count + 1):
            window = content_lines[index : index + needle_line_count]
            if normalize("".join(window)) == normalized_needle:
                start = sum(len(line) for line in content_lines[:index])
                end = start + sum(len(line) for line in window)
                matches.append((start, end))
        return matches

    def _match_indentation_flexible(self, content: str, needle: str) -> list[tuple[int, int]]:
        needle_lines = needle.splitlines()
        if not needle_lines:
            return []

        min_indent = min(
            (len(line) - len(line.lstrip()) for line in needle_lines if line.strip()),
            default=0,
        )
        dedented_needle = [line[min_indent:] if len(line) >= min_indent else line for line in needle_lines]

        content_lines = content.splitlines(keepends=True)
        matches: list[tuple[int, int]] = []
        for index in range(len(content_lines) - len(dedented_needle) + 1):
            window = content_lines[index : index + len(dedented_needle)]
            stripped_window = [line.rstrip("\r\n") for line in window]
            if not stripped_window or not stripped_window[0].strip():
                continue
            indent = len(stripped_window[0]) - len(stripped_window[0].lstrip())
            dedented_window = [line[indent:] if len(line) >= indent else line for line in stripped_window]
            if dedented_window == [line.rstrip("\r\n") for line in dedented_needle]:
                start = sum(len(line) for line in content_lines[:index])
                end = start + sum(len(line) for line in window)
                matches.append((start, end))
        return matches

    def _describe_match_locations(self, content: str, matches: list[tuple[int, int]]) -> str:
        lines = content.splitlines()
        descriptions = []
        for start, _ in matches:
            line_number = content[:start].count("\n") + 1
            line_content = lines[line_number - 1].strip() if line_number <= len(lines) else ""
            descriptions.append(f"  Line {line_number}: {line_content[:80]}")
        return "\n".join(descriptions)

    def _push_history(self, path_key: str, content: str) -> None:
        self.history.setdefault(path_key, []).append(content)
        if len(self.history[path_key]) > self.max_history:
            self.history[path_key].pop(0)


class EditFileToolBuilder(WorkspaceToolBuilder):
    name = "edit_file"
    description = "Edit a file by replacing old_string with new_string."
    parameters_schema = EditFileParams

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        policy: PathPolicy | None = None,
        max_history: int = 10,
    ) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.max_history = max_history
        self._history: dict[str, list[str]] = {}

    def validate(self, params: dict) -> EditFileParams:
        return EditFileParams(**params)

    def build(self, params: EditFileParams) -> ToolInvocation[EditFileParams]:
        return EditFileInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
            history=self._history,
            max_history=self.max_history,
        )

    def undo(self, file_path: str) -> ToolExecutionResult:
        path = _resolve_path(file_path, self.workspace_root)
        history = self._history.get(str(path), [])
        if not history:
            return _error(f"No edit history for: {path}")
        previous = history.pop()
        try:
            path.write_text(previous, encoding="utf-8")
        except OSError as exc:
            return _error(f"Error restoring file: {exc}")
        return _success(f"Reverted {path} to previous state.", data={"path": str(path)})


class UndoEditParams(BaseModel):
    file_path: str


class UndoEditInvocation(ToolInvocation[UndoEditParams]):
    def __init__(self, name: str, params: UndoEditParams, edit_builder: EditFileToolBuilder) -> None:
        super().__init__(name=name, params=params)
        self.edit_builder = edit_builder

    async def execute(self) -> ToolExecutionResult:
        return self.edit_builder.undo(self.params.file_path)


class UndoEditToolBuilder(ToolBuilder[UndoEditParams]):
    name = "undo_edit"
    description = "Undo the most recent edit_file change for a file."
    parameters_schema = UndoEditParams

    def __init__(self, workspace_root: str | Path | None = None, edit_builder: EditFileToolBuilder | None = None) -> None:
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self.edit_builder = edit_builder or EditFileToolBuilder(self.workspace_root)

    def validate(self, params: dict) -> UndoEditParams:
        return UndoEditParams(**params)

    def build(self, params: UndoEditParams) -> ToolInvocation[UndoEditParams]:
        return UndoEditInvocation(name=self.name, params=params, edit_builder=self.edit_builder)
```

- [ ] **Step 2: Remove those 6 classes from `builtin.py`**

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_tools.py -v -k "edit_file or undo_edit" --no-header -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/multiclaw/tools/edit_file.py src/multiclaw/tools/builtin.py
git commit -m "refactor: extract edit_file and undo_edit tools into own file"
```

---

### Task 5: Split `glob.py` from `builtin.py`

**Files:**
- Create: `src/multiclaw/tools/glob.py`
- Modify: `src/multiclaw/tools/builtin.py`

- [ ] **Step 1: Create `glob.py`**

Move `GlobParams`, `GlobInvocation`, `GlobToolBuilder` from `builtin.py`.

```python
"""Glob tool — find files by pattern using ripgrep or Python glob."""

import shutil
from pathlib import Path

from pydantic import BaseModel

from multiclaw.tools._common import (
    MAX_GLOB_RESULTS,
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _resolve_path,
    _run_command,
    _success,
)
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation, ToolStatus


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

        error = self.policy.validate_path(search_dir)
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
```

- [ ] **Step 2: Remove `GlobParams`, `GlobInvocation`, `GlobToolBuilder` from `builtin.py`**

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_tools.py -v -k "glob" --no-header -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/multiclaw/tools/glob.py src/multiclaw/tools/builtin.py
git commit -m "refactor: extract glob tool into own file"
```

---

### Task 6: Split `list_dir.py` from `builtin.py`

**Files:**
- Create: `src/multiclaw/tools/list_dir.py`
- Modify: `src/multiclaw/tools/builtin.py`

- [ ] **Step 1: Create `list_dir.py`**

Move `ListDirParams`, `ListDirInvocation`, `ListDirToolBuilder` from `builtin.py`.

```python
"""ListDir tool — list directory contents flat or recursive."""

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools._common import (
    MAX_LIST_DIR_ENTRIES,
    MAX_LIST_DIR_DEPTH,
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _human_size,
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

        error = self.policy.validate_path(target)
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
```

- [ ] **Step 2: Remove `ListDirParams`, `ListDirInvocation`, `ListDirToolBuilder` from `builtin.py`**

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_tools.py -v -k "list_dir" --no-header -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/multiclaw/tools/list_dir.py src/multiclaw/tools/builtin.py
git commit -m "refactor: extract list_dir tool into own file"
```

---

### Task 7: Split `grep.py` from `builtin.py`

**Files:**
- Create: `src/multiclaw/tools/grep.py`
- Modify: `src/multiclaw/tools/builtin.py`

- [ ] **Step 1: Create `grep.py`**

Move `GrepParams`, `GrepInvocation`, `GrepToolBuilder` from `builtin.py`.

```python
"""Grep tool — search file contents with ripgrep/grep/Python fallback."""

import re
import shutil
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools._common import (
    MAX_GREP_LINE_LENGTH,
    MAX_GREP_RESULTS,
    VCS_DIRS,
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _expand_include,
    _resolve_path,
    _run_command,
    _success,
)
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation, ToolStatus


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

        error = self.policy.validate_path(search_dir)
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
```

- [ ] **Step 2: Remove `GrepParams`, `GrepInvocation`, `GrepToolBuilder` from `builtin.py`**

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_tools.py -v -k "grep" --no-header -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/multiclaw/tools/grep.py src/multiclaw/tools/builtin.py
git commit -m "refactor: extract grep tool into own file"
```

---

### Task 8: Split `find_dir.py` from `builtin.py`

**Files:**
- Create: `src/multiclaw/tools/find_dir.py`
- Modify: `src/multiclaw/tools/builtin.py`

- [ ] **Step 1: Create `find_dir.py`**

Move `FindDirParams`, `FindDirInvocation`, `FindDirToolBuilder` from `builtin.py`.

```python
"""FindDir tool — find directories by name pattern."""

from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools._common import (
    DEFAULT_FIND_DIR_DEPTH,
    MAX_FIND_DIR_RESULTS,
    VCS_DIRS,
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _resolve_path,
    _success,
)
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation


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

        error = self.policy.validate_path(search_dir)
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
```

- [ ] **Step 2: Remove `FindDirParams`, `FindDirInvocation`, `FindDirToolBuilder` from `builtin.py`**

At this point `builtin.py` should be empty except for the imports from `_common.py`. Verify with: `wc -l builtin.py` → should be ~30 lines.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_tools.py -v -k "find_dir" --no-header -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/multiclaw/tools/find_dir.py src/multiclaw/tools/builtin.py
git commit -m "refactor: extract find_dir tool into own file"
```

---

### Task 9: Wire `__init__.py` and `server.py` to new files; delete `builtin.py`

**Files:**
- Modify: `src/multiclaw/tools/__init__.py`
- Modify: `src/multiclaw/server.py`
- Modify: `tests/test_tools.py`
- Delete: `src/multiclaw/tools/builtin.py`

- [ ] **Step 1: Update `__init__.py`**

```python
from multiclaw.tools.base import (
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolStatus,
)
from multiclaw.tools.registry import ToolRegistry
from multiclaw.tools.scheduler import CoreToolScheduler

__all__ = [
    "CoreToolScheduler",
    "ToolBuilder",
    "ToolExecutionResult",
    "ToolInvocation",
    "ToolRegistry",
    "ToolStatus",
]
```

- [ ] **Step 2: Update `server.py` imports**

Replace:
```python
from multiclaw.tools.builtin import (
    EditFileToolBuilder,
    FindDirToolBuilder,
    GlobToolBuilder,
    GrepToolBuilder,
    ListDirToolBuilder,
    ReadFileToolBuilder,
    UndoEditToolBuilder,
    WriteFileToolBuilder,
)
```

With:
```python
from multiclaw.tools.edit_file import EditFileToolBuilder, UndoEditToolBuilder
from multiclaw.tools.find_dir import FindDirToolBuilder
from multiclaw.tools.glob import GlobToolBuilder
from multiclaw.tools.grep import GrepToolBuilder
from multiclaw.tools.list_dir import ListDirToolBuilder
from multiclaw.tools.read_file import ReadFileToolBuilder
from multiclaw.tools.write_file import WriteFileToolBuilder
```

- [ ] **Step 3: Update `tests/test_tools.py` imports**

Replace:
```python
from multiclaw.tools import builtin
```
and
```python
from multiclaw.tools.builtin import (
    EditFileToolBuilder,
    FindDirToolBuilder,
    GlobToolBuilder,
    GrepToolBuilder,
    ListDirToolBuilder,
    ReadFileToolBuilder,
    UndoEditToolBuilder,
    WriteFileToolBuilder,
)
```

With:
```python
from multiclaw.tools import _common
from multiclaw.tools.edit_file import EditFileToolBuilder, UndoEditToolBuilder
from multiclaw.tools.find_dir import FindDirToolBuilder
from multiclaw.tools.glob import GlobToolBuilder
from multiclaw.tools.grep import GrepToolBuilder
from multiclaw.tools.list_dir import ListDirToolBuilder
from multiclaw.tools.read_file import ReadFileToolBuilder
from multiclaw.tools.write_file import WriteFileToolBuilder
```

Also update monkeypatch references from `builtin.XXX` to `_common.XXX`:
- `monkeypatch.setattr(builtin.shutil, "which", ...)` → `monkeypatch.setattr(_common.shutil, "which", ...)`
- `monkeypatch.setattr(builtin, "_run_command", ...)` → `monkeypatch.setattr(_common, "_run_command", ...)`
- `monkeypatch.setattr(builtin, "MAX_FIND_DIR_RESULTS", 3)` → `monkeypatch.setattr(_common, "MAX_FIND_DIR_RESULTS", 3)`

- [ ] **Step 4: Delete `builtin.py`**

```bash
rm src/multiclaw/tools/builtin.py
```

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/test_tools.py -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/multiclaw/tools/__init__.py src/multiclaw/server.py tests/test_tools.py
git rm src/multiclaw/tools/builtin.py
git commit -m "refactor: wire tools package to per-file imports, delete builtin.py"
```

---

### Task 10: Implement `shell.py` (ShellTool)

**Files:**
- Create: `src/multiclaw/tools/shell.py`
- Create: `tests/test_shell.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for ShellTool."""
import asyncio
from pathlib import Path

import pytest

from multiclaw.tools.shell import ShellParams, ShellToolBuilder


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "subdir").mkdir()
    (tmp_path / "hello.txt").write_text("hello world\n", encoding="utf-8")
    return tmp_path


class TestShellTool:
    @pytest.mark.asyncio
    async def test_shell_executes_simple_command(self, workspace):
        builder = ShellToolBuilder(str(workspace), allowed_commands=["echo"])

        result = await builder.build(
            builder.validate({"command": "echo hello"})
        ).execute()

        assert result.status == "success"
        assert "hello" in result.content

    @pytest.mark.asyncio
    async def test_shell_rejects_empty_command(self, workspace):
        builder = ShellToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"command": ""})
        ).execute()

        assert result.status == "error"
        assert "cannot be empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shell_rejects_dangerous_command(self, workspace):
        builder = ShellToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"command": "rm -rf /"})
        ).execute()

        assert result.status == "error"
        assert "blocked" in result.content.lower() or "dangerous" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shell_respects_cwd(self, workspace):
        builder = ShellToolBuilder(str(workspace), allowed_commands=["ls"])

        result = await builder.build(
            builder.validate({"command": "ls", "cwd": str(workspace / "subdir")})
        ).execute()

        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_shell_times_out_long_command(self, workspace):
        builder = ShellToolBuilder(str(workspace), allowed_commands=["sleep"])
        import asyncio

        result = await builder.build(
            builder.validate({"command": "sleep 10", "timeout": 0.5})
        ).execute()

        assert "timed out" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shell_captures_stderr(self, workspace):
        builder = ShellToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"command": "python3 -c 'import sys; sys.stderr.write(\"err msg\")'"})
        ).execute()

        assert result.status == "success"
        assert "err msg" in result.content

    @pytest.mark.asyncio
    async def test_shell_exit_code_nonzero_is_not_error(self, workspace):
        builder = ShellToolBuilder(str(workspace), blocked_commands=[])

        result = await builder.build(
            builder.validate({"command": "python3 -c 'exit(1)'"})
        ).execute()

        assert result.status == "success"
        assert "exit code: 1" in result.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shell.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Create `shell.py`**

Adapt from `20260519-search-tools/src/shell_tool.py` to the ToolBuilder pattern:

```python
"""ShellTool — execute shell commands in the workspace."""

from __future__ import annotations

import asyncio
import os
import signal
import shlex
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools._common import (
    PathPolicy,
    WorkspaceToolBuilder,
    _error,
    _policy_for_invocation,
    _resolve_path,
    _success,
)
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 600.0
MAX_OUTPUT_CHARS = 30_000
TRUNCATION_MARKER = "\n... [output truncated: {removed} characters removed] ...\n"

DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=/dev/zero",
    ":(){ :|:& };:",
    "> /dev/sda",
    "chmod -R 777 /",
]


class ShellParams(BaseModel):
    command: str
    timeout: float | None = None
    cwd: str | None = None


class ShellInvocation(ToolInvocation[ShellParams]):
    def __init__(
        self,
        name: str,
        params: ShellParams,
        workspace_root: Path,
        policy: PathPolicy,
        allowed_commands: list[str] | None,
        blocked_commands: list[str] | None,
    ) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.policy = policy
        self.allowed_commands = allowed_commands
        self.blocked_commands = blocked_commands or []

    async def execute(self) -> ToolExecutionResult:
        if not self.params.command or not self.params.command.strip():
            return _error("Command cannot be empty")

        safety_err = self._check_safety(self.params.command)
        if safety_err:
            return _error(safety_err)

        work_dir = self.workspace_root
        if self.params.cwd:
            work_dir = _resolve_path(self.params.cwd, self.workspace_root)
            policy = _policy_for_invocation(self.policy, self)
            err = policy.validate_path(work_dir)
            if err:
                return _error(err)
            if not work_dir.is_dir():
                return _error(f"Not a directory: {work_dir}")

        effective_timeout = min(self.params.timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)
        if effective_timeout <= 0:
            return _error("Timeout must be positive")

        return await self._run(self.params.command, work_dir, effective_timeout)

    async def _run(self, command: str, cwd: Path, timeout: float) -> ToolExecutionResult:
        env = self._build_env()

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
                start_new_session=True,
            )
        except OSError as e:
            return _error(f"Failed to start process: {e}")

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            stdout_bytes, stderr_bytes = await self._kill_process(proc, timeout)

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        stdout = self._truncate_output(stdout)
        stderr = self._truncate_output(stderr)

        exit_code = proc.returncode if proc.returncode is not None else -1

        output_parts = []
        if timed_out:
            output_parts.append(f"[Command timed out after {timeout:.0f}s]")
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"[stderr]\n{stderr}")
        if not timed_out:
            output_parts.append(f"[exit code: {exit_code}]")

        output = "\n".join(output_parts)

        if exit_code != 0 and not timed_out:
            return ToolExecutionResult(
                status="success", content=output, data={"exit_code": exit_code}
            )

        return _success(output, data={"exit_code": exit_code})

    async def _kill_process(
        self, proc: asyncio.subprocess.Process, timeout: float
    ) -> tuple[bytes, bytes]:
        pgid = None
        try:
            pgid = os.getpgid(proc.pid)
        except (OSError, ProcessLookupError):
            pass

        try:
            if pgid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()
        except (OSError, ProcessLookupError):
            pass

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=2.0
            )
            return stdout or b"", stderr or b""
        except asyncio.TimeoutError:
            pass

        try:
            if pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=2.0
            )
            return stdout or b"", stderr or b""
        except asyncio.TimeoutError:
            return b"", b""

    def _truncate_output(self, text: str) -> str:
        if len(text) <= MAX_OUTPUT_CHARS:
            return text
        keep_each = MAX_OUTPUT_CHARS // 2
        removed = len(text) - MAX_OUTPUT_CHARS
        return (
            text[:keep_each]
            + TRUNCATION_MARKER.format(removed=removed)
            + text[-keep_each:]
        )

    def _check_safety(self, command: str) -> str | None:
        cmd_lower = command.lower().strip()

        for pattern in DANGEROUS_PATTERNS:
            if pattern in cmd_lower:
                return f"Blocked dangerous command pattern: {pattern}"

        if self.blocked_commands:
            try:
                first_token = shlex.split(command)[0]
            except ValueError:
                first_token = command.split()[0] if command.split() else ""
            if first_token in self.blocked_commands:
                return f"Command '{first_token}' is blocked by policy"

        return None

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        sensitive_keys = [
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
            "API_KEY",
            "SECRET_KEY",
            "PASSWORD",
        ]
        for key in list(env.keys()):
            for sensitive in sensitive_keys:
                if sensitive in key.upper():
                    del env[key]
                    break
        return env


class ShellToolBuilder(WorkspaceToolBuilder):
    name = "shell"
    description = "Execute a shell command in the workspace with timeout and safety checks."
    parameters_schema = ShellParams

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        policy: PathPolicy | None = None,
        allowed_commands: list[str] | None = None,
        blocked_commands: list[str] | None = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.allowed_commands = allowed_commands
        self.blocked_commands = blocked_commands or []

    def validate(self, params: dict) -> ShellParams:
        return ShellParams(**params)

    def build(self, params: ShellParams) -> ToolInvocation[ShellParams]:
        return ShellInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
            allowed_commands=self.allowed_commands,
            blocked_commands=self.blocked_commands,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shell.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/tools/shell.py tests/test_shell.py
git commit -m "feat: add ShellTool with async subprocess execution"
```

---

### Task 11: Implement `code_exec.py` (CodeExecTool)

**Files:**
- Create: `src/multiclaw/tools/code_exec.py`
- Create: `tests/test_code_exec.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for CodeExecTool."""

import pytest

from multiclaw.tools.code_exec import CodeExecParams, CodeExecToolBuilder


class TestCodeExecTool:
    @pytest.mark.asyncio
    async def test_code_exec_runs_simple_code(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path))

        result = await builder.build(
            builder.validate({"code": "x = 1 + 2\nprint(x)"})
        ).execute()

        assert result.status == "success"
        assert "3" in result.content

    @pytest.mark.asyncio
    async def test_code_exec_rejects_empty_code(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path))

        result = await builder.build(
            builder.validate({"code": ""})
        ).execute()

        assert result.status == "error"
        assert "cannot be empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_code_exec_captures_exception(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path))

        result = await builder.build(
            builder.validate({"code": "raise ValueError('bad')"})
        ).execute()

        assert result.status == "success"
        assert "ValueError" in result.content

    @pytest.mark.asyncio
    async def test_code_exec_blocks_subprocess_import(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path), restrict_builtins=True)

        result = await builder.build(
            builder.validate({"code": "import subprocess; print('ok')"})
        ).execute()

        assert result.status == "success"
        assert "ImportError" in result.content or "not allowed" in result.content

    @pytest.mark.asyncio
    async def test_code_exec_times_out(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path))

        result = await builder.build(
            builder.validate({"code": "while True: pass", "timeout": 1.0})
        ).execute()

        assert "timed out" in result.content.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_code_exec.py -v`
Expected: FAIL

- [ ] **Step 3: Create `code_exec.py`**

Adapt from `20260519-search-tools/src/code_exec_tool.py`:

```python
"""CodeExecTool — execute Python code in a sandboxed subprocess."""

from __future__ import annotations

import multiprocessing
import sys
import traceback
from io import StringIO
from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools._common import WorkspaceToolBuilder, _error, _success
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
MAX_OUTPUT_CHARS = 30_000

SAFE_BUILTINS = {
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes",
    "callable", "chr", "complex", "dict", "dir", "divmod", "enumerate",
    "filter", "float", "format", "frozenset", "getattr", "hasattr",
    "hash", "hex", "id", "int", "isinstance", "issubclass", "iter",
    "len", "list", "map", "max", "min", "next", "object", "oct",
    "ord", "pow", "print", "range", "repr", "reversed", "round",
    "set", "slice", "sorted", "str", "sum", "tuple", "type", "vars",
    "zip",
    "True", "False", "None",
    "Exception", "BaseException", "ValueError", "TypeError", "KeyError",
    "IndexError", "AttributeError", "ImportError", "RuntimeError",
    "StopIteration", "GeneratorExit", "SystemExit", "KeyboardInterrupt",
    "ArithmeticError", "ZeroDivisionError", "OverflowError",
    "FileNotFoundError", "IOError", "OSError", "PermissionError",
    "NotImplementedError", "RecursionError", "MemoryError",
    "NameError", "UnboundLocalError", "SyntaxError", "IndentationError",
    "UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError",
    "AssertionError", "EOFError", "LookupError",
}

BLOCKED_MODULES = {"subprocess", "shutil", "ctypes", "signal"}


def _execute_in_process(
    code: str,
    result_dict: dict,
    restrict_builtins: bool,
) -> None:
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    captured_stdout = StringIO()
    captured_stderr = StringIO()
    sys.stdout = captured_stdout
    sys.stderr = captured_stderr

    try:
        if restrict_builtins:
            import builtins
            safe_globals = {"__builtins__": {
                k: getattr(builtins, k)
                for k in SAFE_BUILTINS
                if hasattr(builtins, k)
            }}
            safe_globals["__builtins__"]["__import__"] = _restricted_import
        else:
            safe_globals = {"__builtins__": __builtins__}

        exec(code, safe_globals)
        result_dict["success"] = True
    except Exception:
        result_dict["success"] = False
        result_dict["error"] = traceback.format_exc()
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        result_dict["stdout"] = captured_stdout.getvalue()
        result_dict["stderr"] = captured_stderr.getvalue()


def _restricted_import(name, *args, **kwargs):
    if name in BLOCKED_MODULES:
        raise ImportError(f"Import of '{name}' is not allowed in sandbox mode")
    return __import__(name, *args, **kwargs)


class CodeExecParams(BaseModel):
    code: str
    timeout: float | None = Field(default=None, gt=0)


class CodeExecInvocation(ToolInvocation[CodeExecParams]):
    def __init__(
        self,
        name: str,
        params: CodeExecParams,
        workspace_root: Path | None,
        restrict_builtins: bool,
    ) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.restrict_builtins = restrict_builtins

    async def execute(self) -> ToolExecutionResult:
        if not self.params.code or not self.params.code.strip():
            return _error("Code cannot be empty")

        effective_timeout = min(self.params.timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)
        if effective_timeout <= 0:
            return _error("Timeout must be positive")

        manager = multiprocessing.Manager()
        result_dict = manager.dict()
        result_dict["success"] = False
        result_dict["stdout"] = ""
        result_dict["stderr"] = ""
        result_dict["error"] = ""

        proc = multiprocessing.Process(
            target=_execute_in_process,
            args=(self.params.code, result_dict, self.restrict_builtins),
        )
        proc.start()
        proc.join(timeout=effective_timeout)

        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=1.0)
            return _success(
                f"[Execution timed out after {effective_timeout:.0f}s]\n"
                + self._truncate(dict(result_dict).get("stdout", ""))
            )

        stdout = dict(result_dict).get("stdout", "")
        stderr = dict(result_dict).get("stderr", "")
        error = dict(result_dict).get("error", "")
        success = dict(result_dict).get("success", False)

        stdout = self._truncate(stdout)
        stderr = self._truncate(stderr)

        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        if error:
            parts.append(f"[error]\n{error}")
        if not parts:
            parts.append("[No output]")

        output = "\n".join(parts)

        if not success:
            return ToolExecutionResult(
                status="success", content=output, data={"success": False, "error": error}
            )
        return _success(output, data={"success": True})

    def _truncate(self, text: str) -> str:
        if len(text) <= MAX_OUTPUT_CHARS:
            return text
        keep_each = MAX_OUTPUT_CHARS // 2
        removed = len(text) - MAX_OUTPUT_CHARS
        return (
            text[:keep_each]
            + f"\n... [output truncated: {removed} characters removed] ...\n"
            + text[-keep_each:]
        )


class CodeExecToolBuilder(WorkspaceToolBuilder):
    name = "code_exec"
    description = "Execute Python code in a sandboxed subprocess with timeout control."
    parameters_schema = CodeExecParams

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        policy=None,
        restrict_builtins: bool = True,
    ) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.restrict_builtins = restrict_builtins

    def validate(self, params: dict) -> CodeExecParams:
        return CodeExecParams(**params)

    def build(self, params: CodeExecParams) -> ToolInvocation[CodeExecParams]:
        return CodeExecInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            restrict_builtins=self.restrict_builtins,
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_code_exec.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/tools/code_exec.py tests/test_code_exec.py
git commit -m "feat: add CodeExecTool with multiprocessing sandbox"
```

---

### Task 12: Implement `web_fetch.py` (WebFetchTool)

**Files:**
- Create: `src/multiclaw/tools/web_fetch.py`
- Create: `tests/test_web_fetch.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for WebFetchTool."""

import pytest

from multiclaw.tools.web_fetch import WebFetchParams, WebFetchToolBuilder


class TestWebFetchTool:
    @pytest.mark.asyncio
    async def test_web_fetch_rejects_empty_url(self):
        builder = WebFetchToolBuilder()

        result = await builder.build(
            builder.validate({"url": ""})
        ).execute()

        assert result.status == "error"
        assert "empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_web_fetch_normalizes_url(self):
        builder = WebFetchToolBuilder()

        result = await builder.build(
            builder.validate({"url": "example.com"})
        ).execute()

        # The fetch will likely fail (no network in test), but URL should be normalized
        assert "https://example.com" in result.content or "example.com" in result.content

    @pytest.mark.asyncio
    async def test_web_fetch_default_mode_is_auto(self):
        builder = WebFetchToolBuilder()
        assert builder.mode == "auto"

    @pytest.mark.asyncio
    async def test_web_fetch_respects_explicit_mode(self):
        builder = WebFetchToolBuilder(mode="light")

        result = await builder.build(
            builder.validate({"url": "example.com", "mode": "markdown"})
        ).execute()

        # Should use markdown mode
        assert result.data.get("mode", "") == "markdown" or "markdown" in result.content

    @pytest.mark.asyncio
    async def test_web_fetch_invalid_mode_rejected(self):
        builder = WebFetchToolBuilder()
        result = await builder.build(
            builder.validate({"url": "example.com", "mode": "invalid"})
        ).execute()
        assert result.status == "error" or "invalid" in result.content.lower() or "unknown" in result.content.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_fetch.py -v`
Expected: FAIL

- [ ] **Step 3: Create `web_fetch.py`**

Adapt from `20260520-web-fetch/src/web_fetch.py`:

```python
"""WebFetch tool — fetch web pages with automatic mode selection."""

from __future__ import annotations

import re
from enum import Enum
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from multiclaw.tools._common import WorkspaceToolBuilder, _error, _success
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation


class FetchMode(str, Enum):
    LIGHT = "light"
    MARKDOWN = "markdown"
    BROWSER = "browser"
    AUTO = "auto"


SPA_INDICATORS = {
    "react", "angular", "vue", "next", "nuxt", "svelte", "gatsby",
    "vercel", "netlify", "cloudflare-pages",
}

BROWSER_DOMAINS = {
    "twitter.com", "x.com", "instagram.com", "facebook.com",
    "linkedin.com", "reddit.com", "medium.com",
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}


class WebFetchParams(BaseModel):
    url: str
    mode: str = Field(default="auto")


class WebFetchInvocation(ToolInvocation[WebFetchParams]):
    def __init__(
        self,
        name: str,
        params: WebFetchParams,
        mode: FetchMode,
        timeout: float,
    ) -> None:
        super().__init__(name=name, params=params)
        self.default_mode = mode
        self.timeout = timeout

    async def execute(self) -> ToolExecutionResult:
        url = self.params.url
        if not url or not url.strip():
            return _error("URL is empty")

        url = self._normalize_url(url)

        try:
            mode = FetchMode(self.params.mode)
        except ValueError:
            return _error(f"Unknown fetch mode: '{self.params.mode}'. Valid: light, markdown, browser, auto")

        if mode == FetchMode.AUTO:
            return self._auto_fetch(url)
        elif mode == FetchMode.LIGHT:
            return self._light_fetch(url)
        elif mode == FetchMode.MARKDOWN:
            return self._markdown_fetch(url)
        elif mode == FetchMode.BROWSER:
            return self._browser_fetch(url)
        else:
            return _error(f"Unknown mode: {mode}")

    def _normalize_url(self, url: str) -> str:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url

    def _auto_fetch(self, url: str) -> ToolExecutionResult:
        if self._needs_browser(url):
            result = self._browser_fetch(url)
            if result.status == "success":
                return result

        result = self._light_fetch(url)

        if result.status == "error":
            result = self._markdown_fetch(url)
            if result.status == "error":
                browser_result = self._browser_fetch(url)
                return browser_result
            return result

        if self._content_looks_empty(result.content):
            browser_result = self._browser_fetch(url)
            if browser_result.status == "success" and len(browser_result.content) > len(result.content):
                return browser_result

        return result

    def _needs_browser(self, url: str) -> bool:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")
        if domain in BROWSER_DOMAINS:
            return True
        path = parsed.path.lower()
        if any(f"/{spa}" in path or f".{spa}" in path for spa in SPA_INDICATORS):
            return True
        if "#/" in url or "#!" in url:
            return True
        return False

    def _content_looks_empty(self, content: str) -> bool:
        if not content:
            return True
        text = content.strip()
        if len(text) < 200:
            return True
        words = text.split()
        if len(words) < 30:
            return True
        return False

    def _light_fetch(self, url: str) -> ToolExecutionResult:
        try:
            import httpx
        except ImportError:
            return _error("httpx not installed: pip install httpx")

        try:
            import trafilatura
        except ImportError:
            return _error("trafilatura not installed: pip install trafilatura")

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=DEFAULT_HEADERS) as client:
                resp = client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                html = resp.text[:5_000_000]
                text = trafilatura.extract(html, include_links=True, include_tables=True,
                                            include_comments=False, favor_recall=True)
                if not text:
                    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()[:50000]
                title = ""
                match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if match:
                    title = match.group(1).strip()[:200]

                content = f"Fetched: {url} (mode=light)\nTitle: {title}\n\n{text or ''}"
                return _success(content, data={"url": url, "mode": "light", "title": title})
        except Exception as e:
            return _error(f"Light fetch error: {e}")

    def _markdown_fetch(self, url: str) -> ToolExecutionResult:
        try:
            import httpx
        except ImportError:
            return _error("httpx not installed: pip install httpx")

        try:
            import html2text
        except ImportError:
            return _error("html2text not installed: pip install html2text")

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=DEFAULT_HEADERS) as client:
                resp = client.get(url)
                resp.raise_for_status()
                html = resp.text[:5_000_000]
                html = re.sub(r"<nav[^>]*>.*?</nav>", "", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<footer[^>]*>.*?</footer>", "", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

                converter = html2text.HTML2Text()
                converter.body_width = 0
                converter.ignore_images = True
                converter.protect_links = True
                converter.unicode_snob = True
                markdown = converter.handle(html).strip()

                title = ""
                match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if match:
                    title = match.group(1).strip()[:200]

                content = f"Fetched: {url} (mode=markdown)\nTitle: {title}\n\n{markdown}"
                return _success(content, data={"url": url, "mode": "markdown", "title": title})
        except Exception as e:
            return _error(f"Markdown fetch error: {e}")

    def _browser_fetch(self, url: str) -> ToolExecutionResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return _error("playwright not installed: pip install playwright && playwright install chromium")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=DEFAULT_HEADERS["User-Agent"])
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
                title = page.title()
                html = page.content()
                browser.close()

                try:
                    import html2text
                    converter = html2text.HTML2Text()
                    converter.body_width = 0
                    converter.ignore_images = True
                    converter.unicode_snob = True
                    text = converter.handle(html).strip()
                except ImportError:
                    text = re.sub(r"<[^>]+>", " ", html)
                    text = re.sub(r"\s+", " ", text).strip()

                content = f"Fetched: {url} (mode=browser)\nTitle: {title}\n\n{text}"
                return _success(content, data={"url": url, "mode": "browser", "title": title})
        except Exception as e:
            return _error(f"Browser fetch error: {e}")


class WebFetchToolBuilder(WorkspaceToolBuilder):
    name = "web_fetch"
    description = "Fetch a web page and extract content. Modes: light, markdown, browser, auto."
    parameters_schema = WebFetchParams

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        policy=None,
        mode: str = "auto",
        timeout: float = 30.0,
    ) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.mode = mode
        self.timeout = timeout

    def validate(self, params: dict) -> WebFetchParams:
        return WebFetchParams(**params)

    def build(self, params: WebFetchParams) -> ToolInvocation[WebFetchParams]:
        return WebFetchInvocation(
            name=self.name,
            params=params,
            mode=FetchMode(self.mode),
            timeout=self.timeout,
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_web_fetch.py -v`
Expected: PASS (will make real HTTP calls in tests; auto mode test may vary)

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/tools/web_fetch.py tests/test_web_fetch.py
git commit -m "feat: add WebFetchTool with light/markdown/browser/auto modes"
```

---

### Task 13: Implement `web_search.py` (WebSearchTool)

**Files:**
- Create: `src/multiclaw/tools/web_search.py`
- Create: `tests/test_web_search.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for WebSearchTool."""

import pytest

from multiclaw.tools.web_search import WebSearchParams, WebSearchToolBuilder


class TestWebSearchTool:
    @pytest.mark.asyncio
    async def test_web_search_rejects_empty_query(self):
        builder = WebSearchToolBuilder()

        result = await builder.build(
            builder.validate({"query": ""})
        ).execute()

        assert result.status == "error"
        assert "empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_web_search_returns_error_for_unknown_engine(self):
        builder = WebSearchToolBuilder()

        result = await builder.build(
            builder.validate({"query": "test", "engine": "nonexistent"})
        ).execute()

        assert result.status == "error"
        assert "unknown engine" in result.content.lower() or "all engines failed" in result.content.lower()

    @pytest.mark.asyncio
    async def test_web_search_default_engine_is_duckduckgo(self):
        builder = WebSearchToolBuilder()
        assert builder.engine == "duckduckgo"

    @pytest.mark.asyncio
    async def test_web_search_accepts_all_known_engines(self):
        for engine in ["duckduckgo", "bing", "baidu"]:
            builder = WebSearchToolBuilder(engine=engine)
            assert builder.engine == engine
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_web_search.py -v`
Expected: FAIL

- [ ] **Step 3: Create `web_search.py`**

Adapt from `20260520-web-search/src/web_search.py`:

```python
"""WebSearch tool — unified web search with engine fallback."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from multiclaw.tools._common import WorkspaceToolBuilder, _error, _success
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation

DEFAULT_ENGINE = "duckduckgo"
DEFAULT_MAX_RESULTS = 5


class WebSearchParams(BaseModel):
    query: str
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=20)
    engine: str | None = None


class WebSearchInvocation(ToolInvocation[WebSearchParams]):
    def __init__(
        self,
        name: str,
        params: WebSearchParams,
        default_engine: str,
        fallback_engines: list[str],
        lang: str,
        region: str,
    ) -> None:
        super().__init__(name=name, params=params)
        self.default_engine = default_engine
        self.fallback_engines = fallback_engines
        self.lang = lang
        self.region = region
        self._instances: dict[str, object] = {}

    async def execute(self) -> ToolExecutionResult:
        query = self.params.query
        if not query or not query.strip():
            return _error("Query cannot be empty")

        target_engine = self.params.engine or self.default_engine
        order = [target_engine] + [
            e for e in self.fallback_engines if e != target_engine
        ]

        for eng_name in order:
            instance = self._get_engine(eng_name)
            if instance is None:
                if eng_name == target_engine:
                    return _error(f"Unknown engine: {eng_name}")
                continue
            try:
                resp = instance.search(query, max_results=self.params.max_results)
                if not resp.is_error and resp.results:
                    lines = [f"Search results for '{query}' ({resp.engine}):"]
                    for r in resp.results:
                        lines.append(f"\n{r.position}. {r.title}")
                        lines.append(f"   {r.url}")
                        if r.snippet:
                            lines.append(f"   {r.snippet[:200]}")
                    return _success(
                        "\n".join(lines),
                        data={
                            "query": query,
                            "engine": resp.engine,
                            "results": [
                                {"title": r.title, "url": r.url, "snippet": r.snippet}
                                for r in resp.results
                            ],
                        },
                    )
            except Exception as e:
                continue

        return _error(f"All engines failed for query: '{query}'")

    def _get_engine(self, name: str):
        if name in self._instances:
            return self._instances[name]

        if name == "duckduckgo":
            inst = _DuckDuckGoEngine(region=self.region)
        elif name == "bing":
            inst = _BingEngine(lang=self.lang)
        elif name == "baidu":
            inst = _BaiduEngine()
        else:
            return None

        self._instances[name] = inst
        return inst


class _SearchResult:
    def __init__(self, title: str, url: str, snippet: str = "", source: str = "", position: int = 0):
        self.title = title
        self.url = url
        self.snippet = snippet
        self.source = source
        self.position = position


class _SearchResponse:
    def __init__(self, query: str, results: list[_SearchResult], engine: str,
                 error: str = "", is_error: bool = False):
        self.query = query
        self.results = results
        self.engine = engine
        self.error = error
        self.is_error = is_error


class _DuckDuckGoEngine:
    def __init__(self, region: str = "wt-wt", safesearch: str = "moderate"):
        self.region = region
        self.safesearch = safesearch

    def search(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> _SearchResponse:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return _SearchResponse(query, [], "duckduckgo",
                                   error="duckduckgo-search package not installed. Run: pip install duckduckgo-search",
                                   is_error=True)

        try:
            results = []
            with DDGS() as ddgs:
                raw = ddgs.text(query, max_results=max_results, region=self.region, safesearch=self.safesearch)
                for i, item in enumerate(raw):
                    if isinstance(item, dict):
                        results.append(_SearchResult(
                            title=item.get("title", ""),
                            url=item.get("href", item.get("url", "")),
                            snippet=item.get("body", item.get("description", "")),
                            source="duckduckgo",
                            position=i + 1,
                        ))
            return _SearchResponse(query, results, "duckduckgo")
        except Exception as e:
            return _SearchResponse(query, [], "duckduckgo", error=f"DuckDuckGo search error: {e}", is_error=True)


class _BingEngine:
    def __init__(self, lang: str = "en"):
        self.lang = lang

    def search(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> _SearchResponse:
        try:
            import httpx
        except ImportError:
            return _SearchResponse(query, [], "bing", error="httpx not installed: pip install httpx", is_error=True)

        try:
            url = "https://www.bing.com/search"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": self.lang,
            }
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                resp = client.get(url, params={"q": query, "count": max_results}, headers=headers)
                resp.raise_for_status()

                import re
                results = []
                snippet_pattern = re.compile(r'<li class="b_algo".*?<h2><a[^>]*href="([^"]*)"[^>]*>(.*?)</a></h2>.*?<p[^>]*>(.*?)</p>', re.DOTALL)
                matches = snippet_pattern.findall(resp.text)
                for i, (url, title, snippet) in enumerate(matches[:max_results]):
                    title = re.sub(r"<[^>]+>", "", title).strip()
                    snippet = re.sub(r"<[^>]+>", "", snippet).strip()
                    results.append(_SearchResult(title=title, url=url, snippet=snippet, source="bing", position=i + 1))

                return _SearchResponse(query, results, "bing")
        except Exception as e:
            return _SearchResponse(query, [], "bing", error=f"Bing search error: {e}", is_error=True)


class _BaiduEngine:
    def search(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> _SearchResponse:
        try:
            import httpx
        except ImportError:
            return _SearchResponse(query, [], "baidu", error="httpx not installed: pip install httpx", is_error=True)

        try:
            url = "https://www.baidu.com/s"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            }
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                resp = client.get(url, params={"wd": query, "rn": max_results}, headers=headers)
                resp.raise_for_status()

                import re
                results = []
                snippet_pattern = re.compile(r'<div[^>]*class="[^"]*result[^"]*".*?<h3[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?<span[^>]*class="[^"]*content-right_[^"]*"[^>]*>(.*?)</span>', re.DOTALL)
                matches = snippet_pattern.findall(resp.text)
                for i, (url, title, snippet) in enumerate(matches[:max_results]):
                    title = re.sub(r"<[^>]+>", "", title).strip()
                    snippet = re.sub(r"<[^>]+>", "", snippet).strip()
                    results.append(_SearchResult(title=title, url=url, snippet=snippet, source="baidu", position=i + 1))

                return _SearchResponse(query, results, "baidu")
        except Exception as e:
            return _SearchResponse(query, [], "baidu", error=f"Baidu search error: {e}", is_error=True)


class WebSearchToolBuilder(WorkspaceToolBuilder):
    name = "web_search"
    description = "Search the web with engine fallback. Engines: duckduckgo, bing, baidu."
    parameters_schema = WebSearchParams

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        policy=None,
        engine: str = DEFAULT_ENGINE,
        fallback_engines: list[str] | None = None,
        lang: str = "en",
        region: str = "wt-wt",
    ) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.engine = engine
        self.fallback_engines = fallback_engines or [
            e for e in ["duckduckgo", "bing", "baidu"] if e != engine
        ]
        self.lang = lang
        self.region = region

    def validate(self, params: dict) -> WebSearchParams:
        return WebSearchParams(**params)

    def build(self, params: WebSearchParams) -> ToolInvocation[WebSearchParams]:
        return WebSearchInvocation(
            name=self.name,
            params=params,
            default_engine=self.engine,
            fallback_engines=self.fallback_engines,
            lang=self.lang,
            region=self.region,
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_web_search.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/multiclaw/tools/web_search.py tests/test_web_search.py
git commit -m "feat: add WebSearchTool with DuckDuckGo/Bing/Baidu engine fallback"
```

---

### Task 14: Final integration — register new tools in server.py

**Files:**
- Modify: `src/multiclaw/server.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Register new tools in `server.py`**

Add imports:
```python
from multiclaw.tools.shell import ShellToolBuilder
from multiclaw.tools.code_exec import CodeExecToolBuilder
from multiclaw.tools.web_fetch import WebFetchToolBuilder
from multiclaw.tools.web_search import WebSearchToolBuilder
```

Add registrations after the existing ones in `create_agent()`:
```python
registry.register(ShellToolBuilder(workspace_root))
registry.register(CodeExecToolBuilder(workspace_root))
registry.register(WebFetchToolBuilder(workspace_root))
registry.register(WebSearchToolBuilder(workspace_root))
```

Also add new tool names to guarded_tools:
```python
"shell",
"code_exec",
"web_fetch",
"web_search",
```

- [ ] **Step 2: Update the registry test to match expected tool list**

In `tests/test_tools.py`, update `test_runtime_registry_matches_agent_code_tool_set`:
```python
assert [tool.name for tool in agent.registry.list_all()] == [
    "code_exec",
    "edit_file",
    "find_dir",
    "glob",
    "grep",
    "list_dir",
    "read_file",
    "shell",
    "undo_edit",
    "web_fetch",
    "web_search",
    "write_file",
]
```

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/multiclaw/server.py tests/test_tools.py
git commit -m "feat: register new shell/code_exec/web_fetch/web_search tools"
```

---

### Task 15: Final verification

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: all tests PASS

- [ ] **Step 2: Verify import cleanliness**

Run: `python -c "from multiclaw.tools import CoreToolScheduler, ToolRegistry, ToolBuilder, ToolInvocation, ToolExecutionResult, ToolStatus; print('imports OK')"`
Expected: imports OK (no errors)

- [ ] **Step 3: Verify all tool files are importable**

Run:
```bash
python -c "
from multiclaw.tools.read_file import ReadFileToolBuilder
from multiclaw.tools.write_file import WriteFileToolBuilder
from multiclaw.tools.edit_file import EditFileToolBuilder, UndoEditToolBuilder
from multiclaw.tools.glob import GlobToolBuilder
from multiclaw.tools.list_dir import ListDirToolBuilder
from multiclaw.tools.grep import GrepToolBuilder
from multiclaw.tools.find_dir import FindDirToolBuilder
from multiclaw.tools.shell import ShellToolBuilder
from multiclaw.tools.code_exec import CodeExecToolBuilder
from multiclaw.tools.web_fetch import WebFetchToolBuilder
from multiclaw.tools.web_search import WebSearchToolBuilder
print('all tool imports OK')
"
```
Expected: all tool imports OK

- [ ] **Step 4: Check git status is clean**

Run: `git status`
Expected: all changes committed
