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
from skyvern.forge.sdk.copilot.tools._shared import _registered_output_parameter_payloads
from skyvern.forge.sdk.copilot.tools.blockers import _code_output_has_goal_content
from skyvern.forge.sdk.copilot.tools.run_execution import _attach_registered_output_parameter_values
from skyvern.forge.sdk.copilot.turn_halt import TurnHaltKind


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
    return _run_result(
        [
            _code_block(
                "inspect_access_path",
                {
                    "login_only": True,
                    "blocked_by": "online_account_required",
                    "public_form_exists": False,
                    "visible_page_path_label": "Account login page",
                    "recommended_next_action": "Ask the user for online account access before continuing.",
                    "safety_flags": {
                        "no_sensitive_data_entered": True,
                        "no_submission_attempted": True,
                    },
                },
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


def test_blocked_flag_run_records_terminal_challenge() -> None:
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
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_terminal_challenge_blocker"
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.reason_code == "terminal_challenge_blocker"
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
    assert ctx.blocker_signal.internal_reason_code == "tool_error_terminal_challenge_blocker"
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is False


def test_terminal_challenge_blocker_preempts_satisfied_completion() -> None:
    result = _blocked_flag_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.completion_criteria_turn_state = SimpleNamespace(
        adjudication_all_no_evidence_events=[],
        fully_satisfied_workflow_yaml=None,
        last_verdict_state_counts={},
    )

    _record_run_blocks_result(ctx, result, completion_verification=_satisfied("c0"))

    assert result["ok"] is False
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_terminal_challenge_blocker"
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    assert ctx.last_test_suspicious_success is False
    assert verified_goal_satisfied_context(ctx) is False
    assert ctx.completion_criteria_turn_state.fully_satisfied_workflow_yaml is None


def test_domain_blocker_run_waits_for_completion_verification_before_success() -> None:
    result = _domain_blocker_run_result()
    ctx = _ctx(result["data"]["blocks"])

    assert _run_blocks_structured_blocker_message(result) == "online_account_required"
    assert _is_outcome_evidence_candidate(ctx, result) is True

    _record_run_blocks_result(ctx, result, completion_verification=_no_evidence("c0"))

    assert result["ok"] is False
    assert ctx.last_test_ok is False
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False
    assert "online_account_required" in (ctx.last_test_failure_reason or "")
    assert verified_goal_satisfied_context(ctx) is False


@pytest.mark.parametrize(
    "completion_verification",
    [
        None,
        CompletionVerificationResult(status="unavailable"),
        CompletionVerificationResult(status="evaluated", criterion_ids=[]),
        _no_evidence("c0"),
    ],
)
def test_online_account_required_blocker_requires_satisfied_completion_verification(
    completion_verification: CompletionVerificationResult | None,
) -> None:
    result = _domain_blocker_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=completion_verification)

    blocker_payload = result["data"]["blocks"][0]["extracted_data"]
    assert blocker_payload["blocked_by"] == "online_account_required"
    assert blocker_payload["safety_flags"]["no_submission_attempted"] is True
    assert result["ok"] is False
    assert ctx.last_test_ok is False
    assert ctx.last_full_workflow_test_ok is False
    assert verified_goal_satisfied_context(ctx) is False
    assert getattr(ctx, "last_good_workflow", None) is None


def test_satisfied_completion_overrides_domain_blocker_wording() -> None:
    result = _domain_blocker_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _record_run_blocks_result(ctx, result, completion_verification=_satisfied("c0"))

    assert result["ok"] is True
    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None
    assert ctx.last_full_workflow_test_ok is True
    assert verified_goal_satisfied_context(ctx) is True


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


def test_nested_code_output_record_is_meaningful_and_not_suspicious_when_verified() -> None:
    result = _nested_code_output_extraction_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
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
    assert verified_goal_satisfied_context(ctx) is True


def test_registered_code_output_parameter_record_is_meaningful() -> None:
    result = _registered_code_output_parameter_run_result()
    ctx = _ctx([])

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)

    assert empty_data_blocks is False


def test_registered_code_output_parameter_record_is_current_run_scoped() -> None:
    result = _empty_extraction_run_result()
    result["data"]["registered_output_parameter_values"] = _registered_code_output_parameter_run_result(
        workflow_run_id="wr_prior"
    )["data"]["registered_output_parameter_values"]
    ctx = _ctx(result["data"]["blocks"])

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)

    assert empty_data_blocks is True


def test_structured_record_top_level_output_record_is_meaningful() -> None:
    result = _structured_record_qa_top_level_output_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)

    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


@pytest.mark.asyncio
async def test_registered_output_adapter_fetches_db_values_and_synthesizes_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.copilot.tools import run_execution

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


def test_satisfied_completion_prevents_empty_output_suspicious_success() -> None:
    result = _empty_extraction_run_result()
    ctx = _ctx(result["data"]["blocks"])

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is True

    _record_run_blocks_result(ctx, result, completion_verification=_satisfied("c0"))

    assert ctx.last_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None
    assert verified_goal_satisfied_context(ctx) is True


def test_all_null_metadata_goal_fields_are_flagged_as_no_goal_content() -> None:
    result = _all_null_goal_fields_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_goal_fields()}

    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is True
    assert _is_outcome_evidence_candidate(ctx, result) is False

    _record_run_blocks_result(ctx, result, completion_verification=None)

    assert ctx.last_test_ok is None
    assert ctx.last_test_suspicious_success is True
    assert ctx.null_data_streak_count == 1
    assert ctx.last_full_workflow_test_ok is False
    assert getattr(ctx, "last_good_workflow", None) is None


