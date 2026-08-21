"""Tests for per-tool-call budget runtime recording.

Covers four surfaces:

- ``_record_run_blocks_result`` — the ``PER_TOOL_BUDGET`` failure-category
  entry must land on ``last_failure_category_top``.
- ``compute_failure_signature`` — the run_id baked into the watchdog
  message must not make consecutive trips hash differently.
- ``_maybe_clear_reconciliation_flag`` — a ``canceled`` row clears the
  guard for budget exits, but not for other watchdog cancels.
"""

from __future__ import annotations

from skyvern.forge.sdk.copilot.blocker_signal import (
    assert_clean_user_facing_text,
    contains_internal_machinery_leak,
)
from skyvern.forge.sdk.copilot.failure_tracking import (
    PER_TOOL_BUDGET_FAILURE_CATEGORY,
)
from skyvern.forge.sdk.copilot.tools import (
    WatchdogExitReason,
    _composition_anti_bot_reason,
    _record_run_blocks_result,
)
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _per_tool_budget_failure_category,
    _watchdog_user_facing_summary,
)
from skyvern.forge.sdk.copilot.turn_halt import TurnHaltKind
from tests.unit.conftest import make_copilot_context as _fresh_context

_BUDGET_RUN_ID = "wr_1234567890"
_BUDGET_SESSION_ID = "pbs_9876543210"
_BUDGET_SECONDS = 240
_BUDGET_SENTENCE = (
    "The run was still making progress but ran longer than the 240s allowed for a single step, so it was stopped."
)


def _budget_trip_result(workflow_run_id: str = _BUDGET_RUN_ID) -> dict:
    summary = _watchdog_user_facing_summary("per_tool_budget", _BUDGET_SECONDS, None)
    return {
        "ok": False,
        "error": (
            f"The run exceeded the {_BUDGET_SECONDS}s per-tool-call budget while still making progress. "
            f"Run ID: {workflow_run_id}. Next step: call get_run_results with this workflow_run_id."
        ),
        "data": {
            "workflow_run_id": workflow_run_id,
            "overall_status": "running",
            "failure_reason": summary,
            "user_facing_summary": summary,
            "control_signal": {"kind": "watchdog_per_tool_budget", "user_facing_summary": summary},
            "browser_session_id": _BUDGET_SESSION_ID,
            "blocks": [
                {"label": "sign_in", "status": "completed"},
                {"label": "read_statement_total", "status": "running"},
            ],
            "failure_categories": [_per_tool_budget_failure_category(_BUDGET_SECONDS)],
        },
    }


def test_record_sets_top_category_on_per_tool_budget_result() -> None:
    ctx = _fresh_context()
    _record_run_blocks_result(ctx, _budget_trip_result())
    assert ctx.last_failure_category_top == PER_TOOL_BUDGET_FAILURE_CATEGORY
    assert ctx.last_run_blocks_workflow_run_id == _BUDGET_RUN_ID
    assert ctx.last_successful_run_blocks_workflow_run_id is None


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

    _record_run_blocks_result(ctx, _budget_trip_result())

    assert ctx.last_failure_category_top == PER_TOOL_BUDGET_FAILURE_CATEGORY
    assert ctx.last_test_anti_bot is not None
    assert "human-verification" in ctx.last_test_anti_bot
    assert "challenge-gated disabled submit/search control: Search" in ctx.last_test_anti_bot


