import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s %(message)s",
    datefmt="%Y%m%d %H:%M:%S",
)
logger = logging.getLogger("multiclaw")

from multiclaw.agent import MultiClawAgent
from multiclaw.config import Settings
from multiclaw.events import Event, EventBus
from multiclaw.governance import InMemoryAuditLogger, PermissionChecker, ProcessSandbox
from multiclaw.llm import ModelRouter
from multiclaw.memory import SqliteMemory
from multiclaw.planner import Planner
from multiclaw.session import SqliteSessionStore, SessionStatus
from multiclaw.tools import (
    CoreToolScheduler,
    ToolRegistry,
)
from multiclaw.tools.builtin import (
    EditFileToolBuilder,
    FindDirToolBuilder,
    GlobToolBuilder,
    GrepToolBuilder,
    ReadFileToolBuilder,
    UndoEditToolBuilder,
    WriteFileToolBuilder,
)
from multiclaw.tools.list_dir import ListDirToolBuilder


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

agent: MultiClawAgent
shared_bus: EventBus


def create_agent() -> MultiClawAgent:
    global shared_bus
    shared_bus = EventBus()

    config_path = Path(__file__).resolve().parent.parent.parent / "multiclaw.toml"
    if not config_path.exists():
        config_path = Path(__file__).resolve().parent.parent / "multiclaw.toml"
    if not config_path.exists():
        config_path = Path("multiclaw.toml")

    settings = Settings(_config_file=str(config_path) if config_path.exists() else None)
    workspace_root = config_path.resolve().parent if config_path.exists() else Path.cwd().resolve()

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

    scheduler = CoreToolScheduler(
        permission_checker=PermissionChecker(
            guarded_tools={
                "read_file",
                "write_file",
                "edit_file",
                "undo_edit",
                "glob",
                "list_dir",
                "grep",
                "find_dir",
            }
        ),
        sandbox=ProcessSandbox(),
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
    )
    runtime_agent.session_store = SqliteSessionStore(settings.database.path)
    return runtime_agent


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent
    agent = create_agent()
    yield


app = FastAPI(title="MultiClaw", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class SessionCreateRequest(BaseModel):
    title: str = "New Chat"


class SessionRenameRequest(BaseModel):
    title: str


class ApproveRequest(BaseModel):
    request_id: str
    approved: bool


@app.post("/approve")
async def approve(req: ApproveRequest):
    ok = agent.scheduler.resolve_approval(req.request_id, req.approved)
    return {"ok": ok}


@app.get("/sessions")
async def list_sessions(include_archived: bool = False):
    sessions = await agent.session_store.list_sessions(include_archived=include_archived)
    return [session.model_dump(mode="json") for session in sessions]


@app.post("/sessions")
async def create_session(req: SessionCreateRequest):
    session = await agent.session_store.create(title=req.title)
    return session.model_dump(mode="json")


@app.patch("/sessions/{session_id}")
async def rename_session(session_id: str, req: SessionRenameRequest):
    session = await agent.session_store.rename(session_id, req.title)
    return session.model_dump(mode="json")


@app.post("/sessions/{session_id}/archive")
async def archive_session(session_id: str):
    session = await agent.session_store.archive(session_id)
    return session.model_dump(mode="json")


@app.post("/sessions/{session_id}/restore")
async def restore_session(session_id: str):
    session = await agent.session_store.restore(session_id)
    return session.model_dump(mode="json")


@app.post("/chat")
async def chat(req: ChatRequest):
    """SSE streaming — real token streaming from LLM with state events."""

    # Resolve or create session
    session = None
    if req.session_id:
        session = await agent.session_store.get(req.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if session.status == SessionStatus.ARCHIVED:
            raise HTTPException(status_code=409, detail="session is archived")
    else:
        session = await agent.session_store.create()

    # Update session activity (title from first message)
    await agent.session_store.touch_message(session.id, req.message)

    async def event_stream():
        logger.info("SSE stream started, message=%r, session=%r", req.message[:80], session.id)

        # Emit session event first
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "session",
                    "session_id": session.id,
                    "title": session.title,
                }
            )
            + "\n\n"
        )

        token_queue: asyncio.Queue[dict] = asyncio.Queue()
        event_queue: asyncio.Queue[Event] = asyncio.Queue()

        async def collector(event: Event):
            await event_queue.put(event)

        sub_id = shared_bus.subscribe("*", collector)

        async def run_stream():
            try:
                async for item in agent.handle_message_stream(req.message, session_id=session.id):
                    await token_queue.put(item)
            except Exception as exc:
                logger.exception("stream error")
                await token_queue.put({"type": "error", "content": str(exc)})

        stream_task = asyncio.create_task(run_stream())

        try:
            while True:
                # Non-blocking drain of the token stream
                token_count = 0
                while True:
                    try:
                        item = token_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    token_count += 1
                    if item["type"] == "token":
                        yield (
                            "data: "
                            + json.dumps(
                                {"type": "token", "content": item["content"]},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                    elif item["type"] == "done":
                        logger.info("stream done, tokens=%d, content_len=%d", token_count, len(item.get("content", "")))
                        yield (
                            "data: "
                            + json.dumps(
                                {"type": "done", "content": item["content"], "data": item.get("data", {})},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        return
                    elif item["type"] == "error":
                        logger.error("stream error: %s", item["content"])
                        yield (
                            "data: "
                            + json.dumps({"type": "error", "content": item["content"]})
                            + "\n\n"
                        )
                        return
                    else:
                        # Forward tool_call, tool_result, reasoning, and any new types
                        yield (
                            "data: "
                            + json.dumps(item, ensure_ascii=False)
                            + "\n\n"
                        )

                # Non-blocking drain of bus events
                while not event_queue.empty():
                    evt = event_queue.get_nowait()
                    if evt.type == "tool.awaiting_approval":
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "approval_required",
                                    "request_id": evt.data.get("request_id", ""),
                                    "tool": evt.data.get("tool", ""),
                                    "params": evt.data.get("params", {}),
                                }
                            )
                            + "\n\n"
                        )
                    else:
                        yield (
                            "data: "
                            + json.dumps({"type": "state", "state": evt.type})
                            + "\n\n"
                        )

                # Check if stream task crashed
                if stream_task.done():
                    exc = stream_task.exception()
                    if exc:
                        logger.exception("stream task crashed")
                        yield (
                            "data: "
                            + json.dumps({"type": "error", "content": str(exc)})
                            + "\n\n"
                        )
                    return

                await asyncio.sleep(0.02)
        finally:
            stream_task.cancel()
            shared_bus.unsubscribe(sub_id)
            logger.info("SSE stream ended")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# HTML UI (inline)
# ---------------------------------------------------------------------------

_HTML_PATH = Path(__file__).parent / "static" / "index.html"
_CHAT_HTML = _HTML_PATH.read_text()


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=_CHAT_HTML)
