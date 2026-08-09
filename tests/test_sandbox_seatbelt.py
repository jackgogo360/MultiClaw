from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from multiclaw.governance.sandbox.errors import SandboxLaunchError
from multiclaw.governance.sandbox.models import (
    SandboxEnvironment,
    SandboxExecRequest,
    SandboxProfilePolicy,
)


def _environment(tmp_path: Path) -> SandboxEnvironment:
    private_root = tmp_path / "private"
    home = private_root / "home"
    tmp = private_root / "tmp"
    home.mkdir(parents=True)
    tmp.mkdir(parents=True)
    return SandboxEnvironment(
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(tmp),
        },
        private_root=private_root,
        home=home,
        tmp=tmp,
    )


def _policy(
    *,
    name: str = "shell_workspace",
    profile_kind: str | None = None,
    workspace_mode: str = "rw",
    network_mode: str = "disabled",
    allow_subprocesses: bool = True,
    entrypoints: tuple[Path, ...] | None = None,
    runtime_read_only_paths: tuple[Path, ...] = (),
    write_protected_patterns: tuple[str, ...] = (".git",),
    read_hidden_patterns: tuple[str, ...] = (".env", ".env.*"),
) -> SandboxProfilePolicy:
    if profile_kind is None:
        profile_kind = {
            "shell_workspace": "shell",
            "code_exec_python": "code_exec",
            "mcp_stdio_local": "mcp_stdio",
        }.get(name, "shell")
    return SandboxProfilePolicy(
        name=name,
        profile_kind=profile_kind,
        workspace_mode=workspace_mode,
        network_mode=network_mode,
        allow_subprocesses=allow_subprocesses,
        entrypoints=entrypoints or (Path("/bin/sh"),),
        runtime_read_only_paths=runtime_read_only_paths,
        write_protected_patterns=write_protected_patterns,
        read_hidden_patterns=read_hidden_patterns,
    )


def _definition_pairs(args: tuple[str, ...], prefix: str) -> list[tuple[int, str]]:
    pairs: list[tuple[int, str]] = []
    for index, arg in enumerate(args):
        if arg == "-D" and index + 1 < len(args) and args[index + 1].startswith(prefix):
            pairs.append((index, args[index + 1]))
    return pairs


def test_seatbelt_backend_exposes_expected_name_and_binary() -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    backend = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec"))

    assert backend.name == "seatbelt"
    assert backend.binary == Path("/usr/bin/sandbox-exec")


def test_seatbelt_launch_spec_wraps_shell_requests_without_profile_interpolation(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command="printf 'sandbox check'",
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(),
        environment,
    )

    profile_text = launch.args[launch.args.index("-p") + 1]

    assert launch.executable == "/usr/bin/sandbox-exec"
    assert launch.args[-4:] == ("--", "/bin/sh", "-c", request.command)
    assert request.command not in profile_text
    assert str(workspace) not in profile_text
    assert max(index for index, arg in enumerate(launch.args) if arg.startswith("WORKSPACE=") or arg.startswith("PRIVATE_HOME=") or arg.startswith("PRIVATE_TMP=")) < launch.args.index("-p")


def test_seatbelt_launch_spec_preserves_exec_argv_targets(tmp_path: Path) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="python",
        profile_name="code_exec_python",
        mode="exec_argv",
        argv=("/usr/bin/python3", "-c", "print('ok')"),
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(
            name="code_exec_python",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/python3"),),
        ),
        environment,
    )

    assert launch.executable == "/usr/bin/sandbox-exec"
    assert launch.args[-4:] == ("--", "/usr/bin/python3", "-c", "print('ok')")


