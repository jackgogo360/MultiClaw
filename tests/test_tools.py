from pathlib import Path

import pytest
from pydantic import BaseModel

from multiclaw import tools
from multiclaw.tools import _common
from multiclaw.tools import find_dir as find_dir_module
from multiclaw.tools import glob as glob_module
from multiclaw.tools import grep as grep_module
from multiclaw.tools import list_dir as list_dir_module
from multiclaw.events import EventBus
from multiclaw.governance import ExecutionGuard, InMemoryAuditLogger, PermissionChecker
from multiclaw.tools import (
    CoreToolScheduler,
    ToolBuilder,
    ToolExecutionResult,
    ToolInvocation,
    ToolRegistry,
    ToolStatus,
)
from multiclaw.tools.edit_file import EditFileToolBuilder, UndoEditToolBuilder
from multiclaw.tools.find_dir import FindDirToolBuilder
from multiclaw.tools.glob import GlobToolBuilder
from multiclaw.tools.grep import GrepToolBuilder
from multiclaw.tools.list_dir import ListDirToolBuilder
from multiclaw.tools.read_file import ReadFileToolBuilder
from multiclaw.tools.write_file import WriteFileToolBuilder
from sandbox_fakes import (
    ReadyRecordingSandboxController,
    UnavailableSandboxController,
)


class EchoParams(BaseModel):
    text: str


class EchoInvocation(ToolInvocation[EchoParams]):
    async def execute(self) -> ToolExecutionResult:
        return ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content=self.params.text,
            data={"echoed": self.params.text},
        )


class AuditedEchoInvocation(EchoInvocation):
    async def execute(self) -> ToolExecutionResult:
        result = await super().execute()
        result.audit.update(
            {
                "sandbox_profile": "shell_workspace",
                "sandbox_backend": "recording",
                "unsafe_fallback_used": False,
                "command": "echo $OPENAI_API_KEY",
            }
        )
        return result


class AuditedErrorInvocation(EchoInvocation):
    async def execute(self) -> ToolExecutionResult:
        result = ToolExecutionResult(
            status=ToolStatus.ERROR,
            content="boom",
        )
        result.audit.update(
            {
                "sandbox_profile": "shell_workspace",
                "sandbox_backend": "recording",
                "unsafe_fallback_used": False,
                "env": {"OPENAI_API_KEY": "secret"},
            }
        )
        return result


class EchoToolBuilder(ToolBuilder[EchoParams]):
    name = "echo"
    description = "Echoes the supplied text"
    parameters_schema = EchoParams

    def validate(self, params: dict) -> EchoParams:
        return EchoParams(**params)

    def build(self, params: EchoParams) -> ToolInvocation[EchoParams]:
        return EchoInvocation(name=self.name, params=params)


class DeleteToolBuilder(EchoToolBuilder):
    name = "delete_file"


class AuditedEchoToolBuilder(EchoToolBuilder):
    def build(self, params: EchoParams) -> ToolInvocation[EchoParams]:
        return AuditedEchoInvocation(name=self.name, params=params)


class GuardedAuditedToolBuilder(DeleteToolBuilder):
    def build(self, params: EchoParams) -> ToolInvocation[EchoParams]:
        return AuditedEchoInvocation(name=self.name, params=params)


class AuditedErrorToolBuilder(EchoToolBuilder):
    def build(self, params: EchoParams) -> ToolInvocation[EchoParams]:
        return AuditedErrorInvocation(name=self.name, params=params)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "main.py").write_text(
        "def hello():\n"
        "    return 'hello'\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "sub" / "nested.py").write_text(
        "VALUE = 42\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Demo\nhello world\n", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"enabled": true}\n', encoding="utf-8")
    return tmp_path


