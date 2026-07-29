from copy import deepcopy
from datetime import date, datetime, time

import pytest

from multiclaw.agent.resilience import (
    ResilienceAction,
    fingerprint_calls,
    fingerprint_results,
    ResilienceController,
)


def test_observe_calls_reflects_on_third_repeated_call_batch() -> None:
    controller = ResilienceController(repeat_limit=3, max_reflections=1)
    calls = [
        {
            "id": "call-1",
            "function": {
                "name": "search",
                "arguments": {
                    "query": "alpha",
                    "filters": {"country": "US", "limit": 5},
                },
            },
        }
    ]

    assert controller.observe_calls(calls).action == ResilienceAction.CONTINUE
    assert controller.observe_calls(calls).action == ResilienceAction.CONTINUE

    decision = controller.observe_calls(calls)

    assert decision.action == ResilienceAction.REFLECT
    assert "repeated tool call" in decision.reason


def test_observe_results_reflects_then_terminates_after_reflection_budget_used() -> None:
    controller = ResilienceController(repeat_limit=2, max_reflections=1)
    results = [
        {
            "tool": "search",
            "output": [{"title": "A"}, {"title": "B"}],
            "meta": {"cached": False},
        }
    ]

    assert controller.observe_results(results).action == ResilienceAction.CONTINUE

    first_repeat = controller.observe_results(results)
    assert first_repeat.action == ResilienceAction.REFLECT
    assert "repeated tool result" in first_repeat.reason

    controller.mark_reflection_used()

    second_repeat = controller.observe_results(results)
    assert second_repeat.action == ResilienceAction.TERMINATE
    assert "repeated tool result" in second_repeat.reason


def test_fingerprint_calls_ignores_top_level_id_and_dict_order() -> None:
    calls_a = [
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "search",
                "arguments": {"query": "alpha", "options": {"limit": 5, "lang": "en"}},
            },
        }
    ]
    calls_b = [
        {
            "type": "function",
            "id": "call-2",
            "function": {
                "arguments": {"options": {"lang": "en", "limit": 5}, "query": "alpha"},
                "name": "search",
            },
        }
    ]

    assert fingerprint_calls(calls_a) == fingerprint_calls(calls_b)


def test_different_fingerprint_resets_call_repeat_counter() -> None:
    controller = ResilienceController(repeat_limit=2, max_reflections=1)
    first = [{"id": "call-1", "function": {"name": "search", "arguments": {"query": "alpha"}}}]
    second = [{"id": "call-2", "function": {"name": "search", "arguments": {"query": "beta"}}}]

    assert controller.observe_calls(first).action == ResilienceAction.CONTINUE
    assert controller.observe_calls(first).action == ResilienceAction.REFLECT
    assert controller.observe_calls(second).action == ResilienceAction.CONTINUE
    assert controller.observe_calls(second).action == ResilienceAction.REFLECT


def test_zero_reflection_budget_terminates_immediately() -> None:
    controller = ResilienceController(repeat_limit=1, max_reflections=0)

    decision = controller.observe_calls([])

    assert decision.action == ResilienceAction.TERMINATE
    assert "repeated tool call" in decision.reason


def test_calls_and_results_track_repeats_independently() -> None:
    controller = ResilienceController(repeat_limit=2, max_reflections=1)
    calls = [{"id": "call-1", "function": {"name": "search", "arguments": {"query": "alpha"}}}]
    results = [{"output": "alpha"}]

    assert controller.observe_calls(calls).action == ResilienceAction.CONTINUE
    assert controller.observe_results(results).action == ResilienceAction.CONTINUE
    assert controller.observe_calls(calls).action == ResilienceAction.REFLECT
    assert controller.observe_results(results).action == ResilienceAction.REFLECT


def test_fingerprints_handle_empty_lists_non_json_values_and_preserve_inputs() -> None:
    non_json = datetime(2024, 1, 2, 3, 4, 5)
    calls = [
        {
            "id": "call-1",
            "function": {
                "name": "echo",
                "arguments": {
                    "value": non_json,
                    "day": date(2024, 1, 2),
                    "clock": time(3, 4, 5),
                    "nested": {"items": []},
                },
            },
        }
    ]
    results = [
        {
            "tool": "echo",
            "output": {
                "value": non_json,
                "day": date(2024, 1, 2),
                "clock": time(3, 4, 5),
                "items": [],
            },
        }
    ]
    calls_before = deepcopy(calls)
    results_before = deepcopy(results)

    empty_calls_first = fingerprint_calls([])
    empty_calls_second = fingerprint_calls([])
    empty_results_first = fingerprint_results([])
    empty_results_second = fingerprint_results([])

    assert empty_calls_first == empty_calls_second
    assert empty_results_first == empty_results_second
    assert fingerprint_calls(calls) == fingerprint_calls(calls_before)
    assert fingerprint_results(results) == fingerprint_results(results_before)
    assert calls == calls_before
    assert results == results_before


def test_fingerprints_reject_unknown_objects_instead_of_stringifying_them() -> None:
    class UnknownObject:
        pass

    with pytest.raises(TypeError):
        fingerprint_calls([{"function": {"arguments": {"value": UnknownObject()}}}])

    with pytest.raises(TypeError):
        fingerprint_results([{"output": UnknownObject()}])


def test_fingerprints_reject_non_string_dict_keys() -> None:
    with pytest.raises(TypeError):
        fingerprint_calls([{"function": {"arguments": {1: "alpha"}}}])

    with pytest.raises(TypeError):
        fingerprint_results([{1: "alpha"}])


def test_invalid_controller_arguments_raise_value_error() -> None:
    with pytest.raises(ValueError):
        ResilienceController(repeat_limit=0, max_reflections=1)

    with pytest.raises(ValueError):
        ResilienceController(repeat_limit=1, max_reflections=-1)


def test_mark_reflection_used_saturates_at_maximum() -> None:
    controller = ResilienceController(repeat_limit=3, max_reflections=2)

    controller.mark_reflection_used()
    controller.mark_reflection_used()
    controller.mark_reflection_used()

    assert controller.reflections_used == 2
