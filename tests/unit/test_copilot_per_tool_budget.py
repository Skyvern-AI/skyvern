"""Tests for the per-tool-call budget cap and its enforcement nudge.

Covers four surfaces:

- ``_record_run_blocks_result`` — the ``PER_TOOL_BUDGET`` failure-category
  entry must land on ``last_failure_category_top``.
- ``_needs_per_tool_budget_nudge`` — fires while under the per-streak cap,
  stops once the cap is reached.
- ``_check_enforcement`` ordering — the budget nudge must pre-empt the
  generic ``POST_FAILED_TEST_NUDGE`` and the repeated-frontier escalation.
- ``compute_failure_signature`` — the run_id baked into the watchdog
  message must not make consecutive trips hash differently.
- ``_maybe_clear_reconciliation_flag`` — a ``canceled`` row clears the
  guard for budget exits, but not for other watchdog cancels.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    MAX_PER_TOOL_BUDGET_NUDGES,
    POST_ANTI_BOT_FAILED_TEST_NUDGE,
    POST_FAILED_TEST_NUDGE,
    POST_PER_TOOL_BUDGET_NUDGE,
    REPEATED_FRONTIER_STREAK_ESCALATE_AT,
    _check_enforcement,
    _needs_per_tool_budget_nudge,
)
from skyvern.forge.sdk.copilot.failure_tracking import (
    PER_TOOL_BUDGET_FAILURE_CATEGORY,
    compute_failure_signature,
)
from skyvern.forge.sdk.copilot.tools import (
    WatchdogExitReason,
    _composition_anti_bot_reason,
    _last_run_has_terminal_anti_bot_blocker,
    _mark_page_inspected,
    _mark_pending_reconciliation_run,
    _maybe_clear_reconciliation_flag,
    _record_composition_page_observation,
    _record_per_tool_budget_problem_blocks_from_results,
    _record_run_blocks_result,
    _record_workflow_update_result,
    _tool_loop_error,
    _watchdog_user_failure_reason,
)


def _fresh_context() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _budget_trip_result(workflow_run_id: str = "wr_1") -> dict:
    return {
        "ok": False,
        "error": (
            f"The run exceeded the 240s per-tool-call budget while still making progress. "
            f"Run ID: {workflow_run_id}. Next step: call get_run_results with this workflow_run_id."
        ),
        "data": {
            "workflow_run_id": workflow_run_id,
            "overall_status": "running",
            "failure_reason": f"per-tool-call budget exceeded (Run ID: {workflow_run_id})",
            "failure_categories": [
                {
                    "category": PER_TOOL_BUDGET_FAILURE_CATEGORY,
                    "confidence_float": 1.0,
                    "reasoning": "Per-tool-call budget exceeded",
                }
            ],
        },
    }


def _reply_result(user_response: str) -> SimpleNamespace:
    return SimpleNamespace(
        final_output=json.dumps({"type": "REPLY", "user_response": user_response}),
        new_items=[],
    )


def test_record_sets_top_category_on_per_tool_budget_result() -> None:
    ctx = _fresh_context()
    _record_run_blocks_result(ctx, _budget_trip_result("wr_budget"))
    assert ctx.last_failure_category_top == PER_TOOL_BUDGET_FAILURE_CATEGORY
    assert ctx.last_run_blocks_workflow_run_id == "wr_budget"
    assert ctx.last_successful_run_blocks_workflow_run_id is None


def test_record_per_tool_budget_with_current_url_requires_page_inspection() -> None:
    ctx = _fresh_context()
    result = _budget_trip_result("wr_budget")
    result["data"]["current_url"] = "https://example.com/results"

    _record_run_blocks_result(ctx, result)

    assert ctx.post_budget_page_inspection_required is True
    assert ctx.post_budget_page_inspection_url == "https://example.com/results"
    assert ctx.post_budget_page_inspection_run_id == "wr_budget"


def test_record_preserves_pre_run_anti_bot_evidence_on_budget_trip() -> None:
    ctx = _fresh_context()
    ctx.composition_page_evidence = {
        "anti_bot_indicators": ["human-verification", "human-verification"],
        "challenge_controls": [{"selector": "#human-verification-widget"}],
        "challenge_state": {
            "detected": True,
            "kind": "human_verification",
            "indicators": ["verify you are human"],
            "gates_submit_controls": True,
            "gated_submit_controls": [{"text": "Search", "disabled": True}],
        },
    }

    _record_run_blocks_result(ctx, _budget_trip_result("wr_budget"))

    assert ctx.last_failure_category_top == PER_TOOL_BUDGET_FAILURE_CATEGORY
    assert ctx.last_test_anti_bot is not None
    assert "human-verification" in ctx.last_test_anti_bot
    assert "challenge-gated disabled submit/search control: Search" in ctx.last_test_anti_bot


def test_composition_anti_bot_reason_reads_typed_challenge_state_without_legacy_indicators() -> None:
    ctx = _fresh_context()
    ctx.composition_page_evidence = {
        "challenge_state": {
            "detected": True,
            "kind": "human_verification",
            "indicators": ["human-verification-response"],
            "gates_submit_controls": True,
            "gated_submit_controls": [{"text": "Search", "disabled": True}],
        },
    }

    reason = _composition_anti_bot_reason(ctx)

    assert reason is not None
    assert "human_verification" in reason
    assert "challenge-gated disabled submit/search control: Search" in reason


def test_block_running_after_budget_current_url_requires_inspection_first() -> None:
    ctx = _fresh_context()
    ctx.post_budget_page_inspection_required = True
    ctx.post_budget_page_inspection_url = "https://example.com/results"
    ctx.post_budget_page_inspection_run_id = "wr_budget"

    msg = _tool_loop_error(ctx, "update_and_run_blocks", {"block_labels": ["extract_results"]})

    assert msg is not None
    assert "inspect_page_for_composition" in msg
    assert "current_page" in msg
    assert "screenshot/evaluate reads" in msg
    assert "do not satisfy the bounded page-evidence contract" in msg
    assert "answer from that evidence" in msg


def test_page_inspection_clears_post_budget_run_guard() -> None:
    ctx = _fresh_context()
    ctx.post_budget_page_inspection_required = True
    ctx.post_budget_page_inspection_url = "https://example.com/results"
    ctx.post_budget_page_inspection_run_id = "wr_budget"

    _mark_page_inspected(ctx)

    assert ctx.post_budget_page_inspection_required is False
    assert ctx.post_budget_page_inspection_url is None
    assert ctx.post_budget_page_inspection_run_id is None


def test_record_uses_policy_failure_reason_not_llm_tool_instruction() -> None:
    ctx = _fresh_context()

    _record_run_blocks_result(ctx, _budget_trip_result("wr_safe"))

    assert ctx.last_test_failure_reason == "per-tool-call budget exceeded (Run ID: wr_safe)"
    assert "get_run_results" not in ctx.last_test_failure_reason


def test_watchdog_user_failure_reason_excludes_next_tool_instruction() -> None:
    exit_reason: WatchdogExitReason = "per_tool_budget"
    reason = _watchdog_user_failure_reason(exit_reason, "wr_safe", 240, None)

    assert "per-tool-call budget" in reason
    assert "Run ID: wr_safe" in reason
    assert "get_run_results" not in reason
    assert "update_and_run_blocks" not in reason


def test_record_clears_top_category_on_run_with_different_category() -> None:
    ctx = _fresh_context()
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY

    other_failure = {
        "ok": False,
        "error": "boom",
        "data": {
            "blocks": [{"status": "failed", "failure_reason": "something else"}],
            "failure_categories": [{"category": "PARAMETER_BINDING_ERROR"}],
        },
    }
    _record_run_blocks_result(ctx, other_failure)
    assert ctx.last_failure_category_top == "PARAMETER_BINDING_ERROR"


def test_record_clears_top_category_on_success() -> None:
    ctx = _fresh_context()
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY

    success = {
        "ok": True,
        "data": {
            "blocks": [
                {
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {"price": 10},
                }
            ]
        },
    }
    _record_run_blocks_result(ctx, success)
    assert ctx.last_failure_category_top is None


def test_gate_does_not_fire_when_top_category_unset() -> None:
    ctx = _fresh_context()
    assert not _needs_per_tool_budget_nudge(ctx)


def test_gate_fires_when_top_category_is_budget_and_cap_not_reached() -> None:
    ctx = _fresh_context()
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    assert _needs_per_tool_budget_nudge(ctx)


def test_gate_does_not_fire_after_cap_reached() -> None:
    ctx = _fresh_context()
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    ctx.per_tool_budget_nudge_count = MAX_PER_TOOL_BUDGET_NUDGES
    assert not _needs_per_tool_budget_nudge(ctx)


def test_check_enforcement_emits_budget_nudge_before_failed_test() -> None:
    """A budget trip also looks like a failed test (last_test_ok=False), so
    without the dedicated path it would land in POST_FAILED_TEST_NUDGE. The
    budget nudge must pre-empt it."""
    ctx = _fresh_context()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY

    nudge = _check_enforcement(ctx)
    assert nudge == POST_PER_TOOL_BUDGET_NUDGE
    assert ctx.per_tool_budget_nudge_count == 1


def test_check_enforcement_emits_anti_bot_nudge_before_budget_when_challenge_observed() -> None:
    ctx = _fresh_context()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    ctx.last_test_anti_bot = "Observed anti-bot challenge evidence before the run: human-verification"

    nudge = _check_enforcement(ctx)

    assert nudge == POST_ANTI_BOT_FAILED_TEST_NUDGE
    assert ctx.failed_test_nudge_count == 1
    assert ctx.per_tool_budget_nudge_count == 0


def test_final_reply_after_post_run_observation_bypasses_stale_budget_and_anti_bot_nudges() -> None:
    ctx = _fresh_context()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.composition_page_evidence = {
        "anti_bot_indicators": ["human-verification"],
        "challenge_controls": [{"selector": "#human-verification-widget"}],
    }
    _record_run_blocks_result(ctx, _budget_trip_result("wr_budget"))
    assert ctx.last_test_anti_bot is not None

    _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url="https://example.com/registry/search?sendid=494764",
        observed_data={"text": "Credential Number: CRED-000123"},
    )

    assert _check_enforcement(ctx, _reply_result("Observed result: CRED-000123 expires 01/31/2030.")) is None


def test_progress_reply_after_post_run_observation_still_gets_anti_bot_nudge() -> None:
    ctx = _fresh_context()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.composition_page_evidence = {"anti_bot_indicators": ["human-verification"]}
    _record_run_blocks_result(ctx, _budget_trip_result("wr_budget"))
    _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url="https://example.com/registry/search",
    )

    nudge = _check_enforcement(ctx, _reply_result("I will now proceed to inspect the result page."))

    assert nudge == POST_ANTI_BOT_FAILED_TEST_NUDGE


def test_check_enforcement_emits_budget_nudge_before_repeated_frontier_warn() -> None:
    """The budget trip is structural (chain too long) and must not be silently
    consumed by the repeated-frontier escalation, even when both signals fire
    at the same iteration."""
    ctx = _fresh_context()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    ctx.repeated_failure_streak_count = REPEATED_FRONTIER_STREAK_ESCALATE_AT

    nudge = _check_enforcement(ctx)
    assert nudge == POST_PER_TOOL_BUDGET_NUDGE


def test_check_enforcement_falls_through_to_failed_test_after_budget_cap() -> None:
    ctx = _fresh_context()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    ctx.per_tool_budget_nudge_count = MAX_PER_TOOL_BUDGET_NUDGES

    nudge = _check_enforcement(ctx)
    assert nudge == POST_FAILED_TEST_NUDGE


def test_failure_signature_is_stable_across_budget_trips_with_different_run_ids() -> None:
    """Without normalization the watchdog's run_id-laden message would make
    each trip hash uniquely and the streak would never accrue."""
    failure_categories = [{"category": PER_TOOL_BUDGET_FAILURE_CATEGORY, "confidence_float": 1.0}]
    sig_a = compute_failure_signature(
        frontier_start_label="block_a",
        failure_reason="per-tool-call budget exceeded (Run ID: wr_aaaa)",
        failure_categories=failure_categories,
        suspicious_success=False,
    )
    sig_b = compute_failure_signature(
        frontier_start_label="block_a",
        failure_reason="per-tool-call budget exceeded (Run ID: wr_bbbb)",
        failure_categories=failure_categories,
        suspicious_success=False,
    )
    assert sig_a is not None
    assert sig_a == sig_b


def test_failure_signature_changes_when_frontier_label_differs() -> None:
    """A budget trip on a different frontier should still produce a different
    signature so the streak resets when the agent meaningfully changes shape."""
    failure_categories = [{"category": PER_TOOL_BUDGET_FAILURE_CATEGORY, "confidence_float": 1.0}]
    sig_a = compute_failure_signature(
        frontier_start_label="block_a",
        failure_reason="per-tool-call budget exceeded (Run ID: wr_aaaa)",
        failure_categories=failure_categories,
        suspicious_success=False,
    )
    sig_b = compute_failure_signature(
        frontier_start_label="block_b",
        failure_reason="per-tool-call budget exceeded (Run ID: wr_bbbb)",
        failure_categories=failure_categories,
        suspicious_success=False,
    )
    assert sig_a != sig_b


def test_nudge_text_advises_splitting_chain() -> None:
    assert "STOP" in POST_PER_TOOL_BUDGET_NUDGE
    assert "split" in POST_PER_TOOL_BUDGET_NUDGE.lower() or "shrink" in POST_PER_TOOL_BUDGET_NUDGE.lower()
    assert (
        "verified-prefix" in POST_PER_TOOL_BUDGET_NUDGE.lower()
        or "verified prefix" in POST_PER_TOOL_BUDGET_NUDGE.lower()
    )
    assert "Do NOT retry the same chain" in POST_PER_TOOL_BUDGET_NUDGE
    assert "durable workflow blocks solely to rediscover page shape" in POST_PER_TOOL_BUDGET_NUDGE
    assert "challenge_state.gates_submit_controls=true" in POST_PER_TOOL_BUDGET_NUDGE
    assert "still-disabled submit/search control" in POST_PER_TOOL_BUDGET_NUDGE


def test_anti_bot_nudge_stops_repeating_disabled_submit_challenge_flow() -> None:
    assert "challenge_state.gates_submit_controls=true" in POST_ANTI_BOT_FAILED_TEST_NUDGE
    assert "still disabled after challenge resolution was attempted" in POST_ANTI_BOT_FAILED_TEST_NUDGE
    assert "Do NOT retry the same challenge solve" in POST_ANTI_BOT_FAILED_TEST_NUDGE


@pytest.mark.parametrize(
    "watchdog_phrase",
    ["timed out", "wait and retry", "try again with a different selector"],
)
def test_nudge_does_not_invite_generic_retry(watchdog_phrase: str) -> None:
    """The nudge must not lead the agent toward "wait and retry" — that's
    exactly the failure mode this fix exists to prevent."""
    assert watchdog_phrase not in POST_PER_TOOL_BUDGET_NUDGE.lower()


def _get_run_results_response(workflow_run_id: str, status: str) -> dict:
    return {"data": {"workflow_run_id": workflow_run_id, "overall_status": status}}


def test_reconciliation_clears_for_per_tool_budget_even_on_canceled_status() -> None:
    """The whole point of the budget exit is that the agent can issue a smaller
    chain in the same turn. ``canceled`` would normally NOT clear the guard,
    but for budget exits we know it was our own cancel."""
    ctx = _fresh_context()
    ctx.pending_reconciliation_run_id = "wr_1"
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY

    _maybe_clear_reconciliation_flag(ctx, _get_run_results_response("wr_1", "canceled"))

    assert ctx.pending_reconciliation_run_id is None


def test_reconciliation_does_not_clear_for_non_budget_canceled() -> None:
    """Non-budget watchdog cancels keep the existing strict semantics:
    ``canceled`` is ambiguous and must not silently clear the guard."""
    ctx = _fresh_context()
    ctx.pending_reconciliation_run_id = "wr_1"
    ctx.last_failure_category_top = None

    _maybe_clear_reconciliation_flag(ctx, _get_run_results_response("wr_1", "canceled"))

    assert ctx.pending_reconciliation_run_id == "wr_1"


def test_reconciliation_does_not_mark_user_input_for_non_final_poll() -> None:
    ctx = _fresh_context()
    ctx.pending_reconciliation_run_id = "wr_1"
    ctx.last_failure_category_top = None

    _maybe_clear_reconciliation_flag(ctx, _get_run_results_response("wr_1", "running"))

    assert ctx.pending_reconciliation_run_id == "wr_1"
    assert ctx.pending_reconciliation_requires_user_input is False


def test_new_pending_reconciliation_resets_inspected_canceled_flag() -> None:
    ctx = _fresh_context()
    ctx.pending_reconciliation_run_id = "wr_old"
    ctx.pending_reconciliation_requires_user_input = True

    _mark_pending_reconciliation_run(ctx, "wr_new")

    assert ctx.pending_reconciliation_run_id == "wr_new"
    assert ctx.pending_reconciliation_requires_user_input is False


def test_non_budget_canceled_reconciliation_suppresses_failed_test_nudge() -> None:
    ctx = _fresh_context()
    ctx.update_workflow_called = True
    ctx.test_after_update_done = True
    ctx.last_test_ok = False
    ctx.pending_reconciliation_run_id = "wr_1"
    ctx.last_failure_category_top = None

    _maybe_clear_reconciliation_flag(ctx, _get_run_results_response("wr_1", "canceled"))

    assert ctx.pending_reconciliation_run_id == "wr_1"
    assert ctx.pending_reconciliation_requires_user_input is True
    assert _check_enforcement(ctx) is None


def test_block_running_after_non_budget_canceled_inspection_does_not_request_get_results_again() -> None:
    ctx = _fresh_context()
    ctx.pending_reconciliation_run_id = "wr_1"
    ctx.last_failure_category_top = None

    _maybe_clear_reconciliation_flag(ctx, _get_run_results_response("wr_1", "canceled"))

    msg = _tool_loop_error(ctx, "update_and_run_blocks")

    assert msg is not None
    assert "get_run_results" not in msg
    assert "ask" in msg.lower() or "user" in msg.lower()


def test_reconciliation_clears_for_per_tool_budget_failed_status_too() -> None:
    """``failed`` would clear the guard regardless, but check the budget path
    doesn't accidentally regress that case."""
    ctx = _fresh_context()
    ctx.pending_reconciliation_run_id = "wr_1"
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY

    _maybe_clear_reconciliation_flag(ctx, _get_run_results_response("wr_1", "failed"))

    assert ctx.pending_reconciliation_run_id is None


