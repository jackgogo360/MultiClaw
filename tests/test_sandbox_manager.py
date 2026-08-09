from __future__ import annotations

import asyncio
import re
import shutil
import sys
import sysconfig
import threading
from pathlib import Path

import pytest

from multiclaw.config.settings import SandboxProfileNames, SandboxSettings
from multiclaw.governance import (
    SandboxExecRequest,
    SandboxExecResult,
    SandboxLaunchError,
    SandboxProbeResult,
    SandboxProfilePolicy,
    SandboxUnavailableError,
    SandboxedLaunchSpec,
)


_PROBE_CAPABILITIES = {
    "allowed_execution": True,
    "outside_workspace_write_denied": True,
    "network_denied": True,
    "hidden_env_read_denied": True,
    "protected_git_write_denied": True,
    "child_creation_denied": True,
}


class RecordingBackend:
    def __init__(
        self,
        *,
        name: str = "recording",
        probe_result: SandboxProbeResult | None = None,
        render_error: Exception | None = None,
        create_sidecar: bool = False,
    ) -> None:
        self.name = name
        self.probe_result = probe_result or SandboxProbeResult(
            backend_name=name,
            available=True,
            capabilities=dict(_PROBE_CAPABILITIES),
        )
        self.render_error = render_error
        self.create_sidecar = create_sidecar
        self.probe_calls: list[dict[str, object]] = []
        self.build_calls: list[dict[str, object]] = []

    def probe(
        self,
        workspace_root: Path,
        policies: tuple[SandboxProfilePolicy, ...],
    ) -> SandboxProbeResult:
        self.probe_calls.append(
            {
                "workspace_root": workspace_root,
                "policies": policies,
            }
        )
        return self.probe_result

    def build_launch_spec(
        self,
        request: SandboxExecRequest,
        policy: SandboxProfilePolicy,
        environment,
    ) -> SandboxedLaunchSpec:
        if policy.name != request.profile_name:
            raise SandboxLaunchError("policy name must match request profile_name")
        if request.network_mode is not None and request.network_mode != policy.network_mode:
            raise SandboxLaunchError("request network_mode override is not allowed")
        if request.workspace_mode is not None and request.workspace_mode != policy.workspace_mode:
            raise SandboxLaunchError("request workspace_mode override is not allowed")
        if (
            request.allow_subprocesses is not None
            and request.allow_subprocesses is not policy.allow_subprocesses
        ):
            raise SandboxLaunchError("request allow_subprocesses override is not allowed")
        if request.mode == "exec_argv":
            assert request.argv is not None
            executable = request.argv[0]
            if Path(executable).resolve() not in policy.entrypoints:
                raise SandboxLaunchError("request argv[0] is not an allowed entrypoint")
            args = request.argv[1:]
        else:
            executable = str(policy.entrypoints[0])
            if Path(executable).resolve() not in policy.entrypoints:
                raise SandboxLaunchError("request shell entrypoint is not allowed")
            args = ("-c", request.command or "")

        if self.create_sidecar:
            (environment.private_root / "backend.conf").write_text("sidecar", encoding="utf-8")

        self.build_calls.append(
            {
                "request": request,
                "policy": policy,
                "environment": environment,
            }
        )

        if self.render_error is not None:
            raise self.render_error

        return SandboxedLaunchSpec(
            executable=executable,
            args=args,
            cwd=request.cwd.resolve(),
            env=environment.env,
            stdin_bytes=request.stdin_bytes,
            private_root=environment.private_root,
            backend_name=self.name,
            profile_name=request.profile_name,
            correlation_id=request.correlation_id,
            unsafe_fallback_used=False,
        )


class RecordingRunner:
    def __init__(
        self,
        *,
        result: SandboxExecResult | None = None,
        exception: Exception | None = None,
        event: asyncio.Event | None = None,
    ) -> None:
        self.result = result
        self.exception = exception
        self.event = event
        self.calls: list[tuple[SandboxedLaunchSpec, float]] = []

    async def run(self, spec: SandboxedLaunchSpec, timeout_seconds: float) -> SandboxExecResult:
        self.calls.append((spec, timeout_seconds))
        if self.event is not None:
            await self.event.wait()
        if self.exception is not None:
            raise self.exception
        assert self.result is not None
        return self.result