def test_seatbelt_launch_spec_uses_stable_parameter_pairs_for_canonical_paths(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request_runtime = tmp_path / "zzz-runtime"
    request_runtime.mkdir()
    policy_runtime = tmp_path / "aaa-runtime"
    policy_runtime.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="exec_argv",
        argv=("/usr/bin/env",),
        workspace_root=workspace / ".",
        cwd=workspace / ".",
        timeout_seconds=5.0,
        read_only_paths=(request_runtime / ".",),
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(
            entrypoints=(Path("/usr/bin/env"),),
            runtime_read_only_paths=(policy_runtime / ".",),
        ),
        environment,
    )

    profile_text = launch.args[launch.args.index("-p") + 1]
    expected_pairs = (
        "-D",
        "WORKSPACE=" + str(workspace.resolve()),
        "-D",
        "PRIVATE_HOME=" + str(environment.home.resolve()),
        "-D",
        "PRIVATE_TMP=" + str(environment.tmp.resolve()),
        "-D",
        "RUNTIME_ROOT_0=" + str(policy_runtime.resolve()),
        "-D",
        "RUNTIME_ROOT_1=" + str(request_runtime.resolve()),
    )

    first_define_index = launch.args.index("-D")
    assert launch.args[first_define_index : first_define_index + len(expected_pairs)] == expected_pairs
    assert max(
        index
        for index, arg in enumerate(launch.args)
        if arg.startswith("WORKSPACE=")
        or arg.startswith("PRIVATE_HOME=")
        or arg.startswith("PRIVATE_TMP=")
        or arg.startswith("RUNTIME_ROOT_")
    ) < launch.args.index("-p")
    assert str(workspace.resolve()) not in profile_text
    assert str(policy_runtime.resolve()) not in profile_text
    assert str(request_runtime.resolve()) not in profile_text


def test_zero_runtime_roots_still_define_all_runtime_slots_before_profile(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command=":",
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(),
        environment,
    )

    runtime_pairs = _definition_pairs(launch.args, "RUNTIME_ROOT_")

    assert len(runtime_pairs) == 16
    assert [value for _, value in runtime_pairs] == [
        f"RUNTIME_ROOT_{index}={environment.home.resolve()}"
        for index in range(16)
    ]
    assert max(index for index, _ in runtime_pairs) < launch.args.index("-p")


def test_one_runtime_root_uses_leading_slot_and_fills_remaining_slots_with_private_home(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    only_root = tmp_path / "one-runtime"
    only_root.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command=":",
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        read_only_paths=(only_root,),
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(),
        environment,
    )

    runtime_pairs = _definition_pairs(launch.args, "RUNTIME_ROOT_")

    assert len(runtime_pairs) == 16
    assert runtime_pairs[0][1] == "RUNTIME_ROOT_0=" + str(only_root.resolve())
    assert [value for _, value in runtime_pairs[1:]] == [
        f"RUNTIME_ROOT_{index}={environment.home.resolve()}"
        for index in range(1, 16)
    ]
    assert max(index for index, _ in runtime_pairs) < launch.args.index("-p")


def test_fewer_than_sixteen_runtime_roots_use_deterministic_slots_and_safe_fillers(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    roots = [tmp_path / name for name in ("z-root", "a-root", "m-root")]
    for root in roots:
        root.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command=":",
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        read_only_paths=tuple(root / "." for root in roots),
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(),
        environment,
    )

    runtime_pairs = _definition_pairs(launch.args, "RUNTIME_ROOT_")
    sorted_roots = sorted((root.resolve() for root in roots), key=str)

    assert len(runtime_pairs) == 16
    assert [value for _, value in runtime_pairs[:3]] == [
        f"RUNTIME_ROOT_{index}={path}"
        for index, path in enumerate(sorted_roots)
    ]
    assert [value for _, value in runtime_pairs[3:]] == [
        f"RUNTIME_ROOT_{index}={environment.home.resolve()}"
        for index in range(3, 16)
    ]
    assert [index for index, _ in runtime_pairs] == sorted(index for index, _ in runtime_pairs)
    assert max(index for index, _ in runtime_pairs) < launch.args.index("-p")


def test_sixteen_runtime_roots_preserve_all_exact_values(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    roots = [tmp_path / f"root-{index:02d}" for index in range(16)]
    for root in roots:
        root.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command=":",
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        read_only_paths=tuple(root for root in reversed(roots)),
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(),
        environment,
    )

    runtime_pairs = _definition_pairs(launch.args, "RUNTIME_ROOT_")
    sorted_roots = sorted((root.resolve() for root in roots), key=str)

    assert len(runtime_pairs) == 16
    assert [value for _, value in runtime_pairs] == [
        f"RUNTIME_ROOT_{index}={path}"
        for index, path in enumerate(sorted_roots)
    ]
    assert max(index for index, _ in runtime_pairs) < launch.args.index("-p")