def test_reconciliation_does_not_clear_when_run_id_mismatches() -> None:
    """Even with the budget category set, a get_run_results response for a
    different run_id must not clear the pending reconciliation."""
    ctx = _fresh_context()
    ctx.pending_reconciliation_run_id = "wr_1"
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY

    _maybe_clear_reconciliation_flag(ctx, _get_run_results_response("wr_other", "canceled"))

    assert ctx.pending_reconciliation_run_id == "wr_1"


def test_get_run_results_arms_problem_navigation_label_for_budget_run() -> None:
    ctx = _fresh_context()
    ctx.pending_reconciliation_run_id = "wr_1"
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY

    _record_per_tool_budget_problem_blocks_from_results(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_1",
                "overall_status": "canceled",
                "blocks": [
                    {"label": "open_results", "block_type": "GOTO_URL", "status": "completed"},
                    {"label": "apply_filters", "block_type": "NAVIGATION", "status": "canceled"},
                    {"label": "extract_results", "block_type": "EXTRACTION", "status": "created"},
                ],
            },
        },
    )

    assert ctx.per_tool_budget_problem_block_labels == ["apply_filters"]


def test_get_run_results_does_not_arm_problem_label_for_different_run() -> None:
    ctx = _fresh_context()
    ctx.pending_reconciliation_run_id = "wr_1"
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY

    _record_per_tool_budget_problem_blocks_from_results(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_other",
                "overall_status": "canceled",
                "blocks": [{"label": "apply_filters", "block_type": "NAVIGATION", "status": "canceled"}],
            },
        },
    )

    assert ctx.per_tool_budget_problem_block_labels == []


