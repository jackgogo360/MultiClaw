"""CodeExecTool — execute Python code in a sandboxed interpreter."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from multiclaw.tools import _code_runner
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
from multiclaw.workflow.models import RecoveryStrategy

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
MAX_OUTPUT_CHARS = 30_000
TRUNCATION_MARKER = "\n... [output truncated: {removed} characters removed] ...\n"
OUTPUT_LIMIT_ERROR = "Execution exceeded output limit on {stream}"


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
            return self._with_request_audit(_error("sandbox profile unavailable"))
        except SandboxConfigurationError:
            return self._with_request_audit(_error("sandbox configuration unavailable"))
        except SandboxPolicyError:
            return self._with_request_audit(_error("sandbox policy blocked execution"))
        except SandboxLaunchError:
            return self._with_request_audit(_error("sandbox failed to launch command"))
        except Exception:
            return self._with_request_audit(_error("sandbox execution failed"))

        if exec_result.completion_state == "output_limit_exceeded":
            error = OUTPUT_LIMIT_ERROR.format(stream=exec_result.output_limit_stream)
            return self._with_audit(
                _success(
                    f"[error]\n{error}",
                    data={"success": False, "error": error},
                ),
                exec_result,
            )

        if exec_result.timed_out:
            return self._with_audit(
                _success(f"[Execution timed out after {timeout:.0f}s]"),
                exec_result,
            )

        stdout = self._truncate(
            exec_result.stdout.decode("utf-8", errors="replace")
            if exec_result.stdout
            else ""
        )
        stderr = self._truncate(
            exec_result.stderr.decode("utf-8", errors="replace")
            if exec_result.stderr
            else ""
        )

        if exec_result.exit_code == 0:
            parts = []
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(f"[stderr]\n{stderr}")
            if not parts:
                parts.append("[No output]")
            return self._with_audit(
                _success("\n".join(parts), data={"success": True}),
                exec_result,
            )

        error = self._failure_error_text(exec_result, stderr)

        parts = []
        if stdout:
            parts.append(stdout)
        if error:
            parts.append(f"[error]\n{error}")
        if not parts:
            parts.append("[No output]")

        return self._with_audit(
            _success(
                "\n".join(parts),
                data={"success": False, "error": error},
            ),
            exec_result,
        )

    def _build_argv(self) -> tuple[str, ...]:
        return (
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-c",
            _code_runner.build_bootstrap(self.restrict_builtins),
        )

    def _failure_error_text(self, exec_result, stderr: str) -> str:
        if stderr:
            return stderr
        if exec_result.exit_code is not None:
            return f"Python exited with code {exec_result.exit_code}"
        if exec_result.signal:
            return f"Python exited due to {exec_result.signal}"
        return "Python exited unexpectedly"

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

    def _with_request_audit(self, result: ToolExecutionResult) -> ToolExecutionResult:
        result.audit.update(
            {
                "sandbox_backend": self.sandbox_controller.backend_name,
                "sandbox_profile": self.profile_name,
                "unsafe_fallback_used": self.sandbox_controller.mode
                == "host_unsafe_dev_only",
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
    recovery_strategy = RecoveryStrategy.MANUAL_UNCERTAIN

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
