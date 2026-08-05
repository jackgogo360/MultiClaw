"""CodeExecTool — execute Python code in a sandboxed interpreter."""

from __future__ import annotations

import json
import sys
import uuid
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from multiclaw.governance import (
    SandboxConfigurationError,
    SandboxController,
    SandboxExecRequest,
    SandboxLaunchError,
    SandboxPolicyError,
    SandboxUnavailableError,
)
from multiclaw.tools._common import WorkspaceToolBuilder, _error, _success
from multiclaw.tools.base import ToolExecutionResult, ToolInvocation

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
MAX_OUTPUT_CHARS = 30_000
MAX_ENVELOPE_BYTES = 1_048_576
TRUNCATION_MARKER = "\n... [output truncated: {removed} characters removed] ...\n"
RUNNER_MODULE = "multiclaw.tools._code_runner"
RUNNER_FLAG = "--restrict-builtins"
RUNNER_KEYS = ("success", "stdout", "stderr", "error")


class CodeExecParams(BaseModel):
    code: str
    timeout: float | None = Field(default=None, gt=0)


class CodeExecInvocation(ToolInvocation[CodeExecParams]):
    def __init__(
        self,
        name: str,
        params: CodeExecParams,
        workspace_root: Path,
        sandbox_controller: SandboxController,
        profile_name: str,
        restrict_builtins: bool,
    ) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.sandbox_controller = sandbox_controller
        self.profile_name = profile_name
        self.restrict_builtins = restrict_builtins

    async def execute(self) -> ToolExecutionResult:
        if not self.params.code or not self.params.code.strip():
            return _error("Code cannot be empty")

        effective_timeout = min(self.params.timeout or DEFAULT_TIMEOUT, MAX_TIMEOUT)
        if effective_timeout <= 0:
            return _error("Timeout must be positive")

        request = SandboxExecRequest(
            tool_name=self.name,
            profile_name=self.profile_name,
            mode="exec_argv",
            argv=self._build_argv(),
            workspace_root=self.workspace_root.resolve(),
            cwd=self.workspace_root.resolve(),
            stdin_bytes=self.params.code.encode("utf-8"),
            timeout_seconds=effective_timeout,
            correlation_id=uuid.uuid4().hex,
        )
        return await self._run(request, effective_timeout)

    async def _run(
        self,
        request: SandboxExecRequest,
        timeout: float,
    ) -> ToolExecutionResult:
        try:
            exec_result = await self.sandbox_controller.run(request)
        except SandboxUnavailableError:
            return _error("sandbox profile unavailable")
        except SandboxConfigurationError:
            return _error("sandbox configuration unavailable")
        except SandboxPolicyError:
            return _error("sandbox policy blocked execution")
        except SandboxLaunchError:
            return _error("sandbox failed to launch command")
        except Exception:
            return _error("sandbox execution failed")

        if exec_result.timed_out:
            return self._with_audit(
                _success(f"[Execution timed out after {timeout:.0f}s]"),
                exec_result,
            )

        if exec_result.exit_code != 0 or exec_result.stderr:
            return self._with_audit(_error("sandbox execution failed"), exec_result)

        try:
            payload = self._parse_envelope(exec_result.stdout)
        except ValueError:
            return self._with_audit(_error("sandbox execution failed"), exec_result)

        stdout = self._truncate(payload["stdout"])
        stderr = self._truncate(payload["stderr"])
        error = self._truncate(payload["error"])

        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        if error:
            parts.append(f"[error]\n{error}")
        if not parts:
            parts.append("[No output]")

        if not payload["success"]:
            return self._with_audit(
                _success(
                    "\n".join(parts),
                    data={"success": False, "error": error},
                ),
                exec_result,
            )

        return self._with_audit(_success("\n".join(parts), data={"success": True}), exec_result)

    def _build_argv(self) -> tuple[str, ...]:
        argv = [
            str(Path(sys.executable).resolve()),
            "-m",
            RUNNER_MODULE,
        ]
        if self.restrict_builtins:
            argv.append(RUNNER_FLAG)
        return tuple(argv)

    def _parse_envelope(self, raw_stdout: bytes) -> dict[str, bool | str]:
        if len(raw_stdout) > MAX_ENVELOPE_BYTES:
            raise ValueError("runner output too large")

        try:
            envelope_text = raw_stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("runner output must be utf-8") from exc

        decoder = json.JSONDecoder(object_pairs_hook=self._reject_duplicate_keys)
        try:
            payload, end_index = decoder.raw_decode(envelope_text)
        except JSONDecodeError as exc:
            raise ValueError("runner output was not valid json") from exc

        if end_index != len(envelope_text):
            raise ValueError("runner output contained extra bytes")
        if not isinstance(payload, dict):
            raise ValueError("runner payload must be an object")
        if tuple(payload.keys()) != RUNNER_KEYS:
            raise ValueError("runner payload keys did not match protocol")
        if not isinstance(payload["success"], bool):
            raise ValueError("runner success must be a bool")

        for key in ("stdout", "stderr", "error"):
            if not isinstance(payload[key], str):
                raise ValueError(f"runner {key} must be a string")
        return payload

    def _reject_duplicate_keys(
        self,
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError("runner payload contained duplicate keys")
            payload[key] = value
        return payload

    def _with_audit(
        self,
        result: ToolExecutionResult,
        exec_result,
    ) -> ToolExecutionResult:
        result.audit.update(
            {
                "sandbox_backend": exec_result.backend_name,
                "sandbox_profile": exec_result.profile_name,
                "unsafe_fallback_used": exec_result.unsafe_fallback_used,
            }
        )
        return result

    def _truncate(self, text: str) -> str:
        if len(text) <= MAX_OUTPUT_CHARS:
            return text
        keep_each = MAX_OUTPUT_CHARS // 2
        removed = len(text) - MAX_OUTPUT_CHARS
        return (
            text[:keep_each]
            + TRUNCATION_MARKER.format(removed=removed)
            + text[-keep_each:]
        )


class CodeExecToolBuilder(WorkspaceToolBuilder):
    name = "code_exec"
    description = "Execute Python code in a sandboxed interpreter with timeout control."
    parameters_schema = CodeExecParams

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        sandbox_controller: SandboxController | None = None,
        profile_name: str = "code_exec_python",
        policy=None,
        restrict_builtins: bool = True,
    ) -> None:
        if sandbox_controller is None:
            raise ValueError("sandbox_controller is required")
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.sandbox_controller = sandbox_controller
        self.profile_name = profile_name
        self.restrict_builtins = restrict_builtins

    def validate(self, params: dict) -> CodeExecParams:
        return CodeExecParams(**params)

    def approval_description(self, params: dict[str, Any]) -> str:
        code = params.get("code", "?")
        return f"Run Python: {code[:80]}{'...' if len(code) > 80 else ''}"

    def build(self, params: CodeExecParams) -> ToolInvocation[CodeExecParams]:
        return CodeExecInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            sandbox_controller=self.sandbox_controller,
            profile_name=self.profile_name,
            restrict_builtins=self.restrict_builtins,
        )
