import logging

from multiclaw.agent.models import Action, ActionType, Observation, ObservationType
from multiclaw.agent.react import ReActAgent
from multiclaw.config import Settings
from multiclaw.events import EventBus, EventRouter
from multiclaw.llm import LLMResponse, ModelRouter
from multiclaw.memory import MemoryProtocol
from multiclaw.tools import CoreToolScheduler, ToolRegistry

logger = logging.getLogger(__name__)


class ToolCallAgent(ReActAgent):
    def __init__(
        self,
        settings: Settings,
        router: ModelRouter,
        registry: ToolRegistry,
        scheduler: CoreToolScheduler,
        memory: MemoryProtocol,
        event_bus: EventBus,
        event_router: EventRouter | None = None,
    ) -> None:
        super().__init__(memory=memory, event_bus=event_bus, event_router=event_router)
        self.settings = settings
        self.router = router
        self.registry = registry
        self.scheduler = scheduler

    async def think(
        self, messages: list[dict], tools: list[dict]
    ) -> Action:
        response = await self.router.completion(
            model=self.settings.llm.default_model,
            messages=messages,
            tools=tools,
        )
        return self._response_to_action(response)

    def _response_to_action(self, response: LLMResponse) -> Action:
        if response.tool_calls:
            tc = response.tool_calls[0]
            return Action(
                type=ActionType.TOOL_CALL,
                tool_name=tc.name,
                tool_params=tc.arguments,
            )
        return Action(type=ActionType.RESPONSE, content=response.content)

    async def act(self, action: Action) -> Observation:
        if action.type == ActionType.TOOL_CALL:
            builder = self.registry.get(action.tool_name)
            if builder is None:
                return Observation(
                    type=ObservationType.ERROR,
                    content=f"unknown tool: {action.tool_name}",
                )
            result = await self.scheduler.run(builder, action.tool_params)
            return Observation(
                type=ObservationType.TOOL_RESULT,
                content=result.content,
                data=result.data,
            )

        return Observation(type=ObservationType.USER_RESPONSE, content=action.content)
