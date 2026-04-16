"""Tests for the action-sequence fingerprint compute and the hard-abort
short-circuit in ``_tool_loop_error``.

The streak counter that drives the abort is owned by
``failure_tracking.update_repeated_failure_state`` (stack 03). These tests
cover only the tools.py-side behavior:

- ``compute_action_sequence_fingerprint`` is stable across runs that fire
  the same action shape (independent of reasoning text / status).
- ``compute_action_sequence_fingerprint`` distinguishes different sequences.
- ``_tool_loop_error`` returns a hard-abort message when the streak crosses
  ``REPEATED_ACTION_STREAK_ABORT_AT`` for a block-running tool.
- The hard abort does NOT fire for non-block-running tools, regardless of
  streak height.
"""

from __future__ import annotations

from types import SimpleNamespace

from skyvern.forge.sdk.copilot.failure_tracking import compute_action_sequence_fingerprint
from skyvern.forge.sdk.copilot.tools import (
    REPEATED_ACTION_STREAK_ABORT_AT,
    _tool_loop_error,
)


def _ctx_with_streak(streak: int) -> SimpleNamespace:
    return SimpleNamespace(
        consecutive_tool_tracker=[],
        repeated_action_fingerprint_streak_count=streak,
    )


def test_compute_action_sequence_fingerprint_stable_for_same_sequence() -> None:
    trace_a = [
        {"action": "input_text", "element": "elem-name", "reasoning": "r1", "status": "failed"},
        {"action": "input_text", "element": "elem-email", "reasoning": "r2", "status": "failed"},
        {"action": "click", "element": "elem-submit", "reasoning": "r3", "status": "failed"},
    ]
    trace_b = [
        {"action": "input_text", "element": "elem-name", "reasoning": "different_text", "status": "failed"},
        {"action": "input_text", "element": "elem-email", "reasoning": "other", "status": "failed"},
        {"action": "click", "element": "elem-submit", "reasoning": "third", "status": "failed"},
    ]
    fp_a = compute_action_sequence_fingerprint([{"action_trace": trace_a}])
    fp_b = compute_action_sequence_fingerprint([{"action_trace": trace_b}])
    assert fp_a is not None
    # Reasoning / status are excluded from the fingerprint on purpose — only
    # the (action, element) shape matters for detecting a retry loop.
    assert fp_a == fp_b


def test_compute_action_sequence_fingerprint_none_when_trace_missing() -> None:
    assert compute_action_sequence_fingerprint([]) is None
    assert compute_action_sequence_fingerprint([{"status": "completed"}]) is None
    assert compute_action_sequence_fingerprint([{"action_trace": []}]) is None


def test_compute_action_sequence_fingerprint_distinguishes_different_sequences() -> None:
    trace_a = [{"action": "click", "element": "btn-a"}]
    trace_b = [{"action": "click", "element": "btn-b"}]
    trace_c = [{"action": "input_text", "element": "btn-a"}]
    fp_a = compute_action_sequence_fingerprint([{"action_trace": trace_a}])
    fp_b = compute_action_sequence_fingerprint([{"action_trace": trace_b}])
    fp_c = compute_action_sequence_fingerprint([{"action_trace": trace_c}])
    assert fp_a != fp_b
    assert fp_a != fp_c
    assert fp_b != fp_c


def test_tool_loop_error_fires_hard_abort_on_block_running_tools_when_streak_high() -> None:
    ctx = _ctx_with_streak(REPEATED_ACTION_STREAK_ABORT_AT)

    error = _tool_loop_error(ctx, "run_blocks_and_collect_debug")
    assert error is not None
    assert "Repeated-action abort" in error

    error_update = _tool_loop_error(ctx, "update_and_run_blocks")
    assert error_update is not None
    assert "Repeated-action abort" in error_update


def test_tool_loop_error_does_not_fire_for_non_block_running_tools() -> None:
    ctx = _ctx_with_streak(REPEATED_ACTION_STREAK_ABORT_AT + 5)

    # Planning/metadata tools keep their existing loop-detection behavior
    # and are not affected by the block-run streak.
    assert _tool_loop_error(ctx, "update_workflow") is None
    assert _tool_loop_error(ctx, "list_credentials") is None
    assert _tool_loop_error(ctx, "get_run_results") is None


def test_tool_loop_error_does_not_fire_below_threshold() -> None:
    ctx = _ctx_with_streak(REPEATED_ACTION_STREAK_ABORT_AT - 1)
    assert _tool_loop_error(ctx, "run_blocks_and_collect_debug") is None