def test_composition_anti_bot_reason_reads_typed_challenge_state_without_legacy_indicators() -> None:
    ctx = _fresh_context()
    ctx.composition_page_evidence = {
        "observed_after_workflow_run": True,
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


def test_record_relays_the_id_free_reason_while_the_model_error_keeps_the_id() -> None:
    ctx = _fresh_context()
    ctx.last_test_failure_reason = f"An earlier run stalled. Run ID: {_BUDGET_RUN_ID}. Outcome is uncertain."
    result = _budget_trip_result()

    _record_run_blocks_result(ctx, result)

    data = result["data"]
    relayed = ctx.last_test_failure_reason
    assert relayed == _BUDGET_SENTENCE
    for field in (
        relayed,
        data["failure_reason"],
        data["user_facing_summary"],
        data["control_signal"]["user_facing_summary"],
    ):
        assert _BUDGET_RUN_ID not in field
        assert _BUDGET_SESSION_ID not in field
        assert contains_internal_machinery_leak(field) is False
        assert_clean_user_facing_text(field)
    assert "get_run_results" not in relayed
    assert f"Run ID: {_BUDGET_RUN_ID}" in result["error"]


def test_watchdog_user_relayed_text_excludes_next_tool_instruction() -> None:
    exit_reason: WatchdogExitReason = "per_tool_budget"
    reason = _watchdog_user_facing_summary(exit_reason, _BUDGET_SECONDS, None)

    assert reason == _BUDGET_SENTENCE
    assert "per-tool-call budget" not in reason
    assert _BUDGET_RUN_ID not in reason
    assert contains_internal_machinery_leak(reason) is False
    assert_clean_user_facing_text(reason)
    assert "get_run_results" not in reason
    assert "update_and_run_blocks" not in reason


def test_per_tool_budget_failure_category_reasoning_clears_the_guard() -> None:
    category = _per_tool_budget_failure_category(_BUDGET_SECONDS)

    assert category["category"] == PER_TOOL_BUDGET_FAILURE_CATEGORY
    reasoning = str(category["reasoning"])
    assert reasoning == (
        "The run was making progress but ran past the 240s allowed for a single step, so it cannot fit in one call."
    )
    assert contains_internal_machinery_leak(reasoning) is False
    assert_clean_user_facing_text(reasoning)


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


def _prose_blocker_run_result() -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_blocked",
            "overall_status": "completed",
            "failure_categories": [{"category": "PARAMETER_BINDING_ERROR", "confidence_float": 0.95}],
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


def test_record_run_blocks_keeps_prose_blocker_message_out_of_terminal_challenge() -> None:
    ctx = _fresh_context()
    ctx.workflow_yaml = "workflow_definition: {blocks: []}"
    result = _prose_blocker_run_result()

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_test_anti_bot is None
    assert ctx.last_test_failure_reason is None
    categories = result["data"]["failure_categories"]
    assert [category["category"] for category in categories] == ["PARAMETER_BINDING_ERROR"]
    assert ctx.last_failure_category_top == "PARAMETER_BINDING_ERROR"
    assert ctx.last_run_outcome is None or ctx.last_run_outcome.reason_code != "terminal_challenge_blocker"


def test_record_run_blocks_treats_typed_anti_bot_flag_as_terminal_challenge() -> None:
    ctx = _fresh_context()
    ctx.workflow_yaml = "workflow_definition: {blocks: []}"
    result = _prose_blocker_run_result()
    result["data"]["blocks"][0]["extracted_data"]["extracted_information"]["human_verification_required"] = True

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_anti_bot is not None
    assert ctx.last_failed_workflow_yaml == "workflow_definition: {blocks: []}"
    categories = result["data"]["failure_categories"]
    assert categories[0]["category"] == "PARAMETER_BINDING_ERROR"
    assert categories[1]["category"] == "ANTI_BOT_DETECTION"
    assert categories[1]["evidence_source"] == "artifact"
    assert ctx.last_failure_category_top == "ANTI_BOT_DETECTION"
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "terminal_challenge_blocker"
    assert ctx.turn_halt is not None


def test_record_run_blocks_treats_structured_browser_access_blocker_as_terminal_challenge() -> None:
    ctx = _fresh_context()
    ctx.workflow_yaml = "workflow_definition: {blocks: []}"
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_blocked",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "search_sample_records",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {
                        "status": "blocked",
                        "blocker_type": "browser_port_forbidden",
                        "blocker_evidence": (
                            "The browser refused to render the requested localhost port before the search page loaded."
                        ),
                        "records": [],
                        "record_count": 0,
                    },
                }
            ],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_anti_bot is not None
    assert result["data"]["failure_reason"] == "Run output reported a blocker: browser_port_forbidden"
    assert result["data"]["failure_categories"][0]["category"] == "ANTI_BOT_DETECTION"
    assert ctx.last_failure_category_top == "ANTI_BOT_DETECTION"
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "terminal_challenge_blocker"
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE


