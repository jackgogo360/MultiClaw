from pathlib import Path

import pytest
from pydantic import ValidationError


def test_sandbox_exec_request_accepts_shell_string_payload() -> None:
    from multiclaw.governance import SandboxExecRequest

    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command="echo hello",
        workspace_root=Path("/workspace"),
        cwd=Path("/workspace"),
        timeout_seconds=5.0,
    )

    assert request.command == "echo hello"
    assert request.argv is None
    assert request.env_overrides == {}
    assert request.allowed_secret_env == frozenset()
    assert request.read_only_paths == ()
    assert request.correlation_id == ""


def test_sandbox_exec_request_accepts_exec_argv_payload() -> None:
    from multiclaw.governance import SandboxExecRequest

    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="exec_argv",
        argv=("python3", "-V"),
        workspace_root=Path("/workspace"),
        cwd=Path("/workspace"),
        timeout_seconds=5.0,
    )

    assert request.command is None
    assert request.argv == ("python3", "-V")


@pytest.mark.parametrize(
    ("mode", "command", "argv"),
    [
        ("shell_string", None, None),
        ("shell_string", "echo hello", ("sh", "-c", "echo hello")),
        ("exec_argv", None, None),
        ("exec_argv", "echo hello", ("sh", "-c", "echo hello")),
    ],
)
def test_sandbox_exec_request_rejects_invalid_launch_payloads(
    mode: str,
    command: str | None,
    argv: tuple[str, ...] | None,
) -> None:
    from multiclaw.governance import SandboxExecRequest

    with pytest.raises(
        ValidationError,
        match="exactly one launch payload must match mode",
    ):
        SandboxExecRequest(
            tool_name="shell",
            profile_name="shell_workspace",
            mode=mode,
            command=command,
            argv=argv,
            workspace_root=Path("/workspace"),
            cwd=Path("/workspace"),
            timeout_seconds=5.0,
        )


def test_sandbox_exec_request_rejects_more_than_16_read_only_paths() -> None:
    from multiclaw.governance import SandboxExecRequest

    with pytest.raises(ValidationError, match="read_only_paths"):
        SandboxExecRequest(
            tool_name="shell",
            profile_name="shell_workspace",
            mode="exec_argv",
            argv=("python3", "-V"),
            workspace_root=Path("/workspace"),
            cwd=Path("/workspace"),
            timeout_seconds=5.0,
            read_only_paths=tuple(Path(f"/ro/{index}") for index in range(17)),
        )


def test_sandbox_models_are_frozen_and_expose_expected_defaults() -> None:
    from multiclaw.governance import (
        SandboxEnvironment,
        SandboxExecRequest,
        SandboxProbeResult,
        SandboxProfilePolicy,
        SandboxReadiness,
        SandboxExecResult,
        SandboxedLaunchSpec,
    )

    classes = (
        SandboxExecRequest,
        SandboxEnvironment,
        SandboxProfilePolicy,
        SandboxedLaunchSpec,
        SandboxExecResult,
        SandboxProbeResult,
        SandboxReadiness,
    )

    assert all(model.model_config.get("frozen") is True for model in classes)

    readiness = SandboxReadiness(
        ready=True,
        mode="auto",
        backend_name="host-unsafe",
        probe=SandboxProbeResult(
            backend_name="host-unsafe",
            available=True,
            capabilities={"exec": True},
        ),
        profiles={"shell_workspace": True},
        skipped_capabilities={},
    )

    with pytest.raises(ValidationError):
        readiness.ready = False

    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command="echo hello",
        workspace_root=Path("/workspace"),
        cwd=Path("/workspace"),
        timeout_seconds=5.0,
    )

    assert request.network_mode is None
    assert request.workspace_mode is None
    assert request.allow_subprocesses is None
    assert request.mcp_server_name is None
    assert request.stdin_bytes is None

