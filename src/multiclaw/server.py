import asyncio
import hashlib
import inspect
import json
import logging
import re
import threading
import tempfile
from contextlib import asynccontextmanager
from collections.abc import Iterable
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import perf_counter, time
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
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


def _note_startup_cleanup_error(primary: BaseException, phase: str, error: BaseException) -> None:
    primary.add_note(f"{phase} cleanup failed: {type(error).__name__}: {error}")


def _note_cleanup_error(primary: BaseException, phase: str, error: BaseException) -> None:
    primary.add_note(f"{phase} failed: {type(error).__name__}: {error}")


from multiclaw.config import Settings
from multiclaw.events import EventBus, EventScope, ScopedEvent
from multiclaw.governance import (
    SandboxController,
    SandboxReadiness,
)
from multiclaw.session import SessionStatus
from multiclaw.runtime import RuntimeFactory, RuntimePool
from multiclaw.runtime.pool import RuntimeCapacityError, RuntimeUnavailableError
from multiclaw.storage import Database
from multiclaw.storage.uow import TenantUnitOfWork
from multiclaw.tenancy import TenantContext, WorkspaceResolver
from multiclaw.tools import (
    ToolRegistry,
)
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
from multiclaw.auth.middleware import AuthMiddleware
from multiclaw.auth.router import router as auth_router
from multiclaw.api.chat import (
    build_workflow_continuation_service,
    build_workflow_coordinator,
    build_workflow_recovery_service,
    encode_run_metadata,
    encode_scoped_event,
    encode_session_metadata,
    iterate_message_stream,
)
from multiclaw.api.approvals import ApprovalDecisionRequest, ApprovalResponse
from multiclaw.api.dependencies import current_user, tenant_context, tenant_uow
from multiclaw.stream import DataStreamEncoder
from multiclaw.workflow.models import (
    InvalidTransitionError,
    LeaseConflictError,
    RecoveryAction,
    RecoveryOutcome,
    RunLeaseHandle,
    RunStatus,
    StaleFenceError,
    TenantRunQuotaError,
    VersionConflictError,
)
from multiclaw.workflow.recovery import WorkflowRecoveryWorker


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------
_PUBLIC_SECRET_PATTERN = re.compile(
    r"(?:ghp_[A-Za-z0-9_]{1,255}|sk-[A-Za-z0-9_]{1,255}|Bearer\s+\S+"
    r"|token=[^\s&,;\"']{1,255}|key=[^\s&,;\"']{1,255})",
    re.IGNORECASE,
)
_PUBLIC_ASSIGNMENT_PATTERN = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_.-]*)\b\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_PUBLIC_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)\bAuthorization\s*:\s*Bearer\s+[^\s,;]+"
)
_PUBLIC_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")


def _sanitize_mcp_namespace(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name)


def _mcp_namespace_prefix(server_name: str) -> str:
    return f"mcp__{_sanitize_mcp_namespace(server_name)}__"


def _sanitize_public_reason(
    reason: str,
    *,
    workspace_root: Path | None = None,
) -> str:
    text = reason.strip()
    text = _PUBLIC_AUTHORIZATION_PATTERN.sub("Authorization: [REDACTED]", text)
    text = _PUBLIC_BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _PUBLIC_ASSIGNMENT_PATTERN.sub(r"\1=[REDACTED]", text)
    text = _PUBLIC_SECRET_PATTERN.sub("[REDACTED]", text)
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
    workspace_root: Path | None,
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
    controller: SandboxController,
    *,
    name: str,
    reason: str,
    workspace_root: Path,
) -> None:
    controller.record_blocked_capability(
        name,
        _sanitize_public_reason(reason, workspace_root=workspace_root),
    )


