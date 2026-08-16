"""Integration tests for MCP -> ToolRegistry -> execution pipeline."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

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
from multiclaw.secrets.resolver import ResolvedSecret, SecretBytes
from multiclaw.tenancy.context import TenantContext
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


def test_manager_callback_receives_refreshed_list_after_state_replacement():
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

    with patch("multiclaw.mcp.manager.logger.exception") as mock_exception:
        manager._on_tools_changed("alpha", [new_tool])

    assert [tool.original_name for tool in manager.get_server_states()["alpha"].tools] == ["new"]
    assert seen == [["new"]]
    mock_exception.assert_called_once_with(
        "Tools changed callback failed for server '%s'",
        "alpha",
    )


@pytest.mark.asyncio
async def test_manager_resolves_secret_refs_per_tenant_without_mutating_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    class _FakeTransport:
        pass

    class _FakeClient:
        def __init__(self, name: str, transport) -> None:
            self.connected = True

        def set_on_tools_changed(self, callback) -> None:
            self._callback = callback

        async def connect(self) -> None:
            return None

        async def discover_tools(self) -> list[ToolInfo]:
            return []

        async def disconnect(self) -> None:
            return None

    class _FakeResolver:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        async def resolve_reference(self, context: TenantContext, reference: str) -> ResolvedSecret:
            self.calls.append((context.tenant_id, "mcp", reference))
            suffix = "tenant-a-token" if context.tenant_id == "tenant-a" else "tenant-b-token"
            return ResolvedSecret(
                provider_kind="mcp",
                provider_name="demo",
                secret_name="API_TOKEN",
                source="user",
                masked_value="****oken",
                secret_bytes=SecretBytes(suffix.encode("utf-8")),
            )

    def fake_create_transport(config, **kwargs):
        del kwargs
        captured.append(
            {
                "env": dict(getattr(config, "env", {})),
                "headers": dict(getattr(config, "headers", {})),
            }
        )
        return _FakeTransport()

    monkeypatch.setattr("multiclaw.mcp.manager.create_transport", fake_create_transport)
    monkeypatch.setattr("multiclaw.mcp.manager.MCPClient", _FakeClient)

    config = StdioServerConfig(
        command="/bin/echo",
        env={
            "VISIBLE_FLAG": "literal",
            "API_TOKEN": "secret://mcp/demo/API_TOKEN",
        },
    )
    resolver = _FakeResolver()

    manager_a = MCPClientManager(secret_resolver=resolver, tenant_context=TenantContext("tenant-a", "workspace-a"))
    manager_b = MCPClientManager(secret_resolver=resolver, tenant_context=TenantContext("tenant-b", "workspace-b"))

    await manager_a._connect_server("demo", config)
    await manager_b._connect_server("demo", config)

    assert captured[0]["env"]["VISIBLE_FLAG"] == "literal"
    assert captured[0]["env"]["API_TOKEN"] == "tenant-a-token"
    assert captured[1]["env"]["API_TOKEN"] == "tenant-b-token"
    assert config.env["API_TOKEN"] == "secret://mcp/demo/API_TOKEN"
    assert config.env["VISIBLE_FLAG"] == "literal"
    assert resolver.calls == [
        ("tenant-a", "mcp", "secret://mcp/demo/API_TOKEN"),
        ("tenant-b", "mcp", "secret://mcp/demo/API_TOKEN"),
    ]


@pytest.mark.asyncio
async def test_secret_backed_http_manager_re_resolves_each_tool_call_and_scrubs_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_transports: list[object] = []
    client_instances: list[object] = []

    class _FakeTransport:
        def __init__(self, headers: dict[str, str]) -> None:
            self._headers = dict(headers)
            self.disconnect_calls = 0
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            self.connected = False

    class _FakeClient:
        def __init__(self, name: str, transport) -> None:
            self.name = name
            self._transport = transport
            self.connected = False
            self.tool_call_count = 0
            client_instances.append(self)

        def set_on_tools_changed(self, callback) -> None:
            self._callback = callback

        async def connect(self) -> None:
            self.connected = True
            await self._transport.connect()

        async def discover_tools(self) -> list[ToolInfo]:
            return [
                ToolInfo(
                    name="mcp__demo__ping",
                    server_name="demo",
                    original_name="ping",
                    description="ping",
                    input_schema={},
                )
            ]

        async def disconnect(self) -> None:
            self.connected = False
            await self._transport.disconnect()

        async def call_tool_with_retry(self, tool_name: str, arguments: dict[str, object]):
            del tool_name, arguments
            self.tool_call_count += 1
            return type("Result", (), {"content": [{"type": "text", "text": "ok"}], "is_error": False, "external_request_id": None})()

    class _FakeResolver:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def resolve_reference(self, context: TenantContext, reference: str) -> ResolvedSecret:
            self.calls.append((context.tenant_id, reference))
            token = "tenant-a-token"
            return ResolvedSecret(
                provider_kind="mcp",
                provider_name="demo",
                secret_name="Authorization",
                source="user",
                masked_value="****oken",
                secret_bytes=SecretBytes(token.encode("utf-8")),
            )

    def fake_create_transport(config, **kwargs):
        del kwargs
        transport = _FakeTransport(getattr(config, "headers", {}))
        created_transports.append(transport)
        return transport

    monkeypatch.setattr("multiclaw.mcp.manager.create_transport", fake_create_transport)
    monkeypatch.setattr("multiclaw.mcp.manager.MCPClient", _FakeClient)

    resolver = _FakeResolver()
    manager = MCPClientManager(
        secret_resolver=resolver,
        tenant_context=TenantContext("tenant-a", "workspace-a"),
    )
    config = HTTPServerConfig(
        url="https://example.com/mcp",
        headers={"Authorization": "secret://mcp/demo/Authorization"},
    )

    await manager._connect_server("demo", config)
    assert resolver.calls == [("tenant-a", "secret://mcp/demo/Authorization")]

    await manager._call_tool_async("demo", "ping", {})
    await manager._call_tool_async("demo", "ping", {})

    assert resolver.calls == [
        ("tenant-a", "secret://mcp/demo/Authorization"),
        ("tenant-a", "secret://mcp/demo/Authorization"),
        ("tenant-a", "secret://mcp/demo/Authorization"),
    ]
    assert all(transport._headers.get("Authorization") != "tenant-a-token" for transport in created_transports)
    assert all(client.connected is False for client in client_instances)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("boom"), asyncio.CancelledError()])
async def test_secret_backed_stdio_manager_scrubs_on_failure_or_cancel(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    created_transports: list[object] = []

    class _FakeTransport:
        def __init__(self, env: dict[str, str]) -> None:
            self._launch_spec = type("LaunchSpec", (), {"env": dict(env)})()
            self.disconnect_calls = 0

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    class _FakeClient:
        def __init__(self, name: str, transport) -> None:
            del name
            self.connected = False
            self._transport = transport

        def set_on_tools_changed(self, callback) -> None:
            self._callback = callback

        async def connect(self) -> None:
            self.connected = True
            await self._transport.connect()

        async def discover_tools(self) -> list[ToolInfo]:
            return []

        async def disconnect(self) -> None:
            self.connected = False
            await self._transport.disconnect()

        async def call_tool_with_retry(self, tool_name: str, arguments: dict[str, object]):
            del tool_name, arguments
            raise failure

    class _FakeResolver:
        async def resolve_reference(self, context: TenantContext, reference: str) -> ResolvedSecret:
            del context, reference
            return ResolvedSecret(
                provider_kind="mcp",
                provider_name="demo",
                secret_name="API_TOKEN",
                source="user",
                masked_value="****oken",
                secret_bytes=SecretBytes(b"tenant-token"),
            )

    def fake_create_transport(config, **kwargs):
        del kwargs
        transport = _FakeTransport(getattr(config, "env", {}))
        created_transports.append(transport)
        return transport

    monkeypatch.setattr("multiclaw.mcp.manager.create_transport", fake_create_transport)
    monkeypatch.setattr("multiclaw.mcp.manager.MCPClient", _FakeClient)

    manager = MCPClientManager(
        secret_resolver=_FakeResolver(),
        tenant_context=TenantContext("tenant-a", "workspace-a"),
    )
    config = StdioServerConfig(
        command="/bin/echo",
        env={"API_TOKEN": "secret://mcp/demo/API_TOKEN"},
    )

    await manager._connect_server("demo", config)
    with pytest.raises(type(failure)):
        await manager._call_tool_async("demo", "ping", {})

    assert all(transport._launch_spec.env.get("API_TOKEN") != "tenant-token" for transport in created_transports)
    assert all(transport.disconnect_calls >= 1 for transport in created_transports)


@pytest.mark.asyncio
async def test_literal_http_manager_keeps_reusable_connection_without_secret_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_transports: list[object] = []

    class _FakeTransport:
        def __init__(self, headers: dict[str, str]) -> None:
            self._headers = dict(headers)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, name: str, transport) -> None:
            del name
            self.connected = False
            self._transport = transport
            self.tool_call_count = 0

        def set_on_tools_changed(self, callback) -> None:
            self._callback = callback

        async def connect(self) -> None:
            self.connected = True
            await self._transport.connect()

        async def discover_tools(self) -> list[ToolInfo]:
            return []

        async def disconnect(self) -> None:
            self.connected = False
            await self._transport.disconnect()

        async def call_tool_with_retry(self, tool_name: str, arguments: dict[str, object]):
            del tool_name, arguments
            self.tool_call_count += 1
            return type("Result", (), {"content": [{"type": "text", "text": "ok"}], "is_error": False, "external_request_id": None})()

    def fake_create_transport(config, **kwargs):
        del kwargs
        transport = _FakeTransport(getattr(config, "headers", {}))
        created_transports.append(transport)
        return transport

    monkeypatch.setattr("multiclaw.mcp.manager.create_transport", fake_create_transport)
    monkeypatch.setattr("multiclaw.mcp.manager.MCPClient", _FakeClient)

    manager = MCPClientManager(tenant_context=TenantContext("tenant-a", "workspace-a"))
    config = HTTPServerConfig(
        url="https://example.com/mcp",
        headers={"Authorization": "Bearer literal-token"},
    )

    await manager._connect_server("demo", config)
    await manager._call_tool_async("demo", "ping", {})
    await manager._call_tool_async("demo", "ping", {})

    assert len(created_transports) == 1
    assert created_transports[0]._headers["Authorization"] == "Bearer literal-token"


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
        workspace_root=workspace / ".",
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
    assert request.workspace_root == workspace.resolve()
    assert request.cwd == workspace.resolve()
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


def test_create_transport_requires_sandbox_context_for_in_process_transport() -> None:
    with pytest.raises(RuntimeError, match="sandbox controller context"):
        create_transport(
            InProcessServerConfig(server_factory=lambda: object()),
            sandbox_controller=None,
            workspace_root=None,
            server_name="local-inproc",
        )


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


def test_manager_without_sandbox_context_fails_closed_for_local_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[str] = []

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

    monkeypatch.setattr("multiclaw.mcp.manager.MCPClient", FakeClient)

    manager = MCPClientManager()
    states = manager.connect_servers(
        {
            "local-stdio": StdioServerConfig(command="/usr/bin/env"),
            "local-inproc": InProcessServerConfig(server_factory=lambda: object()),
            "remote": HTTPServerConfig(url="https://example.com/mcp"),
        }
    )

    assert states["local-stdio"].status is ServerStatus.FAILED
    assert states["local-inproc"].status is ServerStatus.FAILED
    assert states["remote"].status is ServerStatus.CONNECTED
    assert connected == ["remote"]
    assert "sandbox controller context" in (states["local-stdio"].error or "")
    assert "sandbox controller context" in (states["local-inproc"].error or "")


def test_manager_stop_is_idempotent_after_start() -> None:
    manager = MCPClientManager()

    manager.start()
    manager.stop()
    manager.stop()

    assert manager._started is False
