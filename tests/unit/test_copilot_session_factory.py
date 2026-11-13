"""Tests for the copilot session callback + call-model input filter."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import random
from collections.abc import AsyncIterator, Mapping, Sequence
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents import Agent, RunConfig, Runner, function_tool
from agents.items import ReasoningItem
from agents.run_config import ModelInputData
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import (
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from PIL import Image
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot.ask_user import (
    QuestionAnswer,
    QuestionChoice,
    QuestionInteraction,
    QuestionPart,
    QuestionResponse,
)
from skyvern.forge.sdk.copilot.config import SCREENSHOT_DROPPED_NUDGE, BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.enforcement import (
    _RECENT_TOOL_OUTPUT_CHAR_CAP,
    _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX,
    _TOOL_OUTPUT_SUMMARIZE_THRESHOLD,
    IMAGE_REMOVED_PLACEHOLDER,
    NUDGE_SENTINEL,
    TOKEN_BUDGET,
    TOOL_WINDOW_SUMMARIZED,
    estimate_tokens,
    is_screenshot_message,
    pending_screenshot_message,
    stable_prefix_anchors,
)
from skyvern.forge.sdk.copilot.model_resolver import CopilotLitellmProvider
from skyvern.forge.sdk.copilot.screenshot_utils import (
    ScreenshotActionRelation,
    ScreenshotEntry,
    ScreenshotProvenance,
)
from skyvern.forge.sdk.copilot.session_factory import (
    _drop_older_screenshots,
    _filter_to_budget,
    _summarize_older_tool_items,
    copilot_call_model_input_filter,
    copilot_session_input_callback,
    make_copilot_call_model_input_filter,
)
from skyvern.forge.sdk.copilot.video_attachment import VideoAttachmentFrame, build_image_attachment_message
from skyvern.forge.sdk.schemas.workflow_copilot import CopilotAttachedFile
from tests.unit.copilot_test_helpers import captured_frame_entry, image_parts, load_skill_items, make_copilot_ctx
from tests.unit.copilot_test_helpers import make_model_input_data as _mk_input_data
from tests.unit.copilot_test_helpers import sent_image_px


def _unread_run_results_batch() -> list[dict[str, Any]]:
    call_ids = [f"call_{index}" for index in range(5)]
    calls = [
        {"type": "function_call", "call_id": cid, "name": "get_run_results", "arguments": "{}"} for cid in call_ids
    ]
    outputs = [
        {
            "type": "function_call_output",
            "call_id": cid,
            "output": json.dumps({"ok": True, "data": {"workflow_run_id": cid, "blob": "z" * 3000}}),
        }
        for cid in call_ids
    ]
    return [{"type": "reasoning", "summary": []}, *calls, *outputs]


def _resolved_ask_user_output() -> str:
    interaction = QuestionInteraction(
        interaction_id="q-1",
        turn_id="turn-1",
        tool_call_id="call-ask",
        parts=[
            QuestionPart(
                part_id="p-1",
                prompt="Which report should the workflow retrieve, and for what date range? "
                "Please also provide the dashboard URL if you have it.",
                choices=[
                    QuestionChoice(choice_id="c-7", text="Last 7 days"),
                    QuestionChoice(choice_id="c-30", text="Last 30 days"),
                ],
            )
        ],
        status="resolved",
        response=QuestionResponse(
            answers=[QuestionAnswer(part_id="p-1", choice_id="c-30")],
            text="https://ads.example.com/account/123/dashboard",
        ),
    )
    return json.dumps(interaction.tool_result())


def _scouting_turn() -> list[dict[str, Any]]:
    """An over-cap page read, an answered question, then real-shaped search and browser-code reads."""
    page_text = "Pricing tier details. " * 2_600
    script = "rows = await page.locator('table tr').all_inner_texts()\n" * 8 + "return rows"
    results: list[tuple[str, str, dict[str, Any]]] = [
        (
            "run_browser_code",
            json.dumps({"code": script}),
            {
                "ok": True,
                "value": page_text,
                "stdout": "",
                "executed_source_reference": "src_0001",
            },
        ),
        ("ask_user", "{}", {}),
    ]
    for index in range(3):
        results.append(
            (
                "search_web",
                json.dumps({"query": f"plan pricing page {index}", "max_results": 10}),
                {
                    "ok": True,
                    "data": {
                        "query": f"plan pricing page {index}",
                        "results": [
                            {
                                "title": f"Result {index}-{hit}",
                                "url": f"https://site{index}.example.com/pricing/{hit}",
                                "snippet": f"Plan {hit} costs ${10 * (hit + 1)} per seat per month. " * 3,
                            }
                            for hit in range(5)
                        ],
                    },
                    "error": None,
                },
            )
        )
        results.append(
            (
                "run_browser_code",
                json.dumps({"code": f"return await page.inner_text('#plan-{index}')"}),
                {
                    "ok": True,
                    "value": f"Plan {index}: $49 per seat, billed annually. " * 20,
                    "stdout": f"read #plan-{index}\n" * 10,
                    "executed_source_reference": f"src_00{index + 2}",
                },
            )
        )
    items: list[dict[str, Any]] = [{"role": "user", "content": "compare the pricing of these plans"}]
    for index, (name, arguments, result) in enumerate(results):
        call_id = "call-ask" if name == "ask_user" else f"call-{index}"
        output = _resolved_ask_user_output() if name == "ask_user" else json.dumps(result)
        items.append({"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments})
        items.append({"type": "function_call_output", "call_id": call_id, "output": output})
        items.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": "Read it."}]})
    return items


def _ctx_with_staged_frame(*, supports_vision: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        pending_screenshots=[
            ScreenshotEntry(
                b64="dGVzdA==",
                mime="image/jpeg",
                capture_id="sha256:frame-123",
                provenance=ScreenshotProvenance(
                    source_tool="inspect_page_for_composition",
                    captured_url="https://example.com/results",
                    observation_step=2,
                    browser_session_id="pbs_123",
                    workflow_run_id="wr_123",
                    action_relation=ScreenshotActionRelation.SAME_PAGE_OBSERVATION,
                ),
            )
        ],
        supports_vision=supports_vision,
        pending_frame_lease=None,
    )


def _tool_round(index: int) -> list[dict[str, Any]]:
    output = json.dumps({"ok": True, "data": {"value": f"page {index} " * 400}})
    return [
        {"type": "function_call", "call_id": f"c{index}", "name": "run_browser_code", "arguments": "{}"},
        {"type": "function_call_output", "call_id": f"c{index}", "output": output},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Read it."}]},
    ]


_LADDER_RUNG_EVENTS = {
    "Within budget, outputs whole",
    "Within budget after tool trim",
    "Within budget after screenshot drop",
    "Within budget after soft emergency truncation",
    "Within budget after emergency truncation",
}


class TestModelInputBudget:
    def test_under_budget_every_output_and_argument_reaches_the_model_whole(self) -> None:
        items = _scouting_turn()
        assert len(items[2]["output"]) > _RECENT_TOOL_OUTPUT_CHAR_CAP
        assert any(
            it.get("type") == "function_call" and len(it["arguments"]) > _TOOL_OUTPUT_SUMMARIZE_THRESHOLD
            for it in items
        )

        result = copilot_call_model_input_filter(_mk_input_data(copilot_session_input_callback(items, [])))

        def payloads(source: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str]]:
            return [
                (it["type"], it["call_id"], it["output"] if "output" in it else it["arguments"])
                for it in source
                if it.get("type") in ("function_call", "function_call_output")
            ]

        oldest = items[2]["output"]
        expected = payloads(items)
        expected[1] = (
            "function_call_output",
            "call-0",
            oldest[:_RECENT_TOOL_OUTPUT_CHAR_CAP] + _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX,
        )
        assert payloads(result.input) == expected

    def test_over_budget_older_outputs_and_arguments_are_summarized_first(self) -> None:
        items = _scouting_turn()
        whole = copilot_call_model_input_filter(_mk_input_data(items)).input
        budget = estimate_tokens(whole) - 1

        with capture_logs() as logs:
            result = make_copilot_call_model_input_filter(token_budget=budget)(_mk_input_data(items))

        assert estimate_tokens(result.input) <= budget
        assert "Within budget after tool trim" in [entry["event"] for entry in logs]
        outputs = [it for it in result.input if it.get("type") == "function_call_output"]
        calls = [it for it in result.input if it.get("type") == "function_call"]
        oldest = json.loads(outputs[0]["output"])
        assert "_summarized" in oldest and oldest["ok"] is True
        answer = json.loads(next(it["output"] for it in outputs if it["call_id"] == "call-ask"))
        assert answer["text"] == "https://ads.example.com/account/123/dashboard"
        assert answer["parts"][0]["choice"]["text"] == "Last 30 days"
        original_outputs = [it for it in items if it.get("type") == "function_call_output"]
        assert [it["output"] for it in outputs[-3:]] == [it["output"] for it in original_outputs[-3:]]
        assert "_summarized" in calls[0]["arguments"]
        original_calls = [it for it in items if it.get("type") == "function_call"]
        assert [it["arguments"] for it in calls[-3:]] == [it["arguments"] for it in original_calls[-3:]]

    def test_over_budget_a_json_output_over_the_cap_still_gets_a_structured_synopsis(self) -> None:
        big = json.dumps(
            {"ok": True, "data": {"workflow_run_id": "wr_big", "blob": "z" * _RECENT_TOOL_OUTPUT_CHAR_CAP}}
        )
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "goal"},
            {"type": "function_call", "call_id": "c_big", "name": "get_run_results", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c_big", "output": big},
            {"type": "reasoning", "summary": []},
        ]
        for i in range(3):
            items += [
                {"type": "function_call", "call_id": f"c{i}", "name": "get_run_results", "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"c{i}", "output": json.dumps({"ok": True})},
                {"type": "reasoning", "summary": []},
            ]

        result = make_copilot_call_model_input_filter(token_budget=2000)(_mk_input_data(items))

        synopsis = json.loads(result.input[2]["output"])
        assert synopsis["_summarized"] and synopsis["workflow_run_id"] == "wr_big"

    def test_over_budget_an_output_the_summarizer_keeps_whole_is_still_capped(self) -> None:
        group = json.dumps(
            {
                "workflow_run_group_id": "g1",
                "rows": [{"workflow_run_id": f"wr_{i}", "status": "ok"} for i in range(2000)],
            }
        )
        assert len(group) > _RECENT_TOOL_OUTPUT_CHAR_CAP
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "goal"},
            {"type": "function_call", "call_id": "c_group", "name": "get_run_group", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c_group", "output": group},
            {"type": "reasoning", "summary": []},
        ]
        for i in range(4):
            items += [
                {"type": "function_call", "call_id": f"c{i}", "name": "get_run_results", "arguments": "{}"},
                {
                    "type": "function_call_output",
                    "call_id": f"c{i}",
                    "output": json.dumps({"ok": True, "v": "r" * 8000}),
                },
                {"type": "reasoning", "summary": []},
            ]
        budget = estimate_tokens(copilot_call_model_input_filter(_mk_input_data(items)).input) - 1

        with capture_logs() as logs:
            result = make_copilot_call_model_input_filter(token_budget=budget)(_mk_input_data(items))

        assert "Within budget after tool trim" in [entry["event"] for entry in logs]
        assert result.input[2]["output"] == group[:_RECENT_TOOL_OUTPUT_CHAR_CAP] + _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX

    def test_over_budget_cache_anchor_prefix_is_resent_unchanged_on_the_next_over_budget_call(self) -> None:
        history = [{"role": "user", "content": "goal"}, *(item for i in range(6) for item in _tool_round(i))]
        flt = make_copilot_call_model_input_filter(token_budget=estimate_tokens(history) // 2)

        first = flt(_mk_input_data(history)).input
        anchor = stable_prefix_anchors(first)[-1]
        second = flt(_mk_input_data([*history, *_tool_round(6)])).input

        assert "_summarized" in first[2]["output"] and "_summarized" in second[2]["output"]
        assert first[anchor]["call_id"] == "c2"
        assert first[: anchor + 1] == second[: anchor + 1]

    def test_under_budget_cache_anchor_is_the_newest_read_output_and_its_prefix_is_resent_unchanged(self) -> None:
        history = [{"role": "user", "content": "goal"}, *(item for i in range(6) for item in _tool_round(i))]

        first = copilot_call_model_input_filter(_mk_input_data(history)).input
        anchor = stable_prefix_anchors(first)[-1]
        second = copilot_call_model_input_filter(_mk_input_data([*history, *_tool_round(6)])).input

        assert first == history
        assert first[anchor]["call_id"] == "c5"
        assert first[: anchor + 1] == second[: anchor + 1]

    def test_every_ladder_rung_keeps_the_whole_answer_and_ends_within_budget(self) -> None:
        frame = pending_screenshot_message(make_copilot_ctx(pending_screenshots=[captured_frame_entry()]))
        turn = _scouting_turn()
        items = [
            turn[0],
            frame,
            *turn[1:7],
            frame,
            *turn[7:],
            {"type": "function_call", "call_id": "call-big", "name": "run_browser_code", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-big", "output": "Plan text. " * 800},
            {"type": "reasoning", "summary": []},
        ]
        ctx = make_copilot_ctx(supports_vision=True)
        resolved_at: set[str] = set()
        pruned = False

        for budget in range(estimate_tokens(items), 0, -100):
            with capture_logs() as logs:
                result = make_copilot_call_model_input_filter(token_budget=budget)(_mk_input_data(items, context=ctx))
            events = {entry["event"] for entry in logs}
            sent = [it for it in result.input if it.get("call_id") == "call-ask"]
            assert [it["type"] for it in sent] == ["function_call", "function_call_output"]
            assert sent[1]["output"] == _resolved_ask_user_output()
            if "Aggressive prune needed" in events:
                pruned = True
                break
            resolved_at |= events & _LADDER_RUNG_EVENTS
            assert estimate_tokens(result.input) <= budget

        assert pruned and resolved_at == _LADDER_RUNG_EVENTS

    def test_an_answer_in_an_unread_batch_over_budget_is_never_deferred(self) -> None:
        batch = _unread_run_results_batch()
        batch[5] = {"type": "function_call", "call_id": "call-ask", "name": "ask_user", "arguments": "{}"}
        batch[-1] = {"type": "function_call_output", "call_id": "call-ask", "output": _resolved_ask_user_output()}
        items = [{"role": "user", "content": "please build me a workflow"}, *batch]

        result = make_copilot_call_model_input_filter(token_budget=estimate_tokens(items) - 100)(_mk_input_data(items))

        sent = {it["call_id"]: it["output"] for it in result.input if it.get("type") == "function_call_output"}
        assert sent["call-ask"] == _resolved_ask_user_output()
        assert "already_ran" in sent["call_3"]

    def test_filter_keeps_every_unread_output_of_a_parallel_batch_whole(self) -> None:
        batch = _unread_run_results_batch()
        items = [{"role": "user", "content": "please build me a workflow"}, *batch]

        with capture_logs() as logs:
            result = copilot_call_model_input_filter(_mk_input_data(items))

        sent = {it["call_id"]: it["output"] for it in result.input if it.get("type") == "function_call_output"}
        assert sent == {it["call_id"]: it["output"] for it in batch if it.get("type") == "function_call_output"}
        assert not [entry for entry in logs if "truncat" in entry["event"]]

    def test_an_unread_batch_over_budget_is_split_not_summarized(self) -> None:
        from skyvern.forge.sdk.copilot.session_factory import make_copilot_call_model_input_filter

        batch = _unread_run_results_batch()
        items = [{"role": "user", "content": "please build me a workflow"}, *batch]
        outputs = {it["call_id"]: it["output"] for it in batch if it.get("type") == "function_call_output"}
        budget = estimate_tokens(items) - 100

        result = make_copilot_call_model_input_filter(token_budget=budget)(_mk_input_data(items))

        sent = {it["call_id"]: it["output"] for it in result.input if it.get("type") == "function_call_output"}
        whole = [cid for cid in outputs if sent[cid] == outputs[cid]]
        deferred = [cid for cid in outputs if cid not in whole]
        assert whole[0] == "call_0" and deferred
        for cid in deferred:
            notice = json.loads(sent[cid])
            assert notice["tool_name"] == "get_run_results" and notice["arguments"] == "{}"
            assert notice["already_ran"] is True

    def test_an_over_budget_batch_keeps_a_short_not_run_result_verbatim(self) -> None:
        not_run = json.dumps({"ok": False, "error": "Not run: another call in this response was rejected."})
        batch = _unread_run_results_batch()
        batch[-1] = {**batch[-1], "output": not_run}
        items = [{"role": "user", "content": "please build me a workflow"}, *batch]
        budget = estimate_tokens(items) - 100

        result = make_copilot_call_model_input_filter(token_budget=budget)(_mk_input_data(items))

        sent = [it["output"] for it in result.input if it.get("type") == "function_call_output"]
        assert sent[-1] == not_run
        assert any("already_ran" in output for output in sent[:-1])

    def test_an_over_budget_batch_defers_a_short_result_that_costs_more_tokens_than_its_notice(self) -> None:
        dense = json.dumps(
            {"ok": True, "data": "".join(chr(0x4E00 + index) for index in range(150))}, ensure_ascii=False
        )
        batch = _unread_run_results_batch()
        first_call, second_call = batch[1], batch[2]
        first_output = batch[6]
        second_output = {**batch[7], "output": dense}
        items = [
            {"role": "user", "content": "please build me a workflow"},
            first_call,
            second_call,
            first_output,
            second_output,
        ]
        budget = estimate_tokens(items) - 50

        result = make_copilot_call_model_input_filter(token_budget=budget)(_mk_input_data(items))

        sent = [it["output"] for it in result.input if it.get("type") == "function_call_output"]
        assert sent[0] == first_output["output"]
        assert json.loads(sent[1])["already_ran"] is True

    def test_recent_code_sized_output_survives_session_compaction(self) -> None:
        code_sized = json.dumps({"ok": True, "data": {"code": "await page.click()\n" * 400}})
        assert 2000 < len(code_sized) < _RECENT_TOOL_OUTPUT_CHAR_CAP
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "please build me a workflow"},
            {"type": "function_call_output", "call_id": "call-code", "output": code_sized},
        ]
        result = copilot_call_model_input_filter(_mk_input_data(items))
        outputs = [it for it in result.input if it.get("type") == "function_call_output"]
        assert outputs[0]["output"] == code_sized

    def test_recent_overcap_output_truncates_and_warns_on_session_path(self) -> None:
        oversized = "x" * (_RECENT_TOOL_OUTPUT_CHAR_CAP + 1000)
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "please build me a workflow"},
            {"type": "function_call", "call_id": "call-big", "name": "get_run_results", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call-big", "output": oversized},
        ]
        with capture_logs() as logs:
            result = copilot_call_model_input_filter(_mk_input_data(items))
        outputs = [it for it in result.input if it.get("type") == "function_call_output"]
        assert outputs[0]["output"].endswith(_TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX)
        truncated = [entry for entry in logs if entry["event"] == "copilot_recent_tool_output_truncated"]
        assert [entry["tool_name"] for entry in truncated] == [["get_run_results"]]

    def test_emergency_truncation_logs_distinct_event_with_count(self) -> None:
        import structlog.testing

        from skyvern.forge.sdk.copilot.session_factory import make_copilot_call_model_input_filter

        items: list[dict[str, Any]] = [
            {"role": "user", "content": "please build me a workflow"},
            *({"type": "function_call_output", "call_id": f"call-{i}", "output": "x" * 5000} for i in range(3)),
            {"type": "function_call_output", "call_id": "call-small", "output": "ok"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Read it."}]},
        ]
        tight_filter = make_copilot_call_model_input_filter(token_budget=200)
        with structlog.testing.capture_logs() as logs:
            tight_filter(_mk_input_data(items))
        emergency = [entry for entry in logs if entry["event"] == "copilot_tool_output_emergency_truncated"]
        assert [entry["cap"] for entry in emergency] == [2000, 300]
        assert all(entry["truncated_count"] >= 2 for entry in emergency)

    def test_attached_images_are_dropped_before_any_tool_output_is_cut(self) -> None:
        jpeg = BytesIO()
        Image.new("RGB", (1600, 1200), "white").save(jpeg, format="JPEG")
        files = [CopilotAttachedFile(file_id=f"file_{index}", filename=f"shot{index}.png") for index in range(2)]
        frames = [
            VideoAttachmentFrame(
                file_id=attached.file_id,
                filename=attached.filename,
                timestamp_seconds=0.0,
                jpeg_base64=base64.b64encode(jpeg.getvalue()).decode(),
            )
            for attached in files
        ]
        outputs = [{"type": "function_call_output", "call_id": f"call-{i}", "output": "x" * 5000} for i in range(3)]
        items: list[Any] = [
            {"role": "user", "content": "what do these show?"},
            build_image_attachment_message(files, frames, {}),
            *outputs,
        ]
        budget = estimate_tokens([items[0], *outputs]) + 200

        ctx = make_copilot_ctx(supports_vision=True)
        result = make_copilot_call_model_input_filter(token_budget=budget)(_mk_input_data(items, context=ctx))

        assert not image_parts(result.input[1])
        serialized = json.dumps(result.input[1])
        assert "Attached image shot0.png (file_id=file_0)" in serialized
        assert IMAGE_REMOVED_PLACEHOLDER in serialized
        assert [item for item in result.input if item.get("type") == "function_call_output"] == outputs

    def test_soft_emergency_rung_spares_code_when_it_fits(self) -> None:
        import structlog.testing

        from skyvern.forge.sdk.copilot.session_factory import make_copilot_call_model_input_filter

        items: list[dict[str, Any]] = [
            {"role": "user", "content": "please build me a workflow"},
            {"type": "function_call_output", "call_id": "call-big", "output": "x" * 40_000},
        ]
        soft_filter = make_copilot_call_model_input_filter(token_budget=800)
        with structlog.testing.capture_logs() as logs:
            result = soft_filter(_mk_input_data(items))
        emergency = [entry for entry in logs if entry["event"] == "copilot_tool_output_emergency_truncated"]
        assert [entry["cap"] for entry in emergency] == [2000]
        outputs = [it for it in result.input if it.get("type") == "function_call_output"]
        assert 2000 <= len(outputs[0]["output"]) <= 2020


class TestSessionInputCallback:
    def test_empty_history_returns_new_items(self) -> None:
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        new_items = [{"role": "user", "content": "hello"}]
        assert copilot_session_input_callback([], new_items) == new_items

    def test_materialized_paired_frame_is_placeholdered_after_tool_output(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import SCREENSHOT_PLACEHOLDER
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        goal = {"role": "user", "content": "build a workflow"}
        paired = {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "[copilot:screenshot] [copilot:paired-observation] Frame provenance",
                },
                {"type": "input_image", "image_url": "data:image/jpeg;base64,dGVzdA=="},
            ],
        }
        output = {"type": "function_call_output", "call_id": "click-1", "output": '{"ok":true}'}

        combined = copilot_session_input_callback([goal, paired, output], [])

        assert combined[1] == {"role": "user", "content": SCREENSHOT_PLACEHOLDER}

    def test_unmarked_generic_frame_keeps_existing_retention_behavior(self) -> None:
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        goal = {"role": "user", "content": "build a workflow"}
        generic = {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "[copilot:screenshot] generic tool frame"},
                {"type": "input_image", "image_url": "data:image/jpeg;base64,dGVzdA=="},
            ],
        }
        output = {"type": "function_call_output", "call_id": "read-1", "output": '{"ok":true}'}

        combined = copilot_session_input_callback([goal, generic, output], [])

        assert combined[1] == generic

    def test_merge_passes_older_tool_outputs_through_whole(self) -> None:
        goal = {"role": "user", "content": "please build me a workflow"}
        tool_items = [
            item
            for i in range(5)
            for item in (
                {
                    "type": "function_call_output",
                    "call_id": f"c-{i}",
                    "output": json.dumps({"ok": True, "data": {"blob": "y" * 4000}}),
                },
                {"type": "reasoning", "summary": []},
            )
        ]
        new = [{"role": "user", "content": "[copilot:nudge] please finish"}]

        combined = copilot_session_input_callback([goal, *tool_items], new)

        assert combined == [goal, *tool_items, *new]

    def test_unread_batch_at_the_end_of_the_middle_is_not_summarized(self) -> None:
        goal = {"role": "user", "content": "please build me a workflow"}
        batch = _unread_run_results_batch()
        nudge = [{"role": "user", "content": "[copilot:nudge] please finish"}]

        combined = copilot_session_input_callback([goal, *batch], nudge)

        outputs = [it["output"] for it in combined if it.get("type") == "function_call_output"]
        assert outputs == [it["output"] for it in batch if it.get("type") == "function_call_output"]

    def test_no_duplication_when_boundary_equals_one(self) -> None:
        """Regression guard: when ``_find_real_user_boundary`` returns 1, the
        earlier partitioning logic emitted ``history_items[1:]`` in both the
        middle and recent slices, duplicating every non-goal item. The fix
        makes middle empty and recent = ``history_items[1:]``."""
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        goal = {"role": "user", "content": "original goal"}
        # A shape that pushes ``_find_real_user_boundary(..., recent_turns=2)``
        # to return 1: two real user messages with the second-to-last at index 1.
        items = [
            goal,
            {"role": "user", "content": "followup real user message"},
            {"role": "assistant", "content": "assistant reply"},
            {"role": "user", "content": "latest real user message"},
        ]
        new: list[Any] = [{"role": "user", "content": "freshly arrived"}]

        combined = copilot_session_input_callback(items, new)
        # Total count = goal(1) + items[1:](3) + new(1) = 5. Previously this
        # was 8 due to duplication.
        assert len(combined) == 5
        assert combined[0] == goal
        assert combined[-1] == new[0]


class TestModelInputCapture:
    """COPILOT_DUMP_MODEL_INPUTS records what the model actually receives, so a prompt or
    tool-schema change can be replayed offline instead of re-run live.
    """

    def test_capture_is_inert_and_lossless_when_unset(self, tmp_path: Any, monkeypatch: Any) -> None:
        from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

        monkeypatch.delenv("COPILOT_DUMP_MODEL_INPUTS", raising=False)
        items = [{"role": "user", "content": "build me a workflow"}]

        result = copilot_call_model_input_filter(_mk_input_data(items))

        assert result.input == items
        assert list(tmp_path.iterdir()) == []

    def test_capture_records_instructions_and_input(self, tmp_path: Any, monkeypatch: Any) -> None:
        from agents import FunctionTool

        from skyvern.forge.sdk.copilot.model_input_capture import attach_tool_surface_to_pending_capture
        from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

        monkeypatch.setenv("COPILOT_DUMP_MODEL_INPUTS", str(tmp_path))
        items = [
            {"role": "user", "content": "output the number of azure errors"},
            {"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'},
        ]

        copilot_call_model_input_filter(
            _mk_input_data(
                items,
                instructions="SYSTEM PROMPT",
                context=SimpleNamespace(eval_capture_case_id="ask_or_build"),
            )
        )
        attach_tool_surface_to_pending_capture(
            [
                FunctionTool(
                    name="inspect_page",
                    description="Return structured page evidence.",
                    params_json_schema={
                        "type": "object",
                        "properties": {"selector": {"type": "string"}},
                        "required": ["selector"],
                    },
                    on_invoke_tool=lambda _ctx, _args: None,
                    strict_json_schema=False,
                )
            ]
        )

        dumps = sorted(tmp_path.glob("call-*.json"))
        assert len(dumps) == 1
        payload = json.loads(dumps[0].read_text())
        assert payload["instructions"] == "SYSTEM PROMPT"
        assert payload["input"] == items
        assert payload["capture_case_id"] == "ask_or_build"
        assert payload["token_budget"] == TOKEN_BUDGET
        # A context the derivation helper cannot read must not cost the run its model call.
        assert payload["requested_output_paths"] == []
        assert payload["tool_surface"] == {
            "version": "copilot-model-tool-surface-v1",
            "tools": [
                {
                    "name": "inspect_page",
                    "description": "Return structured page evidence.",
                    "params_json_schema": {
                        "type": "object",
                        "properties": {"selector": {"type": "string"}},
                        "required": ["selector"],
                    },
                    "strict_json_schema": False,
                }
            ],
        }
        assert (
            payload["tool_surface_sha256"]
            == hashlib.sha256(
                json.dumps(payload["tool_surface"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )

    def test_capture_records_the_authoring_capability_the_turn_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COPILOT_DUMP_MODEL_INPUTS", str(tmp_path))
        items = [{"role": "user", "content": "add a step that reads the support email"}]

        copilot_call_model_input_filter(
            _mk_input_data(items, context=make_copilot_ctx(block_authoring_policy=BlockAuthoringPolicy.STANDARD))
        )
        copilot_call_model_input_filter(
            _mk_input_data(
                items,
                context=make_copilot_ctx(
                    block_authoring_policy=BlockAuthoringPolicy.STANDARD, agent_step_available=True
                ),
            )
        )
        copilot_call_model_input_filter(_mk_input_data(items))

        unified, agent_step, context_less = (
            json.loads(path.read_text()) for path in sorted(tmp_path.glob("call-*.json"))
        )
        assert unified["authoring_capability"] == {"code_blocks": True, "agent_blocks": True, "agent_step": False}
        assert agent_step["authoring_capability"] == {"code_blocks": True, "agent_blocks": True, "agent_step": True}
        assert context_less["authoring_capability"] is None

    def test_capture_records_a_call_whichever_shape_carries_the_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A live turn dumped 2 of 34 calls: the copilot's own hand over the context directly, and
        # reading it as a wrapper lost every one of them to an AttributeError (SKY-13226).
        from agents.run_context import RunContextWrapper

        from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

        monkeypatch.setenv("COPILOT_DUMP_MODEL_INPUTS", str(tmp_path))
        items = [{"role": "user", "content": "read the visitor count"}]

        copilot_call_model_input_filter(_mk_input_data(items, context=SimpleNamespace()))
        copilot_call_model_input_filter(_mk_input_data(items, context=RunContextWrapper(context=SimpleNamespace())))

        assert len(sorted(tmp_path.glob("call-*.json"))) == 2


def test_build_test_packet_survives_recent_tool_output_head_compaction() -> None:
    from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
    from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

    packet = {
        "contract_version": "build_test_evidence_packet_v1",
        "workflow_permanent_id": "wfp_compaction",
        "canonical_workflow_yaml": "title: accepted workflow",
        "canonical_workflow_source": "accepted_write_readback",
        "canonical_workflow_yaml_complete": True,
        "attempted_block_labels": ["read_total"],
        "executed_block_labels": ["read_total"],
        "run": {"workflow_run_id": "wr_compaction", "status": "failed"},
        "failure": {"block_label": "read_total", "block_status": "failed", "reason": "missing total"},
        "registered_outputs": [],
        "downloads": [],
        "screenshot": {"present": True, "provenance": "data.screenshot_base64"},
        "unfinished_items": [{"kind": "unverified_block", "label": "read_total"}],
        "omission_notices": [],
    }
    sanitized = sanitize_tool_result_for_llm(
        "run_blocks_and_collect_debug",
        {
            "ok": False,
            "data": {
                "legacy_blob": "x" * 60_000,
                "screenshot_base64": "raw-frame-bytes",
                "build_test_packet": packet,
            },
        },
    )
    output = json.dumps(sanitized)
    filtered = copilot_call_model_input_filter(
        _mk_input_data([{"type": "function_call_output", "call_id": "run-1", "output": output}])
    )
    compacted_output = filtered.input[0]["output"]
    projected_packet = json.dumps(sanitized["data"]["build_test_packet"])

    assert len(output) > 50_000
    assert projected_packet in compacted_output
    assert compacted_output.index("build_test_packet") < compacted_output.index("legacy_blob")
    assert "wfp_compaction" in compacted_output
    assert "wr_compaction" in compacted_output
    assert "raw-frame-bytes" not in compacted_output


def test_model_input_pipeline_has_no_generated_offer_special_case() -> None:
    from skyvern.forge.sdk.copilot import enforcement

    assert not hasattr(enforcement, "collapse_superseded_synthesized_offers")


class TestStagedScreenshotBinding:
    """A frame a tool captured mid-run must reach the acting model on the same turn."""

    def test_staged_frame_rides_as_the_last_item(self) -> None:
        ctx = _ctx_with_staged_frame()
        items = [{"role": "user", "content": "clear the modal on this page"}]

        with capture_logs() as logs:
            result = copilot_call_model_input_filter(_mk_input_data(items, context=ctx))

        assert [len(image_parts(item)) for item in result.input] == [0, 1]
        assert any(log["event"] == "Injecting screenshot user message" for log in logs)

    def test_linked_structural_result_precedes_the_frame_and_its_provenance(self) -> None:
        ctx = _ctx_with_staged_frame()
        items = [
            {"role": "user", "content": "inspect this page"},
            {
                "type": "function_call_output",
                "call_id": "inspect-1",
                "output": json.dumps(
                    {
                        "ok": True,
                        "current_url": "https://example.com/results",
                        "observation_step": 2,
                        "data": {"source_tool": "inspect_page_for_composition"},
                    }
                ),
            },
        ]

        result = copilot_call_model_input_filter(_mk_input_data(items, context=ctx))

        assert result.input[-2] == items[-1]
        assert len(image_parts(result.input[-1])) == 1
        provenance_text = result.input[-1]["content"][0]["text"]
        assert "source_tool=inspect_page_for_composition" in provenance_text
        assert "observation_step=2" in provenance_text

    def test_a_second_pass_over_the_same_context_still_carries_the_frame(self) -> None:
        ctx = _ctx_with_staged_frame()
        items = [{"role": "user", "content": "clear the modal on this page"}]

        first = copilot_call_model_input_filter(_mk_input_data(items, context=ctx))
        second = copilot_call_model_input_filter(_mk_input_data(items, context=ctx))

        assert len(image_parts(first.input[-1])) == 1
        assert len(image_parts(second.input[-1])) == 1
        assert len(ctx.pending_screenshots) == 1

    def test_paired_frame_is_not_redelivered_after_model_input_advances(self) -> None:
        ctx = _ctx_with_staged_frame()
        initial = [{"role": "user", "content": "clear the modal on this page"}]

        first = copilot_call_model_input_filter(_mk_input_data(initial, context=ctx))
        advanced = [
            *initial,
            {"type": "function_call", "call_id": "click-1", "name": "click", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "click-1", "output": '{"ok":true}'},
        ]
        second = copilot_call_model_input_filter(_mk_input_data(advanced, context=ctx))

        assert len(image_parts(first.input[-1])) == 1
        assert not any(image_parts(item) for item in second.input)
        assert ctx.pending_screenshots == []
        assert ctx.pending_frame_lease is None

    def test_new_paired_capture_gets_a_fresh_lease_after_invalidation(self) -> None:
        ctx = _ctx_with_staged_frame()
        initial = [{"role": "user", "content": "clear the modal on this page"}]
        copilot_call_model_input_filter(_mk_input_data(initial, context=ctx))
        copilot_call_model_input_filter(
            _mk_input_data([*initial, {"role": "assistant", "content": "acted"}], context=ctx)
        )
        ctx.pending_screenshots = _ctx_with_staged_frame().pending_screenshots

        delivered = copilot_call_model_input_filter(
            _mk_input_data([*initial, {"role": "assistant", "content": "acted"}], context=ctx)
        )

        assert len(image_parts(delivered.input[-1])) == 1
        assert ctx.pending_frame_lease.capture_id == "sha256:frame-123"

    def test_same_pixels_from_a_new_capture_event_get_a_fresh_lease(self) -> None:
        ctx = _ctx_with_staged_frame()
        initial = [{"role": "user", "content": "inspect this page"}]
        copilot_call_model_input_filter(_mk_input_data(initial, context=ctx))
        first_event_id = ctx.pending_frame_lease.capture_event_id

        recaptured = _ctx_with_staged_frame().pending_screenshots[0]
        assert recaptured.capture_id == ctx.pending_screenshots[0].capture_id
        assert recaptured.capture_event_id != first_event_id
        ctx.pending_screenshots = [recaptured]
        advanced = [*initial, {"role": "assistant", "content": "the page was inspected again"}]

        delivered = copilot_call_model_input_filter(_mk_input_data(advanced, context=ctx))

        assert len(image_parts(delivered.input[-1])) == 1
        assert ctx.pending_frame_lease.capture_event_id == recaptured.capture_event_id

    def test_a_sharp_frame_over_budget_costs_older_screenshots_first_and_then_falls_back_to_the_small_copy(
        self,
    ) -> None:
        older_frame = pending_screenshot_message(make_copilot_ctx(pending_screenshots=[captured_frame_entry()]))
        items = [
            {"role": "user", "content": "what does the page say"},
            older_frame,
            {"type": "function_call", "call_id": "read-1", "name": "evaluate", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "read-1", "output": '{"text":"order 4412 shipped"}'},
        ]

        def call(token_budget: int) -> ModelInputData:
            ctx = make_copilot_ctx(pending_screenshots=[captured_frame_entry()])
            return make_copilot_call_model_input_filter(token_budget=token_budget)(_mk_input_data(items, context=ctx))

        for token_budget, new_frame_px in ((2_200, (1600, 789)), (1_200, (1024, 505))):
            result = call(token_budget)
            assert [sent_image_px(item) for item in result.input] == [[], [], [], [], [new_frame_px]]
            assert result.input[2:4] == items[2:4]

    def test_over_budget_the_sharp_frame_is_weighed_against_the_summarized_input(self) -> None:
        items: list[dict[str, Any]] = [{"role": "user", "content": "what does the page say"}]
        for i in range(6):
            items += [
                {"type": "function_call", "call_id": f"read-{i}", "name": "get_run_results", "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"read-{i}", "output": json.dumps({"v": f"{i} " * 4000})},
                {"type": "reasoning", "summary": []},
            ]
        sharp = pending_screenshot_message(make_copilot_ctx(pending_screenshots=[captured_frame_entry()]), sharp=True)
        budget = estimate_tokens([*_summarize_older_tool_items(items), sharp])
        assert estimate_tokens(items) > budget

        ctx = make_copilot_ctx(pending_screenshots=[captured_frame_entry()])
        result = make_copilot_call_model_input_filter(token_budget=budget)(_mk_input_data(items, context=ctx))

        assert estimate_tokens(result.input) <= budget
        assert sent_image_px(result.input[-1]) == [(1600, 789)]

    def test_the_sharp_frame_never_costs_whole_tool_outputs(self) -> None:
        older_frame = pending_screenshot_message(make_copilot_ctx(pending_screenshots=[captured_frame_entry()]))
        items: list[dict[str, Any]] = [{"role": "user", "content": "what does the page say"}, older_frame]
        for i in range(6):
            items += [
                {"type": "function_call", "call_id": f"read-{i}", "name": "get_run_results", "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"read-{i}", "output": json.dumps({"v": f"{i} " * 1500})},
            ]
        frame_ctx = make_copilot_ctx(pending_screenshots=[captured_frame_entry()])
        small, sharp = pending_screenshot_message(frame_ctx), pending_screenshot_message(frame_ctx, sharp=True)
        whole_with_small = estimate_tokens([*items, small])
        sharp_if_older_dropped = estimate_tokens(_drop_older_screenshots([*items, sharp]))
        assert whole_with_small <= sharp_if_older_dropped < estimate_tokens([*items, sharp])

        def call(token_budget: int) -> ModelInputData:
            ctx = make_copilot_ctx(pending_screenshots=[captured_frame_entry()])
            return make_copilot_call_model_input_filter(token_budget=token_budget)(_mk_input_data(items, context=ctx))

        fits_whole_with_small = call(sharp_if_older_dropped)
        assert sent_image_px(fits_whole_with_small.input[-1]) == [(1024, 505)]
        assert "_summarized" not in json.dumps(fits_whole_with_small.input)
        assert TOOL_WINDOW_SUMMARIZED.get() is False

        summarized_budget = estimate_tokens(_drop_older_screenshots([*_summarize_older_tool_items(items), sharp]))
        assert summarized_budget < whole_with_small
        over_either_way = call(summarized_budget)
        assert sent_image_px(over_either_way.input[-1]) == [(1600, 789)]
        assert estimate_tokens(over_either_way.input) <= summarized_budget

    def test_the_sharp_frame_never_costs_chat_history(self) -> None:
        items: list[dict[str, Any]] = [{"role": "user", "content": "what does the page say"}]
        for i in range(12):
            items += [
                {"role": "user", "content": f"note {i}: " + "detail " * 80},
                {"role": "assistant", "content": f"noted {i}"},
            ]
        frame_ctx = make_copilot_ctx(pending_screenshots=[captured_frame_entry()])
        small, sharp = pending_screenshot_message(frame_ctx), pending_screenshot_message(frame_ctx, sharp=True)
        budget = estimate_tokens([*items, small])
        assert estimate_tokens([*items, sharp]) > budget
        assert estimate_tokens(_filter_to_budget([*items, sharp], None, token_budget=budget).input) <= budget

        ctx = make_copilot_ctx(pending_screenshots=[captured_frame_entry()])
        result = make_copilot_call_model_input_filter(token_budget=budget)(_mk_input_data(items, context=ctx))

        assert sent_image_px(result.input[-1]) == [(1024, 505)]
        assert result.input[:-1] == items

    def test_attached_images_do_not_cost_the_pending_frame_its_sharp_copy(self) -> None:
        jpeg = BytesIO()
        Image.new("RGB", (1600, 1200), "white").save(jpeg, format="JPEG")
        files = [CopilotAttachedFile(file_id=f"file_{index}", filename=f"shot{index}.png") for index in range(3)]
        frames = [
            VideoAttachmentFrame(
                file_id=attached.file_id,
                filename=attached.filename,
                timestamp_seconds=0.0,
                jpeg_base64=base64.b64encode(jpeg.getvalue()).decode(),
            )
            for attached in files
        ]
        items = [
            {"role": "user", "content": "what does the page say"},
            build_image_attachment_message(files, frames, {}),
        ]
        ctx = make_copilot_ctx(pending_screenshots=[captured_frame_entry()], supports_vision=True)

        result = make_copilot_call_model_input_filter(token_budget=2_600)(_mk_input_data(items, context=ctx))

        assert [sent_image_px(item) for item in result.input] == [[], [], [(1600, 789)]]

    def test_the_pending_frame_is_sent_sharp_on_every_call_that_carries_it(self) -> None:
        ctx = SimpleNamespace(pending_screenshots=[captured_frame_entry()], supports_vision=True)
        initial = [{"role": "user", "content": "what does the page say"}]
        advanced = [
            *initial,
            {"type": "function_call", "call_id": "read-1", "name": "evaluate", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "read-1", "output": '{"ok":true}'},
        ]

        first = copilot_call_model_input_filter(_mk_input_data(initial, context=ctx))
        later = copilot_call_model_input_filter(_mk_input_data(advanced, context=ctx))

        for result in (first, later):
            message = result.input[-1]
            assert sent_image_px(message) == [(1600, 789)]
            assert "image_px=1600x789; viewport_css_px=1920x947" in message["content"][0]["text"]

    def test_paired_message_has_machine_readable_marker(self) -> None:
        ctx = _ctx_with_staged_frame()

        result = copilot_call_model_input_filter(_mk_input_data([{"role": "user", "content": "inspect"}], context=ctx))

        assert result.input[-1]["content"][0]["text"].startswith("[copilot:screenshot] [copilot:paired-observation] ")

    def test_a_non_vision_fallback_model_gets_no_image(self) -> None:
        # The frame was staged while the primary was still vision-capable; a retriable failure
        # can swap in a fallback that cannot accept images.
        ctx = _ctx_with_staged_frame(supports_vision=False)
        items = [{"role": "user", "content": "clear the modal on this page"}]

        result = copilot_call_model_input_filter(_mk_input_data(items, context=ctx))

        assert not any(image_parts(item) for item in result.input)
        assert len(ctx.pending_screenshots) == 1

    @pytest.mark.parametrize("supports_vision", [True, False])
    def test_images_already_in_history_reach_only_a_vision_model(self, supports_vision: bool) -> None:
        attached = {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Attached image Capture.PNG (file_id=file_1):"},
                {"type": "input_image", "image_url": "data:image/jpeg;base64,dGVzdA==", "detail": "high"},
            ],
        }
        items = [{"role": "user", "content": "what does this show?"}, attached]

        result = copilot_call_model_input_filter(
            _mk_input_data(items, context=make_copilot_ctx(supports_vision=supports_vision))
        )

        assert [len(image_parts(item)) for item in result.input] == [0, 1 if supports_vision else 0]
        assert result.input[1]["content"][0] == attached["content"][0]
        assert not is_screenshot_message(result.input[1])
        if not supports_vision:
            assert result.input[1]["content"][1] == {"type": "input_text", "text": IMAGE_REMOVED_PLACEHOLDER}

    def test_call_with_nothing_staged_carries_no_image(self) -> None:
        ctx = SimpleNamespace(pending_screenshots=[])
        items = [{"role": "user", "content": "clear the modal on this page"}]

        with capture_logs() as logs:
            result = copilot_call_model_input_filter(_mk_input_data(items, context=ctx))

        assert result.input == items
        assert not any(image_parts(item) for item in result.input)
        assert not any(log["event"] == "Injecting screenshot user message" for log in logs)

    def test_last_resort_prune_keeps_the_bound_frame_and_asks_for_a_retake_only_without_one(self) -> None:
        retake_note = NUDGE_SENTINEL + SCREENSHOT_DROPPED_NUDGE
        frame_older_than_the_kept_steps = pending_screenshot_message(
            make_copilot_ctx(pending_screenshots=[captured_frame_entry()])
        )
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "build me a workflow"},
            frame_older_than_the_kept_steps,
        ]
        for i in range(12):
            items.append({"type": "function_call", "call_id": f"call-{i}", "name": "observe", "arguments": "{}"})
            items.append({"type": "function_call_output", "call_id": f"call-{i}", "output": "y" * 4000})
        retake_note_from_an_earlier_recovery = [*items[:-2], {"role": "user", "content": retake_note}, *items[-2:]]
        over_budget_after_every_trim = make_copilot_call_model_input_filter(1)

        with_frame = over_budget_after_every_trim(
            _mk_input_data(
                retake_note_from_an_earlier_recovery,
                context=make_copilot_ctx(pending_screenshots=_ctx_with_staged_frame().pending_screenshots),
            )
        )
        without_frame = over_budget_after_every_trim(_mk_input_data(items, context=make_copilot_ctx()))

        assert [len(image_parts(item)) for item in with_frame.input if image_parts(item)] == [1]
        assert len(image_parts(with_frame.input[-1])) == 1
        assert not any(item.get("content") == retake_note for item in with_frame.input)
        assert not any(image_parts(item) for item in without_frame.input)
        assert [item.get("content") for item in without_frame.input].count(retake_note) == 1

    def test_last_resort_prune_drops_an_old_frame_even_when_an_older_skill_load_is_kept(self) -> None:
        skill = load_skill_items("call_skill", "example-portal-login", "Tick it.\n" * 50)
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "build me a workflow"},
            *skill,
            pending_screenshot_message(make_copilot_ctx(pending_screenshots=[captured_frame_entry()])),
        ]
        for i in range(12):
            items.append({"type": "function_call", "call_id": f"call-{i}", "name": "observe", "arguments": "{}"})
            items.append({"type": "function_call_output", "call_id": f"call-{i}", "output": "y" * 4000})

        result = make_copilot_call_model_input_filter(1)(_mk_input_data(items, context=make_copilot_ctx()))

        assert skill[2] in result.input
        assert not any(image_parts(item) for item in result.input)

    def test_aggressive_prune_reports_omitted_demonstration_fact_packets(self) -> None:
        items: list[dict[str, Any]] = [{"role": "user", "content": "build the demonstrated workflow"}]
        for i in range(12):
            call_id = f"call_demo_turn-1_{i}"
            items.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "click",
                    "arguments": json.dumps({"selector": "x" * 4000}),
                }
            )
            items.append({"type": "function_call_output", "call_id": call_id, "output": "y" * 4000})

        result = make_copilot_call_model_input_filter(1)(_mk_input_data(items))

        notices = [
            item["content"]
            for item in result.input
            if item.get("role") == "user" and "demonstrated step fact packet" in item.get("content", "")
        ]
        assert len(notices) == 1
        assert notices[0].startswith("[copilot:nudge] ")


@pytest.mark.asyncio
async def test_signature_only_claude_thinking_does_not_use_up_the_model_input_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_text = "page text " * 1500
    signature = base64.b64encode(random.Random(0).randbytes(30_000)).decode()
    requests: list[list[dict[str, Any]]] = []

    def chunk(delta: ChoiceDelta, finish_reason: str | None = None) -> ChatCompletionChunk:
        return ChatCompletionChunk(
            id="chatcmpl-thinking",
            created=0,
            model="claude-sonnet-5-5",
            object="chat.completion.chunk",
            choices=[ChunkChoice(index=0, delta=delta, finish_reason=finish_reason)],
        )

    async def reply(first: bool) -> AsyncIterator[ChatCompletionChunk]:
        yield chunk(ChoiceDelta(thinking_blocks=[{"type": "thinking", "thinking": "", "signature": signature}]))
        if first:
            call = ChoiceDeltaToolCall(
                index=0,
                id="toolu_read",
                type="function",
                function=ChoiceDeltaToolCallFunction(name="read_page", arguments="{}"),
            )
            yield chunk(ChoiceDelta(tool_calls=[call]))
            yield chunk(ChoiceDelta(), "tool_calls")
        else:
            yield chunk(ChoiceDelta(content="done"))
            yield chunk(ChoiceDelta(), "stop")

    async def fake_acompletion(**kwargs: object) -> AsyncIterator[ChatCompletionChunk]:
        requests.append(copy.deepcopy(cast(list[dict[str, Any]], kwargs["messages"])))
        return reply(first=len(requests) == 1)

    @function_tool
    def read_page() -> str:
        return page_text

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    agent = Agent(name="workflow-copilot", model="anthropic/claude-sonnet-5-5", tools=[read_page])
    run_config = RunConfig(
        model_provider=CopilotLitellmProvider(),
        tracing_disabled=True,
        call_model_input_filter=make_copilot_call_model_input_filter(estimate_tokens([page_text]) + 2_000),
    )

    result = Runner.run_streamed(agent, input="Read the page.", run_config=run_config)
    async for _ in result.stream_events():
        pass

    assert any(isinstance(item, ReasoningItem) for item in result.new_items)
    second_request = json.dumps(requests[1])
    assert json.dumps(page_text) in second_request
    assert signature not in second_request


def _skill_session_history() -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    skill = load_skill_items("call_skill", "example-portal-login", "Tick 'Remember this device' first.\n" * 400)
    read_output = json.dumps({"ok": True, "data": {"name": "example-portal-login", "path": "a.md", "text": "y" * 5000}})
    items: list[dict[str, Any]] = [
        {"role": "user", "content": "please build me a workflow"},
        *skill,
        {"type": "function_call", "call_id": "call_read", "name": "read_skill_resource", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_read", "output": read_output},
    ]
    for index in range(6):
        items += [
            {"type": "function_call", "call_id": f"call_{index}", "name": "get_run_results", "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"call_{index}", "output": "x" * 5000},
        ]
    items.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": "Read them."}]})
    return items, skill, read_output


def test_a_loaded_skill_outside_the_recent_outputs_is_sent_byte_identical() -> None:
    items, skill, read_output = _skill_session_history()

    result = make_copilot_call_model_input_filter(token_budget=estimate_tokens(items) - 1)(_mk_input_data(items))

    outputs = {it["call_id"]: it["output"] for it in result.input if it.get("type") == "function_call_output"}
    assert outputs["call_skill"] == skill[2]["output"]
    assert outputs["call_read"] != read_output


def test_a_loaded_skill_survives_every_budget_rung_whole_and_paired() -> None:
    items, skill, read_output = _skill_session_history()

    with capture_logs() as logs:
        result = make_copilot_call_model_input_filter(token_budget=200)(_mk_input_data(items))

    assert {"copilot_tool_output_emergency_truncated", "copilot_aggressive_prune_pair_validity"} <= {
        entry["event"] for entry in logs
    }
    sent = list(result.input)
    skill_outputs = [it for it in sent if it.get("call_id") == "call_skill" and it["type"] == "function_call_output"]
    assert [it["output"] for it in skill_outputs] == [skill[2]["output"]]
    assert sent.index(skill[1]) < sent.index(skill_outputs[0])
    assert read_output not in [it.get("output") for it in sent]
    call_ids = [it["call_id"] for it in sent if it.get("type") == "function_call"]
    assert len(call_ids) == len(set(call_ids))


@pytest.mark.parametrize("skill_first", [True, False])
def test_an_over_budget_batch_never_defers_a_loaded_skill_and_still_defers_its_sibling(skill_first: bool) -> None:
    reasoning, skill_call, skill_output = load_skill_items("call_skill", "example-portal-login", "Tick it.\n" * 200)
    run_call = {"type": "function_call", "call_id": "call_run", "name": "get_run_results", "arguments": "{}"}
    run_output = {"type": "function_call_output", "call_id": "call_run", "output": json.dumps({"data": "x" * 8000})}
    seen_call = {"type": "function_call", "call_id": "call_seen", "name": "get_run_results", "arguments": "{}"}
    seen_output = {"type": "function_call_output", "call_id": "call_seen", "output": json.dumps({"data": "s" * 10_000})}
    pairs = [(skill_call, skill_output), (run_call, run_output)]
    if not skill_first:
        pairs.reverse()
    items = [
        {"role": "user", "content": "please build me a workflow"},
        seen_call,
        seen_output,
        reasoning,
        *(call for call, _ in pairs),
        *(output for _, output in pairs),
    ]

    result = make_copilot_call_model_input_filter(token_budget=estimate_tokens(items) - 100)(_mk_input_data(items))

    sent = {it["call_id"]: it["output"] for it in result.input if it.get("type") == "function_call_output"}
    assert sent["call_skill"] == skill_output["output"]
    assert json.loads(sent["call_run"])["already_ran"] is True
    assert sent["call_seen"] == seen_output["output"]
