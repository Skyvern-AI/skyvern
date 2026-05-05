"""Tests for enforcement pruning and null-data handling.

These cover three regressions observed in trace 019d7b5c884dff0ff648680b9f31f715:
  1. Extraction returning all-null fields was treated as success.
  2. Context grew linearly because old tool outputs kept full content.
  3. No escalation when the agent looped on the same null-data failure.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot.enforcement import (
    KEEP_RECENT_TOOL_OUTPUTS,
    NULL_DATA_STREAK_ESCALATE_AT,
    POST_REPEATED_NULL_DATA_NUDGE,
    POST_SUSPICIOUS_SUCCESS_NUDGE,
    _check_enforcement,
    _needs_repeated_null_data_nudge,
    _needs_suspicious_success_nudge,
    _prune_input_list,
    _summarize_tool_output,
)
from skyvern.forge.sdk.copilot.tools import (
    _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY,
    _analyze_run_blocks,
    _is_meaningful_extracted_data,
    _record_run_blocks_result,
)


class _Ctx:
    """Minimal stand-in for CopilotContext used in enforcement checks.

    Keep this in sync with ``AgentContext`` enforcement-state fields — missing
    attributes would show up as AttributeError in the branches that use bare
    access rather than ``getattr``.
    """

    def __init__(self) -> None:
        self.navigate_called = False
        self.observation_after_navigate = False
        self.navigate_enforcement_done = False
        self.update_workflow_called = False
        self.test_after_update_done = False
        self.post_update_nudge_count = 0
        self.coverage_nudge_count = 0
        self.format_nudge_count = 0
        self.user_message = ""
        self.last_update_block_count = None
        self.last_test_ok = None
        self.last_test_failure_reason = None
        self.last_test_suspicious_success = False
        self.last_test_anti_bot = None
        self.last_failure_category_top = None
        self.failed_test_nudge_count = 0
        self.explore_without_workflow_nudge_count = 0
        self.null_data_streak_count = 0
        self.repeated_failure_streak_count = 0
        self.repeated_failure_nudge_emitted_at_streak = 0


# ---------------------------------------------------------------------------
# _is_meaningful_extracted_data
# ---------------------------------------------------------------------------


def test_meaningful_data_none() -> None:
    assert _is_meaningful_extracted_data(None) is False


def test_meaningful_data_empty_dict() -> None:
    assert _is_meaningful_extracted_data({}) is False


def test_meaningful_data_all_null_dict() -> None:
    # The regression: {"price": None} used to count as meaningful because
    # the dict itself is truthy. It must NOT count as meaningful.
    assert _is_meaningful_extracted_data({"price": None}) is False


def test_meaningful_data_nested_all_null() -> None:
    assert _is_meaningful_extracted_data({"a": None, "b": {"c": None}}) is False


def test_meaningful_data_one_real_value() -> None:
    assert _is_meaningful_extracted_data({"price": "260.48", "other": None}) is True


def test_meaningful_data_empty_list() -> None:
    assert _is_meaningful_extracted_data([]) is False


def test_meaningful_data_list_of_nulls() -> None:
    assert _is_meaningful_extracted_data([None, None]) is False


def test_meaningful_data_scalar_zero() -> None:
    # A literal 0 is still meaningful output — it's a value, not absence of data.
    assert _is_meaningful_extracted_data(0) is True


def test_meaningful_data_empty_string() -> None:
    assert _is_meaningful_extracted_data("") is False


def test_meaningful_data_string() -> None:
    assert _is_meaningful_extracted_data("$260.48") is True


# ---------------------------------------------------------------------------
# _analyze_run_blocks — envelope-unwrap for EXTRACTION blocks
#
# ExtractionBlock stores TaskOutput.from_task() on block.output. Envelope
# fields (task_id, status, *_screenshot_artifact_ids) are always populated on
# a completed run and would short-circuit _is_meaningful_extracted_data to
# True even when the real payload fields (extracted_information,
# downloaded_files, downloaded_file_urls) are empty. The meaningful-data
# check must judge against the payload slice, not the envelope.
# ---------------------------------------------------------------------------


_EMPTY_EXTRACTION_ENVELOPE: dict[str, Any] = {
    "task_id": "tsk_00000000000000000001",
    "status": "completed",
    "extracted_information": [],
    "failure_reason": None,
    "errors": [],
    "failure_category": None,
    "downloaded_files": [],
    "downloaded_file_urls": None,
    "task_screenshots": None,
    "workflow_screenshots": None,
    "task_screenshot_artifact_ids": ["a_00000000000000000001", "a_00000000000000000002"],
    "workflow_screenshot_artifact_ids": ["a_00000000000000000001", "a_00000000000000000003"],
}


def _run_result(blocks: list[dict[str, Any]], ok: bool = True) -> dict[str, Any]:
    return {"ok": ok, "data": {"blocks": blocks}}


def _envelope(**overrides: Any) -> dict[str, Any]:
    """Return a fresh copy of the empty-extraction envelope with field overrides."""
    return {**_EMPTY_EXTRACTION_ENVELOPE, **overrides}


def _extraction_block(extracted_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": "extract_flights",
        "block_type": "EXTRACTION",
        "status": "completed",
        "extracted_data": extracted_data,
    }


def _text_prompt_block(extracted_data: Any) -> dict[str, Any]:
    return {
        "label": "summarize",
        "block_type": "TEXT_PROMPT",
        "status": "completed",
        "extracted_data": extracted_data,
    }


# Case id -> (envelope overrides, expected empty_data_blocks)
#
# empty_payload_trace_repro: extracted_information=[], downloaded_files=[],
#   downloaded_file_urls=None, envelope metadata populated. Envelope-as-a-whole
#   is truthy; real payload is empty; gate must flip. (SKY-9143 repro.)
# download_only_files / download_only_urls: legitimate extraction success where the
#   block produced files but no structured payload — must NOT flip the gate.
_EXTRACTION_ENVELOPE_CASES: list[tuple[str, dict[str, Any], bool]] = [
    ("empty_payload_trace_repro", {}, True),
    ("real_extraction", {"extracted_information": [{"price": "260.48"}]}, False),
    (
        "download_only_files",
        {"downloaded_files": [{"url": "https://example.com/a.pdf", "checksum": "abc123"}]},
        False,
    ),
    (
        "download_only_urls",
        {"extracted_information": None, "downloaded_file_urls": ["https://example.com/a.pdf"]},
        False,
    ),
]


@pytest.mark.parametrize(
    "overrides,expected_empty",
    [(ovr, exp) for _, ovr, exp in _EXTRACTION_ENVELOPE_CASES],
    ids=[case_id for case_id, _, _ in _EXTRACTION_ENVELOPE_CASES],
)
def test_analyze_extraction_envelope(overrides: dict[str, Any], expected_empty: bool) -> None:
    _, empty, _ = _analyze_run_blocks(_run_result([_extraction_block(_envelope(**overrides))]))
    assert empty is expected_empty


def test_analyze_text_prompt_default_schema_is_not_empty() -> None:
    # TEXT_PROMPT blocks return the raw LLM response dict (no Task envelope).
    # Default schema is {"llm_response": "<text>"}.
    _, empty, _ = _analyze_run_blocks(_run_result([_text_prompt_block({"llm_response": "the sentiment is positive"})]))
    assert empty is False


def test_analyze_text_prompt_user_schema_named_extracted_information_is_not_sliced() -> None:
    # Guard against a too-broad unwrap: a user's json_schema may name a
    # top-level field "extracted_information". The helper must not mistake
    # that for an EXTRACTION envelope and discard sibling fields.
    block = _text_prompt_block({"extracted_information": "ignored because this is TEXT_PROMPT", "summary": "x"})
    _, empty, _ = _analyze_run_blocks(_run_result([block]))
    assert empty is False


def test_analyze_text_prompt_all_null_is_empty() -> None:
    # Symmetric to {"price": None} — a text-prompt response with all-null
    # fields counts as no meaningful output.
    _, empty, _ = _analyze_run_blocks(_run_result([_text_prompt_block({"summary": None})]))
    assert empty is True


# ---------------------------------------------------------------------------
# _record_run_blocks_result — end-to-end flip of last_test_ok on empty envelope
# ---------------------------------------------------------------------------


def _fresh_ctx_for_record() -> Any:
    """SimpleNamespace shaped for _record_run_blocks_result + update_repeated_failure_state.

    Uses getattr-with-default-compatible defaults so the function under test
    populates the interesting fields without tripping AttributeError on the
    downstream update_repeated_failure_state call.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        last_test_ok=True,
        last_test_failure_reason=None,
        last_test_suspicious_success=False,
        last_test_anti_bot=None,
        last_failure_category_top=None,
        last_test_non_retriable_nav_error=None,
        null_data_streak_count=0,
        failed_test_nudge_count=0,
        last_failed_workflow_yaml=None,
        non_retriable_nav_error_last_emitted_signature=None,
        workflow_yaml=None,
        last_workflow=None,
        last_frontier_start_label=None,
        last_executed_block_labels=[],
        last_failure_signature=None,
        last_frontier_fingerprint=None,
        repeated_failure_streak_count=0,
        repeated_failure_nudge_emitted_at_streak=0,
        pending_action_sequence_fingerprint=None,
        last_action_sequence_fingerprint=None,
        repeated_action_fingerprint_streak_count=0,
        copilot_total_timeout_exceeded=False,
    )


