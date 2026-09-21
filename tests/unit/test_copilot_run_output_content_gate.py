"""Deterministic content gate over completed code-block run outputs.

Fixtures model a public registry site with a search form and expandable
result rows; domains and person names are generic placeholders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.agent import _verified_workflow_or_none
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    grade_fallback_floor_reached_end_state_criteria,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import verified_goal_satisfied_context
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy, build_classifier_fallback_floor
from skyvern.forge.sdk.copilot.terminal_predicates import outcome_fully_verified
from skyvern.forge.sdk.copilot.tools import (
    _analyze_run_blocks,
    _current_workflow_has_evidence_block,
    _is_outcome_evidence_candidate,
    _is_unfinished_run_verification_candidate,
    _record_run_blocks_result,
    _run_blocks_structured_blocker_message,
    run_execution,
)
from skyvern.forge.sdk.copilot.tools._shared import _registered_output_parameter_payloads
from skyvern.forge.sdk.copilot.tools.blockers import _PROJECTED_BLOCK_FACT_KEYS, _code_output_has_goal_content
from skyvern.forge.sdk.copilot.tools.completion import _apply_present_value_upgrades, _build_run_evidence_snapshot
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _attach_block_fact_projection,
    _attach_registered_output_parameter_values,
    build_test_evidence_packet,
)
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.schemas.workflows import BlockType


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {
                "registered_output_parameter_values": [
                    {"workflow_run_id": "wr_prior", "value": {"x": 1}},
                    {"value": {"y": 2}},
                ]
            },
            [],
        ),
        (
            {
                "workflow_run_id": "wr_now",
                "registered_output_parameter_values": [
                    {"workflow_run_id": "wr_now", "value": {"x": 1}},
                    {"workflow_run_id": "wr_prior", "value": {"y": 2}},
                ],
            },
            [{"x": 1}],
        ),
    ],
)
def test_registered_output_parameter_payloads_scope_to_current_run(
    data: dict[str, Any], expected: list[dict[str, int]]
) -> None:
    assert [dict(p["value"]) for p in _registered_output_parameter_payloads(data)] == expected


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


def _structured_record_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "entity_found": True,
        "entity_name": "Jordan Example",
        "record_number": "1234567890",
        "items": [
            {"item_label": "Sample Practice", "address": "100 Main St, Example City, ST 12345", "status": "Active"}
        ],
        "overall_status": "Active",
        "evidence_text": "Opened Details page",
    }
    payload.update(overrides)
    return payload


def _validation_review_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "validation_only": True,
        "submit_or_finalize_clicked": False,
        "pre_submit_review_reached": True,
        "final_controls_visible": ["Submit Request", "Back"],
        "review_values": {
            "Service Address": "1234 Sample Utility Way, Testville, CA 94016",
            "Requested Start Date": "2026-06-22",
        },
        "evidence_text": (
            "Start Service - Review\n"
            "Service Address\n1234 Sample Utility Way, Testville, CA 94016\n"
            "Requested Start Date\n2026-06-22\n"
            "Submit Request\nBack"
        ),
    }
    payload.update(overrides)
    return payload


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
    ctx.composition_verified_labels = list(labels)
    return ctx


def _gating_challenge_evidence() -> dict[str, Any]:
    return {
        "challenge_state": {
            "detected": True,
            "kind": "captcha",
            "requires_human_verification": True,
            "gates_submit_controls": True,
            "gated_submit_controls": [{"text": "Search", "disabled": True}],
        },
        "anti_bot_indicators": ["captcha", "verify you are human"],
    }


def _no_evidence(cid: str) -> CompletionVerificationResult:
    verdict = CriterionVerdict(criterion_id=cid, state="unsatisfied", reason_code="no_evidence")
    return CompletionVerificationResult(status="evaluated", criterion_ids=[cid], verdicts=[verdict])


def _satisfied(*criterion_ids: str) -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=list(criterion_ids),
        verdicts=[
            CriterionVerdict(criterion_id=criterion_id, state="satisfied", reason_code="evidence_confirms")
            for criterion_id in criterion_ids
        ],
    )


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


def _domain_blocker_run_result() -> dict[str, Any]:
    payload = {
        "login_only": True,
        "blocked_by": "online_account_required",
        "public_form_exists": False,
        "visible_page_path_label": "Account login page",
        "recommended_next_action": "Ask the user for online account access before continuing.",
        "safety_flags": {
            "no_sensitive_data_entered": True,
            "no_submission_attempted": True,
        },
    }
    block = _code_block("inspect_access_path", payload)
    block["output"] = payload
    return _run_result([block])


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


def _all_null_goal_fields_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "expand_result_rows",
                {
                    "search_completed": True,
                    "no_results": False,
                    "certification_records": [
                        {
                            "name": "Generic Credential A",
                            "number": None,
                            "expiration_date": None,
                            "evidence_text": "Navigation menu text: Generic Credential A",
                        },
                        {
                            "name": "Generic Credential B",
                            "number": "",
                            "expiration_date": None,
                            "evidence_text": "Footer text from registry.example.com",
                        },
                    ],
                },
            )
        ]
    )


def _goal_field_success_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "expand_result_rows",
                {
                    "search_completed": True,
                    "certification_records": [
                        {
                            "name": "DOE, JANE",
                            "number": "12345",
                            "expiration_date": "2027-01-31",
                            "evidence_text": "DOE, JANE - credential 12345 expires 2027-01-31",
                        }
                    ],
                },
            )
        ]
    )


def _boolean_goal_path_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "inspect_access_path",
                {
                    "public_form_exists": False,
                    "login_only": True,
                },
            )
        ]
    )


def _domain_path_summary_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "inspect_access_path",
                {
                    "public_form_exists": False,
                    "login_only": True,
                    "visible_page_path_label": "Start service sign-in gate",
                    "recommended_next_action": "Stop before account-specific setup.",
                },
            )
        ]
    )


def _domain_path_alias_summary_run_result(*, include_null_alias_source: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "public_form_exists": False,
        "path_is_login_only": True,
        "visible_page_path_label": "Start service sign-in gate",
        "recommended_next_action": "Stop before account-specific setup.",
    }
    if include_null_alias_source:
        payload["login_only"] = None
    return _run_result([_code_block("inspect_access_path", payload)])


def _nested_code_output_extraction_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "extract_record_status_info",
                {
                    "extract_record_status_info_output": _structured_record_payload(),
                    "extracted_information": [],
                },
                block_type="EXTRACTION",
            )
        ]
    )


def _empty_extraction_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "extract_empty_results",
                {"extracted_information": [], "downloaded_files": [], "downloaded_file_urls": None},
                block_type="EXTRACTION",
            )
        ]
    )


def _registered_code_output_parameter_run_result(*, workflow_run_id: str = "wr_test") -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_test",
            "overall_status": "completed",
            "blocks": [],
            "registered_output_parameter_values": [
                {
                    "workflow_run_id": workflow_run_id,
                    "output_parameter_id": "op_details",
                    "output_parameter_key": "extract_record_status_details_output",
                    "block_label": "extract_record_status_details",
                    "block_type": "CODE",
                    "value": _structured_record_payload(),
                }
            ],
        },
    }


def _structured_record_qa_top_level_output_run_result() -> dict[str, Any]:
    result = _run_result(
        [
            _code_block(
                "extract_record_status_record",
                {"extracted_information": []},
            )
        ]
    )
    result["data"]["output"] = {
        "open_search_search_output": {
            "page_state": "search_search_open",
            "evidence_text": "Opened search search page with search-by-doctor typeahead #searchInput.",
        },
        "search_and_open_record_details_output": {
            "found": True,
            "entity_name": "Jordan Example",
            "opened_record_details": True,
            "evidence_text": "Opened Details page for the selected record.",
        },
        "extract_record_status_record_output": _structured_record_payload(found=True, entity_found=None),
        "extracted_information": [],
    }
    return result


def _partial_goal_field_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "expand_result_rows",
                {
                    "search_completed": True,
                    "certification_records": [
                        {
                            "name": "DOE, JANE",
                            "number": "12345",
                            "expiration_date": None,
                            "evidence_text": "DOE, JANE - credential 12345",
                        }
                    ],
                },
            )
        ]
    )


def _top_level_goal_field_success_run_result() -> dict[str, Any]:
    return _run_result(
        [
            _code_block(
                "expand_result_rows",
                [
                    {
                        "name": "DOE, JANE",
                        "number": "12345",
                        "expiration_date": "2027-01-31",
                    }
                ],
            )
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


def _terminal_metadata_with_goal_fields(label: str = "expand_result_rows") -> dict[str, Any]:
    entry = _terminal_metadata_entry(label)
    goal_value_paths = ["certification_records[].number", "certification_records[].expiration_date"]
    entry["claimed_outcomes"][0]["goal_value_paths"] = goal_value_paths
    entry["terminal_verifier_expectations"] = [
        {
            "id": "expectation:goal",
            "text": "Terminal verification observes requested registry fields.",
            "criteria_ids": ["criterion:goal_0"],
            "goal_value_paths": goal_value_paths,
        }
    ]
    return entry


def _terminal_metadata_with_top_level_goal_fields(label: str = "expand_result_rows") -> dict[str, Any]:
    entry = _terminal_metadata_entry(label)
    goal_value_paths = ["$[*].number", "$[0].expiration_date"]
    entry["claimed_outcomes"][0]["goal_value_paths"] = goal_value_paths
    return entry


def _terminal_metadata_with_boolean_goal_fields(label: str = "inspect_access_path") -> dict[str, Any]:
    entry = _terminal_metadata_entry(label)
    goal_value_paths = ["public_form_exists", "login_only"]
    entry["claimed_outcomes"][0]["goal_value_paths"] = goal_value_paths
    entry["terminal_verifier_expectations"] = [
        {
            "id": "expectation:goal",
            "text": "Terminal verification observes the access classification flags.",
            "criteria_ids": ["criterion:goal_0"],
            "goal_value_paths": goal_value_paths,
        }
    ]
    return entry


def _terminal_metadata_with_path_summary_goal_fields(label: str = "inspect_access_path") -> dict[str, Any]:
    entry = _terminal_metadata_entry(label)
    goal_value_paths = [
        "public_form_exists",
        "login_only",
        "visible_page_path_label",
        "recommended_next_action",
    ]
    entry["claimed_outcomes"][0]["goal_value_paths"] = goal_value_paths
    entry["terminal_verifier_expectations"] = [
        {
            "id": "expectation:goal",
            "text": "Terminal verification observes access classification and next action.",
            "criteria_ids": ["criterion:goal_0"],
            "goal_value_paths": goal_value_paths,
        }
    ]
    return entry


def test_blocked_flag_run_reports_structured_blocker() -> None:
    blocker = _run_blocks_structured_blocker_message(_blocked_flag_run_result())
    assert blocker is not None
    assert "human verification challenge" in blocker


def test_blocked_flag_run_records_challenge_observation_without_halting() -> None:
    result = _blocked_flag_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert result["ok"] is False
    assert result["error"] == ctx.last_test_failure_reason
    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is False
    assert "reported a blocker" in (ctx.last_test_failure_reason or "")
    assert ctx.last_failed_workflow_yaml == "blocks: []"
    categories = result["data"]["failure_categories"]
    assert any(category["category"] == "ANTI_BOT_DETECTION" for category in categories)
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "blocker_reported"
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
    assert ctx.last_test_suspicious_success is False


@pytest.mark.parametrize("block_type", ["CODE", "code"])
def test_blocked_status_value_reports_a_blocker_without_halting(block_type: str) -> None:
    result = _blocked_status_run_result(block_type)
    ctx = _ctx(result["data"]["blocks"])

    assert _run_blocks_structured_blocker_message(result) == "The run output reported status 'blocked_by_challenge'."

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is False


def test_status_blocked_with_challenge_evidence_never_records_a_tested_success() -> None:
    result = _run_result(
        [
            _code_block(
                "search_registry_person",
                {"status": "blocked", "records": [{"name": "DOE, JANE"}], "record_count": 1},
            )
        ]
    )
    ctx = _ctx(result["data"]["blocks"])
    ctx.composition_page_evidence = _gating_challenge_evidence()

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert result["ok"] is False
    assert _is_outcome_evidence_candidate(ctx, result) is False
    assert ctx.last_test_ok is False
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "blocker_reported"
    assert ctx.last_failure_category_top == "ANTI_BOT_DETECTION"


def test_status_blocked_with_challenge_evidence_terminalizes_at_the_settle_seam() -> None:
    result = _run_result(
        [
            _code_block(
                "search_registry_person",
                {"status": "blocked", "records": [{"name": "DOE, JANE"}], "record_count": 1},
            )
        ]
    )
    ctx = _ctx(result["data"]["blocks"])
    ctx.composition_page_evidence = _gating_challenge_evidence()
    ctx.last_test_ok = True

    assert run_execution.settle_terminal_challenge_after_enrichment(ctx, result) is True
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "blocker_reported"


def test_flag_key_with_non_empty_data_still_withholds_the_tested_latch() -> None:
    result = _run_result(
        [
            _code_block(
                "search_registry_person",
                {"anti_bot_blocked": True, "records": [{"name": "DOE, JANE", "status": "Active"}]},
            )
        ]
    )
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert ctx.last_test_ok is True
    assert ctx.last_full_workflow_test_ok is False
    snapshot = getattr(ctx, "outcome_verification_trace_snapshot", {})
    assert snapshot.get("run_output_latch_blocker_detected") is True
    assert snapshot.get("run_output_blocker_detected") is False


def test_challenge_shaped_block_label_does_not_withhold_the_tested_latch() -> None:
    result = _run_result([_code_block("solve_and_submit_recaptcha_demo", {"submitted": True})])
    result["data"]["observed_block_end_urls"] = {
        "solve_and_submit_recaptcha_demo": "https://registry.example.com/demo/result"
    }
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    snapshot = ctx.outcome_verification_trace_snapshot
    assert snapshot.get("run_output_latch_blocker_detected") is False
    assert snapshot.get("run_output_blocker_detected") is False
    assert result["data"]["observed_block_end_urls"] == {
        "solve_and_submit_recaptcha_demo": "https://registry.example.com/demo/result"
    }


def test_challenge_observation_prevents_false_satisfied_completion_without_halting() -> None:
    result = _blocked_flag_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.completion_criteria_turn_state = SimpleNamespace(
        adjudication_all_no_evidence_events=[],
        fully_satisfied_workflow_yaml=None,
        last_verdict_state_counts={},
    )

    _record_run_blocks_result(ctx, result, completion_verification=_satisfied("c0"))

    assert result["ok"] is False
    assert ctx.blocker_signal is None
    assert ctx.turn_halt is None
    assert ctx.last_test_suspicious_success is False
    assert verified_goal_satisfied_context(ctx) is False
    assert ctx.completion_criteria_turn_state.fully_satisfied_workflow_yaml is None


def test_domain_blocker_run_is_recorded_without_becoming_terminal_ready() -> None:
    result = _domain_blocker_run_result()
    ctx = _ctx(result["data"]["blocks"])

    assert _run_blocks_structured_blocker_message(result) is None
    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(ctx, result, completion_verification=_no_evidence("c0"))

    assert result["ok"] is True
    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None
    assert result["data"]["blocks"][0]["extracted_data"]["blocked_by"] == "online_account_required"
    assert verified_goal_satisfied_context(ctx) is False
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.verified_terminal_proposal_ready is False
    assert ctx.last_good_workflow is None

    recorded_output = build_test_evidence_packet(ctx, result).registered_outputs[0].output
    assert recorded_output["blocked_by"] == "online_account_required"
    assert recorded_output["safety_flags"]["no_submission_attempted"] is True


def test_genuine_success_run_still_latches_terminal_ready() -> None:
    result = _genuine_success_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=_no_evidence("c0"))

    assert ctx.last_full_workflow_test_ok is True
    assert ctx.verified_terminal_proposal_ready is True
    assert ctx.last_good_workflow is not None


@pytest.mark.parametrize(
    "completion_verification",
    [
        None,
        CompletionVerificationResult(status="unavailable"),
        CompletionVerificationResult(status="evaluated", criterion_ids=[]),
        _no_evidence("c0"),
    ],
)
def test_online_account_required_blocker_never_uses_interactive_completion_verification(
    completion_verification: CompletionVerificationResult | None,
) -> None:
    result = _domain_blocker_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=completion_verification)

    blocker_payload = result["data"]["blocks"][0]["extracted_data"]
    assert blocker_payload["blocked_by"] == "online_account_required"
    assert blocker_payload["safety_flags"]["no_submission_attempted"] is True
    assert result["ok"] is True
    assert ctx.last_test_ok is True
    assert verified_goal_satisfied_context(ctx) is False


def test_satisfied_interactive_completion_does_not_override_domain_blocker_fact() -> None:
    result = _domain_blocker_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=_satisfied("c0"))

    assert result["ok"] is True
    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None
    assert verified_goal_satisfied_context(ctx) is False


def test_genuine_success_run_keeps_clean_path() -> None:
    result = _genuine_success_run_result()
    ctx = _ctx(result["data"]["blocks"])

    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _, _ = _analyze_run_blocks(result)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True


def test_nested_code_output_record_is_meaningful_and_not_suspicious_when_verified() -> None:
    result = _nested_code_output_extraction_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(
        ctx,
        result,
        completion_verification=_satisfied("fallback_record_identity", "fallback_record_identifier"),
    )

    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True
    assert verified_goal_satisfied_context(ctx) is False


def test_registered_code_output_parameter_record_is_meaningful() -> None:
    result = _registered_code_output_parameter_run_result()
    ctx = _ctx([])

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)

    assert empty_data_blocks is False


def test_registered_code_output_parameter_record_is_current_run_scoped() -> None:
    result = _empty_extraction_run_result()
    result["data"]["registered_output_parameter_values"] = _registered_code_output_parameter_run_result(
        workflow_run_id="wr_prior"
    )["data"]["registered_output_parameter_values"]
    ctx = _ctx(result["data"]["blocks"])

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)

    assert empty_data_blocks is True


def test_structured_record_top_level_output_record_is_meaningful() -> None:
    result = _structured_record_qa_top_level_output_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)

    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


@pytest.mark.asyncio
async def test_registered_output_adapter_fetches_db_values_and_synthesizes_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_workflow_run_output_parameters(*, workflow_run_id: str) -> list[SimpleNamespace]:
        assert workflow_run_id == "wr_test"
        return [
            SimpleNamespace(
                workflow_run_id="wr_test",
                output_parameter_id="op_details",
                value={
                    "entity_name": "Jordan Example",
                    "record_number": "1234567890",
                    "evidence_text": "Opened Details page",
                },
            )
        ]

    monkeypatch.setattr(
        run_execution.app.DATABASE,
        "workflow_runs",
        SimpleNamespace(get_workflow_run_output_parameters=fake_get_workflow_run_output_parameters),
    )
    workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[
                SimpleNamespace(
                    label="extract_record_status_details",
                    block_type="CODE",
                    output_parameter=SimpleNamespace(
                        output_parameter_id="op_details",
                        key="extract_record_status_details_output",
                    ),
                )
            ]
        )
    )
    data: dict[str, Any] = {"workflow_run_id": "wr_test", "blocks": []}

    by_label = await _attach_registered_output_parameter_values(
        workflow_run_id="wr_test",
        workflow=workflow,  # type: ignore[arg-type]
        data=data,
    )

    assert by_label == {
        "extract_record_status_details": {
            "extract_record_status_details_output": {
                "entity_name": "Jordan Example",
                "record_number": "1234567890",
                "evidence_text": "Opened Details page",
            }
        }
    }
    assert data["registered_output_parameter_values"][0]["value"]["record_number"] == "1234567890"
    assert data["blocks"][0]["label"] == "extract_record_status_details"
    assert data["blocks"][0]["extracted_data"]["extract_record_status_details_output"]["record_number"] == "1234567890"


@pytest.mark.asyncio
async def test_registered_validation_review_output_from_dict_workflow_merges_into_block_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_payload = _validation_review_payload()

    async def fake_get_workflow_run_output_parameters(*, workflow_run_id: str) -> list[SimpleNamespace]:
        assert workflow_run_id == "wr_test"
        return [
            SimpleNamespace(
                workflow_run_id="wr_test",
                output_parameter_id="op_review",
                value=review_payload,
            )
        ]

    monkeypatch.setattr(
        run_execution.app.DATABASE,
        "workflow_runs",
        SimpleNamespace(get_workflow_run_output_parameters=fake_get_workflow_run_output_parameters),
    )
    workflow = SimpleNamespace(
        workflow_definition={
            "blocks": [
                {
                    "label": "validate_business_start_service_review",
                    "block_type": "CODE",
                    "output_parameter": {
                        "output_parameter_id": "op_review",
                        "key": "validate_business_start_service_review_output",
                    },
                }
            ]
        }
    )
    data: dict[str, Any] = {"workflow_run_id": "wr_test", "blocks": []}

    by_label = await _attach_registered_output_parameter_values(
        workflow_run_id="wr_test",
        workflow=workflow,  # type: ignore[arg-type]
        data=data,
    )
    ctx = _ctx([{"label": "validate_business_start_service_review"}])
    snapshot = _build_run_evidence_snapshot(ctx, {"ok": True, "data": data})
    verdicts = grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot)

    assert by_label == {
        "validate_business_start_service_review": {"validate_business_start_service_review_output": review_payload}
    }
    assert data["registered_output_parameter_values"][0]["block_label"] == "validate_business_start_service_review"
    assert data["blocks"][0]["extracted_data"]["validate_business_start_service_review_output"] == review_payload
    assert verdicts[0].evidence_ref == "block_outputs:validate_business_start_service_review"


@pytest.mark.asyncio
async def test_registered_output_adapter_retains_rows_without_workflow_definition_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_workflow_run_output_parameters(*, workflow_run_id: str) -> list[SimpleNamespace]:
        assert workflow_run_id == "wr_dispatch"
        return [
            SimpleNamespace(
                workflow_run_id="wr_dispatch",
                output_parameter_id="op_unknown",
                value={"record_number": "1234567890"},
            ),
        ]

    monkeypatch.setattr(
        run_execution.app.DATABASE,
        "workflow_runs",
        SimpleNamespace(get_workflow_run_output_parameters=fake_get_workflow_run_output_parameters),
    )
    source_workflow = SimpleNamespace(
        organization_id="o",
        workflow_definition=SimpleNamespace(
            blocks=[
                SimpleNamespace(
                    label="extract_record",
                    block_type="CODE",
                    output_parameter=SimpleNamespace(
                        output_parameter_id="op_source",
                        key="extract_record_output",
                    ),
                )
            ]
        ),
    )
    data: dict[str, Any] = {"workflow_run_id": "wr_dispatch", "blocks": []}

    by_label = await _attach_registered_output_parameter_values(
        workflow_run_id="wr_dispatch",
        workflow=source_workflow,  # type: ignore[arg-type]
        data=data,
    )

    assert by_label == {}
    assert data["registered_output_parameter_values"] == [
        {
            "workflow_run_id": "wr_dispatch",
            "output_parameter_id": "op_unknown",
            "output_parameter_key": None,
            "block_label": None,
            "block_type": None,
            "value": {"record_number": "1234567890"},
        }
    ]
    assert data["blocks"] == []


def test_clean_empty_output_run_does_not_latch_a_tested_receipt() -> None:
    result = _empty_extraction_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is True

    _record_run_blocks_result(ctx, result, completion_verification=_satisfied("c0"))

    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None
    assert ctx.last_full_workflow_test_ok is False
    assert verified_goal_satisfied_context(ctx) is False
    assert ctx.latest_recorded_build_test_outcome is not None
    assert ctx.latest_recorded_build_test_outcome.verdict == "not_authoritative"
    assert ctx.latest_recorded_build_test_outcome.reason_code == "run_completed_unevaluated"
    assert ctx.latest_recorded_build_test_outcome.structural_key is None


def test_challenge_observation_does_not_leave_authoritative_prompt_outcome() -> None:
    result = _blocked_flag_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert ctx.blocker_signal is None
    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None
    assert outcome.reason_code == "blocker_reported"
    assert outcome.is_authoritative is False


def test_all_null_metadata_goal_fields_are_observations_not_empty_run_grades() -> None:
    result = _all_null_goal_fields_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_goal_fields()}

    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    # The nulls remain in the run facts for the acting model; they do not grade
    # whether the platform run itself completed cleanly.
    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True
    assert ctx.last_good_workflow is ctx.last_workflow


def test_metadata_goal_fields_with_values_keep_clean_path() -> None:
    result = _goal_field_success_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_goal_fields()}

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True


def test_metadata_boolean_goal_paths_count_as_present_content() -> None:
    result = _boolean_goal_path_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"inspect_access_path": _terminal_metadata_with_boolean_goal_fields()}

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


def test_path_summary_goal_paths_with_boolean_flags_keep_clean_path() -> None:
    result = _domain_path_summary_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"inspect_access_path": _terminal_metadata_with_path_summary_goal_fields()}

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


def test_goal_path_alias_without_exact_declared_path_does_not_grade_the_run() -> None:
    result = _domain_path_alias_summary_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"inspect_access_path": _terminal_metadata_with_path_summary_goal_fields()}

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


def test_null_goal_path_value_does_not_fall_back_to_alias_field() -> None:
    result = _domain_path_alias_summary_run_result(include_null_alias_source=True)
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"inspect_access_path": _terminal_metadata_with_path_summary_goal_fields()}

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


def test_partial_metadata_goal_fields_do_not_grade_the_run() -> None:
    result = _partial_goal_field_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_goal_fields()}

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


def test_undeclared_boolean_flags_are_not_goal_content() -> None:
    assert _code_output_has_goal_content({"public_form_exists": False, "login_only": True}) is False


def test_top_level_array_goal_value_paths_keep_clean_path() -> None:
    result = _top_level_goal_field_success_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_top_level_goal_fields()}

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


def test_downloaded_files_satisfy_registered_download_goal_paths() -> None:
    result = _run_result(
        [
            _code_block(
                "download_statement",
                {
                    "downloaded_file_name": "statement.pdf",
                    "downloaded_files": [{"filename": "statement.pdf"}],
                },
            )
        ]
    )
    ctx = _ctx(result["data"]["blocks"])
    entry = _terminal_metadata_entry("download_statement")
    entry["claimed_outcomes"][0]["goal_value_paths"] = ["invoice_pdf"]
    entry["terminal_verifier_expectations"] = [
        {
            "id": "expectation:download_statement_terminal",
            "criteria_ids": ["criterion:goal_0"],
            "goal_value_paths": ["invoice_pdf"],
        }
    ]
    ctx.code_artifact_metadata = {"download_statement": entry}

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True


def test_array_goal_value_path_does_not_match_scalar_root() -> None:
    result = _run_result([_code_block("expand_result_rows", {"number": "12345"})])
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_top_level_goal_fields()}

    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


def test_candidacy_and_recording_agree_on_all_null_metadata_goal_fields() -> None:
    result = _all_null_goal_fields_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_goal_fields()}

    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False


def test_empty_goal_collections_without_blocker_are_flagged() -> None:
    result = _run_result([_code_block("search_registry_person", {"records": [], "result_count": 0})])
    ctx = _ctx(result["data"]["blocks"])

    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _, _ = _analyze_run_blocks(result)
    assert empty_data_blocks is True
    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is False


def test_neutral_status_string_does_not_redeem_empty_collections() -> None:
    result = _run_result([_code_block("search_registry_person", {"status": "completed", "records": []})])
    _, empty_data_blocks, _, _ = _analyze_run_blocks(result)
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
    _, empty_data_blocks, _, _ = _analyze_run_blocks(result)
    assert empty_data_blocks is False


def test_verification_key_counts_as_goal_content_for_code_outputs() -> None:
    # SKY-10916: an output key carrying ``verification`` is data, not a blocker —
    # it must satisfy the emptiness denominator instead of being stripped.
    result = _run_result(
        [_code_block("verify_listing", {"verification_results": [{"name": "DOE, JANE", "verified": True}]})]
    )
    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _, _ = _analyze_run_blocks(result)
    assert empty_data_blocks is False


def test_anti_bot_value_under_broad_key_trips_code_block_blocker() -> None:
    # SKY-10916: a broad/descriptive key carrying a real anti-bot phrase is caught
    # for code outputs even though the key stays out of the strict term set.
    result = _run_result([_code_block("check_access", {"verification": "verify you are human to continue"})])
    assert _run_blocks_structured_blocker_message(result) == "verify you are human to continue"

    status_detail = _run_result([_code_block("check_access", {"status_detail": "human verification required"})])
    assert _run_blocks_structured_blocker_message(status_detail) == "human verification required"


def test_descriptive_verification_key_with_benign_value_stays_exempt() -> None:
    benign = _run_result([_code_block("plan_step", {"verification_method": "fill the login form"})])
    assert _run_blocks_structured_blocker_message(benign) is None

    boolean_flag = _run_result([_code_block("notify", {"verification_passed": True})])
    assert _run_blocks_structured_blocker_message(boolean_flag) is None


def test_anti_bot_value_scan_gated_on_phrases_not_bare_tokens() -> None:
    # Extracted business text mentioning ``verification``/``challenge`` must not
    # false-positive under the phrase-set gate.
    result = _run_result(
        [_code_block("summary", {"notes": "we need verification of the challenge results before May"})]
    )
    assert _run_blocks_structured_blocker_message(result) is None


class _MetadataCtx:
    def __init__(self, metadata: dict) -> None:
        self.code_artifact_metadata = metadata


def test_declared_outcome_keys_count_as_goal_content() -> None:
    metadata = {
        "check_challenge": {
            "claimed_outcomes": [{"id": "captcha_audit_log", "entities": ["challenge_summary"], "required_tokens": []}]
        }
    }
    block = _code_block("check_challenge", {"captcha_audit_log": "3 challenges recorded this month"})
    result = _run_result([block])
    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _, _ = _analyze_run_blocks(result, _MetadataCtx(metadata))
    assert empty_data_blocks is False


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


def test_interactive_completion_verdict_is_inert_for_nonempty_completed_run() -> None:
    result = _genuine_success_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"search_registry_person": _terminal_metadata_entry()}

    _record_run_blocks_result(ctx, result, completion_verification=_no_evidence("c0"))

    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True


def test_interactive_completion_verdict_is_inert_without_metadata() -> None:
    result = _genuine_success_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=_no_evidence("c0"))

    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True
    assert getattr(ctx, "last_good_workflow", None) is not None
    assert "failure_reason" not in result["data"]


def test_interactive_completion_verdict_cannot_promote_unfinished_run() -> None:
    result = _run_result([_code_block("submit_request", {"confirmation_number": "WTR-1842-DEMO"})], ok=False)
    ctx = _ctx(result["data"]["blocks"])

    assert _is_unfinished_run_verification_candidate(ctx, result) is True

    recorded = _record_run_blocks_result(ctx, result, completion_verification=_satisfied("c0"))

    assert outcome_fully_verified(ctx) is False
    assert recorded is not None
    assert recorded.verdict == "not_demonstrated"
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_test_suspicious_success is False


def test_unfinished_run_with_unsatisfied_verifier_stays_unrecognized() -> None:
    result = _run_result([_code_block("submit_request", {"confirmation_number": "WTR-1842-DEMO"})], ok=False)
    ctx = _ctx(result["data"]["blocks"])

    recorded = _record_run_blocks_result(ctx, result, completion_verification=_no_evidence("c0"))

    assert outcome_fully_verified(ctx) is False
    assert ctx.last_full_workflow_test_ok is False
    assert recorded is None or recorded.verdict != "demonstrated"
    assert getattr(ctx, "last_good_workflow", None) is None


def test_present_value_upgrade_flips_lone_judge_unknown_to_demonstrated_on_ok_run() -> None:
    result = _run_result([_code_block("submit_request", {"confirmation_number": "WTR-1842-DEMO"})], ok=True)
    ctx = _ctx(result["data"]["blocks"])
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c0", outcome="the submitted request returns confirmation number WTR-1842-DEMO")
        ]
    )

    run_criteria = ctx.request_policy.completion_criteria
    snapshot = _build_run_evidence_snapshot(ctx, result)
    judge_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="unknown", reason_code="unknown")],
    )
    upgraded = _apply_present_value_upgrades(judge_result, run_criteria, snapshot)
    assert upgraded.is_fully_satisfied() is True

    recorded = _record_run_blocks_result(ctx, result, completion_verification=upgraded)

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert outcome_fully_verified(ctx) is False
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True


def _failed_code_block_run_result(error_code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_test",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "run_code",
                    "block_type": "CODE",
                    "status": "failed",
                    "failure_reason": "Secure CodeBlock runner is unavailable. Please retry.",
                    "error_codes": [error_code],
                }
            ],
        },
    }


@pytest.mark.parametrize("error_code", sorted(run_execution.INFRASTRUCTURE_RUNNER_ERROR_CODES))
def test_infrastructure_runner_code_prepends_unrecoverable_tool_error(error_code: str) -> None:
    result = _failed_code_block_run_result(error_code)
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    categories = result["data"]["failure_categories"]
    assert categories[0]["category"] == "UNRECOVERABLE_TOOL_ERROR"
    assert ctx.last_failure_category_top == "UNRECOVERABLE_TOOL_ERROR"
    assert ctx.last_infrastructure_tool_error == error_code


@pytest.mark.parametrize(
    "error_code",
    ["timeout", "user_code_error", "insecure_code_detected", "memory_limit_exceeded"],
)
def test_repairable_runner_code_injects_no_unrecoverable_category(error_code: str) -> None:
    result = _failed_code_block_run_result(error_code)
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    categories = result["data"].get("failure_categories") or []
    assert all(entry.get("category") != "UNRECOVERABLE_TOOL_ERROR" for entry in categories)
    assert ctx.last_failure_category_top != "UNRECOVERABLE_TOOL_ERROR"
    assert ctx.last_infrastructure_tool_error is None


def test_input_reassembly_memory_limit_stays_available_for_copilot_repair() -> None:
    error_code = "parameter_reassembly_memory_limit_exceeded"
    reason = "CodeBlock inputs exhausted the sandbox memory limit before the block started."
    result = _failed_code_block_run_result(error_code)
    result["data"]["blocks"][0]["failure_reason"] = reason
    result["data"]["failure_categories"] = [
        {
            "category": "INFRASTRUCTURE_ERROR",
            "confidence_float": 0.95,
            "reason_code": "secure_codeblock_input_memory_limit",
            "reasoning": "Secure CodeBlock sandbox ran out of memory before executing the block",
        }
    ]
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert result["data"]["failure_categories"] == [
        {
            "category": "INFRASTRUCTURE_ERROR",
            "confidence_float": 0.95,
            "reason_code": "secure_codeblock_input_memory_limit",
            "reasoning": "Secure CodeBlock sandbox ran out of memory before executing the block",
        }
    ]
    assert ctx.last_infrastructure_tool_error is None
    assert ctx.last_test_failure_reason == reason


def test_unstructured_failure_prose_leaves_category_consumers_clear() -> None:
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_test",
            "overall_status": "failed",
            "blocks": [
                {
                    "label": "submit_form",
                    "block_type": "CODE",
                    "status": "failed",
                    "failure_reason": "Element not found after waiting for submit",
                    "error_codes": ["user_code_error"],
                }
            ],
        },
    }
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert result["data"].get("failure_categories") is None
    assert ctx.last_failure_category_top is None
    assert ctx.last_test_anti_bot is None


def _projected_run_result(block_label: str, end_url: str) -> dict[str, Any]:
    """A completed run carrying an uncleared challenge flag, whose block-fact projection is
    written by the real producer so the only thing an arm varies is the label."""
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_projection",
            "overall_status": "completed",
            "current_url": end_url,
            "page_title": "Demo submitted",
            "challenge_detected": True,
            "blocks": [_code_block(block_label, {"submitted": True})],
        },
    }
    row = WorkflowRunBlock(
        workflow_run_block_id="wrb_projection",
        workflow_run_id="wr_projection",
        organization_id="org",
        block_type=BlockType.CODE,
        label=block_label,
        status="completed",
        final_url=end_url,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _attach_block_fact_projection(
        result["data"], [row], {}, unreported_predecessor_labels=[], sensitive_origin_run=False
    )
    return result


def _settled(result: dict[str, Any]) -> tuple[bool, str | None, dict[str, str] | None]:
    ctx = _ctx(result["data"].get("blocks") or [])
    ctx.last_test_ok = True
    terminalized = run_execution.settle_terminal_challenge_after_enrichment(ctx, result)
    verdict = ctx.last_run_outcome.verdict if ctx.last_run_outcome is not None else None
    return terminalized, verdict, result["data"].get("observed_block_end_urls")


def test_block_label_rename_does_not_change_the_terminal_verdict() -> None:
    end_url = "https://registry.example.com/demo/result"
    neutral = _projected_run_result("submit_demo", end_url)
    challenge_shaped = _projected_run_result("solve_and_submit_recaptcha_demo", end_url)
    nested = _projected_run_result("verification_url", end_url)

    neutral_settled = _settled(neutral)
    challenge_settled = _settled(challenge_shaped)
    nested_settled = _settled(nested)

    assert neutral_settled[:2] == challenge_settled[:2] == nested_settled[:2] == (False, None)
    assert neutral_settled[2] == {"submit_demo": end_url}
    assert challenge_settled[2] == {"solve_and_submit_recaptcha_demo": end_url}
    assert nested_settled[2] == {"verification_url": end_url}
    assert neutral["data"]["current_url"] == challenge_shaped["data"]["current_url"] == end_url


@pytest.mark.parametrize(
    "envelope_fact",
    [
        {"observed_block_end_urls": {"solve_recaptcha_demo": "https://registry.example.com/ok"}},
        {"build_test_packet": {"observed_block_end_urls": {"solve_recaptcha_demo": "https://registry.example.com/ok"}}},
        {"execution_source": {"block_code_sha256": {"human_verification_step": "9f2c"}}},
        # Page-fact strings were never reachable by the key scan; these two rows pin drift.
        {"current_url": "https://registry.example.com/human-verification/complete"},
        {"page_title": "Human verification complete"},
        {"registered_output_parameter_values": [{"value": {"captcha_demo_result": "https://registry.example.com/ok"}}]},
        {"observed_block_end_urls": {"captcha_message": "https://registry.example.com/captcha"}},
        {"build_test_packet": {"observed_block_end_urls": {"captcha_message": "https://x.example.com/captcha"}}},
        {"per_block_action_observations": {"solve_captcha": ["saw https://x.example.com/captcha"]}},
    ],
)
def test_envelope_key_and_value_names_never_mint_a_blocker(envelope_fact: dict[str, Any]) -> None:
    result = _run_result([_code_block("submit_demo", {"submitted": True})])
    result["data"].update(envelope_fact)

    assert _run_blocks_structured_blocker_message(result) is None


def test_projection_writer_emits_exactly_the_keys_the_envelope_scan_skips() -> None:
    """A new block-fact projection field must not silently rejoin the challenge verdict scan."""
    data: dict[str, Any] = {}
    row = WorkflowRunBlock(
        workflow_run_block_id="wrb_drift",
        workflow_run_id="wr_drift",
        organization_id="org",
        block_type=BlockType.CODE,
        label="submit_demo",
        status="completed",
        final_url="https://registry.example.com/ok",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _attach_block_fact_projection(
        data,
        [row],
        {"submit_demo": ["clicked submit"]},
        unreported_predecessor_labels=["earlier_block"],
        sensitive_origin_run=False,
    )

    assert set(data) == _PROJECTED_BLOCK_FACT_KEYS


def test_envelope_failure_reason_still_reports_a_real_anti_bot_blocker() -> None:
    result = _run_result([_code_block("submit_demo", {"submitted": True})])
    result["data"]["failure_reason"] = "Access denied: verify you are human before continuing."

    assert _run_blocks_structured_blocker_message(result) == "Access denied: verify you are human before continuing."


@pytest.mark.parametrize(
    "block",
    [
        _code_block("report_demo", {"captcha_demo_url": "https://registry.example.com/ok"}, block_type="TEXT_PROMPT"),
        _code_block(
            "read_result",
            {"extracted_information": {"verification_ended_url": "https://registry.example.com/ok"}},
            block_type="EXTRACTION",
        ),
        _code_block("report_demo", {"challenge_page_url": "https://registry.example.com/ok"}, block_type="TEXT_PROMPT"),
        _code_block(
            "read_result",
            {"extracted_information": {"captcha_message": "https://registry.example.com/ok"}},
            block_type="EXTRACTION",
        ),
        _code_block(
            "read_result",
            {"extracted_information": {"result_message": "https://registry.example.com/ok"}},
            block_type="EXTRACTION",
        ),
    ],
)
def test_data_block_schema_key_names_never_mint_a_blocker(block: dict[str, Any]) -> None:
    assert _run_blocks_structured_blocker_message(_run_result([block])) is None


@pytest.mark.parametrize(
    "extracted_data",
    [
        {"captcha_page_url": "https://registry.example.com/ok"},
        {"no_captcha_encountered": True, "submitted": True},
        {"blocked_by_challenge": False, "submitted": True},
        {"blocked_by": "online_account_required", "login_only": True},
        {"human_verification_step": "solve the puzzle"},
        {"captcha_message": "https://registry.example.com/ok"},
        {"result_message": "https://registry.example.com/ok"},
    ],
)
def test_code_block_key_names_and_flags_never_mint_a_blocker(extracted_data: dict[str, Any]) -> None:
    assert _run_blocks_structured_blocker_message(_run_result([_code_block("submit_demo", extracted_data)])) is None


@pytest.mark.parametrize(
    ("extracted_data", "expected"),
    [
        ({"status": "challenge cleared", "records": [{"name": "DOE, JANE"}]}, "challenge cleared"),
        ({"state": "captcha_pending"}, "captcha_pending"),
    ],
)
def test_code_block_status_value_reports_a_blocker(extracted_data: dict[str, Any], expected: str) -> None:
    blocker = _run_blocks_structured_blocker_message(_run_result([_code_block("submit_demo", extracted_data)]))
    assert blocker == f"The run output reported status '{expected}'."


@pytest.mark.parametrize(
    ("extracted_data", "expected"),
    [
        (
            {"notes": "The search form is gated by a human verification challenge."},
            "The search form is gated by a human verification challenge.",
        ),
        (
            {"reason": "Access denied: verify you are human before continuing."},
            "Access denied: verify you are human before continuing.",
        ),
    ],
)
def test_code_block_values_still_report_a_real_blocker(extracted_data: dict[str, Any], expected: str) -> None:
    assert _run_blocks_structured_blocker_message(_run_result([_code_block("submit_demo", extracted_data)])) == expected


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            _code_block(
                "report_demo",
                {"message": "Access denied: verify you are human before continuing."},
                block_type="TEXT_PROMPT",
            ),
            "Access denied: verify you are human before continuing.",
        ),
        (
            _code_block(
                "read_result",
                {"extracted_information": {"captcha_message": "hCaptcha challenge is still on screen."}},
                block_type="EXTRACTION",
            ),
            "hCaptcha challenge is still on screen.",
        ),
    ],
)
def test_data_block_values_still_report_a_real_blocker(block: dict[str, Any], expected: str) -> None:
    assert _run_blocks_structured_blocker_message(_run_result([block])) == expected


@pytest.mark.parametrize(
    "block",
    [
        _code_block(
            "read_result",
            {"extracted_information": {"title": "Why do I see Access Denied on the portal?"}},
            block_type="EXTRACTION",
        ),
        _code_block(
            "read_result",
            {"extracted_information": {"shipment": "Requested port of loading: Rotterdam"}},
            block_type="EXTRACTION",
        ),
        _code_block(
            "report_demo",
            {"summary": "The requested port of discharge is Hamburg."},
            block_type="TEXT_PROMPT",
        ),
    ],
)
def test_scraped_page_prose_never_mints_a_blocker(block: dict[str, Any]) -> None:
    assert _run_blocks_structured_blocker_message(_run_result([block])) is None


def test_registered_output_key_name_does_not_terminalize_a_completed_run() -> None:
    end_url = "https://registry.example.com/demo/result"
    result = _projected_run_result("submit_demo", end_url)
    result["data"]["registered_output_parameter_values"] = [{"value": {"captcha_demo_result": end_url}}]

    terminalized, verdict, observed = _settled(result)

    assert (terminalized, verdict) == (False, None)
    assert observed == {"submit_demo": end_url}
    assert result["data"]["registered_output_parameter_values"] == [{"value": {"captcha_demo_result": end_url}}]
