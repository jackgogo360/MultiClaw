"""Detached run producers and bounded, durable, scoped SSE journals.

The journal supports delivery replay only. It never replays model or tool work.
Session/account purge already removes all scoped memory entries.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext, asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4, uuid5, NAMESPACE_URL

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select

from multiclaw.memory import MemoryEntry
from multiclaw.runtime.run_control import RunControl, current_run_control
from multiclaw.security.redaction import redact, public_error_message
from multiclaw.stream import DataStreamEncoder
from multiclaw.storage.repositories.memory import MemoryRepository
from multiclaw.storage.schema import memory_entries
from multiclaw.tenancy import TenantContext

logger = logging.getLogger(__name__)
def _journal_id(context):
    return str(uuid5(NAMESPACE_URL, "multiclaw:run-journal:" + ":".join(run_key(context))))


RunKey = tuple[str, str, str, str]
SessionKey = tuple[str, str, str]


def run_key(context: TenantContext) -> RunKey:
    if not context.session_id or not context.run_id:
        raise ValueError('session and run are required')
    return context.tenant_id, context.workspace_id, context.session_id, context.run_id


def session_key(context: TenantContext) -> SessionKey:
    if not context.session_id:
        raise ValueError('session is required')
    return context.tenant_id, context.workspace_id, context.session_id


def redact_encoded_event(chunk: str) -> str:
    lines = []
    for line in chunk.splitlines(keepends=True):
        if line.startswith('data: '):
            payload = line[6:].strip()
            if payload != '[DONE]':
                try:
                    payload = json.dumps(redact(json.loads(payload)), separators=(',', ':'))
                except (ValueError, TypeError):
                    payload = str(redact(payload))
            line = f'data: {payload}\n'
        lines.append(line)
    return ''.join(lines)


@dataclass
class _Producer:
    context: TenantContext
    control: RunControl
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    events: list[tuple[int, str]] = field(default_factory=list)
    steering_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task | None = None


@dataclass
class _Setup:
    context: TenantContext
    task: asyncio.Task | None = None


class BackgroundRunManager:
    def __init__(self, database, settings, *, max_events: int | None = None):
        self.database = database
        self.settings = settings
        self.max_events = max_events or getattr(settings.runtime, 'max_stream_events', 10000)
        self.max_queue = getattr(settings.runtime, 'max_queued_messages', 20)
        self.max_seconds = getattr(settings.runtime, 'max_run_seconds', 1800)
        self._producers: dict[RunKey, _Producer] = {}
        self._setups: dict[SessionKey, _Setup] = {}
        self._queues: dict[SessionKey, deque[tuple[str, Callable[[], Awaitable[None]]]]] = {}
        self._queue_workers: dict[SessionKey, asyncio.Task] = {}
        self._running_queue_ids: set[str] = set()
        self._closed = False
        self._revoked: set[str] = set()
        self._admitted: set[SessionKey] = set()

    def active(self, context: TenantContext) -> bool:
        return run_key(context) in self._producers

    async def record_event(self, context: TenantContext, chunk: str) -> int:
        """Journal a separately owned worker event under its exact Run scope."""
        return await self._append(context, redact_encoded_event(chunk))

    def active_session(self, context: TenantContext) -> bool:
        key = session_key(context)
        return key in self._setups or any(run[:3] == key for run in self._producers)

    def session_busy(self, context: TenantContext) -> bool:
        return session_key(context) in self._admitted or self.active_session(context)

    async def run_setup(self, context, operation, *, release_admission=False):
        if self._closed or context.tenant_id in self._revoked:
            if release_admission:
                self.release_session(context)
            raise HTTPException(503, 'runtime temporarily unavailable')
        key = session_key(context)
        if key in self._setups:
            raise HTTPException(409, 'session setup is already executing')
        state = _Setup(context)
        self._setups[key] = state

        async def execute():
            try:
                return await operation()
            except asyncio.CancelledError:
                # Preflight may have acquired a durable lease before an SSE producer exists.
                if state.context.run_id and not self.active(state.context):
                    from contextlib import suppress
                    from multiclaw.workflow.coordinator import WorkflowCoordinator
                    from multiclaw.workflow.models import RunLease, TERMINAL_RUN_STATUSES, RunStatus
                    with suppress(Exception):
                        coordinator = WorkflowCoordinator(self.database, settings=self.settings)
                        run = await coordinator.get_run(state.context)
                        if run is not None and run.status not in TERMINAL_RUN_STATUSES and run.lease_owner and run.lease_expires_at:
                            await coordinator.finish_run_with_checkpoint(RunLease(state.context,
                                run.lease_owner, run.fencing_token, run.version, run.lease_expires_at), RunStatus.CANCELLED)
                raise
            finally:
                if self._setups.get(key) is state:
                    self._setups.pop(key, None)
                if release_admission:
                    self.release_session(context)

        state.task = asyncio.create_task(execute())
        # Retrieve failures even when the HTTP waiter disconnected during setup.
        def completed(task):
            if self._setups.get(key) is state:
                self._setups.pop(key, None)
                if release_admission:
                    self.release_session(context)
            if not task.cancelled():
                task.exception()
        state.task.add_done_callback(completed)
        return await asyncio.shield(state.task)

    def bind_setup_run(self, context):
        state = self._setups.get(session_key(context))
        if state is not None and state.task is asyncio.current_task():
            state.context = context

    def reserve_session(self, context):
        key = session_key(context)
        if self.session_busy(context):
            raise HTTPException(409, 'session already has an executing run; queue the next message')
        self._admitted.add(key)

    def release_session(self, context):
        self._admitted.discard(session_key(context))
        self._start_queue_worker(context)

    @asynccontextmanager
    async def session_admission(self, context):
        self.reserve_session(context)
        try:
            yield
        finally:
            self.release_session(context)

    async def start(self, context: TenantContext, source: AsyncIterator[str], *, workspace_root=None) -> None:
        if self._closed or context.tenant_id in self._revoked:
            await source.aclose()
            raise HTTPException(503, 'runtime temporarily unavailable')
        key = run_key(context)
        if key in self._producers:
            await source.aclose()
            raise HTTPException(409, 'run is already executing')
        from multiclaw.runtime.inference import current_inference_budget
        budget = current_inference_budget()
        started = budget.started_at if budget is not None and budget.context == context else time.monotonic()
        state = _Producer(context, RunControl(context, deadline=started + self.max_seconds))
        self._producers[key] = state
        state.task = asyncio.create_task(self._produce(key, state, source, workspace_root))

    async def wait_started(self, context: TenantContext, *, timeout: float = 5) -> None:
        """Wait for a public stream's first durable event, when it has one."""
        state = self._producers.get(run_key(context))
        if state is not None:
            await asyncio.wait_for(state.started.wait(), timeout=timeout)

    async def _produce(self, key, state, source, workspace_root):
        token = current_run_control.set(state.control)
        try:
            from multiclaw.runtime.inference import inference_scope
            scope = inference_scope(state.context, settings=self.settings, database=self.database, workspace_root=workspace_root)
        except ImportError:
            scope = nullcontext()
        try:
            with scope:
                async with asyncio.timeout(max(0.001, state.control.deadline - time.monotonic())):
                    async for chunk in source:
                        safe_chunk = redact_encoded_event(chunk)
                        sequence = await self._append(state.context, safe_chunk)
                        state.events.append((sequence, safe_chunk))
                        if len(state.events) > self.max_events:
                            del state.events[: len(state.events) - self.max_events]
                        state.changed.set()
                        state.started.set()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            encoder = DataStreamEncoder()
            await self._append(state.context, encoder.error('Run time budget exceeded'))
            await self._append(state.context, encoder.finish('error'))
        except Exception as error:
            logger.warning('Background run producer failed error_type=%s', type(error).__name__)
            encoder = DataStreamEncoder()
            try:
                await self._append(state.context, encoder.error(public_error_message(error)))
                await self._append(state.context, encoder.finish('error'))
            except Exception:
                logger.warning('Background event persistence failed')
        finally:
            try:
                await source.aclose()
            finally:
                state.started.set()
                current_run_control.reset(token)
                self._producers.pop(key, None)
                state.changed.set()
                self._start_queue_worker(state.context)

    def _filters(self, context):
        return (
            memory_entries.c.tenant_id == context.tenant_id,
            memory_entries.c.workspace_id == context.workspace_id,
            memory_entries.c.session_id == context.session_id,
            memory_entries.c.type == 'run_event',
        )

    async def _append(self, context, chunk) -> int:
        async with self.database.write_transaction() as conn:
            repository = MemoryRepository(conn, context, self.database.dialect)
            # Sequence is session-wide, including continuations of an existing run.
            latest = await conn.execute(select(memory_entries.c.turn_index).where(
                *self._filters(context)).order_by(memory_entries.c.turn_index.desc()).limit(1))
            sequence = (latest.scalar() or 0) + 1
            await repository.save(MemoryEntry(content=chunk, type='run_event', session_id=context.session_id,
                turn_index=sequence, metadata={'run_id': context.run_id}))
            result = await conn.execute(select(memory_entries.c.id, memory_entries.c.turn_index, memory_entries.c.metadata_json).where(*self._filters(context))
                .order_by(memory_entries.c.turn_index.desc()).offset(self.max_events))
            stale = list(result.mappings())
            if stale:
                pruned = {}
                for row in stale:
                    run_id = json.loads(row['metadata_json']).get('run_id')
                    if run_id:
                        pruned[run_id] = max(pruned.get(run_id, 0), int(row['turn_index']))
                for run_id, sequence in pruned.items():
                    run_context = context.for_run(str(context.session_id), run_id)
                    journal_id = _journal_id(run_context)
                    journal = await repository.get(journal_id, context.session_id)
                    expired = max(sequence, journal.metadata.get('expired_through', 0) if journal else 0)
                    await repository.save(MemoryEntry(id=journal_id, content='Run replay retention',
                        type='run_journal', session_id=context.session_id,
                        metadata={'run_id': run_id, 'expired_through': expired}))
                await conn.execute(delete(memory_entries).where(*self._filters(context),
                    memory_entries.c.id.in_([row['id'] for row in stale])))
                # Markers for streams with no retained events may be dropped: they can
                # only produce an empty replay, never a malformed retained suffix.
                journals = await repository.recent(1000, entry_type='run_journal')
                retained = await conn.execute(select(memory_entries.c.metadata_json).where(*self._filters(context)))
                retained_runs = {json.loads(value).get('run_id') for value in retained.scalars()}
                for journal in journals[100:]:
                    if journal.metadata.get('run_id') not in retained_runs:
                        await repository.forget(journal.id)
            return sequence

    async def events(self, context, cursor: int = 0, *, include_ids: bool = True) -> AsyncIterator[str]:
        key = run_key(context)
        while True:
            state = self._producers.get(key)
            if state is not None:
                state.changed.clear()
                # Live subscribers receive the in-memory delivery mirror. The
                # durable journal remains the source for a later reconnect,
                # while the mirror avoids a cross-connection visibility race
                # on the first events of a freshly created SQLite run.
                for sequence, chunk in state.events:
                    if sequence > cursor:
                        cursor = sequence
                        yield (f'id: {cursor}\n' if include_ids else '') + chunk
            async with self.database.connect() as conn:
                result = await conn.execute(select(memory_entries).where(
                    *self._filters(context), memory_entries.c.turn_index > cursor
                ).order_by(memory_entries.c.turn_index.asc()))
                rows = list(result.mappings())
                journal = await MemoryRepository(conn, context, self.database.dialect).get(_journal_id(context), context.session_id)
            if journal is not None and int(journal.metadata.get('expired_through', 0)) > cursor:
                encoder = DataStreamEncoder()
                yield encoder.error('Replay history expired; reload the persisted session messages')
                yield encoder.finish('error')
                return
            for row in rows:
                cursor = int(row['turn_index'])
                if json.loads(row['metadata_json']).get('run_id') == context.run_id:
                    yield (f'id: {cursor}\n' if include_ids else '') + row["content"]
            if state is None:
                return
            if key not in self._producers:
                continue
            try:
                await asyncio.wait_for(state.changed.wait(), 15)
            except asyncio.TimeoutError:
                yield ': keep-alive\n\n'

    async def wait(self, context):
        state = self._producers.get(run_key(context))
        if state is not None and state.task is not None:
            await asyncio.shield(state.task)

    async def cancel(self, context) -> bool:
        state = self._producers.get(run_key(context))
        if state is None or state.task is None:
            setup = self._setups.get(session_key(context))
            if setup is None or setup.context.run_id != context.run_id or setup.task is None:
                return False
            await self._cancel_task(setup.task)
            return True
        state.control.cancelled = True
        await self._cancel_task(state.task)
        return True

    @staticmethod
    async def _cancel_task(task: asyncio.Task) -> None:
        """Cancel locally owned work and tolerate a test/client loop handoff.

        Production producers and shutdown share an event loop, so shutdown waits
        for cleanup.  A synchronous TestClient may own the lifespan on a portal
        loop while a direct async API caller created a producer on the test loop;
        gathering that foreign task raises instead of cancelling it.
        """
        owner_loop = task.get_loop()
        if owner_loop is asyncio.get_running_loop():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return
        if not owner_loop.is_closed():
            owner_loop.call_soon_threadsafe(task.cancel)

    async def steer(self, context, message):
        state = self._producers.get(run_key(context))
        if state is None:
            raise HTTPException(409, 'run is not executing')
        async with state.steering_lock:
            if len(state.control.steering) >= self.max_queue:
                raise HTTPException(429, 'steering queue is full')
            async with self.database.write_transaction() as conn:
                repository = MemoryRepository(conn, context, self.database.dialect)
                recent = await repository.recent(1, entry_type='chat_message')
                await repository.save(MemoryEntry(content=message, type='chat_message', role='user',
                    session_id=context.session_id, turn_index=(recent[0].turn_index + 1 if recent else 1),
                    metadata={'run_id': context.run_id, 'kind': 'steering'}))
            state.control.steering.append(message)

    def enqueue(self, context, operation: Callable[[], Awaitable[None]], *, queue_id: str | None = None) -> str:
        if self._closed or context.tenant_id in self._revoked:
            raise HTTPException(503, 'runtime temporarily unavailable')
        key = session_key(context)
        queue = self._queues.setdefault(key, deque())
        if len(queue) >= self.max_queue:
            raise HTTPException(429, 'session queue is full')
        queue_id = queue_id or str(uuid4())
        queue.append((queue_id, operation))
        self._start_queue_worker(context)
        return queue_id

    async def queue_message(self, context, operation, *, message):
        key = session_key(context)
        if self._closed or context.tenant_id in self._revoked:
            raise HTTPException(503, 'runtime temporarily unavailable')
        if len(self._queues.get(key, ())) >= self.max_queue:
            raise HTTPException(429, 'session queue is full')
        queue_id = str(uuid4())
        async with self.database.write_transaction() as conn:
            repository = MemoryRepository(conn, context, self.database.dialect)
            await repository.save(MemoryEntry(id=queue_id, content=str(redact(message)), type='run_queue',
                session_id=context.session_id, metadata={'queue_id': queue_id, 'status': 'queued'}))
            rows = await repository.recent(1000, entry_type='run_queue')
            active_ids = {entry_id for entry_id, _ in self._queues.get(key, ())} | self._running_queue_ids | {queue_id}
            for stale in rows[100:]:
                if stale.id not in active_ids:
                    await repository.forget(stale.id)
        try:
            return self.enqueue(context, operation, queue_id=queue_id)
        except HTTPException as error:
            await self._update_queue(context, queue_id, 'failed', error=str(error.detail))
            raise

    async def _update_queue(self, context, queue_id, status, **metadata):
        async with self.database.write_transaction() as conn:
            repository = MemoryRepository(conn, context, self.database.dialect)
            entry = await repository.get(queue_id, context.session_id)
            if entry is None:
                return
            await repository.save(entry.model_copy(update={'metadata': entry.metadata | {'status': status} | metadata}))

    async def list_queue(self, context):
        async with self.database.connect() as conn:
            entries = await MemoryRepository(conn, context, self.database.dialect).recent(100, entry_type='run_queue')
        pending = {queue_id for queue_id, _ in self._queues.get(session_key(context), ())} | self._running_queue_ids
        responses = []
        for entry in entries:
            metadata = dict(entry.metadata)
            if metadata.get('status') in {'queued', 'running'} and entry.id not in pending:
                metadata['status'] = 'interrupted'
                metadata['error'] = 'The server stopped before this queued request finished; submit it again.'
            responses.append(metadata | {'message': entry.content, 'created_at': entry.created_at})
        return responses

    def _start_queue_worker(self, context):
        key = session_key(context)
        if self._closed or context.tenant_id in self._revoked or self.active_session(context) or key in self._admitted:
            return
        if self._queues.get(key) and key not in self._queue_workers:
            self._queue_workers[key] = asyncio.create_task(self._drain_queue(context))

    async def _drain_queue(self, context):
        key = session_key(context)
        try:
            while self._queues.get(key) and not self._closed and context.tenant_id not in self._revoked:
                if self.active_session(context) or key in self._admitted:
                    break
                self.reserve_session(context)
                queue_id, operation = self._queues[key].popleft()
                self._running_queue_ids.add(queue_id)
                try:
                    await self._update_queue(context, queue_id, 'running')
                    result = await operation()
                    if isinstance(result, dict):
                        status = result.get('status', 'completed')
                        await self._update_queue(context, queue_id, status, run_id=result.get('run_id'))
                    else:
                        await self._update_queue(context, queue_id, 'completed', run_id=result)
                except asyncio.CancelledError:
                    await self._update_queue(context, queue_id, 'cancelled')
                    raise
                except Exception as error:
                    await self._update_queue(context, queue_id, 'failed', error=public_error_message(error))
                    logger.warning('Queued request failed error_type=%s', type(error).__name__)
                finally:
                    self._running_queue_ids.discard(queue_id)
                    self.release_session(context)
        finally:
            self._queue_workers.pop(key, None)
            if not self._queues.get(key):
                self._queues.pop(key, None)
            self._start_queue_worker(context)

    async def cancel_session(self, context):
        key = session_key(context)
        pending = self._queues.pop(key, ())
        for queue_id, _ in pending:
            await self._update_queue(context, queue_id, 'cancelled')
        setup = self._setups.get(key)
        if setup is not None and setup.task is not None:
            setup.task.cancel()
            await asyncio.gather(setup.task, return_exceptions=True)
        worker = self._queue_workers.get(key)
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
        states = [state for run, state in self._producers.items() if run[:3] == key]
        for state in states:
            await self.cancel(state.context)
        if worker is not None and worker is not asyncio.current_task():
            await asyncio.gather(worker, return_exceptions=True)

    async def revoke(self, tenant_id):
        self._revoked.add(tenant_id)
        contexts = {session_key(state.context): state.context for state in self._producers.values()
                    if state.context.tenant_id == tenant_id}
        for state in self._setups.values():
            if state.context.tenant_id == tenant_id:
                contexts[session_key(state.context)] = state.context
        for key in self._queues:
            if key[0] == tenant_id:
                contexts[key] = TenantContext(*key)
        for context in contexts.values():
            await self.cancel_session(context)

    async def close(self):
        self._closed = True
        setups = [state.task for state in self._setups.values() if state.task is not None]
        for task in setups:
            task.cancel()
        await asyncio.gather(*setups, return_exceptions=True)
        for key, queue in list(self._queues.items()):
            for queue_id, _ in queue:
                await self._update_queue(TenantContext(*key), queue_id, 'cancelled')
        self._queues.clear()
        workers = list(self._queue_workers.values())
        for worker in workers:
            worker.cancel()
        for state in list(self._producers.values()):
            await self.cancel(state.context)
        await asyncio.gather(*workers, return_exceptions=True)
        self._queue_workers.clear()
        self._setups.clear()
        self._admitted.clear()


