"""Tests for retained context compaction and safety enforcement helpers."""

from __future__ import annotations

import json
from typing import Any

import pytest
from agents import GuardrailFunctionOutput, OutputGuardrail, function_tool, output_guardrail
from agents.exceptions import ModelBehaviorError, OutputGuardrailTripwireTriggered
from agents.items import TResponseInputItem

from skyvern.forge.sdk.copilot.agent import _build_copilot_output_guardrails
from skyvern.forge.sdk.copilot.blocker_signal import clear_active_run_evidence_on_workflow_edit
from skyvern.forge.sdk.copilot.context import adopt_model_authored_context
from skyvern.forge.sdk.copilot.enforcement import (
    SCREENSHOT_PLACEHOLDER,
    _is_context_window_error,
    _prune_input_list,
    _recover_from_context_overflow,
    _strip_input_images,
    enforcement_decision,
)
from skyvern.forge.sdk.copilot.llm_errors import CopilotEmptyCompletionError
from skyvern.forge.sdk.copilot.output_utils import extract_final_text, parse_final_response
from skyvern.forge.sdk.copilot.tools import reply_tool
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType
from tests.unit.conftest import make_copilot_context as _fresh_context
from tests.unit.copilot_test_helpers import (
    REPLY_ARGUMENTS,
    ScriptedModel,
    run_production_loop,
    run_scripted_turn,
    scripted_call,
    scripted_text,
    tool_frame_types,
    tool_outputs_for,
)

# ---------------------------------------------------------------------------
# A — fresh CopilotContext
# ---------------------------------------------------------------------------


def test_enforcement_decision_on_fresh_agent_context_returns_none() -> None:
    ctx = _fresh_context()
    assert enforcement_decision(ctx) is None


# ---------------------------------------------------------------------------
# B1 — tool-call argument compaction
# ---------------------------------------------------------------------------


def test_prune_input_list_summarizes_old_tool_call_arguments() -> None:
    huge_yaml = "workflow:\n" + "  - block: x\n" * 2000  # ~18 KB
    old_call = {
        "type": "function_call",
        "name": "update_workflow",
        "arguments": json.dumps({"workflow_yaml": huge_yaml, "description": "initial"}),
    }
    # Four recent tool calls so the old one is outside the KEEP_RECENT window.
    recent_calls = [
        {
            "type": "function_call",
            "name": "run_blocks_and_collect_debug",
            "arguments": json.dumps({"block_labels": [f"b{i}"]}),
        }
        for i in range(4)
    ]
    items = [old_call] + recent_calls

    pruned = _prune_input_list(items)

    # Oldest call's arguments should be compacted; recent ones untouched.
    pruned_args = json.loads(pruned[0]["arguments"])
    assert "workflow_yaml" in pruned_args
    assert isinstance(pruned_args["workflow_yaml"], str)
    assert "truncated" in pruned_args["workflow_yaml"]
    for item in pruned[-3:]:
        assert "truncated" not in item["arguments"]


def test_prune_input_list_preserves_small_arguments() -> None:
    small_call = {
        "type": "function_call",
        "name": "navigate_browser",
        "arguments": json.dumps({"url": "https://example.com"}),
    }
    pruned = _prune_input_list([small_call])
    assert pruned[0]["arguments"] == small_call["arguments"]


# ---------------------------------------------------------------------------
# C — suspicious-success nudge re-fires if agent ignores it
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# L — overflow recovery strips images
# ---------------------------------------------------------------------------


def test_strip_input_images_replaces_image_parts_with_placeholder() -> None:
    payload: list[Any] = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "see this:"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA" * 1000},
            ],
        }
    ]
    stripped, did_strip = _strip_input_images(payload)
    assert did_strip is True
    assert isinstance(stripped, list)
    content = stripped[0]["content"]
    assert content[0] == {"type": "input_text", "text": "see this:"}
    assert content[1] == {"type": "input_text", "text": SCREENSHOT_PLACEHOLDER}


def test_strip_input_images_no_images_reports_false() -> None:
    payload: list[Any] = [{"role": "user", "content": [{"type": "input_text", "text": "no images here"}]}]
    stripped, did_strip = _strip_input_images(payload)
    assert did_strip is False
    assert stripped == payload


