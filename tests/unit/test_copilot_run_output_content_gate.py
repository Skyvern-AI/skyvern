"""Deterministic content gate over completed code-block run outputs.

Fixtures model a public registry site with a search form and expandable
result rows; domains and person names are generic placeholders.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.agent import _verified_workflow_or_none
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import verified_goal_satisfied_context
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.tools import (
    _analyze_run_blocks,
    _current_workflow_has_evidence_block,
    _is_outcome_evidence_candidate,
    _is_unfinished_run_verification_candidate,
    _record_run_blocks_result,
    _run_blocks_structured_blocker_message,
)
from skyvern.forge.sdk.copilot.turn_halt import TurnHaltKind


def _code_block(label: str, extracted: Any, *, block_type: str = "CODE") -> dict[str, Any]:
    return {"label": label, "block_type": block_type, "status": "completed", "extracted_data": extracted}


def _run_result(blocks: list[dict[str, Any]], *, ok: bool = True) -> dict[str, Any]:
    return {
        "ok": ok,
        "data": {
            "workflow_run_id": "wr_test",
            "overall_status": "completed" if ok else "failed",
            "current_url": "https://registry.example.com/search",
            "blocks": blocks,
        },
    }


def _ctx(blocks: list[dict[str, Any]] | None = None) -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="blocks: []",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        user_message="search the public registry for a person and expand their result rows",
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[CompletionCriterion(id="c0", outcome="result rows extracted")]
    )
    labels = [block["label"] for block in (blocks or [])]
    workflow_blocks = [SimpleNamespace(block_type="code", label=label) for label in labels]
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=workflow_blocks))  # type: ignore[assignment]
    ctx.last_workflow_yaml = "blocks: []"
    ctx.verified_prefix_labels = labels
    return ctx


def _no_evidence(cid: str) -> CompletionVerificationResult:
    verdict = CriterionVerdict(criterion_id=cid, satisfied=False, reason_code="no_evidence")
    return CompletionVerificationResult(status="evaluated", criterion_ids=[cid], verdicts=[verdict])


def _blocked_flag_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block("open_registry_search", {"submit_button_enabled": False}),
            _code_block(
                "search_registry_person",
                {
                    "anti_bot_blocked": True,
                    "blocker": "The search form is gated by a human verification challenge; the search never ran.",
                    "has_results": False,
                    "records": [],
                },
            ),
        ]
    )


def _blocked_status_run_result(block_type: str = "CODE") -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "search_registry_person",
                {"status": "blocked_by_challenge", "records": []},
                block_type=block_type,
            )
        ]
    )


def _genuine_success_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "search_registry_person",
                {"result_row_count": 1, "visible_results_evidence": "DOE, JANE - Status: Active"},
            ),
            _code_block(
                "expand_result_rows",
                {
                    "results_found": 2,
                    "records": [
                        {"name": "DOE, JANE", "detail": "Row A", "status": "Active"},
                        {"name": "DOE, JANE", "detail": "Row B", "status": "Active"},
                    ],
                },
            ),
        ]
    )


def _terminal_metadata_entry(label: str = "search_registry_person") -> dict[str, Any]:
    return {
        "block_label": label,
        "declared_goal": "extract result rows for the requested person",
        "claimed_outcomes": [
            {
                "id": "claim:goal",
                "scope": "outcome",
                "text": "result rows extracted",
                "status": "observed_not_verified",
                "covered_criteria": ["criterion:goal_0"],
            }
        ],
        "completion_criteria": [
            {"id": "criterion:goal_0", "text": "result rows extracted", "level": "terminal", "terminal": True}
        ],
    }


def test_blocked_flag_run_reports_structured_blocker() -> None:
    blocker = _run_blocks_structured_blocker_message(_blocked_flag_run_result())
    assert blocker is not None
    assert "human verification challenge" in blocker


def test_blocked_flag_run_records_suspicious_success() -> None:
    result = _blocked_flag_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert result["ok"] is False
    assert result["error"] == ctx.last_test_failure_reason
    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False
    assert "reported a blocker" in (ctx.last_test_failure_reason or "")
    assert ctx.last_failed_workflow_yaml == "blocks: []"
    categories = result["data"]["failure_categories"]
    assert any(category["category"] == "ANTI_BOT_DETECTION" for category in categories)
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_run_output_terminal_blocker"
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    assert verified_goal_satisfied_context(ctx) is False
    assert _verified_workflow_or_none(ctx) == (None, None)
    snapshot = getattr(ctx, "outcome_verification_trace_snapshot", {})
    assert snapshot.get("run_output_blocker_detected") is True


def test_blocked_flag_run_is_never_a_judge_candidate() -> None:
    result = _blocked_flag_run_result()
    ctx = _ctx(result["data"]["blocks"])
    assert _is_outcome_evidence_candidate(ctx, result) is False


def test_candidacy_and_recording_agree_on_blocked_run() -> None:
    result = _blocked_flag_run_result()
    ctx = _ctx(result["data"]["blocks"])

    assert _is_outcome_evidence_candidate(ctx, result) is False
    failed_variant = {**result, "ok": False}
    assert _is_unfinished_run_verification_candidate(ctx, failed_variant) is False

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is True


@pytest.mark.parametrize("block_type", ["CODE", "code"])
def test_blocked_status_value_rejected_deterministically(block_type: str) -> None:
    result = _blocked_status_run_result(block_type)
    ctx = _ctx(result["data"]["blocks"])

    blocker = _run_blocks_structured_blocker_message(result)
    assert blocker is not None
    assert "blocked_by_challenge" in blocker
    assert _is_outcome_evidence_candidate(ctx, result) is False

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert result["ok"] is False
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_run_output_terminal_blocker"
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False


def test_genuine_success_run_keeps_clean_path() -> None:
    result = _genuine_success_run_result()
    ctx = _ctx(result["data"]["blocks"])

    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _ = _analyze_run_blocks(result)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True


def test_empty_goal_collections_without_blocker_are_flagged() -> None:
    result = _run_result([_code_block("search_registry_person", {"records": [], "result_count": 0})])
    ctx = _ctx(result["data"]["blocks"])

    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _ = _analyze_run_blocks(result)
    assert empty_data_blocks is True
    assert _is_outcome_evidence_candidate(ctx, result) is False

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert ctx.last_test_ok is None
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False


def test_neutral_status_string_does_not_redeem_empty_collections() -> None:
    result = _run_result([_code_block("search_registry_person", {"status": "completed", "records": []})])
    _, empty_data_blocks, _ = _analyze_run_blocks(result)
    assert empty_data_blocks is True


def test_falsy_blocker_flags_and_action_only_outputs_do_not_trip() -> None:
    result = _run_result(
        [
            _code_block("open_registry_search", {"anti_bot_blocked": False, "clicked": True}),
            _code_block("accept_terms", {"clicked": True}),
            {"label": "scroll_results", "block_type": "CODE", "status": "completed"},
        ]
    )
    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _ = _analyze_run_blocks(result)
    assert empty_data_blocks is False


def test_flag_rule_requires_strict_blocker_terms() -> None:
    benign = _run_result([_code_block("notify", {"verification_passed": True})])
    assert _run_blocks_structured_blocker_message(benign) is None

    string_rule_parity = _run_result([_code_block("notify", {"verification_code_sent": "yes"})])
    assert _run_blocks_structured_blocker_message(string_rule_parity) == "yes"


def test_flag_rule_synthesizes_message_and_prefers_sibling_reason() -> None:
    flag_only = _run_result([_code_block("search", {"captcha_required": True, "clicked": True})])
    blocker = _run_blocks_structured_blocker_message(flag_only)
    assert blocker is not None
    assert "captcha" in blocker

    flag_with_reason = _run_result(
        [_code_block("search", {"blocked_by_challenge": True, "reason": "The submit control stayed disabled."})]
    )
    assert _run_blocks_structured_blocker_message(flag_with_reason) == "The submit control stayed disabled."


def test_positive_status_value_with_strict_term_still_trips_blocker() -> None:
    result = _run_result([_code_block("search", {"status": "captcha_solved", "records": [{"name": "DOE, JANE"}]})])
    blocker = _run_blocks_structured_blocker_message(result)
    assert blocker is not None
    assert "captcha_solved" in blocker


def test_status_rule_ignores_long_values_and_matches_state_key() -> None:
    long_value = _run_result([_code_block("search", {"status": "x" * 100 + " challenge"})])
    assert _run_blocks_structured_blocker_message(long_value) is None

    state_value = _run_result([_code_block("search", {"state": "captcha_pending"})])
    blocker = _run_blocks_structured_blocker_message(state_value)
    assert blocker is not None
    assert "captcha_pending" in blocker


def test_extraction_payload_flag_semantics_unchanged() -> None:
    block = {
        "label": "extract_rows",
        "block_type": "EXTRACTION",
        "status": "completed",
        "extracted_data": {"extracted_information": {"blocked": True}},
    }
    assert _run_blocks_structured_blocker_message(_run_result([block])) is None


def test_code_only_workflow_with_seam_metadata_counts_as_evidence_block() -> None:
    blocks = [_code_block("search_registry_person", {})]
    ctx = _ctx(blocks)
    assert _current_workflow_has_evidence_block(ctx) is False

    ctx.code_artifact_metadata = {"search_registry_person": _terminal_metadata_entry()}
    assert _current_workflow_has_evidence_block(ctx) is True


def test_metadata_without_terminal_coverage_is_not_an_evidence_block() -> None:
    entry = _terminal_metadata_entry()
    entry["completion_criteria"][0]["level"] = "prefix"
    entry["completion_criteria"][0]["terminal"] = False
    ctx = _ctx([_code_block("search_registry_person", {})])
    ctx.code_artifact_metadata = {"search_registry_person": entry}
    assert _current_workflow_has_evidence_block(ctx) is False


def test_judge_no_evidence_warrants_repair_for_code_only_workflow_with_metadata() -> None:
    result = _genuine_success_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"search_registry_person": _terminal_metadata_entry()}

    _record_run_blocks_result(ctx, result, completion_verification=_no_evidence("c0"))

    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False


def test_judge_no_evidence_keeps_building_without_metadata() -> None:
    result = _genuine_success_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=_no_evidence("c0"))

    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True
