"""Integration tests for MCP -> ToolRegistry -> execution pipeline."""
from dataclasses import dataclass
from pathlib import Path

import pytest

from multiclaw.config.settings import SandboxSettings
from multiclaw.governance.sandbox.manager import SandboxManager
from multiclaw.mcp.manager import MCPClientManager
from multiclaw.mcp.transport.factory import create_transport
from multiclaw.mcp.transport.http import StreamableHTTPTransport
from multiclaw.mcp.transport.in_process import InProcessTransport
from multiclaw.mcp.transport.stdio import StdioTransport
from multiclaw.mcp.tool_adapter import MCPToolBuilder, _json_schema_to_pydantic
from multiclaw.mcp.types import HTTPServerConfig, InProcessServerConfig, ServerState, ServerStatus, StdioServerConfig, ToolInfo
from multiclaw.tools.registry import ToolRegistry

from test_sandbox_manager import RecordingBackend


class TestMCPToolBuilderRegistration:
    def test_mcp_tool_registers_and_produces_openai_schema(self):
        schema = {
            "properties": {
                "path": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["path"],
        }
        builder = MCPToolBuilder.__new__(MCPToolBuilder)
        builder.name = "mcp__test__read_file"
        builder.description = "Read a file from the filesystem"
        builder._server_name = "test"
        builder._original_name = "read_file"
        builder._manager = None
        builder.parameters_schema = _json_schema_to_pydantic(schema)

        registry = ToolRegistry()
        registry.register(builder)

        schemas = registry.to_openai_schemas()
        assert len(schemas) == 1
        schema_obj = schemas[0]
        assert schema_obj["type"] == "function"
        assert schema_obj["function"]["name"] == "mcp__test__read_file"
        assert "description" in schema_obj["function"]
        assert "parameters" in schema_obj["function"]

    def test_mcp_tool_builder_validate(self):
        schema = {
            "properties": {
                "path": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["path"],
        }
        builder = MCPToolBuilder.__new__(MCPToolBuilder)
        builder.name = "mcp__test__tool"
        builder.description = "test"
        builder._server_name = "test"
        builder._original_name = "tool"
        builder._manager = None
        builder.parameters_schema = _json_schema_to_pydantic(schema)

        params = builder.validate({"path": "/tmp", "count": 3})
        assert params.path == "/tmp"
        assert params.count == 3

    def test_mcp_tool_name_format(self):
        builder = MCPToolBuilder.__new__(MCPToolBuilder)
        builder.name = "mcp__filesystem__read_file"
        builder.description = "..."
        builder._server_name = "filesystem"
        builder._original_name = "read_file"
        builder._manager = None
        builder.parameters_schema = _json_schema_to_pydantic({})

        assert builder.name.startswith("mcp__")
        assert "filesystem" in builder.name
        assert "read_file" in builder.name


@dataclass
class _DummyConfig:
    name: str = "dummy"


def test_registry_replaces_mcp_server_namespace():
    registry = ToolRegistry()

    retained = MCPToolBuilder.__new__(MCPToolBuilder)
    retained.name = "shell"
    retained.description = "shell"
    retained._server_name = "local"
    retained._original_name = "shell"
    retained._manager = None
    retained.parameters_schema = _json_schema_to_pydantic({})

    old_a = MCPToolBuilder.__new__(MCPToolBuilder)
    old_a.name = "mcp__demo_server__read_file"
    old_a.description = "old a"
    old_a._server_name = "demo"
    old_a._original_name = "read_file"
    old_a._manager = None
    old_a.parameters_schema = _json_schema_to_pydantic({})

    old_b = MCPToolBuilder.__new__(MCPToolBuilder)
    old_b.name = "mcp__demo_server__write_file"
    old_b.description = "old b"
    old_b._server_name = "demo"
    old_b._original_name = "write_file"
    old_b._manager = None
    old_b.parameters_schema = _json_schema_to_pydantic({})

    other_namespace = MCPToolBuilder.__new__(MCPToolBuilder)
    other_namespace.name = "mcp__other__list_dir"
    other_namespace.description = "other"
    other_namespace._server_name = "other"
    other_namespace._original_name = "list_dir"
    other_namespace._manager = None
    other_namespace.parameters_schema = _json_schema_to_pydantic({})

    replacement = MCPToolBuilder.__new__(MCPToolBuilder)
    replacement.name = "mcp__demo_server__stat_file"
    replacement.description = "replacement"
    replacement._server_name = "demo"
    replacement._original_name = "stat_file"
    replacement._manager = None
    replacement.parameters_schema = _json_schema_to_pydantic({})

    registry.register(retained)
    registry.register(old_a)
    registry.register(old_b)
    registry.register(other_namespace)

    registry.replace_namespace("mcp__demo_server__", [replacement])

    names = [builder.name for builder in registry.list_all()]
    assert names == [
        "mcp__demo_server__stat_file",
        "mcp__other__list_dir",
        "shell",
    ]


def test_manager_callback_receives_refreshed_list_after_state_replacement(caplog):
    manager = MCPClientManager()
    old_tool = ToolInfo(
        name="mcp__alpha__old",
        server_name="alpha",
        original_name="old",
        description="old",
        input_schema={},
    )
    new_tool = ToolInfo(
        name="mcp__alpha__new",
        server_name="alpha",
        original_name="new",
        description="new",
        input_schema={},
    )
    manager._states["alpha"] = ServerState(
        name="alpha",
        config=_DummyConfig(),
        status=ServerStatus.CONNECTED,
        tools=[old_tool],
    )

    seen: list[list[str]] = []

    def callback(server_name: str, tools: list[ToolInfo]) -> None:
        assert server_name == "alpha"
        assert [tool.original_name for tool in tools] == ["new"]
        seen.append([tool.original_name for tool in manager.get_server_states()["alpha"].tools])
        raise RuntimeError("boom")

    manager.set_tools_changed_callback(callback)

    with caplog.at_level("ERROR"):
        manager._on_tools_changed("alpha", [new_tool])

    assert [tool.original_name for tool in manager.get_server_states()["alpha"].tools] == ["new"]
    assert seen == [["new"]]
    assert "Tools changed callback failed for server 'alpha'" in caplog.text


def test_create_transport_builds_sandboxed_stdio_launch_spec_with_controlled_grants(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    grant_root = tmp_path / "grants"
    grant_bin = grant_root / "bin"
    grant_bin.mkdir(parents=True)
    executable = grant_bin / "demo-mcp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    backend = RecordingBackend(name="recording")
    controller = SandboxManager.create(
        settings=SandboxSettings(),
        debug=False,
        workspace_root=workspace,
        backend_override=backend,
        platform_name="Linux",
    )
    controller.initialize()

    transport = create_transport(
        StdioServerConfig(
            command=str(executable),
            args=["--serve"],
            env={"VISIBLE_FLAG": "1", "API_TOKEN": "secret-token"},
            sandbox_env_allowlist=["API_TOKEN"],
            sandbox_read_only_paths=[grant_root],
        ),
        sandbox_controller=controller,
        workspace_root=workspace,
        server_name="demo",
    )

    assert isinstance(transport, StdioTransport)
    request = backend.build_calls[-1]["request"]
    assert request.profile_name == "mcp_stdio_local"
    assert request.mode == "exec_argv"
    assert request.argv == (str(executable.resolve()), "--serve")
    assert request.allowed_secret_env == frozenset({"API_TOKEN"})
    assert request.read_only_paths == ()

    environment = backend.build_calls[-1]["environment"]
    assert environment.env["VISIBLE_FLAG"] == "1"
    assert environment.env["API_TOKEN"] == "secret-token"
    assert environment.env["PATH"] == f"/usr/bin:/bin:{grant_bin.resolve()}"


def test_create_transport_rejects_implicit_secret_or_ungranted_executable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool_root = tmp_path / "tools"
    tool_root.mkdir()
    executable = tool_root / "demo-mcp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    controller = SandboxManager.create(
        settings=SandboxSettings(),
        debug=False,
        workspace_root=workspace,
        backend_override=RecordingBackend(name="recording"),
        platform_name="Linux",
    )
    controller.initialize()

    with pytest.raises(RuntimeError, match="demo"):
        create_transport(
            StdioServerConfig(
                command=str(executable),
                env={"API_TOKEN": "secret-token"},
            ),
            sandbox_controller=controller,
            workspace_root=workspace,
            server_name="demo",
        )

    with pytest.raises(RuntimeError, match="demo"):
        create_transport(
            StdioServerConfig(command=str(executable)),
            sandbox_controller=controller,
            workspace_root=workspace,
            server_name="demo",
        )


def test_create_transport_rejects_in_process_in_auto_mode_but_allows_remote(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = SandboxManager.create(
        settings=SandboxSettings(),
        debug=False,
        workspace_root=workspace,
        backend_override=RecordingBackend(name="recording"),
        platform_name="Linux",
    )
    controller.initialize()

    with pytest.raises(RuntimeError, match="in-process"):
        create_transport(
            InProcessServerConfig(server_factory=lambda: object()),
            sandbox_controller=controller,
            workspace_root=workspace,
            server_name="local-inproc",
        )

    remote = create_transport(
        HTTPServerConfig(url="https://example.com/mcp"),
        sandbox_controller=controller,
        workspace_root=workspace,
        server_name="remote",
    )
    assert isinstance(remote, StreamableHTTPTransport)


def test_manager_marks_bad_stdio_failed_without_blocking_remote(monkeypatch) -> None:
    connected: list[str] = []

    class FakeTransport:
        pass

    class FakeClient:
        def __init__(self, name, transport):
            self.name = name
            self.transport = transport

        def set_on_tools_changed(self, callback) -> None:
            self.callback = callback

        async def connect(self) -> None:
            connected.append(self.name)

        async def discover_tools(self) -> list[ToolInfo]:
            return []

        async def disconnect(self) -> None:
            return None

        @property
        def connected(self) -> bool:
            return True

    def fake_create_transport(config, *, sandbox_controller, workspace_root, server_name):
        del config, sandbox_controller, workspace_root
        if server_name == "local":
            raise RuntimeError("local failure /tmp/secret")
        return FakeTransport()

    monkeypatch.setattr("multiclaw.mcp.manager.create_transport", fake_create_transport)
    monkeypatch.setattr("multiclaw.mcp.manager.MCPClient", FakeClient)

    manager = MCPClientManager()
    states = manager.connect_servers(
        {
            "local": StdioServerConfig(command="/usr/bin/env"),
            "remote": HTTPServerConfig(url="https://example.com/mcp"),
        }
    )

    assert states["local"].status is ServerStatus.FAILED
    assert states["remote"].status is ServerStatus.CONNECTED
    assert connected == ["remote"]
    assert "/tmp/secret" not in (states["local"].error or "")