@pytest.mark.asyncio
async def test_recover_from_context_overflow_strips_images_without_session() -> None:
    current_input: list[Any] = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA" * 1000},
            ],
        }
    ]
    recovered, stripped = await _recover_from_context_overflow(session=None, current_input=current_input)
    assert stripped is True
    assert isinstance(recovered, list)
    assert recovered[0]["content"][0]["type"] == "input_text"


class _FakeSession:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self.cleared = False

    async def get_items(self) -> list[Any]:
        return list(self.items)

    async def clear_session(self) -> None:
        self.cleared = True
        self.items = []

    async def add_items(self, items: list[Any]) -> None:
        self.items.extend(items)


@pytest.mark.asyncio
async def test_recover_from_context_overflow_with_session_strips_current_input() -> None:
    # Session pruning covers history; current_input still needs its images
    # stripped — that's the case the old code missed.
    session = _FakeSession()
    session.items = [{"role": "user", "content": "old"}]
    current_input: list[Any] = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA" * 1000},
            ],
        }
    ]
    recovered, stripped = await _recover_from_context_overflow(session=session, current_input=current_input)
    assert stripped is True
    assert isinstance(recovered, list)
    assert recovered[0]["content"][0]["type"] == "input_text"
    assert session.cleared is True


# ---------------------------------------------------------------------------
# F — _is_context_window_error is narrow enough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg, expected",
    [
        ("context_length_exceeded: 250000 > 128000", True),
        ("This model's maximum context length is 128000 tokens", True),
        ("Please reduce the length of the messages", True),
        ("context window exceeded", True),
        ("max_tokens_per_request quota hit", False),
        ("rate_limit_exceeded", False),
        ("Some unrelated server error", False),
    ],
)
def test_is_context_window_error_matches_only_overflow_variants(msg: str, expected: bool) -> None:
    assert _is_context_window_error(Exception(msg)) is expected


def test_workflow_edit_clears_recorded_persisted_run_latch() -> None:
    ctx = _fresh_context()
    ctx.last_run_blocks_workflow_run_id = "wr_1"
    ctx.recorded_persisted_block_run_workflow_run_id = "wr_1"

    clear_active_run_evidence_on_workflow_edit(ctx)

    assert ctx.last_run_blocks_workflow_run_id is None
    assert ctx.recorded_persisted_block_run_workflow_run_id is None