def background_manager(request) -> BackgroundRunManager:
    manager = getattr(request.app.state, 'background_runs', None)
    if manager is None:
        manager = BackgroundRunManager(request.app.state.database, request.app.state.settings)
        request.app.state.background_runs = manager
        pool = request.app.state.runtime_pool
        pool.background_runs = manager
    return manager


async def background_response(request, context, source, *, headers=None, response_class=StreamingResponse,
        runtime_lease=None, run_lease_handle=None):
    manager = background_manager(request)
    runtime = await request.app.state.runtime_pool.peek(context.tenant_id) if hasattr(request.app.state.runtime_pool, 'peek') else None
    try:
        await manager.start(context, source, workspace_root=getattr(runtime, 'workspace_root', None))
        await manager.wait_started(context)
        return response_class(manager.events(context, include_ids=False), media_type='text/event-stream',
            headers=(headers or {}) | {
                'X-Run-ID': str(context.run_id),
                'X-Session-ID': str(context.session_id),
            })
    except BaseException:
        await manager.cancel(context)
        if run_lease_handle is not None:
            from contextlib import suppress
            from multiclaw.workflow.coordinator import WorkflowCoordinator
            from multiclaw.workflow.models import RunStatus
            with suppress(Exception):
                await run_lease_handle.refresh(lambda lease: WorkflowCoordinator(
                    request.app.state.database, settings=request.app.state.settings
                ).finish_run_with_checkpoint(lease, RunStatus.CANCELLED))
        if runtime_lease is not None:
            runtime_lease.close()
        raise
