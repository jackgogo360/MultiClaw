"""Tests for ShellTool."""
from pathlib import Path
import pytest
from multiclaw.tools.shell import ShellParams, ShellToolBuilder


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "subdir").mkdir()
    (tmp_path / "hello.txt").write_text("hello world\n", encoding="utf-8")
    return tmp_path


class TestShellTool:
    @pytest.mark.asyncio
    async def test_shell_executes_simple_command(self, workspace):
        builder = ShellToolBuilder(str(workspace), allowed_commands=["echo"])
        result = await builder.build(
            builder.validate({"command": "echo hello"})
        ).execute()
        assert result.status == "success"
        assert "hello" in result.content

    @pytest.mark.asyncio
    async def test_shell_rejects_empty_command(self, workspace):
        builder = ShellToolBuilder(str(workspace))
        result = await builder.build(
            builder.validate({"command": ""})
        ).execute()
        assert result.status == "error"
        assert "cannot be empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shell_rejects_dangerous_command(self, workspace):
        builder = ShellToolBuilder(str(workspace))
        result = await builder.build(
            builder.validate({"command": "rm -rf /"})
        ).execute()
        assert result.status == "error"
        assert "blocked" in result.content.lower() or "dangerous" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shell_respects_cwd(self, workspace):
        builder = ShellToolBuilder(str(workspace), allowed_commands=["ls"])
        result = await builder.build(
            builder.validate({"command": "ls", "cwd": str(workspace / "subdir")})
        ).execute()
        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_shell_times_out_long_command(self, workspace):
        builder = ShellToolBuilder(str(workspace), allowed_commands=["sleep"])
        result = await builder.build(
            builder.validate({"command": "sleep 10", "timeout": 0.5})
        ).execute()
        assert "timed out" in result.content.lower()

    @pytest.mark.asyncio
    async def test_shell_captures_stderr(self, workspace):
        builder = ShellToolBuilder(str(workspace))
        result = await builder.build(
            builder.validate({"command": "python3 -c 'import sys; sys.stderr.write(\"err msg\")'"})
        ).execute()
        assert result.status == "success"
        assert "err msg" in result.content
