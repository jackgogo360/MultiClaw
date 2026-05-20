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
