import asyncio
import json
import logging
import re
import threading
import tempfile
from contextlib import asynccontextmanager
from collections.abc import Iterable
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

LOG_DIR = Path.home() / ".multiclaw" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _log_namer(default_name: str) -> str:
    # TimedRotatingFileHandler produces: /path/multiclaw.log.20260521
    # Rename to:                        /path/multiclaw-20260521.log
    parts = default_name.rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        return f"{parts[0]}-{parts[1]}.log"
    return default_name


_file_handler = TimedRotatingFileHandler(
    filename=str(LOG_DIR / "multiclaw.log"),
    when="midnight",
    interval=1,
    backupCount=30,
    encoding="utf-8",
)
_file_handler.suffix = "%Y%m%d"
_file_handler.namer = _log_namer
_file_handler.setFormatter(
    logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s %(message)s",
        datefmt="%Y%m%d %H:%M:%S",
    )
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s %(message)s",
    datefmt="%Y%m%d %H:%M:%S",
    handlers=[_file_handler],
)
logger = logging.getLogger("multiclaw")


def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    if "nodename nor servname" in msg or "Name or service not known" in msg:
        return (
            "Network error: DNS resolution failed. Please check your internet connection.\n\n"
            f"Details: {msg}"
        )
    if "CERTIFICATE_VERIFY_FAILED" in msg or "self-signed certificate" in msg:
        return (
            "Network error: SSL verification failed. A proxy or VPN may be intercepting "
            "the connection, or the server certificate is invalid.\n\n"
            f"Details: {msg}"
        )
    if "ConnectError" in msg or "connect" in msg.lower():
        return (
            "Network error: Unable to connect to the API server. "
            "Please check your internet connection and try again.\n\n"
            f"Details: {msg}"
        )
    if "Timeout" in msg or "timed out" in msg:
        return (
            "Request timed out. The server may be busy or your connection is slow. "
            "Please try again.\n\n"
            f"Details: {msg}"
        )
    return msg


from multiclaw.agent import MultiClawAgent
from multiclaw.config import Settings
from multiclaw.events import Event, EventBus
from multiclaw.governance import (
    ExecutionGuard,
    InMemoryAuditLogger,
    PermissionChecker,
    SandboxController,
    SandboxProcessRunner,
    SandboxReadiness,
)
from multiclaw.governance.sandbox.manager import SandboxManager
from multiclaw.llm import ModelRouter
from multiclaw.memory import SqliteMemory
from multiclaw.planner import Planner
from multiclaw.session import SqliteSessionStore, SessionStatus
from multiclaw.tools import (
    CoreToolScheduler,
    ToolRegistry,
)
from multiclaw.tools.code_exec import CodeExecToolBuilder
from multiclaw.tools.edit_file import EditFileToolBuilder, UndoEditToolBuilder
from multiclaw.tools.find_dir import FindDirToolBuilder
from multiclaw.tools.glob import GlobToolBuilder
from multiclaw.tools.grep import GrepToolBuilder
from multiclaw.tools.list_dir import ListDirToolBuilder
from multiclaw.tools.read_file import ReadFileToolBuilder
from multiclaw.tools.shell import ShellToolBuilder
from multiclaw.tools.web_fetch import WebFetchToolBuilder
from multiclaw.tools.web_search import WebSearchToolBuilder
from multiclaw.tools.write_file import WriteFileToolBuilder
from multiclaw.mcp import (
    MCPClientManager,
    MCPToolBuilder,
    ToolInfo,
    load_mcp_config,
    load_mcp_tools_config,
)
from multiclaw.mcp.types import (
    HTTPServerConfig,
    InProcessServerConfig,
    SSEServerConfig,
    StdioServerConfig,
    WebSocketServerConfig,
)

from uuid import uuid4

from multiclaw.auth.store import AuthStore
from multiclaw.auth.middleware import AuthMiddleware, require_auth
from multiclaw.auth.router import router as auth_router
from multiclaw.stream import DataStreamEncoder


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

agent: MultiClawAgent
shared_bus: EventBus
_PUBLIC_SECRET_PATTERN = re.compile(
    r"(?:ghp_[A-Za-z0-9_]{1,255}|sk-[A-Za-z0-9_]{1,255}|Bearer\s+\S+"
    r"|token=[^\s&,;\"']{1,255}|key=[^\s&,;\"']{1,255})",
    re.IGNORECASE,
)