def test_metadata_goal_fields_with_values_keep_clean_path() -> None:
    result = _goal_field_success_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_goal_fields()}

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
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

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


def test_path_summary_goal_paths_with_boolean_flags_keep_clean_path() -> None:
    result = _domain_path_summary_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"inspect_access_path": _terminal_metadata_with_path_summary_goal_fields()}

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is False
    assert _is_outcome_evidence_candidate(ctx, result) is True


def test_goal_path_alias_without_exact_declared_path_is_flagged_as_no_goal_content() -> None:
    result = _domain_path_alias_summary_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"inspect_access_path": _terminal_metadata_with_path_summary_goal_fields()}

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is True
    assert _is_outcome_evidence_candidate(ctx, result) is False


def test_null_goal_path_value_does_not_fall_back_to_alias_field() -> None:
    result = _domain_path_alias_summary_run_result(include_null_alias_source=True)
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"inspect_access_path": _terminal_metadata_with_path_summary_goal_fields()}

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is True
    assert _is_outcome_evidence_candidate(ctx, result) is False


def test_partial_metadata_goal_fields_are_flagged_as_no_goal_content() -> None:
    result = _partial_goal_field_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_goal_fields()}

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is True
    assert _is_outcome_evidence_candidate(ctx, result) is False


def test_undeclared_boolean_flags_are_not_goal_content() -> None:
    assert _code_output_has_goal_content({"public_form_exists": False, "login_only": True}) is False


def test_top_level_array_goal_value_paths_keep_clean_path() -> None:
    result = _top_level_goal_field_success_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_top_level_goal_fields()}

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
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

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
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

    _, empty_data_blocks, _ = _analyze_run_blocks(result, ctx)
    assert empty_data_blocks is True
    assert _is_outcome_evidence_candidate(ctx, result) is False


def test_candidacy_and_recording_agree_on_all_null_metadata_goal_fields() -> None:
    result = _all_null_goal_fields_run_result()
    ctx = _ctx(result["data"]["blocks"])
    ctx.code_artifact_metadata = {"expand_result_rows": _terminal_metadata_with_goal_fields()}

    assert _is_outcome_evidence_candidate(ctx, result) is False

    _record_run_blocks_result(ctx, result, completion_verification=None)
    assert ctx.last_test_ok is None
    assert ctx.last_test_suspicious_success is True


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

    # SKY-10916: broad terms like ``verification`` no longer key the code-block
    # string arm; only the strict term set does.
    broad_term_string = _run_result([_code_block("notify", {"verification_code_sent": "yes"})])
    assert _run_blocks_structured_blocker_message(broad_term_string) is None

    strict_term_string = _run_result([_code_block("notify", {"human_verification_step": "solve the puzzle"})])
    assert _run_blocks_structured_blocker_message(strict_term_string) == "solve the puzzle"


def test_verification_key_counts_as_goal_content_for_code_outputs() -> None:
    # SKY-10916: an output key carrying ``verification`` is data, not a blocker —
    # it must satisfy the emptiness denominator instead of being stripped.
    result = _run_result(
        [_code_block("verify_listing", {"verification_results": [{"name": "DOE, JANE", "verified": True}]})]
    )
    assert _run_blocks_structured_blocker_message(result) is None
    _, empty_data_blocks, _ = _analyze_run_blocks(result)
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


def test_declared_outcome_keys_exempt_from_blocker_term_matching() -> None:
    # Metadata-declared goal keys (the #12034 typed source) override string
    # matching even for strict terms.
    metadata = {
        "check_challenge": {
            "claimed_outcomes": [{"id": "captcha_audit_log", "entities": ["challenge_summary"], "required_tokens": []}]
        }
    }
    block = _code_block("check_challenge", {"captcha_audit_log": "3 challenges recorded this month"})
    result = _run_result([block])
    assert _run_blocks_structured_blocker_message(result) is not None
    assert _run_blocks_structured_blocker_message(result, _MetadataCtx(metadata)) is None
    _, empty_data_blocks, _ = _analyze_run_blocks(result, _MetadataCtx(metadata))
    assert empty_data_blocks is False


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
    assert ctx.last_full_workflow_test_ok is False
    assert getattr(ctx, "last_good_workflow", None) is None
    assert "failure_reason" not in result["data"]


@pytest.mark.parametrize("metadata_shape", ["none", "terminal", "prefix_only"])
def test_judge_unmet_on_detector_clean_run_does_not_reset_streaks_or_promote(metadata_shape: str) -> None:
    result = _genuine_success_run_result()
    ctx = _ctx(result["data"]["blocks"])
    if metadata_shape == "terminal":
        ctx.code_artifact_metadata = {"search_registry_person": _terminal_metadata_entry()}
    elif metadata_shape == "prefix_only":
        entry = _terminal_metadata_entry()
        entry["completion_criteria"][0]["level"] = "prefix"
        entry["completion_criteria"][0]["terminal"] = False
        ctx.code_artifact_metadata = {"search_registry_person": entry}
    ctx.failed_test_nudge_count = 2
    ctx.null_data_streak_count = 3
    ctx.probable_site_block_streak_count = 4

    _record_run_blocks_result(ctx, result, completion_verification=_no_evidence("c0"))

    assert ctx.failed_test_nudge_count == 2
    assert ctx.null_data_streak_count == 3
    assert ctx.probable_site_block_streak_count == 4
    assert ctx.last_full_workflow_test_ok is False
    assert getattr(ctx, "last_good_workflow", None) is None
