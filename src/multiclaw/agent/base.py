from abc import ABC, abstractmethod

from multiclaw.agent.models import Observation
from multiclaw.events import AgentState, AgentStateEvent, EventBus
from multiclaw.memory import MemoryEntry, MemoryProtocol
from multiclaw.tenancy.context import TenantContext


class BaseAgent(ABC):
    def __init__(self, memory: MemoryProtocol, event_bus: EventBus) -> None:
        self.memory = memory
        self.event_bus = event_bus
        self.state = AgentState.IDLE

    async def transition(self, next_state: AgentState) -> None:
        event = AgentStateEvent(
            agent_id=self.__class__.__name__,
            from_state=self.state,
            to_state=next_state,
        )
        self.state = next_state
        await self.event_bus.publish(event)

    async def remember(self, context: TenantContext, content: str, entry_type: str) -> None:
        await self.memory.save(context, MemoryEntry(content=content, type=entry_type))

    @abstractmethod
    async def handle_message(self, user_input: str, *, context: TenantContext) -> Observation:
        raise NotImplementedError