def _settings(
    *,
    mode: str = "auto",
    backend_probe_on_startup: bool = True,
    profiles: SandboxProfileNames | None = None,
) -> SandboxSettings:
    return SandboxSettings(
        mode=mode,
        backend_probe_on_startup=backend_probe_on_startup,
        profiles=profiles or SandboxProfileNames(),
    )


def _request(
    workspace_root: Path,
    *,
    profile_name: str = "shell_workspace",
    mode: str = "shell_string",
    command: str = ":",
    argv: tuple[str, ...] | None = None,
    cwd: Path | None = None,
    correlation_id: str = "corr-1",
    **kwargs,
) -> SandboxExecRequest:
    payload: dict[str, object] = {
        "tool_name": "tool",
        "profile_name": profile_name,
        "mode": mode,
        "workspace_root": workspace_root,
        "cwd": cwd or workspace_root,
        "timeout_seconds": 1.0,
        "correlation_id": correlation_id,
    }
    if mode == "shell_string":
        payload["command"] = command
    else:
        payload["argv"] = argv
    payload.update(kwargs)
    return SandboxExecRequest(**payload)


def _canonical_runtime_roots(workspace_root: Path) -> tuple[Path, ...]:
    candidates = {
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
    }
    for value in sysconfig.get_paths().values():
        if value:
            candidates.add(Path(value).resolve())

    workspace = workspace_root.resolve()
    filtered = [path for path in candidates if path.exists() and not path.is_relative_to(workspace)]
    filtered.sort(key=lambda path: (len(path.parts), str(path)))

    result: list[Path] = []
    for candidate in filtered:
        if any(candidate == root or candidate.is_relative_to(root) for root in result):
            continue
        result = [root for root in result if not root.is_relative_to(candidate)]
        result.append(candidate)
    return tuple(result)


def _manager_root(manager) -> Path:
    return manager._manager_root


def _assert_sanitized_payload(text: str, *sensitive_values: str) -> None:
    for sensitive_value in sensitive_values:
        if not sensitive_value:
            continue
        pattern = rf"(?<![A-Za-z0-9._~/-])[\"']?{re.escape(sensitive_value)}[\"']?(?=[\s,.;:!?)]|$)"
        if re.search(pattern, text):
            raise AssertionError("sanitized payload leaked sensitive text")


@pytest.mark.parametrize(
    ("platform_name", "expected_type_name", "expected_backend_name"),
    [
        ("Darwin", "SeatbeltBackend", "seatbelt"),
        ("Linux", "NsJailBackend", "nsjail"),
    ],
)
def test_create_selects_native_backend_by_platform_name(
    tmp_path: Path,
    platform_name: str,
    expected_type_name: str,
    expected_backend_name: str,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        platform_name=platform_name,
    )

    assert type(manager._backend).__name__ == expected_type_name
    assert manager.backend_name == expected_backend_name
    assert manager.readiness.backend_name == expected_backend_name
    assert manager.readiness.ready is False


def test_create_uses_backend_override_and_fails_closed_on_unsupported_platform(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    override = RecordingBackend(name="recording")
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        platform_name="Plan9",
        backend_override=override,
    )
    manager.initialize()
    readiness = manager.finalize_readiness()

    assert manager._backend is override
    assert manager.backend_name == "recording"
    assert readiness.ready is True

    unsupported = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        platform_name="Plan9",
    )
    unsupported.initialize()
    readiness = unsupported.finalize_readiness()

    assert unsupported.backend_name == "unavailable"
    assert readiness.ready is False
    assert readiness.backend_name == "unavailable"
    assert unsupported.is_profile_ready("shell_workspace") is False
    with pytest.raises(SandboxUnavailableError):
        unsupported.build_launch_spec(_request(tmp_path))


def test_explicit_unsafe_mode_requires_debug_even_if_settings_validation_was_bypassed(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox import SandboxConfigurationError
    from multiclaw.governance.sandbox.manager import SandboxManager

    with pytest.raises(SandboxConfigurationError, match="app.debug"):
        SandboxManager.create(
            settings=_settings(mode="host_unsafe_dev_only"),
            debug=False,
            workspace_root=tmp_path,
            platform_name="Linux",
        )

    manager = SandboxManager.create(
        settings=_settings(mode="host_unsafe_dev_only"),
        debug=True,
        workspace_root=tmp_path,
        platform_name="Linux",
    )
    readiness = manager.finalize_readiness()

    assert manager.backend_name == "host-unsafe-dev-only"
    assert readiness.ready is True
    assert readiness.unsafe_fallback_active is True
    assert readiness.backend_name == "host-unsafe-dev-only"


def test_auto_initialize_probes_once_and_marks_only_proven_profiles_ready(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name="recording")
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )

    manager.initialize()
    manager.initialize()
    provisional = manager.readiness
    finalized = manager.finalize_readiness()

    assert len(backend.probe_calls) == 1
    assert provisional.ready is True
    assert provisional.profiles == {
        "shell_workspace": True,
        "code_exec_python": True,
        "mcp_stdio_local": True,
    }
    assert finalized is manager.finalize_readiness()


