"""Tests for `_tool_loop_error`'s BLOCK_RUNNING_TOOLS bypass (SKY-9249) and the
action-sequence fingerprint compute that drives the repeated-action abort.

The streak counter that drives the abort is owned by
``failure_tracking.update_repeated_failure_state`` (stack 03). These tests cover
the tools.py-side behavior: the block-running bypass/guards and the hard-abort
short-circuit, plus the pure ``compute_action_sequence_fingerprint`` shape logic.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from skyvern.forge.sdk.copilot.enforcement import TOTAL_TIMEOUT_SECONDS
from skyvern.forge.sdk.copilot.failure_tracking import compute_action_sequence_fingerprint
from skyvern.forge.sdk.copilot.loop_detection import record_consecutive_tool_result_boundary_for_ctx
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.tools import (
    BLOCK_RUNNING_TOOLS,
    COPILOT_FINAL_REPLY_RESERVE_SECONDS,
    PER_TOOL_CALL_BUDGET_SECONDS,
    REPEATED_ACTION_STREAK_ABORT_AT,
    _active_block_run_budget_seconds,
    _tool_loop_error,
)


def _ctx(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "consecutive_tool_tracker": [],
        "pending_reconciliation_run_id": None,
        "repeated_action_fingerprint_streak_count": 0,
        "last_test_non_retriable_nav_error": None,
        "non_retriable_nav_error_last_emitted_signature": None,
        "last_failed_workflow_yaml": None,
        "last_workflow_yaml": None,
        "last_test_ok": None,
        "copilot_run_start_monotonic": None,
        "last_test_failure_reason": None,
        "last_outcome_gate_reason": None,
        "last_test_anti_bot": None,
        "staged_workflow": None,
        "staged_workflow_yaml": None,
        "has_staged_proposal": False,
        "synthesized_block_reopened_for_output_coverage": False,
        "output_contract_actuation_by_signature": {},
        "output_contract_reject_count_by_signature": {},
        "output_contract_actuation_count_by_signature": {},
        "output_contract_armed_directive_fingerprint_by_signature": {},
        "output_contract_dispatch_reopened_by_signature": {},
        "turn_ownership": None,
        "blocker_signal_claimant": None,
        "gate_precedence_conflict_events": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


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
    ctx = _ctx(repeated_action_fingerprint_streak_count=REPEATED_ACTION_STREAK_ABORT_AT)

    error = _tool_loop_error(ctx, "run_blocks_and_collect_debug")
    assert error is not None
    assert "Repeated-action abort" in error

    error_update = _tool_loop_error(ctx, "update_and_run_blocks")
    assert error_update is not None
    assert "Repeated-action abort" in error_update


def test_tool_loop_error_does_not_fire_for_non_block_running_tools() -> None:
    ctx = _ctx(repeated_action_fingerprint_streak_count=REPEATED_ACTION_STREAK_ABORT_AT + 5)

    # Planning/metadata tools keep their existing loop-detection behavior
    # and are not affected by the block-run streak.
    assert _tool_loop_error(ctx, "update_workflow") is None
    assert _tool_loop_error(ctx, "list_credentials") is None
    assert _tool_loop_error(ctx, "get_run_results") is None


def test_tool_loop_error_does_not_fire_below_threshold() -> None:
    ctx = _ctx(repeated_action_fingerprint_streak_count=REPEATED_ACTION_STREAK_ABORT_AT - 1)
    assert _tool_loop_error(ctx, "run_blocks_and_collect_debug") is None


def test_block_running_tool_is_not_blocked_by_name_only_streak() -> None:
    ctx = _ctx()
    for _ in range(5):
        assert _tool_loop_error(ctx, "update_and_run_blocks") is None


def test_planning_tool_still_trips_name_only_streak() -> None:
    ctx = _ctx()
    assert _tool_loop_error(ctx, "update_workflow") is None
    assert _tool_loop_error(ctx, "update_workflow") is None
    msg = _tool_loop_error(ctx, "update_workflow")
    assert msg is not None
    assert "LOOP DETECTED" in msg


def test_native_site_distinct_arguments_do_not_trip_streak() -> None:
    ctx = _ctx()
    for value in ("a", "b", "c", "d"):
        assert _tool_loop_error(ctx, "update_workflow", {"workflow_yaml": value}) is None


def test_exempt_tool_completion_breaks_non_exempt_streak() -> None:
    ctx = _ctx()
    assert _tool_loop_error(ctx, "update_workflow") is None
    assert _tool_loop_error(ctx, "update_workflow") is None
    record_consecutive_tool_result_boundary_for_ctx(
        ctx, "fill_credential_field", {"ok": True, "data": {}}, {"field": "username"}
    )
    assert _tool_loop_error(ctx, "update_workflow") is None


def test_unresolved_output_contract_ladder_skips_name_only_streak_for_authoring_tool() -> None:
    ctx = _ctx(output_contract_actuation_count_by_signature={"sig_active": 1})
    for _ in range(5):
        assert _tool_loop_error(ctx, "update_workflow") is None


def test_resolved_output_contract_ladder_re_enables_name_only_streak() -> None:
    ctx = _ctx(
        output_contract_actuation_count_by_signature={"sig_done": 3},
        output_contract_actuation_by_signature={"sig_done": OutputContractAdvisoryState.CONSUMED},
    )
    assert _tool_loop_error(ctx, "update_workflow") is None
    assert _tool_loop_error(ctx, "update_workflow") is None
    msg = _tool_loop_error(ctx, "update_workflow")
    assert msg is not None
    assert "LOOP DETECTED" in msg


def test_block_running_tool_is_blocked_by_pending_reconciliation() -> None:
    ctx = _ctx(pending_reconciliation_run_id="wr_123")
    msg = _tool_loop_error(ctx, "update_and_run_blocks")
    assert msg is not None
    assert "wr_123" in msg


def test_block_running_tool_blocks_late_retry_after_failed_workflow() -> None:
    ctx = _ctx(
        last_failed_workflow_yaml="version: '1.0'",
        copilot_run_start_monotonic=time.monotonic()
        - (TOTAL_TIMEOUT_SECONDS - COPILOT_FINAL_REPLY_RESERVE_SECONDS + 10),
    )

    msg = _tool_loop_error(ctx, "update_and_run_blocks")

    assert msg is not None
    assert ctx.blocker_signal.renders_final_reply is False
    assert "less than 90 seconds" in msg.lower()
    assert "Do NOT retry" in msg
    assert "quick browser inspection tools" in msg
    assert "answer from that observed page evidence" in msg
    assert "Never repeat this tool-error text" in msg


def test_late_retry_guard_allows_latter_half_calls_when_reply_room_remains() -> None:
    ctx = _ctx(
        last_failed_workflow_yaml="version: '1.0'",
        copilot_run_start_monotonic=time.monotonic() - 300,
    )

    assert _tool_loop_error(ctx, "run_blocks_and_collect_debug") is None


def test_block_running_tool_blocks_late_continuation_after_successful_prefix() -> None:
    ctx = _ctx(
        last_workflow_yaml="version: '1.0'",
        last_test_ok=True,
        copilot_run_start_monotonic=time.monotonic()
        - (TOTAL_TIMEOUT_SECONDS - COPILOT_FINAL_REPLY_RESERVE_SECONDS + 10),
    )

    msg = _tool_loop_error(ctx, "update_and_run_blocks")

    assert msg is not None
    assert "less than 90 seconds" in msg.lower()
    assert "Do NOT start another block-running tool call" in msg
    assert "workflow draft and progress gathered so far" in msg
    assert "not been verified end-to-end" in msg


def test_late_retry_guard_waits_until_budget_is_low() -> None:
    ctx = _ctx(
        last_failed_workflow_yaml="version: '1.0'",
        copilot_run_start_monotonic=time.monotonic() - 250,
    )

    assert _tool_loop_error(ctx, "update_and_run_blocks") is None


def test_active_block_run_budget_shrinks_near_deadline() -> None:
    remaining = COPILOT_FINAL_REPLY_RESERVE_SECONDS + 30
    ctx = _ctx(copilot_run_start_monotonic=time.monotonic() - (TOTAL_TIMEOUT_SECONDS - remaining))

    budget = _active_block_run_budget_seconds(ctx)
    assert 25 <= budget <= 30


def test_active_block_run_budget_uses_full_budget_when_deadline_is_distant() -> None:
    remaining = COPILOT_FINAL_REPLY_RESERVE_SECONDS + PER_TOOL_CALL_BUDGET_SECONDS + 30
    ctx = _ctx(copilot_run_start_monotonic=time.monotonic() - (TOTAL_TIMEOUT_SECONDS - remaining))

    assert _active_block_run_budget_seconds(ctx) == PER_TOOL_CALL_BUDGET_SECONDS


def test_late_retry_guard_is_scoped_to_block_running_tools() -> None:
    ctx = _ctx(
        last_failed_workflow_yaml="version: '1.0'",
        copilot_run_start_monotonic=time.monotonic() - 540,
    )

    assert _tool_loop_error(ctx, "update_workflow") is None


def test_bypass_applies_to_both_block_running_tool_names() -> None:
    assert "update_and_run_blocks" in BLOCK_RUNNING_TOOLS
    assert "run_blocks_and_collect_debug" in BLOCK_RUNNING_TOOLS
    ctx = _ctx()
    for _ in range(5):
        assert _tool_loop_error(ctx, "run_blocks_and_collect_debug") is None
