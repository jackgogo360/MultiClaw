from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from copy import deepcopy

from multiclaw.events.types import EventScope, ScopedEvent

logger = logging.getLogger(__name__)

type ScopeKey = tuple[str, str, str, str]
type Handler = Callable[[ScopedEvent], Awaitable[None]]


class Subscription:
    def __init__(self, close_fn: Callable[[], None]) -> None:
        self._close_fn = close_fn
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close_fn()


class EventRouter:
    def __init__(self) -> None:
        self._handlers: dict[ScopeKey, list[tuple[str, Handler]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, scope: EventScope, handler: Handler) -> Subscription:
        key = self._scope_key(scope)
        sub_id = uuid.uuid4().hex
        with self._lock:
            self._handlers[key].append((sub_id, handler))
        return Subscription(lambda: self._unsubscribe(key, sub_id))

    async def publish(self, event: ScopedEvent) -> None:
        key = self._scope_key(event)
        with self._lock:
            handlers = list(self._handlers.get(key, ()))
        for _sub_id, handler in handlers:
            try:
                delivery_event = self._clone_event(event)
            except Exception:
                logger.exception(
                    "Error cloning scoped event for subscriber delivery: %s",
                    event.event_type,
                )
                continue
            try:
                await handler(delivery_event)
            except Exception:
                logger.exception(
                    "Error in scoped event handler for %s",
                    event.event_type,
                )

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()

    def _unsubscribe(self, key: ScopeKey, sub_id: str) -> None:
        with self._lock:
            handlers = self._handlers.get(key)
            if handlers is None:
                return
            remaining = [(sid, handler) for sid, handler in handlers if sid != sub_id]
            if remaining:
                self._handlers[key] = remaining
            else:
                self._handlers.pop(key, None)

    @staticmethod
    def _scope_key(scope: EventScope | ScopedEvent) -> ScopeKey:
        return (
            scope.tenant_id,
            scope.workspace_id,
            scope.session_id,
            scope.run_id,
        )

    @staticmethod
    def _clone_event(event: ScopedEvent) -> ScopedEvent:
        return event.model_copy(
            update={"data": deepcopy(event.data)},
        )