def _sanitize_mcp_namespace(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name)


def _mcp_namespace_prefix(server_name: str) -> str:
    return f"mcp__{_sanitize_mcp_namespace(server_name)}__"


def _sanitize_public_reason(
    reason: str,
    *,
    workspace_root: Path | None = None,
) -> str:
    text = _PUBLIC_SECRET_PATTERN.sub("[REDACTED]", reason.strip())
    if not text:
        return ""

    path_markers = {tempfile.gettempdir(), str(Path.home())}
    if workspace_root is not None:
        resolved = workspace_root.resolve()
        path_markers.update({str(workspace_root), str(resolved)})

    if any(marker and marker in text for marker in path_markers):
        return "details redacted"
    if "/." in text or "\\." in text:
        return "details redacted"
    if "/" in text or "\\" in text:
        return "details redacted"
    return text


def _sanitize_public_readiness(
    readiness: SandboxReadiness,
    *,
    workspace_root: Path,
) -> SandboxReadiness:
    return readiness.model_copy(
        update={
            "probe": readiness.probe.model_copy(
                update={
                    "reason": _sanitize_public_reason(
                        readiness.probe.reason,
                        workspace_root=workspace_root,
                    )
                }
            ),
            "skipped_capabilities": {
                _sanitize_mcp_namespace(name): _sanitize_public_reason(
                    reason,
                    workspace_root=workspace_root,
                )
                for name, reason in readiness.skipped_capabilities.items()
            },
        }
    )


def _record_blocked_capability_safely(
    controller: SandboxController | None,
    *,
    name: str,
    reason: str,
    workspace_root: Path,
) -> None:
    if controller is None:
        return
    try:
        controller.record_blocked_capability(
            name,
            _sanitize_public_reason(reason, workspace_root=workspace_root),
        )
    except Exception:
        logger.debug(
            "Sandbox controller rejected blocked capability record for %s",
            name,
            exc_info=True,
        )


def _build_mcp_adapters(
    server_name: str,
    tools: list[ToolInfo],
    manager: MCPClientManager,
    tool_filter: dict[str, list[str]] | None,
) -> list[MCPToolBuilder]:
    from multiclaw.mcp.config import _matches_tool_filter

    prefix = _mcp_namespace_prefix(server_name)
    adapters: list[MCPToolBuilder] = []
    for tool in tools:
        if tool_filter and not _matches_tool_filter(tool.original_name, tool_filter):
            continue
        adapter = MCPToolBuilder.from_tool_info(tool, manager)
        if not adapter.name.startswith(prefix):
            adapter.name = f"{prefix}{_sanitize_mcp_namespace(tool.original_name)}"
        adapters.append(adapter)
    return adapters


