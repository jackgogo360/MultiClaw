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
    read_only = True

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
