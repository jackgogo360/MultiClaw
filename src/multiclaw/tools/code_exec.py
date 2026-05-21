"""CodeExecTool — execute Python code in a sandboxed subprocess."""

from __future__ import annotations

import multiprocessing
import sys
import traceback
from io import StringIO
from typing import Any
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


def _execute_in_process(code: str, result_dict: dict, restrict_builtins: bool) -> None:
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
                k: getattr(builtins, k) for k in SAFE_BUILTINS if hasattr(builtins, k)
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
    def __init__(self, name: str, params: CodeExecParams,
                 workspace_root: Path | None, restrict_builtins: bool) -> None:
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
        success_flag = dict(result_dict).get("success", False)

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
        if not success_flag:
            return ToolExecutionResult(status="success", content=output,
                                       data={"success": False, "error": error})
        return _success(output, data={"success": True})

    def _truncate(self, text: str) -> str:
        if len(text) <= MAX_OUTPUT_CHARS:
            return text
        keep_each = MAX_OUTPUT_CHARS // 2
        removed = len(text) - MAX_OUTPUT_CHARS
        return (text[:keep_each]
                + f"\n... [output truncated: {removed} characters removed] ...\n"
                + text[-keep_each:])


class CodeExecToolBuilder(WorkspaceToolBuilder):
    name = "code_exec"
    description = "Execute Python code in a sandboxed subprocess with timeout control."
    parameters_schema = CodeExecParams

    def __init__(self, workspace_root: str | Path | None = None, policy=None,
                 restrict_builtins: bool = True) -> None:
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.restrict_builtins = restrict_builtins

    def validate(self, params: dict) -> CodeExecParams:
        return CodeExecParams(**params)

    def approval_description(self, params: dict[str, Any]) -> str:
        code = params.get("code", "?")
        return f"Run Python: {code[:80]}{'...' if len(code) > 80 else ''}"

    def build(self, params: CodeExecParams) -> ToolInvocation[CodeExecParams]:
        return CodeExecInvocation(name=self.name, params=params,
                                  workspace_root=self.workspace_root,
                                  restrict_builtins=self.restrict_builtins)