def test_auto_initialize_uses_configured_profile_names_for_readiness_and_events(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    profiles = SandboxProfileNames(
        shell="custom_shell",
        code_exec="custom_code",
        mcp_stdio="custom_mcp",
    )
    backend = RecordingBackend(
        name="recording",
        probe_result=SandboxProbeResult(
            backend_name="recording",
            available=True,
            capabilities={**_PROBE_CAPABILITIES, "child_creation_denied": False},
            reason="missing subprocess proof",
        ),
    )
    manager = SandboxManager.create(
        settings=_settings(profiles=profiles),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )

    manager.initialize()
    readiness = manager.finalize_readiness()
    startup_events = manager.drain_startup_events()

    assert readiness.profiles == {
        "custom_shell": False,
        "custom_code": False,
        "custom_mcp": False,
    }
    assert "shell_workspace" not in readiness.profiles
    assert "code_exec_python" not in readiness.profiles
    assert "mcp_stdio_local" not in readiness.profiles
    unavailable_profiles = {
        event.data["profile_name"]
        for event in startup_events
        if event.type == "sandbox.profile_unavailable"
    }
    assert unavailable_profiles == {"custom_shell", "custom_code", "custom_mcp"}


def test_initialize_after_early_finalize_raises_before_mutating_frozen_state(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name="recording")
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )

    frozen = manager.finalize_readiness()

    assert frozen.ready is False
    with pytest.raises(RuntimeError, match="finalized"):
        manager.initialize()
    assert backend.probe_calls == []
    assert manager.finalize_readiness() is frozen
    assert frozen.profiles == {
        "shell_workspace": False,
        "code_exec_python": False,
        "mcp_stdio_local": False,
    }


def test_record_blocked_capability_cannot_race_past_finalization(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=RecordingBackend(name="recording"),
    )

    class CoordinatedRLock:
        def __init__(self, inner: threading.RLock) -> None:
            self._inner = inner
            self.record_entered = threading.Event()
            self.allow_record_to_continue = threading.Event()
            self._blocked_once = False

        def __enter__(self):
            if threading.current_thread() is not threading.main_thread() and not self._blocked_once:
                self._blocked_once = True
                self.record_entered.set()
                assert self.allow_record_to_continue.wait(timeout=2.0)
            self._inner.acquire()
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            del exc_type, exc, tb
            self._inner.release()

    coordinated_lock = CoordinatedRLock(manager._lifecycle_lock)
    manager._lifecycle_lock = coordinated_lock
    exceptions: list[Exception] = []

    def record() -> None:
        try:
            manager.record_blocked_capability("late capability", "late reason")
        except Exception as exc:  # pragma: no branch - deterministic single path
            exceptions.append(exc)

    worker = threading.Thread(target=record)
    worker.start()
    assert coordinated_lock.record_entered.wait(timeout=2.0)
    frozen = manager.finalize_readiness()
    coordinated_lock.allow_record_to_continue.set()
    worker.join(timeout=2.0)

    assert len(exceptions) == 1
    assert isinstance(exceptions[0], RuntimeError)
    assert frozen.skipped_capabilities == {}
    assert manager.drain_startup_events() == ()


@pytest.mark.parametrize("backend_name", ["seatbelt", "nsjail"])
def test_backend_proven_mcp_profile_stays_ready_for_current_native_backend_shape(
    tmp_path: Path,
    backend_name: str,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name=backend_name)
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )

    manager.initialize()
    readiness = manager.finalize_readiness()

    assert readiness.ready is True
    assert readiness.profiles == {
        "shell_workspace": True,
        "code_exec_python": True,
        "mcp_stdio_local": True,
    }
    assert readiness.skipped_capabilities == {}


