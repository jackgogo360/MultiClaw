from __future__ import annotations

import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

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
            "VISIBLE_FLAG": "visible",
            "API_TOKEN": "super-secret-token",
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
    default_entrypoints = {
        "shell_workspace": (Path("/bin/sh"),),
        "code_exec_python": (Path("/usr/bin/python3"),),
        "mcp_stdio_local": (Path("/usr/bin/env"),),
    }
    return SandboxProfilePolicy(
        name=name,
        profile_kind=profile_kind,
        workspace_mode=workspace_mode,
        network_mode=network_mode,
        allow_subprocesses=allow_subprocesses,
        entrypoints=default_entrypoints[name] if entrypoints is None else entrypoints,
        runtime_read_only_paths=runtime_read_only_paths,
        write_protected_patterns=write_protected_patterns,
        read_hidden_patterns=read_hidden_patterns,
    )


def _config_path(launch_args: tuple[str, ...]) -> Path:
    config_index = launch_args.index("--config")
    return Path(launch_args[config_index + 1])


def _config_text(launch_args: tuple[str, ...]) -> str:
    return _config_path(launch_args).read_text(encoding="utf-8")


def _workspace_tree(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    (workspace / "project").mkdir()
    (workspace / ".env").write_text("TOKEN=workspace-secret\n", encoding="utf-8")
    (workspace / ".env.local").write_text("TOKEN=workspace-secret-local\n", encoding="utf-8")
    return workspace


def test_protobuf_quote_escapes_string_body_characters() -> None:
    from multiclaw.governance.sandbox.nsjail import protobuf_quote

    assert protobuf_quote('a\\b"c\nd\re') == 'a\\\\b\\"c\\nd\\re'


def test_nsjail_backend_missing_module_red_guard_removed() -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    assert NsJailBackend.name == "nsjail"


def test_nsjail_launch_spec_wraps_shell_string_without_shell_invocation(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command="printf 'sandbox check'\n",
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        correlation_id='corr-123-"ignored"',
    )

    launch = NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
        request,
        _policy(),
        environment,
    )
    config_text = _config_text(launch.args)
    config_path = _config_path(launch.args)
    config_mode = stat.S_IMODE(config_path.stat().st_mode)

    assert launch.executable == "/usr/bin/nsjail"
    assert launch.args[:3] == ("--config", str(config_path), "--")
    assert launch.args[3:] == ("/bin/sh", "-c", request.command)
    assert launch.cwd == workspace.resolve()
    assert launch.env == environment.env
    assert launch.stdin_bytes is None
    assert launch.backend_name == "nsjail"
    assert config_path.parent == environment.private_root.resolve()
    assert config_path.name.startswith("nsjail-")
    assert "corr-123" not in config_path.name
    assert "sandbox check" not in config_path.name
    assert config_mode == 0o600
    assert request.command not in config_text
    assert "super-secret-token" not in config_text
    assert "VISIBLE_FLAG=visible" not in config_text


def test_nsjail_launch_spec_rejects_noncanonical_exec_entrypoint_path(tmp_path: Path) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    environment = _environment(tmp_path)
    symlink_entrypoint = tmp_path / "python-link"
    if symlink_entrypoint.exists():
        symlink_entrypoint.unlink()
    symlink_entrypoint.symlink_to("/usr/bin/python3")
    request = SandboxExecRequest(
        tool_name="python",
        profile_name="code_exec_python",
        mode="exec_argv",
        argv=(str(symlink_entrypoint), "-c", "print('ok')"),
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
    )

    with pytest.raises(SandboxLaunchError, match="canonical entrypoint"):
        NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
            request,
            _policy(
                name="code_exec_python",
                network_mode="disabled",
                allow_subprocesses=False,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
            environment,
        )


def test_nsjail_launch_spec_preserves_exact_canonical_exec_argv(tmp_path: Path) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
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

    launch = NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
        request,
        _policy(
            name="code_exec_python",
            network_mode="disabled",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/python3"),),
        ),
        environment,
    )

    assert launch.args[3:] == request.argv