def test_record_run_blocks_result_flips_last_test_ok_on_empty_extraction_envelope() -> None:
    # End-to-end: a run reporting ok=true but whose sole EXTRACTION block
    # produced the empty envelope must push last_test_ok from True to None,
    # so _verified_workflow_or_none blocks the proposal. This is the user-
    # visible SKY-9143 regression.
    ctx = _fresh_ctx_for_record()
    result = _run_result([_extraction_block(_envelope())])
    _record_run_blocks_result(ctx, result)
    assert ctx.last_test_ok is None
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_test_failure_reason is not None


def test_record_run_blocks_result_keeps_failure_when_watchdog_cancel_without_timeout() -> None:
    """Stagnation/ceiling cancels mid-session must still set last_test_ok=False
    so the failed-test nudge can fire — only a coincident total timeout softens
    to ``None`` for the unvalidated WIP rescue path."""
    ctx = _fresh_ctx_for_record()
    result = {
        "ok": False,
        "error": "Run ID: wr_stagnation. Stuck.",
        _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY: True,
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is False
    assert ctx.last_test_failure_reason == "Run ID: wr_stagnation. Stuck."


def test_record_run_blocks_result_sets_last_test_ok_none_on_watchdog_cancel_at_timeout() -> None:
    ctx = _fresh_ctx_for_record()
    ctx.copilot_total_timeout_exceeded = True
    result = {
        "ok": False,
        "error": "Run ID: wr_timeout. Outcome is uncertain.",
        _INTERNAL_RUN_CANCELLED_BY_WATCHDOG_KEY: True,
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is None
    assert ctx.last_test_failure_reason == "Run ID: wr_timeout. Outcome is uncertain."


# ---------------------------------------------------------------------------
# Repeated null-data escalation
# ---------------------------------------------------------------------------


def test_suspicious_success_fires_when_flag_set() -> None:
    ctx = _Ctx()
    ctx.last_test_suspicious_success = True
    ctx.null_data_streak_count = 1
    assert _needs_suspicious_success_nudge(ctx) is True
    assert _needs_repeated_null_data_nudge(ctx) is False


def test_repeated_null_data_fires_at_threshold() -> None:
    ctx = _Ctx()
    ctx.last_test_suspicious_success = True
    ctx.null_data_streak_count = NULL_DATA_STREAK_ESCALATE_AT
    assert _needs_repeated_null_data_nudge(ctx) is True


def test_check_enforcement_returns_repeated_nudge_at_threshold() -> None:
    ctx = _Ctx()
    ctx.last_test_suspicious_success = True
    ctx.null_data_streak_count = NULL_DATA_STREAK_ESCALATE_AT
    nudge = _check_enforcement(ctx)
    assert nudge == POST_REPEATED_NULL_DATA_NUDGE


def test_check_enforcement_returns_regular_suspicious_nudge_below_threshold() -> None:
    ctx = _Ctx()
    ctx.last_test_suspicious_success = True
    ctx.null_data_streak_count = 1
    nudge = _check_enforcement(ctx)
    assert nudge == POST_SUSPICIOUS_SUCCESS_NUDGE


def test_repeated_null_data_requires_suspicious_flag() -> None:
    # If the current test wasn't a suspicious success, don't fire even with a high streak.
    ctx = _Ctx()
    ctx.last_test_suspicious_success = False
    ctx.null_data_streak_count = 99
    assert _needs_repeated_null_data_nudge(ctx) is False


# ---------------------------------------------------------------------------
# Tool-output pruning
# ---------------------------------------------------------------------------


def _fco(call_id: str, output: str) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def test_recent_outputs_preserved_full() -> None:
    # Build KEEP_RECENT_TOOL_OUTPUTS + 1 items so exactly one is "old".
    items = []
    short = '{"ok":true,"data":{"overall_status":"completed"}}'
    for i in range(KEEP_RECENT_TOOL_OUTPUTS + 1):
        items.append(_fco(f"c{i}", short))

    pruned = _prune_input_list(items)
    # Each recent item is unchanged (they're all short and JSON).
    for i in range(1, KEEP_RECENT_TOOL_OUTPUTS + 1):
        assert pruned[i]["output"] == short


def test_old_large_output_is_summarized() -> None:
    # An older, large JSON tool output gets compressed into a synopsis.
    heavy_payload = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_123",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "open_quote_page",
                    "status": "completed",
                    "block_type": "GOTO_URL",
                    "extracted_data": None,
                },
                {
                    "label": "extract_stock_price",
                    "status": "completed",
                    "block_type": "EXTRACTION",
                    "extracted_data": {"price": None},
                    "failure_reason": None,
                },
            ],
            "visible_elements_html": "<html>" + ("x" * 4000) + "</html>",
            "screenshot_base64": "[base64 image omitted]",
        },
    }
    heavy_output = json.dumps(heavy_payload)
    assert len(heavy_output) > 4000

    items = [_fco("c_old", heavy_output)]
    # Add enough recent outputs to push the first one out of the recent window.
    for i in range(KEEP_RECENT_TOOL_OUTPUTS):
        items.append(_fco(f"c_new_{i}", '{"ok":true,"data":{"overall_status":"completed"}}'))

    pruned = _prune_input_list(items)
    summarized = pruned[0]["output"]
    # The summary must be drastically shorter than the original.
    assert len(summarized) < 1000
    # It must preserve the key signal fields so the agent can still reason about past calls.
    parsed = json.loads(summarized)
    assert parsed["ok"] is True
    assert parsed["overall_status"] == "completed"
    assert parsed["workflow_run_id"] == "wr_123"
    assert parsed["_summarized"]
    assert len(parsed["blocks"]) == 2
    assert parsed["blocks"][1]["label"] == "extract_stock_price"
    assert parsed["blocks"][1]["status"] == "completed"


