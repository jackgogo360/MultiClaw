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


def test_sandbox_mapping_fields_are_immutably_wrapped_after_validation() -> None:
    from multiclaw.governance import (
        SandboxEnvironment,
        SandboxExecRequest,
        SandboxProbeResult,
        SandboxReadiness,
        SandboxedLaunchSpec,
    )

    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command="echo hello",
        workspace_root=Path("/workspace"),
        cwd=Path("/workspace"),
        timeout_seconds=5.0,
        env_overrides={"CUSTOM_FLAG": "1"},
    )
    environment = SandboxEnvironment(
        env={"PATH": "/usr/bin:/bin"},
        private_root=Path("/tmp/private"),
        home=Path("/tmp/private/home"),
        tmp=Path("/tmp/private/tmp"),
    )
    launch = SandboxedLaunchSpec(
        executable="/bin/sh",
        args=("-c", "echo hello"),
        cwd=Path("/workspace"),
        env={"PATH": "/usr/bin:/bin"},
        stdin_bytes=None,
        private_root=Path("/tmp/private"),
        backend_name="host_unsafe",
        profile_name="shell_workspace",
        correlation_id="corr-1",
    )
    probe = SandboxProbeResult(
        backend_name="host_unsafe",
        available=True,
        capabilities={"exec": True},
    )
    readiness = SandboxReadiness(
        ready=True,
        mode="auto",
        backend_name="host_unsafe",
        probe=probe,
        profiles={"shell_workspace": True},
        skipped_capabilities={"network": "disabled"},
    )

    with pytest.raises(TypeError):
        request.env_overrides["OTHER"] = "2"
    with pytest.raises(TypeError):
        environment.env["OTHER"] = "2"
    with pytest.raises(TypeError):
        launch.env["OTHER"] = "2"
    with pytest.raises(TypeError):
        probe.capabilities["network"] = False
    with pytest.raises(TypeError):
        readiness.profiles["code_exec"] = False
    with pytest.raises(TypeError):
        readiness.skipped_capabilities["filesystem"] = "blocked"


def test_sandbox_env_contracts_redact_secret_values_in_repr_and_model_dump() -> None:
    from multiclaw.governance import (
        SandboxEnvironment,
        SandboxExecRequest,
        SandboxedLaunchSpec,
    )

    secret_value = "dummy-secret-value"
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command="echo hello",
        workspace_root=Path("/workspace"),
        cwd=Path("/workspace"),
        timeout_seconds=5.0,
        env_overrides={
            "OPENAI_API_KEY": secret_value,
            "CUSTOM_FLAG": "1",
        },
    )
    environment = SandboxEnvironment(
        env={
            "OPENAI_API_KEY": secret_value,
            "CUSTOM_FLAG": "1",
        },
        private_root=Path("/tmp/private"),
        home=Path("/tmp/private/home"),
        tmp=Path("/tmp/private/tmp"),
    )
    launch = SandboxedLaunchSpec(
        executable="/bin/sh",
        args=("-c", "echo hello"),
        cwd=Path("/workspace"),
        env={
            "OPENAI_API_KEY": secret_value,
            "CUSTOM_FLAG": "1",
        },
        stdin_bytes=None,
        private_root=Path("/tmp/private"),
        backend_name="host_unsafe",
        profile_name="shell_workspace",
        correlation_id="corr-1",
    )

    assert secret_value == request.env_overrides["OPENAI_API_KEY"]
    assert secret_value == environment.env["OPENAI_API_KEY"]
    assert secret_value == launch.env["OPENAI_API_KEY"]

    assert secret_value not in repr(request)
    assert secret_value not in repr(environment)
    assert secret_value not in repr(launch)

    request_dump = request.model_dump()
    environment_dump = environment.model_dump()
    launch_dump = launch.model_dump()

    assert request_dump["env_overrides"]["OPENAI_API_KEY"] == "[REDACTED]"
    assert request_dump["env_overrides"]["CUSTOM_FLAG"] == "1"
    assert environment_dump["env"]["OPENAI_API_KEY"] == "[REDACTED]"
    assert environment_dump["env"]["CUSTOM_FLAG"] == "1"
    assert launch_dump["env"]["OPENAI_API_KEY"] == "[REDACTED]"
    assert launch_dump["env"]["CUSTOM_FLAG"] == "1"
    assert secret_value not in str(request_dump)
    assert secret_value not in str(environment_dump)
    assert secret_value not in str(launch_dump)


