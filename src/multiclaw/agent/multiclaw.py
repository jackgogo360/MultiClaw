import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

import httpx

from multiclaw.agent.context import ContextBuilder, ContextRequest
from multiclaw.agent.models import Observation, ObservationType
from multiclaw.agent.resilience import ResilienceAction, ResilienceController
from multiclaw.agent.tool_batch import ToolBatchExecutor, ToolCallOutcome, ToolCallSpec
from multiclaw.agent.toolcall import ToolCallAgent
from multiclaw.config import Settings
from multiclaw.events import AgentState, EventBus, EventRouter
from multiclaw.llm import LLMResponse, ModelRouter
from multiclaw.memory import MemoryEntry, MemoryProtocol
from multiclaw.planner import Planner
from multiclaw.skills import SkillManager
from multiclaw.tenancy.context import TenantContext
from multiclaw.tools import CoreToolScheduler, ToolRegistry
from multiclaw.workflow.models import RunLease, RunLeaseHandle

logger = logging.getLogger(__name__)
DSML_TOOLCALL_PATTERN = re.compile(
    r"<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>",
    re.DOTALL,
)
DSML_TAG_PATTERN = re.compile(r"</?｜｜DSML｜｜[^>]*>")
FINAL_SUMMARY_PLAIN_TEXT_PROMPT = (
    "You have reached the tool limit. "
    "Do not call any tools. "
    "Do not output DSML, XML, HTML, or any tool-call tags. "
    "Using only the information already gathered in the conversation, "
    "answer directly in plain Markdown for the user."
)
REFLECTION_PROMPT = (
    "Runtime reflection required. The previous approach made no progress: {reason}. "
    "Explain the likely root cause in at most 120 words and choose materially different "
    "tools or parameters. Do not call tools in this reflection."
)


