from abc import ABC, abstractmethod

from multiclaw.agent.base import BaseAgent
from multiclaw.agent.models import Action, Observation
from multiclaw.events import AgentState


class ReActAgent(BaseAgent, ABC):
    async def step(self, user_input: str) -> Observation:
        await self.transition(AgentState.THINKING)
        action = await self.think(user_input)
        await self.transition(AgentState.ACTING)
        observation = await self.act(action)
        await self.transition(AgentState.FINISHED)
        return observation

    @abstractmethod
    async def think(self, user_input: str) -> Action:
        raise NotImplementedError

    @abstractmethod
    async def act(self, action: Action) -> Observation:
        raise NotImplementedError
