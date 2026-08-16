import asyncio
import hashlib
import inspect
import logging
import re
import threading
import tempfile
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import perf_counter, time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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
DELETION_POLL_INTERVAL_SECONDS = 1.0
DELETION_BATCH_SIZE = 8


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
from multiclaw.events import EventBus
from multiclaw.governance import (
    SandboxController,
)
from multiclaw.runtime import RuntimeFactory, RuntimePool
from multiclaw.runtime.pool import RuntimeCapacityError, RuntimeUnavailableError
from multiclaw.storage import Database
from multiclaw.tenancy import WorkspaceResolver
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

from multiclaw.auth.cleanup import AuthCleanupWorker
from multiclaw.auth.middleware import AuthMiddleware
from multiclaw.auth.models import build_auth_runtime
from multiclaw.auth.router import router as auth_router
from multiclaw.api.account import router as account_router
from multiclaw.api.approvals import router as approvals_router
from multiclaw.api.chat import router as chat_router
from multiclaw.api.health import router as health_router
from multiclaw.api.secrets import router as secrets_router
from multiclaw.api.sessions import router as sessions_router
from multiclaw.observability import (
    OperationalMetrics,
    TraceEventSink,
    increment_metric,
    observability_scope,
    observe_database_error,
    record_trace_event,
)
from multiclaw.workflow.recovery import WorkflowRecoveryWorker
from multiclaw.deletion.service import DeletionService
from multiclaw.deletion.worker import DeletionWorker
from multiclaw.secrets.keyring import DeploymentKeyring, SecretKeyringError
from multiclaw.secrets.resolver import SecretResolver
from multiclaw.secrets.validation import SecretCredentialTester


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
    secret_resolver = None
    secret_keyring = None
    try:
        secret_keyring = DeploymentKeyring.load(resolved_settings.secrets)
        secret_resolver = SecretResolver(
            database=resolved_database,
            settings=resolved_settings.secrets,
            keyring=secret_keyring,
        )
    except SecretKeyringError:
        secret_resolver = None
    return RuntimeFactory(
        settings=resolved_settings,
        database=resolved_database,
        workspace_resolver=resolved_workspace_resolver,
        secret_resolver=secret_resolver,
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


def _validate_allowed_origins(origins: set[str] | frozenset[str]) -> frozenset[str]:
    if "*" in origins:
        raise ValueError("wildcard origins cannot be used with credentialed auth cookies")
    return frozenset(origins)


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime_factory = None
    runtime_pool = None
    auth_runtime = None
    deletion_service = None
    deletion_worker = None
    recovery_stop: asyncio.Event | None = None
    recovery_task: asyncio.Task | None = None
    deletion_stop: asyncio.Event | None = None
    deletion_task: asyncio.Task | None = None
    auth_cleanup_stop: asyncio.Event | None = None
    auth_cleanup_task: asyncio.Task | None = None
    try:
        runtime_factory = create_runtime_factory()
        runtime_pool = RuntimePool(
            factory=runtime_factory,
            max_resident_tenants=runtime_factory.settings.runtime.max_resident_tenants,
            idle_ttl_ms=runtime_factory.settings.runtime.idle_ttl_seconds * 1000,
        )
        auth_runtime = build_auth_runtime(runtime_factory.settings)
        allowed_origins = _validate_allowed_origins(auth_runtime.allowed_origins)
        readiness, startup_events = runtime_factory.probe_startup()
        app.state.auth = auth_runtime
        app.state.allowed_origins = allowed_origins
        app.state.database = runtime_factory.database
        app.state.runtime_pool = runtime_pool
        app.state.workspace_resolver = runtime_factory.workspace_resolver
        app.state.settings = runtime_factory.settings
        app.state.operational_metrics = OperationalMetrics()
        app.state.trace_sink = TraceEventSink()
        app.state.secret_resolver = getattr(runtime_factory, "secret_resolver", None)
        app.state.secret_keyring = getattr(app.state.secret_resolver, "_keyring", None)
        app.state.secret_credential_tester = (
            SecretCredentialTester(
                resolver=app.state.secret_resolver,
                settings=runtime_factory.settings,
            )
            if app.state.secret_resolver is not None
            else None
        )
        app.state.sandbox_readiness = readiness
        app.state.workspace_root = runtime_factory.workspace_resolver.root
        app.state.sandbox_startup_events = startup_events
        app.state.auth_forced_code = None
        deletion_service = DeletionService(
            database=runtime_factory.database,
            runtime_pool=runtime_pool,
            settings=runtime_factory.settings,
        )
        deletion_worker = DeletionWorker(
            database=runtime_factory.database,
            runtime_pool=runtime_pool,
            workspace_resolver=runtime_factory.workspace_resolver,
            settings=runtime_factory.settings,
        )
        app.state.deletion_service = deletion_service
        app.state.deletion_worker = deletion_worker
        if hasattr(runtime_factory.database, "dialect") and hasattr(runtime_factory.database, "connect"):
            async with runtime_factory.database.connect():
                pass
            recovery_stop = asyncio.Event()
            deletion_stop = asyncio.Event()
            auth_cleanup_stop = asyncio.Event()

            async def recovery_loop() -> None:
                async with observability_scope(
                    metrics=app.state.operational_metrics,
                    trace_sink=app.state.trace_sink,
                ):
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
            if getattr(readiness, "ready", True):
                deletion_task = asyncio.create_task(
                    deletion_worker.run_until_stopped(
                        stop_event=deletion_stop,
                        batch_size=DELETION_BATCH_SIZE,
                        interval_seconds=DELETION_POLL_INTERVAL_SECONDS,
                    )
                )

            async def auth_cleanup_loop() -> None:
                async with observability_scope(
                    metrics=app.state.operational_metrics,
                    trace_sink=app.state.trace_sink,
                ):
                    worker = AuthCleanupWorker(runtime_factory.database)
                    while not auth_cleanup_stop.is_set():
                        await worker.run_once()
                        try:
                            await asyncio.wait_for(auth_cleanup_stop.wait(), timeout=60.0)
                        except asyncio.TimeoutError:
                            continue

            auth_cleanup_task = asyncio.create_task(auth_cleanup_loop())
    except BaseException as primary:
        try:
            if recovery_stop is not None:
                recovery_stop.set()
            if deletion_stop is not None:
                deletion_stop.set()
            if auth_cleanup_stop is not None:
                auth_cleanup_stop.set()
            if recovery_task is not None:
                try:
                    await recovery_task
                except BaseException as error:
                    _note_startup_cleanup_error(primary, "workflow_recovery.await", error)
            if deletion_task is not None:
                try:
                    await deletion_task
                except BaseException as error:
                    _note_startup_cleanup_error(primary, "deletion_worker.await", error)
            if auth_cleanup_task is not None:
                try:
                    await auth_cleanup_task
                except BaseException as error:
                    _note_startup_cleanup_error(primary, "auth_cleanup.await", error)
            if auth_runtime is not None:
                try:
                    await auth_runtime.close()
                except BaseException as error:
                    _note_startup_cleanup_error(primary, "auth.close", error)
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
        if deletion_stop is not None:
            deletion_stop.set()
        if auth_cleanup_stop is not None:
            auth_cleanup_stop.set()
        if recovery_task is not None:
            try:
                await recovery_task
            except BaseException as error:
                primary = error if primary is None else primary
        if deletion_task is not None:
            try:
                await deletion_task
            except BaseException as error:
                primary = error if primary is None else primary
        if auth_cleanup_task is not None:
            try:
                await auth_cleanup_task
            except BaseException as error:
                primary = error if primary is None else primary

        if auth_runtime is not None:
            try:
                await auth_runtime.close()
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
app.include_router(account_router)
app.include_router(health_router)
app.include_router(approvals_router)
app.include_router(sessions_router)
app.include_router(chat_router)
app.include_router(secrets_router)


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
    increment_metric(
        "multiclaw_runtime_capacity_total",
        labels={
            "backend": getattr(request.app.state.database.dialect, "name", "unknown"),
            "operation": "acquire",
            "status": "error",
            "error_class": "runtime_capacity",
        },
    )
    record_trace_event(
        "runtime_capacity",
        attributes={"retry_after": exc.retry_after_seconds},
    )
    return _runtime_error_response(exc.retry_after_seconds)


@app.exception_handler(RuntimeUnavailableError)
async def handle_runtime_unavailable_error(
    request: Request,
    exc: RuntimeUnavailableError,
) -> JSONResponse:
    increment_metric(
        "multiclaw_runtime_capacity_total",
        labels={
            "backend": getattr(request.app.state.database.dialect, "name", "unknown"),
            "operation": "unavailable",
            "status": "error",
            "error_class": "runtime_unavailable",
        },
    )
    return _runtime_error_response(exc.retry_after_seconds)


@app.middleware("http")
async def log_http_requests(request, call_next):
    async with observability_scope(
        metrics=getattr(request.app.state, "operational_metrics", None),
        trace_sink=getattr(request.app.state, "trace_sink", None),
    ):
        started = perf_counter()
        request.state.request_started_at_ms = int(time() * 1000)
        try:
            response = await call_next(request)
        except Exception as error:
            duration_ms = (perf_counter() - started) * 1000
            database = getattr(request.app.state, "database", None)
            backend = getattr(getattr(database, "dialect", None), "name", "unknown")
            observe_database_error(error, backend=backend, operation=request.url.path)
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