def test_nsjail_config_contains_reviewed_namespace_mount_and_rlimit_fragments(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend, protobuf_quote

    workspace = _workspace_tree(tmp_path)
    runtime_root = tmp_path / 'runtime"root\\with\nline'
    runtime_root.mkdir()
    weird_workspace = tmp_path / 'workspace"quoted\\slash\nline'
    workspace.rename(weird_workspace)
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command=":",
        workspace_root=weird_workspace,
        cwd=weird_workspace,
        timeout_seconds=5.0,
        read_only_paths=(runtime_root, runtime_root / ".",),
    )

    launch = NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
        request,
        _policy(runtime_read_only_paths=(runtime_root,)),
        environment,
    )
    config_text = _config_text(launch.args)

    assert "mode: ONCE" in config_text
    assert 'hostname: "multiclaw"' in config_text
    assert f'cwd: "{protobuf_quote(str(weird_workspace.resolve()))}"' in config_text
    assert "keep_env: true" in config_text
    assert "keep_caps: false" in config_text
    assert "disable_no_new_privs: false" in config_text
    assert "clone_newuser: true" in config_text
    assert "clone_newns: true" in config_text
    assert "clone_newpid: true" in config_text
    assert "clone_newipc: true" in config_text
    assert "clone_newuts: true" in config_text
    assert "clone_newnet: true" in config_text
    assert "mount_proc: true" in config_text
    assert 'uidmap {' in config_text
    assert 'gidmap {' in config_text
    assert 'inside_id: "0"' in config_text
    assert f'outside_id: "{os.getuid()}"' in config_text
    assert f'outside_id: "{os.getgid()}"' in config_text
    assert 'rlimit_as_type: VALUE' in config_text
    assert 'rlimit_cpu_type: VALUE' in config_text
    assert 'rlimit_fsize_type: VALUE' in config_text
    assert 'rlimit_nofile_type: VALUE' in config_text
    assert 'rlimit_nproc_type: VALUE' in config_text
    assert "cgroup_pids_max:" not in config_text
    assert 'mount {' in config_text
    assert f'src: "{protobuf_quote(str(weird_workspace.resolve()))}"' in config_text
    assert f'src: "{protobuf_quote(str(runtime_root.resolve()))}"' in config_text
    assert f'src: "{protobuf_quote(str(environment.home.resolve()))}"' in config_text
    assert f'src: "{protobuf_quote(str(environment.tmp.resolve()))}"' in config_text
    assert f'dst: "{protobuf_quote(str((weird_workspace / ".git").resolve()))}"' in config_text
    assert f'dst: "{protobuf_quote(str((weird_workspace / ".env").resolve()))}"' in config_text
    assert f'dst: "{protobuf_quote(str((weird_workspace / ".env.local").resolve()))}"' in config_text
    assert config_text.count(f'src: "{protobuf_quote(str(runtime_root.resolve()))}"') == 1


def test_nsjail_mount_type_semantics_use_real_filesystem_types(tmp_path: Path) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    environment = _environment(tmp_path)
    runtime_file = tmp_path / "runtime-file.txt"
    runtime_file.write_text("runtime\n", encoding="utf-8")

    launch = NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
        SandboxExecRequest(
            tool_name="shell",
            profile_name="shell_workspace",
            mode="shell_string",
            command=":",
            workspace_root=workspace,
            cwd=workspace,
            timeout_seconds=5.0,
            read_only_paths=(runtime_file,),
        ),
        _policy(),
        environment,
    )
    config_text = _config_text(launch.args)

    assert 'src: "/dev/null"\n  dst: "/dev/null"\n  is_bind: true\n  rw: false\n  is_dir: false' in config_text
    assert 'src: "/dev/urandom"\n  dst: "/dev/urandom"\n  is_bind: true\n  rw: false\n  is_dir: false' in config_text
    assert f'src: "{workspace.resolve()}"\n  dst: "{workspace.resolve()}"\n  is_bind: true\n  rw: true\n  is_dir: true' in config_text
    assert f'src: "{runtime_file.resolve()}"\n  dst: "{runtime_file.resolve()}"\n  is_bind: true\n  rw: false\n  is_dir: false' in config_text