def test_tool_loop_error_blocks_rerun_of_problem_navigation_label() -> None:
    ctx = _fresh_context()
    ctx.per_tool_budget_problem_block_labels = ["apply_filters"]

    msg = _tool_loop_error(
        ctx,
        "run_blocks_and_collect_debug",
        {"block_labels": ["apply_filters"], "parameters": {}},
    )

    assert msg is not None
    assert "apply_filters" in msg
    assert "Do NOT rerun" in msg
    assert "live-page inspection evidence" in msg


def test_tool_loop_error_blocks_update_and_run_rerun_of_problem_navigation_label() -> None:
    ctx = _fresh_context()
    ctx.per_tool_budget_problem_block_labels = ["apply_filters"]

    msg = _tool_loop_error(
        ctx,
        "update_and_run_blocks",
        {"workflow_yaml": "title: wf", "block_labels": ["apply_filters"], "parameters": {}},
    )

    assert msg is not None
    assert "apply_filters" in msg
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_per_tool_budget_rerun"


def test_tool_loop_error_treats_missing_labels_as_rerun_all() -> None:
    ctx = _fresh_context()
    ctx.per_tool_budget_problem_block_labels = ["apply_filters"]

    msg = _tool_loop_error(ctx, "run_blocks_and_collect_debug", {"parameters": {}})

    assert msg is not None
    assert "apply_filters" in msg