def _register_mcp_tools(
    *,
    registry: ToolRegistry,
    mcp_manager: MCPClientManager,
    config_path: str | None,
    sandbox_controller: SandboxController | None,
    workspace_root: Path,
) -> None:
    configs = load_mcp_config(config_path)
    if not configs:
        logger.info("No MCP servers configured (no .mcp.json found)")
        return

    tools_configs = load_mcp_tools_config(config_path)
    registry_lock = threading.RLock()
    refreshed_servers: set[str] = set()

    def _replace_server_namespace(server_name: str, tools: list[ToolInfo]) -> list[MCPToolBuilder]:
        adapters = _build_mcp_adapters(
            server_name,
            tools,
            mcp_manager,
            tools_configs.get(server_name),
        )
        registry.replace_namespace(_mcp_namespace_prefix(server_name), adapters)
        return adapters

    def _refresh_registry(server_name: str, tools: list[ToolInfo]) -> None:
        with registry_lock:
            _replace_server_namespace(server_name, tools)
            refreshed_servers.add(server_name)

    filtered_configs: dict[str, object] = {}
    for server_name, config in configs.items():
        if isinstance(config, StdioServerConfig):
            if sandbox_controller is None or sandbox_controller.is_profile_ready("mcp_stdio_local"):
                filtered_configs[server_name] = config
                continue

            reason = "sandbox profile 'mcp_stdio_local' is not ready"
            _record_blocked_capability_safely(
                sandbox_controller,
                name=f"mcp_stdio_{_sanitize_mcp_namespace(server_name)}",
                reason=reason,
                workspace_root=workspace_root,
            )
            logger.warning("Skipping stdio MCP server '%s': %s", server_name, reason)
            continue

        if isinstance(config, InProcessServerConfig):
            if sandbox_controller is not None and sandbox_controller.mode == "host_unsafe_dev_only":
                filtered_configs[server_name] = config
                logger.warning(
                    "Keeping in-process MCP server '%s' with unsafe host execution enabled",
                    server_name,
                )
                continue

            reason = "in-process MCP transport requires host_unsafe_dev_only"
            _record_blocked_capability_safely(
                sandbox_controller,
                name=f"mcp_in_process_{_sanitize_mcp_namespace(server_name)}",
                reason=reason,
                workspace_root=workspace_root,
            )
            logger.warning("Skipping in-process MCP server '%s': %s", server_name, reason)
            continue

        if isinstance(config, (HTTPServerConfig, SSEServerConfig, WebSocketServerConfig)):
            logger.info(
                "Keeping remote MCP server '%s' unsandboxed transport=%s transport_remote_unsandboxed=true",
                server_name,
                config.transport_type.value,
            )
        filtered_configs[server_name] = config

    mcp_manager.set_tools_changed_callback(_refresh_registry)

    try:
        mcp_manager.connect_servers(filtered_configs)
        states = mcp_manager.get_server_states()
        for server_name, state in states.items():
            if state.status.value == "connected":
                tool_filter = tools_configs.get(server_name)
                skipped = len(state.tools)
                with registry_lock:
                    if server_name in refreshed_servers:
                        adapters = _build_mcp_adapters(
                            server_name,
                            state.tools,
                            mcp_manager,
                            tool_filter,
                        )
                    else:
                        adapters = _replace_server_namespace(server_name, state.tools)
                registered = len(adapters)
                skipped -= registered
                logger.info(
                    "Registered %d tools from MCP server '%s'%s",
                    registered, server_name,
                    f" ({skipped} filtered)" if skipped else "",
                )
            else:
                logger.warning(
                    "MCP server '%s' failed to connect: %s",
                    server_name, state.error,
                )
    except Exception:
        logger.exception("Failed to connect MCP servers")