def test_summarize_non_json_output_falls_back_to_head_truncation() -> None:
    text = "not-json " * 1000
    result = _summarize_tool_output(text)
    assert len(result) < len(text)
    assert result.startswith("not-json")
    assert "older tool output truncated" in result


def test_summarize_short_output_is_unchanged() -> None:
    assert _summarize_tool_output("small") == "small"


def test_recent_large_output_is_head_truncated_not_summarized() -> None:
    # Big JSON in the most-recent slot should be head-truncated at 2000 chars,
    # NOT replaced with a summary.
    large = '{"ok":true,"data":{"value":"' + ("y" * 3000) + '"}}'
    items = [_fco("c_recent", large)]
    pruned = _prune_input_list(items)
    out = pruned[0]["output"]
    assert out.startswith('{"ok":true,')
    assert out.endswith("\n... [truncated]")
    assert len(out) <= 2020


class TestEnforcement:
    def _make_ctx(self, **overrides: Any) -> Any:
        """Create a mock context with enforcement attributes."""
        ctx = MagicMock()
        ctx.navigate_called = False
        ctx.observation_after_navigate = False
        ctx.navigate_enforcement_done = False
        ctx.update_workflow_called = False
        ctx.test_after_update_done = False
        ctx.post_update_nudge_count = 0
        ctx.coverage_nudge_count = 0
        ctx.format_nudge_count = 0
        ctx.explore_without_workflow_nudge_count = 0
        ctx.last_test_suspicious_success = False
        ctx.last_test_anti_bot = None
        for k, v in overrides.items():
            setattr(ctx, k, v)
        return ctx

    @staticmethod
    def _reply_result(user_response: str = "") -> Any:
        """Build a RunResultStreaming-shaped mock whose final_output parses as REPLY."""
        import json

        result = MagicMock()
        result.final_output = json.dumps({"type": "REPLY", "user_response": user_response})
        result.new_items = []
        return result

    @staticmethod
    def _empty_result() -> Any:
        """Build a mock with no final text — triggers the 'not sure how to help' fallback."""
        result = MagicMock()
        result.final_output = None
        result.new_items = []
        return result

    def test_no_enforcement_when_nothing_pending(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx()
        assert _check_enforcement(ctx) is None

    def test_post_navigate_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(navigate_called=True, observation_after_navigate=False)
        nudge = _check_enforcement(ctx)
        assert nudge is not None
        assert "observe" in nudge.lower() or "inspect" in nudge.lower()
        assert ctx.navigate_enforcement_done is True

    def test_post_navigate_only_fires_once(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            navigate_enforcement_done=True,
        )
        assert _check_enforcement(ctx) is None

    def test_post_update_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(update_workflow_called=True, test_after_update_done=False)
        nudge = _check_enforcement(ctx)
        assert nudge is not None
        assert "test" in nudge.lower() or "run_blocks" in nudge.lower()

    def test_navigate_takes_priority_over_update(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=True,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        assert "observe" in nudge.lower() or "inspect" in nudge.lower()

    def test_intermediate_success_nudge_for_multistep_goal(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr and then download all french regulations",
            coverage_nudge_count=0,
        )
        from skyvern.forge.sdk.copilot.enforcement import POST_INTERMEDIATE_SUCCESS_NUDGE

        # Coverage gate only fires when the model tries to emit a REPLY.
        nudge = _check_enforcement(ctx, self._reply_result("draft response"))
        assert nudge == POST_INTERMEDIATE_SUCCESS_NUDGE
        assert ctx.coverage_nudge_count == 1

    def test_no_intermediate_success_nudge_for_single_step_goal(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr",
            coverage_nudge_count=0,
        )
        assert _check_enforcement(ctx, self._reply_result("done")) is None

    def test_intermediate_success_nudge_fires_for_two_blocks(self) -> None:
        """Key regression: nudge must fire even when block_count > 1."""
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=2,
            user_message="Go to france.fr and then download all french regulations and extract the titles",
            coverage_nudge_count=0,
        )
        from skyvern.forge.sdk.copilot.enforcement import POST_INTERMEDIATE_SUCCESS_NUDGE

        nudge = _check_enforcement(ctx, self._reply_result("two-block draft"))
        assert nudge == POST_INTERMEDIATE_SUCCESS_NUDGE

    def test_intermediate_nudge_respects_global_cap(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import MAX_INTERMEDIATE_NUDGES, _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=2,
            user_message="Go to france.fr and then download all french regulations",
            coverage_nudge_count=MAX_INTERMEDIATE_NUDGES,
        )
        assert _check_enforcement(ctx, self._reply_result("capped")) is None

    def test_intermediate_nudge_does_not_fire_for_ten_plus_blocks(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=10,
            user_message="Go to france.fr and then download all french regulations",
            coverage_nudge_count=0,
        )
        assert _check_enforcement(ctx, self._reply_result("ten blocks")) is None

    def test_ask_question_always_passes_even_with_coverage_gap(self) -> None:
        """Regression guard: ASK_QUESTION must never be blocked by coverage."""
        import json

        from skyvern.forge.sdk.copilot.enforcement import _check_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=True,
            last_test_ok=True,
            last_update_block_count=1,
            user_message="Go to france.fr and then download all french regulations",
            coverage_nudge_count=0,
        )
        ask = MagicMock()
        ask.final_output = json.dumps({"type": "ASK_QUESTION", "user_response": "Which source?"})
        ask.new_items = []
        assert _check_enforcement(ctx, ask) is None

    def test_explore_without_workflow_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE, _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        assert nudge == POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE
        assert ctx.explore_without_workflow_nudge_count == 1

    def test_explore_without_workflow_not_when_update_called(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import (
            POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE,
            POST_UPDATE_NUDGE,
            _check_enforcement,
        )

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=True,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        assert nudge == POST_UPDATE_NUDGE
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE
        assert ctx.explore_without_workflow_nudge_count == 0

    def test_explore_without_workflow_not_when_test_done(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE, _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=True,
        )
        nudge = _check_enforcement(ctx)
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE

    def test_explore_without_workflow_respects_cap(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import (
            MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES,
            POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE,
            _check_enforcement,
        )

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=True,
            update_workflow_called=False,
            test_after_update_done=False,
            explore_without_workflow_nudge_count=MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES,
        )
        nudge = _check_enforcement(ctx)
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE

    def test_explore_without_workflow_not_without_observation(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE, _check_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=False,
            test_after_update_done=False,
        )
        nudge = _check_enforcement(ctx)
        # Should get navigate nudge, not explore-without-workflow
        assert nudge != POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE
        assert ctx.explore_without_workflow_nudge_count == 0

    @pytest.mark.asyncio
    async def test_post_navigate_nudge_does_not_increment_post_update_counter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from skyvern.forge.sdk.copilot.enforcement import run_with_enforcement

        ctx = self._make_ctx(
            navigate_called=True,
            observation_after_navigate=False,
            update_workflow_called=False,
            post_update_nudge_count=0,
        )
        stream = MagicMock()
        stream.is_disconnected = AsyncMock(return_value=False)

        call_count = {"count": 0}

        # final_output=None + new_items=[] makes extract_final_text return "",
        # which parses to a REPLY fallback — safe for the response-peek path
        # when the state-based branches may or may not short-circuit first.
        fake_result = self._empty_result()
        fake_result.to_input_list.return_value = []

        def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
            call_count["count"] += 1
            return fake_result

        async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
            # Resolve post-navigate enforcement on second pass.
            if call_count["count"] >= 2:
                c.observation_after_navigate = True

        monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
            fake_stream_to_sse,
        )

        returned = await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
        assert returned is fake_result
        assert ctx.post_update_nudge_count == 0

    @pytest.mark.asyncio
    async def test_post_update_nudge_increments_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot.enforcement import run_with_enforcement

        ctx = self._make_ctx(
            update_workflow_called=True,
            test_after_update_done=False,
            post_update_nudge_count=0,
        )
        stream = MagicMock()
        stream.is_disconnected = AsyncMock(return_value=False)

        call_count = {"count": 0}
        fake_result = self._empty_result()
        fake_result.to_input_list.return_value = []

        def fake_run_streamed(*args: Any, **kwargs: Any) -> Any:
            call_count["count"] += 1
            return fake_result

        async def fake_stream_to_sse(result: Any, s: Any, c: Any) -> None:
            # Resolve post-update enforcement on second pass.
            if call_count["count"] >= 2:
                c.test_after_update_done = True

        monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed", fake_run_streamed)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
            fake_stream_to_sse,
        )

        returned = await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
        assert returned is fake_result
        assert ctx.post_update_nudge_count == 1


