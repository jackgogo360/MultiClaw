from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from multiclaw.events import Event
from multiclaw.governance.sandbox.errors import SandboxLaunchError
from multiclaw.governance.sandbox.models import (
    SandboxEnvironment,
    SandboxExecRequest,
    SandboxExecResult,
    SandboxProbeResult,
    SandboxProfilePolicy,
    SandboxReadiness,
    SandboxedLaunchSpec,
)


@runtime_checkable
class SandboxController(Protocol):
    @property
    def mode(self) -> Literal["auto", "host_unsafe_dev_only"]: ...

    @property
    def backend_name(self) -> str: ...

    @property
    def readiness(self) -> SandboxReadiness: ...

    def initialize(self) -> None: ...

    def is_profile_ready(self, profile_name: str) -> bool: ...

    def build_launch_spec(self, request: SandboxExecRequest) -> SandboxedLaunchSpec: ...

    async def run(self, request: SandboxExecRequest) -> SandboxExecResult: ...

    def record_blocked_capability(self, name: str, reason: str) -> None: ...

    def record_unsafe_capability(self, name: str, reason: str) -> None: ...

    def finalize_readiness(self) -> SandboxReadiness: ...

    def drain_startup_events(self) -> tuple[Event, ...]: ...

    def close(self) -> None: ...


@runtime_checkable
class SandboxBackend(Protocol):
    name: str

    def probe(
        self,
        workspace_root: Path,
        policies: tuple[SandboxProfilePolicy, ...],
    ) -> SandboxProbeResult: ...

    def build_launch_spec(
        self,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
        environment: SandboxEnvironment,
    ) -> SandboxedLaunchSpec: ...


class HostUnsafeBackend:
    name = "host_unsafe"

    def probe(
        self,
        workspace_root: Path,
        policies: tuple[SandboxProfilePolicy, ...],
    ) -> SandboxProbeResult:
        del workspace_root, policies
        return SandboxProbeResult(
            backend_name=self.name,
            available=True,
            capabilities={
                "filesystem_isolation": False,
                "network_isolation": False,
                "process_isolation": False,
            },
            reason="development-only host execution without isolation",
        )

    def build_launch_spec(
        self,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
        environment: SandboxEnvironment,
    ) -> SandboxedLaunchSpec:
        del policy
        if request.mode == "shell_string":
            if request.command is None or request.argv is not None:
                raise SandboxLaunchError("command is required for shell_string mode")
            executable = "/bin/sh"
            args = ("-c", request.command)
        else:
            if request.command is not None or not request.argv:
                raise SandboxLaunchError("argv must include executable for exec_argv mode")
            executable = request.argv[0]
            args = request.argv[1:]

        return SandboxedLaunchSpec(
            executable=executable,
            args=args,
            cwd=request.cwd,
            env=environment.env,
            stdin_bytes=request.stdin_bytes,
            private_root=environment.private_root,
            backend_name=self.name,
            profile_name=request.profile_name,
            correlation_id=request.correlation_id,
            unsafe_fallback_used=True,
        )
