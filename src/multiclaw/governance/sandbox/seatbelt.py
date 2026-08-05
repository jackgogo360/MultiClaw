from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from multiclaw.governance.sandbox.errors import SandboxLaunchError
from multiclaw.governance.sandbox.models import (
    SandboxEnvironment,
    SandboxExecRequest,
    SandboxProbeResult,
    SandboxProfilePolicy,
    SandboxedLaunchSpec,
)
from multiclaw.governance.sandbox.seatbelt_profiles import (
    SEATBELT_PROFILES,
    SeatbeltProfileTemplate,
)

_PRODUCTION_BINARY = Path("/usr/bin/sandbox-exec")
_SUPPORTED_NETWORK_MODES = {"disabled", "inherit"}
_SUPPORTED_HIDDEN_PATTERNS = (".env", ".env.*")
_SUPPORTED_PROTECTED_PATTERNS = (".git",)
_MAX_RUNTIME_ROOTS = 16
_PROBE_CAPABILITIES = (
    "allowed_execution",
    "outside_workspace_write_denied",
    "network_denied",
    "hidden_env_read_denied",
    "protected_git_write_denied",
    "child_creation_denied",
)


class SeatbeltBackend:
    name = "seatbelt"

    def __init__(
        self,
        *,
        binary: Path = _PRODUCTION_BINARY,
        probe_timeout_seconds: float = 2.0,
        subprocess_run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        if probe_timeout_seconds <= 0:
            raise ValueError("probe_timeout_seconds must be positive")
        self.binary = Path(binary)
        self.probe_timeout_seconds = probe_timeout_seconds
        self._subprocess_run = subprocess_run

    def build_launch_spec(
        self,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
        environment: SandboxEnvironment,
    ) -> SandboxedLaunchSpec:
        if policy.name != request.profile_name:
            raise SandboxLaunchError("policy name must match request profile_name")

        template = self._template_for(policy.name)
        self._validate_request_overrides(request, policy)
        self._validate_policy_against_template(policy, template)
        target_argv = self._target_argv_for(request, policy)

        workspace_root = self._canonicalize_path(request.workspace_root, "workspace_root")
        cwd = self._canonicalize_path(request.cwd, "cwd")
        private_home = self._canonicalize_path(environment.home, "private_home")
        private_tmp = self._canonicalize_path(environment.tmp, "private_tmp")
        runtime_roots = self._collect_runtime_roots(policy, request)

        args = (
            *self._profile_parameter_args(
                workspace_root=workspace_root,
                private_home=private_home,
                private_tmp=private_tmp,
                runtime_roots=runtime_roots,
            ),
            "-p",
            template.profile_text,
            "--",
            *target_argv,
        )

        return SandboxedLaunchSpec(
            executable=str(self.binary),
            args=args,
            cwd=cwd,
            env=environment.env,
            stdin_bytes=request.stdin_bytes,
            private_root=environment.private_root,
            backend_name=self.name,
            profile_name=request.profile_name,
            correlation_id=request.correlation_id,
            unsafe_fallback_used=False,
        )

    def probe(
        self,
        workspace_root: Path,
        policies: tuple[SandboxProfilePolicy, ...],
    ) -> SandboxProbeResult:
        available, _, reason = self._probe_binary_ready()
        if not available:
            return SandboxProbeResult(
                backend_name=self.name,
                available=False,
                capabilities=self._default_capabilities(),
                reason=reason or "seatbelt backend unavailable",
            )

        shell_policy = self._policy_by_name(policies, "shell_workspace")
        code_policy = self._policy_by_name(policies, "code_exec_python")
        if shell_policy is None or code_policy is None:
            return SandboxProbeResult(
                backend_name=self.name,
                available=False,
                capabilities=self._default_capabilities(),
                reason="required seatbelt profiles are unavailable",
            )

        probe_root = Path(tempfile.mkdtemp(prefix="seatbelt-probe-"))
        try:
            self._canonicalize_path(
                workspace_root, "workspace_root"
            )
            probe_workspace = probe_root / "workspace"
            private_root = probe_root / "private"
            private_home = private_root / "home"
            private_tmp = private_root / "tmp"
            outside_root = probe_root / "outside"
            git_dir = probe_workspace / ".git"
            hidden_env = private_home / ".env"
            for path in (probe_workspace, private_home, private_tmp, outside_root, git_dir):
                path.mkdir(parents=True, exist_ok=True)
            hidden_env.write_text("SEATBELT_SECRET=probe-secret\n", encoding="utf-8")

            environment = SandboxEnvironment(
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(private_home),
                    "TMPDIR": str(private_tmp),
                },
                private_root=private_root,
                home=private_home,
                tmp=private_tmp,
            )
            capabilities = self._default_capabilities()
            code_entrypoint = self._preferred_code_entrypoint(code_policy)
            capability_groups = [
                (
                    "allowed_execution",
                    (
                        self._probe_definition(
                            request=self._probe_shell_request(
                                profile_name="shell_workspace",
                                workspace_root=probe_workspace,
                                cwd=probe_workspace,
                                command=":",
                            ),
                            policy=shell_policy,
                            expected_returncode=0,
                        ),
                        self._probe_definition(
                            request=self._probe_exec_request(
                                profile_name="code_exec_python",
                                workspace_root=probe_workspace,
                                cwd=probe_workspace,
                                argv=(str(code_entrypoint), "-c", "pass"),
                            ),
                            policy=code_policy,
                            expected_returncode=0,
                        ),
                    ),
                ),
                (
                    "outside_workspace_write_denied",
                    (
                        self._probe_definition(
                            request=self._probe_shell_request(
                                profile_name="shell_workspace",
                                workspace_root=probe_workspace,
                                cwd=probe_workspace,
                                command=self._shell_redirection_command(outside_root / "blocked.txt"),
                            ),
                            policy=shell_policy,
                            expected_returncode="nonzero",
                        ),
                    ),
                ),
                (
                    "network_denied",
                    (
                        self._probe_definition(
                            request=self._probe_shell_request(
                                profile_name="shell_workspace",
                                workspace_root=probe_workspace,
                                cwd=probe_workspace,
                                command="nc -G 1 -z 1.1.1.1 53",
                            ),
                            policy=shell_policy,
                            expected_returncode="nonzero",
                        ),
                    ),
                ),
                (
                    "hidden_env_read_denied",
                    (
                        self._probe_definition(
                            request=self._probe_shell_request(
                                profile_name="shell_workspace",
                                workspace_root=probe_workspace,
                                cwd=probe_workspace,
                                command="cat " + shlex.quote(str(hidden_env)) + " >/dev/null",
                            ),
                            policy=shell_policy,
                            expected_returncode="nonzero",
                        ),
                    ),
                ),
                (
                    "protected_git_write_denied",
                    (
                        self._probe_definition(
                            request=self._probe_shell_request(
                                profile_name="shell_workspace",
                                workspace_root=probe_workspace,
                                cwd=probe_workspace,
                                command=self._shell_redirection_command(git_dir / "config"),
                            ),
                            policy=shell_policy,
                            expected_returncode="nonzero",
                        ),
                    ),
                ),
                (
                    "child_creation_denied",
                    (
                        self._probe_definition(
                            request=self._probe_exec_request(
                                profile_name="code_exec_python",
                                workspace_root=probe_workspace,
                                cwd=probe_workspace,
                                argv=(
                                    str(code_entrypoint),
                                    "-c",
                                    "import subprocess; subprocess.run(['/usr/bin/true'], check=True)",
                                ),
                            ),
                            policy=code_policy,
                            expected_returncode="nonzero",
                        ),
                    ),
                ),
            ]

            for capability, definitions in capability_groups:
                capability_passed = True
                for definition in definitions:
                    try:
                        spec = self.build_launch_spec(
                            definition["request"],
                            definition["policy"],
                            environment,
                        )
                        completed = self._run_probe_command(
                            args=[spec.executable, *spec.args],
                            env=environment.env,
                        )
                    except subprocess.TimeoutExpired:
                        capability_passed = False
                        break
                    except Exception:
                        capability_passed = False
                        break

                    expected = definition["expected_returncode"]
                    observed = completed.returncode
                    success = observed == 0 if expected == 0 else observed != 0
                    if not success:
                        capability_passed = False
                        break

                capabilities[capability] = capability_passed
                if not capability_passed:
                    return SandboxProbeResult(
                        backend_name=self.name,
                        available=False,
                        capabilities=capabilities,
                        reason=self._failed_probe_reason(capability),
                    )

            return SandboxProbeResult(
                backend_name=self.name,
                available=True,
                capabilities=capabilities,
                reason="",
            )
        finally:
            shutil.rmtree(probe_root, ignore_errors=True)

    def _probe_binary_ready(self) -> tuple[bool, Path | None, str]:
        if not self.binary.exists() or not self.binary.is_file():
            return False, None, "seatbelt backend unavailable: sandbox-exec missing"
        if not os.access(self.binary, os.X_OK):
            return False, None, "seatbelt backend unavailable: sandbox-exec not executable"
        canonical_binary = self.binary.resolve(strict=False)
        if canonical_binary != _PRODUCTION_BINARY:
            return (
                False,
                canonical_binary,
                "seatbelt backend unavailable: sandbox-exec path is not production canonical",
            )
        return True, canonical_binary, ""

    def _run_probe_command(
        self,
        *,
        args: list[str],
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        return self._subprocess_run(
            args,
            shell=False,
            capture_output=True,
            timeout=self.probe_timeout_seconds,
            check=False,
            env=dict(env),
        )

    def _template_for(self, profile_name: str) -> SeatbeltProfileTemplate:
        template = SEATBELT_PROFILES.get(profile_name)
        if template is None:
            raise SandboxLaunchError(f"unsupported seatbelt profile {profile_name!r}")
        return template

    def _validate_request_overrides(
        self,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
    ) -> None:
        if request.network_mode is not None and request.network_mode != policy.network_mode:
            raise SandboxLaunchError("request network_mode does not match policy")
        if request.workspace_mode is not None and request.workspace_mode != policy.workspace_mode:
            raise SandboxLaunchError("request workspace_mode does not match policy")
        if (
            request.allow_subprocesses is not None
            and request.allow_subprocesses != policy.allow_subprocesses
        ):
            raise SandboxLaunchError("request allow_subprocesses does not match policy")

    def _validate_policy_against_template(
        self,
        policy: SandboxProfilePolicy,
        template: SeatbeltProfileTemplate,
    ) -> None:
        if policy.network_mode not in _SUPPORTED_NETWORK_MODES:
            raise SandboxLaunchError("unsupported network mode for seatbelt profile")
        if policy.network_mode != template.network_mode:
            raise SandboxLaunchError("network mode is not represented by the static template")
        if policy.workspace_mode != template.workspace_mode:
            raise SandboxLaunchError("workspace mode is not represented by the static template")
        if policy.allow_subprocesses != template.allow_subprocesses:
            raise SandboxLaunchError("subprocess policy is not represented by the static template")
        if tuple(policy.read_hidden_patterns) != template.read_hidden_patterns:
            raise SandboxLaunchError("hidden path policy is not represented by the static template")
        if tuple(policy.write_protected_patterns) != template.write_protected_patterns:
            raise SandboxLaunchError(
                "protected path policy is not represented by the static template"
            )
        if tuple(policy.read_hidden_patterns) != _SUPPORTED_HIDDEN_PATTERNS:
            raise SandboxLaunchError("hidden path policy must match the reviewed seatbelt rules")
        if tuple(policy.write_protected_patterns) != _SUPPORTED_PROTECTED_PATTERNS:
            raise SandboxLaunchError(
                "protected path policy must match the reviewed seatbelt rules"
            )

    def _canonicalize_existing_path(self, path: Path, label: str) -> Path:
        value = str(path)
        if "\x00" in value:
            raise SandboxLaunchError(f"{label} is invalid")
        if not path.is_absolute():
            raise SandboxLaunchError(f"{label} is invalid")
        try:
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise SandboxLaunchError(f"{label} is invalid") from exc
        if not canonical.is_file():
            raise SandboxLaunchError(f"{label} is invalid")
        return canonical

    def _canonicalize_path(self, path: Path, label: str) -> Path:
        value = str(path)
        if "\x00" in value:
            raise SandboxLaunchError(f"{label} contains a NUL byte")
        if not path.is_absolute():
            raise SandboxLaunchError(f"{label} must be absolute")
        canonical = path.resolve(strict=False)
        if not canonical.is_absolute():
            raise SandboxLaunchError(f"{label} must resolve to an absolute path")
        return canonical

    def _collect_runtime_roots(
        self,
        policy: SandboxProfilePolicy,
        request: SandboxExecRequest,
    ) -> tuple[Path, ...]:
        canonical_paths = {
            self._canonicalize_path(path, "runtime_read_only_path")
            for path in (*policy.runtime_read_only_paths, *request.read_only_paths)
        }
        ordered_paths = tuple(sorted(canonical_paths, key=str))
        if len(ordered_paths) > _MAX_RUNTIME_ROOTS:
            raise SandboxLaunchError("seatbelt supports at most 16 runtime read-only roots")
        return ordered_paths

    def _profile_parameter_args(
        self,
        *,
        workspace_root: Path,
        private_home: Path,
        private_tmp: Path,
        runtime_roots: tuple[Path, ...],
    ) -> tuple[str, ...]:
        args: list[str] = [
            "-D",
            "WORKSPACE=" + str(workspace_root),
            "-D",
            "PRIVATE_HOME=" + str(private_home),
            "-D",
            "PRIVATE_TMP=" + str(private_tmp),
        ]
        for index, path in enumerate(runtime_roots):
            args.extend(["-D", "RUNTIME_ROOT_" + str(index) + "=" + str(path)])
        # The static SBPL templates always reference RUNTIME_ROOT_0..15. Unused slots
        # are bound to PRIVATE_HOME so every param is defined without broadening access.
        for index in range(len(runtime_roots), _MAX_RUNTIME_ROOTS):
            args.extend(["-D", "RUNTIME_ROOT_" + str(index) + "=" + str(private_home)])
        return tuple(args)

    def _target_argv_for(
        self,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
    ) -> tuple[str, ...]:
        allowed_entrypoints = self._canonical_allowed_entrypoints(policy)
        if request.mode == "shell_string":
            if request.command is None or request.argv is not None:
                raise SandboxLaunchError("command is required for shell_string mode")
            shell_entrypoint = self._canonicalize_existing_path(Path("/bin/sh"), "entrypoint")
            if shell_entrypoint not in allowed_entrypoints:
                raise SandboxLaunchError("entrypoint is not allowed by the seatbelt policy")
            return ("/bin/sh", "-c", request.command)
        if request.mode == "exec_argv":
            if request.command is not None or not request.argv:
                raise SandboxLaunchError("argv must include executable for exec_argv mode")
            executable = Path(request.argv[0])
            target_entrypoint = self._canonicalize_existing_path(executable, "entrypoint")
            if target_entrypoint not in allowed_entrypoints:
                raise SandboxLaunchError("entrypoint is not allowed by the seatbelt policy")
            return request.argv
        raise SandboxLaunchError("unsupported request mode")

    def _probe_shell_request(
        self,
        *,
        profile_name: str,
        workspace_root: Path,
        cwd: Path,
        command: str,
    ) -> SandboxExecRequest:
        return SandboxExecRequest(
            tool_name="probe",
            profile_name=profile_name,
            mode="shell_string",
            command=command,
            workspace_root=workspace_root,
            cwd=cwd,
            timeout_seconds=self.probe_timeout_seconds,
        )

    def _probe_exec_request(
        self,
        *,
        profile_name: str,
        workspace_root: Path,
        cwd: Path,
        argv: tuple[str, ...],
    ) -> SandboxExecRequest:
        return SandboxExecRequest(
            tool_name="probe",
            profile_name=profile_name,
            mode="exec_argv",
            argv=argv,
            workspace_root=workspace_root,
            cwd=cwd,
            timeout_seconds=self.probe_timeout_seconds,
        )

    def _probe_definition(
        self,
        *,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
        expected_returncode: int | str,
    ) -> dict[str, Any]:
        return {
            "request": request,
            "policy": policy,
            "expected_returncode": expected_returncode,
        }

    def _policy_by_name(
        self,
        policies: tuple[SandboxProfilePolicy, ...],
        name: str,
    ) -> SandboxProfilePolicy | None:
        for policy in policies:
            if policy.name == name:
                return policy
        return None

    def _default_capabilities(self) -> dict[str, bool]:
        return {capability: False for capability in _PROBE_CAPABILITIES}

    def _failed_probe_reason(self, capability: str) -> str:
        return "seatbelt capability check failed: " + capability

    def _canonical_allowed_entrypoints(
        self,
        policy: SandboxProfilePolicy,
    ) -> tuple[Path, ...]:
        if not policy.entrypoints:
            raise SandboxLaunchError("entrypoint policy is invalid")
        return tuple(
            self._canonicalize_existing_path(entrypoint, "entrypoint")
            for entrypoint in policy.entrypoints
        )

    def _preferred_code_entrypoint(self, policy: SandboxProfilePolicy) -> Path:
        entrypoints = self._canonical_allowed_entrypoints(policy)
        return entrypoints[0]

    def _shell_redirection_command(self, target_path: Path) -> str:
        return ": > " + shlex.quote(str(target_path))