def test_auto_initialize_fails_closed_on_failed_or_incomplete_probe_and_never_falls_back_to_host(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    failed_backend = RecordingBackend(
        name="recording",
        probe_result=SandboxProbeResult(
            backend_name="recording",
            available=False,
            capabilities=dict(_PROBE_CAPABILITIES),
            reason="probe failed",
        ),
    )
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=failed_backend,
    )
    manager.initialize()
    readiness = manager.finalize_readiness()

    assert readiness.ready is False
    assert readiness.unsafe_fallback_active is False
    assert all(value is False for value in readiness.profiles.values())
    with pytest.raises(SandboxUnavailableError):
        asyncio.run(manager.run(_request(tmp_path)))

    incomplete_backend = RecordingBackend(
        name="recording",
        probe_result=SandboxProbeResult(
            backend_name="recording",
            available=True,
            capabilities={**_PROBE_CAPABILITIES, "network_denied": False},
            reason="missing proof",
        ),
    )
    incomplete = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=incomplete_backend,
    )
    incomplete.initialize()
    readiness = incomplete.finalize_readiness()

    assert readiness.ready is False
    assert all(value is False for value in readiness.profiles.values())
    events = incomplete.drain_startup_events()
    assert any(event.type == "sandbox.profile_unavailable" for event in events)
    assert all("host" not in str(event.model_dump()).lower() for event in events)


def test_auto_mode_without_probe_keeps_all_profiles_unavailable_and_skips_probe(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name="recording")
    manager = SandboxManager.create(
        settings=_settings(backend_probe_on_startup=False),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )

    manager.initialize()
    readiness = manager.finalize_readiness()

    assert backend.probe_calls == []
    assert readiness.ready is False
    assert all(value is False for value in readiness.profiles.values())
    assert readiness.skipped_capabilities["sandbox_probe"] == "backend probe disabled at startup"


def test_fixed_profile_registry_matches_expected_policies_and_runtime_roots(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=RecordingBackend(name="recording"),
    )

    policies = {policy.name: policy for policy in manager._policies}

    assert set(policies) == {"shell_workspace", "code_exec_python", "mcp_stdio_local"}
    assert policies["shell_workspace"].workspace_mode == "rw"
    assert policies["shell_workspace"].network_mode == "disabled"
    assert policies["shell_workspace"].allow_subprocesses is True
    assert policies["shell_workspace"].entrypoints == (Path("/bin/sh").resolve(),)
    assert policies["shell_workspace"].write_protected_patterns == (".git",)
    assert policies["shell_workspace"].read_hidden_patterns == (".env", ".env.*")

    code_policy = policies["code_exec_python"]
    assert code_policy.workspace_mode == "rw"
    assert code_policy.network_mode == "disabled"
    assert code_policy.allow_subprocesses is False
    assert code_policy.entrypoints == (Path(sys.executable).resolve(),)
    assert code_policy.runtime_read_only_paths == _canonical_runtime_roots(tmp_path)
    assert all(not path.is_relative_to(tmp_path.resolve()) for path in code_policy.runtime_read_only_paths)

    mcp_policy = policies["mcp_stdio_local"]
    assert mcp_policy.workspace_mode == "ro"
    assert mcp_policy.network_mode == "disabled"
    assert mcp_policy.allow_subprocesses is False
    assert mcp_policy.entrypoints == (Path("/usr/bin/env").resolve(),)
    assert mcp_policy.write_protected_patterns == (".git",)
    assert mcp_policy.read_hidden_patterns == (".env", ".env.*")


def test_mcp_launch_spec_uses_conservative_defaults_and_dynamic_policy_overrides(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name="recording")
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )
    manager.initialize()

    runtime_root = tmp_path / "granted-runtime"
    runtime_root.mkdir()

    default_spec = manager.build_launch_spec(
        _request(
            tmp_path,
            profile_name="mcp_stdio_local",
            mode="exec_argv",
            argv=("/usr/bin/env",),
        )
    )
    default_policy = backend.build_calls[-1]["policy"]
    assert default_spec.profile_name == "mcp_stdio_local"
    assert default_policy.workspace_mode == "ro"
    assert default_policy.network_mode == "disabled"
    assert default_policy.allow_subprocesses is False
    assert default_policy.entrypoints == (Path("/usr/bin/env").resolve(),)
    assert default_policy.runtime_read_only_paths == ()

    manager.build_launch_spec(
        _request(
            tmp_path,
            profile_name="mcp_stdio_local",
            mode="exec_argv",
            argv=("/usr/bin/env",),
            network_mode="inherit",
            workspace_mode="rw",
            allow_subprocesses=True,
            read_only_paths=(runtime_root / ".", runtime_root),
        )
    )
    dynamic_policy = backend.build_calls[-1]["policy"]
    assert dynamic_policy.workspace_mode == "rw"
    assert dynamic_policy.network_mode == "inherit"
    assert dynamic_policy.allow_subprocesses is True
    assert dynamic_policy.entrypoints == (Path("/usr/bin/env").resolve(),)
    assert dynamic_policy.runtime_read_only_paths == (runtime_root.resolve(),)

    registry_policy = next(
        policy for policy in manager._policies if policy.name == "mcp_stdio_local"
    )
    assert registry_policy.workspace_mode == "ro"
    assert registry_policy.network_mode == "disabled"
    assert registry_policy.allow_subprocesses is False
    assert registry_policy.runtime_read_only_paths == ()


