from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from multiclaw.events import Event
from multiclaw.governance import (
    SandboxExecRequest,
    SandboxExecResult,
    SandboxProbeResult,
    SandboxProcessRunner,
    SandboxReadiness,
    SandboxUnavailableError,
    SandboxedLaunchSpec,
    build_sandbox_environment,
)


_ALL_PROFILES = {
    "shell_workspace": True,
    "code_exec_python": True,
    "mcp_stdio_local": True,
}


class ReadyRecordingSandboxController:
    def __init__(
        self,
        *,
        workspace_root: Path,
        backend_name: str = "recording",
        mode: str = "auto",
        default_path: str = "/usr/bin:/bin",
        runner: SandboxProcessRunner | None = None,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._backend_name = backend_name
        self._mode = mode
        self._default_path = default_path
        self._runner = runner or SandboxProcessRunner()
        self.requests: list[SandboxExecRequest] = []
        self.specs: list[SandboxedLaunchSpec] = []
        self._events: list[Event] = []
        self._manager_root = Path(tempfile.mkdtemp(prefix="sandbox-fake-")).resolve()
        self._readiness = SandboxReadiness(
            ready=True,
            mode=mode,
            backend_name=backend_name,
            probe=SandboxProbeResult(
                backend_name=backend_name,
                available=True,
                capabilities={
                    "allowed_execution": True,
                    "outside_workspace_write_denied": True,
                    "network_denied": True,
                    "hidden_env_read_denied": True,
                    "protected_git_write_denied": True,
                    "child_creation_denied": True,
                },
            ),
            profiles=dict(_ALL_PROFILES),
            skipped_capabilities={},
            unsafe_fallback_active=(mode == "host_unsafe_dev_only"),
        )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def readiness(self) -> SandboxReadiness:
        return self._readiness

    def initialize(self) -> None:
        return None

    def is_profile_ready(self, profile_name: str) -> bool:
        return bool(self._readiness.profiles.get(profile_name, False))

    def build_launch_spec(self, request: SandboxExecRequest) -> SandboxedLaunchSpec:
        self._validate_request(request)
        environment = build_sandbox_environment(
            base_env={},
            overrides=request.env_overrides,
            allowed_secret_keys=request.allowed_secret_env,
            temp_root=self._manager_root,
            default_path=self._default_path,
        )
        if request.mode == "shell_string":
            executable = "/bin/sh"
            args = ("-c", request.command or "")
        else:
            assert request.argv is not None
            executable = request.argv[0]
            args = request.argv[1:]

        spec = SandboxedLaunchSpec(
            executable=executable,
            args=args,
            cwd=request.cwd.resolve(),
            env=environment.env,
            stdin_bytes=request.stdin_bytes,
            private_root=environment.private_root,
            backend_name=self._backend_name,
            profile_name=request.profile_name,
            correlation_id=request.correlation_id,
            unsafe_fallback_used=(self._mode == "host_unsafe_dev_only"),
        )
        self.requests.append(request)
        self.specs.append(spec)
        return spec

    async def run(self, request: SandboxExecRequest) -> SandboxExecResult:
        spec = self.build_launch_spec(request)
        try:
            return await self._runner.run(spec, request.timeout_seconds)
        finally:
            shutil.rmtree(spec.private_root, ignore_errors=True)

    def record_blocked_capability(self, name: str, reason: str) -> None:
        self._events.append(
            Event(
                type="sandbox.registration_skipped",
                data={"capability": name, "reason": reason},
            )
        )

    def finalize_readiness(self) -> SandboxReadiness:
        return self._readiness

    def drain_startup_events(self) -> tuple[Event, ...]:
        events = tuple(self._events)
        self._events.clear()
        return events

    def close(self) -> None:
        if not self._manager_root.exists():
            return
        if any(self._manager_root.iterdir()):
            raise RuntimeError("sandbox fake root still contains launch state")
        self._manager_root.rmdir()

    def _validate_request(self, request: SandboxExecRequest) -> None:
        if request.workspace_root.resolve() != self._workspace_root:
            raise SandboxUnavailableError("sandbox workspace root does not match controller workspace")
        if not request.cwd.resolve().is_relative_to(self._workspace_root):
            raise SandboxUnavailableError("sandbox cwd must stay inside the workspace root")
        if request.profile_name not in _ALL_PROFILES:
            raise SandboxUnavailableError(f"sandbox profile {request.profile_name!r} is unavailable")


class UnavailableSandboxController:
    def __init__(self, *, mode: str = "auto", backend_name: str = "unavailable") -> None:
        self._readiness = SandboxReadiness(
            ready=False,
            mode=mode,
            backend_name=backend_name,
            probe=SandboxProbeResult(
                backend_name=backend_name,
                available=False,
                capabilities={},
                reason="sandbox backend unavailable",
            ),
            profiles={
                "shell_workspace": False,
                "code_exec_python": False,
                "mcp_stdio_local": False,
            },
            skipped_capabilities={"sandbox": "backend unavailable"},
            unsafe_fallback_active=False,
        )
        self._events = (
            Event(
                type="sandbox.profile_unavailable",
                data={"profile_name": "shell_workspace", "reason": "backend unavailable"},
            ),
        )

    @property
    def mode(self) -> str:
        return self._readiness.mode

    @property
    def backend_name(self) -> str:
        return self._readiness.backend_name

    @property
    def readiness(self) -> SandboxReadiness:
        return self._readiness

    def initialize(self) -> None:
        return None

    def is_profile_ready(self, profile_name: str) -> bool:
        del profile_name
        return False

    def build_launch_spec(self, request: SandboxExecRequest) -> SandboxedLaunchSpec:
        del request
        raise SandboxUnavailableError("sandbox backend unavailable")

    async def run(self, request: SandboxExecRequest) -> SandboxExecResult:
        del request
        raise SandboxUnavailableError("sandbox backend unavailable")

    def record_blocked_capability(self, name: str, reason: str) -> None:
        del name, reason
        raise RuntimeError("sandbox readiness is already blocked")

    def finalize_readiness(self) -> SandboxReadiness:
        return self._readiness

    def drain_startup_events(self) -> tuple[Event, ...]:
        events = self._events
        self._events = ()
        return events

    def close(self) -> None:
        return None
