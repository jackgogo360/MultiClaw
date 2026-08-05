"""Tests for sandboxed Python code execution."""

from __future__ import annotations

import io
import json
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
from multiclaw.tools.code_exec import (
    MAX_ENVELOPE_BYTES,
    MAX_OUTPUT_CHARS,
    CodeExecToolBuilder,
)
from sandbox_fakes import ReadyRecordingSandboxController


class StaticSandboxRunner:
    def __init__(
        self,
        *,
        result: SandboxExecResult | None = None,
        exc: Exception | None = None,
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


def _protocol_bytes(
    *,
    success: bool,
    stdout: str = "",
    stderr: str = "",
    error: str = "",
) -> bytes:
    return json.dumps(
        {
            "success": success,
            "stdout": stdout,
            "stderr": stderr,
            "error": error,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _sandbox_result(
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    exit_code: int | None = 0,
    timed_out: bool = False,
    signal: str | None = None,
    backend_name: str = "recording",
    profile_name: str = "code_exec_python",
    unsafe_fallback_used: bool = False,
) -> SandboxExecResult:
    return SandboxExecResult(
        exit_code=exit_code,
        timed_out=timed_out,
        signal=signal,
        stdout=stdout,
        stderr=stderr,
        backend_name=backend_name,
        profile_name=profile_name,
        unsafe_fallback_used=unsafe_fallback_used,
    )


def _invoke_code_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    code: str,
    argv: list[str] | None = None,
) -> tuple[int, str]:
    protocol_stdout = io.StringIO()
    monkeypatch.setattr(_code_runner.sys, "stdin", io.StringIO(code))
    monkeypatch.setattr(_code_runner.sys, "__stdout__", protocol_stdout, raising=False)
    exit_code = _code_runner.main(argv or [])
    return exit_code, protocol_stdout.getvalue()


def _build_tool(
    tmp_path: Path,
    *,
    result: SandboxExecResult | None = None,
    exc: Exception | None = None,
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


class TestCodeRunnerProtocol:
    def test_main_emits_exact_json_shape_for_success_and_unicode(self, monkeypatch):
        exit_code, payload = _invoke_code_runner(
            monkeypatch,
            code="print('你好, sandbox 🌍')",
            argv=["--restrict-builtins"],
        )

        assert exit_code == 0
        assert payload == json.dumps(
            {
                "success": True,
                "stdout": "你好, sandbox 🌍\n",
                "stderr": "",
                "error": "",
            },
            ensure_ascii=False,
        )

    def test_main_captures_user_stdout_and_stderr(self, monkeypatch):
        exit_code, payload = _invoke_code_runner(
            monkeypatch,
            code="import sys\nprint('out')\nsys.stderr.write('err')",
            argv=["--restrict-builtins"],
        )

        assert exit_code == 0
        assert json.loads(payload) == {
            "success": True,
            "stdout": "out\n",
            "stderr": "err",
            "error": "",
        }

    def test_main_captures_base_exception_traceback(self, monkeypatch):
        exit_code, payload = _invoke_code_runner(
            monkeypatch,
            code="raise SystemExit('bad exit')",
            argv=["--restrict-builtins"],
        )

        body = json.loads(payload)
        assert exit_code == 0
        assert body["success"] is False
        assert body["stdout"] == ""
        assert body["stderr"] == ""
        assert "SystemExit: bad exit" in body["error"]

    def test_main_blocks_restricted_imports_by_root_module(self, monkeypatch):
        monkeypatch.setenv("TASK10_SECRET_VALUE", "dont-leak-me")
        exit_code, payload = _invoke_code_runner(
            monkeypatch,
            code="__import__('subprocess.Popen')",
            argv=["--restrict-builtins"],
        )

        body = json.loads(payload)
        assert exit_code == 0
        assert body["success"] is False
        assert "Import of 'subprocess' is not allowed in sandbox mode" in body["error"]
        assert "dont-leak-me" not in body["error"]


class TestCodeExecTool:
    def test_builder_requires_sandbox_controller(self, tmp_path):
        with pytest.raises(ValueError, match="sandbox_controller is required"):
            CodeExecToolBuilder(tmp_path)

    @pytest.mark.asyncio
    async def test_code_exec_rejects_empty_code(self, tmp_path):
        builder, _, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(stdout=_protocol_bytes(success=True)),
        )

        result = await builder.build(builder.validate({"code": ""})).execute()

        assert result.status == "error"
        assert "cannot be empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_code_exec_invokes_sandboxed_runner_with_default_profile_and_restricted_builtins(
        self,
        tmp_path,
    ):
        builder, controller, runner = _build_tool(
            tmp_path,
            result=_sandbox_result(
                stdout=_protocol_bytes(success=True, stdout="3\n"),
            ),
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
            "-m",
            "multiclaw.tools._code_runner",
            "--restrict-builtins",
        )
        assert spec.executable == str(Path(sys.executable).resolve())
        assert spec.args == (
            "-m",
            "multiclaw.tools._code_runner",
            "--restrict-builtins",
        )
        assert timeout_seconds == 1.25

    @pytest.mark.asyncio
    async def test_code_exec_omits_restrict_flag_when_disabled(self, tmp_path):
        builder, controller, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(
                stdout=_protocol_bytes(success=True, stdout="ok\n"),
            ),
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
            "-m",
            "multiclaw.tools._code_runner",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stdout",
        [
            b"{",
            b"[]",
            _protocol_bytes(success=True)
            + _protocol_bytes(success=True, stdout="extra"),
            _protocol_bytes(success=True) + b"\n",
            json.dumps(
                {"success": True, "stdout": "", "stderr": ""},
                ensure_ascii=False,
            ).encode("utf-8"),
            json.dumps(
                {
                    "success": True,
                    "stdout": "",
                    "stderr": "",
                    "error": "",
                    "extra": "x",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            json.dumps(
                {
                    "success": "yes",
                    "stdout": "",
                    "stderr": "",
                    "error": "",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        ],
    )
    async def test_code_exec_rejects_invalid_runner_protocol(self, tmp_path, stdout):
        builder, _, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(stdout=stdout),
        )
        result = await builder.build(
            builder.validate({"code": "print('ok')"})
        ).execute()

        assert result.status == "error"
        assert result.content == "sandbox execution failed"
        assert "print('ok')" not in result.content
        assert str(tmp_path) not in result.content

    @pytest.mark.asyncio
    async def test_code_exec_rejects_invalid_utf8_protocol_bytes(self, tmp_path):
        builder, _, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(stdout=b"\xff"),
        )

        result = await builder.build(
            builder.validate({"code": "print('ok')"})
        ).execute()

        assert result.status == "error"
        assert result.content == "sandbox execution failed"

    @pytest.mark.asyncio
    async def test_code_exec_rejects_oversized_raw_protocol_stdout(self, tmp_path):
        builder, _, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(stdout=b"x" * (MAX_ENVELOPE_BYTES + 1)),
        )

        result = await builder.build(
            builder.validate({"code": "print('ok')"})
        ).execute()

        assert result.status == "error"
        assert result.content == "sandbox execution failed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stdout",
        [
            b'{"success":true,"stdout":"","stderr":"","error":"","error":"dup"}',
            b'{"success":true,"success":false,"stdout":"","stderr":"","error":""}',
            b'{"success":true,"success":"nope","stdout":"","stderr":"","error":""}',
        ],
    )
    async def test_code_exec_rejects_duplicate_runner_protocol_keys(self, tmp_path, stdout):
        builder, _, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(stdout=stdout),
        )

        result = await builder.build(
            builder.validate({"code": "print('ok')"})
        ).execute()

        assert result.status == "error"
        assert result.content == "sandbox execution failed"
        assert "dup" not in result.content
        assert "print('ok')" not in result.content
        assert str(tmp_path) not in result.content

    @pytest.mark.asyncio
    async def test_code_exec_rejects_non_zero_child_exit_even_with_json_output(self, tmp_path):
        builder, _, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(
                stdout=_protocol_bytes(success=True, stdout="ok\n"),
                exit_code=7,
            ),
        )

        result = await builder.build(
            builder.validate({"code": "print('ok')"})
        ).execute()

        assert result.status == "error"
        assert result.content == "sandbox execution failed"

    @pytest.mark.asyncio
    async def test_code_exec_rejects_wrapper_stderr_output(self, tmp_path):
        builder, _, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(
                stdout=_protocol_bytes(success=True, stdout="ok\n"),
                stderr=b"wrapper secret stderr",
            ),
        )

        result = await builder.build(
            builder.validate({"code": "print('ok')"})
        ).execute()

        assert result.status == "error"
        assert result.content == "sandbox execution failed"
        assert "wrapper secret stderr" not in result.content

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
        builder, _, _ = _build_tool(tmp_path, exc=exc)

        result = await builder.build(
            builder.validate({"code": "print('ok')"})
        ).execute()

        assert result.status == "error"
        assert result.content == expected
        assert "secret" not in result.content

    @pytest.mark.asyncio
    async def test_code_exec_times_out_with_public_marker_and_empty_data(self, tmp_path):
        builder, _, _ = _build_tool(
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
    async def test_code_exec_returns_exception_payload_in_success_result(self, tmp_path):
        builder, _, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(
                stdout=_protocol_bytes(
                    success=False,
                    error="Traceback (most recent call last):\nValueError: bad",
                ),
            ),
        )

        result = await builder.build(
            builder.validate({"code": "raise ValueError('bad')"})
        ).execute()

        assert result.status == "success"
        assert result.data["success"] is False
        assert "ValueError: bad" in result.data["error"]
        assert "[error]" in result.content

    @pytest.mark.asyncio
    async def test_code_exec_truncates_stdout_stderr_and_error(self, tmp_path):
        long_stdout = "o" * (MAX_OUTPUT_CHARS * 2)
        long_stderr = "e" * (MAX_OUTPUT_CHARS * 2)
        long_error = "x" * (MAX_OUTPUT_CHARS * 2)
        builder, _, _ = _build_tool(
            tmp_path,
            result=_sandbox_result(
                stdout=_protocol_bytes(
                    success=False,
                    stdout=long_stdout,
                    stderr=long_stderr,
                    error=long_error,
                ),
            ),
        )

        result = await builder.build(
            builder.validate({"code": "raise RuntimeError('boom')"})
        ).execute()

        assert result.status == "success"
        assert result.data["success"] is False
        assert result.content.count("... [output truncated:") == 3
        assert "... [output truncated:" in result.data["error"]
        assert result.data["error"].startswith("x" * (MAX_OUTPUT_CHARS // 2))
        assert result.data["error"].endswith("x" * (MAX_OUTPUT_CHARS // 2))
        assert len(result.data["error"]) < len(long_error)
