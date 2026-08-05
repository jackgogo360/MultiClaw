"""Tests for ShellTool."""
from pathlib import Path

import pytest

from sandbox_fakes import ReadyRecordingSandboxController, UnavailableSandboxController
from multiclaw.tools.shell import ShellParams, ShellToolBuilder


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "subdir").mkdir()
    (tmp_path / "hello.txt").write_text("hello world\n", encoding="utf-8")
    return tmp_path


class TestShellTool:
    @pytest.mark.asyncio
    async def test_shell_executes_simple_command(self, workspace):
        controller = ReadyRecordingSandboxController(workspace_root=workspace)
        builder = ShellToolBuilder(
            str(workspace),
            sandbox_controller=controller,
            profile_name="shell_workspace",
            allowed_commands=["echo"],
        )
        result = await builder.build(
            builder.validate({"command": "echo hello"})
        ).execute()
        assert result.status == "success"
        assert "hello" in result.content
        assert result.data == {"exit_code": 0}
        assert len(controller.requests) == 1
        request = controller.requests[0]
        assert request.mode == "shell_string"
        assert request.command == "echo hello"
        assert request.profile_name == "shell_workspace"
        assert request.cwd == workspace.resolve()

    @pytest.mark.asyncio
    async def test_shell_rejects_empty_command(self, workspace):
        builder = ShellToolBuilder(
            str(workspace),
            sandbox_controller=ReadyRecordingSandboxController(workspace_root=workspace),
        )
        result = await builder.build(
            builder.validate({"command": ""})
        ).execute()
        assert result.status == "error"
        assert "cannot be empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shell_rejects_dangerous_command(self, workspace):
        builder = ShellToolBuilder(
            str(workspace),
            sandbox_controller=ReadyRecordingSandboxController(workspace_root=workspace),
        )
        result = await builder.build(
            builder.validate({"command": "rm -rf /"})
        ).execute()
        assert result.status == "error"
        assert "blocked" in result.content.lower() or "dangerous" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shell_respects_cwd(self, workspace):
        controller = ReadyRecordingSandboxController(workspace_root=workspace)
        builder = ShellToolBuilder(
            str(workspace),
            sandbox_controller=controller,
            allowed_commands=["ls"],
        )
        result = await builder.build(
            builder.validate({"command": "ls", "cwd": "subdir"})
        ).execute()
        assert result.status == "success"
        assert controller.requests[0].cwd == (workspace / "subdir").resolve()

    @pytest.mark.asyncio
    async def test_shell_times_out_long_command(self, workspace):
        builder = ShellToolBuilder(
            str(workspace),
            sandbox_controller=ReadyRecordingSandboxController(workspace_root=workspace),
            allowed_commands=["sleep"],
        )
        result = await builder.build(
            builder.validate({"command": "sleep 10", "timeout": 0.5})
        ).execute()
        assert "timed out" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shell_captures_stderr(self, workspace):
        builder = ShellToolBuilder(
            str(workspace),
            sandbox_controller=ReadyRecordingSandboxController(workspace_root=workspace),
        )
        result = await builder.build(
            builder.validate({"command": "python3 -c 'import sys; sys.stderr.write(\"err msg\")'"})
        ).execute()
        assert result.status == "success"
        assert "err msg" in result.content

    @pytest.mark.asyncio
    async def test_shell_preserves_pipeline_redirect_quote_glob_and_env(self, workspace):
        builder = ShellToolBuilder(
            str(workspace),
            sandbox_controller=ReadyRecordingSandboxController(workspace_root=workspace),
        )
        command = (
            "VALUE='a b'; export VALUE; "
            "touch one.py two.py; "
            "printf '%s\\n' *.py > files.txt; "
            "printf '%s|' \"$VALUE\"; tail -n 1 files.txt | cat"
        )
        result = await builder.build(builder.validate({"command": command})).execute()
        assert result.status == "success"
        assert "a b|two.py" in result.content

    @pytest.mark.asyncio
    async def test_shell_preserves_nonzero_exit_code_and_stderr(self, workspace):
        builder = ShellToolBuilder(
            str(workspace),
            sandbox_controller=ReadyRecordingSandboxController(workspace_root=workspace),
        )
        result = await builder.build(
            builder.validate({"command": "printf problem >&2; exit 7"})
        ).execute()
        assert result.status == "success"
        assert "[stderr]" in result.content
        assert "problem" in result.content
        assert result.data == {"exit_code": 7}

    @pytest.mark.asyncio
    async def test_shell_returns_sanitized_profile_unavailable_error(self, workspace):
        builder = ShellToolBuilder(
            str(workspace),
            sandbox_controller=UnavailableSandboxController(),
        )

        result = await builder.build(
            builder.validate(
                {
                    "command": "printf %s \"$OPENAI_API_KEY\"",
                    "cwd": ".",
                }
            )
        ).execute()

        assert result.status == "error"
        assert result.content == "sandbox profile unavailable"
        assert "printf" not in result.content
        assert "OPENAI_API_KEY" not in result.content
