from pathlib import Path

import pytest

from multiclaw.agent.compaction import (
    ContextBudgetExceeded,
    ContextCompactor,
    estimate_request_tokens,
)
from multiclaw.tools._common import PathPolicy


def exchange(index: int, size: int = 1000) -> list[dict]:
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call-{index}", "type": "function", "function": {
                "name": "read_file", "arguments": '{"file_path":"a.txt"}'}}
        ]},
        {"role": "tool", "tool_call_id": f"call-{index}", "content": f"result-{index}:" + "x" * size},
    ]


def assert_complete(messages: list[dict]) -> None:
    pending = set()
    for message in messages:
        if message.get("tool_calls"):
            assert not pending
            pending = {call["id"] for call in message["tool_calls"]}
        elif message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending
    assert not pending


async def test_compaction_retains_objective_steering_and_complete_exchanges() -> None:
    messages = [{"role": "system", "content": "Keep instructions"},
                {"role": "user", "content": "Original task objective"}]
    for index in range(6):
        messages.extend(exchange(index))
    messages.extend([{"role": "user", "content": "Latest steering"}, *exchange(6, 20)])
    compactor = ContextCompactor(450, 100)
    prepared = await compactor.prepare(messages)
    assert messages[0] in prepared
    assert messages[1] in prepared
    assert {"role": "user", "content": "Latest steering"} in prepared
    assert exchange(6, 20)[1] in prepared
    assert estimate_request_tokens(prepared) + 100 <= 450
    assert_complete(prepared)
    assert any("summary" in (message.get("content") or "").lower() for message in prepared)
    assert len(messages) == 17


async def test_schema_and_response_reserve_are_budgeted() -> None:
    tools = [{"type": "function", "function": {"name": "large", "description": "x" * 4000}}]
    compactor = ContextCompactor(400, 100)
    with pytest.raises(ContextBudgetExceeded, match="context budget"):
        await compactor.prepare([{"role": "user", "content": "objective"}], tools=tools)


async def test_irreducible_objective_and_instructions_raise_safe_error() -> None:
    with pytest.raises(ContextBudgetExceeded) as error:
        await ContextCompactor(50, 10).prepare([
            {"role": "system", "content": "secret marker" * 100},
            {"role": "user", "content": "objective"},
        ])
    assert "secret marker" not in str(error.value)


async def test_optional_summarizer_receives_complete_exchanges_and_is_bounded() -> None:
    calls = []

    async def summarize(messages: list[dict]) -> str:
        assert_complete(messages)
        calls.append(messages)
        return "Decided to use approach A. " * 1000

    messages = [{"role": "user", "content": "objective"}]
    for index in range(5):
        messages.extend(exchange(index))
    prepared = await ContextCompactor(350, 80, summarizer=summarize).prepare(messages)
    assert len(calls) == 1
    assert any("Decided to use approach A" in (m.get("content") or "") for m in prepared)
    assert estimate_request_tokens(prepared) + 80 <= 350
    assert_complete(prepared)


async def test_failed_summarizer_has_deterministic_fallback_and_repeated_compaction_is_bounded() -> None:
    async def fail(messages: list[dict]) -> str:
        raise RuntimeError("secret model error")

    initial = [{"role": "user", "content": "objective"}, *exchange(0), *exchange(1)]
    compactor = ContextCompactor(350, 80, summarizer=fail)
    prepared = await compactor.prepare(initial)
    assert prepared == await compactor.prepare(initial)
    for index in range(2, 12):
        prepared = await compactor.prepare([*prepared, *exchange(index)])
        assert estimate_request_tokens(prepared) + 80 <= 350
        assert_complete(prepared)
        assert sum("Earlier context summary:" in (m.get("content") or "") for m in prepared) == 1
        assert initial[0] in prepared
    assert "secret model error" not in str(prepared)


