"""ShellTool — execute shell commands in the workspace."""

from __future__ import annotations

import shlex
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from multiclaw.governance import (
    SandboxConfigurationError,
    SandboxController,
    SandboxExecRequest,
    SandboxLaunchError,
    SandboxPolicyError,
    SandboxUnavailableError,
)
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
        sandbox_controller: SandboxController,
        profile_name: str,
        policy: PathPolicy,
        allowed_commands: list[str] | None,
        blocked_commands: list[str] | None,
    ) -> None:
        super().__init__(name=name, params=params)
        self.workspace_root = workspace_root
        self.sandbox_controller = sandbox_controller
        self.profile_name = profile_name
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

        request = SandboxExecRequest(
            tool_name=self.name,
            profile_name=self.profile_name,
            mode="shell_string",
            command=self.params.command,
            workspace_root=self.workspace_root.resolve(),
            cwd=work_dir.resolve(),
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

        stdout = (
            exec_result.stdout.decode("utf-8", errors="replace")
            if exec_result.stdout
            else ""
        )
        stderr = (
            exec_result.stderr.decode("utf-8", errors="replace")
            if exec_result.stderr
            else ""
        )
        stdout = self._truncate_output(stdout)
        stderr = self._truncate_output(stderr)
        exit_code = (
            exec_result.exit_code if exec_result.exit_code is not None else -1
        )

        output_parts = []
        if exec_result.timed_out:
            output_parts.append(f"[Command timed out after {timeout:.0f}s]")
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"[stderr]\n{stderr}")
        if not exec_result.timed_out:
            output_parts.append(f"[exit code: {exit_code}]")

        output = "\n".join(output_parts)
        # Compatibility: timed out shell runs stay "success" so callers keep
        # using content/data rather than treating timeouts as scheduler errors.
        result = _success(output, data={"exit_code": exit_code})
        result.audit.update(
            {
                "sandbox_backend": exec_result.backend_name,
                "sandbox_profile": exec_result.profile_name,
                "unsafe_fallback_used": exec_result.unsafe_fallback_used,
            }
        )
        return result

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
        try:
            first_token = shlex.split(command)[0]
        except ValueError:
            first_token = command.split()[0] if command.split() else ""
        if self.blocked_commands:
            if first_token in self.blocked_commands:
                return f"Command '{first_token}' is blocked by policy"
        if self.allowed_commands:
            if first_token not in self.allowed_commands:
                return f"Command '{first_token}' is not allowed by policy"
        return None

class ShellToolBuilder(WorkspaceToolBuilder):
    name = "shell"
    description = "Execute a shell command in the workspace with timeout and safety checks."
    parameters_schema = ShellParams

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        sandbox_controller: SandboxController | None = None,
        profile_name: str = "shell_workspace",
        policy: PathPolicy | None = None,
        allowed_commands: list[str] | None = None,
        blocked_commands: list[str] | None = None,
    ) -> None:
        if sandbox_controller is None:
            raise ValueError("sandbox_controller is required")
        super().__init__(workspace_root=workspace_root, policy=policy)
        self.sandbox_controller = sandbox_controller
        self.profile_name = profile_name
        self.allowed_commands = allowed_commands
        self.blocked_commands = blocked_commands or []

    def validate(self, params: dict) -> ShellParams:
        return ShellParams(**params)

    def approval_description(self, params: dict[str, Any]) -> str:
        cmd = params.get("command", "?")
        return f"Shell: {cmd[:80]}{'...' if len(cmd) > 80 else ''}"

    def build(self, params: ShellParams) -> ToolInvocation[ShellParams]:
        return ShellInvocation(
            name=self.name,
            params=params,
            workspace_root=self.workspace_root,
            sandbox_controller=self.sandbox_controller,
            profile_name=self.profile_name,
            policy=self.policy,
            allowed_commands=self.allowed_commands,
            blocked_commands=self.blocked_commands,
        )
