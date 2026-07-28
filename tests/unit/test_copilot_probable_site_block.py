"""Tests for the probable-site-block-wall detector and stop nudge — the
copilot's own shape-independent streak for sites that the shared classifier
routes to ``DATA_EXTRACTION_FAILURE`` rather than ``ANTI_BOT_DETECTION``."""

from __future__ import annotations

import pytest

from skyvern.forge.sdk.copilot.enforcement import (
    MAX_PROBABLE_SITE_BLOCK_STOP_NUDGES,
    POST_PROBABLE_SITE_BLOCK_STOP_NUDGE,
    PROBABLE_SITE_BLOCK_STREAK_STOP_AT,
    REPEATED_FRONTIER_STREAK_ESCALATE_AT,
    _check_enforcement,
    _needs_probable_site_block_stop_nudge,
    _repeated_frontier_failure_nudge,
)
from skyvern.forge.sdk.copilot.tools import (
    _detect_probable_site_block_wall,
    _record_run_blocks_result,
)
from skyvern.forge.sdk.copilot.turn_halt import CopilotTurnHalt, TurnHaltKind
from tests.unit.conftest import make_copilot_context as _fresh_context

_SCRAPE_WALL_REASON = (
    "Skyvern failed to load the website. The page may have navigated "
    "unexpectedly or become unresponsive during analysis."
)


def _scrape_wall_result() -> dict:
    return {
        "ok": False,
        "data": {
            "blocks": [
                {"block_type": "GOTO_URL", "status": "completed"},
                {
                    "block_type": "EXTRACTION",
                    "status": "failed",
                    "failure_reason": _SCRAPE_WALL_REASON,
                },
            ]
        },
    }


# ---------------------------------------------------------------------------
# _detect_probable_site_block_wall
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(_scrape_wall_result(), True, id="completed_nav_plus_scrape_wall"),
        pytest.param(
            {
                "ok": False,
                "data": {
                    "blocks": [
                        {"block_type": "NAVIGATION", "status": "completed"},
                        {
                            "block_type": "EXTRACTION",
                            "status": "failed",
                            "failure_reason": "We think the page may have navigated unexpectedly during analysis.",
                        },
                    ]
                },
            },
            True,
            id="page_navigated_unexpectedly_phrasing",
        ),
        pytest.param({**_scrape_wall_result(), "ok": True}, False, id="run_ok"),
        pytest.param(
            {
                "ok": False,
                "data": {
                    "blocks": [
                        {
                            "block_type": "NAVIGATION",
                            "status": "failed",
                            "failure_reason": _SCRAPE_WALL_REASON,
                        },
                    ]
                },
            },
            True,
            id="nav_only_failure_with_template_reason",
        ),
        pytest.param(
            {
                "ok": False,
                "data": {
                    "blocks": [
                        {
                            "block_type": "GOTO_URL",
                            "status": "failed",
                            "failure_reason": (
                                "Failed to navigate to url https://x.invalid. Error message: net::ERR_NAME_NOT_RESOLVED"
                            ),
                        },
                        {
                            "block_type": "EXTRACTION",
                            "status": "failed",
                            "failure_reason": _SCRAPE_WALL_REASON,
                        },
                    ]
                },
            },
            False,
            id="non_retriable_nav",
        ),
        pytest.param(
            {
                "ok": False,
                "data": {
                    "blocks": [
                        {"block_type": "GOTO_URL", "status": "completed"},
                        {
                            "block_type": "EXTRACTION",
                            "status": "failed",
                            "failure_reason": "Timeout waiting for selector #submit",
                        },
                    ]
                },
            },
            False,
            id="other_failure_reasons",
        ),
        pytest.param({"ok": False}, False, id="missing_data"),
        pytest.param({"ok": False, "data": "not a dict"}, False, id="data_not_a_dict"),
        pytest.param({"ok": False, "data": {}}, False, id="empty_data"),
    ],
)
def test_detect_probable_site_block_wall(result: dict, expected: bool) -> None:
    assert _detect_probable_site_block_wall(result) is expected


# ---------------------------------------------------------------------------
# Streak maintenance in _record_run_blocks_result
# ---------------------------------------------------------------------------


def test_streak_increments_on_consecutive_scrape_walls() -> None:
    ctx = _fresh_context()
    _record_run_blocks_result(ctx, _scrape_wall_result())
    assert ctx.probable_site_block_streak_count == 1
    _record_run_blocks_result(ctx, _scrape_wall_result())
    assert ctx.probable_site_block_streak_count == 2


def test_streak_holds_through_intermediate_nav_only_template_failure() -> None:
    ctx = _fresh_context()
    _record_run_blocks_result(ctx, _scrape_wall_result())
    assert ctx.probable_site_block_streak_count == 1
    nav_only_template_failure = {
        "ok": False,
        "data": {
            "blocks": [
                {
                    "block_type": "NAVIGATION",
                    "status": "failed",
                    "failure_reason": _SCRAPE_WALL_REASON,
                },
            ]
        },
    }
    _record_run_blocks_result(ctx, nav_only_template_failure)
    assert ctx.probable_site_block_streak_count == 2
    _record_run_blocks_result(ctx, _scrape_wall_result())
    assert ctx.probable_site_block_streak_count == 3


def test_streak_resets_on_real_success() -> None:
    ctx = _fresh_context()
    _record_run_blocks_result(ctx, _scrape_wall_result())
    assert ctx.probable_site_block_streak_count == 1
    success = {
        "ok": True,
        "data": {
            "blocks": [
                {
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {"answer": "42"},
                }
            ]
        },
    }
    _record_run_blocks_result(ctx, success)
    assert ctx.probable_site_block_streak_count == 0


