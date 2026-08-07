"""Tests for the copilot session callback + call-model input filter."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _mk_input_data(items: list[Any], *, instructions: str | None = None, context: Any = None) -> Any:
    """Build a fake CallModelData payload with a model_data.input list.

    ``CallModelData.context`` is the run context itself (``TContext | None``), not a wrapper around
    one; a fake that nests it hides an attribute error behind a passing test.
    """
    return SimpleNamespace(
        model_data=SimpleNamespace(input=list(items), instructions=instructions),
        context=context,
    )


class TestFirstTurnCompaction:
    """CORR-3 regression guards: first-turn transcripts (one real user +
    long tool chain) must compact older tool outputs / function-call args
    using the KEEP_RECENT_TOOL_OUTPUTS rule, not a user-boundary fallback.
    """

    def test_filter_compacts_older_tool_outputs_on_first_turn(self) -> None:
        from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

        large_output = "x" * 5000
        small_summary_marker = "_summarized"

        items: list[dict[str, Any]] = [
            {"role": "user", "content": "please build me a workflow"},
            # six function_call_output items; the last 3 stay raw, older 3 compact.
            *(
                {
                    "type": "function_call_output",
                    "call_id": f"call-{i}",
                    "output": json.dumps({"ok": True, "data": {"blob": large_output}}),
                }
                for i in range(6)
            ),
        ]
        result = copilot_call_model_input_filter(_mk_input_data(items))
        outputs = [it for it in result.input if it.get("type") == "function_call_output"]
        assert len(outputs) == 6
        older_three = outputs[:3]
        recent_three = outputs[3:]
        for older in older_three:
            assert small_summary_marker in older["output"]
        for recent in recent_three:
            assert small_summary_marker not in recent["output"]

    def test_recent_code_sized_output_survives_session_compaction(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _RECENT_TOOL_OUTPUT_CHAR_CAP
        from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

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
        import structlog.testing

        from skyvern.forge.sdk.copilot.enforcement import _RECENT_TOOL_OUTPUT_CHAR_CAP
        from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

        oversized = "x" * (_RECENT_TOOL_OUTPUT_CHAR_CAP + 1000)
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "please build me a workflow"},
            {"type": "function_call_output", "call_id": "call-big", "output": oversized},
        ]
        with structlog.testing.capture_logs() as logs:
            result = copilot_call_model_input_filter(_mk_input_data(items))
        outputs = [it for it in result.input if it.get("type") == "function_call_output"]
        assert outputs[0]["output"].endswith("... [truncated]")
        assert any(entry["event"] == "copilot_recent_tool_output_truncated" for entry in logs)

    def test_emergency_truncation_logs_distinct_event_with_count(self) -> None:
        import structlog.testing

        from skyvern.forge.sdk.copilot.session_factory import make_copilot_call_model_input_filter

        items: list[dict[str, Any]] = [
            {"role": "user", "content": "please build me a workflow"},
            *({"type": "function_call_output", "call_id": f"call-{i}", "output": "x" * 5000} for i in range(3)),
            {"type": "function_call_output", "call_id": "call-small", "output": "ok"},
        ]
        tight_filter = make_copilot_call_model_input_filter(token_budget=200)
        with structlog.testing.capture_logs() as logs:
            tight_filter(_mk_input_data(items))
        emergency = [entry for entry in logs if entry["event"] == "copilot_tool_output_emergency_truncated"]
        assert [entry["cap"] for entry in emergency] == [2000, 300]
        assert all(entry["truncated_count"] >= 2 for entry in emergency)

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

    def test_filter_summarizes_older_function_call_args_on_first_turn(self) -> None:
        """F3/CORR-2 guard: older `function_call` items get their bulky
        ``arguments`` payload (e.g. a full workflow YAML) compacted, exactly
        as ``_prune_input_list`` does today in the non-session path."""
        from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

        huge_yaml = "title: workflow\n" + ("  block: xxxxxxxxxxxxxxxxxxxx\n" * 500)
        items: list[dict[str, Any]] = [
            {"role": "user", "content": "build a workflow"},
            # six function_call items; the last 3 stay raw, older 3 get summarized.
            *(
                {
                    "type": "function_call",
                    "name": "update_workflow",
                    "call_id": f"fc-{i}",
                    "arguments": json.dumps({"workflow_yaml": huge_yaml}),
                }
                for i in range(6)
            ),
        ]
        result = copilot_call_model_input_filter(_mk_input_data(items))
        calls = [it for it in result.input if it.get("type") == "function_call"]
        assert len(calls) == 6
        older_three = calls[:3]
        recent_three = calls[3:]
        for older in older_three:
            assert "_summarized" in older["arguments"]
            assert len(older["arguments"]) < len(huge_yaml)
        for recent in recent_three:
            assert "_summarized" not in recent["arguments"]
            assert json.loads(recent["arguments"])["workflow_yaml"] == huge_yaml


class TestSessionInputCallback:
    def test_empty_history_returns_new_items(self) -> None:
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        new_items = [{"role": "user", "content": "hello"}]
        assert copilot_session_input_callback([], new_items) == new_items

    def test_preserves_original_goal_and_applies_compaction_to_middle(self) -> None:
        """First-turn shape (one real user, several tool iterations): the
        goal at index 0 is preserved; older function_call_output items get
        compacted; the last KEEP_RECENT_TOOL_OUTPUTS stay raw."""
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        goal = {"role": "user", "content": "please build me a workflow"}
        tool_items = [
            {
                "type": "function_call_output",
                "call_id": f"c-{i}",
                "output": json.dumps({"ok": True, "data": {"blob": "y" * 4000}}),
            }
            for i in range(5)
        ]
        new = [{"role": "user", "content": "[copilot:nudge] please finish"}]

        combined = copilot_session_input_callback([goal, *tool_items], new)
        assert combined[0] == goal
        # older items (first 2 of 5) compact; last 3 stay raw.
        tool_outputs_in_combined = [it for it in combined if it.get("type") == "function_call_output"]
        assert len(tool_outputs_in_combined) == 5
        assert "_summarized" in tool_outputs_in_combined[0]["output"]
        assert "_summarized" in tool_outputs_in_combined[1]["output"]
        for recent in tool_outputs_in_combined[2:]:
            assert "_summarized" not in recent["output"]

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
        from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

        monkeypatch.setenv("COPILOT_DUMP_MODEL_INPUTS", str(tmp_path))
        items = [
            {"role": "user", "content": "output the number of azure errors"},
            {"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'},
        ]

        copilot_call_model_input_filter(_mk_input_data(items, instructions="SYSTEM PROMPT"))

        dumps = sorted(tmp_path.glob("call-*.json"))
        assert len(dumps) == 1
        payload = json.loads(dumps[0].read_text())
        assert payload["instructions"] == "SYSTEM PROMPT"
        assert payload["input"] == items
        # A context the derivation helper cannot read must not cost the run its model call.
        assert payload["requested_output_paths"] == []

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


class TestSynthesizedOfferCollapse:
    """The model-input filter keeps at most one synthesized-block offer, and
    offers do not count as real user turns for the session callback's
    recent-turn boundary."""

    @staticmethod
    def _offer(body: str) -> dict[str, Any]:
        from skyvern.forge.sdk.copilot.code_block_synthesis import SYNTHESIZED_OFFER_SENTINEL

        return {"role": "user", "content": SYNTHESIZED_OFFER_SENTINEL + " " + body}

    def test_filter_keeps_at_most_one_offer(self) -> None:
        from skyvern.forge.sdk.copilot.session_factory import copilot_call_model_input_filter

        code = "await page.goto('https://a.example')\n" * 200
        items: list[dict[str, Any]] = [{"role": "user", "content": "build me a workflow"}]
        for i in range(6):
            items.append(self._offer(f"v{i} " + code))
            items.append({"type": "function_call", "call_id": f"c-{i}", "name": "evaluate", "arguments": "{}"})
            items.append({"type": "function_call_output", "call_id": f"c-{i}", "output": '{"ok": true}'})

        result = copilot_call_model_input_filter(_mk_input_data(items))
        offers = [
            it for it in result.input if isinstance(it, dict) and "SYNTHESIZED CODE BLOCK" in str(it.get("content"))
        ]
        assert len(offers) == 1
        assert offers[0]["content"].startswith("SYNTHESIZED CODE BLOCK")
        assert "v5 " in offers[0]["content"]

    def test_production_shape_stale_offers_in_history_fresh_offer_in_new_items(self) -> None:
        """The shape the enforcement loop actually emits: prior offers sit in session
        history while the refreshed offer arrives beside the screenshot and nudge, and
        only the refreshed one may reach the model."""
        from skyvern.forge.sdk.copilot.enforcement import NUDGE_SENTINEL
        from skyvern.forge.sdk.copilot.session_factory import (
            copilot_call_model_input_filter,
            copilot_session_input_callback,
        )

        history: list[dict[str, Any]] = [
            {"role": "user", "content": "please build me a workflow"},
            self._offer("stale one"),
            {"type": "function_call", "call_id": "c-1", "name": "evaluate", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c-1", "output": '{"ok": true}'},
            self._offer("stale two"),
        ]
        new_items: list[dict[str, Any]] = [
            self._offer("fresh"),
            self._screenshot("live frame"),
            {"role": "user", "content": NUDGE_SENTINEL + "keep going"},
        ]

        merged = copilot_session_input_callback(history, new_items)
        result = copilot_call_model_input_filter(_mk_input_data(merged))

        offers = [
            it for it in result.input if isinstance(it, dict) and "SYNTHESIZED CODE BLOCK" in str(it.get("content"))
        ]
        assert len(offers) == 1
        assert "fresh" in offers[0]["content"]

    def test_offers_do_not_hold_recent_real_turn_slots(self) -> None:
        """Offers interleaved with the tool chain used to hold the recent-real-turn
        slots and shield everything after them from middle-region compaction; as
        synthetic messages the older tool outputs compact while pairing survives."""
        from skyvern.forge.sdk.copilot.enforcement import KEEP_RECENT_TOOL_OUTPUTS
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        goal = {"role": "user", "content": "please build me a workflow"}

        def pair(i: int) -> list[dict[str, Any]]:
            return [
                {
                    "type": "function_call",
                    "call_id": f"c-{i}",
                    "name": "evaluate",
                    "arguments": json.dumps({"blob": "z" * 4000}),
                },
                {
                    "type": "function_call_output",
                    "call_id": f"c-{i}",
                    "output": json.dumps({"ok": True, "data": {"blob": "z" * 4000}}),
                },
            ]

        history: list[dict[str, Any]] = [goal, *pair(0), *pair(1), self._offer("stale")]
        history += [*pair(2), *pair(3), self._offer("fresh"), *pair(4), *pair(5)]

        combined = copilot_session_input_callback(history, [{"role": "user", "content": "[copilot:nudge] finish"}])

        outputs = [it for it in combined if isinstance(it, dict) and it.get("type") == "function_call_output"]
        assert len(outputs) == 6
        for older in outputs[:-KEEP_RECENT_TOOL_OUTPUTS]:
            assert "_summarized" in older["output"]
        for recent in outputs[-KEEP_RECENT_TOOL_OUTPUTS:]:
            assert "_summarized" not in recent["output"]
        # Pairing survives: every retained output still has its call.
        call_ids = {it["call_id"] for it in combined if isinstance(it, dict) and it.get("type") == "function_call"}
        assert {out["call_id"] for out in outputs} <= call_ids

    @staticmethod
    def _screenshot(label: str) -> dict[str, Any]:
        from skyvern.forge.sdk.copilot.enforcement import SCREENSHOT_SENTINEL

        return {
            "role": "user",
            "content": [
                {"type": "input_text", "text": SCREENSHOT_SENTINEL + " " + label},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ],
        }

    def test_newest_history_screenshot_survives_middle_compaction(self) -> None:
        """With every injected message synthetic the whole post-goal history is
        middle, and the newest frame must not collapse to a placeholder."""
        from skyvern.forge.sdk.copilot.enforcement import is_screenshot_message
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        history = [
            {"role": "user", "content": "please build me a workflow"},
            self._screenshot("old frame"),
            {"type": "function_call", "call_id": "c-1", "name": "evaluate", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c-1", "output": "{}"},
            self._screenshot("newest frame"),
            self._offer("fresh"),
        ]
        combined = copilot_session_input_callback(history, [{"role": "user", "content": "[copilot:nudge] finish"}])
        frames = [it for it in combined if is_screenshot_message(it) and isinstance(it.get("content"), list)]
        assert len(frames) == 1
        assert "newest frame" in frames[0]["content"][0]["text"]

    def test_text_only_screenshot_lookalike_cannot_shadow_the_real_frame(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import SCREENSHOT_SENTINEL, is_screenshot_message
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        history = [
            {"role": "user", "content": "please build me a workflow"},
            self._screenshot("real frame"),
            {"role": "user", "content": SCREENSHOT_SENTINEL + " text-only lookalike"},
            self._offer("fresh"),
        ]
        combined = copilot_session_input_callback(history, [{"role": "user", "content": "[copilot:nudge] finish"}])
        frames = [it for it in combined if is_screenshot_message(it) and isinstance(it.get("content"), list)]
        assert len(frames) == 1
        assert "real frame" in frames[0]["content"][0]["text"]

    def test_history_screenshot_yields_to_newer_frame_in_new_items(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import is_screenshot_message
        from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback

        history = [
            {"role": "user", "content": "please build me a workflow"},
            self._screenshot("old frame"),
            self._offer("fresh"),
        ]
        combined = copilot_session_input_callback(history, [self._screenshot("live frame")])
        list_frames = [it for it in combined if is_screenshot_message(it) and isinstance(it.get("content"), list)]
        assert len(list_frames) == 1
        assert "live frame" in list_frames[0]["content"][0]["text"]
