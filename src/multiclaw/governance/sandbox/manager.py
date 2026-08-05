from __future__ import annotations

import os
import re
import shutil
import sys
import sysconfig
import tempfile
import threading
from pathlib import Path

from multiclaw.config.settings import SandboxSettings
from multiclaw.events import Event, EventBus
from multiclaw.governance.sandbox.backend import (
    HostUnsafeBackend,
    SandboxBackend,
    SandboxController,
)
from multiclaw.governance.sandbox.environment import build_sandbox_environment
from multiclaw.governance.sandbox.errors import (
    SandboxConfigurationError,
    SandboxLaunchError,
    SandboxPolicyError,
    SandboxUnavailableError,
)
from multiclaw.governance.sandbox.models import (
    SandboxExecRequest,
    SandboxExecResult,
    SandboxProbeResult,
    SandboxProfilePolicy,
    SandboxReadiness,
    SandboxedLaunchSpec,
    _is_secret_env_key,
)
from multiclaw.governance.sandbox.nsjail import NsJailBackend
from multiclaw.governance.sandbox.runner import SandboxProcessRunner
from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

_PROBE_CAPABILITIES = (
    "allowed_execution",
    "outside_workspace_write_denied",
    "network_denied",
    "hidden_env_read_denied",
    "protected_git_write_denied",
    "child_creation_denied",
)
_SHELL_CAPABILITIES = frozenset(
    capability for capability in _PROBE_CAPABILITIES if capability != "child_creation_denied"
)
_CODE_CAPABILITIES = frozenset(_PROBE_CAPABILITIES)
_MCP_CAPABILITIES = _SHELL_CAPABILITIES
_DARWIN_DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_LINUX_DEFAULT_PATH = "/usr/bin:/bin"


class _UnavailableBackend:
    name = "unavailable"

    def __init__(self, *, reason: str) -> None:
        self._reason = reason

    def probe(
        self,
        workspace_root: Path,
        policies: tuple[SandboxProfilePolicy, ...],
    ) -> SandboxProbeResult:
        del workspace_root, policies
        return SandboxProbeResult(
            backend_name=self.name,
            available=False,
            capabilities={capability: False for capability in _PROBE_CAPABILITIES},
            reason=self._reason,
        )

    def build_launch_spec(
        self,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
        environment,
    ) -> SandboxedLaunchSpec:
        del request, policy, environment
        raise SandboxUnavailableError(self._reason)


