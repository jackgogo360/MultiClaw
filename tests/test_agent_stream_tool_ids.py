import pytest

from multiclaw.agent.multiclaw import MultiClawAgent


class _DummySkillManager:
    def process_message(self, _message: str):
        return []

    def get_active_skill_prompts(self):
        return []

    def invoke(self, _skill_name: str, _args: str):
        return None


class _DummyContextBuilder:
    async def build(self, _request):
        return [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]


class _DummyRegistry:
    def to_openai_schemas(self):
        return []


class _DummyMemory:
    async def save(self, _entry):
        return None


class _DummyRouter:
    async def stream_completion(self, **_kwargs):
        yield {
            "type": "tool_calls",
            "calls": [
                {
                    "id": "call_123",
                    "name": "web_search",
                    "arguments": {"query": "hello"},
                }
            ],
            "reasoning_content": "",
        }


class _DummyDsmlRetryRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_completion(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_calls",
                "calls": [
                    {
                        "id": "call_123",
                        "name": "web_search",
                        "arguments": {"query": "hello"},
                    }
                ],
                "reasoning_content": "",
            }
            return

        if self.calls == 2:
            yield {
                "type": "token",
                "content": "<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name=\"web_search\"></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>",
            }
            return

        yield {"type": "token", "content": "Final summary"}


class _DummyObservation:
    def __init__(self, content: str):
        self.content = content


@pytest.mark.asyncio
async def test_handle_message_stream_preserves_tool_call_ids(monkeypatch):
    agent = MultiClawAgent.__new__(MultiClawAgent)
    agent.skill_manager = _DummySkillManager()
    agent.context_builder = _DummyContextBuilder()
    agent.registry = _DummyRegistry()
    agent.memory = _DummyMemory()
    agent.router = _DummyRouter()
    agent.settings = type(
        "Settings",
        (),
        {
            "agent": type("AgentSettings", (), {"system_prompt": "sys", "max_tool_rounds": 1})(),
            "memory": type("MemorySettings", (), {"context_window_limit": 1000})(),
            "llm": type("LLMSettings", (), {"default_model": "x"})(),
        },
    )()
    agent.transition = lambda *_args, **_kwargs: _async_none()
    agent._save_chat_msg = lambda *_args, **_kwargs: _async_none()
    agent.remember = lambda *_args, **_kwargs: _async_none()
    agent.act = lambda *_args, **_kwargs: _async_obs("search results")

    events = []
    async for event in agent.handle_message_stream("hello", session_id="s1"):
        events.append(event)

    tool_call = next(event for event in events if event["type"] == "tool_call")
    tool_result = next(event for event in events if event["type"] == "tool_result")

    assert tool_call["call_id"] == "call_123"
    assert tool_result["call_id"] == "call_123"


@pytest.mark.asyncio
async def test_handle_message_stream_retries_when_final_summary_contains_dsml():
    agent = MultiClawAgent.__new__(MultiClawAgent)
    router = _DummyDsmlRetryRouter()
    agent.skill_manager = _DummySkillManager()
    agent.context_builder = _DummyContextBuilder()
    agent.registry = _DummyRegistry()
    agent.memory = _DummyMemory()
    agent.router = router
    agent.settings = type(
        "Settings",
        (),
        {
            "agent": type("AgentSettings", (), {"system_prompt": "sys", "max_tool_rounds": 1})(),
            "memory": type("MemorySettings", (), {"context_window_limit": 1000})(),
            "llm": type("LLMSettings", (), {"default_model": "x"})(),
        },
    )()
    agent.transition = lambda *_args, **_kwargs: _async_none()
    agent._save_chat_msg = lambda *_args, **_kwargs: _async_none()
    agent.remember = lambda *_args, **_kwargs: _async_none()
    agent.act = lambda *_args, **_kwargs: _async_obs("search results")

    events = []
    async for event in agent.handle_message_stream("hello", session_id="s1"):
        events.append(event)

    done = next(event for event in events if event["type"] == "done")
    tokens = [event["content"] for event in events if event["type"] == "token"]

    assert router.calls == 3
    assert done["content"] == "Final summary"
    assert "".join(tokens) == "Final summary"


async def _async_none():
    return None


async def _async_obs(content: str):
    return _DummyObservation(content)