def test_tool_loop_error_allows_new_smaller_label_after_budget_problem() -> None:
    ctx = _fresh_context()
    ctx.per_tool_budget_problem_block_labels = ["apply_filters"]

    assert (
        _tool_loop_error(
            ctx,
            "run_blocks_and_collect_debug",
            {"block_labels": ["click_parking"], "parameters": {}},
        )
        is None
    )


def test_tool_loop_error_blocks_challenge_gated_disabled_submit_rerun() -> None:
    ctx = _fresh_context()
    ctx.workflow_yaml = """
title: Registry standard credential lookup
workflow_definition:
  blocks:
    - label: open_registry_search
      block_type: goto_url
      url: https://example.com/registry/search
"""
    ctx.last_failed_workflow_yaml = ctx.workflow_yaml
    ctx.last_test_anti_bot = (
        "Observed anti-bot challenge evidence before the run: challenge-gated disabled submit/search control: Search"
    )
    ctx.last_test_failure_reason = (
        "The required Search button remains disabled after verification, and there is no enabled alternative."
    )
    ctx.composition_page_evidence = {
        "challenge_state": {
            "detected": True,
            "kind": "human_verification",
            "gates_submit_controls": True,
            "gated_submit_controls": [{"text": "Search", "disabled": True}],
        },
    }

    msg = _tool_loop_error(
        ctx,
        "update_and_run_blocks",
        {
            "workflow_yaml": ctx.workflow_yaml,
            "block_labels": ["fill_sample_record_search"],
        },
    )

    assert msg is not None
    assert "anti-bot challenge" in msg
    assert "disabled submit/search control" in msg
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_challenge_gated_submit_disabled"
    assert ctx.blocker_signal.renders_final_reply is True


