from datetime import datetime, timezone
from dataclasses import dataclass, field

from multiclaw.context import ContextBuildReport, ContextBuildResult, estimate_tokens
from multiclaw.memory import MemoryEntry, MemoryProtocol
from multiclaw.tenancy.context import TenantContext


@dataclass
class ContextRequest:
    system_prompt: str
    user_input: str
    context: TenantContext
    context_window_limit: int
    skill_prompts: list[tuple[str, str]] = field(default_factory=list)


class ContextBuilder:
    def __init__(
        self,
        memory: MemoryProtocol,
        recent_turns: int,
        context_history_ratio: float,
        include_legacy_memory: bool = False,
        progressive_enabled: bool = False,
        response_reserve_tokens: int = 0,
        l1_ratio: float = 0.6,
    ) -> None:
        self.memory = memory
        self.recent_turns = recent_turns
        self.context_history_ratio = context_history_ratio
        self.include_legacy_memory = include_legacy_memory
        self.progressive_enabled = progressive_enabled
        self.response_reserve_tokens = response_reserve_tokens
        self.l1_ratio = l1_ratio

    async def build(self, request: ContextRequest) -> list[dict]:
        return (await self.build_with_report(request)).messages

    async def build_with_report(self, request: ContextRequest) -> ContextBuildResult:
        if not self.progressive_enabled:
            return await self._build_legacy_result(request)
        return await self._build_progressive_result(request)

    @staticmethod
    def _temporal_anchor_message() -> str:
        today = datetime.now(timezone.utc).date().isoformat()
        return (
            f"Current date: {today} (UTC). "
            "Use this date to resolve relative time references such as today, yesterday, "
            "last week, and last month. "
            "For recent or latest information, do not rewrite the user's timeframe to an "
            "older month or year unless the user explicitly asks for that older period."
        )

    async def _build_legacy_result(self, request: ContextRequest) -> ContextBuildResult:
        messages: list[dict] = [{"role": "system", "content": request.system_prompt}]
        anchor_message = self._temporal_anchor_message()
        messages.append({"role": "system", "content": anchor_message})

        for _, body in request.skill_prompts:
            messages.append({"role": "system", "content": body})

        recent_entries = await self._recent_chat_entries(request.context)
        for entry in recent_entries:
            messages.append({"role": entry.role, "content": entry.content})

        relevant_entries = await self._relevant_entries(request, recent_entries)
        relevant_text, _, dropped_l2 = self._fit_relevant_memory_by_chars(
            relevant_entries,
            request.context_window_limit,
        )
        if relevant_text:
            messages.append({"role": "system", "content": relevant_text})

        messages.append({"role": "user", "content": request.user_input})
        return ContextBuildResult(
            messages=messages,
            report=ContextBuildReport(
                limit_tokens=request.context_window_limit,
                reserved_response_tokens=0,
                used_tokens_by_level={
                    "L0": estimate_tokens(request.system_prompt)
                    + estimate_tokens(anchor_message)
                    + estimate_tokens(request.user_input),
                    "L1": sum(estimate_tokens(body) for _, body in request.skill_prompts)
                    + sum(estimate_tokens(entry.content) for entry in recent_entries),
                    "L2": estimate_tokens(relevant_text),
                },
                dropped_by_level={"L0": 0, "L1": 0, "L2": dropped_l2},
            ),
        )

    async def _build_progressive_result(self, request: ContextRequest) -> ContextBuildResult:
        messages: list[dict] = [{"role": "system", "content": request.system_prompt}]
        available_tokens = max(request.context_window_limit - self.response_reserve_tokens, 0)
        anchor_message = self._temporal_anchor_message()

        system_tokens = estimate_tokens(request.system_prompt)
        anchor_tokens = estimate_tokens(anchor_message)
        user_tokens = estimate_tokens(request.user_input)
        l0_tokens = system_tokens + user_tokens
        l0_dropped = 0

        if system_tokens + anchor_tokens + user_tokens <= available_tokens:
            messages.append({"role": "system", "content": anchor_message})
            l0_tokens += anchor_tokens
        else:
            l0_dropped = 1

        recent_entries = await self._recent_chat_entries(request.context)
        relevant_entries = await self._relevant_entries(request, recent_entries)

        remaining_after_l0 = max(available_tokens - l0_tokens, 0)
        l1_budget = int(remaining_after_l0 * self.l1_ratio)
        l1_messages, l1_tokens, l1_dropped = self._fit_l1_messages(
            request.skill_prompts,
            recent_entries,
            l1_budget,
        )
        messages.extend(l1_messages)

        remaining_after_l1 = max(available_tokens - l0_tokens - l1_tokens, 0)
        relevant_text, _, dropped_l2 = self._fit_relevant_memory_by_tokens(
            relevant_entries,
            remaining_after_l1,
        )
        if relevant_text:
            messages.append({"role": "system", "content": relevant_text})

        messages.append({"role": "user", "content": request.user_input})
        return ContextBuildResult(
            messages=messages,
            report=ContextBuildReport(
                limit_tokens=request.context_window_limit,
                reserved_response_tokens=self.response_reserve_tokens,
                used_tokens_by_level={
                    "L0": l0_tokens,
                    "L1": l1_tokens,
                    "L2": estimate_tokens(relevant_text),
                },
                dropped_by_level={
                    "L0": l0_dropped,
                    "L1": l1_dropped,
                    "L2": dropped_l2,
                },
            ),
        )

    async def _recent_chat_entries(self, context: TenantContext) -> list[MemoryEntry]:
        recent_entries = await self.memory.recent(
            context,
            limit=self.recent_turns * 4,
            entry_type="chat_message",
        )
        return [
            entry
            for entry in reversed(recent_entries)
            if entry.role in ("user", "assistant")
        ][: self.recent_turns * 2]

    async def _relevant_entries(
        self,
        request: ContextRequest,
        recent_entries: list[MemoryEntry],
    ) -> list[MemoryEntry]:
        recent_contents = {entry.content for entry in recent_entries}
        relevant_entries = await self.memory.query(
            request.context,
            request.user_input,
            top_k=5,
        )
        return [
            entry
            for entry in relevant_entries
            if entry.content not in recent_contents and entry.type != "chat_message"
        ]

    def _fit_l1_messages(
        self,
        skill_prompts: list[tuple[str, str]],
        recent_entries: list[MemoryEntry],
        budget_tokens: int,
    ) -> tuple[list[dict], int, int]:
        messages: list[dict] = []
        used_tokens = 0
        dropped = 0

        for _, body in skill_prompts:
            body_tokens = estimate_tokens(body)
            if used_tokens + body_tokens > budget_tokens:
                dropped += 1
                continue
            messages.append({"role": "system", "content": body})
            used_tokens += body_tokens

        selected_newest: list[MemoryEntry] = []
        newest_first = list(reversed(recent_entries))
        for index, entry in enumerate(newest_first):
            entry_tokens = estimate_tokens(entry.content)
            if used_tokens + entry_tokens > budget_tokens:
                dropped += len(newest_first) - index
                break
            selected_newest.append(entry)
            used_tokens += entry_tokens

        selected_recent_entries = reversed(selected_newest)
        messages.extend(
            {"role": entry.role, "content": entry.content}
            for entry in selected_recent_entries
        )
        return messages, used_tokens, dropped

    def _fit_relevant_memory_by_chars(
        self,
        entries: list[MemoryEntry],
        context_window_limit: int,
    ) -> tuple[str, int, int]:
        budget = int(context_window_limit * self.context_history_ratio)
        lines: list[str] = []
        used = 0
        for entry in entries:
            line = f"- [{entry.type}] {entry.content}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return "", 0, len(entries)
        return "Relevant memory:\n" + "\n".join(lines), len(lines), len(entries) - len(lines)

    def _fit_relevant_memory_by_tokens(
        self,
        entries: list[MemoryEntry],
        budget_tokens: int,
    ) -> tuple[str, int, int]:
        lines: list[str] = []
        for entry in entries:
            line = f"- [{entry.type}] {entry.content}"
            candidate_lines = lines + [line]
            candidate_text = "Relevant memory:\n" + "\n".join(candidate_lines)
            if estimate_tokens(candidate_text) > budget_tokens:
                break
            lines = candidate_lines
        if not lines:
            return "", 0, len(entries)
        return "Relevant memory:\n" + "\n".join(lines), len(lines), len(entries) - len(lines)