def test_static_seatbelt_profiles_are_reviewable_and_semantically_distinct() -> None:
    from multiclaw.governance.sandbox.seatbelt_profiles import SEATBELT_PROFILES

    assert set(SEATBELT_PROFILES) == {
        "shell_workspace",
        "code_exec_python",
        "mcp_stdio_local",
    }

    shell_profile = SEATBELT_PROFILES["shell_workspace"].profile_text
    code_profile = SEATBELT_PROFILES["code_exec_python"].profile_text
    mcp_profile = SEATBELT_PROFILES["mcp_stdio_local"].profile_text

    for profile_text in (shell_profile, code_profile, mcp_profile):
        assert "(version 1)" in profile_text
        assert "(deny default)" in profile_text
        assert '(param "WORKSPACE")' in profile_text
        assert '(param "PRIVATE_HOME")' in profile_text
        assert '(param "PRIVATE_TMP")' in profile_text
        assert '(param "RUNTIME_ROOT_0")' in profile_text
        assert '(param "RUNTIME_ROOT_15")' in profile_text
        assert ".git" in profile_text
        assert ".env" in profile_text
        assert "process-exec" in profile_text

    assert "(allow file-write*" in shell_profile
    assert "(allow file-write*" in code_profile
    assert "(allow file-read*" in mcp_profile
    assert "(deny network*)" in shell_profile
    assert "(deny network*)" in code_profile
    assert "(deny network*)" in mcp_profile
    assert "(allow process-fork)" in shell_profile
    assert "(deny process-fork)" in code_profile
    assert "(deny process-fork)" in mcp_profile


def test_mcp_profile_keeps_workspace_read_only_but_private_runtime_writeable() -> None:
    from multiclaw.governance.sandbox.seatbelt_profiles import SEATBELT_PROFILES

    profile_text = SEATBELT_PROFILES["mcp_stdio_local"].profile_text

    assert '(allow file-read* (subpath (param "WORKSPACE")))' in profile_text
    assert '(allow file-write* (subpath (param "PRIVATE_HOME")))' in profile_text
    assert '(allow file-write* (subpath (param "PRIVATE_TMP")))' in profile_text
    assert '(allow file-write* (subpath (param "WORKSPACE")))' not in profile_text
    assert '(deny file-read* (regex #".*/\\.env(\\..*)?$"))' in profile_text
    assert '(deny file-write* (regex #".*/\\.git($|/.*)"))' in profile_text


@pytest.mark.parametrize("workspace_mode", ["ro", "rw"])
@pytest.mark.parametrize("network_mode", ["disabled", "inherit"])
@pytest.mark.parametrize("allow_subprocesses", [False, True])
def test_seatbelt_renders_dynamic_mcp_policy_combinations(
    tmp_path: Path,
    workspace_mode: str,
    network_mode: str,
    allow_subprocesses: bool,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="mcp",
        profile_name="mcp_stdio_local",
        mode="exec_argv",
        argv=("/usr/bin/env",),
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        network_mode=network_mode,
        workspace_mode=workspace_mode,
        allow_subprocesses=allow_subprocesses,
        mcp_server_name="demo",
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(
            name="mcp_stdio_local",
            workspace_mode=workspace_mode,
            network_mode=network_mode,
            allow_subprocesses=allow_subprocesses,
            entrypoints=(Path("/usr/bin/env"),),
        ),
        environment,
    )
    profile_text = launch.args[launch.args.index("-p") + 1]

    if workspace_mode == "rw":
        assert '(allow file-write* (subpath (param "WORKSPACE")))' in profile_text
    else:
        assert '(allow file-write* (subpath (param "WORKSPACE")))' not in profile_text
    if network_mode == "disabled":
        assert "(deny network*)" in profile_text
        assert "(allow network*)" not in profile_text
    else:
        assert "(allow network*)" in profile_text
    if allow_subprocesses:
        assert "(allow process-fork)" in profile_text
        assert "(deny process-fork)" not in profile_text
    else:
        assert "(deny process-fork)" in profile_text


def test_seatbelt_treats_configured_custom_mcp_profile_as_reviewed_mcp_policy(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="mcp",
        profile_name="custom_mcp_profile",
        mode="exec_argv",
        argv=("/usr/bin/env",),
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        network_mode="disabled",
        workspace_mode="ro",
        allow_subprocesses=False,
        mcp_server_name="demo",
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(
            name="custom_mcp_profile",
            profile_kind="mcp_stdio",
            workspace_mode="ro",
            network_mode="disabled",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/env"),),
        ),
        environment,
    )
    profile_text = launch.args[launch.args.index("-p") + 1]

    assert "(deny network*)" in profile_text
    assert "(deny process-fork)" in profile_text
    assert '(allow file-write* (subpath (param "WORKSPACE")))' not in profile_text


