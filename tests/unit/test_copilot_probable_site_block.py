"""Tests for the probable-site-block-wall detector and stop nudge — the
copilot's own shape-independent streak for sites that the shared classifier
routes to ``DATA_EXTRACTION_FAILURE`` rather than ``ANTI_BOT_DETECTION``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.context import CopilotContext
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
    _detect_timing_only_challenge_wait_blocks,
    _record_run_blocks_result,
    _timing_only_challenge_wait_reject_message,
    _update_workflow,
)

_SCRAPE_WALL_REASON = (
    "Skyvern failed to load the website. The page may have navigated "
    "unexpectedly or become unresponsive during analysis."
)

_CHALLENGE_WAIT_WORKFLOW = """
workflow_definition:
  blocks:
    - label: open_page
      block_type: goto_url
      url: https://example.com
      next_block_label: wait_challenge
    - label: wait_challenge
      title: Wait for challenge
      block_type: wait
      wait_sec: 10
"""

_GENERIC_WAIT_WORKFLOW = """
workflow_definition:
  blocks:
    - label: wait_for_download
      title: Wait for download
      block_type: wait
      wait_sec: 10
"""

_CONDITIONAL_ACTION_WORKFLOW = """
workflow_definition:
  blocks:
    - label: check_for_challenge
      block_type: conditional
      branch_conditions:
        - condition_type: prompt
          condition: If a challenge is visible on the page
          next_block_label: handle_visible_challenge
      next_block_label: extract_data
    - label: handle_visible_challenge
      title: Handle visible challenge
      block_type: navigation
      navigation_goal: Click the visible verification control if present.
      next_block_label: extract_data
    - label: extract_data
      block_type: extraction
      data_extraction_goal: Extract the requested data.
"""


def _fresh_context() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
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


def test_detect_matches_completed_nav_plus_scrape_wall() -> None:
    assert _detect_probable_site_block_wall(_scrape_wall_result()) is True


def test_detect_matches_page_navigated_unexpectedly_phrasing() -> None:
    result = {
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
    }
    assert _detect_probable_site_block_wall(result) is True


def test_detect_returns_false_when_run_ok() -> None:
    result = _scrape_wall_result()
    result["ok"] = True
    assert _detect_probable_site_block_wall(result) is False


def test_detect_matches_nav_only_failure_with_template_reason() -> None:
    result = {
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
    assert _detect_probable_site_block_wall(result) is True


def test_detect_returns_false_when_non_retriable_nav() -> None:
    result = {
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
    assert _detect_probable_site_block_wall(result) is False


def test_detect_returns_false_for_other_failure_reasons() -> None:
    result = {
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
    assert _detect_probable_site_block_wall(result) is False


def test_detect_tolerates_missing_data() -> None:
    assert _detect_probable_site_block_wall({"ok": False}) is False
    assert _detect_probable_site_block_wall({"ok": False, "data": "not a dict"}) is False
    assert _detect_probable_site_block_wall({"ok": False, "data": {}}) is False


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

    nudge = _check_enforcement(ctx)

    assert nudge is not None
    assert "configure a proxy" not in nudge.lower()
    assert "different proxy location" in nudge.lower()
    assert "US-CA" in nudge
    assert "US-NY" in nudge
    assert "residential/ISP" in nudge


def test_stop_nudge_keeps_configure_proxy_advice_when_proxy_is_none() -> None:
    ctx = _fresh_context()
    ctx.probable_site_block_streak_count = PROBABLE_SITE_BLOCK_STREAK_STOP_AT
    ctx.effective_workflow_proxy_location = "NONE"

    nudge = _check_enforcement(ctx)

    assert nudge is not None
    assert "configure a proxy" in nudge.lower()


def test_detects_challenge_named_wait_block() -> None:
    assert _detect_timing_only_challenge_wait_blocks(_CHALLENGE_WAIT_WORKFLOW) == ["wait_challenge"]


def test_rejects_challenge_wait_after_explicit_anti_bot_evidence() -> None:
    ctx = _fresh_context()
    ctx.last_test_anti_bot = "Cloudflare challenge page detected"

    message = _timing_only_challenge_wait_reject_message(ctx, _CHALLENGE_WAIT_WORKFLOW)

    assert message is not None
    assert "wait_challenge" in message
    assert "timing-only challenge wait" in message


@pytest.mark.asyncio
async def test_update_workflow_rejects_challenge_wait_after_explicit_anti_bot_evidence() -> None:
    ctx = _fresh_context()
    ctx.last_test_anti_bot = "Cloudflare challenge page detected"

    result = await _update_workflow({"workflow_yaml": _CHALLENGE_WAIT_WORKFLOW}, ctx)

    assert result["ok"] is False
    assert "wait_challenge" in str(result["error"])


def test_rejects_challenge_wait_after_repeated_scrape_wall() -> None:
    ctx = _fresh_context()
    _record_run_blocks_result(ctx, _scrape_wall_result())
    _record_run_blocks_result(ctx, _scrape_wall_result())

    message = _timing_only_challenge_wait_reject_message(ctx, _CHALLENGE_WAIT_WORKFLOW)

    assert message is not None
    assert "wait_challenge" in message


def test_allows_generic_wait_after_block_evidence() -> None:
    ctx = _fresh_context()
    ctx.last_test_anti_bot = "challenge page detected"

    assert _timing_only_challenge_wait_reject_message(ctx, _GENERIC_WAIT_WORKFLOW) is None


def test_allows_conditional_challenge_action_after_block_evidence() -> None:
    ctx = _fresh_context()
    ctx.last_test_anti_bot = "challenge page detected"

    assert _timing_only_challenge_wait_reject_message(ctx, _CONDITIONAL_ACTION_WORKFLOW) is None
