"""Tests for the copilot session callback + call-model input filter."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


def _mk_input_data(items: list[Any]) -> Any:
    """Build a fake CallModelData payload with a model_data.input list."""
    return SimpleNamespace(model_data=SimpleNamespace(input=list(items), instructions=None))


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
