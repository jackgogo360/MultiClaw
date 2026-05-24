import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from multiclaw.agent.context import ContextBuilder, ContextRequest
from multiclaw.agent.models import Action, ActionType, Observation, ObservationType
from multiclaw.agent.toolcall import ToolCallAgent
from multiclaw.config import Settings
from multiclaw.events import AgentState, EventBus
from multiclaw.llm import LLMResponse, ModelRouter
from multiclaw.memory import MemoryEntry, MemoryProtocol
from multiclaw.planner import Planner
from multiclaw.tools import CoreToolScheduler, ToolRegistry
from multiclaw.skills import SkillManager

logger = logging.getLogger(__name__)


def _build_assistant_tool_calls_msg(response: LLMResponse) -> dict:
    msg: dict = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tc.id or f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for i, tc in enumerate(response.tool_calls)
        ],
    }
    if response.reasoning_content:
        msg["reasoning_content"] = response.reasoning_content
    return msg


def _build_tool_result_msg(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class MultiClawAgent(ToolCallAgent):
    def __init__(
        self,
        settings: Settings,
        router: ModelRouter,
        registry: ToolRegistry,
        scheduler: CoreToolScheduler,
        memory: MemoryProtocol,
        planner: Planner,
        event_bus: EventBus,
        skill_manager: SkillManager | None = None,
    ) -> None:
        super().__init__(
            settings=settings,
            router=router,
            registry=registry,
            scheduler=scheduler,
            memory=memory,
            event_bus=event_bus,
        )
        self.planner = planner
        self.context_builder = ContextBuilder(
            memory=memory,
            recent_turns=settings.memory.recent_turns,
            context_history_ratio=settings.memory.context_history_ratio,
            include_legacy_memory=settings.memory.include_legacy_memory_in_retrieval,
        )
        self.skill_manager = skill_manager or SkillManager()

    # ------------------------------------------------------------------
    # non-streaming path
    # ------------------------------------------------------------------

    async def handle_message(self, user_input: str, session_id: str = "") -> Observation:
        if user_input.startswith("plan:"):
            await self._save_chat_msg(user_input, "user", session_id)
            request = user_input[len("plan:") :].strip()
            plan = self.planner.create_plan(request)
            return Observation(
                type=ObservationType.USER_RESPONSE,
                content=self.planner.summary(plan),
            )

        # --- Skill handling ---
        user_msg = user_input

        if user_input.startswith("/"):
            parts = user_input[1:].split(None, 1)
            skill_name = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            self.skill_manager.invoke(skill_name, args)
        else:
            self.skill_manager.process_message(user_msg)

        skill_prompts = self.skill_manager.get_active_skill_prompts()

        messages = await self.context_builder.build(
            ContextRequest(
                system_prompt=self.settings.agent.system_prompt,
                user_input=user_msg,
                session_id=session_id,
                context_window_limit=self.settings.memory.context_window_limit,
                skill_prompts=skill_prompts,
            )
        )
        await self._save_chat_msg(user_msg, "user", session_id)
        tools = self.registry.to_openai_schemas()
        max_rounds = self.settings.agent.max_tool_rounds

        for _ in range(max_rounds):
            response: LLMResponse = await self.router.completion(
                model=self.settings.llm.default_model,
                messages=messages,
                tools=tools,
            )

            if not response.tool_calls:
                await self._save_chat_msg(response.content, "assistant", session_id)
                return Observation(
                    type=ObservationType.USER_RESPONSE,
                    content=response.content,
                )

            # Execute each tool call
            assistant_msg = _build_assistant_tool_calls_msg(response)
            messages.append(assistant_msg)

            for i, tc in enumerate(response.tool_calls):
                call_id = tc.id or f"call_{i}"
                action = Action(
                    type=ActionType.TOOL_CALL,
                    tool_name=tc.name,
                    tool_params=tc.arguments,
                )
                await self.transition(AgentState.ACTING)
                obs = await self.act(action)
                messages.append(
                    _build_tool_result_msg(call_id, obs.content)
                )
                await self.remember(obs.content, "tool_result")

        # Max rounds exceeded — force a final summary without tools
        logger.warning(
            "max tool rounds (%d) exceeded for session=%s, forcing final summary with %d messages",
            max_rounds, session_id, len(messages),
        )
        try:
            response = await self.router.completion(
                model=self.settings.llm.default_model,
                messages=messages,
                tools=None,
            )
            await self._save_chat_msg(response.content, "assistant", session_id)
            return Observation(
                type=ObservationType.USER_RESPONSE,
                content=response.content,
            )
        except Exception:
            logger.exception("final summary failed")
            return Observation(
                type=ObservationType.ERROR,
                content="I wasn't able to complete this task within the allowed rounds.",
            )

    # ------------------------------------------------------------------
    # streaming path
    # ------------------------------------------------------------------

    async def handle_message_stream(
        self, user_input: str, session_id: str = ""
    ) -> AsyncIterator[dict[str, Any]]:
        logger.info("handle_message_stream: %r", user_input[:80])

        if user_input.startswith("plan:"):
            await self._save_chat_msg(user_input, "user", session_id)
            logger.info("-> plan mode")
            request = user_input[len("plan:") :].strip()
            plan = self.planner.create_plan(request)
            yield {
                "type": "done",
                "content": self.planner.summary(plan),
                "data": {},
            }
            return

        await self.transition(AgentState.THINKING)

        # --- Skill handling ---
        user_msg = user_input

        if user_input.startswith("/"):
            parts = user_input[1:].split(None, 1)
            skill_name = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            body = self.skill_manager.invoke(skill_name, args)
            if body is not None:
                yield {"type": "skill", "name": skill_name, "active": True}
        else:
            activated = self.skill_manager.process_message(user_msg)
            for s in activated:
                yield {"type": "skill", "name": s.name, "active": True}

        skill_prompts = self.skill_manager.get_active_skill_prompts()

        messages = await self.context_builder.build(
            ContextRequest(
                system_prompt=self.settings.agent.system_prompt,
                user_input=user_msg,
                session_id=session_id,
                context_window_limit=self.settings.memory.context_window_limit,
                skill_prompts=skill_prompts,
            )
        )
        await self._save_chat_msg(user_msg, "user", session_id)
        tools = self.registry.to_openai_schemas()
        max_rounds = self.settings.agent.max_tool_rounds

        for round_num in range(max_rounds):
            logger.info("round %d/%d, messages=%d", round_num + 1, max_rounds, len(messages))
            for i, msg in enumerate(messages):
                logger.info("  msg[%d] role=%s tc=%s", i, msg.get("role"), bool(msg.get("tool_calls")))
            full_text = ""

            try:
                async for event in self.router.stream_completion(
                    model=self.settings.llm.default_model,
                    messages=messages,
                    tools=tools,
                ):
                    if event["type"] == "token":
                        full_text += event["content"]
                        yield event

                    elif event["type"] == "reasoning":
                        yield event

                    elif event["type"] == "tool_calls":
                        tc_names = [tc["name"] for tc in event["calls"]]
                        logger.info(
                            "round %d/%d tool_calls=%s reasoning=%s",
                            round_num + 1, max_rounds, tc_names,
                            event.get("reasoning_content", "")[:120],
                        )
                        reasoning = event.get("reasoning_content", "")
                        for tc in event["calls"]:
                            yield {
                                "type": "tool_call",
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            }

                        tool_calls_msg: dict = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": tc["id"] or f"call_{i}",
                                    "type": "function",
                                    "function": {
                                        "name": tc["name"],
                                        "arguments": json.dumps(
                                            tc["arguments"], ensure_ascii=False
                                        ),
                                    },
                                }
                                for i, tc in enumerate(event["calls"])
                            ],
                        }
                        if reasoning:
                            tool_calls_msg["reasoning_content"] = reasoning
                        messages.append(tool_calls_msg)

                        for i, tc in enumerate(event["calls"]):
                            call_id = tc["id"] or f"call_{i}"
                            action = Action(
                                type=ActionType.TOOL_CALL,
                                tool_name=tc["name"],
                                tool_params=tc["arguments"],
                            )
                            await self.transition(AgentState.ACTING)
                            obs = await self.act(action)

                            yield {
                                "type": "tool_result",
                                "name": tc["name"],
                                "content": obs.content,
                            }

                            messages.append(
                                _build_tool_result_msg(call_id, obs.content)
                            )
                            await self.remember(obs.content, "tool_result")

                        break  # tool_calls handled, continue outer loop

                else:
                    # No tool_calls — pure text response
                    logger.info("streaming complete, text_len=%d", len(full_text))
                    await self._save_chat_msg(full_text, "assistant", session_id)
                    await self.transition(AgentState.FINISHED)
                    yield {"type": "done", "content": full_text, "data": {}}
                    return

            except (httpx.ReadTimeout, asyncio.TimeoutError) as exc:
                logger.error(
                    "stream timeout round=%d/%d session=%s input=%r",
                    round_num + 1, max_rounds, session_id, user_input[:200],
                )
                yield {
                    "type": "error",
                    "content": f"Request timed out: {exc}",
                }
                return

        # Max rounds exceeded — force a final summary without tools
        logger.warning(
            "max tool rounds (%d) exceeded for session=%s, forcing final summary with %d messages",
            max_rounds, session_id, len(messages),
        )
        full_text = ""
        try:
            async for event in self.router.stream_completion(
                model=self.settings.llm.default_model,
                messages=messages,
                tools=None,  # no tools — force text response
            ):
                if event["type"] == "token":
                    full_text += event["content"]
                    yield event
                elif event["type"] == "reasoning":
                    yield event
            if full_text:
                await self._save_chat_msg(full_text, "assistant", session_id)
            yield {"type": "done", "content": full_text, "data": {}}
        except Exception:
            logger.exception("final summary failed")
            yield {
                "type": "done",
                "content": "I wasn't able to complete this task within the allowed rounds.",
                "data": {},
            }

    async def _save_chat_msg(self, content: str, role: str, session_id: str) -> None:
        await self.memory.save(
            MemoryEntry(
                content=content,
                type="chat_message",
                role=role,
                session_id=session_id,
            )
        )