def test_tool_loop_error_allows_challenge_gated_retry_with_changed_proxy_location() -> None:
    ctx = _fresh_context()
    ctx.last_failed_workflow_yaml = """
title: Registry standard credential lookup
workflow_definition:
  blocks:
    - label: open_registry_search
      block_type: goto_url
      url: https://example.com/registry/search
"""
    ctx.last_test_anti_bot = (
        "Observed anti-bot challenge evidence before the run: challenge-gated disabled submit/search control: Search"
    )
    ctx.last_test_failure_reason = "The required Search button remains disabled after verification."
    ctx.composition_page_evidence = {
        "challenge_state": {
            "detected": True,
            "kind": "human_verification",
            "gates_submit_controls": True,
            "gated_submit_controls": [{"text": "Search", "disabled": True}],
        },
    }
    changed_proxy_yaml = """
title: Registry standard credential lookup
proxy_location: RESIDENTIAL
workflow_definition:
  blocks:
    - label: open_registry_search
      block_type: goto_url
      url: https://example.com/registry/search
"""

    assert (
        _tool_loop_error(
            ctx,
            "update_and_run_blocks",
            {
                "workflow_yaml": changed_proxy_yaml,
                "block_labels": ["fill_sample_record_search"],
            },
        )
        is None
    )
    assert ctx.challenge_gated_proxy_retry_count == 1