def create_agent(
    *,
    sandbox_controller: SandboxController | None = None,
) -> MultiClawAgent:
    global shared_bus
    shared_bus = EventBus()

    config_path = Path(__file__).resolve().parent.parent.parent / "multiclaw.toml"
    if not config_path.exists():
        config_path = Path(__file__).resolve().parent.parent / "multiclaw.toml"
    if not config_path.exists():
        config_path = Path("multiclaw.toml")

    settings = Settings(_config_file=str(config_path) if config_path.exists() else None)
    workspace_root = config_path.resolve().parent if config_path.exists() else Path.cwd().resolve()

    from multiclaw.skills import SkillManager

    skill_manager = SkillManager(
        project_root=workspace_root,
        max_active=settings.skill.max_active if hasattr(settings, 'skill') else 5,
    )
    if settings.skill.enabled if hasattr(settings, 'skill') else True:
        skill_manager.discover()

    if sandbox_controller is None:
        sandbox_controller = SandboxManager.create(
            settings=settings.governance.sandbox,
            debug=settings.app.debug,
            workspace_root=workspace_root,
            event_bus=shared_bus,
            runner=SandboxProcessRunner(),
        )
    sandbox_controller.initialize()

    registry = ToolRegistry()
    read_builder = ReadFileToolBuilder(workspace_root)
    edit_builder = EditFileToolBuilder(workspace_root)
    registry.register(read_builder)
    registry.register(WriteFileToolBuilder(workspace_root, read_builder))
    registry.register(edit_builder)
    registry.register(UndoEditToolBuilder(workspace_root, edit_builder))
    registry.register(GlobToolBuilder(workspace_root))
    registry.register(ListDirToolBuilder(workspace_root))
    registry.register(GrepToolBuilder(workspace_root))
    registry.register(FindDirToolBuilder(workspace_root))
    if sandbox_controller.is_profile_ready(settings.governance.sandbox.profiles.shell):
        registry.register(ShellToolBuilder(workspace_root))
    else:
        _record_blocked_capability_safely(
            sandbox_controller,
            name="shell",
            reason=f"sandbox profile {settings.governance.sandbox.profiles.shell!r} is not ready",
            workspace_root=workspace_root,
        )
    if sandbox_controller.is_profile_ready(settings.governance.sandbox.profiles.code_exec):
        registry.register(CodeExecToolBuilder(workspace_root))
    else:
        _record_blocked_capability_safely(
            sandbox_controller,
            name="code_exec",
            reason=f"sandbox profile {settings.governance.sandbox.profiles.code_exec!r} is not ready",
            workspace_root=workspace_root,
        )
    registry.register(
        WebFetchToolBuilder(
            workspace_root,
            allow_private_networks=settings.tools.web_fetch_allow_private_networks,
        )
    )
    registry.register(WebSearchToolBuilder(workspace_root))

    # Register MCP tools if enabled
    mcp_manager = None
    if settings.mcp.enabled:
        mcp_manager = MCPClientManager()
        _register_mcp_tools(
            registry=registry,
            mcp_manager=mcp_manager,
            config_path=(
                settings.mcp.config_path if settings.mcp.config_path else None
            ),
            sandbox_controller=sandbox_controller,
            workspace_root=workspace_root,
        )

    readiness = _sanitize_public_readiness(
        sandbox_controller.finalize_readiness(),
        workspace_root=workspace_root,
    )

    scheduler = CoreToolScheduler(
        permission_checker=PermissionChecker(
            guarded_tools={
                "write_file",
                "edit_file",
                "undo_edit",
                "shell",
                "code_exec",
            }
        ),
        execution_guard=ExecutionGuard(),
        audit_logger=InMemoryAuditLogger(),
        event_bus=shared_bus,
    )

    runtime_agent = MultiClawAgent(
        settings=settings,
        router=ModelRouter(settings),
        registry=registry,
        scheduler=scheduler,
        memory=SqliteMemory(settings.database.path),
        planner=Planner(),
        event_bus=shared_bus,
        skill_manager=skill_manager,
    )
    runtime_agent.session_store = SqliteSessionStore(settings.database.path)
    runtime_agent.mcp_manager = mcp_manager
    runtime_agent.sandbox_controller = sandbox_controller
    runtime_agent.sandbox_readiness = readiness
    return runtime_agent


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent
    agent = create_agent()
    auth_store = AuthStore(agent.settings.database.path)
    await auth_store.initialize()
    app.state.auth_store = auth_store
    app.state.settings = agent.settings
    app.state.sandbox_readiness = agent.sandbox_readiness
    sandbox_controller = getattr(agent, "sandbox_controller", None)
    if sandbox_controller is not None:
        for event in sandbox_controller.drain_startup_events():
            await shared_bus.publish(event)
    try:
        yield
    finally:
        if hasattr(agent, "mcp_manager") and agent.mcp_manager:
            agent.mcp_manager.stop()
        if sandbox_controller is not None:
            try:
                sandbox_controller.close()
            except Exception:
                logger.warning(
                    "Sandbox controller reported residual startup state during shutdown; details redacted"
                )


