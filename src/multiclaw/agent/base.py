from abc import ABC, abstractmethod

from multiclaw.agent.models import Observation
from multiclaw.events import AgentState, AgentStateEvent, EventBus, EventRouter, ScopedEvent
from multiclaw.memory import MemoryEntry, MemoryProtocol
from multiclaw.tenancy.context import TenantContext


class BaseAgent(ABC):
    def __init__(
        self,
        memory: MemoryProtocol,
        event_bus: EventBus,
        event_router: EventRouter | None = None,
    ) -> None:
        self.memory = memory
        self.event_bus = event_bus
        self.event_router = event_router
        self.state = AgentState.IDLE

    async def transition(
        self,
        next_state: AgentState,
        *,
        context: TenantContext | None = None,
    ) -> None:
        event = AgentStateEvent(
            agent_id=self.__class__.__name__,
            from_state=self.state,
            to_state=next_state,
        )
        self.state = next_state
        await self.event_bus.publish(event)
        if self.event_router is not None and context is not None:
            await self.event_router.publish(
                ScopedEvent.from_context(
                    context,
                    "agent.state_change",
                    {
                        "agent_id": self.__class__.__name__,
                        "from_state": event.from_state.value,
                        "to_state": event.to_state.value,
                    },
                )
            )

    async def remember(self, context: TenantContext, content: str, entry_type: str) -> None:
        if context.session_id is None:
            raise ValueError("session_id is required for agent memory entries")
        await self.memory.save(
            context,
            MemoryEntry(content=content, type=entry_type, session_id=context.session_id),
        )

    @abstractmethod
    async def handle_message(self, user_input: str, *, context: TenantContext) -> Observation:
        raise NotImplementedError