class TestToolBaseTypes:
    def test_tool_status_has_expected_values(self):
        assert [status.value for status in ToolStatus] == [
            "scheduled",
            "validating",
            "awaiting_approval",
            "executing",
            "success",
            "error",
            "cancelled",
        ]

    def test_tool_execution_result_carries_fields(self):
        result = ToolExecutionResult(
            status=ToolStatus.SUCCESS,
            content="hello",
            data={"echoed": "hello"},
            audit={"sandbox_backend": "recording", "secret": "hidden"},
        )

        assert result.status == ToolStatus.SUCCESS
        assert result.content == "hello"
        assert result.data == {"echoed": "hello"}
        assert result.audit == {"sandbox_backend": "recording", "secret": "hidden"}
        assert result.model_dump() == {
            "status": ToolStatus.SUCCESS,
            "content": "hello",
            "data": {"echoed": "hello"},
        }

    def test_tool_invocation_and_builder_are_abstract_bases(self):
        with pytest.raises(TypeError):
            ToolInvocation(name="base", params=EchoParams(text="hello"))

        with pytest.raises(TypeError):
            ToolBuilder()

        builder = EchoToolBuilder()
        invocation = builder.build(EchoParams(text="hello"))

        assert isinstance(builder, ToolBuilder)
        assert builder.parameters_schema is EchoParams
        assert isinstance(invocation, ToolInvocation)
        assert invocation.params == EchoParams(text="hello")

    def test_tools_package_exports_expected_surface(self):
        assert tools.CoreToolScheduler is CoreToolScheduler
        assert tools.ToolBuilder is ToolBuilder
        assert tools.ToolExecutionResult is ToolExecutionResult
        assert tools.ToolInvocation is ToolInvocation
        assert tools.ToolRegistry is ToolRegistry
        assert tools.ToolStatus is ToolStatus


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        builder = EchoToolBuilder()

        registry.register(builder)

        assert registry.get("echo") is builder
        assert [tool.name for tool in registry.list_all()] == ["echo"]

    def test_runtime_registry_matches_agent_code_tool_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
        monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
        monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
        from multiclaw.server import create_agent

        agent = create_agent(
            sandbox_controller=ReadyRecordingSandboxController(workspace_root=tmp_path)
        )

        assert [tool.name for tool in agent.registry.list_all()] == [
            "code_exec",
            "edit_file",
            "find_dir",
            "glob",
            "grep",
            "list_dir",
            "read_file",
            "shell",
            "undo_edit",
            "web_fetch",
            "web_search",
            "write_file",
        ]

    def test_runtime_registry_skips_unready_execution_tools(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MULTICLAW_DATABASE__PATH", str(tmp_path / "app.db"))
        monkeypatch.setenv("MULTICLAW_MCP__ENABLED", "false")
        monkeypatch.setenv("MULTICLAW_SKILL__ENABLED", "false")
        from multiclaw.server import create_agent

        agent = create_agent(sandbox_controller=UnavailableSandboxController())
        names = [tool.name for tool in agent.registry.list_all()]

        assert "shell" not in names
        assert "code_exec" not in names
        assert "read_file" in names
        assert "web_fetch" in names
        assert agent.sandbox_readiness.ready is False
        assert agent.sandbox_controller is not None


class TestCoreToolScheduler:
    @pytest.fixture
    def scheduler(self):
        return CoreToolScheduler(
            permission_checker=PermissionChecker(guarded_tools={"delete_file"}),
            execution_guard=ExecutionGuard(),
            audit_logger=InMemoryAuditLogger(),
            event_bus=EventBus(),
        )

    @pytest.mark.asyncio
    async def test_executes_safe_tool(self, scheduler):
        result = await scheduler.run(EchoToolBuilder(), {"text": "hello"})

        assert result.status == ToolStatus.SUCCESS
        assert result.content == "hello"
        assert result.data == {"echoed": "hello"}

    @pytest.mark.asyncio
    async def test_guarded_tool_blocks_for_approval(self, scheduler):
        import asyncio
        import uuid

        async def run():
            return await scheduler.run(DeleteToolBuilder(), {"text": "danger"})

        orig = uuid.uuid4
        uuid.uuid4 = lambda: type("FakeUUID", (), {"hex": "req-1"})()

        try:
            run_task = asyncio.create_task(run())
            await asyncio.sleep(0.02)
            scheduler.resolve_approval("req-1", True)
            result = await run_task
        finally:
            uuid.uuid4 = orig

        assert result.status == ToolStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_guarded_tool_rejected_by_user(self, scheduler):
        import asyncio
        import uuid

        async def run():
            return await scheduler.run(DeleteToolBuilder(), {"text": "danger"})

        orig = uuid.uuid4
        uuid.uuid4 = lambda: type("FakeUUID", (), {"hex": "req-2"})()

        try:
            run_task = asyncio.create_task(run())
            await asyncio.sleep(0.02)
            scheduler.resolve_approval("req-2", False)
            result = await run_task
        finally:
            uuid.uuid4 = orig

        assert result.status == ToolStatus.CANCELLED
        assert "rejected" in result.content

    @pytest.mark.asyncio
    async def test_records_audit_entries(self, scheduler):
        await scheduler.run(EchoToolBuilder(), {"text": "audit"})

        entries = await scheduler.audit_logger.list_entries()
        assert len(entries) == 1
        assert entries[0].tool_name == "echo"
        assert entries[0].status == "success"

    @pytest.mark.asyncio
    async def test_records_allowlisted_audit_prefix_for_normal_tools(self, scheduler):
        await scheduler.run(AuditedEchoToolBuilder(), {"text": "audit"})

        entries = await scheduler.audit_logger.list_entries()
        assert entries[-1].detail == (
            "[audit] sandbox_backend=recording sandbox_profile=shell_workspace "
            "unsafe_fallback_used=False\n"
            "audit"
        )
        assert "OPENAI_API_KEY" not in entries[-1].detail

    @pytest.mark.asyncio
    async def test_records_allowlisted_audit_prefix_after_approval(self, scheduler):
        import asyncio
        import uuid

        async def run():
            return await scheduler.run(GuardedAuditedToolBuilder(), {"text": "danger"})

        orig = uuid.uuid4
        uuid.uuid4 = lambda: type("FakeUUID", (), {"hex": "req-4"})()

        try:
            run_task = asyncio.create_task(run())
            await asyncio.sleep(0.02)
            scheduler.resolve_approval("req-4", True)
            result = await run_task
        finally:
            uuid.uuid4 = orig

        assert result.status == ToolStatus.SUCCESS
        entries = await scheduler.audit_logger.list_entries()
        assert entries[-1].detail == (
            "[audit] sandbox_backend=recording sandbox_profile=shell_workspace "
            "unsafe_fallback_used=False\n"
            "danger"
        )

    @pytest.mark.asyncio
    async def test_records_allowlisted_audit_prefix_for_mcp_results(self, scheduler):
        from multiclaw.mcp.tool_adapter import MCPToolBuilder

        class MCPParams(BaseModel):
            pass

        class AuditedMCPToolBuilder(MCPToolBuilder):
            def build(self, params):
                del params

                class _Invocation(ToolInvocation[MCPParams]):
                    async def execute(self_inner) -> ToolExecutionResult:
                        return ToolExecutionResult(
                            status=ToolStatus.SUCCESS,
                            content="mcp audit",
                            audit={
                                "sandbox_profile": "mcp_stdio_local",
                                "sandbox_backend": "recording",
                                "unsafe_fallback_used": True,
                                "env": {"OPENAI_API_KEY": "secret"},
                            },
                        )

                return _Invocation(name=self.name, params=MCPParams())

        result = await scheduler.run(
            AuditedMCPToolBuilder(
                name="mcp__demo__tool",
                server_name="demo",
                original_name="tool",
                description="demo",
                input_schema={},
                manager=object(),
            ),
            {},
        )

        assert result.status == ToolStatus.SUCCESS
        entries = await scheduler.audit_logger.list_entries()
        assert entries[-1].detail == (
            "[audit] sandbox_backend=recording sandbox_profile=mcp_stdio_local "
            "unsafe_fallback_used=True\n"
            "mcp audit"
        )
        assert "OPENAI_API_KEY" not in entries[-1].detail

    @pytest.mark.asyncio
    async def test_returned_error_uses_error_audit_status_and_error_event(self, scheduler):
        events = []

        async def handler(event):
            if event.type.startswith("tool."):
                events.append((event.type.removeprefix("tool."), event.data))

        scheduler.event_bus.subscribe("*", handler)

        result = await scheduler.run(AuditedErrorToolBuilder(), {"text": "ignored"})

        assert result.status == ToolStatus.ERROR
        assert events == [
            ("scheduled", {"tool": "echo"}),
            ("validating", {"tool": "echo"}),
            ("executing", {"tool": "echo"}),
            ("error", {"tool": "echo", "error": "tool returned error"}),
        ]
        entries = await scheduler.audit_logger.list_entries()
        assert entries[-1].status == ToolStatus.ERROR.value
        assert entries[-1].detail == (
            "[audit] sandbox_backend=recording sandbox_profile=shell_workspace "
            "unsafe_fallback_used=False\n"
            "boom"
        )
        assert "OPENAI_API_KEY" not in entries[-1].detail

    @pytest.mark.asyncio
    async def test_mcp_returned_error_uses_error_audit_status_and_error_event(self, scheduler):
        from multiclaw.mcp.tool_adapter import MCPToolBuilder

        events = []

        async def handler(event):
            if event.type.startswith("tool."):
                events.append((event.type.removeprefix("tool."), event.data))

        scheduler.event_bus.subscribe("*", handler)

        class MCPParams(BaseModel):
            pass

        class AuditedErrorMCPToolBuilder(MCPToolBuilder):
            def build(self, params):
                del params

                class _Invocation(ToolInvocation[MCPParams]):
                    async def execute(self_inner) -> ToolExecutionResult:
                        return ToolExecutionResult(
                            status=ToolStatus.ERROR,
                            content="mcp boom",
                            audit={
                                "sandbox_profile": "mcp_stdio_local",
                                "sandbox_backend": "recording",
                                "unsafe_fallback_used": True,
                                "env": {"OPENAI_API_KEY": "secret"},
                            },
                        )

                return _Invocation(name=self.name, params=MCPParams())

        result = await scheduler.run(
            AuditedErrorMCPToolBuilder(
                name="mcp__demo__tool",
                server_name="demo",
                original_name="tool",
                description="demo",
                input_schema={},
                manager=object(),
            ),
            {},
        )

        assert result.status == ToolStatus.ERROR
        assert events == [
            ("scheduled", {"tool": "mcp__demo__tool"}),
            ("validating", {"tool": "mcp__demo__tool"}),
            ("error", {"tool": "mcp__demo__tool", "error": "tool returned error"}),
        ]
        entries = await scheduler.audit_logger.list_entries()
        assert entries[-1].status == ToolStatus.ERROR.value
        assert entries[-1].detail == (
            "[audit] sandbox_backend=recording sandbox_profile=mcp_stdio_local "
            "unsafe_fallback_used=True\n"
            "mcp boom"
        )
        assert "OPENAI_API_KEY" not in entries[-1].detail

    @pytest.mark.asyncio
    async def test_safe_tool_emits_expected_event_order_and_audit_before_return(self, scheduler):
        events = []

        async def handler(event):
            if event.type.startswith("tool."):
                events.append(event.type.removeprefix("tool."))

        scheduler.event_bus.subscribe("*", handler)

        result = await scheduler.run(EchoToolBuilder(), {"text": "ordered"})

        assert result.status == ToolStatus.SUCCESS
        assert events == ["scheduled", "validating", "executing", "completed"]

        entries = await scheduler.audit_logger.list_entries()
        assert any(
            entry.tool_name == "echo"
            and entry.status == ToolStatus.SUCCESS.value
            and entry.detail == "ordered"
            for entry in entries
        )

    @pytest.mark.asyncio
    async def test_external_read_allowed_after_approval(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("outside text\n", encoding="utf-8")

        scheduler = CoreToolScheduler(
            permission_checker=PermissionChecker(),
            execution_guard=ExecutionGuard(),
            audit_logger=InMemoryAuditLogger(),
            event_bus=EventBus(),
        )
        builder = ReadFileToolBuilder(str(workspace))

        import asyncio
        import uuid

        orig = uuid.uuid4
        uuid.uuid4 = lambda: type("FakeUUID", (), {"hex": "req-3"})()

        try:
            run_task = asyncio.create_task(
                scheduler.run(builder, {"file_path": str(outside)})
            )
            await asyncio.sleep(0.02)
            scheduler.resolve_approval("req-3", True)
            result = await run_task
        finally:
            uuid.uuid4 = orig

        assert result.status == ToolStatus.SUCCESS
        assert "outside text" in result.content

    @pytest.mark.asyncio
    async def test_dangerous_external_write_stays_blocked_after_approval(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        dangerous_dir = tmp_path / ".ssh"
        dangerous_dir.mkdir()
        dangerous_file = dangerous_dir / "config"

        scheduler = CoreToolScheduler(
            permission_checker=PermissionChecker(),
            execution_guard=ExecutionGuard(),
            audit_logger=InMemoryAuditLogger(),
            event_bus=EventBus(),
        )
        builder = WriteFileToolBuilder(str(workspace))

        import asyncio
        import uuid

        orig = uuid.uuid4
        uuid.uuid4 = lambda: type("FakeUUID", (), {"hex": "req-4"})()

        try:
            run_task = asyncio.create_task(
                scheduler.run(
                    builder,
                    {"file_path": str(dangerous_file), "content": "Host *\n"},
                )
            )
            await asyncio.sleep(0.02)
            scheduler.resolve_approval("req-4", True)
            result = await run_task
        finally:
            uuid.uuid4 = orig

        assert result.status == ToolStatus.ERROR
        assert "dangerous path" in result.content


class TestFileTools:
    @pytest.mark.asyncio
    async def test_read_file_uses_agent_code_parameters(self, workspace):
        builder = ReadFileToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"file_path": "src/main.py", "offset": 1, "limit": 1})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "Lines 1-1" in result.content
        assert "1\tdef hello():" in result.content

    @pytest.mark.asyncio
    async def test_read_file_rejects_outside_workspace(self, workspace):
        builder = ReadFileToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"file_path": "/etc/passwd"})
        ).execute()

        assert result.status == ToolStatus.ERROR
        assert "outside workspace" in result.content

    @pytest.mark.asyncio
    async def test_read_file_suggests_similar_filename(self, workspace):
        builder = ReadFileToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"file_path": "confg.json"})
        ).execute()

        assert result.status == ToolStatus.ERROR
        assert "Did you mean" in result.content
        assert "config.json" in result.content

    @pytest.mark.asyncio
    async def test_read_file_falls_back_for_non_utf8_text(self, workspace):
        path = workspace / "utf16.txt"
        path.write_bytes("hello\nworld\n".encode("utf-16"))

        builder = ReadFileToolBuilder(str(workspace))
        result = await builder.build(
            builder.validate({"file_path": "utf16.txt"})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "hello" in result.content
        assert "world" in result.content

    @pytest.mark.asyncio
    async def test_read_file_binary_error_includes_tool_hint(self, workspace):
        path = workspace / "data.bin"
        path.write_bytes(b"\x00\x01\x02\x00\x03")

        builder = ReadFileToolBuilder(str(workspace))
        result = await builder.build(
            builder.validate({"file_path": "data.bin"})
        ).execute()

        assert result.status == ToolStatus.ERROR
        assert "Use appropriate tools" in result.content

    @pytest.mark.asyncio
    async def test_write_file_requires_read_before_overwrite(self, workspace):
        read_builder = ReadFileToolBuilder(str(workspace))
        write_builder = WriteFileToolBuilder(str(workspace), read_builder)

        result = await write_builder.build(
            write_builder.validate(
                {"file_path": "config.json", "content": '{"enabled": false}\n'}
            )
        ).execute()

        assert result.status == ToolStatus.ERROR
        assert "Read it first" in result.content

    @pytest.mark.asyncio
    async def test_write_file_allows_overwrite_after_read(self, workspace):
        read_builder = ReadFileToolBuilder(str(workspace))
        write_builder = WriteFileToolBuilder(str(workspace), read_builder)

        await read_builder.build(
            read_builder.validate({"file_path": "config.json"})
        ).execute()
        result = await write_builder.build(
            write_builder.validate(
                {"file_path": "config.json", "content": '{"enabled": false}\n'}
            )
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "Updated file" in result.content
        assert workspace.joinpath("config.json").read_text(encoding="utf-8") == '{"enabled": false}\n'

    @pytest.mark.asyncio
    async def test_edit_file_replaces_unique_match(self, workspace):
        builder = EditFileToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate(
                {
                    "file_path": "src/main.py",
                    "old_string": "return 'hello'",
                    "new_string": "return 'goodbye'",
                }
            )
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "Edited" in result.content
        assert "goodbye" in workspace.joinpath("src/main.py").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_edit_file_can_create_new_file(self, workspace):
        builder = EditFileToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate(
                {
                    "file_path": "notes/todo.txt",
                    "old_string": "",
                    "new_string": "ship it\n",
                }
            )
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert workspace.joinpath("notes/todo.txt").read_text(encoding="utf-8") == "ship it\n"

    @pytest.mark.asyncio
    async def test_undo_edit_reverts_previous_edit(self, workspace):
        edit_builder = EditFileToolBuilder(str(workspace))
        undo_builder = UndoEditToolBuilder(str(workspace), edit_builder)

        await edit_builder.build(
            edit_builder.validate(
                {
                    "file_path": "src/main.py",
                    "old_string": "return 'hello'",
                    "new_string": "return 'goodbye'",
                }
            )
        ).execute()

        result = await undo_builder.build(
            undo_builder.validate({"file_path": "src/main.py"})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "Reverted" in result.content
        assert "return 'hello'" in workspace.joinpath("src/main.py").read_text(encoding="utf-8")


class TestSearchTools:
    @pytest.mark.asyncio
    async def test_glob_finds_matching_files(self, workspace):
        builder = GlobToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"pattern": "*.py", "path": "src"})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "main.py" in result.content
        assert "nested.py" in result.content

    @pytest.mark.asyncio
    async def test_glob_ripgrep_branch_sorts_by_modified(self, workspace, monkeypatch):
        builder = GlobToolBuilder(str(workspace))
        calls = []

        def fake_run_command(cmd, cwd, timeout=30.0):
            calls.append(cmd)
            return 0, "./main.py\n", ""

        monkeypatch.setattr(glob_module.shutil, "which", lambda name: "/usr/bin/rg" if name == "rg" else None)
        monkeypatch.setattr(glob_module, "_run_command", fake_run_command)

        result = await builder.build(
            builder.validate({"pattern": "*.py", "path": "src"})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "--sort=modified" in calls[0]

    @pytest.mark.asyncio
    async def test_list_dir_supports_recursive_mode(self, workspace):
        builder = ListDirToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"dir_path": "src", "recursive": True})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "[DIR]  sub" in result.content
        assert "sub/nested.py" in result.content
        paths = [entry["path"] for entry in result.data["entries"]]
        assert paths[0] == "sub"
        assert "main.py" in paths
        assert "sub/nested.py" in paths

    @pytest.mark.asyncio
    async def test_grep_searches_content(self, workspace):
        builder = GrepToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"pattern": "hello", "path": "."})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "README.md" in result.content or "src/main.py" in result.content

    @pytest.mark.asyncio
    async def test_grep_falls_back_to_system_grep(self, workspace, monkeypatch):
        builder = GrepToolBuilder(str(workspace))
        calls = []

        def fake_which(name):
            if name == "rg":
                return None
            if name == "grep":
                return "/usr/bin/grep"
            return None

        def fake_run_command(cmd, cwd, timeout=30.0):
            calls.append(cmd)
            return 0, "./README.md:2:hello world\n", ""

        monkeypatch.setattr(grep_module.shutil, "which", fake_which)
        monkeypatch.setattr(grep_module, "_run_command", fake_run_command)

        result = await builder.build(
            builder.validate({"pattern": "hello", "path": "."})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert calls[0][0] == "grep"
        assert "README.md:2:hello world" in result.content

    @pytest.mark.asyncio
    async def test_grep_files_with_matches_ripgrep_branch_sorts_by_modified(self, workspace, monkeypatch):
        builder = GrepToolBuilder(str(workspace))
        calls = []

        def fake_run_command(cmd, cwd, timeout=30.0):
            calls.append(cmd)
            return 0, "./README.md\n", ""

        monkeypatch.setattr(grep_module.shutil, "which", lambda name: "/usr/bin/rg" if name == "rg" else None)
        monkeypatch.setattr(grep_module, "_run_command", fake_run_command)

        result = await builder.build(
            builder.validate({"pattern": "hello", "path": ".", "output_mode": "files_with_matches"})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "--sort=modified" in calls[0]

    @pytest.mark.asyncio
    async def test_find_dir_matches_nested_directory(self, workspace):
        builder = FindDirToolBuilder(str(workspace))

        result = await builder.build(
            builder.validate({"pattern": "sub", "path": "src", "max_depth": 2})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "sub" in result.content

    @pytest.mark.asyncio
    async def test_find_dir_reports_truncation(self, workspace, monkeypatch):
        for index in range(5):
            (workspace / f"dir-{index}").mkdir()

        builder = FindDirToolBuilder(str(workspace))
        monkeypatch.setattr(find_dir_module, "MAX_FIND_DIR_RESULTS", 3)

        result = await builder.build(
            builder.validate({"pattern": "dir-*", "path": ".", "max_depth": 1})
        ).execute()

        assert result.status == ToolStatus.SUCCESS
        assert "limited to 3" in result.content