def test_model_copy_update_preserves_immutable_wrapped_mappings() -> None:
    from multiclaw.governance import (
        SandboxEnvironment,
        SandboxExecRequest,
        SandboxProbeResult,
        SandboxReadiness,
        SandboxedLaunchSpec,
    )

    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command="echo hello",
        workspace_root=Path("/workspace"),
        cwd=Path("/workspace"),
        timeout_seconds=5.0,
        env_overrides={"CUSTOM_FLAG": "1"},
    )
    copied_request = request.model_copy(
        update={"env_overrides": {"CUSTOM_FLAG": "2", "SECOND_FLAG": "3"}}
    )
    assert copied_request.env_overrides == {"CUSTOM_FLAG": "2", "SECOND_FLAG": "3"}
    with pytest.raises(TypeError):
        copied_request.env_overrides["THIRD_FLAG"] = "4"

    environment = SandboxEnvironment(
        env={"PATH": "/usr/bin:/bin"},
        private_root=Path("/tmp/private"),
        home=Path("/tmp/private/home"),
        tmp=Path("/tmp/private/tmp"),
    )
    copied_environment = environment.model_copy(
        update={"env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}}
    )
    assert copied_environment.env == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    with pytest.raises(TypeError):
        copied_environment.env["TERM"] = "xterm-256color"

    launch = SandboxedLaunchSpec(
        executable="/bin/sh",
        args=("-c", "echo hello"),
        cwd=Path("/workspace"),
        env={"PATH": "/usr/bin:/bin"},
        stdin_bytes=None,
        private_root=Path("/tmp/private"),
        backend_name="host_unsafe",
        profile_name="shell_workspace",
        correlation_id="corr-1",
    )
    copied_launch = launch.model_copy(
        update={"env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}}
    )
    assert copied_launch.env == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    with pytest.raises(TypeError):
        copied_launch.env["TERM"] = "xterm-256color"

    probe = SandboxProbeResult(
        backend_name="host_unsafe",
        available=True,
        capabilities={"exec": True},
    )
    copied_probe = probe.model_copy(
        update={"capabilities": {"exec": True, "network": False}}
    )
    assert copied_probe.capabilities == {"exec": True, "network": False}
    with pytest.raises(TypeError):
        copied_probe.capabilities["filesystem"] = False

    readiness = SandboxReadiness(
        ready=True,
        mode="auto",
        backend_name="host_unsafe",
        probe=probe,
        profiles={"shell_workspace": True},
        skipped_capabilities={"network": "disabled"},
    )
    copied_readiness = readiness.model_copy(
        update={
            "profiles": {"shell_workspace": True, "code_exec": False},
            "skipped_capabilities": {
                "network": "disabled",
                "filesystem": "blocked",
            },
        }
    )
    assert copied_readiness.profiles == {
        "shell_workspace": True,
        "code_exec": False,
    }
    assert copied_readiness.skipped_capabilities == {
        "network": "disabled",
        "filesystem": "blocked",
    }
    with pytest.raises(TypeError):
        copied_readiness.profiles["mcp_stdio"] = False
    with pytest.raises(TypeError):
        copied_readiness.skipped_capabilities["mcp_stdio"] = "blocked"


def test_model_copy_update_preserves_raw_secret_env_values_while_redacting_repr_and_dump() -> None:
    from multiclaw.governance import (
        SandboxEnvironment,
        SandboxExecRequest,
        SandboxedLaunchSpec,
    )

    secret_value = "dummy-secret-value"
    request = SandboxExecRequest(
        tool_name="shell",
        profile_name="shell_workspace",
        mode="shell_string",
        command="echo hello",
        workspace_root=Path("/workspace"),
        cwd=Path("/workspace"),
        timeout_seconds=5.0,
    )
    copied_request = request.model_copy(
        update={
            "env_overrides": {
                "OPENAI_API_KEY": secret_value,
                "CUSTOM_FLAG": "1",
            }
        }
    )
    assert copied_request.env_overrides["OPENAI_API_KEY"] == secret_value
    assert secret_value not in repr(copied_request)
    assert copied_request.model_dump()["env_overrides"]["OPENAI_API_KEY"] == "[REDACTED]"

    environment = SandboxEnvironment(
        env={"PATH": "/usr/bin:/bin"},
        private_root=Path("/tmp/private"),
        home=Path("/tmp/private/home"),
        tmp=Path("/tmp/private/tmp"),
    )
    copied_environment = environment.model_copy(
        update={
            "env": {
                "OPENAI_API_KEY": secret_value,
                "CUSTOM_FLAG": "1",
            }
        }
    )
    assert copied_environment.env["OPENAI_API_KEY"] == secret_value
    assert secret_value not in repr(copied_environment)
    assert copied_environment.model_dump()["env"]["OPENAI_API_KEY"] == "[REDACTED]"

    launch = SandboxedLaunchSpec(
        executable="/bin/sh",
        args=("-c", "echo hello"),
        cwd=Path("/workspace"),
        env={"PATH": "/usr/bin:/bin"},
        stdin_bytes=None,
        private_root=Path("/tmp/private"),
        backend_name="host_unsafe",
        profile_name="shell_workspace",
        correlation_id="corr-1",
    )
    copied_launch = launch.model_copy(
        update={
            "env": {
                "OPENAI_API_KEY": secret_value,
                "CUSTOM_FLAG": "1",
            }
        }
    )
    assert copied_launch.env["OPENAI_API_KEY"] == secret_value
    assert secret_value not in repr(copied_launch)
    assert copied_launch.model_dump()["env"]["OPENAI_API_KEY"] == "[REDACTED]"