class TestGoalLikelyNeedsMoreBlocks:
    @staticmethod
    def _check(user_message: str, block_count: int) -> bool:
        from skyvern.forge.sdk.copilot.enforcement import _goal_likely_needs_more_blocks

        return _goal_likely_needs_more_blocks(user_message, block_count)

    def test_navigate_and_download_needs_two(self) -> None:
        assert self._check("Go to france.fr and then download regulations", 1) is True
        assert self._check("Go to france.fr and then download regulations", 2) is False

    def test_login_search_and_extract_needs_three(self) -> None:
        assert self._check("Login to the site, search for products, and extract prices", 1) is True
        assert self._check("Login to the site, search for products, and extract prices", 2) is True
        assert self._check("Login to the site, search for products, and extract prices", 3) is False

    def test_single_action_does_not_need_more(self) -> None:
        assert self._check("Go to france.fr", 1) is False

    def test_sequential_connector_needs_at_least_two(self) -> None:
        assert self._check("Do X and then do Y", 1) is True

    def test_ten_plus_blocks_always_false(self) -> None:
        assert self._check("Go to X and then download Y and extract Z", 10) is False

    def test_non_string_returns_false(self) -> None:
        assert self._check(None, 1) is False  # type: ignore[arg-type]
        assert self._check(123, 1) is False  # type: ignore[arg-type]
