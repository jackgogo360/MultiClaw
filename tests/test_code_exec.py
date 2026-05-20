"""Tests for CodeExecTool."""
import pytest
from multiclaw.tools.code_exec import CodeExecParams, CodeExecToolBuilder


class TestCodeExecTool:
    @pytest.mark.asyncio
    async def test_code_exec_runs_simple_code(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path))
        result = await builder.build(
            builder.validate({"code": "x = 1 + 2\nprint(x)"})
        ).execute()
        assert result.status == "success"
        assert "3" in result.content

    @pytest.mark.asyncio
    async def test_code_exec_rejects_empty_code(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path))
        result = await builder.build(
            builder.validate({"code": ""})
        ).execute()
        assert result.status == "error"
        assert "cannot be empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_code_exec_captures_exception(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path))
        result = await builder.build(
            builder.validate({"code": "raise ValueError('bad')"})
        ).execute()
        assert result.status == "success"
        assert "ValueError" in result.content

    @pytest.mark.asyncio
    async def test_code_exec_blocks_subprocess_import(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path), restrict_builtins=True)
        result = await builder.build(
            builder.validate({"code": "import subprocess; print('ok')"})
        ).execute()
        assert result.status == "success"
        assert "ImportError" in result.content or "not allowed" in result.content

    @pytest.mark.asyncio
    async def test_code_exec_times_out(self, tmp_path):
        builder = CodeExecToolBuilder(str(tmp_path))
        result = await builder.build(
            builder.validate({"code": "while True: pass", "timeout": 1.0})
        ).execute()
        assert "timed out" in result.content.lower()