@pytest.mark.parametrize(
    ("profile_name", "workspace_mode", "network_mode", "allow_subprocesses", "entrypoint"),
    [
        ("shell_workspace", "rw", "disabled", True, Path("/bin/sh")),
        ("code_exec_python", "rw", "disabled", False, Path("/usr/bin/python3")),
        ("mcp_stdio_local", "ro", "disabled", False, Path("/usr/bin/env")),
    ],
)
def test_nsjail_profile_templates_match_reviewed_policy_shapes(
    tmp_path: Path,
    profile_name: str,
    workspace_mode: str,
    network_mode: str,
    allow_subprocesses: bool,
    entrypoint: Path,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend
    from multiclaw.governance.sandbox.nsjail_profiles import NSJAIL_PROFILES

    workspace = _workspace_tree(tmp_path)
    environment = _environment(tmp_path)
    request = SandboxExecRequest(
        tool_name="tool",
        profile_name=profile_name,
        mode="exec_argv" if profile_name != "shell_workspace" else "shell_string",
        command=":" if profile_name == "shell_workspace" else None,
        argv=(str(entrypoint), "-c", "pass") if profile_name == "code_exec_python" else ((str(entrypoint),) if profile_name == "mcp_stdio_local" else None),
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        network_mode=network_mode,
        workspace_mode=workspace_mode,
        allow_subprocesses=allow_subprocesses,
    )
    policy = _policy(
        name=profile_name,
        workspace_mode=workspace_mode,
        network_mode=network_mode,
        allow_subprocesses=allow_subprocesses,
        entrypoints=(entrypoint,),
    )

    launch = NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
        request,
        policy,
        environment,
    )
    config_text = _config_text(launch.args)
    template = NSJAIL_PROFILES[profile_name]

    assert template.name == profile_name
    assert template.workspace_mode == workspace_mode
    assert template.network_mode == network_mode
    assert template.allow_subprocesses is allow_subprocesses
    workspace_dst = f'dst: "{workspace.resolve()}"'
    if workspace_mode == "rw":
        assert workspace_dst in config_text
        assert "rw: true" in config_text
    else:
        assert workspace_dst in config_text
        assert "rw: false" in config_text
    if network_mode == "disabled":
        assert "clone_newnet: true" in config_text
    else:
        assert "clone_newnet: false" in config_text
    if allow_subprocesses:
        assert "cgroup_pids_max:" not in config_text
        assert "ERRNO(EPERM) { clone, clone3, fork, vfork, unshare }" not in config_text
    else:
        assert "cgroup_pids_max:" not in config_text
        assert "ERRNO(EPERM) { clone, clone3, fork, vfork, unshare }" in config_text


def test_nsjail_rejects_policy_template_mismatches_and_unsupported_patterns(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    environment = _environment(tmp_path)
    backend = NsJailBackend(binary=Path("/usr/bin/nsjail"))
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command=":",
        workspace_root=workspace,
        cwd=workspace,
        timeout_seconds=5.0,
        network_mode="inherit",
    )

    with pytest.raises(SandboxLaunchError, match="request network_mode"):
        backend.build_launch_spec(request, _policy(), environment)

    with pytest.raises(SandboxLaunchError, match="hidden path policy"):
        backend.build_launch_spec(
            request.model_copy(update={"network_mode": None}),
            _policy(read_hidden_patterns=(".secrets",)),
            environment,
        )

    with pytest.raises(SandboxLaunchError, match="protected path policy"):
        backend.build_launch_spec(
            request.model_copy(update={"network_mode": None}),
            _policy(write_protected_patterns=(".venv",)),
            environment,
        )

    with pytest.raises(SandboxLaunchError, match="entrypoint policy is invalid"):
        backend.build_launch_spec(
            request.model_copy(update={"network_mode": None}),
            _policy(entrypoints=()),
            environment,
        )