def _mcp_capability_id(prefix: str, server_name: str) -> str:
    readable = _sanitize_mcp_namespace(server_name).strip("_") or "server"
    digest = hashlib.sha256(server_name.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{readable}_{digest}"


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
    mcp_profile_name: str,
) -> None:
    if sandbox_controller is None:
        raise RuntimeError("sandbox controller is required for MCP transport gating")

    configs = _load_mcp_config_for_workspace(
        config_path,
        workspace_root=workspace_root,
    )
    if not configs:
        logger.info("No MCP servers configured (no .mcp.json found)")
        return

    tools_configs = _load_mcp_tools_config_for_workspace(
        config_path,
        workspace_root=workspace_root,
    )
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
        if _is_workspace_untrusted_config(config):
            capability_prefix = _mcp_transport_capability_prefix(config)
            reason = (
                "workspace_untrusted MCP configs never auto-connect; "
                "move this server to an operator-managed config outside the workspace"
            )
            _record_blocked_capability_safely(
                sandbox_controller,
                name=_mcp_capability_id(capability_prefix, server_name),
                reason=reason,
                workspace_root=workspace_root,
            )
            logger.warning(
                "Skipping workspace-untrusted MCP server '%s': %s",
                server_name,
                reason,
            )
            continue

        if isinstance(config, StdioServerConfig):
            if sandbox_controller.is_profile_ready(mcp_profile_name):
                filtered_configs[server_name] = config
                continue

            reason = f"sandbox profile {mcp_profile_name!r} is not ready"
            _record_blocked_capability_safely(
                sandbox_controller,
                name=_mcp_capability_id("mcp_stdio", server_name),
                reason=reason,
                workspace_root=workspace_root,
            )
            logger.warning("Skipping stdio MCP server '%s': %s", server_name, reason)
            continue

        if isinstance(config, InProcessServerConfig):
            if sandbox_controller.mode == "host_unsafe_dev_only":
                sandbox_controller.record_unsafe_capability(
                    _mcp_capability_id("mcp_in_process", server_name),
                    "unsafe transport kept for development",
                )
                filtered_configs[server_name] = config
                logger.warning(
                    "Keeping in-process MCP server '%s' with unsafe host execution enabled",
                    server_name,
                )
                continue

            reason = "in-process MCP transport requires host_unsafe_dev_only"
            _record_blocked_capability_safely(
                sandbox_controller,
                name=_mcp_capability_id("mcp_in_process", server_name),
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


def _load_mcp_config_for_workspace(
    config_path: str | None,
    *,
    workspace_root: Path,
) -> dict[str, object]:
    if "workspace_root" in inspect.signature(load_mcp_config).parameters:
        return load_mcp_config(config_path, workspace_root=workspace_root)
    return load_mcp_config(config_path)


def _load_mcp_tools_config_for_workspace(
    config_path: str | None,
    *,
    workspace_root: Path,
) -> dict[str, dict[str, list[str]]]:
    if "workspace_root" in inspect.signature(load_mcp_tools_config).parameters:
        return load_mcp_tools_config(config_path, workspace_root=workspace_root)
    return load_mcp_tools_config(config_path)


def _is_workspace_untrusted_config(config: object) -> bool:
    return getattr(config, "config_trust", "trusted_operator") == "workspace_untrusted"


def _mcp_transport_capability_prefix(config: object) -> str:
    if isinstance(config, StdioServerConfig):
        return "mcp_stdio"
    if isinstance(config, InProcessServerConfig):
        return "mcp_in_process"
    if isinstance(config, HTTPServerConfig):
        return "mcp_http"
    if isinstance(config, SSEServerConfig):
        return "mcp_sse"
    if isinstance(config, WebSocketServerConfig):
        return "mcp_websocket"
    return "mcp_unknown"
def _resolve_config_path() -> Path | None:
    candidates = (
        Path(__file__).resolve().parent.parent.parent / "multiclaw.toml",
        Path(__file__).resolve().parent.parent / "multiclaw.toml",
        Path("multiclaw.toml"),
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _resolve_workspace_root(settings: Settings, config_path: Path | None) -> Path:
    base_root = config_path.resolve().parent if config_path is not None else Path.cwd().resolve()
    configured = Path(settings.workspace.root)
    workspace_root = configured if configured.is_absolute() else (base_root / configured)
    workspace_root.mkdir(parents=True, exist_ok=True)
    return workspace_root.resolve()


def create_runtime_factory(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    workspace_resolver: WorkspaceResolver | None = None,
    sandbox_controller_factory=None,
    mcp_manager_factory: type[MCPClientManager] = MCPClientManager,
) -> RuntimeFactory:
    config_path = _resolve_config_path()
    resolved_settings = settings or Settings(
        _config_file=str(config_path) if config_path is not None else None
    )
    resolved_database = database or Database.create(resolved_settings.database)
    resolved_workspace_resolver = workspace_resolver or WorkspaceResolver(
        _resolve_workspace_root(resolved_settings, config_path)
    )
    return RuntimeFactory(
        settings=resolved_settings,
        database=resolved_database,
        workspace_resolver=resolved_workspace_resolver,
        sandbox_controller_factory=sandbox_controller_factory,
        mcp_manager_factory=mcp_manager_factory,
        mcp_tool_registrar=_register_mcp_tools,
        config_path=(
            resolved_settings.mcp.config_path if resolved_settings.mcp.config_path else None
        ),
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime_factory = None
    runtime_pool = None
    auth_store = None
    recovery_stop: asyncio.Event | None = None
    recovery_task: asyncio.Task | None = None
    try:
        runtime_factory = create_runtime_factory()
        runtime_pool = RuntimePool(
            factory=runtime_factory,
            max_resident_tenants=runtime_factory.settings.runtime.max_resident_tenants,
            idle_ttl_ms=runtime_factory.settings.runtime.idle_ttl_seconds * 1000,
        )
        auth_store = AuthStore(runtime_factory.settings.database.path)
        readiness, startup_events = runtime_factory.probe_startup()
        await auth_store.initialize()
        app.state.auth_store = auth_store
        app.state.database = runtime_factory.database
        app.state.runtime_pool = runtime_pool
        app.state.workspace_resolver = runtime_factory.workspace_resolver
        app.state.settings = runtime_factory.settings
        app.state.sandbox_readiness = readiness
        app.state.workspace_root = runtime_factory.workspace_resolver.root
        app.state.sandbox_startup_events = startup_events
        if hasattr(runtime_factory.database, "dialect") and hasattr(runtime_factory.database, "connect"):
            recovery_stop = asyncio.Event()

            async def recovery_loop() -> None:
                worker = WorkflowRecoveryWorker(
                    database=runtime_factory.database,
                    settings=runtime_factory.settings,
                    runtime_pool=runtime_pool,
                )
                workflow_settings = getattr(runtime_factory.settings, "workflow", None)
                heartbeat_ms = getattr(workflow_settings, "heartbeat_ms", 1_000)
                interval_seconds = max(
                    1.0,
                    heartbeat_ms / 1000,
                )
                while not recovery_stop.is_set():
                    await worker.run_once()
                    try:
                        await asyncio.wait_for(recovery_stop.wait(), timeout=interval_seconds)
                    except asyncio.TimeoutError:
                        continue

            recovery_task = asyncio.create_task(recovery_loop())
    except BaseException as primary:
        try:
            if auth_store is not None:
                try:
                    await auth_store.close()
                except BaseException as error:
                    _note_startup_cleanup_error(primary, "auth_store.close", error)
            if runtime_pool is not None:
                try:
                    await runtime_pool.close()
                except BaseException as error:
                    _note_startup_cleanup_error(primary, "runtime_pool.close", error)
            if runtime_factory is not None:
                try:
                    await runtime_factory.database.dispose()
                except BaseException as error:
                    _note_startup_cleanup_error(primary, "database.dispose", error)
        finally:
            raise primary
    try:
        yield
    finally:
        primary: BaseException | None = None

        if recovery_stop is not None:
            recovery_stop.set()
        if recovery_task is not None:
            try:
                await recovery_task
            except BaseException as error:
                primary = error if primary is None else primary

        if auth_store is not None:
            try:
                await auth_store.close()
            except BaseException as error:
                primary = error

        if runtime_pool is not None:
            try:
                await runtime_pool.close()
            except BaseException as error:
                if primary is None:
                    primary = error
                else:
                    _note_cleanup_error(primary, "runtime_pool.close", error)

        if runtime_factory is not None:
            try:
                await runtime_factory.database.dispose()
            except BaseException as error:
                if primary is None:
                    primary = error
                else:
                    _note_cleanup_error(primary, "database.dispose", error)

        if primary is not None:
            raise primary


app = FastAPI(title="MultiClaw", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.include_router(auth_router)
app.include_router(auth_router, prefix="/api")

api = APIRouter(prefix="/api")


def _runtime_error_response(retry_after_seconds: int) -> JSONResponse:
    return JSONResponse(
        {"detail": "runtime temporarily unavailable"},
        status_code=503,
        headers={"Retry-After": str(retry_after_seconds)},
    )


@app.exception_handler(RuntimeCapacityError)
async def handle_runtime_capacity_error(
    request: Request,
    exc: RuntimeCapacityError,
) -> JSONResponse:
    del request
    return _runtime_error_response(exc.retry_after_seconds)


@app.exception_handler(RuntimeUnavailableError)
async def handle_runtime_unavailable_error(
    request: Request,
    exc: RuntimeUnavailableError,
) -> JSONResponse:
    del request
    return _runtime_error_response(exc.retry_after_seconds)


@app.middleware("http")
async def log_http_requests(request, call_next):
    started = perf_counter()
    request.state.request_started_at_ms = int(time() * 1000)
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


@app.get("/health/ready")
async def health_ready(request: Request):
    readiness = getattr(request.app.state, "sandbox_readiness", None)
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

    workspace_root = getattr(request.app.state, "workspace_root", None)
    public_readiness = _sanitize_public_readiness(
        readiness,
        workspace_root=workspace_root.resolve() if isinstance(workspace_root, Path) else workspace_root,
    )
    payload = public_readiness.model_dump(mode="json")
    return JSONResponse(payload, status_code=200 if readiness.ready else 503)


@api.post("/approve")
async def approve(
    req: ApprovalDecisionRequest,
    request: Request,
    context: TenantContext = Depends(tenant_context),
):
    coordinator = build_workflow_coordinator(
        request.app.state.database,
        request.app.state.settings,
    )
    try:
        record = await coordinator.decide_approval(
            context=context,
            approval_id=req.approval_id,
            approved=req.approved,
            version=req.version,
        )
    except InvalidTransitionError as error:
        if str(error) == "approval expired":
            raise HTTPException(status_code=410, detail="approval expired") from error
        raise HTTPException(status_code=409, detail="approval already resolved") from error
    except VersionConflictError as error:
        message = str(error)
        if message == "approval record not found":
            raise HTTPException(status_code=404, detail="approval not found") from error
        raise HTTPException(status_code=409, detail=message) from error
    return ApprovalResponse.from_record(record)


@api.get("/sessions")
async def list_sessions(
    include_archived: bool = False,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    sessions = await uow.sessions.list(include_archived=include_archived)
    return [session.model_dump(mode="json") for session in sessions]


@api.post("/sessions")
async def create_session(
    req: SessionCreateRequest,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.create(title=req.title)
    return session.model_dump(mode="json")


@api.patch("/sessions/{session_id}")
async def rename_session(
    session_id: str,
    req: SessionRenameRequest,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    session = await uow.sessions.rename(session_id, req.title)
    return session.model_dump(mode="json")


@api.post("/sessions/{session_id}/archive")
async def archive_session(
    session_id: str,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    session = await uow.sessions.archive(session_id)
    return session.model_dump(mode="json")


@api.post("/sessions/{session_id}/restore")
async def restore_session(
    session_id: str,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    session = await uow.sessions.restore(session_id)
    return session.model_dump(mode="json")


@api.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    await uow.sessions.delete(session_id)
    return {"ok": True}


@api.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = 50,
    uow: TenantUnitOfWork = Depends(tenant_uow),
):
    session = await uow.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return await uow.sessions.get_messages(session_id, limit)


@api.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    context: TenantContext = Depends(tenant_context),
    uow: TenantUnitOfWork = Depends(tenant_uow, scope="function"),
):
    """SSE streaming — real token streaming from LLM with state events."""
    message = _resolve_chat_message(req)
    has_session_id = req.session_id is not None
    has_id_alias = req.id is not None
    requested_session_id = req.session_id if has_session_id else req.id

    # Resolve or create session
    session = None
    if has_session_id or has_id_alias:
        if not requested_session_id:
            raise HTTPException(status_code=404, detail="session not found")
        session = await uow.sessions.get(requested_session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if session.status == SessionStatus.ARCHIVED:
            raise HTTPException(status_code=409, detail="session is archived")
    else:
        session = await uow.sessions.create()

    # Update session activity (title from first message)
    session = await uow.sessions.touch_message(session.id, message)
    assert session is not None
    run_id = str(uuid4())
    run_context = context.for_run(session.id, run_id)
    runtime = await request.app.state.runtime_pool.acquire(run_context)
    workflow = build_workflow_coordinator(
        request.app.state.database,
        request.app.state.settings,
        connection=uow.conn,
    )
    workflow_continuation = build_workflow_continuation_service(
        request.app.state.database,
        request.app.state.settings,
    )
    workflow_recovery = build_workflow_recovery_service(
        request.app.state.database,
        request.app.state.settings,
    )

    async def _cleanup_prestream_failure(
        primary: BaseException,
        *,
        workflow_lease,
        recovery_outcome: RecoveryOutcome | None,
        request: Request,
    ) -> None:
        target = RunStatus.FAILED_TERMINAL
        if recovery_outcome is not None and recovery_outcome.status in {
            RunStatus.BLOCKED_CORRUPT,
            RunStatus.BLOCKED_INCOMPATIBLE,
        }:
            target = recovery_outcome.status

        coordinator = build_workflow_coordinator(
            request.app.state.database,
            request.app.state.settings,
        )
        try:
            await coordinator.finish_run_with_checkpoint(workflow_lease, target)
            return
        except Exception as terminal_error:
            logger.exception("failed to persist pre-stream terminal checkpoint cleanup")
            primary.add_note(
                f"pre-stream terminal checkpoint cleanup failed: {type(terminal_error).__name__}: {terminal_error}"
            )
        try:
            await coordinator.finish_run(workflow_lease, target)
        except Exception as terminal_state_error:
            logger.exception("failed to persist pre-stream terminal state cleanup")
            primary.add_note(
                f"pre-stream terminal state cleanup failed: {type(terminal_state_error).__name__}: {terminal_state_error}"
            )

    workflow_lease = None
    try:
        workflow_lease = await workflow.start_run_with_checkpoint(
            run_context,
            runtime.runtime_instance_id,
        )
        await uow.commit()
        live_recovery = await workflow_recovery.validate_live_run(run_context)
        if live_recovery.action is not RecoveryAction.RESUME_MODEL:
            raise RuntimeError(
                f"live workflow checkpoint validation failed: {live_recovery.reason or live_recovery.status}"
            )
    except TenantRunQuotaError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except Exception as error:
        if workflow_lease is not None:
            await _cleanup_prestream_failure(
                error,
                workflow_lease=workflow_lease,
                recovery_outcome=locals().get("live_recovery"),
                request=request,
            )
        raise
    try:
        run_lease = runtime.begin_run()
    except RuntimeError as error:
        if workflow_lease is not None:
            await build_workflow_coordinator(
                request.app.state.database,
                request.app.state.settings,
            ).finish_run_with_checkpoint(workflow_lease, RunStatus.CANCELLED)
        if str(error) == "runtime is unavailable":
            raise RuntimeUnavailableError(
                request.app.state.runtime_pool.idle_ttl_ms // 1000 or 1
            ) from error
        raise

    async def event_stream():
        logger.info("SSE stream started, message=%r, session=%r", message[:80], session.id)
        enc = DataStreamEncoder()
        text_part_id: str | None = None
        reasoning_part_id: str | None = None
        step_open = False
        pending_tool_results = 0
        subscription = None
        stream_task: asyncio.Task | None = None
        heartbeat_task: asyncio.Task | None = None
        assert workflow_lease is not None
        workflow_lease_handle = RunLeaseHandle(workflow_lease)
        terminal_persisted = False
        fence_lost = False

        async def persist_terminal(status: RunStatus) -> None:
            nonlocal terminal_persisted
            if terminal_persisted:
                return
            await workflow_lease_handle.refresh(
                lambda lease: build_workflow_coordinator(
                    request.app.state.database,
                    request.app.state.settings,
                ).finish_run_with_checkpoint(lease, status)
            )
            terminal_persisted = True

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

        def drain_event_queue() -> list[str]:
            chunks: list[str] = []
            while not event_queue.empty():
                evt = event_queue.get_nowait()
                chunks.append(encode_scoped_event(evt))
                if evt.event_type == "tool.awaiting_approval":
                    logger.info(
                        "yield approval_required: request_id=%s tool=%s",
                        evt.data.get("request_id"),
                        evt.data.get("tool"),
                    )
                    chunks.extend(open_step())
                    chunks.extend(close_open_parts())
                    tool_call_id = (
                        evt.data.get("call_id")
                        or evt.data.get("request_id")
                        or ""
                    )
                    chunks.append(
                        enc.tool_input_available(
                            tool_call_id,
                            evt.data.get("tool", ""),
                            evt.data.get("params", {}),
                        )
                    )
                    chunks.append(
                        enc.tool_approval_request(
                            evt.data.get("request_id", ""),
                            tool_call_id,
                        )
                    )
            return chunks

        try:
            yield enc.start()
            yield encode_session_metadata(session.model_dump(mode="json"))
            yield encode_run_metadata(session.id, run_id)
            for chunk in open_step():
                yield chunk

            token_queue: asyncio.Queue[dict] = asyncio.Queue()
            event_queue: asyncio.Queue[ScopedEvent] = asyncio.Queue()
            heartbeat_stop = asyncio.Event()

            async def collector(event: ScopedEvent):
                await event_queue.put(event)

            subscription = runtime.event_router.subscribe(
                EventScope.from_context(run_context),
                collector,
            )

            async def run_stream():
                try:
                    async for item in iterate_message_stream(
                        runtime.agent.handle_message_stream,
                        message,
                        context=run_context,
                        run_lease=await workflow_lease_handle.current(),
                        run_lease_handle=workflow_lease_handle,
                        workflow_recovery=workflow_recovery,
                        workflow_continuation=workflow_continuation,
                    ):
                        await token_queue.put(item)
                except Exception as exc:
                    logger.exception("stream error")
                    msg = _friendly_error(exc)
                    await token_queue.put({"type": "error", "content": msg})

            async def heartbeat_run_lease() -> None:
                nonlocal fence_lost
                interval_seconds = max(
                    0.001,
                    request.app.state.settings.workflow.heartbeat_ms / 1000,
                )
                while True:
                    try:
                        await asyncio.wait_for(heartbeat_stop.wait(), timeout=interval_seconds)
                        return
                    except asyncio.TimeoutError:
                        pass
                    if terminal_persisted:
                        continue
                    try:
                        await workflow_lease_handle.refresh(
                            lambda lease: build_workflow_coordinator(
                                request.app.state.database,
                                request.app.state.settings,
                            ).heartbeat(lease)
                        )
                    except StaleFenceError as exc:
                        fence_lost = True
                        logger.exception("run lease heartbeat lost current fence")
                        if stream_task is not None:
                            stream_task.cancel()
                        await token_queue.put({"type": "error", "content": _friendly_error(exc)})
                        return
                    except Exception as exc:
                        logger.exception("run lease heartbeat failed")
                        await token_queue.put({"type": "error", "content": _friendly_error(exc)})
                        return

            stream_task = asyncio.create_task(run_stream())
            heartbeat_task = asyncio.create_task(heartbeat_run_lease())

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
                        if fence_lost:
                            continue
                        logger.info("stream done, tokens=%d, content_len=%d", token_count, len(item.get("content", "")))
                        await persist_terminal(RunStatus.COMPLETED)
                        for chunk in drain_event_queue():
                            yield chunk
                        for chunk in close_open_parts():
                            yield chunk
                        for chunk in close_step():
                            yield chunk
                        yield enc.finish("stop")
                        return
                    elif item["type"] == "error":
                        logger.error("stream error: %s", item["content"])
                        if not fence_lost:
                            await persist_terminal(RunStatus.FAILED_TERMINAL)
                        for chunk in drain_event_queue():
                            yield chunk
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
                        run_lease.mark_tool_execution_started()
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
                            run_lease.mark_tool_execution_finished()
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

                for chunk in drain_event_queue():
                    yield chunk
                if stream_task.done():
                    exc = stream_task.exception()
                    if exc:
                        logger.exception("stream task crashed")
                        if not fence_lost:
                            await persist_terminal(RunStatus.FAILED_TERMINAL)
                        for chunk in drain_event_queue():
                            yield chunk
                        for chunk in close_open_parts():
                            yield chunk
                        for chunk in close_step():
                            yield chunk
                        yield enc.error(str(exc))
                    else:
                        for chunk in drain_event_queue():
                            yield chunk
                        for chunk in close_open_parts():
                            yield chunk
                        for chunk in close_step():
                            yield chunk
                        yield enc.finish("stop")
                    return

                await asyncio.sleep(0.02)
        finally:
            if heartbeat_task is not None:
                heartbeat_stop.set()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            if stream_task is not None:
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
            if subscription is not None:
                subscription.close()
            if not terminal_persisted and not fence_lost:
                try:
                    await persist_terminal(RunStatus.CANCELLED)
                except Exception:
                    logger.exception("failed to persist terminal run status")
            run_lease.close()
            logger.info("SSE stream ended")

    try:
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"X-Vercel-AI-Data-Stream": "v1"},
        )
    except BaseException:
        if workflow_lease is not None:
            try:
                await build_workflow_coordinator(
                    request.app.state.database,
                    request.app.state.settings,
                ).finish_run_with_checkpoint(workflow_lease, RunStatus.CANCELLED)
            except Exception:
                logger.exception("failed to cancel run after streaming setup error")
        run_lease.close()
        raise


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