def test_tool_loop_error_blocks_post_budget_challenge_even_with_changed_proxy_location() -> None:
    ctx = _fresh_context()
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    ctx.last_test_ok = False
    ctx.per_tool_budget_problem_block_labels = ["submit_registry_search"]
    ctx.last_failed_workflow_yaml = """
title: Registry standard credential lookup
workflow_definition:
  blocks:
    - label: open_registry_search
      block_type: goto_url
      url: https://example.com/registry/search
    - label: submit_registry_search
      block_type: navigation
      goal: Submit the credential search
"""
    ctx.composition_page_evidence = {
        "current_url": "https://example.com/registry/search",
        "workflow_run_id": "wr_budget",
        "observed_after_workflow_run": True,
        "anti_bot_indicators": ["human-verification"],
        "forms": [
            {
                "submit_controls": [
                    {
                        "text": "Search",
                        "selector": "#search",
                        "disabled": True,
                    }
                ]
            }
        ],
        "challenge_state": {
            "detected": True,
            "kind": "human_verification",
            "requires_human_verification": True,
            "gates_submit_controls": True,
            "gated_submit_controls": [{"text": "Search", "disabled": True}],
        },
    }
    changed_proxy_yaml = """
title: Registry standard credential lookup
proxy_location: US-CA
workflow_definition:
  blocks:
    - label: open_registry_search
      block_type: goto_url
      url: https://example.com/registry/search
    - label: submit_registry_search
      block_type: navigation
      goal: Submit the credential search
"""

    msg = _tool_loop_error(
        ctx,
        "update_and_run_blocks",
        {
            "workflow_yaml": changed_proxy_yaml,
            "block_labels": ["open_registry_search", "submit_registry_search"],
        },
    )

    assert msg is not None
    assert "bounded current-page inspection" in msg
    assert "disabled submit/search control" in msg
    assert "another proxy" in msg
    assert ctx.challenge_gated_proxy_retry_count == 0
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_post_budget_challenge_blocker"
    assert ctx.blocker_signal.renders_final_reply is True


