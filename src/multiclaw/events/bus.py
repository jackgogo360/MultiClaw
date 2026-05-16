import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Awaitable

from multiclaw.events.types import Event

logger = logging.getLogger(__name__)

Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[str, Handler]]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> str:
        sub_id = uuid.uuid4().hex
        self._handlers[event_type].append((sub_id, handler))
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        for event_type in list(self._handlers):
            self._handlers[event_type] = [
                (sid, h) for sid, h in self._handlers[event_type] if sid != sub_id
            ]

    async def publish(self, event: Event) -> None:
        handlers = {
            sid: h
            for handlers_list in (
                self._handlers.get(event.type, []),
                self._handlers.get("*", []),
            )
            for sid, h in handlers_list
        }
        for handler in handlers.values():
            try:
                await handler(event)
            except Exception:
                logger.exception("Error in event handler for %s", event.type)
