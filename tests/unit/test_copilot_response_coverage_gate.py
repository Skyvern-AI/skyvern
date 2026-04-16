"""Tests for the response-aware coverage gate in _check_enforcement.

Covers a regression where a 2-action user goal slipped past enforcement
because the old `premature_completion_nudge_done` latch was bypassed by a
no-op turn (model emits REPLY JSON without calling any update tool). The
new gate peeks at the final response text to distinguish REPLY with a
coverage gap (nudge), REPLY with progress-narration prose (nudge), and
ASK_QUESTION (always allowed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from skyvern.forge.sdk.copilot.enforcement import (
    MAX_FORMAT_NUDGES,
    MAX_INTERMEDIATE_NUDGES,
    POST_FORMAT_NUDGE,
    POST_INTERMEDIATE_SUCCESS_NUDGE,
    _check_enforcement,
    _is_progress_narration,
    _response_coverage_nudge,
)


class _Ctx:
    """Minimal stand-in for CopilotContext used in enforcement checks."""

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
        self.failed_test_nudge_count = 0
        self.explore_without_workflow_nudge_count = 0
        self.null_data_streak_count = 0
        self.repeated_failure_streak_count = 0
        self.repeated_failure_nudge_emitted_at_streak = 0


@dataclass
class _FakeRunResult:
    """Stand-in for RunResultStreaming — exposes only what extract_final_text uses."""

    final_output: Any = None
    new_items: list[Any] = field(default_factory=list)


def _reply_result(user_response: str) -> _FakeRunResult:
    return _FakeRunResult(
        final_output=json.dumps({"type": "REPLY", "user_response": user_response}),
    )


def _ask_question_result(question: str) -> _FakeRunResult:
    return _FakeRunResult(
        final_output=json.dumps({"type": "ASK_QUESTION", "user_response": question}),
    )


def _post_success_ctx(user_message: str, block_count: int = 1) -> _Ctx:
    """Build a ctx in the 'workflow test passed' state that would previously
    have triggered the intermediate-success nudge."""
    ctx = _Ctx()
    ctx.user_message = user_message
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.last_test_ok = True
    ctx.last_update_block_count = block_count
    return ctx


# ---------------------------------------------------------------------------
# _response_coverage_nudge — direct unit tests
# ---------------------------------------------------------------------------


def test_reply_with_coverage_gap_fires_nudge() -> None:
    ctx = _post_success_ctx("go to example.com and download the regulations")
    parsed = {"type": "REPLY", "user_response": "I created a nav block."}
    nudge = _response_coverage_nudge(ctx, parsed)
    assert nudge == POST_INTERMEDIATE_SUCCESS_NUDGE
    assert ctx.coverage_nudge_count == 1


def test_coverage_nudge_respects_counter_cap() -> None:
    ctx = _post_success_ctx("go to X and download Y")
    parsed = {"type": "REPLY", "user_response": "one block draft"}
    for _ in range(MAX_INTERMEDIATE_NUDGES):
        assert _response_coverage_nudge(ctx, parsed) == POST_INTERMEDIATE_SUCCESS_NUDGE
    # One more call — the cap should now let the response through.
    assert _response_coverage_nudge(ctx, parsed) is None


def test_ask_question_always_passes_through_even_with_coverage_gap() -> None:
    ctx = _post_success_ctx("go to site and download file")
    parsed = {"type": "ASK_QUESTION", "user_response": "Which file do you mean?"}
    assert _response_coverage_nudge(ctx, parsed) is None
    assert ctx.coverage_nudge_count == 0


def test_reply_without_coverage_gap_passes_through() -> None:
    # 2-action goal and 2 blocks — no gap.
    ctx = _post_success_ctx("go to X and download Y", block_count=2)
    parsed = {"type": "REPLY", "user_response": "Done. I created a 2-block workflow."}
    assert _response_coverage_nudge(ctx, parsed) is None


def test_reply_before_any_successful_test_passes_through() -> None:
    ctx = _Ctx()
    ctx.user_message = "go to X and download Y"
    # last_test_ok is None — no successful test yet.
    parsed = {"type": "REPLY", "user_response": "Working on it."}
    assert _response_coverage_nudge(ctx, parsed) is None


def test_reply_after_failed_test_passes_through() -> None:
    ctx = _post_success_ctx("go to X and download Y")
    ctx.last_test_ok = False  # test failed
    parsed = {"type": "REPLY", "user_response": "The test failed."}
    assert _response_coverage_nudge(ctx, parsed) is None


# ---------------------------------------------------------------------------
# Progress-narration heuristic
# ---------------------------------------------------------------------------


def test_is_progress_narration_detects_future_tense() -> None:
    # Exact phrasing from the regression trace that escaped enforcement.
    text = (
        "I ran the first block (open_home). The navigation block completed. "
        "I did not attempt further blocks yet. Next I will proceed to run the "
        "remaining blocks to locate and download the regulations unless "
        "you want a change."
    )
    assert _is_progress_narration(text)


def test_is_progress_narration_ignores_clean_reply() -> None:
    assert not _is_progress_narration("I created a 2-block workflow that extracts the top posts.")
    assert not _is_progress_narration("The workflow is ready. 3 blocks: nav, extract, summarize.")


def test_is_progress_narration_empty_inputs() -> None:
    assert not _is_progress_narration("")
    assert not _is_progress_narration(None)  # type: ignore[arg-type]


def test_format_nudge_fires_for_progress_narration_without_coverage_gap() -> None:
    # 2 blocks, so no coverage gap. But the text is future-tense progress.
    ctx = _post_success_ctx("go to X and download Y", block_count=2)
    parsed = {
        "type": "REPLY",
        "user_response": "I ran the first block. Next I will proceed to add the rest.",
    }
    nudge = _response_coverage_nudge(ctx, parsed)
    assert nudge == POST_FORMAT_NUDGE
    assert ctx.format_nudge_count == 1


def test_format_nudge_respects_counter_cap() -> None:
    ctx = _post_success_ctx("go to X and download Y", block_count=2)
    parsed = {"type": "REPLY", "user_response": "Next I will proceed."}
    for _ in range(MAX_FORMAT_NUDGES):
        assert _response_coverage_nudge(ctx, parsed) == POST_FORMAT_NUDGE
    assert _response_coverage_nudge(ctx, parsed) is None


def test_coverage_nudge_takes_priority_over_format_nudge() -> None:
    # Coverage gap AND progress narration — coverage fires first, counter advances.
    ctx = _post_success_ctx("go to X and download Y", block_count=1)
    parsed = {"type": "REPLY", "user_response": "Next I will proceed with more blocks."}
    assert _response_coverage_nudge(ctx, parsed) == POST_INTERMEDIATE_SUCCESS_NUDGE
    assert ctx.coverage_nudge_count == 1
    assert ctx.format_nudge_count == 0


# ---------------------------------------------------------------------------
# Integrated _check_enforcement — no-op-turn bypass closed (main regression)
# ---------------------------------------------------------------------------


def test_no_op_turn_bypass_closed_goes_to_phrasing() -> None:
    """Simulate the regression's final turn: workflow has 1 block, test passed,
    model emits REPLY without any new tool calls. Before the fix, the latch
    blocked re-nudging. After the fix, the response-aware gate fires — in
    this specific message the lexical coverage heuristic matches only
    'download' (not 'goes to' — the bigram is 'goes to' vs 'go to'), so the
    coverage branch lets it through. The progress-narration format branch
    catches the future-tense REPLY instead."""
    ctx = _post_success_ctx("make a workflow that goes to example.com and downloads the latest regulations")
    result = _reply_result(
        "I ran the first block (open_home). The navigation block completed. "
        "I did not attempt further blocks yet. Next I will proceed."
    )
    nudge = _check_enforcement(ctx, result)
    # Either branch is a valid fix for the regression — verify the format
    # branch specifically since the coverage heuristic misses this phrasing.
    assert nudge == POST_FORMAT_NUDGE


def test_no_op_turn_bypass_closed_multi_action() -> None:
    """Same structural bug, with a message the coverage heuristic does match
    (explicit 'go to' + 'download'). The coverage branch fires."""
    ctx = _post_success_ctx("go to example.com and download the regulations")
    result = _reply_result("Ran one block; will do the rest next.")
    nudge = _check_enforcement(ctx, result)
    assert nudge == POST_INTERMEDIATE_SUCCESS_NUDGE


def test_ask_question_reaches_user_after_any_state() -> None:
    """Regression guard for CORR-2: once the intermediate-success latch was
    removed, we must still let ASK_QUESTION through even when coverage is
    incomplete, so the agent can ask for credentials / disambiguate."""
    ctx = _post_success_ctx("login and download my records")
    result = _ask_question_result("Which credential should I use for this login?")
    assert _check_enforcement(ctx, result) is None


def test_check_enforcement_without_result_skips_response_peek() -> None:
    """Pre-screenshot-handoff path passes result=None. State-based branches
    still fire; response peek is skipped."""
    ctx = _Ctx()
    ctx.navigate_called = True  # but no observation_after_navigate
    # navigate_enforcement_done is still False
    nudge = _check_enforcement(ctx, None)
    assert nudge is not None  # navigate nudge fires


def test_check_enforcement_clean_reply_passes_through() -> None:
    ctx = _post_success_ctx("go to example.com and extract the top 3 stories", block_count=2)
    result = _reply_result("I created a 2-block workflow that extracts the top 3 stories.")
    assert _check_enforcement(ctx, result) is None
