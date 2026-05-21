"""WriteFile tool — write full file content with atomic writes and read-before-write safety."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

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
from multiclaw.tools.read_file import ReadFileToolBuilder


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

    def approval_description(self, params: dict[str, Any]) -> str:
        return f"Write file: {params.get('file_path', '?')}"

    def build(self, params: WriteFileParams) -> ToolInvocation[WriteFileParams]:
        return WriteFileInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            policy=self.policy,
            read_builder=self.read_builder,
            require_read_before_write=self.require_read_before_write,
        )