def test_configured_mcp_profile_name_keeps_explicit_mcp_policy_identity(
    tmp_path: Path,
) -> None:
    from multiclaw.config.settings import SandboxProfileNames
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(
            profiles=SandboxProfileNames(
                shell="shell_workspace",
                code_exec="code_exec_python",
                mcp_stdio="custom_mcp_profile",
            )
        ),
        debug=False,
        workspace_root=tmp_path,
        backend_override=RecordingBackend(name="recording"),
    )

    policies = {policy.name: policy for policy in manager._policies}

    assert policies["shell_workspace"].profile_kind == "shell"
    assert policies["code_exec_python"].profile_kind == "code_exec"
    assert policies["custom_mcp_profile"].profile_kind == "mcp_stdio"


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"network_mode": "inherit"},
        {"workspace_mode": "ro"},
        {"allow_subprocesses": False},
        {"read_only_paths": (Path("/tmp"),)},
    ],
)
def test_non_mcp_profiles_reject_dynamic_sandbox_overrides(
    tmp_path: Path,
    request_kwargs: dict[str, object],
) -> None:
    from multiclaw.governance import SandboxPolicyError
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=RecordingBackend(name="recording"),
    )
    manager.initialize()

    with pytest.raises(SandboxPolicyError, match="mcp_stdio"):
        manager.build_launch_spec(_request(tmp_path, **request_kwargs))


@pytest.mark.parametrize(
    ("platform_name", "expected_path"),
    [
        ("Darwin", "/usr/bin:/bin:/usr/sbin:/sbin"),
        ("Linux", "/usr/bin:/bin"),
    ],
)
def test_build_launch_spec_requires_ready_profile_and_uses_scrubbed_platform_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    expected_path: str,
) -> None:
    from multiclaw.governance import SandboxPolicyError
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name="recording")
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        platform_name=platform_name,
        backend_override=backend,
    )

    with pytest.raises(SandboxUnavailableError, match="initialize"):
        manager.build_launch_spec(_request(tmp_path))

    manager.initialize()
    monkeypatch.setenv("PATH", "/tmp/host-only")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("LANG", "C.UTF-8")
    spec = manager.build_launch_spec(_request(tmp_path, env_overrides={"custom_flag": "1"}))

    environment = backend.build_calls[-1]["environment"]
    assert spec.env["PATH"] == expected_path
    assert environment.env["PATH"] == expected_path
    assert environment.env["LANG"] == "C.UTF-8"
    assert environment.env["CUSTOM_FLAG"] == "1"
    assert "SSH_AUTH_SOCK" not in environment.env
    assert spec.private_root.parent == _manager_root(manager)

    with pytest.raises(SandboxLaunchError, match="workspace root"):
        manager.build_launch_spec(
            _request(tmp_path / "other", cwd=tmp_path / "other")
        )
    with pytest.raises(SandboxLaunchError, match="cwd"):
        manager.build_launch_spec(
            _request(tmp_path, cwd=tmp_path.parent)
        )
    with pytest.raises(SandboxUnavailableError, match="profile"):
        manager.build_launch_spec(
            _request(tmp_path, profile_name="missing_profile")
        )
    with pytest.raises(SandboxPolicyError, match="mcp_stdio"):
        manager.build_launch_spec(
            _request(tmp_path, network_mode="inherit")
        )


