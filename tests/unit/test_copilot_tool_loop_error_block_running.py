"""Tests for `_tool_loop_error`'s BLOCK_RUNNING_TOOLS bypass (SKY-9249)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from skyvern.forge.sdk.copilot.enforcement import TOTAL_TIMEOUT_SECONDS
from skyvern.forge.sdk.copilot.tools import (
    BLOCK_RUNNING_TOOLS,
    COPILOT_FINAL_REPLY_RESERVE_SECONDS,
    PER_TOOL_CALL_BUDGET_SECONDS,
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
    }
    base.update(overrides)
    return SimpleNamespace(**base)


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


def test_block_running_tool_is_blocked_by_pending_reconciliation() -> None:
    ctx = _ctx(pending_reconciliation_run_id="wr_123")
    msg = _tool_loop_error(ctx, "update_and_run_blocks")
    assert msg is not None
    assert "wr_123" in msg


def test_block_running_tool_is_blocked_by_repeated_action_streak() -> None:
    ctx = _ctx(repeated_action_fingerprint_streak_count=5)
    msg = _tool_loop_error(ctx, "update_and_run_blocks")
    assert msg is not None
    assert "Repeated-action abort" in msg


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
