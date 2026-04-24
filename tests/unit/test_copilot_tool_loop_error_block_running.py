"""Tests for `_tool_loop_error`'s BLOCK_RUNNING_TOOLS bypass (SKY-9249)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from skyvern.forge.sdk.copilot.tools import BLOCK_RUNNING_TOOLS, _tool_loop_error


def _ctx(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "consecutive_tool_tracker": [],
        "pending_reconciliation_run_id": None,
        "repeated_action_fingerprint_streak_count": 0,
        "last_test_non_retriable_nav_error": None,
        "non_retriable_nav_error_last_emitted_signature": None,
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


def test_bypass_applies_to_both_block_running_tool_names() -> None:
    assert "update_and_run_blocks" in BLOCK_RUNNING_TOOLS
    assert "run_blocks_and_collect_debug" in BLOCK_RUNNING_TOOLS
    ctx = _ctx()
    for _ in range(5):
        assert _tool_loop_error(ctx, "run_blocks_and_collect_debug") is None