def test_streak_resets_on_failure_without_pattern() -> None:
    ctx = _fresh_context()
    _record_run_blocks_result(ctx, _scrape_wall_result())
    assert ctx.probable_site_block_streak_count == 1
    other_failure = {
        "ok": False,
        "data": {
            "blocks": [
                {"block_type": "GOTO_URL", "status": "completed"},
                {
                    "block_type": "EXTRACTION",
                    "status": "failed",
                    "failure_reason": "Timeout waiting for selector #submit",
                },
            ]
        },
    }
    _record_run_blocks_result(ctx, other_failure)
    assert ctx.probable_site_block_streak_count == 0


def test_streak_stays_zero_when_navigation_itself_failed() -> None:
    # Orthogonality contract: when the navigation block did not reach
    # status=completed (e.g. non-retriable nav error — DNS, SSL, invalid URL),
    # the scrape-wall detector must not count the run even if a later block
    # also emitted the generic load-failure template. That case belongs to
    # _detect_non_retriable_nav_error, not the probable-site-block streak.
    ctx = _fresh_context()
    nav_failed_with_wall_text = {
        "ok": False,
        "data": {
            "blocks": [
                {
                    "block_type": "GOTO_URL",
                    "status": "failed",
                    "failure_reason": (
                        "Failed to navigate to url https://x.invalid. Error message: net::ERR_NAME_NOT_RESOLVED"
                    ),
                },
                {
                    "block_type": "EXTRACTION",
                    "status": "failed",
                    "failure_reason": _SCRAPE_WALL_REASON,
                },
            ]
        },
    }
    _record_run_blocks_result(ctx, nav_failed_with_wall_text)
    assert ctx.probable_site_block_streak_count == 0


# ---------------------------------------------------------------------------
# Enforcement gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("streak", [0, 1])
def test_gate_does_not_fire_below_threshold(streak: int) -> None:
    ctx = _fresh_context()
    ctx.probable_site_block_streak_count = streak
    assert not _needs_probable_site_block_stop_nudge(ctx)


def test_gate_fires_at_stop_threshold() -> None:
    ctx = _fresh_context()
    ctx.probable_site_block_streak_count = PROBABLE_SITE_BLOCK_STREAK_STOP_AT
    assert _needs_probable_site_block_stop_nudge(ctx)


def test_gate_does_not_fire_after_cap_reached() -> None:
    ctx = _fresh_context()
    ctx.probable_site_block_streak_count = PROBABLE_SITE_BLOCK_STREAK_STOP_AT
    ctx.probable_site_block_stop_nudge_count = MAX_PROBABLE_SITE_BLOCK_STOP_NUDGES
    assert not _needs_probable_site_block_stop_nudge(ctx)


def test_frontier_warn_defers_to_wall_when_both_apply() -> None:
    ctx = _fresh_context()
    ctx.repeated_failure_streak_count = REPEATED_FRONTIER_STREAK_ESCALATE_AT
    ctx.probable_site_block_streak_count = PROBABLE_SITE_BLOCK_STREAK_STOP_AT
    assert _repeated_frontier_failure_nudge(ctx) is None


def test_frontier_warn_still_fires_when_wall_below_threshold() -> None:
    ctx = _fresh_context()
    ctx.repeated_failure_streak_count = REPEATED_FRONTIER_STREAK_ESCALATE_AT
    ctx.probable_site_block_streak_count = 1
    assert _repeated_frontier_failure_nudge(ctx) is not None


def test_nudge_text_is_stop_oriented() -> None:
    # Sanity-check the stop nudge tells the agent not to retry.
    assert "STOP" in POST_PROBABLE_SITE_BLOCK_STOP_NUDGE
    assert "Do NOT" in POST_PROBABLE_SITE_BLOCK_STOP_NUDGE


def test_stop_nudge_uses_different_proxy_advice_when_effective_proxy_is_active() -> None:
    ctx = _fresh_context()
    ctx.probable_site_block_streak_count = PROBABLE_SITE_BLOCK_STREAK_STOP_AT
    ctx.effective_workflow_proxy_location = "RESIDENTIAL"

    with pytest.raises(CopilotTurnHalt) as exc_info:
        _check_enforcement(ctx)

    halt = exc_info.value.halt
    assert halt.kind == TurnHaltKind.PROBABLE_SITE_BLOCK
    assert halt.blocker_signal is ctx.blocker_signal
    user_facing = halt.blocker_signal.user_facing_reason
    assert "configure a proxy" not in user_facing.lower()
    assert "different proxy location" in user_facing.lower()
    assert "US-CA" in user_facing
    assert "US-NY" in user_facing
    assert "residential/ISP" in user_facing


def test_stop_nudge_keeps_configure_proxy_advice_when_proxy_is_none() -> None:
    ctx = _fresh_context()
    ctx.probable_site_block_streak_count = PROBABLE_SITE_BLOCK_STREAK_STOP_AT
    ctx.effective_workflow_proxy_location = "NONE"

    with pytest.raises(CopilotTurnHalt) as exc_info:
        _check_enforcement(ctx)

    assert exc_info.value.halt.kind == TurnHaltKind.PROBABLE_SITE_BLOCK
    assert "configure a proxy" in exc_info.value.halt.blocker_signal.user_facing_reason.lower()