def test_seatbelt_rejects_unknown_profile_even_with_mcp_server_name(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="mcp",
        profile_name="custom_unknown_profile",
        mode="exec_argv",
        argv=("/usr/bin/env",),
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        mcp_server_name="demo",
    )

    with pytest.raises(SandboxLaunchError, match="unsupported seatbelt profile"):
        SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
            request,
            _policy(
                name="custom_unknown_profile",
                profile_kind="shell",
                entrypoints=(Path("/usr/bin/env"),),
            ),
            environment,
        )


def test_seatbelt_shell_profile_with_mcp_server_name_keeps_shell_semantics(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command=":",
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        mcp_server_name="demo",
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(name="shell_workspace", profile_kind="shell"),
        environment,
    )
    profile_text = launch.args[launch.args.index("-p") + 1]

    assert launch.args[-3:] == ("/bin/sh", "-c", ":")
    assert "(allow process-fork)" in profile_text
    assert "(deny network*)" in profile_text
    assert '(allow file-write* (subpath (param "WORKSPACE")))' in profile_text


def test_seatbelt_rejects_network_or_capability_mismatches(tmp_path: Path) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="exec_argv",
        argv=("/usr/bin/env",),
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
    )
    backend = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec"))

    invalid_network = _policy()
    object.__setattr__(invalid_network, "network_mode", "bogus")
    with pytest.raises(SandboxLaunchError, match="network"):
        backend.build_launch_spec(request, invalid_network, environment)

    no_subprocess_shell = _policy(allow_subprocesses=False)
    with pytest.raises(SandboxLaunchError, match="subprocess"):
        backend.build_launch_spec(request, no_subprocess_shell, environment)

    unexpected_hidden = _policy(read_hidden_patterns=(".secrets",))
    with pytest.raises(SandboxLaunchError, match="hidden"):
        backend.build_launch_spec(request, unexpected_hidden, environment)

    unexpected_protected = _policy(write_protected_patterns=(".cache",))
    with pytest.raises(SandboxLaunchError, match="protected"):
        backend.build_launch_spec(request, unexpected_protected, environment)


def test_code_exec_profile_requires_child_creation_denial(tmp_path: Path) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="python",
        profile_name="code_exec_python",
        mode="exec_argv",
        argv=("/usr/bin/python3", "-c", "print('ok')"),
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
    )
    backend = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec"))

    launch = backend.build_launch_spec(
        request,
        _policy(
            name="code_exec_python",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/python3"),),
        ),
        environment,
    )
    profile_text = launch.args[launch.args.index("-p") + 1]

    assert "(deny process-fork)" in profile_text

    with pytest.raises(SandboxLaunchError, match="subprocess"):
        backend.build_launch_spec(
            request,
            _policy(
                name="code_exec_python",
                allow_subprocesses=True,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
            environment,
        )


def test_exec_argv_requires_canonical_entrypoint_match_but_preserves_original_argv(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="python",
        profile_name="code_exec_python",
        mode="exec_argv",
        argv=("/usr/bin/../bin/python3", "-c", "print('ok')"),
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
    )

    launch = SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
        request,
        _policy(
            name="code_exec_python",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/python3"),),
        ),
        environment,
    )

    assert launch.args[-4:] == ("--", "/usr/bin/../bin/python3", "-c", "print('ok')")