def test_tool_loop_error_blocks_second_challenge_gated_proxy_retry() -> None:
    ctx = _fresh_context()
    ctx.challenge_gated_proxy_retry_count = 1
    ctx.last_failed_workflow_yaml = """
title: Registry standard credential lookup
workflow_definition:
  blocks:
    - label: open_registry_search
      block_type: goto_url
      url: https://example.com/registry/search
"""
    ctx.last_test_anti_bot = "Extracted data reported anti-bot blocker: Verify you are human"
    ctx.last_test_failure_reason = "Run completed, but extracted data reported a blocker: Verify you are human"
    changed_proxy_yaml = """
title: Registry standard credential lookup
proxy_location: US-NY
workflow_definition:
  blocks:
    - label: open_registry_search
      block_type: goto_url
      url: https://example.com/registry/search
"""

    msg = _tool_loop_error(
        ctx,
        "update_and_run_blocks",
        {
            "workflow_yaml": changed_proxy_yaml,
            "block_labels": ["search_sample_record"],
        },
    )

    assert msg is not None
    assert "anti-bot challenge" in msg
    assert "blocker" in msg
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_challenge_gated_submit_disabled"
    assert ctx.blocker_signal.renders_final_reply is True


def test_terminal_anti_bot_blocker_requires_gated_submit_evidence_for_disabled_failure() -> None:
    ctx = _fresh_context()
    ctx.last_test_anti_bot = "Observed anti-bot challenge evidence before the run: human-verification"
    ctx.last_test_failure_reason = "The page has a disabled button unrelated to the challenged search path."
    ctx.composition_page_evidence = {
        "challenge_state": {
            "detected": True,
            "kind": "human_verification",
            "gates_submit_controls": False,
        }
    }

    assert _last_run_has_terminal_anti_bot_blocker(ctx) is False


def test_record_run_blocks_treats_structured_anti_bot_blocker_as_failed_test() -> None:
    ctx = _fresh_context()
    ctx.workflow_yaml = "workflow_definition: {blocks: []}"
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_blocked",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "extract_sample_credentials",
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {
                        "extracted_information": {
                            "results_exist": None,
                            "credentials": [],
                            "no_results_message": None,
                            "blocker_message": "Verify you are human",
                        }
                    },
                }
            ],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_test_anti_bot is not None
    assert "Verify you are human" in ctx.last_test_failure_reason
    assert ctx.last_failed_workflow_yaml == "workflow_definition: {blocks: []}"
    assert result["data"]["failure_reason"] == (
        "Run completed, but extracted data reported a blocker: Verify you are human"
    )
    assert result["data"]["failure_categories"][0]["category"] == "ANTI_BOT_DETECTION"


def test_tool_loop_error_blocks_upstream_replay_after_post_budget_page_evidence() -> None:
    ctx = _fresh_context()
    ctx.per_tool_budget_problem_block_labels = ["apply_filters"]
    ctx.composition_page_evidence = {
        "current_url": "https://example.com/results",
        "workflow_run_id": "wr_budget",
        "observed_after_workflow_run": True,
    }
    workflow_yaml = """
workflow_definition:
  blocks:
    - label: open_site
      block_type: goto_url
      url: https://example.com
    - label: open_search
      block_type: navigation
      goal: Open search
    - label: apply_filters
      block_type: navigation
      goal: Apply filters
"""

    msg = _tool_loop_error(
        ctx,
        "update_and_run_blocks",
        {
            "workflow_yaml": workflow_yaml,
            "block_labels": ["open_search"],
            "parameters": {},
        },
    )

    assert msg is not None
    assert "Do NOT restart upstream" in msg
    assert "open_search" in msg
    assert "apply_filters" in msg
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_post_budget_upstream_replay"