def test_record_run_blocks_prefers_nested_port_blocker_over_status_shell() -> None:
    ctx = _fresh_context()
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_blocked",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "search_sample_records",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {
                        "status": "blocked",
                        "query": "Sample",
                        "records": [],
                        "record_count": 0,
                        "blocker": {
                            "type": "browser_or_environment_port_block",
                            "message": "The page shows a port-forbidden error instead of the target search UI.",
                        },
                        "evidence_text": "Requested port 8900 is forbidden",
                    },
                }
            ],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is False
    assert ctx.last_failure_category_top == "ANTI_BOT_DETECTION"
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "terminal_challenge_blocker"
    assert ctx.turn_halt is not None
    assert "Requested port" in ctx.turn_halt.extra["evidence_reason"]


def test_record_run_blocks_keyword_only_top_category_is_not_latched() -> None:
    ctx = _fresh_context()
    result = {
        "ok": False,
        "error": "run failed",
        "data": {
            "workflow_run_id": "wr_failed",
            "overall_status": "failed",
            "failure_reason": "Cloudflare interstitial was displayed while the page loaded.",
            "failure_categories": [
                {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.7, "evidence_source": "keyword_only"},
                {"category": "PAGE_LOAD_TIMEOUT", "confidence_float": 0.8},
            ],
            "blocks": [],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_failure_category_top == "PAGE_LOAD_TIMEOUT"
    assert ctx.last_test_anti_bot is None
    assert ctx.turn_halt is None


def test_watchdog_cancel_with_stale_challenge_markup_is_not_promoted() -> None:
    ctx = _fresh_context()
    ctx.composition_page_evidence = {
        "anti_bot_indicators": ["turnstile"],
        "challenge_controls": [],
        "challenge_state": {
            "detected": True,
            "kind": "captcha",
            "requires_human_verification": False,
            "gates_submit_controls": False,
        },
    }
    result = {
        "ok": False,
        "error": "Run canceled after 90s of stagnation while a Cloudflare interstitial was displayed.",
        "data": {
            "workflow_run_id": "wr_cancelled",
            "overall_status": "canceled",
            "failure_reason": "Run canceled after stagnation.",
            "blocks": [],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_anti_bot is None
    assert ctx.last_failure_category_top != "ANTI_BOT_DETECTION"
    assert ctx.turn_halt is None
    assert ctx.blocker_signal is None


def test_record_run_blocks_combines_status_blocked_with_page_challenge_evidence() -> None:
    ctx = _fresh_context()
    ctx.composition_page_evidence = {
        "challenge_state": {
            "detected": True,
            "kind": "captcha",
            "requires_human_verification": True,
            "gates_submit_controls": True,
            "gated_submit_controls": [{"text": "Search", "disabled": True}],
        },
        "anti_bot_indicators": ["captcha", "verify you are human"],
    }
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_blocked",
            "overall_status": "completed",
            "blocks": [
                {
                    "label": "search_sample_records",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {
                        "status": "blocked",
                        "records": [],
                        "record_count": 0,
                    },
                }
            ],
        },
    }

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_failure_category_top == "ANTI_BOT_DETECTION"
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "terminal_challenge_blocker"
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    assert "challenge-gated disabled submit/search control: Search" in ctx.turn_halt.extra["evidence_reason"]
    assert "Run output reported" in ctx.turn_halt.extra["evidence_reason"]