class SandboxManager(SandboxController):
    def __init__(
        self,
        *,
        settings: SandboxSettings,
        debug: bool,
        workspace_root: Path,
        backend: SandboxBackend,
        event_bus: EventBus | None,
        runner: SandboxProcessRunner | None,
        platform_name: str,
        backend_name: str,
    ) -> None:
        self._settings = settings
        self._debug = debug
        self._workspace_root_lexical = self._absolute_lexical_path(workspace_root)
        self._workspace_root = self._canonical_workspace_root(workspace_root)
        self._workspace_root_aliases = self._build_path_aliases(
            self._workspace_root_lexical,
            self._workspace_root,
        )
        self._backend = backend
        self._event_bus = event_bus
        self._runner = runner or SandboxProcessRunner()
        self._platform_name = platform_name
        self._backend_name = backend_name
        self._policies = self._build_policies()
        self._policies_by_name = {policy.name: policy for policy in self._policies}
        manager_root_raw = Path(tempfile.mkdtemp(prefix="multiclaw-sandbox-"))
        self._manager_root_lexical = self._absolute_lexical_path(manager_root_raw)
        self._manager_root = manager_root_raw.resolve()
        self._manager_root_aliases = self._build_path_aliases(
            self._manager_root_lexical,
            self._manager_root,
        )
        self._manager_root.chmod(0o700)
        self._probe = self._initial_probe()
        self._profile_readiness = {policy.name: False for policy in self._policies}
        self._blocked_capabilities: dict[str, str] = {}
        self._startup_events: list[Event] = []
        self._lifecycle_lock = threading.RLock()
        self._readiness_snapshot: SandboxReadiness | None = None
        self._initialized = False
        self._probed = False

        if self.mode == "host_unsafe_dev_only":
            if not debug:
                raise SandboxConfigurationError(
                    "governance.sandbox.mode='host_unsafe_dev_only' requires app.debug=true"
                )
            self._profile_readiness = {policy.name: True for policy in self._policies}
            self._probe = SandboxProbeResult(
                backend_name=self._backend_name,
                available=True,
                capabilities={capability: False for capability in _PROBE_CAPABILITIES},
                reason="development-only host execution without isolation",
            )
            self._initialized = True
            self._buffer_event(
                Event(
                    type="sandbox.unsafe_fallback_used",
                    data={"backend_name": self._backend_name, "scope": "startup"},
                )
            )

    @classmethod
    def create(
        cls,
        *,
        settings: SandboxSettings,
        debug: bool,
        workspace_root: Path,
        event_bus: EventBus | None = None,
        runner: SandboxProcessRunner | None = None,
        platform_name: str | None = None,
        backend_override: SandboxBackend | None = None,
    ) -> "SandboxManager":
        resolved_platform = platform_name or sys.platform
        if resolved_platform.startswith("linux"):
            resolved_platform = "Linux"
        elif resolved_platform.startswith("darwin"):
            resolved_platform = "Darwin"

        if settings.mode == "host_unsafe_dev_only":
            backend: SandboxBackend = HostUnsafeBackend()
            backend_name = "host-unsafe-dev-only"
        elif backend_override is not None:
            backend = backend_override
            backend_name = backend_override.name
        elif resolved_platform == "Darwin":
            backend = SeatbeltBackend()
            backend_name = backend.name
        elif resolved_platform == "Linux":
            backend = NsJailBackend()
            backend_name = backend.name
        else:
            reason = f"unsupported sandbox platform {resolved_platform!r}"
            backend = _UnavailableBackend(reason=reason)
            backend_name = backend.name

        return cls(
            settings=settings,
            debug=debug,
            workspace_root=workspace_root,
            backend=backend,
            event_bus=event_bus,
            runner=runner,
            platform_name=resolved_platform,
            backend_name=backend_name,
        )

    @property
    def mode(self) -> str:
        return self._settings.mode

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def readiness(self) -> SandboxReadiness:
        with self._lifecycle_lock:
            return self._readiness_snapshot or self._build_readiness()

    def initialize(self) -> None:
        with self._lifecycle_lock:
            if self._initialized:
                return
            if self._readiness_snapshot is not None:
                raise RuntimeError("sandbox readiness has already been finalized before initialize")

            self._initialized = True
            if self.mode != "auto":
                return

            if not self._settings.backend_probe_on_startup:
                self._probe = SandboxProbeResult(
                    backend_name=self._backend_name,
                    available=False,
                    capabilities={capability: False for capability in _PROBE_CAPABILITIES},
                    reason="backend probe disabled at startup",
                )
                self.record_blocked_capability(
                    "sandbox_probe",
                    "backend probe disabled at startup",
                )
                return

            self._probed = True
            self._probe = self._sanitize_probe_result(
                self._backend.probe(self._workspace_root, self._policies)
            )
            self._profile_readiness = self._profiles_from_probe(self._probe)
            if not self._probe.available or not all(
                self._probe.capabilities.get(name, False) for name in _CODE_CAPABILITIES
            ):
                for profile_name in self._profile_readiness:
                    self._profile_readiness[profile_name] = False

            if not self._profile_readiness["shell_workspace"]:
                self._buffer_profile_unavailable(
                    "shell_workspace",
                    self._probe.reason or "probe did not prove shell isolation",
                )
            if not self._profile_readiness["code_exec_python"]:
                self._buffer_profile_unavailable(
                    "code_exec_python",
                    self._probe.reason or "probe did not prove code execution isolation",
                )
            if not self._profile_readiness["mcp_stdio_local"]:
                self._buffer_profile_unavailable(
                    "mcp_stdio_local",
                    self._probe.reason or "probe did not prove MCP stdio isolation",
                )

    def is_profile_ready(self, profile_name: str) -> bool:
        return bool(self.readiness.profiles.get(profile_name, False))

    def build_launch_spec(self, request: SandboxExecRequest) -> SandboxedLaunchSpec:
        if self.mode == "auto" and not self._initialized:
            raise SandboxUnavailableError("sandbox manager must initialize before building launch specs")

        policy = self._policies_by_name.get(request.profile_name)
        if policy is None:
            raise SandboxUnavailableError(f"sandbox profile {request.profile_name!r} is unavailable")
        if not self.is_profile_ready(policy.name):
            raise SandboxUnavailableError(f"sandbox profile {policy.name!r} is not ready")

        canonical_workspace = self._canonical_request_workspace(request.workspace_root)
        canonical_cwd = self._canonical_request_cwd(request.cwd, canonical_workspace)
        normalized_request = request.model_copy(
            update={
                "workspace_root": canonical_workspace,
                "cwd": canonical_cwd,
            }
        )

        environment = build_sandbox_environment(
            base_env=os.environ,
            overrides=normalized_request.env_overrides,
            allowed_secret_keys=normalized_request.allowed_secret_env,
            temp_root=self._manager_root,
            default_path=self._default_path(),
        )
        try:
            rendered = self._backend.build_launch_spec(normalized_request, policy, environment)
            spec = rendered.model_copy(
                update={
                    "backend_name": self._backend_name,
                    "profile_name": policy.name,
                    "unsafe_fallback_used": self.mode == "host_unsafe_dev_only",
                }
            )
        except Exception:
            shutil.rmtree(environment.private_root, ignore_errors=True)
            raise

        if self.mode == "host_unsafe_dev_only":
            self._buffer_event(
                Event(
                    type="sandbox.unsafe_fallback_used",
                    data={"backend_name": self._backend_name, "scope": "launch"},
                )
            )
        return spec

    async def run(self, request: SandboxExecRequest) -> SandboxExecResult:
        spec = self.build_launch_spec(request)
        try:
            result = await self._runner.run(spec, request.timeout_seconds)
            return result.model_copy(
                update={
                    "backend_name": self._backend_name,
                    "profile_name": spec.profile_name,
                    "unsafe_fallback_used": spec.unsafe_fallback_used,
                }
            )
        finally:
            shutil.rmtree(spec.private_root, ignore_errors=True)

    def record_blocked_capability(self, name: str, reason: str) -> None:
        with self._lifecycle_lock:
            if self._readiness_snapshot is not None:
                raise RuntimeError("sandbox readiness has already been finalized")

            safe_name = self._sanitize_text(name)
            safe_reason = self._sanitize_text(reason)
            self._blocked_capabilities[safe_name] = safe_reason
            self._startup_events.append(
                Event(
                    type="sandbox.registration_skipped",
                    data={"capability": safe_name, "reason": safe_reason},
                )
            )

    def record_unsafe_capability(self, name: str, reason: str) -> None:
        with self._lifecycle_lock:
            if self._readiness_snapshot is not None:
                raise RuntimeError("sandbox readiness has already been finalized")
            if self.mode != "host_unsafe_dev_only":
                raise SandboxPolicyError(
                    "unsafe capability evidence requires mode='host_unsafe_dev_only'"
                )

            self._startup_events.append(
                Event(
                    type="sandbox.unsafe_fallback_used",
                    data={
                        "backend_name": self._backend_name,
                        "scope": "capability",
                        "capability": self._sanitize_text(name),
                        "reason": self._sanitize_text(reason),
                    },
                )
            )

    def finalize_readiness(self) -> SandboxReadiness:
        with self._lifecycle_lock:
            if self._readiness_snapshot is None:
                self._readiness_snapshot = self._build_readiness()
            return self._readiness_snapshot

    def drain_startup_events(self) -> tuple[Event, ...]:
        with self._lifecycle_lock:
            events = tuple(self._startup_events)
            self._startup_events.clear()
            return events

    def close(self) -> None:
        if not self._manager_root.exists():
            return
        children = tuple(self._manager_root.iterdir())
        if children:
            raise RuntimeError("sandbox manager still has live launch state under its root")
        self._manager_root.rmdir()

    def _initial_probe(self) -> SandboxProbeResult:
        if self.mode == "host_unsafe_dev_only":
            return SandboxProbeResult(
                backend_name=self._backend_name,
                available=True,
                capabilities={capability: False for capability in _PROBE_CAPABILITIES},
                reason="development-only host execution without isolation",
            )
        return SandboxProbeResult(
            backend_name=self._backend_name,
            available=False,
            capabilities={capability: False for capability in _PROBE_CAPABILITIES},
            reason="sandbox backend has not been probed yet",
        )

    def _build_readiness(self) -> SandboxReadiness:
        profiles = dict(self._profile_readiness)
        skipped = dict(self._blocked_capabilities)
        if self.mode == "auto":
            ready = (
                self._probe.available
                and all(profiles.values())
                and not skipped
            )
        else:
            ready = self._debug
        return SandboxReadiness(
            ready=ready,
            mode=self.mode,
            backend_name=self._backend_name,
            probe=self._probe,
            profiles=profiles,
            skipped_capabilities=skipped,
            unsafe_fallback_active=(self.mode == "host_unsafe_dev_only"),
        )

    def _profiles_from_probe(self, probe: SandboxProbeResult) -> dict[str, bool]:
        result = {policy.name: False for policy in self._policies}
        if not probe.available:
            return result

        required = {
            "shell_workspace": _SHELL_CAPABILITIES,
            "code_exec_python": _CODE_CAPABILITIES,
            "mcp_stdio_local": _MCP_CAPABILITIES,
        }
        for profile_name, capability_names in required.items():
            if all(probe.capabilities.get(name, False) for name in capability_names):
                result[profile_name] = True

        return result

    def _buffer_profile_unavailable(self, profile_name: str, reason: str) -> None:
        self._buffer_event(
            Event(
                type="sandbox.profile_unavailable",
                data={
                    "profile_name": profile_name,
                    "reason": self._sanitize_text(reason),
                },
            )
        )

    def _buffer_event(self, event: Event) -> None:
        with self._lifecycle_lock:
            self._startup_events.append(event)

    def _build_policies(self) -> tuple[SandboxProfilePolicy, ...]:
        protected = tuple(self._settings.write_protected_workspace_paths)
        hidden = tuple(self._settings.read_hidden_workspace_paths)
        return (
            SandboxProfilePolicy(
                name=self._settings.profiles.shell,
                workspace_mode="rw",
                network_mode="disabled",
                allow_subprocesses=True,
                entrypoints=(Path("/bin/sh").resolve(),),
                write_protected_patterns=protected,
                read_hidden_patterns=hidden,
            ),
            SandboxProfilePolicy(
                name=self._settings.profiles.code_exec,
                workspace_mode="rw",
                network_mode="disabled",
                allow_subprocesses=False,
                entrypoints=(Path(sys.executable).resolve(),),
                runtime_read_only_paths=self._runtime_read_only_paths(),
                write_protected_patterns=protected,
                read_hidden_patterns=hidden,
            ),
            SandboxProfilePolicy(
                name=self._settings.profiles.mcp_stdio,
                workspace_mode="ro",
                # Task 7 keeps the backend-compatible MCP base profile. Task 11
                # will layer server-specific request overrides and conservative
                # per-server defaults on top of this profile.
                network_mode="inherit",
                allow_subprocesses=True,
                entrypoints=(Path("/usr/bin/env").resolve(),),
                write_protected_patterns=protected,
                read_hidden_patterns=hidden,
            ),
        )

    def _runtime_read_only_paths(self) -> tuple[Path, ...]:
        candidates = {
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
        }
        for value in sysconfig.get_paths().values():
            if value:
                candidates.add(Path(value).resolve())

        ordered = sorted(
            (
                path
                for path in candidates
                if path.exists() and not path.is_relative_to(self._workspace_root)
            ),
            key=lambda path: (len(path.parts), str(path)),
        )
        result: list[Path] = []
        for candidate in ordered:
            if any(candidate == root or candidate.is_relative_to(root) for root in result):
                continue
            result = [root for root in result if not root.is_relative_to(candidate)]
            result.append(candidate)
        return tuple(result)

    def _default_path(self) -> str:
        if self._platform_name == "Darwin":
            return _DARWIN_DEFAULT_PATH
        return _LINUX_DEFAULT_PATH

    def _canonical_workspace_root(self, workspace_root: Path) -> Path:
        try:
            resolved = workspace_root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SandboxConfigurationError("sandbox workspace_root must exist") from exc
        if not resolved.is_dir():
            raise SandboxConfigurationError("sandbox workspace_root must be a directory")
        return resolved

    def _canonical_request_workspace(self, workspace_root: Path) -> Path:
        try:
            resolved = workspace_root.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SandboxLaunchError("sandbox request workspace root must exist") from exc
        if resolved != self._workspace_root:
            raise SandboxLaunchError("sandbox request workspace root must match manager workspace root")
        return resolved

    def _canonical_request_cwd(self, cwd: Path, workspace_root: Path) -> Path:
        try:
            resolved = cwd.resolve(strict=True)
        except FileNotFoundError as exc:
            raise SandboxLaunchError("sandbox request cwd must exist") from exc
        if not resolved.is_relative_to(workspace_root):
            raise SandboxLaunchError("sandbox request cwd must stay inside the workspace root")
        return resolved

    def _sanitize_text(self, value: str) -> str:
        sanitized = " ".join(value.split())
        sanitized = self._replace_path_aliases(
            sanitized,
            self._manager_root_aliases,
            "[PRIVATE_ROOT]",
        )
        sanitized = self._replace_path_aliases(
            sanitized,
            self._workspace_root_aliases,
            "[WORKSPACE_ROOT]",
        )
        sanitized = re.sub(
            r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)",
            self._redact_assignment_match,
            sanitized,
        )
        return sanitized

    def _sanitize_probe_result(self, probe: SandboxProbeResult) -> SandboxProbeResult:
        return probe.model_copy(update={"reason": self._sanitize_text(probe.reason)})

    def _redact_assignment_match(self, match: re.Match[str]) -> str:
        key = match.group(1)
        value = match.group(2)
        if _is_secret_env_key(key) or self._looks_like_sensitive_value(value):
            return f"{key}=[REDACTED]"
        return f"{key}={value}"

    def _looks_like_sensitive_value(self, value: str) -> bool:
        return self._path_token_matches_alias(
            value,
            self._manager_root_aliases | self._workspace_root_aliases,
        )

    def _absolute_lexical_path(self, path: Path) -> Path:
        return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))

    def _build_path_aliases(self, *paths: Path) -> set[str]:
        return {str(path) for path in paths if str(path)}

    def _replace_path_aliases(
        self,
        text: str,
        aliases: set[str],
        placeholder: str,
    ) -> str:
        if not aliases:
            return text
        return re.sub(
            r"(?<![A-Za-z0-9._~/-])([\"']?/[A-Za-z0-9._~/-]+[\"']?)(?=[\s,.;:!?)]|$)",
            lambda match: self._replace_path_token_match(match, aliases, placeholder),
            text,
        )

    def _replace_path_token_match(
        self,
        match: re.Match[str],
        aliases: set[str],
        placeholder: str,
    ) -> str:
        token = match.group(1)
        leading = token[:1] if token[:1] in {'"', "'"} else ""
        trailing = token[-1:] if token[-1:] in {'"', "'"} else ""
        raw_token = token[len(leading): len(token) - len(trailing) if trailing else len(token)]
        if self._path_token_matches_alias(raw_token, aliases):
            return f"{leading}{placeholder}{trailing}"
        return token

    def _path_token_matches_alias(self, token: str, aliases: set[str]) -> bool:
        normalized = Path(token)
        normalized_resolved = normalized.resolve(strict=False)
        for alias in aliases:
            alias_path = Path(alias)
            alias_resolved = alias_path.resolve(strict=False)
            if self._same_or_descendant(normalized, alias_path):
                return True
            if self._same_or_descendant(normalized_resolved, alias_resolved):
                return True
        return False

    def _same_or_descendant(self, candidate: Path, root: Path) -> bool:
        return candidate == root or candidate.is_relative_to(root)
