"""ShellTool — execute shell commands in the workspace."""

from __future__ import annotations

import asyncio
import os
import signal
import shlex
from pathlib import Path

from pydantic import BaseModel

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
        return _success(output, data={"exit_code": exit_code})

    async def _kill_process(self, proc, timeout: float) -> tuple[bytes, bytes]:
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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
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
            "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "API_KEY", "SECRET_KEY", "PASSWORD",
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
