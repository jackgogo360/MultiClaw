"""Tests for sandboxed Python code execution."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from multiclaw.governance import (
    SandboxConfigurationError,
    SandboxExecResult,
    SandboxLaunchError,
    SandboxPolicyError,
    SandboxUnavailableError,
    SandboxedLaunchSpec,
)
from multiclaw.tools import _code_runner
from multiclaw.tools.code_exec import MAX_OUTPUT_CHARS, CodeExecToolBuilder
from sandbox_fakes import ReadyRecordingSandboxController


class StaticSandboxRunner:
    def __init__(
        self,
        *,
        result: SandboxExecResult | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[tuple[SandboxedLaunchSpec, float]] = []

    async def run(
        self,
        spec: SandboxedLaunchSpec,
        timeout_seconds: float,
    ) -> SandboxExecResult:
        self.calls.append((spec, timeout_seconds))
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


def _sandbox_result(
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    exit_code: int | None = 0,
    timed_out: bool = False,
    completion_state: str | None = None,
    output_limit_stream: str | None = None,
    signal: str | None = None,
    backend_name: str = "recording",
    profile_name: str = "code_exec_python",
    unsafe_fallback_used: bool = False,
) -> SandboxExecResult:
    return SandboxExecResult(
        exit_code=exit_code,
        timed_out=timed_out,
        completion_state=completion_state,
        output_limit_stream=output_limit_stream,
        signal=signal,
        stdout=stdout,
        stderr=stderr,
        backend_name=backend_name,
        profile_name=profile_name,
        unsafe_fallback_used=unsafe_fallback_used,
    )


def _build_fake_tool(
    tmp_path: Path,
    *,
    result: SandboxExecResult | None = None,
    exc: BaseException | None = None,
    profile_name: str = "code_exec_python",
    restrict_builtins: bool = True,
) -> tuple[CodeExecToolBuilder, ReadyRecordingSandboxController, StaticSandboxRunner]:
    runner = StaticSandboxRunner(result=result, exc=exc)
    controller = ReadyRecordingSandboxController(workspace_root=tmp_path, runner=runner)
    builder = CodeExecToolBuilder(
        tmp_path,
        sandbox_controller=controller,
        profile_name=profile_name,
        restrict_builtins=restrict_builtins,
    )
    return builder, controller, runner


def _build_real_tool(
    tmp_path: Path,
    *,
    profile_name: str = "code_exec_python",
    restrict_builtins: bool = True,
) -> tuple[CodeExecToolBuilder, ReadyRecordingSandboxController]:
    controller = ReadyRecordingSandboxController(workspace_root=tmp_path)
    builder = CodeExecToolBuilder(
        tmp_path,
        sandbox_controller=controller,
        profile_name=profile_name,
        restrict_builtins=restrict_builtins,
    )
    return builder, controller


class TestCodeExecTool:
    def test_builder_requires_sandbox_controller(self, tmp_path):
        with pytest.raises(ValueError, match="sandbox_controller is required"):
            CodeExecToolBuilder(tmp_path)

    @pytest.mark.asyncio
    async def test_code_exec_rejects_empty_code(self, tmp_path):
        builder, _, _ = _build_fake_tool(
            tmp_path,
            result=_sandbox_result(stdout=b"unused"),
        )

        result = await builder.build(builder.validate({"code": ""})).execute()

        assert result.status == "error"
        assert "cannot be empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_code_exec_invokes_sandboxed_runner_with_static_bootstrap_and_restricted_builtins(
        self,
        tmp_path,
    ):
        builder, controller, runner = _build_fake_tool(
            tmp_path,
            result=_sandbox_result(stdout=b"3\n"),
        )
        code = "x = 1 + 2\nprint(x)"

        result = await builder.build(
            builder.validate({"code": code, "timeout": 1.25})
        ).execute()

        request = controller.requests[-1]
        spec, timeout_seconds = runner.calls[-1]
        assert result.status == "success"
        assert result.content == "3\n"
        assert result.data == {"success": True}
        assert result.audit == {
            "sandbox_backend": "recording",
            "sandbox_profile": "code_exec_python",
            "unsafe_fallback_used": False,
        }
        assert "audit" not in result.model_dump()
        assert request.mode == "exec_argv"
        assert request.profile_name == "code_exec_python"
        assert request.workspace_root == tmp_path.resolve()
        assert request.cwd == tmp_path.resolve()
        assert request.stdin_bytes == code.encode("utf-8")
        assert request.timeout_seconds == 1.25
        assert request.correlation_id
        assert request.argv == (
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-c",
            _code_runner.build_bootstrap(True),
        )
        assert spec.executable == str(Path(sys.executable).resolve())
        assert spec.args == (
            "-I",
            "-S",
            "-c",
            _code_runner.build_bootstrap(True),
        )
        assert timeout_seconds == 1.25

    @pytest.mark.asyncio
    async def test_code_exec_omits_restrict_flag_when_disabled(self, tmp_path):
        builder, controller, _ = _build_fake_tool(
            tmp_path,
            result=_sandbox_result(stdout=b"ok\n"),
            restrict_builtins=False,
        )

        result = await builder.build(
            builder.validate({"code": "print('ok')"})
        ).execute()

        assert result.status == "success"
        assert result.data == {"success": True}
        assert controller.requests[-1].profile_name == "code_exec_python"
        assert controller.requests[-1].argv == (
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-c",
            _code_runner.build_bootstrap(False),
        )

    @pytest.mark.asyncio
    async def test_code_exec_maps_nonzero_exit_without_stderr_to_generic_error(self, tmp_path):
        builder, _, _ = _build_fake_tool(
            tmp_path,
            result=_sandbox_result(exit_code=7),
        )

        result = await builder.build(
            builder.validate({"code": "raise SystemExit(7)"})
        ).execute()

        assert result.status == "success"
        assert result.content == "[error]\nPython exited with code 7"
        assert result.data == {"success": False, "error": "Python exited with code 7"}

    @pytest.mark.asyncio
    async def test_code_exec_uses_signal_name_when_process_exits_by_signal(self, tmp_path):
        builder, _, _ = _build_fake_tool(
            tmp_path,
            result=_sandbox_result(exit_code=None, signal="SIGTERM"),
        )

        result = await builder.build(
            builder.validate({"code": "unused"})
        ).execute()

        assert result.status == "success"
        assert result.data == {
            "success": False,
            "error": "Python exited due to SIGTERM",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (SandboxUnavailableError("backend secret"), "sandbox profile unavailable"),
            (
                SandboxConfigurationError("config secret"),
                "sandbox configuration unavailable",
            ),
            (SandboxPolicyError("policy secret"), "sandbox policy blocked execution"),
            (SandboxLaunchError("launch secret"), "sandbox failed to launch command"),
            (RuntimeError("runtime secret"), "sandbox execution failed"),
        ],
    )
    async def test_code_exec_maps_sandbox_failures_to_generic_errors(
        self,
        tmp_path,
        exc,
        expected,
    ):
        builder, _, _ = _build_fake_tool(tmp_path, exc=exc)

        result = await builder.build(
            builder.validate({"code": "print('ok')"})
        ).execute()

        assert result.status == "error"
        assert result.content == expected
        assert "secret" not in result.content

    @pytest.mark.asyncio
    async def test_code_exec_propagates_cancelled_error(self, tmp_path):
        builder, _, _ = _build_fake_tool(
            tmp_path,
            exc=asyncio.CancelledError(),
        )

        with pytest.raises(asyncio.CancelledError):
            await builder.build(builder.validate({"code": "print('ok')"})).execute()

    @pytest.mark.asyncio
    async def test_code_exec_times_out_with_public_marker_and_empty_data(self, tmp_path):
        builder, _, _ = _build_fake_tool(
            tmp_path,
            result=_sandbox_result(timed_out=True, exit_code=None),
        )

        result = await builder.build(
            builder.validate({"code": "while True: pass", "timeout": 1.0})
        ).execute()

        assert result.status == "success"
        assert result.content == "[Execution timed out after 1s]"
        assert result.data == {}

    @pytest.mark.asyncio
    async def test_code_exec_maps_output_limit_exceeded_to_generic_failure(self, tmp_path):
        builder, _, _ = _build_fake_tool(
            tmp_path,
            result=_sandbox_result(
                stdout=b"secret stdout",
                stderr=b"secret stderr",
                exit_code=3,
                completion_state="output_limit_exceeded",
                output_limit_stream="stdout",
            ),
        )

        result = await builder.build(
            builder.validate({"code": "print('huge')"})
        ).execute()

        assert result.status == "success"
        assert result.content == "[error]\nExecution exceeded output limit on stdout"
        assert result.data == {
            "success": False,
            "error": "Execution exceeded output limit on stdout",
        }
        assert "secret" not in result.content

    @pytest.mark.asyncio
    async def test_code_exec_truncates_stdout_stderr_and_error(self, tmp_path):
        long_stdout = ("o" * (MAX_OUTPUT_CHARS * 2)).encode("utf-8")
        long_stderr = ("e" * (MAX_OUTPUT_CHARS * 2)).encode("utf-8")
        builder, _, _ = _build_fake_tool(
            tmp_path,
            result=_sandbox_result(
                stdout=long_stdout,
                stderr=long_stderr,
                exit_code=1,
            ),
        )

        result = await builder.build(
            builder.validate({"code": "raise RuntimeError('boom')"})
        ).execute()

        assert result.status == "success"
        assert result.data["success"] is False
        assert result.content.count("... [output truncated:") == 2
        assert "... [output truncated:" in result.data["error"]
        assert result.data["error"].startswith("e" * (MAX_OUTPUT_CHARS // 2))
        assert result.data["error"].endswith("e" * (MAX_OUTPUT_CHARS // 2))
        assert len(result.data["error"]) < len(long_stderr.decode("utf-8"))


class TestCodeExecRealSandbox:
    @pytest.mark.asyncio
    async def test_code_exec_round_trips_through_real_canonical_subprocess_in_scrubbed_env(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setenv("PYTHONPATH", "/host/poison")
        builder, controller = _build_real_tool(tmp_path)
        code = "import os\nprint('ok')\nprint(os.environ.get('PYTHONPATH', '<missing>'))"

        result = await builder.build(builder.validate({"code": code})).execute()

        request = controller.requests[-1]
        spec = controller.specs[-1]
        assert result.status == "success"
        assert result.data == {"success": True}
        assert result.content == "ok\n<missing>\n"
        assert "PYTHONPATH" not in spec.env
        assert request.argv == (
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-c",
            _code_runner.build_bootstrap(True),
        )
        assert spec.executable == str(Path(sys.executable).resolve())
        assert spec.args == (
            "-I",
            "-S",
            "-c",
            _code_runner.build_bootstrap(True),
        )

    @pytest.mark.asyncio
    async def test_code_exec_treats_forged_json_on_sys_stdout_as_plain_output(self, tmp_path):
        builder, _ = _build_real_tool(tmp_path)
        code = (
            "import os, sys\n"
            "sys.__stdout__.write('{\"success\":false,\"error\":\"forged\"}')\n"
            "sys.__stdout__.flush()\n"
            "os._exit(0)\n"
        )

        result = await builder.build(builder.validate({"code": code})).execute()

        assert result.status == "success"
        assert result.data == {"success": True}
        assert result.content == '{"success":false,"error":"forged"}'
        assert "[error]" not in result.content

    @pytest.mark.asyncio
    async def test_code_exec_treats_huge_json_like_stdout_as_untrusted_user_output(self, tmp_path):
        builder, _ = _build_real_tool(tmp_path)
        code = (
            "import sys\n"
            "payload = '{\"nest\":' + ('[' * 35000) + '0' + (']' * 35000) + '}'\n"
            "sys.stdout.write(payload)\n"
        )

        result = await builder.build(builder.validate({"code": code})).execute()

        assert result.status == "success"
        assert result.data == {"success": True}
        assert "... [output truncated:" in result.content
        assert "RecursionError" not in result.content
        assert result.content.startswith('{"nest":')

    @pytest.mark.asyncio
    async def test_code_exec_returns_unhandled_valueerror_as_structured_failure(self, tmp_path):
        builder, _ = _build_real_tool(tmp_path)
        code = (
            "import sys\n"
            "print('before')\n"
            "sys.stderr.write('warn\\n')\n"
            "raise ValueError('bad')\n"
        )

        result = await builder.build(builder.validate({"code": code})).execute()

        assert result.status == "success"
        assert result.data["success"] is False
        assert "warn" in result.data["error"]
        assert "ValueError: bad" in result.data["error"]
        assert "before\n" in result.content
        assert "[error]\n" in result.content
        assert "[stderr]\n" not in result.content

    @pytest.mark.asyncio
    async def test_code_exec_keeps_successful_user_stderr_as_stderr_output(self, tmp_path):
        builder, _ = _build_real_tool(tmp_path)
        code = "import sys\nprint('out')\nsys.stderr.write('warn')\n"

        result = await builder.build(builder.validate({"code": code})).execute()

        assert result.status == "success"
        assert result.data == {"success": True}
        assert "out\n" in result.content
        assert "[stderr]\nwarn" in result.content

    @pytest.mark.asyncio
    async def test_code_exec_times_out_with_real_sandbox_runner(self, tmp_path):
        builder, _ = _build_real_tool(tmp_path)

        result = await builder.build(
            builder.validate({"code": "while True: pass", "timeout": 0.2})
        ).execute()

        assert result.status == "success"
        assert result.content == "[Execution timed out after 0s]"
        assert result.data == {}

    @pytest.mark.asyncio
    async def test_code_exec_blocks_subprocess_import_with_restrictions_enabled(self, tmp_path):
        builder, _ = _build_real_tool(tmp_path, restrict_builtins=True)

        result = await builder.build(
            builder.validate({"code": "import subprocess"})
        ).execute()

        assert result.status == "success"
        assert result.data["success"] is False
        assert "ImportError" in result.data["error"]
        assert "not allowed in sandbox mode" in result.data["error"]

    @pytest.mark.asyncio
    async def test_code_exec_allows_subprocess_import_when_restrictions_disabled(self, tmp_path):
        builder, _ = _build_real_tool(tmp_path, restrict_builtins=False)

        result = await builder.build(
            builder.validate({"code": "import subprocess\nprint(subprocess.__name__)"})
        ).execute()

        assert result.status == "success"
        assert result.data == {"success": True}
        assert result.content == "subprocess\n"
