from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from multiclaw.governance.sandbox.errors import SandboxLaunchError
from multiclaw.governance.sandbox.models import (
    SandboxEnvironment,
    SandboxExecRequest,
    SandboxProbeResult,
    SandboxProfilePolicy,
    SandboxedLaunchSpec,
)
from multiclaw.governance.sandbox.nsjail_profiles import (
    NSJAIL_PROFILES,
    NsJailProfileTemplate,
    NsJailSystemMount,
)

_PRODUCTION_BINARY = Path("/usr/bin/nsjail")
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
_PROBE_DENIED_MARKER = "MULTICLAW_NSJAIL_DENIED\n"


def protobuf_quote(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


class NsJailBackend:
    name = "nsjail"

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

        binary = self._canonicalize_path(self.binary, "binary")
        workspace_root = self._canonicalize_existing_dir(request.workspace_root, "workspace_root")
        cwd = self._canonicalize_path(request.cwd, "cwd")
        self._ensure_relative_to(cwd, workspace_root, "cwd")

        private_root = self._canonicalize_existing_dir(environment.private_root, "private_root")
        private_home = self._canonicalize_existing_dir(environment.home, "private_home")
        private_tmp = self._canonicalize_existing_dir(environment.tmp, "private_tmp")
        self._ensure_relative_to(private_home, private_root, "private_home")
        self._ensure_relative_to(private_tmp, private_root, "private_tmp")

        runtime_roots = self._collect_runtime_roots(policy, request)
        hidden_mounts = self._prepare_hidden_mount_sources(
            workspace_root=workspace_root,
            private_root=private_root,
            patterns=tuple(policy.read_hidden_patterns),
        )
        protected_mounts = self._existing_workspace_matches(
            workspace_root=workspace_root,
            patterns=tuple(policy.write_protected_patterns),
        )
        config_text = self._render_config_text(
            request=request,
            template=template,
            workspace_root=workspace_root,
            cwd=cwd,
            private_home=private_home,
            private_tmp=private_tmp,
            runtime_roots=runtime_roots,
            hidden_mounts=hidden_mounts,
            protected_mounts=protected_mounts,
        )
        config_path = self._write_private_config(
            private_root=private_root,
            config_text=config_text,
        )

        return SandboxedLaunchSpec(
            executable=str(binary),
            args=("--config", str(config_path), "--", *target_argv),
            cwd=cwd,
            env=environment.env,
            stdin_bytes=request.stdin_bytes,
            private_root=private_root,
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
        available, canonical_binary, reason = self._probe_binary_ready()
        if not available or canonical_binary is None:
            return SandboxProbeResult(
                backend_name=self.name,
                available=False,
                capabilities=self._default_capabilities(),
                reason=reason or "nsjail backend unavailable",
            )

        shell_policy = self._policy_by_name(policies, "shell_workspace")
        code_policy = self._policy_by_name(policies, "code_exec_python")
        if shell_policy is None or code_policy is None:
            return SandboxProbeResult(
                backend_name=self.name,
                available=False,
                capabilities=self._default_capabilities(),
                reason="required nsjail profiles are unavailable",
            )

        self._canonicalize_existing_dir(workspace_root, "workspace_root")
        probe_root = Path(tempfile.mkdtemp(prefix="nsjail-probe-"))
        network_listener: socket.socket | None = None
        try:
            probe_workspace = probe_root / "workspace"
            private_root = probe_root / "private"
            private_home = private_root / "home"
            private_tmp = private_root / "tmp"
            outside_root = probe_root / "outside"
            git_dir = probe_workspace / ".git"
            for path in (probe_workspace, private_home, private_tmp, outside_root, git_dir):
                path.mkdir(parents=True, exist_ok=True)
            (probe_workspace / ".env").write_text("probe-secret\n", encoding="utf-8")
            (probe_workspace / ".env.local").write_text("probe-secret-local\n", encoding="utf-8")
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
            network_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            network_listener.bind(("127.0.0.1", 0))
            network_listener.listen(1)
            network_port = network_listener.getsockname()[1]
            capability_groups: tuple[
                tuple[str, bool, tuple[SandboxExecRequest, ...], SandboxProfilePolicy],
                ...,
            ] = (
                (
                    "allowed_execution",
                    False,
                    (
                        self._probe_shell_request(
                            profile_name="shell_workspace",
                            workspace_root=probe_workspace,
                            cwd=probe_workspace,
                            command=": # probe-allow-shell",
                        ),
                        self._probe_exec_request(
                            profile_name="code_exec_python",
                            workspace_root=probe_workspace,
                            cwd=probe_workspace,
                            argv=(
                                str(code_entrypoint),
                                "-c",
                                "pass # probe-allow-code",
                            ),
                        ),
                    ),
                    shell_policy,
                ),
                (
                    "outside_workspace_write_denied",
                    True,
                    (
                        self._probe_shell_python_request(
                            profile_name="shell_workspace",
                            workspace_root=probe_workspace,
                            cwd=probe_workspace,
                            python_entrypoint=code_entrypoint,
                            script=self._denied_write_probe_script(outside_root / "blocked.txt"),
                            marker_name="probe-deny-outside-write",
                        ),
                    ),
                    shell_policy,
                ),
                (
                    "network_denied",
                    True,
                    (
                        self._probe_shell_python_request(
                            profile_name="shell_workspace",
                            workspace_root=probe_workspace,
                            cwd=probe_workspace,
                            python_entrypoint=code_entrypoint,
                            script=self._denied_network_probe_script(network_port),
                            marker_name="probe-deny-network",
                        ),
                    ),
                    shell_policy,
                ),
                (
                    "hidden_env_read_denied",
                    True,
                    (
                        self._probe_shell_python_request(
                            profile_name="shell_workspace",
                            workspace_root=probe_workspace,
                            cwd=probe_workspace,
                            python_entrypoint=code_entrypoint,
                            script=self._denied_read_probe_script(probe_workspace / ".env"),
                            marker_name="probe-deny-hidden-read",
                        ),
                    ),
                    shell_policy,
                ),
                (
                    "protected_git_write_denied",
                    True,
                    (
                        self._probe_shell_python_request(
                            profile_name="shell_workspace",
                            workspace_root=probe_workspace,
                            cwd=probe_workspace,
                            python_entrypoint=code_entrypoint,
                            script=self._denied_write_probe_script(git_dir / "config"),
                            marker_name="probe-deny-protected-write",
                        ),
                    ),
                    shell_policy,
                ),
                (
                    "child_creation_denied",
                    True,
                    (
                        self._probe_exec_request(
                            profile_name="code_exec_python",
                            workspace_root=probe_workspace,
                            cwd=probe_workspace,
                            argv=(
                                str(code_entrypoint),
                                "-c",
                                self._denied_child_process_probe_script(),
                            ),
                        ),
                    ),
                    code_policy,
                ),
            )

            for capability, expect_denied_marker, requests, default_policy in capability_groups:
                capability_passed = True
                for probe_request in requests:
                    policy = code_policy if probe_request.profile_name == "code_exec_python" else default_policy
                    try:
                        spec = self.build_launch_spec(probe_request, policy, environment)
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

                    success = self._probe_result_matches(
                        completed=completed,
                        expect_denied_marker=expect_denied_marker,
                    )
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
            try:
                if network_listener is not None:
                    network_listener.close()
            except Exception:
                pass
            shutil.rmtree(probe_root, ignore_errors=True)

    def _probe_binary_ready(self) -> tuple[bool, Path | None, str]:
        if not self.binary.exists() or not self.binary.is_file():
            return False, None, "nsjail backend unavailable: nsjail missing"
        if not os.access(self.binary, os.X_OK):
            return False, None, "nsjail backend unavailable: nsjail not executable"
        canonical_binary = self.binary.resolve(strict=False)
        if canonical_binary != _PRODUCTION_BINARY.resolve(strict=False):
            return (
                False,
                canonical_binary,
                "nsjail backend unavailable: nsjail path is not production canonical",
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

    def _template_for(self, profile_name: str) -> NsJailProfileTemplate:
        template = NSJAIL_PROFILES.get(profile_name)
        if template is None:
            raise SandboxLaunchError(f"unsupported nsjail profile {profile_name!r}")
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
        template: NsJailProfileTemplate,
    ) -> None:
        if policy.network_mode not in _SUPPORTED_NETWORK_MODES:
            raise SandboxLaunchError("unsupported network mode for nsjail profile")
        if policy.network_mode != template.network_mode:
            raise SandboxLaunchError("network mode is not represented by the reviewed template")
        if policy.workspace_mode != template.workspace_mode:
            raise SandboxLaunchError("workspace mode is not represented by the reviewed template")
        if policy.allow_subprocesses != template.allow_subprocesses:
            raise SandboxLaunchError("subprocess policy is not represented by the reviewed template")
        if tuple(policy.read_hidden_patterns) != template.read_hidden_patterns:
            raise SandboxLaunchError("hidden path policy is not represented by the reviewed template")
        if tuple(policy.write_protected_patterns) != template.write_protected_patterns:
            raise SandboxLaunchError("protected path policy is not represented by the reviewed template")
        if tuple(policy.read_hidden_patterns) != _SUPPORTED_HIDDEN_PATTERNS:
            raise SandboxLaunchError("hidden path policy must match the reviewed nsjail rules")
        if tuple(policy.write_protected_patterns) != _SUPPORTED_PROTECTED_PATTERNS:
            raise SandboxLaunchError("protected path policy must match the reviewed nsjail rules")

    def _canonicalize_existing_file(self, path: Path, label: str) -> Path:
        self._reject_nul(str(path), label)
        if not path.is_absolute():
            raise SandboxLaunchError(f"{label} is invalid")
        try:
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise SandboxLaunchError(f"{label} is invalid") from exc
        if not canonical.is_file():
            raise SandboxLaunchError(f"{label} is invalid")
        return canonical

    def _canonicalize_existing_dir(self, path: Path, label: str) -> Path:
        self._reject_nul(str(path), label)
        if not path.is_absolute():
            raise SandboxLaunchError(f"{label} must be absolute")
        try:
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise SandboxLaunchError(f"{label} is invalid") from exc
        if not canonical.is_dir():
            raise SandboxLaunchError(f"{label} is invalid")
        return canonical

    def _canonicalize_path(self, path: Path, label: str) -> Path:
        self._reject_nul(str(path), label)
        if not path.is_absolute():
            raise SandboxLaunchError(f"{label} must be absolute")
        canonical = path.resolve(strict=False)
        if not canonical.is_absolute():
            raise SandboxLaunchError(f"{label} must resolve to an absolute path")
        return canonical

    def _reject_nul(self, value: str, label: str) -> None:
        if "\x00" in value:
            raise SandboxLaunchError(f"{label} contains a NUL byte")

    def _ensure_relative_to(self, child: Path, parent: Path, label: str) -> None:
        try:
            child.relative_to(parent)
        except ValueError as exc:
            raise SandboxLaunchError(f"{label} must remain within {parent}") from exc

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
            raise SandboxLaunchError("nsjail supports at most 16 runtime read-only roots")
        return ordered_paths

    def _existing_workspace_matches(
        self,
        *,
        workspace_root: Path,
        patterns: tuple[str, ...],
    ) -> tuple[tuple[Path, Path], ...]:
        matches: dict[Path, Path] = {}
        canonical_workspace = workspace_root.resolve(strict=True)
        for pattern in patterns:
            for path in workspace_root.glob(pattern):
                if path.is_symlink():
                    raise SandboxLaunchError("workspace path match is invalid")
                canonical = path.resolve(strict=True)
                self._ensure_relative_to(canonical, canonical_workspace, "workspace path match")
                matches[path] = canonical
        return tuple(sorted(matches.items(), key=lambda item: str(item[0])))

    def _prepare_hidden_mount_sources(
        self,
        *,
        workspace_root: Path,
        private_root: Path,
        patterns: tuple[str, ...],
    ) -> tuple[tuple[Path, Path], ...]:
        hidden_root = private_root / ".nsjail-hidden"
        hidden_root.mkdir(mode=0o700, exist_ok=True)
        mounts: list[tuple[Path, Path]] = []
        for index, (lexical_path, canonical_path) in enumerate(
            self._existing_workspace_matches(workspace_root=workspace_root, patterns=patterns)
        ):
            target = hidden_root / f"hidden-{index}"
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            if canonical_path.is_dir():
                target.mkdir(mode=0o000, exist_ok=True)
                os.chmod(target, 0o000)
            else:
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
                os.chmod(target, 0o000)
            mounts.append((lexical_path, target.resolve(strict=True)))
        return tuple(mounts)

    def _render_config_text(
        self,
        *,
        request: SandboxExecRequest,
        template: NsJailProfileTemplate,
        workspace_root: Path,
        cwd: Path,
        private_home: Path,
        private_tmp: Path,
        runtime_roots: tuple[Path, ...],
        hidden_mounts: tuple[tuple[Path, Path], ...],
        protected_mounts: tuple[Path, ...],
    ) -> str:
        lines = [
            "mode: ONCE",
            'hostname: "multiclaw"',
            f'cwd: "{protobuf_quote(str(cwd))}"',
            f"time_limit: {max(1, int(request.timeout_seconds))}",
            "keep_env: true",
            "keep_caps: false",
            "disable_no_new_privs: false",
            "mount_proc: true",
            f'clone_newnet: {"true" if template.network_mode == "disabled" else "false"}',
            "clone_newuser: true",
            "clone_newns: true",
            "clone_newpid: true",
            "clone_newipc: true",
            "clone_newuts: true",
            'uidmap {',
            '  inside_id: "0"',
            f'  outside_id: "{os.getuid()}"',
            '  count: 1',
            '}',
            'gidmap {',
            '  inside_id: "0"',
            f'  outside_id: "{os.getgid()}"',
            '  count: 1',
            '}',
            f"rlimit_as: {template.rlimit_as_mb}",
            "rlimit_as_type: VALUE",
            "rlimit_core: 0",
            "rlimit_core_type: VALUE",
            f"rlimit_cpu: {template.rlimit_cpu_seconds}",
            "rlimit_cpu_type: VALUE",
            f"rlimit_fsize: {template.rlimit_fsize_mb}",
            "rlimit_fsize_type: VALUE",
            f"rlimit_nofile: {template.rlimit_nofile}",
            "rlimit_nofile_type: VALUE",
            f"rlimit_nproc: {template.rlimit_nproc}",
            "rlimit_nproc_type: VALUE",
            f'seccomp_string: "{protobuf_quote(template.seccomp_policy)}"',
        ]
        lines.extend(
            self._mount_block(
                src=workspace_root,
                dst=workspace_root,
                rw=template.workspace_mode == "rw",
                is_dir=True,
                mandatory=True,
            )
        )
        lines.extend(
            self._mount_block(
                src=private_home,
                dst=private_home,
                rw=True,
                is_dir=True,
                mandatory=True,
            )
        )
        lines.extend(
            self._mount_block(
                src=private_tmp,
                dst=private_tmp,
                rw=True,
                is_dir=True,
                mandatory=True,
            )
        )
        for root in template.system_read_only_roots:
            src = self._canonicalize_path(Path(root.path), "system_root")
            is_dir = self._mount_type_for_system_root(src, root)
            lines.extend(
                self._mount_block(
                    src=src,
                    dst=src,
                    rw=False,
                    is_dir=is_dir,
                    mandatory=root.mandatory,
                )
            )
        for root in runtime_roots:
            lines.extend(
                self._mount_block(
                    src=root,
                    dst=root,
                    rw=False,
                    is_dir=root.is_dir(),
                    mandatory=True,
                )
            )
        for protected, canonical_protected in protected_mounts:
            lines.extend(
                self._mount_block(
                    src=canonical_protected,
                    dst=protected,
                    rw=False,
                    is_dir=canonical_protected.is_dir(),
                    mandatory=True,
                )
            )
        for hidden_target, hidden_source in hidden_mounts:
            lines.extend(
                self._mount_block(
                    src=hidden_source,
                    dst=hidden_target,
                    rw=False,
                    is_dir=hidden_target.is_dir(),
                    mandatory=True,
                )
            )
        return "\n".join(lines) + "\n"

    def _mount_block(
        self,
        *,
        src: Path,
        dst: Path,
        rw: bool,
        is_dir: bool,
        mandatory: bool,
    ) -> tuple[str, ...]:
        return (
            "mount {",
            f'  src: "{protobuf_quote(str(src))}"',
            f'  dst: "{protobuf_quote(str(dst))}"',
            '  is_bind: true',
            f'  rw: {"true" if rw else "false"}',
            f'  is_dir: {"true" if is_dir else "false"}',
            f'  mandatory: {"true" if mandatory else "false"}',
            "}",
        )

    def _write_private_config(
        self,
        *,
        private_root: Path,
        config_text: str,
    ) -> Path:
        self._reject_nul(config_text, "config_text")
        fd = -1
        config_path: Path | None = None
        success = False
        try:
            fd, path_string = tempfile.mkstemp(
                prefix="nsjail-",
                suffix=".cfg",
                dir=str(private_root),
            )
            config_path = Path(path_string).resolve(strict=False)
            os.fchmod(fd, 0o600)
            payload = config_text.encode("utf-8")
            written = 0
            while written < len(payload):
                written += os.write(fd, payload[written:])
            os.fsync(fd)
            success = True
            return config_path
        except OSError as exc:
            raise SandboxLaunchError("failed to write nsjail config") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if not success and config_path is not None:
                try:
                    config_path.unlink()
                except OSError:
                    pass

    def _target_argv_for(
        self,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
    ) -> tuple[str, ...]:
        allowed_entrypoints = self._canonical_allowed_entrypoints(policy)
        if request.mode == "shell_string":
            if request.command is None or request.argv is not None:
                raise SandboxLaunchError("command is required for shell_string mode")
            self._reject_nul(request.command, "command")
            shell_entrypoint = self._canonicalize_existing_file(Path("/bin/sh"), "entrypoint")
            if shell_entrypoint not in allowed_entrypoints:
                raise SandboxLaunchError("entrypoint is not allowed by the nsjail policy")
            return ("/bin/sh", "-c", request.command)
        if request.mode == "exec_argv":
            if request.command is not None or not request.argv:
                raise SandboxLaunchError("argv must include executable for exec_argv mode")
            executable = Path(request.argv[0])
            if not executable.is_absolute():
                raise SandboxLaunchError("entrypoint is invalid")
            normalized_entrypoint = executable.absolute()
            target_entrypoint = self._canonicalize_existing_file(executable, "entrypoint")
            if normalized_entrypoint != target_entrypoint:
                raise SandboxLaunchError("canonical entrypoint is required")
            if target_entrypoint not in allowed_entrypoints:
                raise SandboxLaunchError("entrypoint is not allowed by the nsjail policy")
            for index, arg in enumerate(request.argv):
                self._reject_nul(arg, f"argv[{index}]")
            return request.argv
        raise SandboxLaunchError("unsupported request mode")

    def _canonical_allowed_entrypoints(
        self,
        policy: SandboxProfilePolicy,
    ) -> tuple[Path, ...]:
        if not policy.entrypoints:
            raise SandboxLaunchError("entrypoint policy is invalid")
        return tuple(
            self._canonicalize_existing_file(entrypoint, "entrypoint")
            for entrypoint in policy.entrypoints
        )

    def _preferred_code_entrypoint(self, policy: SandboxProfilePolicy) -> Path:
        return self._canonical_allowed_entrypoints(policy)[0]

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

    def _probe_shell_python_request(
        self,
        *,
        profile_name: str,
        workspace_root: Path,
        cwd: Path,
        python_entrypoint: Path,
        script: str,
        marker_name: str,
    ) -> SandboxExecRequest:
        command = (
            shlex.quote(str(python_entrypoint))
            + " -c "
            + shlex.quote(script)
            + " # "
            + marker_name
        )
        return self._probe_shell_request(
            profile_name=profile_name,
            workspace_root=workspace_root,
            cwd=cwd,
            command=command,
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
        return "nsjail capability check failed: " + capability

    def _probe_result_matches(
        self,
        *,
        completed: subprocess.CompletedProcess[bytes],
        expect_denied_marker: bool,
    ) -> bool:
        if expect_denied_marker:
            return (
                completed.returncode == 0
                and completed.stdout == _PROBE_DENIED_MARKER.encode("utf-8")
            )
        return (
            completed.returncode == 0
            and completed.stdout != _PROBE_DENIED_MARKER.encode("utf-8")
        )

    def _mount_type_for_system_root(
        self,
        path: Path,
        system_root: NsJailSystemMount,
    ) -> bool:
        if path.exists():
            return path.is_dir()
        return system_root.is_dir

    def _denied_read_probe_script(self, target_path: Path) -> str:
        return (
            "import sys\n"
            f"target = {str(target_path)!r}\n"
            "try:\n"
            "    with open(target, 'rb') as handle:\n"
            "        handle.read(1)\n"
            "except (OSError, PermissionError):\n"
            f"    sys.stdout.write({_PROBE_DENIED_MARKER!r})\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(23)\n"
        )

    def _denied_write_probe_script(self, target_path: Path) -> str:
        return (
            "import sys\n"
            f"target = {str(target_path)!r}\n"
            "try:\n"
            "    with open(target, 'wb') as handle:\n"
            "        handle.write(b'x')\n"
            "except (OSError, PermissionError):\n"
            f"    sys.stdout.write({_PROBE_DENIED_MARKER!r})\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(23)\n"
        )

    def _denied_network_probe_script(self, port: int) -> str:
        return (
            "import socket\n"
            "import sys\n"
            "try:\n"
            f"    sock = socket.create_connection(('127.0.0.1', {port}), timeout=1.0)\n"
            "except OSError:\n"
            f"    sys.stdout.write({_PROBE_DENIED_MARKER!r})\n"
            "    raise SystemExit(0)\n"
            "else:\n"
            "    sock.close()\n"
            "raise SystemExit(23)\n"
        )

    def _denied_child_process_probe_script(self) -> str:
        return (
            "import subprocess\n"
            "import sys\n"
            "# probe-deny-child-process\n"
            "try:\n"
            "    subprocess.run(['/usr/bin/true'], check=True)\n"
            "except (OSError, PermissionError):\n"
            f"    sys.stdout.write({_PROBE_DENIED_MARKER!r})\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(23)\n"
        )
