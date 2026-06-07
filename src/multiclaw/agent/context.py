from datetime import datetime, timezone
from dataclasses import dataclass, field

from multiclaw.memory import MemoryEntry, MemoryProtocol


@dataclass
class ContextRequest:
    system_prompt: str
    user_input: str
    session_id: str
    context_window_limit: int
    skill_prompts: list[tuple[str, str]] = field(default_factory=list)


class ContextBuilder:
    def __init__(
        self,
        memory: MemoryProtocol,
        recent_turns: int,
        context_history_ratio: float,
        include_legacy_memory: bool = False,
    ) -> None:
        self.memory = memory
        self.recent_turns = recent_turns
        self.context_history_ratio = context_history_ratio
        self.include_legacy_memory = include_legacy_memory

    async def build(self, request: ContextRequest) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": request.system_prompt}]
        messages.append({"role": "system", "content": self._temporal_anchor_message()})

        # Inject active skill prompts as independent system messages
        for name, body in request.skill_prompts:
            messages.append({"role": "system", "content": body})

        recent_entries = await self.memory.recent(
            limit=self.recent_turns * 4,
            entry_type="chat_message",
            session_id=request.session_id,
        )
        # Only keep user and assistant messages — tool messages are
        # intermediate artifacts that bloat context and confuse the LLM.
        recent_entries = [
            e for e in reversed(recent_entries)
            if e.role in ("user", "assistant")
        ][: self.recent_turns * 2]
        for entry in recent_entries:
            messages.append({"role": entry.role, "content": entry.content})

        recent_contents = {entry.content for entry in recent_entries}
        relevant_entries = await self.memory.query(
            request.user_input,
            top_k=5,
            session_id=request.session_id,
            include_legacy=self.include_legacy_memory,
        )
        relevant_entries = [
            entry
            for entry in relevant_entries
            if entry.content not in recent_contents and entry.type != "chat_message"
        ]
        relevant_text = self._fit_relevant_memory(
            relevant_entries,
            request.context_window_limit,
        )
        if relevant_text:
            messages.append({"role": "system", "content": relevant_text})

        messages.append({"role": "user", "content": request.user_input})
        return messages

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

    def _fit_relevant_memory(
        self,
        entries: list[MemoryEntry],
        context_window_limit: int,
    ) -> str:
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
            return ""
        return "Relevant memory:\n" + "\n".join(lines)
