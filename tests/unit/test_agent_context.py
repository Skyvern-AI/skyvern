"""Tests for generic agent context message and tool-result helpers."""

from __future__ import annotations

import json
from typing import Any

from skyvern.forge.sdk.agents.context import (
    compact_agent_messages_for_llm,
    pair_tool_calls_with_outputs,
    sanitize_agent_tool_result_for_llm,
)


def _call(call_id: str, name: str = "inspect_page") -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _output(call_id: str, text: str = "ok") -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": text}


def test_pair_tool_calls_reseats_a_result_that_drifted_past_an_assistant_turn() -> None:
    """The call stays put and its result lands after a later message."""
    items = [
        {"role": "user", "content": "goal"},
        _call("a"),
        _call("b"),
        _output("b"),
        {"type": "message", "content": "thinking out loud"},
        _output("a"),
    ]

    repaired = pair_tool_calls_with_outputs(items)

    assert [item.get("type") or item.get("role") for item in repaired] == [
        "user",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert repaired[1]["call_id"] == "a"
    assert repaired[2]["call_id"] == "a"


def test_pair_tool_calls_keeps_every_result_it_moves() -> None:
    items = [_call("a"), {"type": "message", "content": "m"}, _output("a", "browser work worth keeping")]

    repaired = pair_tool_calls_with_outputs(items)

    outputs = [item for item in repaired if item.get("type") == "function_call_output"]
    assert [item["output"] for item in outputs] == ["browser work worth keeping"]


def test_pair_tool_calls_leaves_lone_halves_where_they_are() -> None:
    """Ordering repair never prunes: a partial slice of history is a valid input here."""
    items = [
        _call("a"),
        {"type": "message", "content": "m"},
        _output("a"),
        _output("orphan"),
        _call("never_answered"),
    ]

    repaired = pair_tool_calls_with_outputs(items)

    assert [item.get("call_id") or item.get("type") for item in repaired] == [
        "a",
        "a",
        "message",
        "orphan",
        "never_answered",
    ]


def test_pair_tool_calls_leaves_a_valid_history_untouched() -> None:
    items = [
        {"role": "user", "content": "goal"},
        _call("a"),
        _output("a"),
        {"type": "message", "content": "done"},
    ]

    assert pair_tool_calls_with_outputs(list(items)) == items


def test_pair_tool_calls_reports_what_it_repaired() -> None:
    seen: list[tuple[int, int]] = []
    items = [_call("a"), {"type": "message", "content": "m"}, _output("a"), _output("orphan")]

    pair_tool_calls_with_outputs(items, on_repair=lambda *args: seen.append(args))

    assert seen == [(1, 0)]


def test_pair_tool_calls_stays_quiet_on_a_parallel_batch_the_provider_accepts() -> None:
    """Calls and results interleaving inside one batch is valid; only a turn between them is drift."""
    seen: list[tuple[int, int]] = []
    items = [_call("a"), _call("b"), _output("a"), _output("b")]

    repaired = pair_tool_calls_with_outputs(items, on_repair=lambda *args: seen.append(args))

    assert [item["call_id"] for item in repaired] == ["a", "a", "b", "b"]
    assert seen == []


def test_compact_agent_messages_summarizes_old_tool_items_and_caps_recent_outputs() -> None:
    def summarize_output(output: str) -> str:
        return json.dumps({"_summarized": True, "length": len(output)})

    def summarize_arguments(arguments: str) -> str:
        return json.dumps({"_summarized": True, "length": len(arguments)})

    older_output = "x" * 100
    recent_output = "y" * 100
    older_args = json.dumps({"workflow_yaml": "z" * 100})
    recent_args = json.dumps({"workflow_yaml": "a" * 100})
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "build a workflow"},
        {"type": "function_call_output", "call_id": "out-old-1", "output": older_output},
        {"role": "tool", "tool_call_id": "out-old-2", "content": older_output},
        {"type": "function_call_output", "call_id": "out-new-1", "output": recent_output},
        {"type": "function_call_output", "call_id": "out-new-2", "output": recent_output},
        {"type": "function_call", "call_id": "call-old", "arguments": older_args},
        {"type": "function_call", "call_id": "call-mid", "arguments": recent_args},
        {"type": "function_call", "call_id": "call-new", "arguments": recent_args},
    ]

    compacted = compact_agent_messages_for_llm(
        messages,
        keep_recent_tool_outputs=2,
        max_recent_tool_output_chars=12,
        summarize_tool_output=summarize_output,
        summarize_tool_arguments=summarize_arguments,
    )

    outputs = [item for item in compacted if item.get("type") == "function_call_output" or item.get("role") == "tool"]
    assert json.loads(outputs[0]["output"]) == {"_summarized": True, "length": 100}
    assert json.loads(outputs[1]["content"]) == {"_summarized": True, "length": 100}
    assert outputs[2]["output"] == "y" * 12 + "\n... [truncated]"
    assert outputs[3]["output"] == "y" * 12 + "\n... [truncated]"
    calls = [item for item in compacted if item.get("type") == "function_call"]
    assert json.loads(calls[0]["arguments"]) == {"_summarized": True, "length": len(older_args)}
    assert calls[1]["arguments"] == recent_args
    assert calls[2]["arguments"] == recent_args
    assert messages[1]["output"] == older_output


def test_compact_agent_messages_replaces_old_synthetic_messages_when_over_budget() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "goal"},
        {"role": "user", "content": "[screenshot] old"},
        {"role": "assistant", "content": "middle"},
        {"role": "user", "content": "[screenshot] recent"},
    ]

    compacted = compact_agent_messages_for_llm(
        messages,
        keep_recent_tool_outputs=3,
        max_recent_tool_output_chars=2000,
        token_budget=1,
        estimate_tokens=lambda items: sum(len(str(item)) for item in items),
        is_synthetic_message=lambda item: item.get("content", "").startswith("[screenshot]"),
        synthetic_message_placeholder={"role": "user", "content": "[screenshot omitted]"},
    )

    assert compacted[1] == {"role": "user", "content": "[screenshot omitted]"}
    assert compacted[3] == messages[3]
    assert messages[1]["content"] == "[screenshot] old"


def test_sanitize_agent_tool_result_for_llm_shapes_configured_fields_without_mutating_original() -> None:
    raw = {
        "ok": True,
        "action": "inspect_page",
        "data": {
            "content": "a" * 30,
            "sdk_equivalent": "await page.content()",
            "screenshot_base64": "iVBORw0KGgo" + "A" * 200,
            "nested": [{"html": "b" * 30}],
        },
    }

    sanitized = sanitize_agent_tool_result_for_llm(
        tool_name="inspect_page",
        result=raw,
        drop_top_level_keys={"action"},
        drop_data_keys={"sdk_equivalent"},
        replacement_fields={"screenshot_base64": "[image omitted]"},
        large_fields={"content", "html"},
        max_chars=10,
    )

    assert "action" not in sanitized
    assert "sdk_equivalent" not in sanitized["data"]
    assert sanitized["data"]["screenshot_base64"] == "[image omitted]"
    assert sanitized["data"]["content"] == "a" * 10 + "\n... [truncated]"
    assert sanitized["data"]["nested"][0]["html"] == "b" * 10 + "\n... [truncated]"
    assert raw["action"] == "inspect_page"
    assert raw["data"]["content"] == "a" * 30
    assert raw["data"]["nested"][0]["html"] == "b" * 30