def test_build_launch_spec_cleans_up_private_root_when_rendering_fails_and_correlation_id_never_enters_paths(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(
        name="recording",
        render_error=SandboxLaunchError("renderer rejected launch"),
    )
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )
    manager.initialize()

    with pytest.raises(SandboxLaunchError, match="renderer rejected"):
        manager.build_launch_spec(
            _request(tmp_path, correlation_id="../../../../sneaky/correlation")
        )

    assert list(_manager_root(manager).iterdir()) == []
    assert all("sneaky" not in str(path) for path in _manager_root(manager).rglob("*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner", "expect_exception", "timed_out"),
    [
        (
            RecordingRunner(
                result=SandboxExecResult(
                    exit_code=0,
                    timed_out=False,
                    signal=None,
                    stdout=b"ok",
                    stderr=b"",
                    backend_name="wrong",
                    profile_name="wrong",
                    unsafe_fallback_used=False,
                )
            ),
            None,
            False,
        ),
        (
            RecordingRunner(
                result=SandboxExecResult(
                    exit_code=None,
                    timed_out=True,
                    signal="SIGKILL",
                    stdout=b"",
                    stderr=b"timeout",
                    backend_name="wrong",
                    profile_name="wrong",
                    unsafe_fallback_used=False,
                )
            ),
            None,
            True,
        ),
        (
            RecordingRunner(exception=SandboxLaunchError("spawn failed")),
            SandboxLaunchError,
            None,
        ),
    ],
)
async def test_run_cleans_up_launch_roots_and_preserves_honest_metadata(
    tmp_path: Path,
    runner: RecordingRunner,
    expect_exception: type[Exception] | None,
    timed_out: bool | None,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name="recording", create_sidecar=True)
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
        runner=runner,
    )
    manager.initialize()
    request = _request(
        tmp_path,
        profile_name="code_exec_python",
        mode="exec_argv",
        argv=(sys.executable, "-c", "print('ok')"),
    )

    if expect_exception is not None:
        with pytest.raises(expect_exception):
            await manager.run(request)
    else:
        result = await manager.run(request)
        assert result.backend_name == "recording"
        assert result.profile_name == "code_exec_python"
        assert result.unsafe_fallback_used is False
        assert result.timed_out is timed_out

    assert _manager_root(manager).exists()
    assert list(_manager_root(manager).iterdir()) == []


@pytest.mark.asyncio
async def test_run_cleans_up_launch_root_on_cancellation(tmp_path: Path) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    gate = asyncio.Event()
    runner = RecordingRunner(event=gate)
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=RecordingBackend(name="recording", create_sidecar=True),
        runner=runner,
    )
    manager.initialize()
    request = _request(tmp_path)

    task = asyncio.create_task(manager.run(request))
    await asyncio.sleep(0)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert list(_manager_root(manager).iterdir()) == []


def test_build_only_specs_are_not_auto_deleted_and_close_requires_clean_manager_root(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=RecordingBackend(name="recording"),
    )
    manager.initialize()
    spec = manager.build_launch_spec(_request(tmp_path))

    assert spec.private_root.exists()
    with pytest.raises(RuntimeError, match="live launch state"):
        manager.close()

    shutil.rmtree(spec.private_root, ignore_errors=True)
    manager.close()
    manager.close()
    assert _manager_root(manager).exists() is False


def test_readiness_lifecycle_buffers_events_until_finalization_and_blocks_late_mutation(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(backend_probe_on_startup=False),
        debug=False,
        workspace_root=tmp_path,
        backend_override=RecordingBackend(name="recording"),
    )

    provisional = manager.readiness
    assert provisional.ready is False
    manager.record_blocked_capability("mcp_stdio", "deferred for task 11")
    manager.initialize()
    first = manager.finalize_readiness()
    second = manager.finalize_readiness()
    events = manager.drain_startup_events()

    assert first is second
    assert first.skipped_capabilities["mcp_stdio"] == "deferred for task 11"
    assert any(event.type == "sandbox.registration_skipped" for event in events)
    assert manager.drain_startup_events() == ()
    with pytest.raises(RuntimeError, match="finalized"):
        manager.record_blocked_capability("late", "too late")