def _build_assistant_tool_calls_msg(
    calls: list[dict[str, Any]],
    reasoning_content: str = "",
) -> dict:
    msg: dict = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tc["id"] or f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                },
            }
            for i, tc in enumerate(calls)
        ],
    }
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
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
        event_router: EventRouter | None = None,
        skill_manager: SkillManager | None = None,
    ) -> None:
        super().__init__(
            settings=settings,
            router=router,
            registry=registry,
            scheduler=scheduler,
            memory=memory,
            event_bus=event_bus,
            event_router=event_router,
        )
        self.planner = planner
        self.context_builder = ContextBuilder(
            memory=memory,
            recent_turns=settings.memory.recent_turns,
            context_history_ratio=settings.memory.context_history_ratio,
            include_legacy_memory=settings.memory.include_legacy_memory_in_retrieval,
            progressive_enabled=settings.memory.progressive_context_enabled,
            response_reserve_tokens=settings.memory.context_response_reserve_tokens,
            l1_ratio=settings.memory.context_l1_ratio,
        )
        self.skill_manager = skill_manager or SkillManager()
        self.tool_batch_executor = ToolBatchExecutor(
            registry=registry,
            scheduler=scheduler,
            max_concurrency=settings.tools.parallel_max_concurrency,
            enabled=settings.tools.parallel_read_only_enabled,
        )

    def _build_resilience_controller(self) -> ResilienceController | None:
        if not self.settings.agent.resilience_enabled:
            return None
        return ResilienceController(
            repeat_limit=self.settings.agent.no_progress_repeat_limit,
            max_reflections=self.settings.agent.reflection_max_attempts,
        )

    async def _generate_reflection(
        self,
        messages: list[dict[str, Any]],
        reason: str,
    ) -> str:
        prompt = REFLECTION_PROMPT.format(reason=reason)
        response = await self.router.completion(
            model=self.settings.llm.default_model,
            messages=[*messages, {"role": "system", "content": prompt}],
            tools=None,
        )
        reflection = response.content.strip()
        return reflection or "Use a materially different approach."

    async def _attempt_reflection(
        self,
        messages: list[dict[str, Any]],
        reason: str,
    ) -> str | None:
        try:
            reflection = await self._generate_reflection(messages, reason)
        except Exception:
            logger.exception("reflection generation failed")
            return None
        return reflection

    @staticmethod
    def _normalize_tool_calls(calls: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for i, call in enumerate(calls):
            if hasattr(call, "name") and hasattr(call, "arguments"):
                call_id = getattr(call, "id", "") or f"call_{i}"
                name = call.name
                arguments = call.arguments
            else:
                call_id = call.get("id") or f"call_{i}"
                name = call["name"]
                arguments = call["arguments"]
            normalized.append(
                {
                    "id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )
        return normalized

    @staticmethod
    def _build_reflection_feedback_msg(reflection: str) -> dict[str, str]:
        return {
            "role": "system",
            "content": f"Runtime reflection feedback: {reflection}",
        }

    def _require_tool_batch_executor(self) -> ToolBatchExecutor:
        executor = getattr(self, "tool_batch_executor", None)
        if executor is None:
            raise RuntimeError("tool_batch_executor is not initialized")
        return executor

    async def _build_context(self, request: ContextRequest) -> list[dict[str, Any]]:
        result = await self.context_builder.build_with_report(request)
        logger.info(
            "context_budget used=%s dropped=%s limit=%d reserve=%d",
            result.report.used_tokens_by_level,
            result.report.dropped_by_level,
            result.report.limit_tokens,
            result.report.reserved_response_tokens,
        )
        return result.messages

    @staticmethod
    def _build_tool_call_specs(calls: list[dict[str, Any]]) -> list[ToolCallSpec]:
        return [
            ToolCallSpec(
                call_id=call["id"],
                name=call["name"],
                arguments=call["arguments"],
            )
            for call in calls
        ]

    async def _execute_tool_batch(
        self,
        calls: list[dict[str, Any]],
        *,
        context: TenantContext | None = None,
        run_lease_handle: RunLeaseHandle | None = None,
    ) -> list[ToolCallOutcome]:
        if not calls:
            return []
        await self.transition(AgentState.ACTING, context=context)
        return await self._require_tool_batch_executor().execute(
            self._build_tool_call_specs(calls),
            context=context,
            run_lease_handle=run_lease_handle,
        )

    # ------------------------------------------------------------------
    # non-streaming path
    # ------------------------------------------------------------------

    async def handle_message(
        self,
        user_input: str,
        *,
        context: TenantContext,
    ) -> Observation:
        if user_input.startswith("plan:"):
            next_turn_index = await self._next_turn_index(context)
            await self._save_chat_msg(context, "user", user_input, next_turn_index)
            request = user_input[len("plan:") :].strip()
            plan = self.planner.create_plan(request)
            return Observation(
                type=ObservationType.USER_RESPONSE,
                content=self.planner.summary(plan),
            )
        try:
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

            messages = await self._build_context(
                ContextRequest(
                    system_prompt=self.settings.agent.system_prompt,
                    user_input=user_msg,
                    context=context,
                    context_window_limit=self.settings.memory.context_window_limit,
                    skill_prompts=skill_prompts,
                )
            )
            next_turn_index = await self._next_turn_index(context)
            await self._save_chat_msg(context, "user", user_msg, next_turn_index)
            tools = self.registry.to_openai_schemas()
            max_rounds = self.settings.agent.max_tool_rounds
            controller = self._build_resilience_controller()

            for _ in range(max_rounds):
                response: LLMResponse = await self.router.completion(
                    model=self.settings.llm.default_model,
                    messages=messages,
                    tools=tools,
                )

                if not response.tool_calls:
                    await self._save_chat_msg(
                        context,
                        "assistant",
                        response.content,
                        next_turn_index + 1,
                    )
                    return Observation(
                        type=ObservationType.USER_RESPONSE,
                        content=response.content,
                    )

                normalized_calls = self._normalize_tool_calls(response.tool_calls)
                if controller is not None:
                    call_decision = controller.observe_calls(normalized_calls)
                    if call_decision.action == ResilienceAction.REFLECT:
                        reflection = await self._attempt_reflection(
                            messages, call_decision.reason
                        )
                        if reflection is None:
                            break
                        controller.mark_reflection_used()
                        messages.append(self._build_reflection_feedback_msg(reflection))
                        continue
                    if call_decision.action == ResilienceAction.TERMINATE:
                        break

                assistant_msg = _build_assistant_tool_calls_msg(
                    normalized_calls,
                    response.reasoning_content,
                )
                messages.append(assistant_msg)
                outcomes = await self._execute_tool_batch(normalized_calls, context=context)
                result_contents = [outcome.observation.content for outcome in outcomes]

                for outcome in outcomes:
                    messages.append(
                        _build_tool_result_msg(
                            outcome.call_id,
                            outcome.observation.content,
                        )
                    )
                    await self.remember(context, outcome.observation.content, "tool_result")

                if controller is not None:
                    result_decision = controller.observe_results(result_contents)
                    if result_decision.action == ResilienceAction.REFLECT:
                        reflection = await self._attempt_reflection(
                            messages, result_decision.reason
                        )
                        if reflection is None:
                            break
                        controller.mark_reflection_used()
                        messages.append(self._build_reflection_feedback_msg(reflection))
                        continue
                    if result_decision.action == ResilienceAction.TERMINATE:
                        break

            # Max rounds exceeded — force a final summary without tools
            logger.warning(
                "max tool rounds (%d) exceeded for session=%s, forcing final summary with %d messages",
                max_rounds, context.session_id, len(messages),
            )
            try:
                full_text = await self._generate_final_summary(
                    messages,
                    self._collect_completion_text_response,
                )
                await self._save_chat_msg(context, "assistant", full_text, next_turn_index + 1)
                return Observation(
                    type=ObservationType.USER_RESPONSE,
                    content=full_text,
                )
            except Exception:
                logger.exception("final summary failed")
                return Observation(
                    type=ObservationType.ERROR,
                    content="I wasn't able to complete this task within the allowed rounds.",
                )
        finally:
            if self.state != AgentState.IDLE:
                try:
                    await self.transition(AgentState.IDLE, context=context)
                except BaseException:
                    logger.exception("failed to reset agent state after non-stream run termination")

    # ------------------------------------------------------------------
    # streaming path
    # ------------------------------------------------------------------

    @staticmethod
    def _contains_dsml_tool_markup(text: str) -> bool:
        return "<｜｜DSML｜｜" in text

    @staticmethod
    def _strip_dsml_tool_markup(text: str) -> str:
        cleaned = DSML_TOOLCALL_PATTERN.sub("", text)
        cleaned = DSML_TAG_PATTERN.sub("", cleaned)
        return cleaned.strip()

    async def _collect_completion_text_response(
        self,
        messages: list[dict[str, Any]],
    ) -> str:
        response = await self.router.completion(
            model=self.settings.llm.default_model,
            messages=messages,
            tools=None,
        )
        return response.content

    async def _collect_plain_text_response(
        self,
        messages: list[dict[str, Any]],
    ) -> str:
        full_text = ""
        reasoning_text = ""
        async for event in self.router.stream_completion(
            model=self.settings.llm.default_model,
            messages=messages,
            tools=None,
        ):
            if event["type"] == "token":
                full_text += event["content"]
            elif event["type"] == "reasoning":
                reasoning_text += event["content"]
        return full_text or reasoning_text

    async def _generate_final_summary(
        self,
        messages: list[dict[str, Any]],
        collect_response: Callable[
            [list[dict[str, Any]]],
            Awaitable[str],
        ],
    ) -> str:
        full_text = await collect_response(messages)
        if self._contains_dsml_tool_markup(full_text):
            logger.warning(
                "DSML tool markup detected in forced final summary, retrying with stricter plain-text prompt"
            )
            retry_messages = [
                {"role": "system", "content": FINAL_SUMMARY_PLAIN_TEXT_PROMPT},
                *messages,
            ]
            retry_text = await collect_response(retry_messages)
            full_text = retry_text or full_text
        return self._strip_dsml_tool_markup(full_text)

    async def handle_message_stream(
        self,
        user_input: str,
        *,
        context: TenantContext,
        run_lease: RunLease | None = None,
        run_lease_handle: RunLeaseHandle | None = None,
        workflow_continuation=None,
        workflow_recovery=None,
    ) -> AsyncIterator[dict[str, Any]]:
        del run_lease, workflow_recovery
        logger.info("handle_message_stream: %r", user_input[:80])

        if user_input.startswith("plan:"):
            next_turn_index = await self._next_turn_index(context)
            await self._save_chat_msg(context, "user", user_input, next_turn_index)
            logger.info("-> plan mode")
            request = user_input[len("plan:") :].strip()
            plan = self.planner.create_plan(request)
            yield {
                "type": "done",
                "content": self.planner.summary(plan),
                "data": {},
            }
            return

        try:
            await self.transition(AgentState.THINKING, context=context)

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

            messages = await self._build_context(
                ContextRequest(
                    system_prompt=self.settings.agent.system_prompt,
                    user_input=user_msg,
                    context=context,
                    context_window_limit=self.settings.memory.context_window_limit,
                    skill_prompts=skill_prompts,
                )
            )
            next_turn_index = await self._next_turn_index(context)
            await self._save_chat_msg(context, "user", user_msg, next_turn_index)
            tools = self.registry.to_openai_schemas()
            max_rounds = self.settings.agent.max_tool_rounds
            controller = self._build_resilience_controller()

            for round_num in range(max_rounds):
                logger.info("round %d/%d, messages=%d", round_num + 1, max_rounds, len(messages))
                for i, msg in enumerate(messages):
                    logger.info("  msg[%d] role=%s tc=%s", i, msg.get("role"), bool(msg.get("tool_calls")))
                full_text = ""
                reasoning_text = ""
                handled_tool_calls = False
                reflect_requested = False
                terminate_requested = False

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
                            reasoning_text += event["content"]
                            yield event

                        elif event["type"] == "tool_calls":
                            tc_names = [tc["name"] for tc in event["calls"]]
                            logger.info(
                                "round %d/%d tool_calls=%s reasoning=%s",
                                round_num + 1, max_rounds, tc_names,
                                event.get("reasoning_content", "")[:120],
                            )
                            reasoning = event.get("reasoning_content", "")
                            normalized_calls = self._normalize_tool_calls(event["calls"])
                            if controller is not None:
                                call_decision = controller.observe_calls(normalized_calls)
                                if call_decision.action == ResilienceAction.REFLECT:
                                    reflection = await self._attempt_reflection(
                                        messages, call_decision.reason
                                    )
                                    if reflection is None:
                                        terminate_requested = True
                                        break
                                    controller.mark_reflection_used()
                                    messages.append(
                                        self._build_reflection_feedback_msg(reflection)
                                    )
                                    yield {
                                        "type": "state",
                                        "name": "reflection",
                                        "content": reflection,
                                    }
                                    reflect_requested = True
                                    break
                                if call_decision.action == ResilienceAction.TERMINATE:
                                    terminate_requested = True
                                    break

                            for call in normalized_calls:
                                yield {
                                    "type": "tool_call",
                                    "call_id": call["id"],
                                    "name": call["name"],
                                    "arguments": call["arguments"],
                                }

                            tool_calls_msg = _build_assistant_tool_calls_msg(
                                normalized_calls,
                                reasoning,
                            )
                            messages.append(tool_calls_msg)
                            outcomes = await self._execute_tool_batch(
                                normalized_calls,
                                context=context,
                                run_lease_handle=run_lease_handle,
                            )
                            result_contents: list[str] = []

                            for outcome in outcomes:
                                yield {
                                    "type": "tool_result",
                                    "call_id": outcome.call_id,
                                    "name": outcome.name,
                                    "content": outcome.observation.content,
                                }

                                result_contents.append(outcome.observation.content)
                                messages.append(
                                    _build_tool_result_msg(
                                        outcome.call_id,
                                        outcome.observation.content,
                                    )
                                )
                                await self.remember(context, outcome.observation.content, "tool_result")

                            if controller is not None:
                                result_decision = controller.observe_results(result_contents)
                                if result_decision.action == ResilienceAction.REFLECT:
                                    reflection = await self._attempt_reflection(
                                        messages, result_decision.reason
                                    )
                                    if reflection is None:
                                        terminate_requested = True
                                    else:
                                        controller.mark_reflection_used()
                                        messages.append(
                                            self._build_reflection_feedback_msg(reflection)
                                        )
                                        yield {
                                            "type": "state",
                                            "name": "reflection",
                                            "content": reflection,
                                        }
                                        reflect_requested = True
                                elif result_decision.action == ResilienceAction.TERMINATE:
                                    terminate_requested = True

                            handled_tool_calls = True
                            break  # tool_calls handled, continue outer loop

                    else:
                        # No tool_calls — pure text response.
                        # DeepSeek thinking mode may emit only reasoning_content with no
                        # content deltas; use reasoning as the visible text in that case.
                        if not full_text and reasoning_text:
                            full_text = reasoning_text
                            yield {"type": "token", "content": full_text}
                        logger.info("streaming complete, text_len=%d", len(full_text))
                        await self._persist_stream_assistant_output(
                            context=context,
                            content=full_text,
                            turn_index=next_turn_index + 1,
                            run_lease_handle=run_lease_handle,
                            workflow_continuation=workflow_continuation,
                        )
                        await self.transition(AgentState.FINISHED, context=context)
                        yield {"type": "done", "content": full_text, "data": {}}
                        return

                except (httpx.ReadTimeout, asyncio.TimeoutError) as exc:
                    logger.error(
                        "stream timeout round=%d/%d session=%s input=%r",
                        round_num + 1, max_rounds, context.session_id, user_input[:200],
                    )
                    yield {
                        "type": "error",
                        "content": f"Request timed out: {exc}",
                    }
                    return

                if terminate_requested:
                    break
                if reflect_requested or handled_tool_calls:
                    continue

            # Max rounds exceeded — force a final summary without tools
            logger.warning(
                "max tool rounds (%d) exceeded for session=%s, forcing final summary with %d messages",
                max_rounds, context.session_id, len(messages),
            )
            try:
                full_text = await self._generate_final_summary(
                    messages,
                    self._collect_plain_text_response,
                )
            except Exception:
                logger.exception("final summary failed")
                yield {
                    "type": "done",
                    "content": "I wasn't able to complete this task within the allowed rounds.",
                    "data": {},
                }
                return
            if full_text:
                await self._persist_stream_assistant_output(
                    context=context,
                    content=full_text,
                    turn_index=next_turn_index + 1,
                    run_lease_handle=run_lease_handle,
                    workflow_continuation=workflow_continuation,
                )
                yield {"type": "token", "content": full_text}
            yield {"type": "done", "content": full_text, "data": {}}
        finally:
            if self.state != AgentState.IDLE:
                try:
                    await self.transition(AgentState.IDLE, context=context)
                except BaseException:
                    logger.exception("failed to reset agent state after run termination")

    async def _next_turn_index(self, context: TenantContext) -> int:
        if context.session_id is None:
            raise ValueError("session_id is required for chat messages")
        recent = await self.memory.recent(context, limit=1, entry_type="chat_message")
        return (recent[0].turn_index + 1) if recent else 1

    async def _save_chat_msg(
        self,
        context: TenantContext,
        role: Literal["user", "assistant"],
        content: str,
        turn_index: int,
    ) -> None:
        if context.session_id is None:
            raise ValueError("session_id is required for chat messages")
        await self.memory.save(
            context,
            MemoryEntry(
                content=content,
                type="chat_message",
                role=role,
                session_id=context.session_id,
                turn_index=turn_index,
            ),
        )

    async def _persist_stream_assistant_output(
        self,
        *,
        context: TenantContext,
        content: str,
        turn_index: int,
        run_lease_handle: RunLeaseHandle | None,
        workflow_continuation,
    ) -> None:
        if workflow_continuation is not None and run_lease_handle is not None:
            await workflow_continuation.persist_assistant_output(
                context=context,
                run_lease_handle=run_lease_handle,
                content=content,
                turn_index=turn_index,
            )
            return
        await self._save_chat_msg(
            context,
            "assistant",
            content,
            turn_index,
        )
