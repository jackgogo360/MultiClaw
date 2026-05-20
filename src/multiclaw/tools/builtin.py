import os
import re
import shutil
import tempfile
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
        read_builder: ReadFileToolBuilder | None,
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
        read_builder: ReadFileToolBuilder | None = None,
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