app = FastAPI(title="MultiClaw", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.include_router(auth_router)
app.include_router(auth_router, prefix="/api")

api = APIRouter(prefix="/api")


@app.middleware("http")
async def log_http_requests(request, call_next):
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (perf_counter() - started) * 1000
        logger.exception(
            "HTTP %s %s -> 500 (%.1fms)",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = (perf_counter() - started) * 1000
    logger.info(
        "HTTP %s %s -> %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


class ChatRequest(BaseModel):
    message: str | None = None
    session_id: str | None = None
    id: str | None = None
    messages: list[dict[str, Any]] | None = None


class SessionCreateRequest(BaseModel):
    title: str = "New Chat"


class SessionRenameRequest(BaseModel):
    title: str


class ApproveRequest(BaseModel):
    request_id: str
    approved: bool


@app.get("/health/ready")
async def health_ready():
    readiness = getattr(app.state, "sandbox_readiness", None)
    if readiness is None:
        payload = {
            "ready": False,
            "mode": "auto",
            "backend_name": "unknown",
            "probe": {
                "backend_name": "unknown",
                "available": False,
                "capabilities": {},
                "reason": "readiness unavailable",
            },
            "profiles": {},
            "skipped_capabilities": {"sandbox_readiness": "readiness unavailable"},
            "unsafe_fallback_active": False,
        }
        return JSONResponse(payload, status_code=503)

    payload = readiness.model_dump(mode="json")
    return JSONResponse(payload, status_code=200 if readiness.ready else 503)


@api.post("/approve")
async def approve(req: ApproveRequest, user: dict = Depends(require_auth)):
    ok = agent.scheduler.resolve_approval(req.request_id, req.approved)
    return {"ok": ok}


@api.get("/sessions")
async def list_sessions(include_archived: bool = False, user: dict = Depends(require_auth)):
    sessions = await agent.session_store.list_sessions(
        include_archived=include_archived, user_id=user["id"]
    )
    return [session.model_dump(mode="json") for session in sessions]


@api.post("/sessions")
async def create_session(req: SessionCreateRequest, user: dict = Depends(require_auth)):
    session = await agent.session_store.create(title=req.title, user_id=user["id"])
    return session.model_dump(mode="json")


@api.patch("/sessions/{session_id}")
async def rename_session(session_id: str, req: SessionRenameRequest, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    session = await agent.session_store.rename(session_id, req.title)
    return session.model_dump(mode="json")


@api.post("/sessions/{session_id}/archive")
async def archive_session(session_id: str, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    session = await agent.session_store.archive(session_id)
    return session.model_dump(mode="json")


@api.post("/sessions/{session_id}/restore")
async def restore_session(session_id: str, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    session = await agent.session_store.restore(session_id)
    return session.model_dump(mode="json")


@api.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    await agent.session_store.delete(session_id)
    return {"ok": True}


@api.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, limit: int = 50, user: dict = Depends(require_auth)):
    session = await agent.session_store.get(session_id)
    if session is None or session.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")
    return await agent.session_store.get_messages(session_id, limit)


@api.post("/chat")
async def chat(req: ChatRequest, user: dict = Depends(require_auth)):
    """SSE streaming — real token streaming from LLM with state events."""
    message = _resolve_chat_message(req)
    requested_session_id = req.session_id or req.id

    # Resolve or create session
    session = None
    if requested_session_id:
        session = await agent.session_store.get(requested_session_id)
        if session is None or session.user_id != user["id"]:
            session = None
        elif session.status == SessionStatus.ARCHIVED:
            raise HTTPException(status_code=409, detail="session is archived")
    if session is None:
        session = await agent.session_store.create(user_id=user["id"])

    # Update session activity (title from first message)
    session = await agent.session_store.touch_message(session.id, message)

    async def event_stream():
        logger.info("SSE stream started, message=%r, session=%r", message[:80], session.id)
        enc = DataStreamEncoder()
        text_part_id: str | None = None
        reasoning_part_id: str | None = None
        step_open = False
        pending_tool_results = 0

        def close_text_part() -> list[str]:
            nonlocal text_part_id
            if text_part_id is None:
                return []
            chunks = [enc.text_end(text_part_id)]
            text_part_id = None
            return chunks

        def close_reasoning_part() -> list[str]:
            nonlocal reasoning_part_id
            if reasoning_part_id is None:
                return []
            chunks = [enc.reasoning_end(reasoning_part_id)]
            reasoning_part_id = None
            return chunks

        def close_open_parts() -> list[str]:
            return [*close_reasoning_part(), *close_text_part()]

        def open_step() -> list[str]:
            nonlocal step_open
            if step_open:
                return []
            step_open = True
            return [enc.start_step()]

        def close_step() -> list[str]:
            nonlocal step_open
            if not step_open:
                return []
            step_open = False
            return [enc.finish_step()]

        yield enc.start()
        yield enc.data_part(
            "data-session",
            session.model_dump(mode="json"),
            transient=True,
        )
        for chunk in open_step():
            yield chunk

        token_queue: asyncio.Queue[dict] = asyncio.Queue()
        event_queue: asyncio.Queue[Event] = asyncio.Queue()

        async def collector(event: Event):
            await event_queue.put(event)

        sub_id = shared_bus.subscribe("*", collector)

        async def run_stream():
            try:
                async for item in agent.handle_message_stream(message, session_id=session.id):
                    await token_queue.put(item)
            except Exception as exc:
                logger.exception("stream error")
                msg = _friendly_error(exc)
                await token_queue.put({"type": "error", "content": msg})

        stream_task = asyncio.create_task(run_stream())

        try:
            while True:
                token_count = 0
                while True:
                    try:
                        item = token_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    token_count += 1
                    if item["type"] == "token":
                        for chunk in open_step():
                            yield chunk
                        for chunk in close_reasoning_part():
                            yield chunk
                        if text_part_id is None:
                            text_part_id = uuid4().hex
                            yield enc.text_start(text_part_id)
                        yield enc.text_delta(text_part_id, item["content"])
                    elif item["type"] == "done":
                        logger.info("stream done, tokens=%d, content_len=%d", token_count, len(item.get("content", "")))
                        for chunk in close_open_parts():
                            yield chunk
                        for chunk in close_step():
                            yield chunk
                        yield enc.finish("stop")
                        return
                    elif item["type"] == "error":
                        logger.error("stream error: %s", item["content"])
                        for chunk in close_open_parts():
                            yield chunk
                        for chunk in close_step():
                            yield chunk
                        yield enc.error(item["content"])
                        return
                    elif item["type"] == "tool_call":
                        for chunk in open_step():
                            yield chunk
                        for chunk in close_open_parts():
                            yield chunk
                        pending_tool_results += 1
                        tool_call_id = item.get("call_id") or uuid4().hex
                        yield enc.tool_input_available(
                            tool_call_id,
                            item["name"],
                            item.get("arguments", {}),
                        )
                    elif item["type"] == "tool_result":
                        for chunk in close_open_parts():
                            yield chunk
                        tool_call_id = item.get("call_id", "")
                        if item.get("is_error", False):
                            yield enc.tool_output_error(tool_call_id, item.get("content", ""))
                        else:
                            yield enc.tool_output_available(
                                tool_call_id,
                                {"content": item.get("content", "")},
                            )
                        if pending_tool_results > 0:
                            pending_tool_results -= 1
                        if pending_tool_results == 0:
                            for chunk in close_step():
                                yield chunk
                    elif item["type"] == "reasoning":
                        for chunk in open_step():
                            yield chunk
                        for chunk in close_text_part():
                            yield chunk
                        if reasoning_part_id is None:
                            reasoning_part_id = uuid4().hex
                            yield enc.reasoning_start(reasoning_part_id)
                        yield enc.reasoning_delta(reasoning_part_id, item["content"])
                    else:
                        yield enc.data_part("data-state", {"item": item}, transient=True)

                while not event_queue.empty():
                    evt = event_queue.get_nowait()
                    if evt.type == "tool.awaiting_approval":
                        logger.info(
                            "yield approval_required: request_id=%s tool=%s",
                            evt.data.get("request_id"), evt.data.get("tool"),
                        )
                        for chunk in open_step():
                            yield chunk
                        for chunk in close_open_parts():
                            yield chunk
                        tool_call_id = evt.data.get("call_id") or uuid4().hex
                        yield enc.tool_input_available(
                            tool_call_id,
                            evt.data.get("tool", ""),
                            evt.data.get("params", {}),
                        )
                        yield enc.tool_approval_request(
                            evt.data.get("request_id", ""),
                            tool_call_id,
                        )
                    else:
                        yield enc.data_part("data-state", {"state": evt.type}, transient=True)

                if stream_task.done():
                    exc = stream_task.exception()
                    if exc:
                        logger.exception("stream task crashed")
                        for chunk in close_open_parts():
                            yield chunk
                        for chunk in close_step():
                            yield chunk
                        yield enc.error(str(exc))
                    else:
                        for chunk in close_open_parts():
                            yield chunk
                        for chunk in close_step():
                            yield chunk
                        yield enc.finish("stop")
                    return

                await asyncio.sleep(0.02)
        finally:
            stream_task.cancel()
            shared_bus.unsubscribe(sub_id)
            logger.info("SSE stream ended")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Vercel-AI-Data-Stream": "v1"},
    )


def _resolve_chat_message(req: ChatRequest) -> str:
    if req.message:
        return req.message

    message = _extract_latest_user_message(req.messages or [])
    if message:
        return message

    raise HTTPException(status_code=422, detail="No user message found in request")


def _extract_latest_user_message(messages: list[dict[str, Any]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _extract_message_text(message)
        if text:
            return text
    return None


def _extract_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, Iterable) and not isinstance(content, (str, bytes, dict)):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


# ---------------------------------------------------------------------------
# HTML UI (inline)
# ---------------------------------------------------------------------------

_HTML_PATH = Path(__file__).parent / "static" / "index.html"
_CHAT_HTML = _HTML_PATH.read_text()
_PNG_PATH = Path(__file__).resolve().parent.parent.parent / "multiclaw.png"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=_CHAT_HTML)


# Mount static assets built by Vite (JS, CSS bundles)
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=str(_STATIC_DIR / "assets")), name="assets")


@app.get("/multiclaw.png")
async def favicon():
    return FileResponse(_PNG_PATH, media_type="image/png")


app.include_router(api)
