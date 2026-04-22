"""Tests for non-retriable navigation error handling in the copilot layer.

Covers SKY-9136: when the browser layer raises ``FailedToNavigateToUrl`` with
a DNS / cert / SSL / invalid-URL pattern (``SKIP_INNER_NAV_RETRY_ERRORS``),
the copilot must surface the real error instead of "Unknown error", must not
keep retrying, and must fail deterministically even if the model tries to
narrate a completion.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE,
    CopilotNonRetriableNavError,
    _check_enforcement,
    _extract_url_from_nav_error,
    _maybe_raise_non_retriable_nav,
    _needs_failed_test_nudge,
    _needs_repeated_null_data_nudge,
    _needs_suspicious_success_nudge,
    _non_retriable_nav_error_nudge,
    _repeated_frontier_failure_nudge,
)
from skyvern.forge.sdk.copilot.tools import (
    _detect_non_retriable_nav_error,
    _record_run_blocks_result,
    _record_workflow_update_result,
    _tool_loop_error,
)

_DNS_FAILURE_REASON = (
    "Failed to navigate to url https://www.example.invalid/path. Error message: net::ERR_NAME_NOT_RESOLVED"
)
_CERT_FAILURE_REASON = "Failed to navigate to url https://expired.example. Error message: net::ERR_CERT_DATE_INVALID"
_GENERIC_FAILURE_REASON = "Timeout waiting for element #submit"


def _fresh_context() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# _detect_non_retriable_nav_error
# ---------------------------------------------------------------------------


def test_detect_matches_dns_error_in_block_failure_reason() -> None:
    result = {"ok": False, "data": {"blocks": [{"failure_reason": _DNS_FAILURE_REASON}]}}
    assert _detect_non_retriable_nav_error(result) == _DNS_FAILURE_REASON


def test_detect_matches_cert_error_in_run_level_failure_reason() -> None:
    result = {"ok": False, "data": {"failure_reason": _CERT_FAILURE_REASON, "blocks": []}}
    assert _detect_non_retriable_nav_error(result) == _CERT_FAILURE_REASON


def test_detect_matches_invalid_url_error() -> None:
    invalid_url = "Failed to navigate to url not-a-url. Error message: net::ERR_INVALID_URL"
    result = {"ok": False, "data": {"blocks": [{"failure_reason": invalid_url}]}}
    assert _detect_non_retriable_nav_error(result) == invalid_url


def test_detect_matches_name_resolution_failed() -> None:
    reason = "net::ERR_NAME_RESOLUTION_FAILED happened mid-flight"
    result = {"ok": False, "data": {"blocks": [{"failure_reason": reason}]}}
    assert _detect_non_retriable_nav_error(result) == reason


def test_detect_matches_ssl_error() -> None:
    reason = "SSL error: net::ERR_SSL_PROTOCOL_ERROR"
    result = {"ok": False, "data": {"blocks": [{"failure_reason": reason}]}}
    assert _detect_non_retriable_nav_error(result) == reason


def test_detect_returns_none_for_generic_failure() -> None:
    result = {"ok": False, "data": {"blocks": [{"failure_reason": _GENERIC_FAILURE_REASON}]}}
    assert _detect_non_retriable_nav_error(result) is None


def test_detect_returns_none_for_missing_data() -> None:
    assert _detect_non_retriable_nav_error({"ok": False}) is None


def test_detect_returns_none_for_empty_blocks() -> None:
    assert _detect_non_retriable_nav_error({"ok": False, "data": {"blocks": []}}) is None


def test_detect_prefers_run_level_over_block_level() -> None:
    # When both match, the run-level reason wins (it is the authoritative
    # aggregate failure the workflow service recorded).
    result = {
        "ok": False,
        "data": {
            "failure_reason": _DNS_FAILURE_REASON,
            "blocks": [{"failure_reason": _CERT_FAILURE_REASON}],
        },
    }
    assert _detect_non_retriable_nav_error(result) == _DNS_FAILURE_REASON


# ---------------------------------------------------------------------------
# _record_run_blocks_result — context flag plumbing
# ---------------------------------------------------------------------------


def test_record_sets_flag_on_dns_failure() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"blocks": [{"failure_reason": _DNS_FAILURE_REASON}]},
        },
    )
    assert ctx.last_test_non_retriable_nav_error == _DNS_FAILURE_REASON
    assert ctx.last_test_ok is False


def test_record_leaves_flag_none_for_generic_failure() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"blocks": [{"failure_reason": _GENERIC_FAILURE_REASON}]},
        },
    )
    assert ctx.last_test_non_retriable_nav_error is None


def test_record_clears_flag_on_every_call() -> None:
    # Stale state from a prior run must not leak into the next run's context.
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_non_retriable_nav_error = "stale DNS error"
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"blocks": [{"failure_reason": _GENERIC_FAILURE_REASON}]},
        },
    )
    assert ctx.last_test_non_retriable_nav_error is None


def test_workflow_update_clears_non_retriable_flag_and_signature_latch() -> None:
    # Codex review P2-2: after a DNS-failed run, if the agent edits the
    # workflow (e.g. fixing the URL), the stale flag must not survive the
    # edit — otherwise an exhausted POST_UPDATE_NUDGE on the new draft
    # would raise CopilotNonRetriableNavError with the OLD run's error
    # message, telling the user to verify a URL they just corrected.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.non_retriable_nav_error_last_emitted_signature = "dns_signature_123"
    ctx.last_test_ok = False
    ctx.workflow_yaml = "updated yaml"

    _record_workflow_update_result(
        ctx,
        {
            "ok": True,
            "data": {"block_count": 2},
            "_workflow": SimpleNamespace(workflow_id="wf_new"),
        },
    )
    assert ctx.last_test_non_retriable_nav_error is None
    assert ctx.non_retriable_nav_error_last_emitted_signature is None
    # Consistency check: the other per-test fields are also reset (pre-existing behavior).
    assert ctx.last_test_ok is None
    assert ctx.last_test_failure_reason is None


def test_workflow_update_does_not_clear_flag_on_failed_update() -> None:
    # Only a SUCCESSFUL update invalidates prior test state — a failed
    # validation attempt leaves the existing flags alone.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.non_retriable_nav_error_last_emitted_signature = "dns_signature_123"
    ctx.last_test_ok = False

    _record_workflow_update_result(
        ctx,
        {"ok": False, "error": "validation failed"},
    )
    assert ctx.last_test_non_retriable_nav_error == _DNS_FAILURE_REASON
    assert ctx.non_retriable_nav_error_last_emitted_signature == "dns_signature_123"


def test_record_clears_signature_latch_on_meaningful_success() -> None:
    # After a real success, the stop nudge must be able to re-fire if a new
    # bad URL fails later in the same session.
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.non_retriable_nav_error_last_emitted_signature = "some previous signature"
    _record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "blocks": [
                    {
                        "label": "extract",
                        "block_type": "extraction",
                        "status": "completed",
                        "extracted_data": [{"name": "widget", "price": 1.0}],
                    }
                ],
            },
        },
    )
    assert ctx.non_retriable_nav_error_last_emitted_signature is None


# ---------------------------------------------------------------------------
# _non_retriable_nav_error_nudge — signature latch
# ---------------------------------------------------------------------------


def test_nudge_helper_fires_first_time() -> None:
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    result = _non_retriable_nav_error_nudge(ctx)
    assert result is not None
    nudge, signature = result
    assert nudge == POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE
    assert signature  # non-empty


def test_nudge_helper_does_not_refire_same_signature() -> None:
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    first = _non_retriable_nav_error_nudge(ctx)
    assert first is not None
    ctx.non_retriable_nav_error_last_emitted_signature = first[1]
    assert _non_retriable_nav_error_nudge(ctx) is None


def test_nudge_helper_refires_on_different_signature() -> None:
    # User corrects from one bad URL to another bad URL in the same session
    # without a successful run in between; the stop nudge must re-fire.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    first = _non_retriable_nav_error_nudge(ctx)
    assert first is not None
    ctx.non_retriable_nav_error_last_emitted_signature = first[1]

    ctx.last_test_non_retriable_nav_error = _CERT_FAILURE_REASON
    second = _non_retriable_nav_error_nudge(ctx)
    assert second is not None
    assert second[1] != first[1]


def test_nudge_helper_returns_none_when_flag_unset() -> None:
    ctx = _fresh_context()
    assert _non_retriable_nav_error_nudge(ctx) is None


# ---------------------------------------------------------------------------
# _check_enforcement — branch ordering and latching
# ---------------------------------------------------------------------------


def test_check_enforcement_returns_non_retriable_nudge_before_failed_test() -> None:
    # Conditions that would normally trigger POST_FAILED_TEST_NUDGE (last_test
    # failed, test_after_update_done=True) must yield the non-retriable stop
    # nudge instead when the flag is set.
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_test_failure_reason = _DNS_FAILURE_REASON
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    nudge = _check_enforcement(ctx)
    assert nudge == POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE
    # Emission latched for future calls with same signature.
    assert ctx.non_retriable_nav_error_last_emitted_signature


def test_check_enforcement_one_shot_per_signature() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_test_failure_reason = _DNS_FAILURE_REASON
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    first = _check_enforcement(ctx)
    assert first == POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE
    # Same signature on next iteration: nudge returns None (and does NOT
    # fall through to POST_FAILED_TEST_NUDGE thanks to competing-branch
    # suppression).
    assert _check_enforcement(ctx) is None


def test_check_enforcement_pre_empts_post_navigate_nudge() -> None:
    # Codex review P2-1: a navigate-hygiene state (navigate_called=True,
    # observation_after_navigate=False) must not steal the nudge slot when
    # a non-retriable nav error is also present — POST_NAVIGATE_NUDGE tells
    # the agent to observe the page, which does not resolve a DNS failure
    # and merely delays the deterministic stop by at least one iteration.
    ctx = _fresh_context()
    ctx.navigate_called = True
    ctx.observation_after_navigate = False
    ctx.navigate_enforcement_done = False
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    assert _check_enforcement(ctx) == POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE


def test_check_enforcement_pre_empts_post_update_nudge() -> None:
    # Similar P2-1 case: an update-without-test state must not pre-empt the
    # terminal stop. In practice P2-2 clears the flag on update, but if a
    # race leaves the flag set, stop-nudge still wins.
    ctx = _fresh_context()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = False
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    assert _check_enforcement(ctx) == POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE


# ---------------------------------------------------------------------------
# Competing-branch suppression — each failure helper
# ---------------------------------------------------------------------------


def test_needs_failed_test_nudge_suppressed_when_flag_set() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    assert _needs_failed_test_nudge(ctx) is False


def test_needs_failed_test_nudge_still_fires_without_flag() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    assert _needs_failed_test_nudge(ctx) is True


def test_needs_suspicious_success_nudge_suppressed_when_flag_set() -> None:
    ctx = _fresh_context()
    ctx.last_test_suspicious_success = True
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    assert _needs_suspicious_success_nudge(ctx) is False


def test_needs_repeated_null_data_nudge_suppressed_when_flag_set() -> None:
    ctx = _fresh_context()
    ctx.last_test_suspicious_success = True
    ctx.null_data_streak_count = 5
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    assert _needs_repeated_null_data_nudge(ctx) is False


def test_repeated_frontier_failure_nudge_suppressed_when_flag_set() -> None:
    ctx = _fresh_context()
    ctx.repeated_failure_streak_count = 5
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    assert _repeated_frontier_failure_nudge(ctx) is None


# ---------------------------------------------------------------------------
# _extract_url_from_nav_error
# ---------------------------------------------------------------------------


def test_extract_url_parses_standard_format() -> None:
    url = _extract_url_from_nav_error(_DNS_FAILURE_REASON)
    assert url == "https://www.example.invalid/path"


def test_extract_url_returns_none_on_malformed_message() -> None:
    assert _extract_url_from_nav_error("some unrelated error text") is None


# ---------------------------------------------------------------------------
# _maybe_raise_non_retriable_nav — deterministic exit-path
# ---------------------------------------------------------------------------


def test_maybe_raise_noops_when_flag_unset() -> None:
    ctx = _fresh_context()
    _maybe_raise_non_retriable_nav(ctx)  # must not raise


def test_maybe_raise_noops_when_last_test_is_ok() -> None:
    # A prior successful run does NOT suppress the exception (per CORR-3),
    # but the MOST RECENT run being a real success does — because that
    # means this turn's test did not hit the non-retriable path.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = True
    _maybe_raise_non_retriable_nav(ctx)  # must not raise


def test_maybe_raise_raises_when_flag_and_last_test_failed() -> None:
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = False
    with pytest.raises(CopilotNonRetriableNavError) as excinfo:
        _maybe_raise_non_retriable_nav(ctx)
    assert excinfo.value.error_message == _DNS_FAILURE_REASON
    assert excinfo.value.url == "https://www.example.invalid/path"


def test_maybe_raise_raises_when_last_test_ok_is_none() -> None:
    # The guard is ``last_test_ok is not True`` (not ``is False``), so an
    # ambiguous None (e.g. a suspicious-success run) with the flag set still
    # surfaces the cached nav failure rather than letting the loop return
    # silently. Locks in the tri-state semantics.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = None
    with pytest.raises(CopilotNonRetriableNavError):
        _maybe_raise_non_retriable_nav(ctx)


def test_maybe_raise_raises_when_prior_run_succeeded_but_current_failed() -> None:
    # Codex CORR-3: the guard must NOT be gated on session history. A fresh
    # non-retriable nav failure on the most recent run still raises, even if
    # an earlier run in the same session succeeded.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = False  # most recent run
    # Simulate a prior successful run in the session — there is no
    # `any_test_ok_ever` flag; the helper only inspects current state.
    with pytest.raises(CopilotNonRetriableNavError):
        _maybe_raise_non_retriable_nav(ctx)


# ---------------------------------------------------------------------------
# Sanity: exception carries the expected attributes for the agent handler
# ---------------------------------------------------------------------------


def test_exception_carries_url_and_error_message() -> None:
    exc = CopilotNonRetriableNavError(url="https://x.test", error_message="some reason")
    assert exc.url == "https://x.test"
    assert exc.error_message == "some reason"
    assert "some reason" in str(exc)


# ---------------------------------------------------------------------------
# Sanity: when flag is set, no failure-nudge branch competes
# ---------------------------------------------------------------------------


def test_all_competing_branches_silent_after_latch() -> None:
    # Reproduce the full steady state after the one-shot stop nudge has
    # latched: ctx has a non-retriable error, last_test_ok=False,
    # test_after_update_done=True, and all counters are set such that the
    # other branches would normally fire. Assert all four helpers are silent.
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_test_suspicious_success = True
    ctx.null_data_streak_count = 5
    ctx.repeated_failure_streak_count = 5
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON

    assert _needs_failed_test_nudge(ctx) is False
    assert _needs_suspicious_success_nudge(ctx) is False
    assert _needs_repeated_null_data_nudge(ctx) is False
    assert _repeated_frontier_failure_nudge(ctx) is None


def test_without_flag_competing_branches_still_active() -> None:
    # Inverse: same setup but without the flag — all relevant branches fire.
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    assert _needs_failed_test_nudge(ctx) is True


# ---------------------------------------------------------------------------
# Integration-ish: record -> check -> exception flow
# ---------------------------------------------------------------------------


def test_full_flow_record_then_check_then_raise() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    # Simulate a failed run with a DNS error.
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"blocks": [{"failure_reason": _DNS_FAILURE_REASON}]},
        },
    )
    # Enforcement fires the stop nudge.
    nudge = _check_enforcement(ctx)
    assert nudge == POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE
    # Same signature on next iteration: no nudge, and the exit-path guard
    # raises because last_test_ok is still False.
    assert _check_enforcement(ctx) is None
    with pytest.raises(CopilotNonRetriableNavError):
        _maybe_raise_non_retriable_nav(ctx)


def test_full_flow_cleared_after_successful_run() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {"ok": False, "data": {"blocks": [{"failure_reason": _DNS_FAILURE_REASON}]}},
    )
    assert _check_enforcement(ctx) == POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE
    # Then a real success happens.
    _record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "blocks": [
                    {
                        "label": "extract",
                        "block_type": "extraction",
                        "status": "completed",
                        "extracted_data": [{"x": 1}],
                    }
                ],
            },
        },
    )
    # Signature latch cleared so a later bad URL can re-fire.
    assert ctx.non_retriable_nav_error_last_emitted_signature is None
    # Last-test fields now reflect success; the exit-path guard does nothing.
    _maybe_raise_non_retriable_nav(ctx)  # must not raise


# ---------------------------------------------------------------------------
# Within-turn fail-fast guard — _tool_loop_error
# ---------------------------------------------------------------------------


def test_tool_loop_error_blocks_update_and_run_blocks_after_dns_failure() -> None:
    # Observed repro: agent called update_and_run_blocks on www.example.invalid,
    # got DNS failure, then the LLM tried again with the bare host on the next
    # tool call — still within the same agent turn. The enforcement-loop stop
    # nudge only fires between turns, so the guard must live at the tool
    # entrypoint to actually prevent the speculative retry.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    err = _tool_loop_error(ctx, "update_and_run_blocks")
    assert err is not None
    assert "permanent navigation error" in err
    assert "net::ERR_NAME_NOT_RESOLVED" in err
    assert "Do NOT retry" in err


def test_tool_loop_error_blocks_run_blocks_and_collect_debug_after_dns_failure() -> None:
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    err = _tool_loop_error(ctx, "run_blocks_and_collect_debug")
    assert err is not None
    assert "permanent navigation error" in err


def test_tool_loop_error_does_not_block_planning_tools() -> None:
    # get_run_results / list_credentials / update_workflow are scoped out of
    # _BLOCK_RUNNING_TOOLS and should remain callable so the agent can inspect
    # the failure and decide how to respond to the user.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    for tool in ("get_run_results", "list_credentials", "update_workflow"):
        assert _tool_loop_error(ctx, tool) is None, f"{tool} should not be blocked"


def test_tool_loop_error_does_not_block_when_flag_unset() -> None:
    ctx = _fresh_context()
    assert _tool_loop_error(ctx, "update_and_run_blocks") is None
    assert _tool_loop_error(ctx, "run_blocks_and_collect_debug") is None