async def test_large_tool_output_has_safe_deterministic_readable_artifact(tmp_path: Path) -> None:
    content = "BEGIN " + "中" * 20000 + " END"
    messages = [{"role": "user", "content": "objective"}, *exchange(0, 0)]
    messages[-1]["content"] = content
    compactor = ContextCompactor(2000, 100, workspace_root=tmp_path)
    prepared = await compactor.prepare(messages)
    output = prepared[-1]["content"]
    artifacts = list((tmp_path / ".multiclaw/tool-results").glob("*.txt"))
    assert len(artifacts) == 1
    assert artifacts[0].read_text() == content
    assert PathPolicy(tmp_path).validate_read(artifacts[0]) is None
    assert str(artifacts[0].relative_to(tmp_path)) in output
    assert str(tmp_path) not in output
    assert "BEGIN " in output and " END" in output
    assert len(output) < 3000
    assert prepared == await compactor.prepare(messages)
    assert messages[-1]["content"] == content
    assert_complete(prepared)


@pytest.mark.parametrize("component", [".multiclaw", ".multiclaw/tool-results"])
async def test_tool_artifact_never_writes_through_symlink(tmp_path: Path, component: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    messages = [{"role": "user", "content": "objective"}, *exchange(0, 20000)]
    prepared = await ContextCompactor(1000, 100, workspace_root=workspace).prepare(messages)
    assert not list(outside.iterdir())
    assert str(outside) not in str(prepared)
    assert "artifact unavailable" in prepared[-1]["content"].lower()
    assert_complete(prepared)


async def test_orphan_tool_messages_fail_before_inference() -> None:
    with pytest.raises(ContextBudgetExceeded, match="tool exchange"):
        await ContextCompactor(1000, 100).prepare([
            {"role": "user", "content": "objective"},
            {"role": "tool", "tool_call_id": "missing", "content": "orphan"},
        ])


def test_invalid_budgets_rejected() -> None:
    with pytest.raises(ValueError):
        ContextCompactor(100, 100)
    with pytest.raises(ValueError):
        ContextCompactor(100, -1)


async def test_summary_retains_spilled_artifact_locator_when_exchange_no_longer_fits(tmp_path: Path) -> None:
    messages = [{"role": "system", "content": "instructions " * 24},
                {"role": "user", "content": "objective"}, *exchange(0, 20000)]
    compactor = ContextCompactor(220, 30, workspace_root=tmp_path)
    prepared = await compactor.prepare(messages)
    artifact = next((tmp_path / ".multiclaw/tool-results").glob("*.txt"))
    assert str(artifact.relative_to(tmp_path)) in str(prepared)
    assert estimate_request_tokens(prepared) + 30 <= 220
    assert_complete(prepared)


async def test_artifact_can_be_read_with_default_read_file_tool(tmp_path: Path) -> None:
    from multiclaw.tools.base import ToolStatus
    from multiclaw.tools.read_file import ReadFileToolBuilder

    messages = [{"role": "user", "content": "objective"}, *exchange(0, 20000)]
    await ContextCompactor(2000, 100, workspace_root=tmp_path).prepare(messages)
    artifact = next((tmp_path / ".multiclaw/tool-results").glob("*.txt"))
    builder = ReadFileToolBuilder(tmp_path)
    result = await builder.build(builder.validate({"file_path": str(artifact.relative_to(tmp_path))})).execute()
    assert result.status == ToolStatus.SUCCESS
    assert "result-0:" in result.content


async def test_parallel_tool_results_are_preserved_as_one_unit() -> None:
    group = exchange(0, 300)
    group[0]["tool_calls"].append({"id": "parallel", "type": "function", "function": {"name": "other", "arguments": "{}"}})
    group.append({"role": "tool", "tool_call_id": "parallel", "content": "second output"})
    messages = [{"role": "user", "content": "objective"}, *group, *exchange(1, 1000)]
    prepared = await ContextCompactor(250, 50).prepare(messages)
    assert_complete(prepared)
    assert estimate_request_tokens(prepared) + 50 <= 250


async def test_cancelled_summarizer_is_not_swallowed() -> None:
    import asyncio

    async def cancel(messages: list[dict]) -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await ContextCompactor(200, 50, summarizer=cancel).prepare([
            {"role": "user", "content": "objective"}, *exchange(0, 2000),
        ])