@pytest.mark.parametrize("workspace_mode", ["ro", "rw"])
@pytest.mark.parametrize("network_mode", ["disabled", "inherit"])
@pytest.mark.parametrize("allow_subprocesses", [False, True])
def test_nsjail_renders_dynamic_mcp_policy_combinations(
    tmp_path: Path,
    workspace_mode: str,
    network_mode: str,
    allow_subprocesses: bool,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
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

    launch = NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
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
    config_text = _config_text(launch.args)

    workspace_dst = f'dst: "{workspace.resolve()}"'
    assert workspace_dst in config_text
    if workspace_mode == "rw":
        assert "rw: true" in config_text
    else:
        assert "rw: false" in config_text
    if network_mode == "disabled":
        assert "clone_newnet: true" in config_text
    else:
        assert "clone_newnet: false" in config_text
    if allow_subprocesses:
        assert "ERRNO(EPERM) { clone, clone3, fork, vfork, unshare }" not in config_text
        assert "rlimit_nproc: 1024" in config_text
    else:
        assert "ERRNO(EPERM) { clone, clone3, fork, vfork, unshare }" in config_text
        assert "rlimit_nproc: 1" in config_text


def test_nsjail_treats_configured_custom_mcp_profile_as_reviewed_mcp_policy(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
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

    launch = NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
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
    config_text = _config_text(launch.args)

    assert "clone_newnet: true" in config_text
    assert "ERRNO(EPERM) { clone, clone3, fork, vfork, unshare }" in config_text
    assert "rlimit_nproc: 1" in config_text


def test_nsjail_rejects_unknown_profile_even_with_mcp_server_name(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
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

    with pytest.raises(SandboxLaunchError, match="unsupported nsjail profile"):
        NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
            request,
            _policy(
                name="custom_unknown_profile",
                profile_kind="shell",
                entrypoints=(Path("/usr/bin/env"),),
            ),
            environment,
        )


def test_nsjail_shell_profile_with_mcp_server_name_keeps_shell_semantics(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
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

    launch = NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
        request,
        _policy(name="shell_workspace", profile_kind="shell"),
        environment,
    )
    config_text = _config_text(launch.args)

    assert launch.args[3:] == ("/bin/sh", "-c", ":")
    assert "clone_newnet: true" in config_text
    assert "ERRNO(EPERM) { clone, clone3, fork, vfork, unshare }" not in config_text
    assert "rw: true" in config_text


def test_nsjail_rejects_relative_or_nul_paths_and_too_many_runtime_roots(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    environment = _environment(tmp_path)
    backend = NsJailBackend(binary=Path("/usr/bin/nsjail"))

    with pytest.raises(SandboxLaunchError, match="workspace_root must be absolute"):
        backend.build_launch_spec(
            SandboxExecRequest(
                tool_name="shell",
                profile_name="shell_workspace",
                mode="shell_string",
                command=":",
                workspace_root=Path("relative"),
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            _policy(),
            environment,
        )

    with pytest.raises(SandboxLaunchError, match="contains a NUL byte"):
        backend.build_launch_spec(
            SandboxExecRequest(
                tool_name="shell",
                profile_name="shell_workspace",
                mode="shell_string",
                command=":",
                workspace_root=workspace,
                cwd=Path("/tmp/\x00bad"),
                timeout_seconds=5.0,
            ),
            _policy(),
            environment,
        )

    roots = []
    for index in range(17):
        root = tmp_path / f"root-{index}"
        root.mkdir()
        roots.append(root)

    with pytest.raises(ValidationError, match="read_only_paths cannot exceed 16 entries"):
        SandboxExecRequest(
            tool_name="shell",
            profile_name="shell_workspace",
            mode="shell_string",
            command=":",
            workspace_root=workspace,
            cwd=workspace,
            timeout_seconds=5.0,
            read_only_paths=tuple(roots),
        )


def test_nsjail_shell_wrapper_requires_bin_sh_policy_and_exec_entrypoint_is_allowed(
    tmp_path: Path,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    environment = _environment(tmp_path)
    backend = NsJailBackend(binary=Path("/usr/bin/nsjail"))

    with pytest.raises(SandboxLaunchError, match="entrypoint is not allowed"):
        backend.build_launch_spec(
            SandboxExecRequest(
                tool_name="shell",
                profile_name="shell_workspace",
                mode="shell_string",
                command=":",
                workspace_root=workspace,
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            _policy(entrypoints=(Path("/usr/bin/env"),)),
            environment,
        )

    with pytest.raises(SandboxLaunchError, match="entrypoint is invalid"):
        backend.build_launch_spec(
            SandboxExecRequest(
                tool_name="exec",
                profile_name="code_exec_python",
                mode="exec_argv",
                argv=("python3", "-V"),
                workspace_root=workspace,
                cwd=workspace,
                timeout_seconds=5.0,
            ),
            _policy(
                name="code_exec_python",
                allow_subprocesses=False,
                entrypoints=(Path("/usr/bin/python3"),),
            ),
            environment,
        )


@pytest.mark.parametrize("path_name", [".git", ".env", ".env.local"])
def test_nsjail_rejects_workspace_symlink_matches_for_protected_and_hidden_paths(
    tmp_path: Path,
    path_name: str,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    outside_file = outside_root / "secret.txt"
    outside_file.write_text("secret\n", encoding="utf-8")
    if path_name == ".git":
        target = outside_root / "git-dir"
        target.mkdir()
    else:
        target = outside_file
    (workspace / path_name).symlink_to(target)

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

    with pytest.raises(SandboxLaunchError, match="workspace path match is invalid"):
        NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
            request,
            _policy(),
            environment,
        )


def test_nsjail_probe_rejects_missing_nonfile_nonexecutable_and_noncanonical_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    policies = (_policy(), _policy(name="code_exec_python", allow_subprocesses=False))
    calls: list[tuple[str, ...]] = []

    def never_run(*args, **kwargs):
        calls.append(tuple(kwargs.get("args") or args[0]))
        return subprocess.CompletedProcess(args[0], 0, b"", b"")

    missing = NsJailBackend(binary=tmp_path / "missing-nsjail", subprocess_run=never_run)
    missing_result = missing.probe(workspace, policies)
    assert missing_result.available is False
    assert "missing" in missing_result.reason
    assert calls == []

    not_file = tmp_path / "not-a-file"
    not_file.mkdir()
    result = NsJailBackend(binary=not_file, subprocess_run=never_run).probe(workspace, policies)
    assert result.available is False
    assert "missing" in result.reason
    assert calls == []

    not_exec = tmp_path / "nsjail"
    not_exec.write_text("#!/bin/sh\n", encoding="utf-8")
    not_exec.chmod(0o644)
    result = NsJailBackend(binary=not_exec, subprocess_run=never_run).probe(workspace, policies)
    assert result.available is False
    assert "executable" in result.reason
    assert calls == []

    real_binary = tmp_path / "real-nsjail"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    real_binary.chmod(0o755)
    alias_binary = tmp_path / "alias-nsjail"
    if alias_binary.exists():
        alias_binary.unlink()
    alias_binary.symlink_to(real_binary)
    monkeypatch.setattr("multiclaw.governance.sandbox.nsjail._PRODUCTION_BINARY", alias_binary.resolve())
    result = NsJailBackend(binary=alias_binary, subprocess_run=never_run).probe(workspace, policies)
    assert "canonical" not in result.reason
    calls_after_canonical_probe = list(calls)

    monkeypatch.setattr("multiclaw.governance.sandbox.nsjail._PRODUCTION_BINARY", Path("/usr/bin/nsjail"))
    result = NsJailBackend(binary=alias_binary, subprocess_run=never_run).probe(workspace, policies)
    assert result.available is False
    assert "canonical" in result.reason
    assert calls == calls_after_canonical_probe


def test_nsjail_probe_reports_capabilities_only_after_behavioral_proofs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    real_binary = tmp_path / "nsjail"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    real_binary.chmod(0o755)
    monkeypatch.setattr("multiclaw.governance.sandbox.nsjail._PRODUCTION_BINARY", real_binary.resolve())

    calls: list[dict[str, object]] = []
    deny_marker = b"MULTICLAW_NSJAIL_DENIED\n"

    def fake_run(args, *, shell, capture_output, timeout, check, env):
        assert shell is False
        assert capture_output is True
        assert check is False
        assert timeout == 2.0
        config_text = Path(args[2]).read_text(encoding="utf-8")
        marker = args[-1]
        calls.append({"args": tuple(args), "env": dict(env), "config": config_text})
        if "probe-allow-shell" in marker:
            return subprocess.CompletedProcess(args, 0, b"ok", b"")
        if "probe-allow-code" in marker:
            return subprocess.CompletedProcess(args, 0, b"ok", b"")
        if any(
            probe_name in marker
            for probe_name in (
                "probe-deny-outside-write",
                "probe-deny-network",
                "probe-deny-hidden-read",
                "probe-deny-protected-write",
                "probe-deny-child-process",
            )
        ):
            return subprocess.CompletedProcess(args, 0, deny_marker, b"")
        raise AssertionError(f"unexpected marker {marker!r}")

    policies = (
        _policy(),
        _policy(
            name="code_exec_python",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/python3"),),
        ),
    )

    result = NsJailBackend(
        binary=real_binary,
        subprocess_run=fake_run,
    ).probe(workspace, policies)

    assert result.available is True
    assert result.reason == ""
    assert result.capabilities == {
        "allowed_execution": True,
        "outside_workspace_write_denied": True,
        "network_denied": True,
        "hidden_env_read_denied": True,
        "protected_git_write_denied": True,
        "child_creation_denied": True,
    }
    assert len(calls) == 7
    assert all(call["args"][0] == str(real_binary.resolve()) for call in calls)
    assert all("--config" in call["args"] for call in calls)
    assert all("super-secret-token" not in call["config"] for call in calls)
    assert all("probe-secret" not in call["config"] for call in calls)
    assert all("1.1.1.1" not in " ".join(call["args"]) for call in calls)
    assert all(" nc " not in f" {' '.join(call['args'])} " for call in calls)
    network_calls = [
        call for call in calls if "probe-deny-network" in call["args"][-1]
    ]
    assert len(network_calls) == 1
    assert "127.0.0.1" in " ".join(network_calls[0]["args"])


def test_nsjail_probe_fails_closed_on_timeout_and_sanitizes_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    real_binary = tmp_path / "nsjail"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    real_binary.chmod(0o755)
    probe_root = tmp_path / "probe-root"
    monkeypatch.setattr("multiclaw.governance.sandbox.nsjail._PRODUCTION_BINARY", real_binary.resolve())
    monkeypatch.setattr("multiclaw.governance.sandbox.nsjail.tempfile.mkdtemp", lambda prefix: str(probe_root))

    def fake_run(args, *, shell, capture_output, timeout, check, env):
        del shell, capture_output, timeout, check, env
        marker = args[-1]
        if marker == "probe-allow-shell":
            return subprocess.CompletedProcess(args, 0, b"ok", b"")
        raise subprocess.TimeoutExpired(args=args, timeout=2.0, output=b"", stderr=b"TOP-SECRET")

    policies = (
        _policy(),
        _policy(
            name="code_exec_python",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/python3"),),
        ),
    )

    before_children = tuple(workspace.iterdir())
    result = NsJailBackend(binary=real_binary, subprocess_run=fake_run).probe(workspace, policies)

    assert result.available is False
    assert result.reason == "nsjail capability check failed: allowed_execution"
    assert result.capabilities["allowed_execution"] is False
    assert "TOP-SECRET" not in result.reason
    assert tuple(workspace.iterdir()) == before_children
    assert not probe_root.exists()


def test_nsjail_probe_rejects_generic_nonzero_without_denial_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    real_binary = tmp_path / "nsjail"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    real_binary.chmod(0o755)
    monkeypatch.setattr("multiclaw.governance.sandbox.nsjail._PRODUCTION_BINARY", real_binary.resolve())

    def fake_run(args, *, shell, capture_output, timeout, check, env):
        del shell, capture_output, timeout, check, env
        marker = args[-1]
        if "probe-allow" in marker:
            return subprocess.CompletedProcess(args, 0, b"ok", b"")
        return subprocess.CompletedProcess(args, 1, b"", b"generic failure")

    policies = (
        _policy(),
        _policy(
            name="code_exec_python",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/python3"),),
        ),
    )

    result = NsJailBackend(binary=real_binary, subprocess_run=fake_run).probe(workspace, policies)

    assert result.available is False
    assert result.reason == "nsjail capability check failed: outside_workspace_write_denied"
    assert result.capabilities["allowed_execution"] is True
    assert result.capabilities["outside_workspace_write_denied"] is False


def test_nsjail_probe_rejects_missing_required_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    real_binary = tmp_path / "nsjail"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    real_binary.chmod(0o755)
    monkeypatch.setattr(
        "multiclaw.governance.sandbox.nsjail._PRODUCTION_BINARY",
        real_binary.resolve(),
    )
    result = NsJailBackend(binary=real_binary).probe(workspace, (_policy(),))

    assert result.available is False
    assert "required nsjail profiles" in result.reason


def test_nsjail_probe_closes_loopback_listener_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend

    workspace = _workspace_tree(tmp_path)
    real_binary = tmp_path / "nsjail"
    real_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    real_binary.chmod(0o755)
    monkeypatch.setattr("multiclaw.governance.sandbox.nsjail._PRODUCTION_BINARY", real_binary.resolve())

    closed: list[bool] = []
    original_socket = socket.socket

    class TrackingSocket(socket.socket):
        def close(self):
            closed.append(True)
            return super().close()

    monkeypatch.setattr("multiclaw.governance.sandbox.nsjail.socket.socket", TrackingSocket)

    def fake_run(args, *, shell, capture_output, timeout, check, env):
        del shell, capture_output, timeout, check, env
        marker = args[-1]
        if "probe-allow" in marker:
            return subprocess.CompletedProcess(args, 0, b"ok", b"")
        return subprocess.CompletedProcess(args, 0, b"MULTICLAW_NSJAIL_DENIED\n", b"")

    policies = (
        _policy(),
        _policy(
            name="code_exec_python",
            allow_subprocesses=False,
            entrypoints=(Path("/usr/bin/python3"),),
        ),
    )

    result = NsJailBackend(binary=real_binary, subprocess_run=fake_run).probe(workspace, policies)

    assert result.available is True
    assert closed
    assert original_socket is not None


def test_nsjail_config_creation_failure_cleans_partial_private_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from multiclaw.governance.sandbox.nsjail import NsJailBackend
    import multiclaw.governance.sandbox.nsjail as nsjail_module

    workspace = _workspace_tree(tmp_path)
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
    created_path = environment.private_root / "nsjail-failure.cfg"

    def fake_mkstemp(*, prefix: str, suffix: str, dir: str):
        del prefix, suffix, dir
        fd = os.open(created_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        return fd, str(created_path)

    def fake_write(fd: int, data: bytes) -> int:
        del fd, data
        raise OSError("disk full")

    monkeypatch.setattr(nsjail_module.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(nsjail_module.os, "write", fake_write)

    with pytest.raises(SandboxLaunchError, match="failed to write nsjail config"):
        NsJailBackend(binary=Path("/usr/bin/nsjail")).build_launch_spec(
            request,
            _policy(),
            environment,
        )

    assert not created_path.exists()