@pytest.mark.parametrize(
    ("request_factory", "policy_factory", "pattern"),
    [
        pytest.param(
            lambda workspace: SandboxExecRequest(
                tool_name="python",
                profile_name="code_exec_python",
                mode="shell_string",
                command="echo nope",
                workspace_root=workspace,
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            lambda: _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
            "entrypoint",
            id="code-policy-rejects-shell-wrapper",
        ),
        pytest.param(
            lambda workspace: SandboxExecRequest(
                tool_name="shell",
                profile_name="shell_workspace",
                mode="shell_string",
                command=":",
                workspace_root=workspace,
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            lambda: _policy(entrypoints=(Path("/usr/bin/python3"),)),
            "entrypoint",
            id="shell-wrapper-must-be-allowed",
        ),
        pytest.param(
            lambda workspace: SandboxExecRequest(
                tool_name="python",
                profile_name="code_exec_python",
                mode="exec_argv",
                argv=("/bin/sh", "-c", "echo nope"),
                workspace_root=workspace,
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            lambda: _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
            "entrypoint",
            id="exec-argv-mismatch-rejected",
        ),
        pytest.param(
            lambda workspace: SandboxExecRequest(
                tool_name="python",
                profile_name="code_exec_python",
                mode="exec_argv",
                argv=("python3", "-c", "print('ok')"),
                workspace_root=workspace,
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            lambda: _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
            "entrypoint",
            id="bare-executable-rejected",
        ),
        pytest.param(
            lambda workspace: SandboxExecRequest(
                tool_name="python",
                profile_name="code_exec_python",
                mode="exec_argv",
                argv=("./python3", "-c", "print('ok')"),
                workspace_root=workspace,
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            lambda: _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
            "entrypoint",
            id="relative-executable-rejected",
        ),
        pytest.param(
            lambda workspace: SandboxExecRequest(
                tool_name="python",
                profile_name="code_exec_python",
                mode="exec_argv",
                argv=("/usr/bin/python3", "-c", "print('ok')"),
                workspace_root=workspace,
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            lambda: _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(),
            ),
            "entrypoint",
            id="empty-entrypoints-rejected",
        ),
        pytest.param(
            lambda workspace: SandboxExecRequest(
                tool_name="python",
                profile_name="code_exec_python",
                mode="exec_argv",
                argv=("/usr/bin/python3", "-c", "print('ok')"),
                workspace_root=workspace,
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            lambda: _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(Path("python3"),),
            ),
            "entrypoint",
            id="relative-policy-entrypoint-rejected",
        ),
    ],
)
def test_seatbelt_fails_closed_on_invalid_entrypoint_configurations(
    tmp_path: Path,
    request_factory,
    policy_factory,
    pattern: str,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = _environment(tmp_path)

    with pytest.raises(SandboxLaunchError, match=pattern) as excinfo:
        SeatbeltBackend(binary=Path("/usr/bin/sandbox-exec")).build_launch_spec(
            request_factory(workspace),
            policy_factory(),
            environment,
        )

    message = str(excinfo.value)
    assert str(workspace) not in message
    assert "echo nope" not in message


def test_probe_short_circuits_when_binary_missing_or_not_executable(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    calls: list[object] = []

    def unexpected_runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("runner should not be called")

    backend = SeatbeltBackend(
        binary=tmp_path / "missing-sandbox-exec",
        subprocess_run=unexpected_runner,
    )

    result = backend.probe(tmp_path / "workspace", (_policy(),))

    assert result.available is False
    assert result.backend_name == "seatbelt"
    assert result.reason
    assert calls == []


def test_probe_short_circuits_when_binary_is_not_executable(tmp_path: Path) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    calls: list[object] = []
    binary = tmp_path / "sandbox-exec"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o600)

    def unexpected_runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("runner should not be called")

    backend = SeatbeltBackend(
        binary=binary,
        subprocess_run=unexpected_runner,
    )

    result = backend.probe(tmp_path / "workspace", (_policy(),))

    assert result.available is False
    assert result.backend_name == "seatbelt"
    assert result.reason
    assert "sandbox-exec" in result.reason
    assert str(binary) not in result.reason
    assert calls == []


def test_probe_interprets_behavioral_checks_with_exec_form_subprocess_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    calls: list[dict[str, object]] = []
    responses = iter(
        [
            subprocess.CompletedProcess(args=["shell-ok"], returncode=0, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["code-ok"], returncode=0, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["outside"], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["network"], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["hidden"], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["protected"], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["child"], returncode=1, stdout=b"", stderr=b""),
        ]
    )

    def fake_runner(args, **kwargs):
        calls.append({"args": args, **kwargs})
        return next(responses)

    backend = SeatbeltBackend(
        binary=Path("/usr/bin/sandbox-exec"),
        probe_timeout_seconds=3.0,
        subprocess_run=fake_runner,
    )
    monkeypatch.setattr(
        backend,
        "_probe_binary_ready",
        lambda: (True, Path("/usr/bin/sandbox-exec"), ""),
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policies = (
        _policy(entrypoints=(Path("/bin/sh"),)),
        _policy(
            name="code_exec_python",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/python3"),),
        ),
    )

    result = backend.probe(workspace, policies)

    assert result.available is True
    assert result.capabilities == {
        "allowed_execution": True,
        "outside_workspace_write_denied": True,
        "network_denied": True,
        "hidden_env_read_denied": True,
        "protected_git_write_denied": True,
        "child_creation_denied": True,
    }
    assert len(calls) == 7
    assert all(isinstance(call["args"], list) for call in calls)
    assert all(call["shell"] is False for call in calls)
    assert all(call["capture_output"] is True for call in calls)
    assert all(call["check"] is False for call in calls)
    assert all(call["timeout"] == 3.0 for call in calls)
    assert all(call["env"]["PATH"] == "/usr/bin:/bin" for call in calls)
    assert all(call["args"][0] == "/usr/bin/sandbox-exec" for call in calls)
    assert calls[0]["args"][-4:] == ["--", "/bin/sh", "-c", ":"]
    assert calls[1]["args"][-4:] == ["--", "/usr/bin/python3", "-c", "pass"]
    assert all("mcp_stdio_local" not in str(call["args"]) for call in calls)


def test_probe_uses_disposable_workspace_and_does_not_mutate_caller_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    workspace = tmp_path / "caller-workspace"
    workspace.mkdir()
    calls: list[list[str]] = []
    responses = iter(
        [
            subprocess.CompletedProcess(args=["shell"], returncode=0, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["code"], returncode=0, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["outside"], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["network"], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["hidden"], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["protected"], returncode=1, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(args=["child"], returncode=1, stdout=b"", stderr=b""),
        ]
    )

    def fake_runner(args, **kwargs):
        calls.append(list(args))
        return next(responses)

    backend = SeatbeltBackend(
        binary=Path("/usr/bin/sandbox-exec"),
        subprocess_run=fake_runner,
    )
    monkeypatch.setattr(
        backend,
        "_probe_binary_ready",
        lambda: (True, Path("/usr/bin/sandbox-exec"), ""),
    )

    result = backend.probe(
        workspace,
        (
            _policy(entrypoints=(Path("/bin/sh"),)),
            _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
        ),
    )

    assert result.available is True
    assert list(workspace.iterdir()) == []
    assert all(str(workspace / ".git") not in " ".join(args) for args in calls)


def test_probe_reports_failed_capability_without_leaking_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    secret = "seatbelt-secret"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend = SeatbeltBackend(
        binary=Path("/usr/bin/sandbox-exec"),
        probe_timeout_seconds=1.5,
        subprocess_run=lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["ok"],
            returncode=0,
            stdout=b"",
            stderr=secret.encode("utf-8"),
        ),
    )
    monkeypatch.setattr(
        backend,
        "_probe_binary_ready",
        lambda: (True, Path("/usr/bin/sandbox-exec"), ""),
    )

    result = backend.probe(
        workspace,
        (
            _policy(entrypoints=(Path("/bin/sh"),)),
            _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
        ),
    )

    assert result.available is False
    assert result.capabilities["allowed_execution"] is True
    assert result.capabilities["outside_workspace_write_denied"] is False
    assert "outside_workspace_write_denied" in result.reason
    assert secret not in result.reason
    assert str(workspace) not in result.reason


def test_probe_treats_timeout_as_unavailable_with_capability_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    def timeout_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    backend = SeatbeltBackend(
        binary=Path("/usr/bin/sandbox-exec"),
        probe_timeout_seconds=2.5,
        subprocess_run=timeout_runner,
    )
    monkeypatch.setattr(
        backend,
        "_probe_binary_ready",
        lambda: (True, Path("/usr/bin/sandbox-exec"), ""),
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = backend.probe(
        workspace,
        (
            _policy(entrypoints=(Path("/bin/sh"),)),
            _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
        ),
    )

    assert result.available is False
    assert result.capabilities["allowed_execution"] is False
    assert "allowed_execution" in result.reason
    assert "2.5" not in result.reason
    assert "sandbox-exec" not in result.reason


def test_probe_uses_exec_form_subprocess_invocation_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    from multiclaw.governance.sandbox.seatbelt import SeatbeltBackend

    captured: list[dict[str, object]] = []

    def fake_runner(args, **kwargs):
        captured.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    backend = SeatbeltBackend(
        binary=Path("/usr/bin/sandbox-exec"),
        subprocess_run=fake_runner,
    )
    monkeypatch.setattr(
        backend,
        "_probe_binary_ready",
        lambda: (True, Path("/usr/bin/sandbox-exec"), ""),
    )

    proof = backend._run_probe_command(
        args=["/usr/bin/sandbox-exec", "--", "/usr/bin/true"],
        env={"PATH": "/usr/bin:/bin"},
    )

    assert proof.returncode == 0
    assert captured[0]["args"] == ["/usr/bin/sandbox-exec", "--", "/usr/bin/true"]
    assert captured[0]["shell"] is False
    assert captured[0]["capture_output"] is True
    assert captured[0]["check"] is False
    assert captured[0]["timeout"] == backend.probe_timeout_seconds
    assert captured[0]["env"] == {"PATH": "/usr/bin:/bin"}
    assert "cwd" not in captured[0]