def test_manager_sanitizes_blocked_and_profile_reasons_in_readiness_and_events(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name="recording")
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )

    explicit_secret = "task7-sensitive-value"
    blocked_name = (
        f"capability {_manager_root(manager)} {tmp_path} OPENAI_API_KEY={explicit_secret}"
    )
    blocked_reason = (
        f"blocked because {_manager_root(manager)} {tmp_path} OPENAI_API_KEY={explicit_secret}"
    )
    manager.record_blocked_capability(blocked_name, blocked_reason)
    backend.probe_result = backend.probe_result.model_copy(
        update={
            "available": False,
            "reason": (
                f"probe failed at {_manager_root(manager)} {tmp_path} "
                f"OPENAI_API_KEY={explicit_secret}"
            ),
        }
    )

    manager.initialize()
    readiness = manager.finalize_readiness()
    events = manager.drain_startup_events()

    readiness_text = str(readiness.model_dump())
    events_text = str([event.model_dump() for event in events])
    _assert_sanitized_payload(
        readiness_text,
        str(_manager_root(manager)),
        str(tmp_path.resolve()),
        explicit_secret,
    )
    _assert_sanitized_payload(
        events_text,
        str(_manager_root(manager)),
        str(tmp_path.resolve()),
        explicit_secret,
    )


def test_manager_sanitizes_lexical_and_canonical_path_aliases(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name="recording")
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )

    workspace_lexical = "/var/folders/fake/workspace"
    workspace_canonical = "/private/var/folders/fake/workspace"
    manager_lexical = "/var/folders/fake/manager"
    manager_canonical = "/private/var/folders/fake/manager"
    manager._workspace_root_aliases = {workspace_lexical, workspace_canonical}
    manager._manager_root_aliases = {manager_lexical, manager_canonical}

    manager.record_blocked_capability(
        f"skip {workspace_lexical} {workspace_canonical}",
        f"reason {manager_lexical} {manager_canonical}",
    )
    backend.probe_result = backend.probe_result.model_copy(
        update={
            "available": False,
            "reason": (
                f"probe saw {workspace_lexical} {workspace_canonical} "
                f"and {manager_lexical} {manager_canonical}"
            ),
        }
    )

    manager.initialize()
    readiness = manager.finalize_readiness()
    events = manager.drain_startup_events()

    readiness_text = str(readiness.model_dump())
    events_text = str([event.model_dump() for event in events])
    _assert_sanitized_payload(
        readiness_text,
        workspace_lexical,
        workspace_canonical,
        manager_lexical,
        manager_canonical,
    )
    _assert_sanitized_payload(
        events_text,
        workspace_lexical,
        workspace_canonical,
        manager_lexical,
        manager_canonical,
    )
    assert "[WORKSPACE_ROOT]" in readiness_text
    assert "[PRIVATE_ROOT]" in readiness_text
    assert "[WORKSPACE_ROOT]" in events_text
    assert "[PRIVATE_ROOT]" in events_text


