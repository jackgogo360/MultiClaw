"""Transport factory for MCP server configs."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from multiclaw.governance import SandboxController, SandboxExecRequest
from multiclaw.governance.sandbox.models import _is_secret_env_key

from ..types import (
    HTTPServerConfig,
    InProcessServerConfig,
    SSEServerConfig,
    ServerConfig,
    StdioServerConfig,
    WebSocketServerConfig,
)
from .base import BaseTransport
from .http import StreamableHTTPTransport
from .in_process import InProcessTransport
from .sse import SSETransport
from .stdio import StdioTransport
from .ws import WebSocketTransport


def create_transport(
    config: ServerConfig,
    *,
    sandbox_controller: SandboxController | None,
    workspace_root: Path | None,
    server_name: str,
) -> BaseTransport:
    match config:
        case StdioServerConfig():
            return _create_stdio_transport(
                config,
                sandbox_controller=sandbox_controller,
                workspace_root=workspace_root,
                server_name=server_name,
            )
        case SSEServerConfig():
            return SSETransport(
                url=config.url,
                headers=config.headers,
            )
        case HTTPServerConfig():
            return StreamableHTTPTransport(
                url=config.url,
                headers=config.headers,
            )
        case WebSocketServerConfig():
            return WebSocketTransport(
                url=config.url,
                headers=config.headers,
            )
        case InProcessServerConfig():
            _require_sandbox_context(
                sandbox_controller=sandbox_controller,
                workspace_root=workspace_root,
                server_name=server_name,
            )
            if sandbox_controller.mode != "host_unsafe_dev_only":
                raise RuntimeError(
                    f"MCP server '{server_name}' in-process transport requires host_unsafe_dev_only"
                )
            return InProcessTransport()
        case _:
            raise ValueError(f"Unknown server config type: {type(config)}")


def _create_stdio_transport(
    config: StdioServerConfig,
    *,
    sandbox_controller: SandboxController | None,
    workspace_root: Path | None,
    server_name: str,
) -> StdioTransport:
    _require_sandbox_context(
        sandbox_controller=sandbox_controller,
        workspace_root=workspace_root,
        server_name=server_name,
    )

    workspace = workspace_root.resolve(strict=True)
    explicit_roots = tuple(
        _canonical_grant_root(path, server_name=server_name)
        for path in config.sandbox_read_only_paths
    )
    controlled_path = _controlled_path(
        sandbox_controller=sandbox_controller,
        explicit_roots=explicit_roots,
    )
    executable = _resolve_command(
        config.command,
        server_name=server_name,
        controlled_path=controlled_path,
    )
    if not _is_executable_allowed(executable, explicit_roots, controlled_path):
        raise RuntimeError(f"MCP server '{server_name}' executable is not inside an allowed runtime root")

    cwd = _resolve_cwd(
        config.cwd,
        workspace_root=workspace,
        explicit_roots=explicit_roots,
        server_name=server_name,
    )
    env_overrides, allowed_secret_env = _env_overrides(
        config,
        server_name=server_name,
    )
    request = SandboxExecRequest(
        tool_name=f"mcp_stdio::{server_name}",
        profile_name=_mcp_profile_name(sandbox_controller),
        mode="exec_argv",
        argv=(str(executable), *config.args),
        workspace_root=workspace,
        cwd=cwd,
        timeout_seconds=30.0,
        env_overrides=env_overrides,
        allowed_secret_env=allowed_secret_env,
        network_mode=config.sandbox_network,
        workspace_mode=config.sandbox_workspace,
        allow_subprocesses=config.sandbox_allow_subprocesses,
        read_only_paths=explicit_roots,
        mcp_server_name=server_name,
    )

    spec = sandbox_controller.build_launch_spec(request)
    try:
        return StdioTransport(server_name=server_name, launch_spec=spec)
    except Exception:
        shutil.rmtree(spec.private_root, ignore_errors=True)
        raise


def _require_sandbox_context(
    *,
    sandbox_controller: SandboxController | None,
    workspace_root: Path | None,
    server_name: str,
) -> None:
    if sandbox_controller is None or workspace_root is None:
        raise RuntimeError(f"MCP server '{server_name}' requires sandbox controller context")


def _controller_default_path(sandbox_controller: SandboxController) -> str:
    default_path = getattr(sandbox_controller, "_default_path", None)
    if callable(default_path):
        return default_path()
    if isinstance(default_path, str):
        return default_path
    return "/usr/bin:/bin"


def _mcp_profile_name(sandbox_controller: SandboxController) -> str:
    settings = getattr(sandbox_controller, "_settings", None)
    profiles = getattr(settings, "profiles", None)
    profile_name = getattr(profiles, "mcp_stdio", None)
    if isinstance(profile_name, str) and profile_name:
        return profile_name
    return "mcp_stdio_local"


def _controlled_path(
    *,
    sandbox_controller: SandboxController,
    explicit_roots: tuple[Path, ...],
) -> str:
    entries = _controller_default_path(sandbox_controller).split(":")
    for root in explicit_roots:
        if root.is_file():
            if root.parent.name == "bin":
                entries.append(str(root.parent))
            continue
        if root.name == "bin":
            entries.append(str(root))
            continue
        candidate = root / "bin"
        if candidate.is_dir():
            entries.append(str(candidate.resolve(strict=True)))

    ordered: list[str] = []
    seen: set[Path] = set()
    for entry in entries:
        if not entry:
            continue
        canonical = Path(entry).resolve(strict=False)
        if canonical in seen:
            continue
        seen.add(canonical)
        ordered.append(str(canonical))
    return ":".join(ordered)


def _resolve_command(command: str, *, server_name: str, controlled_path: str) -> Path:
    if os.sep in command:
        return _canonical_executable(Path(command), server_name=server_name)
    discovered = shutil.which(command, path=controlled_path)
    if discovered is None:
        raise RuntimeError(f"MCP server '{server_name}' command could not be resolved")
    return _canonical_executable(Path(discovered), server_name=server_name)


def _canonical_executable(path: Path, *, server_name: str) -> Path:
    lexical = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"MCP server '{server_name}' command must exist") from exc
    if lexical != resolved:
        raise RuntimeError(f"MCP server '{server_name}' command must use its canonical path")
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError(f"MCP server '{server_name}' command must be executable")
    return resolved


def _canonical_grant_root(path: Path, *, server_name: str) -> Path:
    lexical = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"MCP server '{server_name}' read-only grant must exist") from exc
    if lexical != resolved:
        raise RuntimeError(f"MCP server '{server_name}' read-only grant must use its canonical path")
    return resolved


def _is_executable_allowed(
    executable: Path,
    explicit_roots: tuple[Path, ...],
    controlled_path: str,
) -> bool:
    for entry in controlled_path.split(":"):
        if not entry:
            continue
        candidate = Path(entry).resolve(strict=False)
        if executable.parent == candidate:
            return True
    for root in explicit_roots:
        if root.is_file():
            if executable == root:
                return True
            continue
        if executable == root or executable.is_relative_to(root):
            return True
    return False


def _resolve_cwd(
    configured_cwd: Path | None,
    *,
    workspace_root: Path,
    explicit_roots: tuple[Path, ...],
    server_name: str,
) -> Path:
    if configured_cwd is None:
        return workspace_root

    lexical = Path(os.path.abspath(os.path.expanduser(os.fspath(configured_cwd))))
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"MCP server '{server_name}' cwd must exist") from exc
    if lexical != resolved or not resolved.is_dir():
        raise RuntimeError(f"MCP server '{server_name}' cwd must use a canonical directory path")
    if resolved.is_relative_to(workspace_root):
        return resolved
    if any(
        resolved == root or (root.is_dir() and resolved.is_relative_to(root))
        for root in explicit_roots
    ):
        return workspace_root
    raise RuntimeError(f"MCP server '{server_name}' cwd must stay inside the workspace root")


def _env_overrides(
    config: StdioServerConfig,
    *,
    server_name: str,
) -> tuple[dict[str, str], frozenset[str]]:
    allowed = {name.upper() for name in config.sandbox_env_allowlist}
    overrides = dict(config.env)
    for key in overrides:
        normalized = key.upper()
        if _is_secret_env_key(normalized) and normalized not in allowed:
            raise RuntimeError(f"MCP server '{server_name}' secret env requires sandbox_env_allowlist")
    return overrides, frozenset(allowed)
