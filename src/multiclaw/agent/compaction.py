"""Prepare bounded inference context without splitting tool exchanges."""

from collections.abc import Awaitable, Callable
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from multiclaw.context import estimate_tokens

Summarizer = Callable[[list[dict]], Awaitable[str]]
SUMMARY_PREFIX = "Earlier context summary:\n"


class ContextBudgetExceeded(ValueError):
    """The request cannot safely fit in the configured inference budget."""


def estimate_request_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """Include message envelopes, tool arguments, schemas, and protocol overhead.

    This is a deterministic estimate, not a provider-specific tokenizer. Escaping
    non-ASCII text avoids the severe undercount of character-only estimates.
    """
    payload = json.dumps(messages, ensure_ascii=True, separators=(",", ":"))
    tokens = estimate_tokens(payload) + 8 * len(messages) + 3
    if tools:
        tokens += estimate_tokens(json.dumps(tools, ensure_ascii=True, separators=(",", ":"))) + 16
    return tokens


class ContextCompactor:
    def __init__(
        self,
        context_window_limit: int,
        response_reserve_tokens: int,
        workspace_root: str | Path | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        if context_window_limit <= 0 or not 0 <= response_reserve_tokens < context_window_limit:
            raise ValueError("Response reserve must be nonnegative and smaller than the context window")
        self.context_window_limit = context_window_limit
        self.response_reserve_tokens = response_reserve_tokens
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        self.summarizer = summarizer

    async def prepare(self, messages: list[dict], *, tools: list[dict] | None = None) -> list[dict]:
        """Return an independent, budgeted request; never mutate durable history."""
        prepared = deepcopy(messages)
        groups = self._groups(prepared)
        budget = self.context_window_limit - self.response_reserve_tokens
        self._bound_tool_results(prepared, budget)
        if estimate_request_tokens(prepared, tools) <= budget:
            return prepared

        users = [index for index, m in enumerate(prepared) if m.get("role") == "user"]
        pinned = {index for index, m in enumerate(prepared) if m.get("role") in {"system", "developer"}}
        if users:
            pinned.update((users[0], users[-1]))
        kept = set(pinned)
        minimum = self._select(prepared, kept)
        if estimate_request_tokens(minimum, tools) > budget:
            raise ContextBudgetExceeded("Required instructions, objective, and tool schemas exceed the context budget")

        # Reserve a bounded summary first, then retain the newest whole exchanges.
        free = budget - estimate_request_tokens(minimum, tools)
        summary_reserve = min(256, max(32, free // 3)) if free >= 32 else free
        for group in reversed(groups):
            if any(index in pinned for index in group):
                kept.update(group)
                continue
            if any(self._is_summary(prepared[index]) for index in group):
                continue
            candidate = kept.union(group)
            if estimate_request_tokens(self._select(prepared, candidate), tools) <= budget - summary_reserve:
                kept = candidate
            else:
                # Summarize the older prefix rather than retain disconnected history.
                break

        removed = [m for index, m in enumerate(prepared) if index not in kept]
        retained = self._select(prepared, kept)
        if not removed:
            return retained
        summary = ""
        if self.summarizer is not None:
            try:
                summary = await self.summarizer(deepcopy(removed))
            except Exception:
                # Cancellation remains observable; model failures fall back locally.
                summary = ""
        if not isinstance(summary, str) or not summary.strip():
            summary = self._fallback_summary(removed)
        artifacts = list(dict.fromkeys(
            path
            for message in reversed(removed)
            for path in re.findall(r"\.multiclaw/tool-results/[a-f0-9]{64}\.txt", str(message.get("content") or ""))
        ))
        if artifacts:
            # Locators are more useful than an excerpt when an exchange is
            # summarized; keep them ahead of model prose or fallback snippets.
            summary = "Artifacts: " + ", ".join(artifacts) + "\n" + summary
        return self._insert_bounded_summary(retained, summary, tools, budget)

    @staticmethod
    def _select(messages: list[dict], indices: set[int]) -> list[dict]:
        return [message for index, message in enumerate(messages) if index in indices]

    @staticmethod
    def _is_summary(message: dict) -> bool:
        return message.get("role") == "assistant" and isinstance(message.get("content"), str) and message["content"].startswith(SUMMARY_PREFIX)

    @staticmethod
    def _groups(messages: list[dict]) -> list[list[int]]:
        """Validate and group each assistant call with all of its tool results."""
        groups: list[list[int]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.get("role") == "tool":
                raise ContextBudgetExceeded("Cannot prepare an incomplete tool exchange")
            calls = message.get("tool_calls") or []
            group = [index]
            index += 1
            if calls:
                pending = {call.get("id") for call in calls}
                if message.get("role") != "assistant" or None in pending or len(pending) != len(calls):
                    raise ContextBudgetExceeded("Cannot prepare an invalid tool exchange")
                while index < len(messages) and messages[index].get("role") == "tool":
                    call_id = messages[index].get("tool_call_id")
                    if call_id not in pending:
                        raise ContextBudgetExceeded("Cannot prepare an invalid tool exchange")
                    pending.remove(call_id)
                    group.append(index)
                    index += 1
                if pending:
                    raise ContextBudgetExceeded("Cannot prepare an incomplete tool exchange")
            groups.append(group)
        return groups

    @staticmethod
    def _fallback_summary(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            content = message.get("content") or ""
            if ContextCompactor._is_summary(message):
                content = content[len(SUMMARY_PREFIX):]
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            calls = message.get("tool_calls") or []
            if calls:
                details = "; ".join(
                    f"{call.get('function', {}).get('name', 'tool')}({str(call.get('function', {}).get('arguments', ''))[:100]})"
                    for call in calls
                )
                lines.append(f"assistant tools: {details[:240]}")
            if content:
                lines.append(f"{message.get('role', 'message')}: {content[:240]}")
        return "\n".join(lines) or "Older complete exchanges omitted."

    @staticmethod
    def _insert_bounded_summary(
        retained: list[dict], summary: str, tools: list[dict] | None, budget: int,
    ) -> list[dict]:
        # Place after the initial objective (and instructions), before retained work.
        insertion = next((index + 1 for index, m in enumerate(retained) if m.get("role") == "user"), 0)
        summary = summary.replace(SUMMARY_PREFIX, "")[:4096]
        low, high = 0, len(summary)
        result = retained
        while low <= high:
            length = (low + high) // 2
            candidate = [*retained[:insertion], {"role": "assistant", "content": SUMMARY_PREFIX + summary[:length]}, *retained[insertion:]]
            if estimate_request_tokens(candidate, tools) <= budget:
                result = candidate
                low = length + 1
            else:
                high = length - 1
        return result

    def _bound_tool_results(self, messages: list[dict], budget: int) -> None:
        if self.workspace_root is None:
            return
        threshold = max(1024, min(8192, budget * 2))
        for message in messages:
            content = message.get("content")
            if message.get("role") != "tool" or not isinstance(content, str) or len(content) <= threshold:
                continue
            artifact = self._store_artifact(content)
            marker = f"Full tool result: {artifact} (use read_file)." if artifact else "Full tool result artifact unavailable."
            # Leave room for envelopes and escaped multi-byte text as well as
            # the call that owns this result.
            preview = min(2048, max(128, budget // 3))
            message["content"] = f"{content[:preview // 2]}\n\n[Tool result truncated; {marker}]\n\n{content[-preview // 2:]}"

    def _store_artifact(self, content: str) -> str | None:
        """Use directory descriptors and no-follow opens, including artifact parents."""
        assert self.workspace_root is not None
        descriptors: list[int] = []
        temporary: str | None = None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            directory = os.open(self.workspace_root, flags)
            descriptors.append(directory)
            for name in (".multiclaw", "tool-results"):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                directory = os.open(name, flags, dir_fd=directory)
                descriptors.append(directory)
            data = content.encode("utf-8")
            filename = hashlib.sha256(data).hexdigest() + ".txt"
            temporary = f".{uuid.uuid4().hex}.tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            return f".multiclaw/tool-results/{filename}"
        except (OSError, UnicodeError):
            return None
        finally:
            if temporary is not None and descriptors:
                try:
                    os.unlink(temporary, dir_fd=descriptors[-1])
                except OSError:
                    pass
            for descriptor in reversed(descriptors):
                os.close(descriptor)