def test_manager_sanitizes_descendant_path_aliases_without_touching_similar_prefixes(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    backend = RecordingBackend(name="recording")
    manager = SandboxManager.create(
        settings=_settings(),
        debug=False,
        workspace_root=tmp_path,
        backend_override=backend,
    )

    workspace_lexical = "/var/folders/fake/workspace"
    workspace_canonical = "/private/var/folders/fake/workspace"
    manager_lexical = "/var/folders/fake/manager"
    manager_canonical = "/private/var/folders/fake/manager"
    workspace_descendants = (
        f"{workspace_lexical}/nested/secret.txt",
        f"{workspace_canonical}/nested/secret.txt",
    )
    manager_descendants = (
        f"{manager_lexical}/launch-x/home/token.json",
        f"{manager_canonical}/launch-x/home/token.json",
    )
    safe_similar_prefixes = (
        f"{workspace_lexical}-other/keep.txt",
        f"{manager_canonical}_other/keep.txt",
    )
    manager._workspace_root_aliases = {workspace_lexical, workspace_canonical}
    manager._manager_root_aliases = {manager_lexical, manager_canonical}

    manager.record_blocked_capability(
        f"blocked '{workspace_descendants[0]}' and {workspace_descendants[1]}. "
        f"OPENAI_API_KEY={manager_descendants[0]}",
        f"reason \"{manager_descendants[0]}\" plus {manager_descendants[1]}! "
        f"TOKEN={workspace_descendants[1]}",
    )
    backend.probe_result = backend.probe_result.model_copy(
        update={
            "available": False,
            "reason": (
                f"probe saw '{workspace_descendants[0]}' and \"{workspace_descendants[1]}\"; "
                f"then {manager_descendants[0]}. "
                f"Safe refs: {safe_similar_prefixes[0]} and {safe_similar_prefixes[1]}"
            ),
        }
    )

    manager.initialize()
    readiness = manager.finalize_readiness()
    events = manager.drain_startup_events()

    readiness_text = str(readiness.model_dump())
    events_text = str([event.model_dump() for event in events])
    _assert_sanitized_payload(
        readiness_text,
        workspace_lexical,
        workspace_canonical,
        manager_lexical,
        manager_canonical,
        *workspace_descendants,
        *manager_descendants,
    )
    _assert_sanitized_payload(
        events_text,
        workspace_lexical,
        workspace_canonical,
        manager_lexical,
        manager_canonical,
        *workspace_descendants,
        *manager_descendants,
    )
    assert safe_similar_prefixes[0] in readiness_text
    assert safe_similar_prefixes[1] in readiness_text
    assert safe_similar_prefixes[0] in events_text
    assert safe_similar_prefixes[1] in events_text
    assert "[WORKSPACE_ROOT]" in readiness_text
    assert "[PRIVATE_ROOT]" in readiness_text


def test_unsafe_mode_emits_startup_and_launch_events_once_without_run_duplication(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(mode="host_unsafe_dev_only"),
        debug=True,
        workspace_root=tmp_path,
        platform_name="Darwin",
    )

    startup = manager.drain_startup_events()
    assert [event.type for event in startup] == ["sandbox.unsafe_fallback_used"]
    manager.build_launch_spec(_request(tmp_path))
    launch_events = manager.drain_startup_events()
    assert [event.type for event in launch_events] == ["sandbox.unsafe_fallback_used"]
    assert manager.drain_startup_events() == ()


@pytest.mark.parametrize(
    ("platform_name", "expected_path"),
    [
        ("Darwin", "/usr/bin:/bin:/usr/sbin:/sbin"),
        ("Linux", "/usr/bin:/bin"),
    ],
)
def test_host_unsafe_backend_still_uses_common_environment_and_validates_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    expected_path: str,
) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(mode="host_unsafe_dev_only"),
        debug=True,
        workspace_root=tmp_path,
        platform_name=platform_name,
    )
    monkeypatch.setenv("PATH", "/tmp/host-path")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")

    spec = manager.build_launch_spec(_request(tmp_path, env_overrides={"custom_flag": "1"}))

    assert spec.backend_name == "host-unsafe-dev-only"
    assert spec.unsafe_fallback_used is True
    assert spec.env["PATH"] == expected_path
    assert spec.env["CUSTOM_FLAG"] == "1"
    assert "SSH_AUTH_SOCK" not in spec.env

    with pytest.raises(SandboxLaunchError, match="workspace root"):
        manager.build_launch_spec(_request(tmp_path / "other", cwd=tmp_path / "other"))
    with pytest.raises(SandboxLaunchError, match="cwd"):
        manager.build_launch_spec(_request(tmp_path, cwd=tmp_path.parent))


def test_unavailable_sandbox_controller_drains_startup_events_once() -> None:
    from sandbox_fakes import UnavailableSandboxController

    controller = UnavailableSandboxController()

    first = controller.drain_startup_events()
    second = controller.drain_startup_events()

    assert len(first) == 1
    assert second == ()


def test_unsafe_capability_event_does_not_block_unsafe_mode_readiness(tmp_path: Path) -> None:
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(mode="host_unsafe_dev_only"),
        debug=True,
        workspace_root=tmp_path,
        platform_name="Darwin",
    )

    manager.drain_startup_events()
    manager.record_unsafe_capability(
        "mcp_in_process_demo",
        "unsafe transport kept for development",
    )
    readiness = manager.finalize_readiness()
    events = manager.drain_startup_events()

    assert readiness.ready is True
    assert readiness.unsafe_fallback_active is True
    assert readiness.skipped_capabilities == {}
    assert [event.type for event in events] == ["sandbox.unsafe_fallback_used"]
    assert events[0].data["scope"] == "capability"
    assert events[0].data["capability"] == "mcp_in_process_demo"


def test_record_unsafe_capability_rejects_auto_mode(tmp_path: Path) -> None:
    from multiclaw.governance import SandboxPolicyError
    from multiclaw.governance.sandbox.manager import SandboxManager

    manager = SandboxManager.create(
        settings=_settings(mode="auto"),
        debug=False,
        workspace_root=tmp_path,
        platform_name="Darwin",
    )

    with pytest.raises(SandboxPolicyError, match="host_unsafe_dev_only"):
        manager.record_unsafe_capability(
            "mcp_in_process_demo",
            "unsafe transport kept for development",
        )