@pytest.mark.asyncio
async def test_unknown_tool_name_gets_an_error_result_and_the_turn_continues() -> None:
    model = ScriptedModel(
        [[scripted_call("REPLY", {}, "call_made_up")], [scripted_call("reply", REPLY_ARGUMENTS, "call_reply")]]
    )
    result, ctx, stream = await run_scripted_turn(model)

    assert len(model.inputs) == 2
    (unknown_result,) = tool_outputs_for(model.inputs[1], "call_made_up")
    assert json.loads(unknown_result)["ok"] is False
    assert "REPLY" in json.loads(unknown_result)["error"]
    assert parse_final_response(extract_final_text(result))["user_response"] == REPLY_ARGUMENTS["user_response"]
    assert ctx.pending_stream_tool_call_ids == set()
    assert ctx.in_flight_stream_tool_call is None
    calls = [f for f in stream.sent if f.type == WorkflowCopilotStreamMessageType.TOOL_CALL]
    results = [f for f in stream.sent if f.type == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [(f.tool_name, f.tool_call_id) for f in calls] == [("REPLY", "call_made_up")]
    assert [(f.tool_call_id, f.success) for f in results] == [("call_made_up", False)]


@pytest.mark.asyncio
async def test_reply_call_ends_the_turn_as_the_assistant_message() -> None:
    model = ScriptedModel([[scripted_call("reply", REPLY_ARGUMENTS)]])
    result, ctx, stream = await run_scripted_turn(model)

    assert len(model.inputs) == 1
    parsed = parse_final_response(extract_final_text(result))
    assert parsed["type"] == "REPLY"
    assert parsed["user_response"] == REPLY_ARGUMENTS["user_response"]
    assert adopt_model_authored_context(None, parsed["global_llm_context"]).user_goal == "loop over URLs"
    assert tool_frame_types(stream) == []
    assert ctx.narrator_state is not None
    assert ctx.narrator_state.design_activity == []
    assert ctx.tool_activity == []
    assert ctx.tool_calls_this_turn == 0


@pytest.mark.asyncio
async def test_reply_withheld_by_the_output_guardrail_is_not_saved_to_the_session() -> None:
    checked: list[Any] = []

    @output_guardrail
    async def withhold(_ctx: Any, _agent: Any, output: Any) -> GuardrailFunctionOutput:
        checked.append(output)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    saved_items: list[TResponseInputItem] = []
    with pytest.raises(OutputGuardrailTripwireTriggered):
        await run_scripted_turn(
            ScriptedModel([[scripted_call("reply", REPLY_ARGUMENTS)]]),
            output_guardrails=[withhold],
            saved_items=saved_items,
        )

    assert [parse_final_response(output)["user_response"] for output in checked] == [REPLY_ARGUMENTS["user_response"]]
    assert saved_items
    assert "for_loop" not in json.dumps(saved_items)


@pytest.mark.asyncio
async def test_reply_with_invalid_arguments_continues_the_turn() -> None:
    model = ScriptedModel([[scripted_call("reply", {}, "call_bad")], [scripted_text("Use a `for_loop` block.")]])
    result, _ctx, stream = await run_scripted_turn(model)

    assert len(model.inputs) == 2
    (failure,) = tool_outputs_for(model.inputs[1], "call_bad")
    assert json.loads(failure)["ok"] is False
    assert parse_final_response(extract_final_text(result)) == {
        "type": "REPLY",
        "user_response": "Use a `for_loop` block.",
    }
    assert tool_frame_types(stream) == []


@pytest.mark.asyncio
async def test_reply_beside_another_call_is_not_sent() -> None:
    probed: list[str] = []

    @function_tool
    async def probe() -> str:
        probed.append("ran")
        return json.dumps({"ok": True})

    model = ScriptedModel(
        [
            [
                scripted_call("reply", {"user_response": "Too early."}, "call_early"),
                scripted_call("probe", {}, "call_probe"),
            ],
            [scripted_call("reply", REPLY_ARGUMENTS, "call_reply")],
        ]
    )
    result, _ctx, _stream = await run_scripted_turn(model, tools=[probe])

    assert probed == ["ran"]
    assert len(model.inputs) == 2
    (refusal,) = tool_outputs_for(model.inputs[1], "call_early")
    assert json.loads(refusal)["ok"] is False
    assert parse_final_response(extract_final_text(result))["user_response"] == REPLY_ARGUMENTS["user_response"]


@pytest.mark.asyncio
async def test_text_final_answer_is_shown_without_a_reply_call_or_another_model_call() -> None:
    model = ScriptedModel([[scripted_text("Use a `for_loop` block.")]])
    result, _ctx, _stream = await run_scripted_turn(model)

    assert len(model.inputs) == 1
    assert parse_final_response(extract_final_text(result)) == {
        "type": "REPLY",
        "user_response": "Use a `for_loop` block.",
    }


@pytest.mark.asyncio
async def test_final_reply_drain_offers_only_reply_and_answers_any_other_call_with_a_result() -> None:
    probed: list[str] = []

    @function_tool
    async def probe() -> str:
        probed.append("ran")
        return json.dumps({"ok": True})

    model = ScriptedModel(
        [[scripted_call("probe", {}, "call_probe")], [scripted_call("reply", REPLY_ARGUMENTS, "call_reply")]]
    )
    result, ctx, stream = await run_production_loop(model, final_reply=True, native_tools=[probe, reply_tool])

    assert [tool.name for tool in model.served_tools] == ["reply"]
    assert probed == []
    assert len(model.inputs) == 2
    (not_offered,) = tool_outputs_for(model.inputs[1], "call_probe")
    assert json.loads(not_offered)["ok"] is False
    assert parse_final_response(extract_final_text(result))["user_response"] == REPLY_ARGUMENTS["user_response"]
    results = [f for f in stream.sent if f.type == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [(f.tool_call_id, f.success) for f in results] == [("call_probe", False)]
    assert ctx.pending_stream_tool_call_ids == set()


@pytest.mark.asyncio
async def test_reply_carrying_its_context_as_a_json_string_is_sent() -> None:
    arguments = {"user_response": "Use a `for_loop` block.", "global_llm_context": '{"user_goal": "loop over URLs"}'}
    model = ScriptedModel([[scripted_call("reply", arguments)]])
    result, _ctx, _stream = await run_scripted_turn(model)

    assert len(model.inputs) == 1
    parsed = parse_final_response(extract_final_text(result))
    assert adopt_model_authored_context(None, parsed["global_llm_context"]).user_goal == "loop over URLs"


@pytest.mark.asyncio
async def test_model_error_with_no_response_raises_after_an_unknown_tool_recovery() -> None:
    model = ScriptedModel([[scripted_call("REPLY", {}, "call_made_up")], None])

    with pytest.raises(ModelBehaviorError):
        await run_scripted_turn(model)
    assert len(model.inputs) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("final_reply", [False, True])
async def test_production_agent_loop_ends_the_turn_on_a_reply_call(final_reply: bool) -> None:
    model = ScriptedModel([[scripted_call("reply", REPLY_ARGUMENTS)], [scripted_text("A second answer.")]])
    result, ctx, stream = await run_production_loop(model, final_reply=final_reply)

    assert len(model.inputs) == 1
    parsed = parse_final_response(extract_final_text(result))
    assert parsed["user_response"] == REPLY_ARGUMENTS["user_response"]
    assert adopt_model_authored_context(None, parsed["global_llm_context"]).user_goal == "loop over URLs"
    assert ctx.tool_calls_this_turn == 0
    assert tool_frame_types(stream) == []


@pytest.mark.asyncio
async def test_final_reply_drain_withholds_a_reply_call_carrying_a_raw_secret() -> None:
    model = ScriptedModel([[scripted_call("reply", {"user_response": "I used password: hunter2."})]])

    with pytest.raises(OutputGuardrailTripwireTriggered) as raised:
        await run_production_loop(
            model,
            final_reply=True,
            output_guardrails=_build_copilot_output_guardrails(OutputGuardrail, GuardrailFunctionOutput),
        )

    assert raised.value.guardrail_result.output.output_info["reason_codes"] == ["raw_secret_leak"]


@pytest.mark.asyncio
async def test_unsent_reply_before_an_empty_completion_leaves_the_attempt_retryable() -> None:
    model = ScriptedModel([[scripted_call("reply", {"user_response": ""}, "call_blank")], [scripted_text("")]])

    with pytest.raises(CopilotEmptyCompletionError) as raised:
        await run_production_loop(model)

    assert len(model.inputs) == 2
    assert raised.value.retry_allowed is True


@pytest.mark.asyncio
async def test_blank_reply_is_not_sent_and_the_turn_continues() -> None:
    model = ScriptedModel(
        [
            [scripted_call("reply", {"user_response": "  "}, "call_blank")],
            [scripted_call("reply", REPLY_ARGUMENTS, "call_reply")],
        ]
    )
    result, _ctx, stream = await run_scripted_turn(model)

    assert len(model.inputs) == 2
    (refusal,) = tool_outputs_for(model.inputs[1], "call_blank")
    assert json.loads(refusal)["ok"] is False
    assert parse_final_response(extract_final_text(result))["user_response"] == REPLY_ARGUMENTS["user_response"]
    assert tool_frame_types(stream) == []


@pytest.mark.asyncio
async def test_valid_call_beside_an_unknown_tool_does_not_run_and_its_row_is_closed_in_product_words() -> None:
    probed: list[str] = []

    @function_tool
    async def probe() -> str:
        probed.append("ran")
        return json.dumps({"ok": True})

    model = ScriptedModel(
        [
            [scripted_call("REPLY", {}, "call_made_up"), scripted_call("probe", {}, "call_probe")],
            [scripted_call("reply", REPLY_ARGUMENTS, "call_reply")],
        ]
    )
    result, ctx, stream = await run_scripted_turn(model, tools=[probe])

    assert probed == []
    assert len(model.inputs) == 2
    model_facing = [
        json.loads(output)["error"]
        for call_id in ("call_made_up", "call_probe")
        for output in tool_outputs_for(model.inputs[1], call_id)
    ]
    assert len(model_facing) == 2 and model_facing[0] != model_facing[1]
    assert "REPLY" in model_facing[0] and "REPLY" not in model_facing[1]
    assert parse_final_response(extract_final_text(result))["user_response"] == REPLY_ARGUMENTS["user_response"]
    results = [f for f in stream.sent if f.type == WorkflowCopilotStreamMessageType.TOOL_RESULT]
    assert [(f.tool_call_id, f.success) for f in results] == [("call_made_up", False), ("call_probe", False)]
    assert ctx.narrator_state is not None
    shown = json.dumps([f.model_dump(mode="json") for f in results]) + json.dumps(ctx.narrator_state.design_activity)
    for text in model_facing:
        assert text not in shown