def test_tool_loop_error_allows_downstream_recovery_after_post_budget_page_evidence() -> None:
    ctx = _fresh_context()
    ctx.per_tool_budget_problem_block_labels = ["apply_filters"]
    ctx.composition_page_evidence = {
        "current_url": "https://example.com/results",
        "workflow_run_id": "wr_budget",
        "observed_after_workflow_run": True,
    }
    workflow_yaml = """
workflow_definition:
  blocks:
    - label: open_site
      block_type: goto_url
      url: https://example.com
    - label: open_search
      block_type: navigation
      goal: Open search
    - label: apply_filters
      block_type: navigation
      goal: Apply filters
    - label: expand_results
      block_type: navigation
      goal: Expand results
"""

    assert (
        _tool_loop_error(
            ctx,
            "update_and_run_blocks",
            {
                "workflow_yaml": workflow_yaml,
                "block_labels": ["expand_results"],
                "parameters": {},
            },
        )
        is None
    )


def test_successful_workflow_update_keeps_problem_label_while_still_navigation() -> None:
    ctx = _fresh_context()
    ctx.workflow_yaml = "updated yaml"
    ctx.per_tool_budget_problem_block_labels = ["apply_filters"]
    workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[
                SimpleNamespace(block_type="navigation", label="apply_filters"),
                SimpleNamespace(block_type="extraction", label="extract_results"),
            ]
        )
    )

    _record_workflow_update_result(ctx, {"ok": True, "_workflow": workflow, "data": {"block_count": 2}})

    assert ctx.per_tool_budget_problem_block_labels == ["apply_filters"]


def test_successful_workflow_update_clears_problem_label_changed_away_from_navigation() -> None:
    ctx = _fresh_context()
    ctx.workflow_yaml = "updated yaml"
    ctx.per_tool_budget_problem_block_labels = ["apply_filters"]
    workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[
                SimpleNamespace(block_type="code", label="apply_filters"),
                SimpleNamespace(block_type="navigation", label="click_parking"),
            ]
        )
    )

    _record_workflow_update_result(ctx, {"ok": True, "_workflow": workflow, "data": {"block_count": 2}})

    assert ctx.per_tool_budget_problem_block_labels == []


def test_nudge_counter_resets_on_success() -> None:
    """The counter must reset so a budget trip on a different chain in the
    same turn gets the dedicated split nudge again."""
    ctx = _fresh_context()
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    ctx.per_tool_budget_nudge_count = MAX_PER_TOOL_BUDGET_NUDGES

    success = {
        "ok": True,
        "data": {
            "blocks": [
                {
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {"price": 10},
                }
            ]
        },
    }
    _record_run_blocks_result(ctx, success)

    assert ctx.per_tool_budget_nudge_count == 0


def test_nudge_counter_resets_when_failure_changes_to_non_budget() -> None:
    ctx = _fresh_context()
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    ctx.per_tool_budget_nudge_count = MAX_PER_TOOL_BUDGET_NUDGES

    other_failure = {
        "ok": False,
        "error": "boom",
        "data": {
            "blocks": [{"status": "failed", "failure_reason": "something else"}],
            "failure_categories": [{"category": "PARAMETER_BINDING_ERROR"}],
        },
    }
    _record_run_blocks_result(ctx, other_failure)

    assert ctx.per_tool_budget_nudge_count == 0
    assert ctx.last_failure_category_top == "PARAMETER_BINDING_ERROR"


def test_nudge_counter_preserved_across_consecutive_budget_trips() -> None:
    """Two consecutive budget trips should not reset the counter; otherwise
    the cap would never kick in."""
    ctx = _fresh_context()
    ctx.last_failure_category_top = PER_TOOL_BUDGET_FAILURE_CATEGORY
    ctx.per_tool_budget_nudge_count = 1

    _record_run_blocks_result(ctx, _budget_trip_result(workflow_run_id="wr_2"))

    assert ctx.per_tool_budget_nudge_count == 1
    assert ctx.last_failure_category_top == PER_TOOL_BUDGET_FAILURE_CATEGORY
