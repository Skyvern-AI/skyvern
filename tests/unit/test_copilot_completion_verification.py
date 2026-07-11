from __future__ import annotations

import asyncio
import textwrap
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.agent import (
    _completion_contract_not_violated,
    _rewrite_failed_test_response,
    _verified_workflow_or_none,
)
from skyvern.forge.sdk.copilot.completion_criteria_store import (
    StoredCriteriaSet,
    StoredCriteriaSnapshot,
    apply_requested_output_producer_floor,
    criteria_from_json,
    criteria_to_json,
    reconcile_completion_criteria,
)
from skyvern.forge.sdk.copilot.completion_output_grounding import (
    _schema_boolean_output_paths,
    _value_matches_expected,
    grade_requested_output_criteria,
    split_requested_output_criteria,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
    CompletionVerificationResult,
    CriterionVerdict,
    DeliveredUnverifiedTerminalState,
    EvidenceSourceKind,
    FloorRekeyedDeliverableCredit,
    RunEvidenceSnapshot,
    _coerce_result,
    _structured_record_has_identifier,
    carry_floor_rekeyed_criterion_ids,
    combine_verification_results,
    degraded_contract_delivered_unverified_terminal_state,
    evaluate_completion_criteria,
    floor_rekeyed_deliverable_credit,
    grade_definition_criteria,
    grade_fallback_floor_reached_end_state_criteria,
    grade_present_value_criteria,
    grade_record_semantic_consistency,
    grade_registered_download_criteria,
    grade_structured_record_criteria,
    grade_terminal_goal_record_criteria,
    grade_validation_classification_criteria,
    registered_download_completion_criterion,
    run_plane_all_no_evidence,
    structural_unfired_contingent_criterion_ids,
    structured_record_has_goal_content,
    structured_record_has_identity,
    summarize_unsatisfied_outcomes,
    zero_requested_output_criteria_credit,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisFailureType,
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairNextAction,
    VerificationResult,
    _verification_satisfaction,
    build_diagnosis_repair_contract,
)
from skyvern.forge.sdk.copilot.enforcement import (
    built_unverified_repair_inert_context,
    outcome_fully_verified,
    verified_goal_satisfied_context,
)
from skyvern.forge.sdk.copilot.failure_tracking import ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
from skyvern.forge.sdk.copilot.hooks import _tool_completion_satisfies_turn
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    JudgmentTruthCondition,
    RequestPolicy,
    _apply_classifier_typed_requested_output_corroborators,
    _apply_requested_output_completion_criteria,
    _parse_completion_criteria,
    build_classifier_fallback_floor,
)
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.tools import (
    ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY,
    _active_run_terminal_evidence_needs_visual_fallback,
    _active_run_terminal_evidence_result,
    _active_run_terminal_evidence_sample,
    _build_run_evidence_snapshot,
    _composition_visual_prompt,
    _current_workflow_has_evidence_block,
    _is_outcome_evidence_candidate,
    _is_unfinished_run_verification_candidate,
    _maybe_run_completion_verification,
    _maybe_run_completion_verification_from_page_observation,
    _outcome_failure_warrants_repair,
    _outcome_unverified_reason,
    _record_composition_page_observation,
    _record_run_blocks_result,
    _tool_loop_error,
    _tool_visible_result_after_completion_verification,
    _watchdog_exit_allows_terminal_promotion,
)
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools._shared import (
    _TASK_ENVELOPE_BLOCK_TYPES,
    _has_meaningful_registered_output_payload,
)
from skyvern.forge.sdk.copilot.tools.completion import (
    _POST_RUN_PAGE_OBSERVATION_LABEL,
    _artifact_health_blocker_from_result,
    _completion_verification_from_run_result,
    _reconcile_download_completion_criterion,
)
from skyvern.forge.sdk.copilot.tools.composition_capture import _active_run_terminal_monitor_enabled
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    _apply_code_artifact_requested_output_evidence_sources,
    _normalize_code_artifact_metadata,
)
from tests.unit.copilot_test_helpers import (
    DISPATCHED_LOGIN_GATE_HTML,
    DISPATCHED_NAV_ONLY_HTML,
    DISPATCHED_RESULTS_HTML,
)
from tests.unit.copilot_test_helpers import make_completion_criterion as _criterion
from tests.unit.copilot_test_helpers import (
    make_stub_html_artifact,
    stub_artifact_app,
)

_STRUCTURED_RECORD_CRITERIA = (
    ("fallback_record_identity", "The returned record identifies the target record."),
    ("fallback_record_identifier", "The returned record includes the record identifier."),
    ("fallback_record_groups", "The returned record includes record items."),
    (
        "fallback_record_status",
        "The returned record's per-location statuses and overall status are present and consistent.",
    ),
)
_STRUCTURED_RECORD_CRITERION_IDS = {cid for cid, _ in _STRUCTURED_RECORD_CRITERIA}


def _structured_record_criteria() -> list[CompletionCriterion]:
    return [CompletionCriterion(id=cid, outcome=outcome) for cid, outcome in _STRUCTURED_RECORD_CRITERIA]


def _status_consistency_criteria() -> list[CompletionCriterion]:
    return [
        CompletionCriterion(
            id="fallback_record_status",
            outcome="The returned record's per-location statuses and overall status are present and consistent.",
        )
    ]


def test_structured_record_identity_ignores_substring_only_keys() -> None:
    assert structured_record_has_identity({"provider_name": "Alex Example"}) is True
    assert structured_record_has_identity({"providerName": "Alex Example"}) is True
    assert structured_record_has_identity({"title": "Permit A"}) is True
    assert structured_record_has_identity({"filename": "report.pdf"}) is False
    assert structured_record_has_identity({"tablename": "providers"}) is False


def test_structured_record_identifier_ignores_substring_only_keys() -> None:
    assert _structured_record_has_identifier({"providerId": "abc"}) is True
    assert _structured_record_has_identifier({"record_number": "x"}) is True
    assert _structured_record_has_identifier({"idea": "some text"}) is False
    assert _structured_record_has_identifier({"_identical": "yes"}) is False
    assert structured_record_has_identity({"subtitle": "detail"}) is False
    assert structured_record_has_identity({"mislabeled": "detail"}) is False


def _satisfied_criterion_ids(verdicts: list[CriterionVerdict]) -> set[str]:
    return {verdict.criterion_id for verdict in verdicts if verdict.satisfied}


def _record_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "entity_found": True,
        "entity_name": "Jordan Example",
        "record_number": "1234567890",
        "items": [
            {"item_name": "Sample Practice", "address": "100 Main St, Example City, ST 12345", "status": "Active"},
            {
                "item_name": "Secondary Practice",
                "address": "300 Market St, Example City, ST 12345",
                "status": "Inactive",
            },
        ],
        "overall_status": "Active",
        "evidence_text": "Opened Details page; read Overview/Affiliations items and More Details identifier.",
    }
    payload.update(overrides)
    return payload


def _terminal_goal_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "submitted": True,
        "blocker": None,
        "confirmation_number": "WTR-1842-DEMO",
        "account_number": "100245",
        "selected_start_date": "2026-06-22",
        "deposit_amount": "$41.00 plus initiation fee",
        "next_owner": "Provider",
        "evidence_text": "Water Service Request Submitted. Confirmation Number WTR-1842-DEMO.",
    }
    payload.update(overrides)
    return payload


def _validation_review_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "all_checks_passed": True,
        "validation_only": True,
        "review_page_visible": True,
        "submit_or_finalize_clicked": False,
        "submitted_request": False,
        "confirmation_page_visible": False,
        "review_values": {
            "visible_service_address": "1234 Sample Utility Way, Testville, CA 94016",
            "visible_requested_start_date": "2026-06-22",
            "visible_account_holder": "EXAMPLE REALTY LABS INC",
        },
        "evidence_text": (
            "Visible Review page showed service address 1234 Sample Utility Way, Testville, CA 94016, "
            "start date 2026-06-22, and account holder EXAMPLE REALTY LABS INC. "
            "No Submit Request or final confirmation control was clicked."
        ),
    }
    payload.update(overrides)
    return payload


def _generated_validation_review_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "pre_submit_review_reached": True,
        "exact_service_address_verified": True,
        "requested_start_date_verified": True,
        "business_name_verified": True,
        "account_holder_verified": True,
        "safe_mailing_address_verified": True,
        "no_payment_or_deposit_gate": True,
        "validation_only": True,
        "submit_or_finalize_clicked": False,
        "terminal_summary": "Pre-submit review reached: True; submit/finalize control clicked: False.",
        "final_controls_visible": ["Submit Request", "Back"],
        "review_fields": {
            "Service Address": "1234 Sample Utility Way, Testville, CA 94016",
            "Requested Start Date": "2026-06-22",
            "Business Name": "Example Realty Labs Inc",
            "Account Holder": "EXAMPLE REALTY LABS INC",
        },
        "evidence_text": (
            "Start Service - Review\n"
            "Service Address\n1234 Sample Utility Way, Testville, CA 94016\n"
            "Requested Start Date\n2026-06-22\n"
            "Business Name\nExample Realty Labs Inc\n"
            "Account Holder\nEXAMPLE REALTY LABS INC\n"
            "Submit Request\nBack"
        ),
    }
    payload.update(overrides)
    return payload


def _live_validation_review_output_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "all_validations_passed": True,
        "evidence_text": (
            "Step 1 Find Address\n"
            "Step 2 About Business\n"
            "Step 3 Account Information\n"
            "Step 4 Review\n"
            "Step 5 Confirmation\n"
            "Start Service - Review\n\n"
            "Review all request details before final submission.\n\n"
            "Service Address\n1234 Sample Utility Way, Testville, CA 94016\n"
            "Requested Start Date\n2026-06-22\n"
            "Business Name\nExample Realty Labs Inc\n"
            "Account Holder\nEXAMPLE REALTY LABS INC\n"
            "Mailing Address\nPO Box 4300, Harbor City, CA 90210\n"
            "Tax Identifier\nMasked ending in 4321\n"
            "Payment / Deposit\nNo payment, deposit, autopay, or unsafe attestation required before review.\n"
            "Submit Request\nBack"
        ),
        "final_submit_controls_present": 1,
        "pre_submit_review_reached": True,
        "review_page_label": "Start Service - Review",
        "submit_mode": "validation_only",
        "review_values": {
            "Account Holder": "EXAMPLE REALTY LABS INC",
            "Business Name": "Example Realty Labs Inc",
            "Mailing Address": "PO Box 4300, Harbor City, CA 90210",
            "Payment / Deposit": "No payment, deposit, autopay, or unsafe attestation required before review.",
            "Requested Start Date": "2026-06-22",
            "Service Address": "1234 Sample Utility Way, Testville, CA 94016",
            "Tax Identifier": "Masked ending in 4321",
        },
        "submit_finalize_control_clicked": False,
        "terminal_summary": "Pre-submit review reached: True; submit/finalize control clicked: False",
        "validations": {
            "account_holder_verified": True,
            "business_name_verified": True,
            "exact_service_address_verified": True,
            "no_payment_deposit_gate_verified": True,
            "requested_start_date_verified": True,
            "safe_mailing_address_verified": True,
        },
    }
    payload.update(overrides)
    return payload


def _status_snapshot(
    status: str,
    *,
    item_name: str = "Sample Practice",
    evidence_text: str | None = None,
) -> RunEvidenceSnapshot:
    payload = _record_payload(
        items=[{"item_name": item_name, "address": "100 Main St, Example City, ST 12345", "status": status}],
        overall_status=status,
    )
    if evidence_text is not None:
        payload["evidence_text"] = evidence_text
    return RunEvidenceSnapshot(block_outputs={"lookup_record_status": payload})


def _evaluated(*satisfied_by_id: tuple[str, bool]) -> CompletionVerificationResult:
    ids = [cid for cid, _ in satisfied_by_id]
    verdicts = [
        CriterionVerdict(
            criterion_id=cid,
            state="satisfied" if ok else "unsatisfied",
            reason_code="evidence_confirms" if ok else "no_evidence",
        )
        for cid, ok in satisfied_by_id
    ]
    return CompletionVerificationResult(status="evaluated", criterion_ids=ids, verdicts=verdicts)


def _completion_handler_lookup(handler: object) -> Callable[[object], Awaitable[object]]:
    async def _lookup(_ctx: object) -> object:
        return handler

    return _lookup


def _patch_completion_handler(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler",
        _completion_handler_lookup(handler),
    )


def test_is_fully_satisfied_requires_every_criterion() -> None:
    assert _evaluated(("c0", True), ("c1", True)).is_fully_satisfied() is True
    assert _evaluated(("c0", True), ("c1", False)).is_fully_satisfied() is False


def test_observed_end_state_satisfaction_takes_precedence_over_reperception_contradiction() -> None:
    result = _mixed(
        CriterionVerdict(
            criterion_id="c_reach",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="observed_end_state_url",
        ),
        CriterionVerdict(
            criterion_id="c_reperception",
            state="unsatisfied",
            reason_code="evidence_contradicts",
            evidence_ref="scout_synthesized_browser_steps_output",
        ),
    )

    assert result.is_fully_satisfied() is True


def test_reperception_contradiction_without_observed_end_state_satisfaction_still_blocks() -> None:
    result = _mixed(
        CriterionVerdict(
            criterion_id="c_output",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:submit_request",
        ),
        CriterionVerdict(
            criterion_id="c_reperception",
            state="unsatisfied",
            reason_code="evidence_contradicts",
            evidence_ref="scout_synthesized_browser_steps_output",
        ),
    )

    assert result.is_fully_satisfied() is False


def test_observed_end_state_satisfaction_does_not_override_requested_output_contradiction() -> None:
    result = _mixed(
        CriterionVerdict(
            criterion_id="c_reach",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="observed_end_state_url",
        ),
        CriterionVerdict(
            criterion_id="c_requested_output",
            state="unsatisfied",
            reason_code="evidence_contradicts",
            evidence_ref="block_outputs:submit_request.confirmation_number",
        ),
    )

    assert result.is_fully_satisfied() is False


def test_observed_end_state_corroborates_structural_requested_output_abstention() -> None:
    result = _mixed(
        CriterionVerdict(
            criterion_id="c_reach",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="observed_end_state_url",
        ),
        CriterionVerdict(
            criterion_id="c_requested_output",
            state="unsatisfied",
            reason_code="structurally_abstained",
            evidence_ref="block_outputs:extract_profile.customer_name",
            output_path="output.customer_name",
            grounding_mode="shape",
        ),
    )

    assert result.is_fully_satisfied() is True


def test_structural_requested_output_abstention_without_typed_corroboration_does_not_veto() -> None:
    result = _mixed(
        CriterionVerdict(
            criterion_id="c_output",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="observed_end_state_url",
            evidence_source="independent_page_evidence",
        ),
        CriterionVerdict(
            criterion_id="c_requested_output",
            state="unsatisfied",
            reason_code="structurally_abstained",
            evidence_ref="block_outputs:extract_profile.customer_name",
            output_path="output.customer_name",
            grounding_mode="shape",
        ),
    )

    assert result.is_fully_satisfied() is True


def test_structural_requested_output_abstention_without_typed_corroboration_gets_no_floor_credit() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_requested_output"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_output",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="block_outputs:extract_profile",
            ),
            CriterionVerdict(
                criterion_id="c_requested_output",
                state="unsatisfied",
                reason_code="structurally_abstained",
                evidence_ref="block_outputs:extract_profile.customer_name",
                output_path="output.customer_name",
                grounding_mode="shape",
            ),
        ],
    )

    assert result.is_fully_satisfied() is False


def test_structural_requested_output_abstention_with_classifier_corroborator_satisfies() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c0__requested_output_corroborator"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="unsatisfied",
                reason_code="structurally_abstained",
                evidence_ref="block_outputs:extract_first_three_quotes.quotes",
                output_path="output.quotes",
                grounding_mode="missing",
            ),
            CriterionVerdict(
                criterion_id="c0__requested_output_corroborator",
                state="satisfied",
                reason_code="evidence_confirms",
            ),
        ],
    )

    assert result.is_fully_satisfied() is True


def test_requested_output_no_evidence_with_observed_end_state_still_blocks() -> None:
    result = _mixed(
        CriterionVerdict(
            criterion_id="c_reach",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="observed_end_state_url",
        ),
        CriterionVerdict(
            criterion_id="c_requested_output",
            state="unsatisfied",
            reason_code="no_evidence",
            evidence_ref="block_outputs:extract_profile.customer_name",
            output_path="output.customer_name",
            grounding_mode="missing",
        ),
    )

    assert result.is_fully_satisfied() is False


def test_terminal_record_corroborates_structural_requested_output_abstention() -> None:
    result = _mixed(
        CriterionVerdict(
            criterion_id="c_submit",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:submit_request",
            grounding_mode="terminal_record",
        ),
        CriterionVerdict(
            criterion_id="c_requested_output",
            state="unsatisfied",
            reason_code="structurally_abstained",
            evidence_ref="block_outputs:submit_request.confirmation_number",
            output_path="output.confirmation_number",
            grounding_mode="shape",
        ),
    )

    assert result.is_fully_satisfied() is True


def test_terminal_record_corroboration_gives_structural_requested_output_abstention_floor_credit() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_requested_output"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_submit",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="block_outputs:submit_request",
                grounding_mode="terminal_record",
            ),
            CriterionVerdict(
                criterion_id="c_requested_output",
                state="unsatisfied",
                reason_code="structurally_abstained",
                evidence_ref="block_outputs:submit_request.confirmation_number",
                output_path="output.confirmation_number",
                grounding_mode="shape",
            ),
        ],
    )

    assert result.is_fully_satisfied() is True


def test_unmarked_block_output_does_not_corroborate_structural_requested_output_abstention() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_requested_output"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_submit",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="block_outputs:submit_request",
            ),
            CriterionVerdict(
                criterion_id="c_requested_output",
                state="unsatisfied",
                reason_code="structurally_abstained",
                evidence_ref="block_outputs:submit_request.confirmation_number",
                output_path="output.confirmation_number",
                grounding_mode="shape",
            ),
        ],
    )

    assert result.is_fully_satisfied() is False


def test_requested_output_wrong_exact_value_with_observed_end_state_still_blocks() -> None:
    result = _mixed(
        CriterionVerdict(
            criterion_id="c_reach",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="observed_end_state_url",
        ),
        CriterionVerdict(
            criterion_id="c_requested_output",
            state="unsatisfied",
            reason_code="evidence_contradicts",
            evidence_ref="block_outputs:submit_request.confirmation_number",
            output_path="output.confirmation_number",
            grounding_mode="exact_value",
            has_exact_value=True,
        ),
    )

    assert result.is_fully_satisfied() is False


def _mixed(*verdicts: CriterionVerdict) -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated", criterion_ids=[v.criterion_id for v in verdicts], verdicts=list(verdicts)
    )


def test_definition_plane_abstention_does_not_sink_evidence_confirmed_run() -> None:
    definition_unknown = CriterionVerdict(
        criterion_id="c0", state="unknown", reason_code="definition_parameters_absent"
    )
    confirmed = CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms")
    assert _mixed(definition_unknown, confirmed).is_fully_satisfied() is True


def test_run_plane_unknown_still_blocks() -> None:
    run_unknown = CriterionVerdict(criterion_id="c0", state="unknown", reason_code="unknown")
    confirmed = CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms")
    assert _mixed(run_unknown, confirmed).is_fully_satisfied() is False


def test_all_definition_abstentions_do_not_vacuously_satisfy() -> None:
    abstain_a = CriterionVerdict(criterion_id="c0", state="unknown", reason_code="definition_unknown")
    abstain_b = CriterionVerdict(criterion_id="c1", state="unknown", reason_code="definition_parameters_absent")
    assert _mixed(abstain_a, abstain_b).is_fully_satisfied() is False


def test_definition_plane_unsatisfied_still_blocks() -> None:
    unreferenced = CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="definition_parameters_missing")
    confirmed = CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms")
    assert _mixed(unreferenced, confirmed).is_fully_satisfied() is False


def test_unfired_contingent_abstention_does_not_sink_evidence_confirmed_run() -> None:
    contingent_unknown = CriterionVerdict(criterion_id="c0", state="unknown", reason_code="unknown")
    confirmed = CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms")
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1"],
        contingent_criterion_ids=["c0"],
        contingent_on_by_criterion_id={"c0": "the provider site blocks online submission"},
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
        structural_unfired_criterion_ids=["c0"],
        verdicts=[contingent_unknown, confirmed],
    )

    assert result.is_fully_satisfied() is True
    trace = result.to_trace_data()
    assert trace["contingent_criterion_ids"] == ["c0"]
    assert trace["structural_unfired_criterion_ids"] == ["c0"]
    assert trace["verdict_0_state"] == "unknown"
    assert trace["verdict_0_contingent_on"] == "the provider site blocks online submission"
    assert trace["verdict_0_contingent_antecedent_output_path"] == "output.blocker"
    assert trace["verdict_0_structural_unfired"] is True
    assert trace["unmet_criterion_ids"] == []
    assert trace["missing_evidence"] == []
    assert "verdict_0_missing_evidence" not in trace


@pytest.mark.parametrize(
    "reason_code,state",
    [("unknown", "unknown"), ("no_evidence", "unsatisfied"), ("evidence_contradicts", "unsatisfied")],
)
def test_structural_unfired_contingent_abstention_accepts_non_satisfied_verdicts(reason_code: str, state: str) -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
        structural_unfired_criterion_ids=["c0"],
        verdicts=[
            CriterionVerdict(criterion_id="c0", state=state, reason_code=reason_code),  # type: ignore[arg-type]
            CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms"),
        ],
    )

    assert result.is_fully_satisfied() is True


def test_all_contingent_abstentions_do_not_vacuously_satisfy() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
        structural_unfired_criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="unknown", reason_code="unknown")],
    )

    assert result.is_fully_satisfied() is False


def test_contingent_reason_does_not_abstain_for_non_contingent_criterion() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1"],
        verdicts=[
            CriterionVerdict(criterion_id="c0", state="unknown", reason_code="unknown"),
            CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms"),
        ],
        structural_unfired_criterion_ids=["c0"],
    )

    assert result.is_fully_satisfied() is False


def test_judge_contingent_reason_cannot_authorize_structural_abstention() -> None:
    raw = {
        "verdicts": [
            {"criterion_id": "c0", "satisfied": False, "reason_code": "contingent_unfired"},
            {"criterion_id": "c1", "satisfied": True, "reason_code": "evidence_confirms"},
        ]
    }

    result = _coerce_result(
        raw,
        ["c0", "c1"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
    )

    assert result.verdicts[0].reason_code == "unknown"
    assert result.verdicts[0].state == "unknown"
    assert result.is_fully_satisfied() is False


def test_fired_contingent_criterion_without_blocker_evidence_fails() -> None:
    raw = {"verdicts": [{"criterion_id": "c0", "satisfied": False, "reason_code": "no_evidence"}]}
    criteria = [
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
            contingent_antecedent_output_path="output.blocker",
        )
    ]
    result = _coerce_result(
        raw,
        ["c0"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
        structural_unfired_criterion_ids=structural_unfired_contingent_criterion_ids(
            criteria,
            RunEvidenceSnapshot(block_outputs={"terminal_result": {"blocker": "Provider requires a phone call."}}),
        ),
    )

    assert result.verdicts[0].reason_code == "no_evidence"
    assert result.structural_unfired_criterion_ids == []
    assert result.is_fully_satisfied() is False


def test_fired_contingent_criterion_with_blocker_evidence_can_satisfy() -> None:
    raw = {
        "verdicts": [
            {"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"},
            {"criterion_id": "c1", "satisfied": True, "reason_code": "evidence_confirms"},
        ]
    }
    result = _coerce_result(
        raw,
        ["c0", "c1"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
    )

    assert result.is_fully_satisfied() is True


def test_combine_verification_results_preserves_contingent_ids() -> None:
    run_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1"],
        contingent_criterion_ids=["c1"],
        contingent_on_by_criterion_id={"c1": "the provider site blocks online submission"},
        contingent_antecedent_output_path_by_criterion_id={"c1": "output.blocker"},
        structural_unfired_criterion_ids=["c1"],
        verdicts=[
            CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms"),
            CriterionVerdict(criterion_id="c1", state="unknown", reason_code="unknown"),
        ],
    )

    result = combine_verification_results(["c0", "c1"], run_result, [])

    assert result.contingent_criterion_ids == ["c1"]
    assert result.contingent_on_by_criterion_id == {"c1": "the provider site blocks online submission"}
    assert result.contingent_antecedent_output_path_by_criterion_id == {"c1": "output.blocker"}
    assert result.structural_unfired_criterion_ids == ["c1"]
    assert result.is_fully_satisfied() is True


def test_structural_unfired_ids_derive_from_empty_output_path() -> None:
    criteria = [
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
            contingent_antecedent_output_path="output.blocker",
        )
    ]
    snapshot = RunEvidenceSnapshot(block_outputs={"blocker_output": None})

    assert structural_unfired_contingent_criterion_ids(criteria, snapshot) == ["c0"]


def test_p7_manual_service_no_blocker_abstains() -> None:
    criteria = [
        _criterion(
            "c0",
            "Any manual service blocker is reported to the user.",
            contingent_on="a manual service blocker exists",
            contingent_antecedent_output_path="output.blocker",
        )
    ]
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": {"manual_service_blocker": None}})

    assert structural_unfired_contingent_criterion_ids(criteria, snapshot) == ["c0"]


@pytest.mark.parametrize("reason_code,state", [("evidence_contradicts", "unsatisfied"), ("unknown", "unknown")])
def test_false_contingent_antecedent_output_abstains(reason_code: str, state: str) -> None:
    criteria = [
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
            contingent_antecedent_output_path="output.blocker",
        )
    ]
    structural_unfired_ids = structural_unfired_contingent_criterion_ids(
        criteria,
        RunEvidenceSnapshot(block_outputs={"terminal_result": {"blocker": False}}),
    )
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
        structural_unfired_criterion_ids=structural_unfired_ids,
        verdicts=[
            CriterionVerdict(criterion_id="c0", state=state, reason_code=reason_code),  # type: ignore[arg-type]
            CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms"),
        ],
    )

    assert structural_unfired_ids == ["c0"]
    assert result.is_fully_satisfied() is True


def test_true_contingent_antecedent_output_fires_and_requires_consequent_evidence() -> None:
    criteria = [
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
            contingent_antecedent_output_path="output.blocker",
        )
    ]
    structural_unfired_ids = structural_unfired_contingent_criterion_ids(
        criteria,
        RunEvidenceSnapshot(block_outputs={"terminal_result": {"blocker": True}}),
    )
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
        structural_unfired_criterion_ids=structural_unfired_ids,
        verdicts=[
            CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="evidence_contradicts"),
            CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms"),
        ],
    )

    assert structural_unfired_ids == []
    assert result.is_fully_satisfied() is False


def test_missing_contingent_antecedent_output_path_fails_closed() -> None:
    criteria = [
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
            contingent_antecedent_output_path="output.blocker",
        )
    ]
    snapshot = RunEvidenceSnapshot(block_outputs={"confirmation_output": "submitted"})
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
        structural_unfired_criterion_ids=structural_unfired_contingent_criterion_ids(criteria, snapshot),
        verdicts=[
            CriterionVerdict(criterion_id="c0", state="unknown", reason_code="unknown"),
            CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms"),
        ],
    )

    assert result.structural_unfired_criterion_ids == []
    assert result.is_fully_satisfied() is False


def test_structural_fired_evidence_overrides_empty_output_path() -> None:
    criteria = [
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
            contingent_antecedent_output_path="output.blocker",
        )
    ]
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "blocker_output": None,
            "blocker_detector": {"blocker": "Provider site requires a phone call."},
        }
    )

    assert structural_unfired_contingent_criterion_ids(criteria, snapshot) == []


def test_real_blocker_family_evidence_overrides_primary_no_blocker_marker() -> None:
    criteria = [
        _criterion(
            "c0",
            "A provider blocker is reported to the user.",
            contingent_on="the provider site blocks online submission",
            contingent_antecedent_output_path="output.blocker",
        )
    ]
    snapshot = RunEvidenceSnapshot(
        block_outputs={"terminal_result": {"blocker": None, "manual_service_blocker": "Provider requires phone call"}}
    )
    structural_unfired_ids = structural_unfired_contingent_criterion_ids(criteria, snapshot)
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
        structural_unfired_criterion_ids=structural_unfired_ids,
        verdicts=[
            CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="evidence_contradicts"),
            CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms"),
        ],
    )

    assert structural_unfired_ids == []
    assert result.is_fully_satisfied() is False


def test_empty_verdicts_with_criteria_is_not_vacuously_satisfied() -> None:
    result = CompletionVerificationResult(status="evaluated", criterion_ids=["c0"], verdicts=[])
    assert result.is_fully_satisfied() is False


def test_unavailable_and_empty_criteria_never_satisfied() -> None:
    assert CompletionVerificationResult(status="unavailable").is_fully_satisfied() is False
    assert CompletionVerificationResult(status="evaluated", criterion_ids=[]).is_fully_satisfied() is False


def test_coerce_requires_evidence_confirms_for_satisfied() -> None:
    raw = {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "unknown"}]}
    result = _coerce_result(raw, ["c0"])
    assert result.status == "evaluated"
    assert result.verdicts[0].satisfied is False


def test_coerce_preserves_missing_evidence_for_unmet_verdict() -> None:
    raw = {
        "verdicts": [
            {
                "criterion_id": "c0",
                "satisfied": False,
                "reason_code": "no_evidence",
                "missing_evidence": "block output containing the extracted first paragraph",
                "evidence_ref": "extract_example_page",
            }
        ]
    }

    result = _coerce_result(raw, ["c0"])

    assert result.verdicts[0].missing_evidence == "block output containing the extracted first paragraph"
    trace = result.to_trace_data()
    assert trace["unmet_criterion_ids"] == ["c0"]
    assert trace["missing_evidence"] == ["c0: block output containing the extracted first paragraph"]
    assert trace["verdict_0_criterion_id"] == "c0"
    assert trace["verdict_0_reason_code"] == "no_evidence"
    assert trace["verdict_0_missing_evidence"] == "block output containing the extracted first paragraph"
    assert trace["verdict_0_evidence_ref"] == "extract_example_page"


def test_coerce_bounds_and_redacts_missing_evidence_and_evidence_ref() -> None:
    raw = {
        "verdicts": [
            {
                "criterion_id": "c0",
                "satisfied": False,
                "reason_code": "unknown",
                "evidence_ref": "https://example.test/callback?password=hunter2&token=abc " + ("y" * 700),
                "missing_evidence": "password: hunter2 " + ("x" * 700),
            }
        ]
    }

    result = _coerce_result(raw, ["c0"])

    missing = result.verdicts[0].missing_evidence
    assert missing is not None
    assert "hunter2" not in missing
    assert len(missing) <= 500
    evidence_ref = result.verdicts[0].evidence_ref
    assert evidence_ref is not None
    assert "hunter2" not in evidence_ref
    assert "token=abc" not in evidence_ref
    assert len(evidence_ref) <= 240


def test_trace_redacts_direct_missing_evidence_and_evidence_ref_values() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="unsatisfied",
                reason_code="unknown",
                missing_evidence="password: hunter2 " + ("x" * 700),
            )
        ],
    )

    trace = result.to_trace_data()

    assert "hunter2" not in trace["missing_evidence"][0]
    assert "hunter2" not in trace["verdict_0_missing_evidence"]
    assert len(trace["verdict_0_missing_evidence"]) <= 500
    trace = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="unsatisfied",
                reason_code="unknown",
                evidence_ref="password: hunter2 " + ("z" * 700),
            )
        ],
    ).to_trace_data()
    assert "hunter2" not in trace["verdict_0_evidence_ref"]
    assert len(trace["verdict_0_evidence_ref"]) <= 240


def test_coerce_missing_criterion_defaults_to_diagnosable_unknown() -> None:
    result = _coerce_result({"verdicts": []}, ["c0", "c1"])
    assert [v.reason_code for v in result.verdicts] == ["unknown", "unknown"]
    assert [v.state for v in result.verdicts] == ["unknown", "unknown"]
    assert [v.missing_evidence for v in result.verdicts] == [
        "judge did not return a verdict for this criterion",
        "judge did not return a verdict for this criterion",
    ]
    assert result.is_fully_satisfied() is False


def test_coerce_ignores_unknown_ids_and_dedupes_first_wins() -> None:
    raw = {
        "verdicts": [
            {"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"},
            {"criterion_id": "c0", "satisfied": False, "reason_code": "no_evidence"},
            {"criterion_id": "ghost", "satisfied": True, "reason_code": "evidence_confirms"},
        ]
    }
    result = _coerce_result(raw, ["c0"])
    assert len(result.verdicts) == 1
    assert result.verdicts[0].satisfied is True


def test_coerce_accepts_bytes_and_rejects_malformed() -> None:
    raw_bytes = b'{"verdicts": [{"criterion_id": "c0", "satisfied": true, "reason_code": "evidence_confirms"}]}'
    assert _coerce_result(raw_bytes, ["c0"]).is_fully_satisfied() is True
    assert _coerce_result("not json at all", ["c0"]).status == "unavailable"
    assert _coerce_result({"no_verdicts_key": 1}, ["c0"]).status == "unavailable"


@pytest.mark.parametrize(
    ("negative_status", "item_name", "missing_text"),
    [
        ("Inactive", "Sample Practice 200 Oak Ave, Example City, ST 12345 Active", "non-status fields include"),
        *[
            (status, "Sample Practice Active", None)
            for status in ["Expired", "Suspended", "Terminated", "Revoked", "Lapsed", "Pending"]
        ],
    ],
)
def test_record_semantic_consistency_flags_status_contradictions(
    negative_status: str, item_name: str, missing_text: str | None
) -> None:
    verdicts = grade_record_semantic_consistency(
        _status_consistency_criteria(),
        _status_snapshot(negative_status, item_name=item_name),
    )

    assert len(verdicts) == 1
    assert verdicts[0].criterion_id == "fallback_record_status"
    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    if missing_text:
        assert missing_text in (verdicts[0].missing_evidence or "")


@pytest.mark.parametrize(
    ("status", "item_name", "evidence_text"),
    [
        ("Non-active", "Sample Practice non-active listing", "Sample Practice non-active listing 100 Main St"),
        ("Active", "Sample Practice", "Sample Practice 100 Main St, Example City, ST 12345 Active"),
        *[
            ("Expired", "Sample Practice", text)
            for text in [
                "License is no longer active",
                "Provider was previously active",
                "Status note: not currently active",
                "The active license expired",
            ]
        ],
    ],
)
def test_record_semantic_consistency_accepts_non_contradictory_status_text(
    status: str, item_name: str, evidence_text: str
) -> None:
    assert (
        grade_record_semantic_consistency(
            _status_consistency_criteria(), _status_snapshot(status, item_name=item_name, evidence_text=evidence_text)
        )
        == []
    )


def test_structured_record_identifier_requires_consecutive_digit_run() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "lookup_record": _record_payload(
                phone="555-1234",
                record_number=None,
                items=[{"item_name": "Sample Practice", "address": "1234 Main St, Apt 56", "status": "Active"}],
            )
        }
    )

    satisfied = _satisfied_criterion_ids(grade_structured_record_criteria(_structured_record_criteria(), snapshot))

    assert "fallback_record_identifier" not in satisfied
    assert "fallback_record_identity" in satisfied


@pytest.mark.parametrize(
    "block_outputs",
    [
        {
            "extract_record_status_info": {
                "extract_record_status_info_output": _record_payload(),
                "extracted_information": [],
            }
        },
        {"extract_record_status_record_output": _record_payload(found=True, entity_found=None)},
    ],
)
def test_structured_record_criteria_satisfy_structured_record_outputs(block_outputs: dict[str, Any]) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs=block_outputs)

    verdicts = grade_structured_record_criteria(_structured_record_criteria(), snapshot)

    assert _satisfied_criterion_ids(verdicts) == _STRUCTURED_RECORD_CRITERION_IDS


def test_terminal_goal_record_satisfies_flat_submit_payload() -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_water_request": _terminal_goal_payload()})

    verdicts = grade_terminal_goal_record_criteria(
        [
            _criterion(
                "c0",
                "a commercial water service request is submitted",
                kind="terminal_action",
                terminal_action_family="request",
            )
        ],
        snapshot,
    )

    assert verdicts == [
        CriterionVerdict(
            criterion_id="c0",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:submit_water_request",
            grounding_mode="terminal_record",
            evidence_source="terminal_record",
        )
    ]


def test_terminal_goal_record_accepts_family_artifact_without_self_asserted_boolean() -> None:
    payload = _terminal_goal_payload(submitted=None)
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_water_request": payload})

    verdicts = grade_terminal_goal_record_criteria(
        [
            _criterion(
                "c0",
                "a commercial water service request is submitted",
                kind="terminal_action",
                terminal_action_family="request",
            )
        ],
        snapshot,
    )

    assert _satisfied_criterion_ids(verdicts) == {"c0"}


def test_terminal_goal_record_accepts_started_request_when_typed_with_artifact() -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_water_request": _terminal_goal_payload()})

    verdicts = grade_terminal_goal_record_criteria(
        [
            _criterion(
                "c0",
                "a commercial water service request is started",
                kind="terminal_action",
                terminal_action_family="request",
            )
        ],
        snapshot,
    )

    assert _satisfied_criterion_ids(verdicts) == {"c0"}


def test_terminal_goal_record_accepts_typed_create_or_verify_with_identifier() -> None:
    payload = _terminal_goal_payload(
        submitted=None,
        confirmation_number=None,
        request_id="QC-2002-DEMO",
        submission_result="already_present",
    )
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    verdicts = grade_terminal_goal_record_criteria(
        [
            _criterion(
                "c0",
                "the service request is created or verified",
                kind="terminal_action",
                terminal_action_family="request",
            )
        ],
        snapshot,
    )

    assert _satisfied_criterion_ids(verdicts) == {"c0"}


def test_terminal_goal_record_submission_result_is_not_credit_source() -> None:
    payload = _terminal_goal_payload(
        submitted=None,
        confirmation_number=None,
        request_id=None,
        submission_result="already_present",
    )
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    assert (
        grade_terminal_goal_record_criteria(
            [
                _criterion(
                    "c0",
                    "the service request is created or verified",
                    kind="terminal_action",
                    terminal_action_family="request",
                )
            ],
            snapshot,
        )
        == []
    )


def test_terminal_goal_record_requires_typed_terminal_action_family() -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_water_request": _terminal_goal_payload()})

    assert grade_terminal_goal_record_criteria([_criterion("c0", "a request is submitted")], snapshot) == []
    assert (
        grade_terminal_goal_record_criteria(
            [_criterion("c1", "a request is submitted", kind="terminal_action")], snapshot
        )
        == []
    )
    assert (
        grade_terminal_goal_record_criteria(
            [
                _criterion(
                    "c2",
                    "a request is submitted",
                    kind="terminal_action",
                    terminal_action_family="invoice",
                )
            ],
            snapshot,
        )
        == []
    )


def test_terminal_goal_record_self_asserted_boolean_without_identifier_remains_uncredited() -> None:
    payload = _terminal_goal_payload(submitted=True, confirmation_number=None)
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    assert (
        grade_terminal_goal_record_criteria(
            [
                _criterion(
                    "c0",
                    "a request is submitted",
                    kind="terminal_action",
                    terminal_action_family="request",
                )
            ],
            snapshot,
        )
        == []
    )


def test_submit_terminal_still_rejects_false_submit_with_author_time_log_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": _terminal_goal_payload(submitted=False)})

    assert (
        grade_terminal_goal_record_criteria(
            [
                _criterion(
                    "c0",
                    "a commercial water service request is submitted",
                    kind="terminal_action",
                    terminal_action_family="request",
                )
            ],
            snapshot,
        )
        == []
    )


@pytest.mark.parametrize(
    "payload",
    [
        _terminal_goal_payload(confirmation_number=None),
        _terminal_goal_payload(confirmation_number=True),
        _terminal_goal_payload(confirmation_number="", account_number="100245"),
        _terminal_goal_payload(confirmation_number=None, record_number="RN-100245"),
        _terminal_goal_payload(confirmation_number=None, customer_id="cus_123456"),
    ],
)
def test_terminal_goal_record_rejects_ordinary_identifiers(payload: dict[str, Any]) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_water_request": payload})

    assert (
        grade_terminal_goal_record_criteria(
            [
                _criterion(
                    "c0",
                    "a commercial water service request is submitted",
                    kind="terminal_action",
                    terminal_action_family="request",
                )
            ],
            snapshot,
        )
        == []
    )


@pytest.mark.parametrize(
    ("payload", "outcome", "family"),
    [
        (
            _terminal_goal_payload(confirmation_number="WTR-1842-DEMO"),
            "a commercial water service request is submitted",
            "request",
        ),
        (
            _terminal_goal_payload(order_placed=True, submitted=None, order_number="ORD-1842"),
            "an order is placed",
            "order",
        ),
        (
            _terminal_goal_payload(application_submitted=True, submitted=None, application_id="APP-1842"),
            "an application is submitted",
            "application",
        ),
        (
            _terminal_goal_payload(form_submitted=True, submitted=None, submission_id="SUB-1842"),
            "the form is submitted",
            "form",
        ),
        (
            _terminal_goal_payload(request_submitted=True, submitted=None, request_id="REQ-1842"),
            "a service request is submitted",
            "request",
        ),
    ],
)
def test_terminal_goal_record_accepts_narrow_terminal_artifacts(
    payload: dict[str, Any], outcome: str, family: str
) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    verdicts = grade_terminal_goal_record_criteria(
        [_criterion("c0", outcome, kind="terminal_action", terminal_action_family=family)], snapshot
    )

    assert _satisfied_criterion_ids(verdicts) == {"c0"}


@pytest.mark.parametrize(
    ("payload", "outcome", "family"),
    [
        (_terminal_goal_payload(), "an order is placed", "order"),
        (
            _terminal_goal_payload(order_placed=True, submitted=None, order_number="ORD-1842"),
            "a request is submitted",
            "request",
        ),
        (
            _terminal_goal_payload(application_submitted=True, submitted=None, application_id="APP-1842"),
            "an order is placed",
            "order",
        ),
    ],
)
def test_terminal_goal_record_rejects_mismatched_families(payload: dict[str, Any], outcome: str, family: str) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    assert (
        grade_terminal_goal_record_criteria(
            [_criterion("c0", outcome, kind="terminal_action", terminal_action_family=family)], snapshot
        )
        == []
    )


@pytest.mark.parametrize(
    "payload",
    [
        _terminal_goal_payload(submitted=None, completed=True),
        _terminal_goal_payload(submitted=None, succeeded=True),
        _terminal_goal_payload(submitted=None, success=True),
        _terminal_goal_payload(submitted=None, status="completed"),
    ],
)
def test_terminal_goal_record_rejects_generic_success_synonyms(payload: dict[str, Any]) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    assert (
        grade_terminal_goal_record_criteria(
            [
                _criterion(
                    "c0",
                    "a request is submitted",
                    kind="terminal_action",
                    terminal_action_family="request",
                )
            ],
            snapshot,
        )
        == []
    )


@pytest.mark.parametrize(
    "outcome",
    [
        "the account status is retrieved",
        "the record lookup result is returned",
        "the water request status is shown",
    ],
)
def test_terminal_goal_record_abstains_for_lookup_and_status_criteria(outcome: str) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": _terminal_goal_payload()})

    assert grade_terminal_goal_record_criteria([_criterion("c0", outcome)], snapshot) == []


@pytest.mark.parametrize(
    "payload",
    [
        _terminal_goal_payload(blocker="provider requires a phone call"),
        _terminal_goal_payload(error="submission failed"),
        _terminal_goal_payload(failure_reason="network failure"),
        _terminal_goal_payload(challenge_detected=True),
        _terminal_goal_payload(submitted=False),
        _terminal_goal_payload(status="failed"),
        _terminal_goal_payload(status="denied"),
        _terminal_goal_payload(status="cancelled"),
        _terminal_goal_payload(status="canceled"),
        _terminal_goal_payload(status="incomplete"),
        _terminal_goal_payload(status="timeout"),
        _terminal_goal_payload(status="captcha required"),
        _terminal_goal_payload(status="not submitted"),
        _terminal_goal_payload(status="unable to submit"),
    ],
)
def test_terminal_goal_record_negative_guards_abstain(payload: dict[str, Any]) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    assert (
        grade_terminal_goal_record_criteria(
            [
                _criterion(
                    "c0",
                    "a commercial water service request is submitted",
                    kind="terminal_action",
                    terminal_action_family="request",
                )
            ],
            snapshot,
        )
        == []
    )


@pytest.mark.parametrize("key", ["not_submitted", "previously_submitted"])
def test_terminal_goal_record_rejects_negated_or_temporal_action_keys(key: str) -> None:
    payload = _terminal_goal_payload(submitted=None)
    payload[key] = True
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    assert (
        grade_terminal_goal_record_criteria(
            [
                _criterion(
                    "c0",
                    "a commercial water service request is submitted",
                    kind="terminal_action",
                    terminal_action_family="request",
                )
            ],
            snapshot,
        )
        == []
    )


def test_terminal_goal_record_does_not_take_literal_criteria_from_present_value() -> None:
    criteria = [_criterion("c0", "the confirmation number WTR-1842-DEMO is reported")]
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": _terminal_goal_payload()})

    assert grade_terminal_goal_record_criteria(criteria, snapshot) == []
    assert _satisfied_criterion_ids(grade_present_value_criteria(criteria, snapshot)) == {"c0"}


def _validation_classification_criterion(
    expected_classification: str | bool = "login_gated",
    *,
    key: str = "path_classification",
) -> CompletionCriterion:
    return _criterion(
        "c_validation",
        "The run classifies whether the path is login gated.",
        kind="validation_classification",
        classification_output_key=key,
        expected_classification=expected_classification,
    )


def test_validation_classification_grader_credits_matching_string_value() -> None:
    criterion = _validation_classification_criterion("login_gated")
    snapshot = RunEvidenceSnapshot(block_outputs={"classify_path": {"path_classification": "login_gated"}})

    assert grade_validation_classification_criteria([criterion], snapshot) == [
        CriterionVerdict(
            criterion_id="c_validation",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:classify_path.path_classification",
            output_path="path_classification",
            grounding_mode="exact_value",
            has_exact_value=True,
        )
    ]


def test_validation_classification_grader_credits_matching_boolean_value() -> None:
    criterion = _validation_classification_criterion(True, key="login_gated")
    snapshot = RunEvidenceSnapshot(block_outputs={"classify_path": {"login_gated": True}})

    assert _satisfied_criterion_ids(grade_validation_classification_criteria([criterion], snapshot)) == {"c_validation"}


def test_validation_classification_grader_credits_repeated_matching_boolean_values() -> None:
    criterion = _validation_classification_criterion(True, key="login_only")
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "classify_path": {"login_only": True},
            "classify_path_output": {"login_only": True},
        }
    )

    assert grade_validation_classification_criteria([criterion], snapshot) == [
        CriterionVerdict(
            criterion_id="c_validation",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:classify_path.login_only",
            output_path="login_only",
            grounding_mode="exact_value",
            has_exact_value=True,
        )
    ]


@pytest.mark.parametrize(
    "criterion",
    [
        _criterion(
            "c_validation",
            "The run classifies whether the path is login gated.",
            kind="validation_classification",
            expected_classification="login_gated",
        ),
        _criterion(
            "c_validation",
            "The run classifies whether the path is login gated.",
            kind="validation_classification",
            classification_output_key="path_classification",
        ),
    ],
)
def test_validation_classification_grader_fails_closed_for_incomplete_contract(
    criterion: CompletionCriterion,
) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"classify_path": {"path_classification": "login_gated"}})

    verdicts = grade_validation_classification_criteria([criterion], snapshot)

    assert len(verdicts) == 1
    assert verdicts[0].criterion_id == "c_validation"
    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "no_evidence"
    assert verdicts[0].missing_evidence == "incomplete typed classification contract"


@pytest.mark.parametrize(
    ("payload", "reason_code"),
    [
        ({}, "no_evidence"),
        ({"path_classification": None}, "no_evidence"),
        ({"path_classification": ""}, "no_evidence"),
        ({"path_classification": []}, "no_evidence"),
        ({"path_classification": {}}, "no_evidence"),
        ({"path_classification": "public"}, "evidence_contradicts"),
        ({"path_classification": ["login_gated"]}, "evidence_contradicts"),
        ({"path_classification": True}, "evidence_contradicts"),
        ({"success": True}, "no_evidence"),
        ({"evidence_text": "The path is login_gated."}, "no_evidence"),
    ],
)
def test_validation_classification_grader_fails_closed_for_non_matching_values(
    payload: Any,
    reason_code: str,
) -> None:
    criterion = _validation_classification_criterion("login_gated")
    snapshot = RunEvidenceSnapshot(block_outputs={"classify_path": payload})

    verdicts = grade_validation_classification_criteria([criterion], snapshot)

    assert len(verdicts) == 1
    assert verdicts[0].criterion_id == "c_validation"
    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == reason_code


def test_validation_classification_grader_credits_repeated_matching_string_values() -> None:
    criterion = _validation_classification_criterion("login_gated")
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "classify_path_a": {"path_classification": "login_gated"},
            "classify_path_b": {"path_classification": "login_gated"},
        }
    )

    verdicts = grade_validation_classification_criteria([criterion], snapshot)

    assert len(verdicts) == 1
    assert verdicts[0] == CriterionVerdict(
        criterion_id="c_validation",
        state="satisfied",
        reason_code="evidence_confirms",
        evidence_ref="block_outputs:classify_path_a.path_classification",
        output_path="path_classification",
        grounding_mode="exact_value",
        has_exact_value=True,
    )


def test_validation_classification_grader_fails_closed_for_conflicting_candidates() -> None:
    criterion = _validation_classification_criterion("login_gated")
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "classify_path_a": {"path_classification": "login_gated"},
            "classify_path_b": {"path_classification": "public"},
        }
    )

    verdicts = grade_validation_classification_criteria([criterion], snapshot)

    assert len(verdicts) == 1
    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"


@pytest.mark.parametrize(
    "mixed_value",
    [
        None,
        "",
        [],
        {},
        "true",
        False,
    ],
)
def test_validation_classification_grader_fails_closed_for_mixed_repeated_boolean_candidates(
    mixed_value: Any,
) -> None:
    criterion = _validation_classification_criterion(True, key="login_only")
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "classify_path": {"login_only": True},
            "classify_path_output": {"login_only": mixed_value},
        }
    )

    verdicts = grade_validation_classification_criteria([criterion], snapshot)

    assert len(verdicts) == 1
    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"


def test_validation_classification_grader_rejects_same_named_scalar_block_label() -> None:
    criterion = _validation_classification_criterion("login_gated")
    snapshot = RunEvidenceSnapshot(block_outputs={"path_classification": "login_gated"})

    verdicts = grade_validation_classification_criteria([criterion], snapshot)

    assert len(verdicts) == 1
    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "no_evidence"


def test_validation_classification_is_not_requested_output_criterion() -> None:
    criterion = _criterion(
        "c_validation",
        "The run classifies whether the path is login gated.",
        kind="validation_classification",
        output_path="output.path_classification",
        expected_output_value="login_gated",
    )

    requested, remaining = split_requested_output_criteria([criterion])

    assert requested == []
    assert remaining == [criterion]


def test_validation_classification_grader_does_not_cross_credit_outcome_or_terminal_action() -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"classify_path": {"path_classification": "login_gated"}})

    assert (
        grade_validation_classification_criteria(
            [
                _criterion(
                    "c_outcome",
                    "The run classifies whether the path is login gated.",
                    classification_output_key="path_classification",
                    expected_classification="login_gated",
                ),
                _criterion(
                    "c_terminal",
                    "The run classifies whether the path is login gated.",
                    kind="terminal_action",
                    terminal_action_family="request",
                    classification_output_key="path_classification",
                    expected_classification="login_gated",
                ),
            ],
            snapshot,
        )
        == []
    )


def test_validation_classification_grader_repeats_same_verdict_for_same_output() -> None:
    criterion = _validation_classification_criterion("login_gated")
    snapshot = RunEvidenceSnapshot(block_outputs={"classify_path": {"path_classification": "login_gated"}})

    first = grade_validation_classification_criteria([criterion], snapshot)
    second = grade_validation_classification_criteria([criterion], snapshot)

    assert first == second


def test_fallback_floor_accepts_validation_review_evidence() -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_request": _validation_review_payload()})

    verdicts = grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot)

    assert verdicts == [
        CriterionVerdict(
            criterion_id="__copilot_fallback_floor__run",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:submit_request",
        )
    ]


def test_fallback_floor_accepts_generated_validation_review_fields_evidence() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={"validate_business_start_service": _generated_validation_review_payload()}
    )

    verdicts = grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot)

    assert verdicts == [
        CriterionVerdict(
            criterion_id="__copilot_fallback_floor__run",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:validate_business_start_service",
        )
    ]


def test_fallback_floor_accepts_live_validation_review_output_parameter_shape() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={"validate_business_start_service_review_output": _live_validation_review_output_payload()}
    )

    verdicts = grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot)

    assert verdicts == [
        CriterionVerdict(
            criterion_id="__copilot_fallback_floor__run",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:validate_business_start_service_review_output",
        )
    ]


def test_fallback_floor_accepts_live_validation_review_submit_controls_shape_without_url() -> None:
    payload = _live_validation_review_output_payload(final_submit_controls_present=None)
    payload["submit_controls_visible"] = ["Submit Request", "Back"]
    snapshot = RunEvidenceSnapshot(block_outputs={"validate_business_start_service_review_output": payload})

    verdicts = grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot)

    assert verdicts == [
        CriterionVerdict(
            criterion_id="__copilot_fallback_floor__run",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:validate_business_start_service_review_output",
        )
    ]


def test_fallback_floor_rejects_live_validation_review_submit_controls_after_final_click() -> None:
    payload = _live_validation_review_output_payload(
        final_submit_controls_present=None,
        submit_finalize_control_clicked=True,
    )
    payload["submit_controls_visible"] = ["Submit Request", "Back"]
    snapshot = RunEvidenceSnapshot(block_outputs={"validate_business_start_service_review_output": payload})

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_live_validation_review_back_only_submit_controls() -> None:
    payload = _live_validation_review_output_payload(final_submit_controls_present=None)
    payload["submit_controls_visible"] = ["Back"]
    snapshot = RunEvidenceSnapshot(block_outputs={"validate_business_start_service_review_output": payload})

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_observed_end_state_url_without_review_contract() -> None:
    snapshot = RunEvidenceSnapshot(
        current_url="http://localhost:8900/utility_services/peach_electric/",
        page_title="Peach Electric - Start Service",
    )

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_page_title_without_observed_end_state_url() -> None:
    snapshot = RunEvidenceSnapshot(page_title="Start Service - Review")

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


@pytest.mark.parametrize(
    "snapshot",
    [
        RunEvidenceSnapshot(current_url="https://example.test/review", failed_block_labels=["submit_request"]),
        RunEvidenceSnapshot(current_url="https://example.test/review", failure_classes=["ActionFailed"]),
    ],
)
def test_fallback_floor_rejects_observed_end_state_url_with_typed_failure_evidence(
    snapshot: RunEvidenceSnapshot,
) -> None:
    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_generated_validation_review_after_final_click() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "validate_business_start_service": _generated_validation_review_payload(
                submit_or_finalize_clicked=True,
                terminal_summary="Pre-submit review reached: True; submit/finalize control clicked: True.",
            )
        }
    )

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_live_validation_review_confirmation_page() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "validate_business_start_service_review_output": _live_validation_review_output_payload(
                confirmation_page_visible=True,
            )
        }
    )

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_generated_validation_review_confirmation_page() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "validate_business_start_service": _generated_validation_review_payload(
                confirmation_page_visible=True,
            )
        }
    )

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_bare_all_checks_passed() -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_request": {"all_checks_passed": True}})

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_normal_submit_review_without_validation_only_marker() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "submit_request": _validation_review_payload(
                validation_only=None,
                submit_mode=None,
            )
        }
    )

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


@pytest.mark.parametrize("marker", ["not_validation_only", "validation_only_disabled", "previous_validation_only"])
def test_fallback_floor_rejects_validation_only_marker_prefix_suffix_matches(marker: str) -> None:
    payload = _validation_review_payload(validation_only=None)
    payload[marker] = True
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_request": payload})

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_boolean_only_validation_review_evidence() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "validate_business_start_service_review": _live_validation_review_output_payload(
                all_validations_passed=None,
                review_values=None,
            )
        }
    )

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_review_text_without_structured_review_page() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "submit_request": _validation_review_payload(
                review_page_visible=False,
                evidence_text=(
                    "No review page was necessary, but values 1234 Sample Utility Way, Testville, CA 94016, "
                    "2026-06-22, and EXAMPLE REALTY LABS INC were checked."
                ),
            )
        }
    )

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_review_values_without_corroborating_text() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "submit_request": _validation_review_payload(
                evidence_text="Visible Review page showed safe values and no final click."
            )
        }
    )

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_validation_review_values_contradicting_requested_literals() -> None:
    criteria = [
        *build_classifier_fallback_floor([]),
        _criterion(
            "c_requested_values",
            'Review shows service address "1234 Sample Utility Way, Testville, CA 94016" and start date "2026-06-22".',
        ),
    ]
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "submit_request": _validation_review_payload(
                review_values={
                    "visible_service_address": "100 Wrong Way, Atlanta, GA 30318",
                    "visible_requested_start_date": "2026-07-01",
                    "visible_account_holder": "EXAMPLE REALTY LABS INC",
                },
                evidence_text=(
                    "Visible Review page showed service address 100 Wrong Way, Atlanta, GA 30318, "
                    "start date 2026-07-01, and account holder EXAMPLE REALTY LABS INC. "
                    "No Submit Request or final confirmation control was clicked."
                ),
            )
        }
    )

    assert grade_fallback_floor_reached_end_state_criteria(criteria, snapshot) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"error": "review mismatch"},
        {"failure_reason": "submit disabled"},
        {"status": "failed"},
        {"all_checks_passed": False},
        {"submitted_request": True},
        {"confirmation_page_visible": True},
        {"submit_or_finalize_clicked": True},
    ],
)
def test_fallback_floor_rejects_validation_review_negative_guards(overrides: dict[str, Any]) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_request": _validation_review_payload(**overrides)})

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_validation_review_structured_contradiction() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "submit_request": _validation_review_payload(
                items=[{"item_name": "Service Review Active", "status": "Expired"}],
                overall_status="Expired",
            )
        }
    )

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_fallback_floor_rejects_validation_review_nested_under_failed_parent() -> None:
    payload = {"status": "failed", "error": "submit blocked", "validate_review_output": _validation_review_payload()}
    snapshot = RunEvidenceSnapshot(block_outputs={"validate_review": payload})

    assert grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot) == []


def test_structured_record_goal_content_remains_strict_for_flat_terminal_payload() -> None:
    assert structured_record_has_goal_content(_terminal_goal_payload()) is False


def test_structured_record_partial_matches_do_not_combine_across_blocks() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "identity_block": _record_payload(items=[], overall_status=None),
            "status_block": {
                "items": [{"item_name": "Sample Practice", "address": "100 Main St", "status": "Active"}],
                "overall_status": "Active",
            },
        }
    )

    criteria = _structured_record_criteria()
    verdicts = grade_structured_record_criteria(criteria, snapshot)
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=[criterion.id for criterion in criteria],
        verdicts=verdicts,
    )

    assert _satisfied_criterion_ids(verdicts) < _STRUCTURED_RECORD_CRITERION_IDS
    assert result.is_fully_satisfied() is False


@pytest.mark.asyncio
async def test_evaluate_no_handler_or_no_criteria_is_unavailable() -> None:
    snapshot = RunEvidenceSnapshot(current_url="https://example.com")
    assert (await evaluate_completion_criteria([_criterion("c0", "x")], snapshot, None)).status == "unavailable"
    assert (await evaluate_completion_criteria([], snapshot, lambda **_: {})).status == "unavailable"


@pytest.mark.asyncio
async def test_evaluate_handler_exception_is_unavailable() -> None:
    async def boom(**_: object) -> object:
        raise RuntimeError("llm down")

    snapshot = RunEvidenceSnapshot(current_url="https://example.com")
    result = await evaluate_completion_criteria([_criterion("c0", "x")], snapshot, boom)
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_evaluate_uses_completion_judge_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS", 0.01)

    async def handler(**_: object) -> dict[str, object]:
        await asyncio.sleep(0.05)
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    snapshot = RunEvidenceSnapshot(current_url="https://example.com/done")
    result = await evaluate_completion_criteria([_criterion("c0", "done page visible")], snapshot, handler)

    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_evaluate_happy_path_returns_evaluated() -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    snapshot = RunEvidenceSnapshot(block_outputs={"confirm": {"count": 1}})
    result = await evaluate_completion_criteria([_criterion("c0", "item in cart")], snapshot, handler)
    assert result.status == "evaluated"
    assert result.is_fully_satisfied() is True


def test_snapshot_has_evidence() -> None:
    assert RunEvidenceSnapshot().has_evidence() is False
    assert RunEvidenceSnapshot(run_terminal_status="failed").has_evidence() is False
    assert RunEvidenceSnapshot(current_url="https://example.com").has_evidence() is True
    assert RunEvidenceSnapshot(block_outputs={"a": 1}).has_evidence() is True
    assert RunEvidenceSnapshot(failed_block_labels=["extract"]).has_evidence() is True
    assert RunEvidenceSnapshot(failure_classes=["SyntaxError"]).has_evidence() is True
    assert RunEvidenceSnapshot(failure_reasons=["SyntaxError: bad generated code"]).has_evidence() is True
    assert RunEvidenceSnapshot(page_evidence={"visible_text_excerpt": "cart item PART-001-TEST"}).has_evidence() is True


def test_snapshot_renders_bounded_page_evidence() -> None:
    long_visible_text = "Footer recommendation " * 200
    snapshot = RunEvidenceSnapshot(
        workflow_run_id="wr_active",
        current_url="https://example.com/cart",
        page_title="Cart",
        page_evidence={
            "visible_text_excerpt": long_visible_text,
            "visual_evidence_summary": "Screenshot shows the cart with TESTBRAND PART-001-TEST quantity 1.",
            "screenshot_used": True,
            "evidence_sources": ["dom_html", "screenshot", "vision_summary"],
            "forms": [{"id": "checkout", "submit_controls": [{"text": "Checkout"}]}],
            "result_containers": [{"selector": "#cart"}],
            "anti_bot_indicators": [],
            "raw_html": "<div>must not render</div>",
        },
    )

    rendered = snapshot.render_prompt_block()

    assert "page_evidence:" in rendered
    assert "visible_text_excerpt" in rendered
    assert "visual_evidence_summary" in rendered
    assert "screenshot" in rendered
    assert "PART-001-TEST" in rendered
    assert rendered.index("visual_evidence_summary") < rendered.index("visible_text_excerpt")
    assert "raw_html" not in rendered


def test_snapshot_renders_failed_run_artifact_health_signal() -> None:
    snapshot = RunEvidenceSnapshot(
        workflow_run_id="wr_failed",
        block_outputs={"extract_results": {"extracted_information": ["goal text"]}},
        current_url="https://example.com/results",
        run_terminal_status="failed",
        failed_block_labels=["extract_results"],
        failure_classes=["SyntaxError"],
        failure_reasons=["Page.evaluate: SyntaxError: Unexpected token ')'"],
    )

    rendered = snapshot.render_prompt_block()

    assert "run_terminal_status: failed" in rendered
    assert "failed_block_labels: extract_results" in rendered
    assert "failure_classes: SyntaxError" in rendered
    assert "Page.evaluate: SyntaxError" in rendered


def test_active_run_terminal_visual_fallback_uses_screenshot_when_missing() -> None:
    assert (
        _active_run_terminal_evidence_needs_visual_fallback(
            {
                "visible_text_excerpt": "",
                "forms": [],
                "navigation_targets": [],
                "result_containers": [],
                "evidence_confidence": 0.1,
            }
        )
        is True
    )
    assert (
        _active_run_terminal_evidence_needs_visual_fallback(
            {
                "visible_text_excerpt": "Cart TESTBRAND PART-001-TEST quantity 1",
                "forms": [],
                "navigation_targets": [],
                "result_containers": [],
                "evidence_confidence": 0.1,
            }
        )
        is True
    )
    assert (
        _active_run_terminal_evidence_needs_visual_fallback(
            {
                "visible_text_excerpt": "Cart contains item PART-001-TEST with quantity 1. " * 4,
                "forms": [],
                "navigation_targets": [],
                "result_containers": [],
                "evidence_confidence": 0.1,
            }
        )
        is True
    )
    assert (
        _active_run_terminal_evidence_needs_visual_fallback(
            {
                "visible_text_excerpt": "",
                "forms": [],
                "navigation_targets": [],
                "result_containers": [{"selector": "#cart"}],
                "evidence_confidence": 0.3,
            }
        )
        is True
    )
    assert (
        _active_run_terminal_evidence_needs_visual_fallback(
            {
                "visible_text_excerpt": "Cart TESTBRAND PART-001-TEST quantity 1",
                "result_containers": [{"selector": "#cart"}],
                "screenshot_used": True,
            }
        )
        is False
    )


def test_visual_prompt_requests_outcome_relevant_page_state() -> None:
    prompt = _composition_visual_prompt({"current_url": "https://example.com/cart", "page_title": "Cart"})

    assert "cart items" in prompt
    assert "visible identifiers" in prompt
    assert "quantities" in prompt
    assert "human-verification" in prompt


@pytest.mark.asyncio
async def test_active_run_terminal_monitor_skips_requested_output_only_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {}

    async def fake_completion_verification_handler(_ctx: object) -> object:
        return handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._completion_verification_handler",
        fake_completion_verification_handler,
    )
    ctx = _run_ctx()
    ctx.browser_session_id = "bs_1"
    ctx.discovery_mcp_server = object()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[_criterion("c0", "return record id", output_path="output.record_id")]
    )

    assert await _active_run_terminal_monitor_enabled(ctx) is False


@pytest.mark.asyncio
async def test_active_run_terminal_monitor_keeps_mixed_requested_output_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {}

    async def fake_completion_verification_handler(_ctx: object) -> object:
        return handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._completion_verification_handler",
        fake_completion_verification_handler,
    )
    ctx = _run_ctx()
    ctx.browser_session_id = "bs_1"
    ctx.discovery_mcp_server = object()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion("c0", "return record id", output_path="output.record_id"),
            _criterion("c1", "cart page is visible"),
        ]
    )

    assert await _active_run_terminal_monitor_enabled(ctx) is True


@pytest.mark.asyncio
async def test_active_run_terminal_monitor_skips_requested_output_corroborator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {}

    async def fake_completion_verification_handler(_ctx: object) -> object:
        return handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._completion_verification_handler",
        fake_completion_verification_handler,
    )
    ctx = _run_ctx()
    ctx.browser_session_id = "bs_1"
    ctx.discovery_mcp_server = object()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion("c_requested", "return quotes", output_path="output.quotes"),
            _criterion(
                "c_quotes",
                "The run extracts the first 3 quotes and authors into the returned quotes JSON.",
                requested_output_corroborator=True,
            ),
        ]
    )

    assert await _active_run_terminal_monitor_enabled(ctx) is False


@pytest.mark.asyncio
async def test_active_run_terminal_monitor_keeps_unmarked_fallback_floor_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {}

    async def fake_completion_verification_handler(_ctx: object) -> object:
        return handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._completion_verification_handler",
        fake_completion_verification_handler,
    )
    ctx = _run_ctx()
    ctx.browser_session_id = "bs_1"
    ctx.discovery_mcp_server = object()
    ctx.request_policy = RequestPolicy(completion_criteria=build_classifier_fallback_floor([]))

    assert await _active_run_terminal_monitor_enabled(ctx) is True


@pytest.mark.asyncio
async def test_active_run_terminal_monitor_skips_marked_fallback_floor_corroborator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {}

    async def fake_completion_verification_handler(_ctx: object) -> object:
        return handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._completion_verification_handler",
        fake_completion_verification_handler,
    )
    ctx = _run_ctx()
    ctx.browser_session_id = "bs_1"
    ctx.discovery_mcp_server = object()
    floor = replace(build_classifier_fallback_floor([])[0], requested_output_corroborator=True)
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion("c_requested", "return record id", output_path="output.record_id"),
            floor,
        ]
    )

    assert await _active_run_terminal_monitor_enabled(ctx) is False


@pytest.mark.asyncio
async def test_active_run_terminal_monitor_keeps_terminal_action_criteria_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {}

    async def fake_completion_verification_handler(_ctx: object) -> object:
        return handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._completion_verification_handler",
        fake_completion_verification_handler,
    )
    ctx = _run_ctx()
    ctx.browser_session_id = "bs_1"
    ctx.discovery_mcp_server = object()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "service request is submitted",
                kind="terminal_action",
                terminal_action_family="request",
            )
        ]
    )

    assert await _active_run_terminal_monitor_enabled(ctx) is True


def test_summarize_unsatisfied_lists_unmet_outcomes() -> None:
    criteria = [_criterion("c0", "item in cart"), _criterion("c1", "added exactly once")]
    result = _evaluated(("c0", True), ("c1", False))
    assert summarize_unsatisfied_outcomes(result, criteria) == "added exactly once"


def test_parse_assigns_deterministic_ids_and_dedupes() -> None:
    raw = [
        {"outcome": "Item in cart", "id": "model-supplied-ignored"},
        {"outcome": "item in cart"},
        {"outcome": "", "implicit": True},
        {"outcome": "Added exactly once", "implicit": True},
    ]
    criteria = _parse_completion_criteria(raw)
    assert [c.id for c in criteria] == ["c0", "c1"]
    assert [c.outcome for c in criteria] == ["Item in cart", "Added exactly once"]
    assert criteria[1].implicit is True


def test_parse_caps_count() -> None:
    raw = [{"outcome": f"outcome {i}"} for i in range(20)]
    assert len(_parse_completion_criteria(raw)) == 8


def _verification_satisfaction_ctx(
    completion_verification: CompletionVerificationResult | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        completion_verification_result=completion_verification,
        last_artifact_health_blocker_reason=None,
        last_run_blocks_workflow_run_id=None,
        last_run_outcome=None,
    )


def test_verification_satisfaction_no_cvr_uses_prior_proxy() -> None:
    ctx = _verification_satisfaction_ctx()
    assert _verification_satisfaction(ctx, True, False, "completed", None) == (True, True)
    assert _verification_satisfaction(ctx, True, True, "completed", None) == (False, False)
    assert _verification_satisfaction(ctx, False, False, None, None) == (None, None)


def test_verification_satisfaction_evaluated_drives_contract_signal() -> None:
    satisfied = _evaluated(("c0", True))
    unsatisfied = _evaluated(("c0", False))
    assert _verification_satisfaction(
        _verification_satisfaction_ctx(satisfied), True, False, "completed", satisfied
    ) == (
        True,
        True,
    )
    _, contract = _verification_satisfaction(
        _verification_satisfaction_ctx(unsatisfied), True, False, "completed", unsatisfied
    )
    assert contract is False


def test_verification_satisfaction_unavailable_fails_closed() -> None:
    unavailable = CompletionVerificationResult("unavailable")
    _, contract = _verification_satisfaction(
        _verification_satisfaction_ctx(unavailable), True, False, "completed", unavailable
    )
    assert contract is False


def _satisfied_contract() -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="run_blocks_and_collect_debug"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=RepairNextAction.NO_CHANGE),
        verification_result=VerificationResult(user_goal_satisfied=True, completion_contract_satisfied=True),
    )


def _gate_ctx() -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        user_message="do A then B",
    )
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = True
    ctx.latest_diagnosis_repair_contract = _satisfied_contract()
    ctx.last_update_block_count = 1
    ctx.request_policy = RequestPolicy(completion_contract="done when B happens")
    return ctx


def test_gate_bypasses_heuristic_only_on_evaluated_verdict() -> None:
    bypass = _gate_ctx()
    bypass.completion_verification_result = _evaluated(("c0", True))
    assert verified_goal_satisfied_context(bypass) is True

    retained = _gate_ctx()
    retained.completion_verification_result = None
    assert verified_goal_satisfied_context(retained) is False


def test_gate_withholds_on_evaluated_unconfirmed_even_with_clean_run_status() -> None:
    # The judge verdict is authoritative in both directions: an evaluated-but-
    # unconfirmed verdict withholds even when run-status latches and the diagnosis
    # contract would otherwise pass -- recognition must weigh the verdict, not just
    # whether the judge ran.
    ctx = _gate_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True), ("c1", False))
    assert verified_goal_satisfied_context(ctx) is False


def test_completion_contract_not_violated() -> None:
    ctx = SimpleNamespace(completion_verification_result=None, last_artifact_health_blocker_reason=None)
    assert _completion_contract_not_violated(ctx) is True  # type: ignore[arg-type]
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert _completion_contract_not_violated(ctx) is True  # type: ignore[arg-type]
    ctx.completion_verification_result = _evaluated(("c0", False))
    assert _completion_contract_not_violated(ctx) is False  # type: ignore[arg-type]


def test_outcome_unverified_reason_for_unsatisfied_and_unavailable() -> None:
    policy = RequestPolicy(completion_criteria=[_criterion("c0", "item in cart")])
    ctx = SimpleNamespace(request_policy=policy)
    assert _outcome_unverified_reason(ctx, None) is None
    assert _outcome_unverified_reason(ctx, _evaluated(("c0", True))) is None
    unsatisfied = _outcome_unverified_reason(ctx, _evaluated(("c0", False)))
    assert unsatisfied is not None and "item in cart" in unsatisfied
    unavailable = _outcome_unverified_reason(ctx, CompletionVerificationResult("unavailable"))
    assert unavailable is not None and "could not be verified" in unavailable


def test_outcome_unverified_reason_uses_typed_missing_evidence_not_confirmation_block() -> None:
    policy = RequestPolicy(completion_criteria=[_criterion("c0", "first paragraph text is reported")])
    verification = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="unsatisfied",
                reason_code="no_evidence",
                missing_evidence="block output containing the full first paragraph text",
            )
        ],
    )
    ctx = SimpleNamespace(request_policy=policy)

    reason = _outcome_unverified_reason(ctx, verification)

    assert reason is not None
    assert "block output containing the full first paragraph text" in reason
    assert "confirm" not in reason.lower()
    assert "confirmation" not in reason.lower()
    assert "boolean" not in reason.lower()

    ctx.completion_criteria_turn_state = SimpleNamespace(known_good_yaml_available=True)
    known_good_reason = _outcome_unverified_reason(ctx, verification)
    assert known_good_reason is not None
    assert "previously tested revision" in known_good_reason
    assert "prefer restoring that revision" in known_good_reason


def test_outcome_unverified_reason_guides_fallback_floor_review_output_contract() -> None:
    policy = RequestPolicy(
        completion_criteria=build_classifier_fallback_floor([]),
        classifier_status="fallback",
    )
    floor_id = policy.completion_criteria[0].id
    verification = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=[floor_id],
        verdicts=[
            CriterionVerdict(
                criterion_id=floor_id,
                state="unsatisfied",
                reason_code="no_evidence",
                missing_evidence="run output did not include evidence for this criterion",
            )
        ],
    )
    ctx = SimpleNamespace(request_policy=policy)

    reason = _outcome_unverified_reason(ctx, verification)

    assert reason is not None
    assert "review_values" in reason
    assert "review_fields" in reason
    assert "evidence_text" in reason
    assert "validation_only" in reason
    assert "submit_mode" in reason
    assert "visible Review-page label/value strings" in reason
    assert "do not click Submit/Finalize" in reason


def test_outcome_unverified_reason_excludes_structurally_abstained_contingent_missing_evidence() -> None:
    policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "A provider blocker is reported to the user.",
                contingent_on="the provider site blocks online submission",
                contingent_antecedent_output_path="output.blocker",
            ),
            _criterion("c1", "The confirmation number is extracted."),
        ]
    )
    verification = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1", "c2"],
        contingent_criterion_ids=["c0"],
        contingent_antecedent_output_path_by_criterion_id={"c0": "output.blocker"},
        structural_unfired_criterion_ids=["c0"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="unsatisfied",
                reason_code="evidence_contradicts",
                missing_evidence="blocker report",
            ),
            CriterionVerdict(
                criterion_id="c1",
                state="unsatisfied",
                reason_code="no_evidence",
                missing_evidence="confirmation output",
            ),
            CriterionVerdict(criterion_id="c2", state="satisfied", reason_code="evidence_confirms"),
        ],
    )
    ctx = SimpleNamespace(request_policy=policy)

    reason = _outcome_unverified_reason(ctx, verification)

    assert reason is not None
    assert "confirmation output" in reason
    assert "confirmation number" in reason
    assert "provider blocker" not in reason
    assert "blocker report" not in reason

    missing_metadata = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_missing"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_missing",
                state="unknown",
                reason_code="unknown",
                missing_evidence="judge did not return a verdict for this criterion",
            )
        ],
    )
    missing_metadata_reason = _outcome_unverified_reason(ctx, missing_metadata)
    assert missing_metadata_reason is not None
    assert "c_missing: judge did not return a verdict for this criterion" in missing_metadata_reason


def _clean_success_result() -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_x",
            "overall_status": "completed",
            "executed_block_labels": ["confirm"],
            "current_url": "https://example.com/cart",
            "blocks": [
                {
                    "label": "confirm",
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {"extracted_information": {"items": ["a"]}},
                }
            ],
        },
    }


def _structured_record_top_level_output_result() -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_structured_record",
            "overall_status": "completed",
            "executed_block_labels": ["extract_record_status_record"],
            "current_url": "https://structured_record.test/entity-details",
            "blocks": [
                {
                    "label": "extract_record_status_record",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {"extracted_information": []},
                }
            ],
            "output": {
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
                "extract_record_status_record_output": _record_payload(found=True, entity_found=None),
                "extracted_information": [],
            },
        },
    }


def _terminal_goal_output_result(**payload_overrides: Any) -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_terminal_goal",
            "overall_status": "completed",
            "executed_block_labels": ["submit_water_request"],
            "current_url": "https://example.test/confirmation",
            "blocks": [
                {
                    "label": "submit_water_request",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": _terminal_goal_payload(**payload_overrides),
                }
            ],
        },
    }


def _validation_review_output_result(**payload_overrides: Any) -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_validation_review",
            "overall_status": "completed",
            "executed_block_labels": ["submit_request"],
            "current_url": "https://example.test/review",
            "page_title": "Start Service - Review",
            "blocks": [
                {
                    "label": "submit_request",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": _validation_review_payload(**payload_overrides),
                }
            ],
        },
    }


def _validation_classification_output_result(value: Any) -> dict:
    return _validation_classification_payload_result({"path_classification": value})


def _validation_classification_payload_result(payload: dict[str, Any]) -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_validation_classification",
            "overall_status": "completed",
            "executed_block_labels": ["classify_path"],
            "current_url": "https://example.test/login",
            "blocks": [
                {
                    "label": "classify_path",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": payload,
                }
            ],
        },
    }


def _live_validation_review_output_result(**payload_overrides: Any) -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_validation_review",
            "overall_status": "completed",
            "executed_block_labels": ["validate_business_start_service_review"],
            "current_url": "https://example.test/review",
            "page_title": "Start Service - Review",
            "blocks": [],
            "output": {
                "extracted_information": [],
                "validate_business_start_service_review_output": _live_validation_review_output_payload(
                    **payload_overrides
                ),
            },
        },
    }


def _requested_output_result(output: Any) -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_requested_output",
            "overall_status": "completed",
            "executed_block_labels": ["extract_profile"],
            "current_url": "https://example.test/profile",
            "blocks": [
                {
                    "label": "extract_profile",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": output,
                }
            ],
        },
    }


def _metadata_for_requested_paths(*paths: str) -> dict[str, Any]:
    return {
        label: {
            "claimed_outcomes": [{"goal_value_paths": list(paths)}],
            "terminal_verifier_expectations": [{"goal_value_paths": list(paths)}],
        }
        for label in (
            "extract_profile",
            "utility_citrus_turn_on",
            "utility_peach_gas_quickconnect",
        )
    }


def _admit_code_artifact_metadata_for_test(
    ctx: CopilotContext,
    *,
    block_label: str,
    completion_criteria: list[CompletionCriterion],
) -> None:
    ctx.request_policy = RequestPolicy(completion_contract_status="present", completion_criteria=completion_criteria)
    workflow_yaml = textwrap.dedent(
        f"""
        workflow_definition:
          blocks:
            - block_type: code
              label: {block_label}
              code: |
                return {{"document_name": "Selected Document.pdf"}}
        """
    ).strip()
    metadata = {
        "block_label": block_label,
        "declared_goal": "Return the selected highest-priority document name.",
        "claimed_outcomes": [
            {
                "id": "claim:document_name",
                "scope": "outcome",
                "text": "The selected document name is returned.",
                "status": "observed_not_verified",
                "depends_on": ["dependency:page"],
                "covered_criteria": ["criterion:document_name"],
                "goal_value_paths": ["document_name"],
                "observation_refs": ["obs1"],
            }
        ],
        "page_dependencies": [
            {
                "id": "dependency:page",
                "scope": "page",
                "status": "observed_not_verified",
                "observation_refs": ["obs1"],
            }
        ],
        "completion_criteria": [
            {
                "id": "criterion:document_name",
                "text": "The returned document_name is the highest-priority selected document.",
                "level": "terminal",
                "output_path": "output.document_name",
                "requested_output_evidence_source": "independent_run_evidence",
            }
        ],
        "terminal_verifier_expectations": [
            {
                "id": "expectation:document_name",
                "text": "Verifier should inspect the returned document name.",
                "criteria_ids": ["criterion:document_name"],
                "goal_value_paths": ["document_name"],
            }
        ],
        "observation_refs": [
            {
                "observation_ref": "obs1",
                "dependency_id": "dependency:page",
                "status": "observed_not_verified",
                "source_tool": "inspect_page_for_composition",
            }
        ],
    }
    normalized, error = _normalize_code_artifact_metadata([metadata], workflow_yaml)
    assert error is None
    ctx.code_artifact_metadata = normalized
    _apply_code_artifact_requested_output_evidence_sources(ctx, normalized)


def _run_ctx() -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        user_message="add item to cart and confirm",
    )
    ctx.request_policy = RequestPolicy(completion_criteria=[_criterion("c0", "item in cart")])
    return ctx


def _ctx_with_blocks(*block_types: str) -> CopilotContext:
    ctx = _run_ctx()
    blocks = [SimpleNamespace(block_type=bt, label=f"b{i}") for i, bt in enumerate(block_types)]
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=blocks))
    ctx.verified_prefix_labels = [b.label for b in blocks]
    return ctx


def _set_workflow_labels(ctx: CopilotContext, *labels: str) -> None:
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(blocks=[SimpleNamespace(label=label) for label in labels])
    )


def _contradicted(cid: str) -> CompletionVerificationResult:
    verdict = CriterionVerdict(criterion_id=cid, state="unsatisfied", reason_code="evidence_contradicts")
    return CompletionVerificationResult(status="evaluated", criterion_ids=[cid], verdicts=[verdict])


def test_record_run_blocks_downgrades_when_confirmation_block_present_but_unmet() -> None:
    ctx = _ctx_with_blocks("extraction")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", False)))
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_good_workflow is None
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert "item in cart" in (ctx.last_test_failure_reason or "")


def test_tool_visible_result_fails_when_confirmation_block_outcome_unmet() -> None:
    ctx = _ctx_with_blocks("extraction")
    result = _clean_success_result()
    verification = _evaluated(("c0", False))

    visible = _tool_visible_result_after_completion_verification(ctx, result, verification)

    assert visible["ok"] is False
    assert "item in cart" in visible["error"]
    assert result["ok"] is True
    assert visible["data"]["overall_status"] == "completed"
    assert visible["data"]["completion_verification"]["fully_satisfied"] is False
    assert visible["data"]["completion_verification"]["missing_evidence"]
    assert visible["data"]["failure_categories"][0]["category"] == "OUTCOME_UNVERIFIED"


def test_tool_visible_result_keeps_mid_build_run_visible_success() -> None:
    ctx = _ctx_with_blocks("goto_url", "navigation")

    visible = _tool_visible_result_after_completion_verification(
        ctx,
        _clean_success_result(),
        _evaluated(("c0", False)),
    )

    assert visible["ok"] is True


def test_tool_visible_result_keeps_committed_same_run_success_after_later_contradiction() -> None:
    ctx = _ctx_with_blocks("extraction")
    result = _clean_success_result()

    _record_run_blocks_result(ctx, result, completion_verification=_evaluated(("c0", True)))
    visible = _tool_visible_result_after_completion_verification(ctx, result, _contradicted("c0"))

    assert visible["ok"] is True
    assert visible is result
    assert "failure_categories" not in visible["data"]
    assert "completion_verification" not in visible["data"]


def test_tool_visible_result_downgrades_first_pass_contradiction_without_committed_outcome() -> None:
    ctx = _ctx_with_blocks("extraction")
    result = _clean_success_result()

    visible = _tool_visible_result_after_completion_verification(ctx, result, _contradicted("c0"))

    assert visible["ok"] is False
    assert visible["data"]["failure_categories"][0]["category"] == "OUTCOME_UNVERIFIED"


def test_record_run_blocks_keeps_building_on_mid_build_no_evidence() -> None:
    ctx = _ctx_with_blocks("goto_url", "navigation")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", False)))
    # A nav-only WIP that has not added a confirmation block yet must keep building,
    # not enter repair...
    assert ctx.last_test_suspicious_success is False
    # ...but terminal success and good-workflow promotion stay withheld because
    # the outcome is unverified.
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_good_workflow is None
    assert _completion_contract_not_violated(ctx) is False


def test_record_run_blocks_downgrades_on_contradiction_without_confirmation_block() -> None:
    ctx = _ctx_with_blocks("goto_url", "navigation")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_contradicted("c0"))
    assert ctx.last_test_suspicious_success is True
    assert ctx.last_full_workflow_test_ok is False


def test_record_run_blocks_demonstrated_when_lone_definition_abstention_with_confirmed_run() -> None:
    ctx = _ctx_with_blocks("extraction")
    verification = _mixed(
        CriterionVerdict(criterion_id="c0", state="unknown", reason_code="definition_parameters_absent"),
        CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms"),
    )

    recorded = _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=verification)

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_full_workflow_test_ok is True
    assert verified_goal_satisfied_context(ctx) is True


def test_record_run_blocks_keeps_clean_structural_abstention_as_built_unverified() -> None:
    ctx = _ctx_with_blocks("extraction")
    verification = _mixed(
        CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="structurally_abstained")
    )

    recorded = _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=verification)

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert recorded.reason_code is None
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None
    assert ctx.last_full_workflow_test_ok is True
    ctx.latest_diagnosis_repair_contract = DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=RepairNextAction.NO_CHANGE),
        verification_result=VerificationResult(user_goal_satisfied=False, completion_contract_satisfied=False),
    )
    assert verified_goal_satisfied_context(ctx) is False
    assert built_unverified_repair_inert_context(ctx) is True
    assert outcome_fully_verified(ctx) is False
    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None
    assert outcome.verdict == "not_authoritative"
    assert outcome.is_authoritative is False


def _degraded_delivered_result(*verdicts: CriterionVerdict) -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["__copilot_fallback_floor__run", *[verdict.criterion_id for verdict in verdicts]],
        verdicts=[
            CriterionVerdict(
                criterion_id="__copilot_fallback_floor__run",
                state="unsatisfied",
                reason_code="no_evidence",
            ),
            *verdicts,
        ],
        degraded_criterion_ids=["__copilot_fallback_floor__run"],
    )


def _observed_structural_abstention(
    criterion_id: str = "requested_output",
    *,
    evidence_source: str = "runtime_output",
    reason_code: str = "structurally_abstained",
    output_path: str = "output.document_name",
    grounding_mode: str | None = "missing",
) -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id=criterion_id,
        state="unsatisfied",
        reason_code=reason_code,
        evidence_ref="block_outputs:extract.document_name",
        output_path=output_path,
        grounding_mode=grounding_mode,
        evidence_source=evidence_source,
    )


def _delivered_terminal_state(
    result: CompletionVerificationResult,
    *,
    run_ok: bool = True,
    workflow_run_id: str | None = "wr_x",
    latest_workflow_run_id: str | None = "wr_x",
    artifact_health_blocked: bool = False,
    terminal_blocked: bool = False,
) -> DeliveredUnverifiedTerminalState | None:
    return degraded_contract_delivered_unverified_terminal_state(
        result,
        run_ok=run_ok,
        workflow_run_id=workflow_run_id,
        latest_workflow_run_id=latest_workflow_run_id,
        artifact_health_blocked=artifact_health_blocked,
        terminal_blocked=terminal_blocked,
    )


def test_degraded_delivered_unverified_terminal_state_allows_observed_runtime_output() -> None:
    terminal_state = _delivered_terminal_state(_degraded_delivered_result(_observed_structural_abstention()))

    assert terminal_state is not None
    assert [verdict.criterion_id for verdict in terminal_state.observed_verdicts] == ["requested_output"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"run_ok": False},
        {"workflow_run_id": "wr_old"},
        {"latest_workflow_run_id": "wr_old"},
        {"artifact_health_blocked": True},
        {"terminal_blocked": True},
    ],
)
def test_degraded_delivered_unverified_terminal_state_rejects_run_and_blocker_exclusions(
    kwargs: dict[str, bool | str | None],
) -> None:
    assert _delivered_terminal_state(_degraded_delivered_result(_observed_structural_abstention()), **kwargs) is None


@pytest.mark.parametrize(
    "verdict",
    [
        CriterionVerdict(
            criterion_id="requested_output",
            state="unsatisfied",
            reason_code="missing_exact_field",
            evidence_ref="block_outputs:extract.document_name",
            output_path="output.document_name",
            evidence_source="runtime_output",
        ),
        _observed_structural_abstention(output_path="output.evidence_text"),
        _observed_structural_abstention(grounding_mode="shape"),
        _observed_structural_abstention(evidence_source="independent_page_evidence"),
    ],
)
def test_degraded_delivered_unverified_terminal_state_rejects_non_value_output(verdict: CriterionVerdict) -> None:
    assert _delivered_terminal_state(_degraded_delivered_result(verdict)) is None


def test_degraded_delivered_unverified_terminal_state_is_not_verified_success() -> None:
    ctx = _ctx_with_blocks("extraction")
    result = _clean_success_result()
    result["data"]["blocks"][0]["label"] = "extract"
    result["data"]["blocks"][0]["extracted_data"] = {"document_name": "Resale Demand Package"}
    verification = _degraded_delivered_result(_observed_structural_abstention())

    recorded = _record_run_blocks_result(ctx, result, completion_verification=verification)

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert ctx.delivered_unverified_terminal is True
    assert ctx.delivered_unverified_observed_outputs == {"document_name": "Resale Demand Package"}
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind.value == "delivered_unverified"
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_test_suspicious_success is False


def _registered_output_result(value: dict[str, Any], block_type: str = "CODE") -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_x",
            "overall_status": "completed",
            "executed_block_labels": ["extract_bill"],
            "current_url": "https://example.com/account",
            "blocks": [
                {
                    "label": "extract_bill",
                    "block_type": block_type,
                    "status": "completed",
                    "extracted_data": {"bill_statement": value},
                }
            ],
            "registered_output_parameter_values": [
                {
                    "workflow_run_id": "wr_x",
                    "output_parameter_id": "op_bill",
                    "output_parameter_key": "bill_statement",
                    "block_label": "extract_bill",
                    "block_type": block_type,
                    "value": value,
                }
            ],
        },
    }


def _persisted_output_parameter_result(value: dict[str, Any], run_id: str = "wr_x") -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": run_id,
            "overall_status": "completed",
            "executed_block_labels": ["extract_bill"],
            "current_url": "https://example.com/account",
            "blocks": [],
            "workflow_run_output_parameters": [
                {
                    "workflow_run_id": run_id,
                    "output_parameter_id": "op_bill",
                    "output_parameter_key": "bill_statement",
                    "block_label": "extract_bill",
                    "block_type": "CODE",
                    "value": value,
                }
            ],
        },
    }


def test_zero_requested_output_criteria_credit_fires_only_with_payload() -> None:
    satisfied = _evaluated(("login", True))

    assert zero_requested_output_criteria_credit(satisfied, has_meaningful_registered_output=True) is True
    assert zero_requested_output_criteria_credit(satisfied, has_meaningful_registered_output=False) is False


def test_zero_requested_output_criteria_credit_ignored_when_criteria_formed() -> None:
    with_criteria = replace(_evaluated(("bill", True)), requested_output_criteria_count=1)

    assert zero_requested_output_criteria_credit(with_criteria, has_meaningful_registered_output=True) is False


def test_zero_requested_output_criteria_credit_requires_evaluated_full_satisfaction() -> None:
    assert zero_requested_output_criteria_credit(None, has_meaningful_registered_output=True) is False
    assert (
        zero_requested_output_criteria_credit(
            CompletionVerificationResult(status="unavailable"), has_meaningful_registered_output=True
        )
        is False
    )
    assert (
        zero_requested_output_criteria_credit(_evaluated(("c0", False)), has_meaningful_registered_output=True) is False
    )


_PAGE_EVIDENCE_DELIVERABLE_REF = "block_outputs:post_run_page_observation.document_name"


def _corroborated_abstention_verdicts(
    *,
    corroborator_source: EvidenceSourceKind | None,
    marked_id: str = "deliverable",
    marked_output_path: str = "output.document_name",
    marked_evidence_ref: str = _PAGE_EVIDENCE_DELIVERABLE_REF,
    corroborator_evidence_ref: str | None = None,
) -> list[CriterionVerdict]:
    return [
        CriterionVerdict(
            criterion_id=marked_id,
            state="unsatisfied",
            reason_code="structurally_abstained",
            evidence_ref=marked_evidence_ref,
            output_path=marked_output_path,
            has_exact_value=False,
        ),
        CriterionVerdict(
            criterion_id=f"{marked_id}__requested_output_corroborator",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_source=corroborator_source,
            evidence_ref=corroborator_evidence_ref,
        ),
    ]


def _observed_end_state_verdict(cid: str = "deliverable") -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id=cid,
        state="satisfied",
        reason_code="evidence_confirms",
        evidence_ref="observed_end_state_url",
    )


def _floored_run_plane_verdicts(
    *,
    corroborator_source: EvidenceSourceKind | None,
    marked_id: str = "deliverable",
    corroborator_evidence_ref: str | None = _PAGE_EVIDENCE_DELIVERABLE_REF,
) -> list[CriterionVerdict]:
    return [
        CriterionVerdict(
            criterion_id=marked_id,
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:post_run_page_observation",
            evidence_source="independent_page_evidence",
        ),
        CriterionVerdict(
            criterion_id=f"{marked_id}__requested_output_corroborator",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_source=corroborator_source,
            evidence_ref=corroborator_evidence_ref,
        ),
    ]


def _floor_rekeyed_result(
    verdicts: list[CriterionVerdict],
    marked_ids: list[str],
    output_path_by_id: dict[str, str] | None = None,
) -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=[verdict.criterion_id for verdict in verdicts],
        verdicts=list(verdicts),
        floor_rekeyed_criterion_ids=list(marked_ids),
        floor_rekeyed_output_path_by_criterion_id=dict(output_path_by_id or {}),
    )


def test_floor_rekeyed_credit_grants_on_independent_page_evidence() -> None:
    result = _floor_rekeyed_result(
        _corroborated_abstention_verdicts(
            corroborator_source="independent_page_evidence",
            corroborator_evidence_ref=_PAGE_EVIDENCE_DELIVERABLE_REF,
        ),
        ["deliverable"],
    )

    credit = floor_rekeyed_deliverable_credit(result)

    assert isinstance(credit, FloorRekeyedDeliverableCredit)
    assert credit.criterion_ids == ("deliverable",)
    assert credit.evidence_sources == ("independent_page_evidence",)
    assert credit.evidence_refs == (_PAGE_EVIDENCE_DELIVERABLE_REF,)


def test_floor_rekeyed_credit_grants_on_run_plane_marked_verdict() -> None:
    result = _floor_rekeyed_result(
        _floored_run_plane_verdicts(corroborator_source="independent_page_evidence"),
        ["deliverable"],
        {"deliverable": "output.document_name"},
    )

    credit = floor_rekeyed_deliverable_credit(result)

    assert credit is not None
    assert credit.evidence_sources == ("independent_page_evidence",)
    assert credit.output_paths == ("output.document_name",)


def test_floor_rekeyed_credit_withholds_without_corroborator() -> None:
    result = _floor_rekeyed_result([_observed_end_state_verdict()], ["deliverable"])

    assert result.is_fully_satisfied() is True
    assert floor_rekeyed_deliverable_credit(result) is None


@pytest.mark.parametrize(
    "corroborator_source",
    ["registered_output_parameter", "registered_artifact_content"],
)
def test_floor_rekeyed_credit_withholds_on_registered_corroborator_source(
    corroborator_source: EvidenceSourceKind,
) -> None:
    result = _floor_rekeyed_result(
        _corroborated_abstention_verdicts(corroborator_source=corroborator_source),
        ["deliverable"],
    )

    assert result.is_fully_satisfied() is True
    assert floor_rekeyed_deliverable_credit(result) is None


@pytest.mark.parametrize(
    "corroborator_source",
    ["runtime_output", "same_record_context", None],
)
def test_floor_rekeyed_credit_withholds_on_self_emitted_corroborator(
    corroborator_source: EvidenceSourceKind | None,
) -> None:
    result = _floor_rekeyed_result(
        _corroborated_abstention_verdicts(corroborator_source=corroborator_source),
        ["deliverable"],
    )

    assert result.is_fully_satisfied() is True
    assert floor_rekeyed_deliverable_credit(result) is None


def test_floor_rekeyed_credit_requires_every_marked_id_page_grounded() -> None:
    verdicts = [
        *_corroborated_abstention_verdicts(
            corroborator_source="independent_page_evidence",
            marked_id="deliverable_a",
            corroborator_evidence_ref=_PAGE_EVIDENCE_DELIVERABLE_REF,
        ),
        *_corroborated_abstention_verdicts(corroborator_source="runtime_output", marked_id="deliverable_b"),
    ]
    result = _floor_rekeyed_result(verdicts, ["deliverable_a", "deliverable_b"])

    assert floor_rekeyed_deliverable_credit(result) is None


def test_floor_rekeyed_credit_none_without_marked_ids() -> None:
    result = _floor_rekeyed_result(
        _corroborated_abstention_verdicts(corroborator_source="independent_page_evidence"),
        [],
    )

    assert floor_rekeyed_deliverable_credit(result) is None


def test_floor_rekeyed_credit_ignores_corroborator_scoped_to_other_id() -> None:
    verdicts = [
        *_corroborated_abstention_verdicts(corroborator_source="runtime_output"),
        CriterionVerdict(
            criterion_id="unrelated__requested_output_corroborator",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_source="independent_page_evidence",
            evidence_ref=_PAGE_EVIDENCE_DELIVERABLE_REF,
        ),
    ]
    result = _floor_rekeyed_result(verdicts, ["deliverable"])

    assert floor_rekeyed_deliverable_credit(result) is None


def test_floor_rekeyed_credit_none_when_partial_satisfaction() -> None:
    verdicts = [
        *_corroborated_abstention_verdicts(
            corroborator_source="independent_page_evidence",
            corroborator_evidence_ref=_PAGE_EVIDENCE_DELIVERABLE_REF,
        ),
        CriterionVerdict(criterion_id="other", state="unsatisfied", reason_code="no_evidence"),
    ]
    result = _floor_rekeyed_result(verdicts, ["deliverable"])

    assert result.is_fully_satisfied() is False
    assert floor_rekeyed_deliverable_credit(result) is None


def test_floor_rekeyed_credit_none_for_kill_shape() -> None:
    assert floor_rekeyed_deliverable_credit(_evaluated(("login", True))) is None


def test_floor_rekeyed_credit_does_not_mutate_result() -> None:
    result = _floor_rekeyed_result(
        _corroborated_abstention_verdicts(
            corroborator_source="independent_page_evidence",
            corroborator_evidence_ref=_PAGE_EVIDENCE_DELIVERABLE_REF,
        ),
        ["deliverable"],
    )
    before = replace(result)

    floor_rekeyed_deliverable_credit(result)

    assert result == before


def test_carry_floor_rekeyed_ids_from_marked_criteria() -> None:
    criteria = [
        CompletionCriterion(id="deliverable", outcome="Document name is shown.", requested_output_floor_rekeyed=True),
        CompletionCriterion(id="plain", outcome="Login succeeds."),
    ]
    result = CompletionVerificationResult(status="evaluated", criterion_ids=["deliverable", "plain"])

    carried = carry_floor_rekeyed_criterion_ids(result, criteria)

    assert carried.floor_rekeyed_criterion_ids == ["deliverable"]


def test_combine_threads_floor_rekeyed_ids_from_run_result() -> None:
    run_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["deliverable"],
        verdicts=[CriterionVerdict(criterion_id="deliverable", state="satisfied", reason_code="evidence_confirms")],
        floor_rekeyed_criterion_ids=["deliverable"],
    )

    combined = combine_verification_results(["deliverable"], run_result, [])

    assert combined.floor_rekeyed_criterion_ids == ["deliverable"]


def test_floor_marker_re_derived_across_persistence_round_trip() -> None:
    fresh = [
        CompletionCriterion(id="deliverable", outcome="Document name is shown.", output_path="output.document_name")
    ]

    floored_first, rekeyed_first = apply_requested_output_producer_floor(fresh)
    assert rekeyed_first == ("output.document_name",)
    assert floored_first[0].requested_output_floor_rekeyed is True

    persisted = criteria_to_json(fresh)
    assert persisted[0]["output_path"] == "output.document_name"
    assert "requested_output_floor_rekeyed" not in persisted[0]

    stored = criteria_from_json(persisted)
    assert stored[0].requested_output_floor_rekeyed is False

    snapshot = StoredCriteriaSnapshot(
        active=StoredCriteriaSet(set_id="set_1", goal_epoch=1, criteria=stored), next_epoch=2
    )
    decision = reconcile_completion_criteria(snapshot, fresh, actionable=True)
    assert decision.action == "adopt_stored"

    floored_again, rekeyed_again = apply_requested_output_producer_floor(decision.criteria)
    assert rekeyed_again == ("output.document_name",)
    assert floored_again[0].requested_output_floor_rekeyed is True


def test_floor_idempotent_on_already_floored_set() -> None:
    fresh = [
        CompletionCriterion(id="deliverable", outcome="Document name is shown.", output_path="output.document_name")
    ]
    floored_once, _ = apply_requested_output_producer_floor(fresh)

    floored_twice, rekeyed_twice = apply_requested_output_producer_floor(floored_once)

    assert rekeyed_twice == ()
    assert floored_twice[0].requested_output_floor_rekeyed is True


def test_floor_rekeyed_credit_grants_when_floor_path_binds_delivered_payload() -> None:
    ctx = _ctx_with_blocks("code")
    verification = _floor_rekeyed_result(
        _floored_run_plane_verdicts(corroborator_source="independent_page_evidence"),
        ["deliverable"],
        {"deliverable": "output.document_name"},
    )

    with capture_logs() as logs:
        recorded = _record_run_blocks_result(
            ctx,
            _registered_output_result({"document_name": "Resale certificate"}),
            completion_verification=verification,
        )

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    granted = [entry for entry in logs if entry.get("event") == "copilot.completion.floor_rekeyed_deliverable_credit"]
    assert granted and granted[0]["credited_output_paths"] == ["output.document_name"]
    assert granted[0]["registered_output_keys"] == ["bill_statement"]


def test_floor_rekeyed_credit_withheld_when_floor_path_absent_from_payload() -> None:
    ctx = _ctx_with_blocks("code")
    verification = _floor_rekeyed_result(
        _floored_run_plane_verdicts(corroborator_source="independent_page_evidence"),
        ["deliverable"],
        {"deliverable": "output.document_name"},
    )

    with capture_logs() as logs:
        recorded = _record_run_blocks_result(
            ctx, _registered_output_result({"summary": "raw"}), completion_verification=verification
        )

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert ctx.delivered_unverified_terminal is True
    unbound = [
        entry for entry in logs if entry.get("event") == "copilot.completion.floor_rekeyed_credit_payload_unbound"
    ]
    assert unbound and unbound[0]["unbound_output_paths"] == ["output.document_name"]
    assert any(entry.get("event") == "copilot.completion.zero_requested_output_credit_withheld" for entry in logs)


def test_floor_rekeyed_deliverable_reaches_verified_success() -> None:
    ctx = _ctx_with_blocks("code")
    verification = _floor_rekeyed_result(
        _corroborated_abstention_verdicts(
            corroborator_source="independent_page_evidence",
            corroborator_evidence_ref=_PAGE_EVIDENCE_DELIVERABLE_REF,
        ),
        ["deliverable"],
    )

    with capture_logs() as logs:
        recorded = _record_run_blocks_result(
            ctx, _registered_output_result({"summary": "raw"}), completion_verification=verification
        )

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    assert ctx.delivered_unverified_terminal is False
    assert ctx.turn_halt is None
    granted = [entry for entry in logs if entry.get("event") == "copilot.completion.floor_rekeyed_deliverable_credit"]
    assert granted and granted[0]["criterion_ids"] == ["deliverable"]
    assert granted[0]["evidence_sources"] == ["independent_page_evidence"]
    assert not any(entry.get("event") == "copilot.completion.zero_requested_output_credit_withheld" for entry in logs)


def _floored_policy_criteria(criteria: list[CompletionCriterion], user_message: str) -> list[CompletionCriterion]:
    policy = RequestPolicy(completion_criteria=criteria)
    _apply_requested_output_completion_criteria(policy, user_message)
    _apply_classifier_typed_requested_output_corroborators(policy)
    floored, _rekeyed = apply_requested_output_producer_floor(policy.completion_criteria)
    return list(floored)


def _satisfied_page_evidence_result(criteria: list[CompletionCriterion]) -> CompletionVerificationResult:
    verdicts = [
        CriterionVerdict(
            criterion_id=criterion.id,
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_source="independent_page_evidence",
            evidence_ref=_PAGE_EVIDENCE_DELIVERABLE_REF,
        )
        for criterion in criteria
    ]
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=[criterion.id for criterion in criteria],
        verdicts=verdicts,
    )
    return carry_floor_rekeyed_criterion_ids(result, criteria)


def test_floor_rekeyed_credit_is_inert_when_the_seam_mints_no_per_deliverable_corroborator() -> None:
    criteria = _floored_policy_criteria(
        [
            CompletionCriterion(
                id="run_end_state",
                outcome="The run opens the order-level document list and selects the demand document row.",
            )
        ],
        "Return a final record with document name.",
    )
    marked_id = "__copilot_requested_output__output_document_name"
    result = _satisfied_page_evidence_result(criteria)

    assert result.floor_rekeyed_criterion_ids == [marked_id]
    assert result.is_fully_satisfied() is True
    assert [criterion.id for criterion in criteria if criterion.requested_output_corroborator] == []
    assert floor_rekeyed_deliverable_credit(result) is None


@pytest.mark.asyncio
async def test_floor_rekeyed_deliverable_is_credited_through_the_verification_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c1",
                outcome="The run captures the document status from the order documents list.",
                output_path="output.doc_status",
            )
        ]
    )
    _apply_classifier_typed_requested_output_corroborators(policy)
    floored, rekeyed_paths = apply_requested_output_producer_floor(policy.completion_criteria)
    policy.completion_criteria = list(floored)
    assert rekeyed_paths == ("output.doc_status",)
    assert "c1__requested_output_corroborator" in {criterion.id for criterion in floored}

    async def handler(**_: object) -> dict:
        return {
            "verdicts": [
                {
                    "criterion_id": criterion.id,
                    "satisfied": True,
                    "reason_code": "evidence_confirms",
                    "evidence_ref": "block_outputs:post_run_page_observation.visible_text_excerpt",
                }
                for criterion in policy.completion_criteria
            ]
        }

    async def handler_lookup(_ctx: object) -> object:
        return handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler",
        handler_lookup,
    )
    ctx = _ctx_with_blocks("code")
    ctx.request_policy = policy
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_x",
        "observed_after_workflow_run": True,
        "visible_text_excerpt": "Resale certificate - Delivered",
    }
    result = _registered_output_result({"doc_status": "Delivered"})

    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())

    assert verification is not None
    assert verification.floor_rekeyed_criterion_ids == ["c1"]
    assert verification.floor_rekeyed_output_path_by_criterion_id == {"c1": "output.doc_status"}
    assert verification.is_fully_satisfied() is True

    with capture_logs() as logs:
        recorded = _record_run_blocks_result(ctx, result, completion_verification=verification)

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    granted = [entry for entry in logs if entry.get("event") == "copilot.completion.floor_rekeyed_deliverable_credit"]
    assert granted and granted[0]["criterion_ids"] == ["c1"]
    assert granted[0]["evidence_sources"] == ["independent_page_evidence"]


def test_floor_rekeyed_runtime_output_only_still_withholds() -> None:
    ctx = _ctx_with_blocks("code")
    verification = _floor_rekeyed_result(
        _corroborated_abstention_verdicts(corroborator_source="runtime_output"),
        ["deliverable"],
    )

    with capture_logs() as logs:
        recorded = _record_run_blocks_result(
            ctx, _registered_output_result({"summary": "raw"}), completion_verification=verification
        )

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert ctx.delivered_unverified_terminal is True
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind.value == "delivered_unverified"
    assert any(entry.get("event") == "copilot.completion.zero_requested_output_credit_withheld" for entry in logs)
    assert not any(entry.get("event") == "copilot.completion.floor_rekeyed_deliverable_credit" for entry in logs)


def test_zero_requested_output_criteria_withholds_verified_success() -> None:
    ctx = _ctx_with_blocks("code")

    with capture_logs() as logs:
        recorded = _record_run_blocks_result(
            ctx,
            _registered_output_result({"summary": "raw"}),
            completion_verification=_evaluated(("login", True)),
        )

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert ctx.delivered_unverified_terminal is True
    assert ctx.delivered_unverified_observed_outputs == {"bill_statement": {"summary": "raw"}}
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind.value == "delivered_unverified"
    assert ctx.last_full_workflow_test_ok is False
    assert any(entry.get("event") == "copilot.completion.zero_requested_output_credit_withheld" for entry in logs)

    other_ctx = _ctx_with_blocks("code")
    other = _record_run_blocks_result(
        other_ctx,
        _registered_output_result({"amount_due": "$99.99", "statement_month": "January 2026"}),
        completion_verification=_evaluated(("login", True)),
    )
    assert other is not None
    assert other.verdict == recorded.verdict
    assert other_ctx.delivered_unverified_terminal is True


def test_zero_requested_output_criteria_withholds_verified_success_on_failed_run() -> None:
    ctx = _ctx_with_blocks("code")
    result = _registered_output_result({"summary": "raw"})
    result["ok"] = False
    result["data"]["overall_status"] = "canceled"

    recorded = _record_run_blocks_result(
        ctx,
        result,
        completion_verification=_evaluated(("login", True)),
    )

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert ctx.last_test_ok is False
    assert ctx.delivered_unverified_terminal is True
    assert ctx.delivered_unverified_observed_outputs == {"bill_statement": {"summary": "raw"}}
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind.value == "delivered_unverified"


def test_requested_output_criteria_still_reach_verified_success() -> None:
    ctx = _ctx_with_blocks("code")
    verification = replace(_evaluated(("bill", True)), requested_output_criteria_count=1)

    recorded = _record_run_blocks_result(
        ctx, _registered_output_result({"summary": "raw"}), completion_verification=verification
    )

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    assert ctx.delivered_unverified_terminal is False


def test_zero_requested_output_criteria_empty_task_output_reaches_verified_success() -> None:
    ctx = _ctx_with_blocks("extraction")
    empty_task_output = {
        "task_id": "tsk_login",
        "status": "completed",
        "extracted_information": None,
        "downloaded_files": None,
        "downloaded_file_urls": None,
    }

    recorded = _record_run_blocks_result(
        ctx,
        _registered_output_result(empty_task_output, block_type="EXTRACTION"),
        completion_verification=_evaluated(("login", True)),
    )

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    assert ctx.delivered_unverified_terminal is False


def test_zero_requested_output_criteria_login_reach_state_stays_inert() -> None:
    ctx = _ctx_with_blocks("navigation")
    reach_state_envelope = {
        "task_id": "tsk_nav",
        "status": "completed",
        "extracted_information": None,
        "downloaded_files": None,
        "downloaded_file_urls": None,
    }
    result = _registered_output_result(reach_state_envelope, block_type="NAVIGATION")

    assert _has_meaningful_registered_output_payload(result["data"]) is False

    recorded = _record_run_blocks_result(ctx, result, completion_verification=_evaluated(("login", True)))

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    assert ctx.delivered_unverified_terminal is False


def test_zero_requested_output_criteria_lowercase_block_type_slices_empty_envelope() -> None:
    ctx = _ctx_with_blocks("login")
    reach_state_envelope = {
        "task_id": "tsk_login",
        "status": "completed",
        "extracted_information": None,
        "downloaded_files": None,
        "downloaded_file_urls": None,
    }
    result = _registered_output_result(reach_state_envelope, block_type="login")

    assert _has_meaningful_registered_output_payload(result["data"]) is False

    recorded = _record_run_blocks_result(ctx, result, completion_verification=_evaluated(("login", True)))

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    assert ctx.delivered_unverified_terminal is False


@pytest.mark.parametrize("block_type", ["file_download", "goto_url", "human_interaction", "task_v2"])
def test_zero_requested_output_criteria_every_task_backed_block_reach_state_stays_inert(block_type: str) -> None:
    ctx = _ctx_with_blocks(block_type)
    reach_state_envelope = {
        "task_id": f"tsk_{block_type}",
        "status": "completed",
        "extracted_information": None,
        "downloaded_files": None,
        "downloaded_file_urls": None,
    }
    result = _registered_output_result(reach_state_envelope, block_type=block_type)

    assert _has_meaningful_registered_output_payload(result["data"]) is False

    recorded = _record_run_blocks_result(ctx, result, completion_verification=_evaluated(("login", True)))

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    assert ctx.delivered_unverified_terminal is False


def test_task_envelope_block_types_matches_explicit_envelope_block_set() -> None:
    # Explicit, human-maintained list of every block type whose output is a TaskOutput
    # envelope. Independent of the production walk on purpose: TaskV2Block subclasses
    # Block directly (not BaseTaskBlock), so a walk from BaseTaskBlock alone would drop
    # it. If a new envelope block is added, update both this literal and the derivation.
    expected = {
        "LOGIN",
        "NAVIGATION",
        "EXTRACTION",
        "ACTION",
        "TASK",
        "VALIDATION",
        "FILE_DOWNLOAD",
        "GOTO_URL",
        "HUMAN_INTERACTION",
        "TASK_V2",
    }
    assert _TASK_ENVELOPE_BLOCK_TYPES == expected


def test_zero_requested_output_criteria_task_id_user_schema_is_meaningful() -> None:
    ctx = _ctx_with_blocks("code")
    user_schema = {"task_id": "tsk_1", "amount_due": "$3,927.75", "statement_month": "March 2026"}

    recorded = _record_run_blocks_result(
        ctx,
        _registered_output_result(user_schema, block_type="CODE"),
        completion_verification=_evaluated(("login", True)),
    )

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert ctx.delivered_unverified_terminal is True
    assert ctx.delivered_unverified_observed_outputs == {"bill_statement": user_schema}


def test_zero_requested_output_criteria_fires_on_persisted_output_parameters_only() -> None:
    ctx = _ctx_with_blocks("code")

    recorded = _record_run_blocks_result(
        ctx, _persisted_output_parameter_result({"summary": "raw"}), completion_verification=_evaluated(("login", True))
    )

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert ctx.delivered_unverified_terminal is True
    assert ctx.delivered_unverified_observed_outputs == {"bill_statement": {"summary": "raw"}}


def test_zero_requested_output_criteria_excludes_foreign_run_registered_output() -> None:
    ctx = _ctx_with_blocks("code")
    result = _registered_output_result({"summary": "raw"})
    result["data"]["registered_output_parameter_values"].append(
        {
            "workflow_run_id": "wr_other",
            "output_parameter_id": "op_stale",
            "output_parameter_key": "stale_output",
            "block_label": "stale_block",
            "block_type": "CODE",
            "value": {"stale": "prior-run"},
        }
    )

    recorded = _record_run_blocks_result(ctx, result, completion_verification=_evaluated(("login", True)))

    assert recorded is not None
    assert recorded.verdict == "not_evaluated"
    assert ctx.delivered_unverified_observed_outputs == {"bill_statement": {"summary": "raw"}}


def test_zero_requested_output_criteria_empty_valid_deliverable_still_credits() -> None:
    ctx = _ctx_with_blocks("code")

    recorded = _record_run_blocks_result(
        ctx, _registered_output_result({"rows": []}), completion_verification=_evaluated(("login", True))
    )

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    assert ctx.delivered_unverified_terminal is False


def test_record_run_blocks_verifies_structural_requested_output_with_run_corroborator() -> None:
    ctx = _ctx_with_blocks("extraction")
    verification = _mixed(
        CriterionVerdict(criterion_id="c_quotes", state="satisfied", reason_code="evidence_confirms"),
        CriterionVerdict(
            criterion_id="c_requested_output",
            state="unsatisfied",
            reason_code="structurally_abstained",
            evidence_ref="block_outputs:extract_first_three_quotes.quotes",
            output_path="output.quotes",
            grounding_mode="missing",
        ),
    )

    recorded = _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=verification)

    assert recorded is not None
    assert recorded.verdict == "demonstrated"
    assert verified_goal_satisfied_context(ctx) is True
    assert built_unverified_repair_inert_context(ctx) is False
    assert outcome_fully_verified(ctx) is True


def test_committed_same_run_outcome_survives_later_contradictory_overwrite() -> None:
    ctx = _ctx_with_blocks("extraction")
    verification = _mixed(
        CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms"),
        CriterionVerdict(criterion_id="c1", state="unknown", reason_code="definition_parameters_absent"),
    )

    recorded = _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=verification)

    assert recorded == RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_x")
    assert outcome_fully_verified(ctx) is True

    ctx.completion_verification_result = _mixed(
        CriterionVerdict(criterion_id="c0", state="unsatisfied", reason_code="evidence_contradicts"),
        CriterionVerdict(criterion_id="c1", state="unknown", reason_code="definition_parameters_absent"),
    )

    assert outcome_fully_verified(ctx) is True
    assert verified_goal_satisfied_context(ctx) is True


def test_first_pass_contradiction_without_committed_run_outcome_still_fails() -> None:
    ctx = _ctx_with_blocks("extraction")

    recorded = _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_contradicted("c0"))

    assert recorded is not None
    assert recorded.verdict == "not_demonstrated"
    assert outcome_fully_verified(ctx) is False
    assert verified_goal_satisfied_context(ctx) is False


def test_same_run_contradiction_after_committed_outcome_does_not_churn() -> None:
    ctx = _ctx_with_blocks("extraction")

    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", True)))
    recorded = _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_contradicted("c0"))

    assert recorded == RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_x")
    assert ctx.last_test_suspicious_success is False
    assert ctx.last_test_failure_reason is None
    assert ctx.last_full_workflow_test_ok is True
    assert outcome_fully_verified(ctx) is True


def test_missing_run_id_does_not_preserve_committed_same_run_outcome() -> None:
    ctx = _ctx_with_blocks("extraction")
    malformed_result = _clean_success_result()
    malformed_result["data"].pop("workflow_run_id")

    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", True)))
    recorded = _record_run_blocks_result(ctx, malformed_result, completion_verification=_contradicted("c0"))

    assert recorded is not None
    assert recorded.verdict == "not_demonstrated"
    assert ctx.last_run_outcome == recorded
    assert outcome_fully_verified(ctx) is False


def test_committed_same_run_outcome_surfaces_verified_workflow_after_later_contradiction() -> None:
    ctx = _ctx_with_blocks("extraction")
    ctx.last_workflow_yaml = "workflow: {}"

    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", True)))
    ctx.completion_verification_result = _contradicted("c0")

    assert _completion_contract_not_violated(ctx) is True
    assert _verified_workflow_or_none(ctx) == (ctx.last_workflow, "workflow: {}")


def test_first_pass_contradiction_does_not_surface_verified_workflow() -> None:
    ctx = _ctx_with_blocks("extraction")
    ctx.last_workflow_yaml = "workflow: {}"

    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_contradicted("c0"))

    assert _completion_contract_not_violated(ctx) is False
    assert _verified_workflow_or_none(ctx) == (None, None)


def test_different_run_id_committed_outcome_does_not_surface_verified_workflow() -> None:
    ctx = _ctx_with_blocks("extraction")
    ctx.last_workflow_yaml = "workflow: {}"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = True
    ctx.last_run_blocks_workflow_run_id = "wr_new"
    ctx.last_run_outcome = RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_prior")
    ctx.completion_verification_result = _contradicted("c0")

    assert outcome_fully_verified(ctx) is False
    assert _completion_contract_not_violated(ctx) is False
    assert _verified_workflow_or_none(ctx) == (None, None)


def test_missing_current_run_id_does_not_surface_verified_workflow_from_prior_outcome() -> None:
    ctx = _ctx_with_blocks("extraction")
    ctx.last_workflow_yaml = "workflow: {}"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = True
    ctx.last_run_blocks_workflow_run_id = None
    ctx.last_run_outcome = RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_prior")
    ctx.completion_verification_result = _contradicted("c0")

    assert outcome_fully_verified(ctx) is False
    assert _completion_contract_not_violated(ctx) is False
    assert _verified_workflow_or_none(ctx) == (None, None)


def test_artifact_health_blocks_verified_workflow_surfacing_with_committed_outcome() -> None:
    ctx = _ctx_with_blocks("extraction")
    ctx.last_workflow_yaml = "workflow: {}"

    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", True)))
    ctx.last_artifact_health_blocker_reason = "Code block failed with SyntaxError."

    assert outcome_fully_verified(ctx) is False
    assert _completion_contract_not_violated(ctx) is False
    assert _verified_workflow_or_none(ctx) == (None, None)


def test_unavailable_verification_without_committed_outcome_fails_closed_for_surfacing() -> None:
    ctx = _ctx_with_blocks("extraction")
    ctx.last_workflow_yaml = "workflow: {}"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = True
    ctx.completion_verification_result = CompletionVerificationResult(status="unavailable")

    assert outcome_fully_verified(ctx) is False
    assert _completion_contract_not_violated(ctx) is False
    assert _verified_workflow_or_none(ctx) == (None, None)


def _goto_only_result() -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_goto",
            "overall_status": "completed",
            "executed_block_labels": ["open_example"],
            "current_url": "https://example.com/",
            "page_title": "Example Domain",
            "blocks": [
                {
                    "label": "open_example",
                    "block_type": "GOTO_URL",
                    "status": "completed",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_goto_only_run_still_fails_extraction_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {
            "verdicts": [
                {
                    "criterion_id": "c0",
                    "satisfied": False,
                    "reason_code": "no_evidence",
                    "missing_evidence": "block output containing the requested heading and first paragraph text",
                }
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("goto_url")
    ctx.request_policy = RequestPolicy(completion_criteria=[_criterion("c0", "heading and paragraph are extracted")])

    verification = await _maybe_run_completion_verification(ctx, _goto_only_result(), time.monotonic())
    assert verification is not None
    assert verification.is_fully_satisfied() is False

    _record_run_blocks_result(ctx, _goto_only_result(), completion_verification=verification)

    assert ctx.last_full_workflow_test_ok is False
    assert getattr(ctx, "last_good_workflow", None) is None
    assert verified_goal_satisfied_context(ctx) is False


@pytest.mark.asyncio
async def test_structured_blocker_run_skips_completion_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        raise AssertionError("terminal challenge runs must not be sent to the completion judge")

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("code")
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_blocked",
            "overall_status": "completed",
            "executed_block_labels": ["search"],
            "current_url": "https://example.com/",
            "blocks": [
                {
                    "label": "search",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {
                        "blocked_by_challenge": True,
                        "reason": "The submit control stayed disabled by a challenge.",
                    },
                }
            ],
        },
    }

    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())

    assert verification is None


def test_proxy_location_none_definition_criterion_stays_unknown() -> None:
    verdicts = grade_definition_criteria(
        [
            CompletionCriterion(
                id="c7",
                outcome="The workflow definition sets proxy_location to NONE.",
                level="definition",
            )
        ],
        "proxy_location: NONE\nworkflow_definition:\n  blocks: []\n",
    )

    assert verdicts == [CriterionVerdict(criterion_id="c7", state="unknown", reason_code="definition_unknown")]


@pytest.mark.asyncio
async def test_classifier_fallback_record_is_not_verified_without_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        raise AssertionError("value-agnostic fallback criteria must not reach the completion judge")

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("code")
    ctx.request_policy = RequestPolicy(completion_criteria=_structured_record_criteria())

    result = _structured_record_top_level_output_result()
    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())
    assert verification is None

    _record_run_blocks_result(ctx, result, completion_verification=verification)
    # The strict barrier predicate and its telemetry flag stay false, so the proposal is
    # not preserved as a verified success; legacy clean-run flags may still promote, as
    # they do for any genuine zero-criteria run.
    assert getattr(ctx, "verified_terminal_proposal_ready", False) is not True
    assert outcome_fully_verified(ctx) is False


@pytest.mark.asyncio
async def test_non_fallback_judge_confirmed_run_still_fires_barrier(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("extraction")
    ctx.request_policy = RequestPolicy(completion_criteria=[_criterion("c0", "item in cart")])

    result = _clean_success_result()
    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())
    assert verification is not None
    assert verification.is_fully_satisfied() is True

    _record_run_blocks_result(ctx, result, completion_verification=verification)
    assert outcome_fully_verified(ctx) is True
    assert verified_goal_satisfied_context(ctx) is True


@pytest.mark.asyncio
async def test_validation_classification_block_output_satisfies_without_judge_unknown_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        raise AssertionError("typed validation classification should be graded deterministically")

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("code")
    _set_workflow_labels(ctx, "classify_path")
    ctx.request_policy = RequestPolicy(completion_criteria=[_validation_classification_criterion("login_gated")])

    first = await _maybe_run_completion_verification(
        ctx,
        _validation_classification_output_result("login_gated"),
        time.monotonic(),
    )
    second = await _maybe_run_completion_verification(
        ctx,
        _validation_classification_output_result("login_gated"),
        time.monotonic(),
    )

    assert first is not None
    assert second is not None
    assert first.is_fully_satisfied() is True
    assert second.is_fully_satisfied() is True
    assert first.verdicts == second.verdicts


@pytest.mark.asyncio
async def test_validation_classification_duplicate_registered_output_surfaces_satisfy_without_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        raise AssertionError("repeated coherent typed classification evidence should not require judge authority")

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("code")
    _set_workflow_labels(ctx, "classify_path")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[_validation_classification_criterion(True, key="login_only")]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_validation_classification",
                "overall_status": "completed",
                "executed_block_labels": ["classify_path"],
                "current_url": "https://example.test/login",
                "blocks": [
                    {
                        "label": "classify_path",
                        "block_type": "CODE",
                        "status": "completed",
                        "extracted_data": {"login_only": True},
                    }
                ],
                "registered_output_parameter_values": [
                    {
                        "workflow_run_id": "wr_validation_classification",
                        "output_parameter_id": "op_login_only",
                        "output_parameter_key": "classify_path_output",
                        "block_label": "classify_path",
                        "block_type": "CODE",
                        "value": {"login_only": True},
                    }
                ],
            },
        },
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts == [
        CriterionVerdict(
            criterion_id="c_validation",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:classify_path.login_only",
            output_path="login_only",
            grounding_mode="exact_value",
            has_exact_value=True,
        )
    ]


@pytest.mark.asyncio
async def test_validation_classification_is_not_satisfied_by_fallback_floor_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {
            "verdicts": [
                {
                    "criterion_id": "c_validation",
                    "satisfied": False,
                    "reason_code": "no_evidence",
                    "missing_evidence": "matching classification output at path_classification",
                }
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("code")
    _set_workflow_labels(ctx, "submit_request")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _validation_classification_criterion("login_gated"),
            *build_classifier_fallback_floor([]),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _validation_review_output_result(),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert "c_validation" in {verdict.criterion_id for verdict in verification.verdicts if not verdict.satisfied}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"evidence_text": "The path is login_gated."},
        {"success": True},
    ],
)
@pytest.mark.asyncio
async def test_validation_classification_missing_or_prose_only_evidence_cannot_be_judge_approved(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    calls = 0

    async def handler(**_: object) -> dict:
        nonlocal calls
        calls += 1
        return {"verdicts": [{"criterion_id": "c_validation", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("code")
    _set_workflow_labels(ctx, "classify_path")
    ctx.request_policy = RequestPolicy(completion_criteria=[_validation_classification_criterion("login_gated")])

    verification = await _maybe_run_completion_verification(
        ctx,
        _validation_classification_payload_result(payload),
        time.monotonic(),
    )

    assert calls == 0
    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert verification.verdicts == [
        CriterionVerdict(
            criterion_id="c_validation",
            state="unsatisfied",
            reason_code="no_evidence",
            output_path="path_classification",
            grounding_mode="exact_value",
            has_exact_value=True,
            missing_evidence="missing classification output key path_classification",
        )
    ]


@pytest.mark.parametrize(
    "criterion",
    [
        _criterion(
            "c_validation",
            "The run classifies whether the path is login gated.",
            kind="validation_classification",
            expected_classification="login_gated",
        ),
        _criterion(
            "c_validation",
            "The run classifies whether the path is login gated.",
            kind="validation_classification",
            classification_output_key="path_classification",
        ),
        _criterion(
            "c_validation",
            "The run classifies whether the path is login gated.",
            kind="validation_classification",
            output_path="output.path_classification",
            expected_output_value="login_gated",
        ),
    ],
)
@pytest.mark.asyncio
async def test_validation_classification_incomplete_contract_cannot_be_judge_approved(
    monkeypatch: pytest.MonkeyPatch,
    criterion: CompletionCriterion,
) -> None:
    calls = 0

    async def handler(**_: object) -> dict:
        nonlocal calls
        calls += 1
        return {
            "verdicts": [
                {"criterion_id": "c_validation", "satisfied": True, "reason_code": "evidence_confirms"},
                {"criterion_id": "c_other", "satisfied": True, "reason_code": "evidence_confirms"},
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("code")
    _set_workflow_labels(ctx, "classify_path")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("path_classification")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            criterion,
            _criterion("c_other", "The run produced ordinary page evidence."),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _validation_classification_output_result("login_gated"),
        time.monotonic(),
    )

    assert calls == 1
    assert verification is not None
    assert verification.is_fully_satisfied() is False
    verdict_by_id = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdict_by_id["c_validation"].state == "unsatisfied"
    assert verdict_by_id["c_validation"].reason_code == "no_evidence"
    assert verdict_by_id["c_validation"].missing_evidence == "incomplete typed classification contract"
    assert verdict_by_id["c_other"].satisfied is True


@pytest.mark.asyncio
async def test_classifier_fallback_record_contradiction_still_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        raise AssertionError("structural contradictions are deterministic, not judged")

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("code")
    ctx.request_policy = RequestPolicy(completion_criteria=_structured_record_criteria())

    contradictory_record = _record_payload(
        items=[{"item_name": "Sample Practice Active", "address": "100 Main St", "status": "Expired"}],
        overall_status="Expired",
    )
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_contradiction",
            "overall_status": "completed",
            "executed_block_labels": ["extract_record_status_record"],
            "current_url": "https://structured_record.test/entity-details",
            "blocks": [
                {
                    "label": "extract_record_status_record",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {"extracted_information": []},
                }
            ],
            "output": {"extract_record_status_record_output": contradictory_record},
        },
    }

    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())
    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert any(not verdict.satisfied for verdict in verification.verdicts)


def _failed_code_block_result() -> dict:
    raw = (
        "code block failed. failure reason: Failed to execute code block. Reason: TimeoutError: "
        "Timeout 30000ms exceeded. =========================== logs =========================== "
        '"load" event fired ============================================================'
    )
    return {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_x",
            "overall_status": "failed",
            "executed_block_labels": ["b0"],
            "blocks": [{"label": "b0", "block_type": "code", "status": "failed", "failure_reason": raw}],
        },
    }


def test_failed_run_records_gate_reason_separately_from_raw_block_failure() -> None:
    ctx = _ctx_with_blocks("extraction")
    _record_run_blocks_result(ctx, _failed_code_block_result(), completion_verification=_evaluated(("c0", False)))
    assert "item in cart" in (ctx.last_outcome_gate_reason or "")
    assert "TimeoutError" not in (ctx.last_outcome_gate_reason or "")
    assert "TimeoutError" in (ctx.last_test_failure_reason or "")


def test_gate_reason_survives_a_later_run_without_verification() -> None:
    ctx = _ctx_with_blocks("extraction")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", False)))
    assert "item in cart" in (ctx.last_outcome_gate_reason or "")
    _record_run_blocks_result(ctx, _failed_code_block_result(), completion_verification=None)
    assert "item in cart" in (ctx.last_outcome_gate_reason or "")


def test_gate_reason_cleared_when_outcome_verified() -> None:
    ctx = _ctx_with_blocks("extraction")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", False)))
    assert ctx.last_outcome_gate_reason is not None
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", True)))
    assert ctx.last_outcome_gate_reason is None


def test_record_run_blocks_keeps_success_when_outcome_verified() -> None:
    ctx = _ctx_with_blocks("extraction")
    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=_evaluated(("c0", True)))
    assert ctx.last_full_workflow_test_ok is True
    assert ctx.last_test_suspicious_success is False
    assert ctx.workflow_verification_evidence.full_workflow_verified is True


def test_current_workflow_has_evidence_block() -> None:
    assert _current_workflow_has_evidence_block(_ctx_with_blocks("extraction")) is True
    assert _current_workflow_has_evidence_block(_ctx_with_blocks("goto_url", "validation")) is True
    assert _current_workflow_has_evidence_block(_ctx_with_blocks("goto_url", "navigation")) is False
    assert _current_workflow_has_evidence_block(_run_ctx()) is False


def test_active_terminal_watchdog_exit_cannot_promote_to_terminal_success() -> None:
    assert _watchdog_exit_allows_terminal_promotion("active_run_terminal_evidence") is False
    assert _watchdog_exit_allows_terminal_promotion("per_tool_budget") is True
    assert _watchdog_exit_allows_terminal_promotion("task_exit_unfinalized") is True


def test_outcome_failure_warrants_repair() -> None:
    has_block = _ctx_with_blocks("extraction")
    nav_only = _ctx_with_blocks("goto_url", "navigation")
    structural_abstention = _mixed(
        CriterionVerdict(
            criterion_id="c_requested_output",
            state="unsatisfied",
            reason_code="structurally_abstained",
            evidence_ref="block_outputs:lookup.missing_value",
        )
    )
    mixed_abstention_and_failure = _mixed(
        CriterionVerdict(
            criterion_id="c_requested_output",
            state="unsatisfied",
            reason_code="structurally_abstained",
            evidence_ref="block_outputs:lookup.missing_value",
        ),
        CriterionVerdict(criterion_id="c_real_failure", state="unsatisfied", reason_code="no_evidence"),
    )
    assert _outcome_failure_warrants_repair(nav_only, None) is False
    # Contradiction is a real failure regardless of which blocks exist.
    assert _outcome_failure_warrants_repair(nav_only, _contradicted("c0")) is True
    # Absence of evidence: failure only once a confirmation block exists.
    assert _outcome_failure_warrants_repair(has_block, _evaluated(("c0", False))) is True
    assert _outcome_failure_warrants_repair(nav_only, _evaluated(("c0", False))) is False
    assert structural_abstention.is_fully_satisfied() is False
    assert _outcome_failure_warrants_repair(has_block, structural_abstention) is False
    assert _outcome_failure_warrants_repair(has_block, mixed_abstention_and_failure) is True


# --- Direction 2: recognition governed by evidence, not run status ---------------
#
# A run canceled or only partially completed (ok=False) still produces runtime
# evidence. When that evidence confirms every outcome criterion, the goal the user
# can observe was reached, and recognition must not be suppressed by run status.


def _canceled_budget_result() -> dict:
    # The watchdog budget-cancel result shape: ok=False, no "blocks" list (the
    # result is returned before block harvest), only the reached URL survives.
    return {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_cancel",
            "overall_status": "canceled",
            "current_url": "https://example.com/cart",
            "failure_reason": "Task wr_cancel was canceled",
            "failure_categories": [{"category": "PER_TOOL_BUDGET", "confidence_float": 1.0, "reasoning": "budget"}],
        },
    }


def _canceled_gate_ctx() -> CopilotContext:
    # A run that did not finish cleanly: every run-status latch is false and the
    # diagnosis routed to repair, yet the judge confirmed the outcome from evidence.
    ctx = _gate_ctx()
    ctx.last_test_ok = False
    ctx.last_full_workflow_test_ok = False
    ctx.latest_diagnosis_repair_contract = DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="run_blocks_and_collect_debug"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=RepairNextAction.REPAIR),
        verification_result=VerificationResult(user_goal_satisfied=False, completion_contract_satisfied=True),
    )
    return ctx


def _failed_generated_code_result() -> dict:
    return {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_failed_code",
            "overall_status": "failed",
            "executed_block_labels": ["extract_results"],
            "current_url": "https://example.com/results",
            "blocks": [
                {
                    "label": "extract_results",
                    "block_type": "EXTRACTION",
                    "status": "failed",
                    "extracted_data": {"extracted_information": ["goal text from partial output"]},
                    "failure_reason": "Page.evaluate: SyntaxError: Unexpected token ')'",
                }
            ],
        },
    }


def test_artifact_health_type_error_is_not_masked_by_timeout_category() -> None:
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_failed_code",
            "overall_status": "failed",
            "failure_categories": [
                {
                    "category": "PAGE_LOAD_TIMEOUT",
                    "confidence_float": 0.8,
                    "reasoning": "Timeout in failure reason",
                }
            ],
            "blocks": [
                {
                    "label": "wait_for_results",
                    "block_type": "ACTION",
                    "status": "failed",
                    "failure_reason": (
                        "TypeError: Page.wait_for_function() got an unexpected keyword argument 'timeout_ms'"
                    ),
                }
            ],
        },
    }

    reason, failed_labels, failure_classes = _artifact_health_blocker_from_result(result)

    assert reason is not None
    assert "TypeError" in reason
    assert failed_labels == ["wait_for_results"]
    assert failure_classes == ["TypeError"]


def test_artifact_health_not_masked_by_mixed_excluded_category() -> None:
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_failed_code",
            "overall_status": "failed",
            "failure_categories": [
                {"category": "AUTH_FAILURE", "confidence_float": 0.8},
                {"category": "SCRIPT_ERROR", "confidence_float": 0.9},
            ],
            "blocks": [
                {
                    "label": "extract_results",
                    "block_type": "EXTRACTION",
                    "status": "failed",
                    "failure_reason": "Page.evaluate: SyntaxError: Unexpected token ')'",
                }
            ],
        },
    }

    reason, failed_labels, failure_classes = _artifact_health_blocker_from_result(result)

    assert reason is not None
    assert "SyntaxError" in reason
    assert failed_labels == ["extract_results"]
    assert failure_classes == ["SyntaxError"]


def test_artifact_health_skips_when_all_categories_are_excluded() -> None:
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_auth_failed",
            "overall_status": "failed",
            "failure_categories": [
                {"category": "AUTH_FAILURE", "confidence_float": 0.8},
                {"category": "CREDENTIAL_ERROR", "confidence_float": 0.7},
            ],
            "blocks": [
                {
                    "label": "extract_results",
                    "block_type": "EXTRACTION",
                    "status": "failed",
                    "failure_reason": "Page.evaluate: SyntaxError: Unexpected token ')'",
                }
            ],
        },
    }

    reason, failed_labels, failure_classes = _artifact_health_blocker_from_result(result)

    assert reason is None
    assert failed_labels == []
    assert failure_classes == []


def _syntax_error_result_with_anti_bot_category(evidence_source: str | None) -> dict:
    category = {"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9}
    if evidence_source is not None:
        category["evidence_source"] = evidence_source
    return {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_failed_code",
            "overall_status": "failed",
            "failure_categories": [category],
            "blocks": [
                {
                    "label": "extract_results",
                    "block_type": "EXTRACTION",
                    "status": "failed",
                    "failure_reason": "Page.evaluate: SyntaxError: Unexpected token ')'",
                }
            ],
        },
    }


def test_artifact_health_not_suppressed_by_keyword_only_anti_bot_category() -> None:
    result = _syntax_error_result_with_anti_bot_category("keyword_only")

    reason, failed_labels, failure_classes = _artifact_health_blocker_from_result(result)

    assert reason is not None
    assert "SyntaxError" in reason
    assert failed_labels == ["extract_results"]
    assert failure_classes == ["SyntaxError"]


def test_artifact_health_skips_when_anti_bot_category_is_carrier_backed() -> None:
    result = _syntax_error_result_with_anti_bot_category("challenge_state")

    reason, failed_labels, failure_classes = _artifact_health_blocker_from_result(result)

    assert reason is None
    assert failed_labels == []
    assert failure_classes == []


def test_unfinished_run_verification_candidate_admits_canceled_with_evidence() -> None:
    ctx = _run_ctx()
    assert _is_unfinished_run_verification_candidate(ctx, _canceled_budget_result()) is True
    # ok=True belongs to the clean-success candidate path, not this one.
    assert _is_unfinished_run_verification_candidate(ctx, _clean_success_result()) is False
    # ok=False with no reached runtime URL leaves nothing to judge.
    assert _is_unfinished_run_verification_candidate(ctx, {"ok": False, "data": {}}) is False


def test_artifact_health_blocks_fully_satisfied_failed_run() -> None:
    result = _failed_generated_code_result()
    ctx = _run_ctx()
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[]))
    ctx.last_workflow_yaml = "workflow: {}"

    snapshot = _build_run_evidence_snapshot(ctx, result)
    rendered = snapshot.render_prompt_block()
    assert "run_terminal_status: failed" in rendered
    assert "failure_classes: SyntaxError" in rendered
    assert "failed_block_labels: extract_results" in rendered

    _record_run_blocks_result(ctx, result, completion_verification=_evaluated(("c0", True)))

    assert ctx.last_artifact_health_blocker_reason is not None
    assert "SyntaxError" in ctx.last_artifact_health_blocker_reason
    assert ctx.last_artifact_health_blocker_labels == ["extract_results"]
    assert ctx.last_artifact_health_failure_classes == ["SyntaxError"]
    assert outcome_fully_verified(ctx) is False
    assert verified_goal_satisfied_context(ctx) is False
    assert _verified_workflow_or_none(ctx) == (None, None)


def test_artifact_health_blocks_committed_same_run_outcome() -> None:
    ctx = _ctx_with_blocks("extraction")
    verification = _evaluated(("c0", True))

    _record_run_blocks_result(ctx, _clean_success_result(), completion_verification=verification)
    ctx.last_artifact_health_blocker_reason = "Code block failed with SyntaxError."

    assert outcome_fully_verified(ctx) is False
    assert verified_goal_satisfied_context(ctx) is False


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_runs_on_canceled_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    result = await _maybe_run_completion_verification(ctx, _canceled_budget_result(), time.monotonic())
    assert result is not None
    assert result.status == "evaluated"
    assert result.is_fully_satisfied() is True


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_skips_active_terminal_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        raise AssertionError("active-run terminal evidence must not be promoted to final success")

    async def fake_completion_verification_handler(_ctx: object) -> object:
        return handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler",
        fake_completion_verification_handler,
    )
    ctx = _run_ctx()
    result = _canceled_budget_result()
    result["data"]["active_run_terminal_evidence_detected"] = True

    assert await _maybe_run_completion_verification(ctx, result, time.monotonic()) is None


@pytest.mark.asyncio
async def test_active_run_terminal_evidence_sample_matches_current_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def handler(**kwargs: object) -> dict:
        captured.update(kwargs)
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    async def fake_fallback_page_info(_ctx: object) -> tuple[str, str]:
        return "https://example.com/cart", "Cart"

    async def fake_capture_composition_evidence(
        _ctx: object,
        *,
        inspected_url: str,
        current_url: str,
        active_run_terminal_sample: bool = False,
    ) -> tuple[dict, None]:
        captured["active_run_terminal_sample"] = active_run_terminal_sample
        return (
            {
                "inspected_url": inspected_url,
                "current_url": current_url,
                "page_title": "Cart",
                "visible_text_excerpt": "Cart TESTBRAND PART-001-TEST quantity 1",
                "forms": [],
                "result_containers": [{"selector": "#cart"}],
                "anti_bot_indicators": [],
            },
            None,
        )

    async def fake_completion_verification_handler(_ctx: object) -> object:
        return handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._completion_verification_handler",
        fake_completion_verification_handler,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._fallback_page_info", fake_fallback_page_info
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._capture_composition_evidence",
        fake_capture_composition_evidence,
    )
    ctx = _run_ctx()

    sample = await _active_run_terminal_evidence_sample(
        ctx,
        workflow_run_id="wr_active",
        labels_to_execute=["search_and_add"],
        sample_index=1,
    )

    assert sample is not None
    assert sample.completion_verification.is_fully_satisfied() is True
    assert sample.current_url == "https://example.com/cart"
    assert sample.page_evidence["observed_during_active_workflow_run"] is True
    assert captured["active_run_terminal_sample"] is True
    assert "PART-001-TEST" in str(captured["prompt"])


@pytest.mark.asyncio
async def test_active_run_terminal_evidence_sample_skips_method_only_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        raise AssertionError("method-mandated criteria cannot be verified from page state")

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.composition_capture._completion_verification_handler", lambda: handler
    )
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[_criterion("c0", "must use website search", method_mandated=True)]
    )

    assert (
        await _active_run_terminal_evidence_sample(
            ctx,
            workflow_run_id="wr_active",
            labels_to_execute=["search_and_add"],
            sample_index=1,
        )
        is None
    )


def test_active_run_terminal_evidence_result_shape_is_not_final_success() -> None:
    sample = SimpleNamespace(
        current_url="https://example.com/cart",
        page_title="Cart",
        sample_index=2,
        completion_verification=_evaluated(("c0", True)),
        page_evidence={
            "current_url": "https://example.com/cart",
            "page_title": "Cart",
            "visible_text_excerpt": "Cart TESTBRAND PART-001-TEST quantity 1",
        },
    )

    result = _active_run_terminal_evidence_result(
        workflow_run_id="wr_active",
        run_status="running",
        sample=sample,
        requested_block_labels=["search_and_add"],
        executed_block_labels=["search_and_add"],
    )

    assert result["ok"] is False
    assert result["data"]["active_run_terminal_evidence_detected"] is True
    assert result["data"]["active_run_terminal_evidence_reason_code"] == ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
    assert result["data"]["full_workflow_verified"] is False
    assert result["data"]["failure_categories"][0]["category"] == ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY


def test_active_run_terminal_evidence_contract_noops_when_outcome_fully_verified() -> None:
    sample = SimpleNamespace(
        current_url="https://example.com/cart",
        page_title="Cart",
        sample_index=2,
        completion_verification=_evaluated(("c0", True)),
        page_evidence={
            "current_url": "https://example.com/cart",
            "page_title": "Cart",
            "visible_text_excerpt": "Cart TESTBRAND PART-001-TEST quantity 1",
        },
    )
    result = _active_run_terminal_evidence_result(
        workflow_run_id="wr_active",
        run_status="canceled",
        sample=sample,
        requested_block_labels=["search_and_add"],
        executed_block_labels=["search_and_add"],
    )
    ctx = _run_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True))

    contract = build_diagnosis_repair_contract(source_tool="update_and_run_blocks", result=result, ctx=ctx)

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.NO_FAILURE
    assert contract.repair_decision.next_action == RepairNextAction.NO_CHANGE
    assert contract.verification_result.user_goal_satisfied is True
    assert contract.verification_result.completion_contract_satisfied is True


def test_active_run_terminal_evidence_contract_requires_reason_code_for_verified_noop() -> None:
    sample = SimpleNamespace(
        current_url="https://example.com/cart",
        page_title="Cart",
        sample_index=2,
        completion_verification=_evaluated(("c0", True)),
        page_evidence={"current_url": "https://example.com/cart"},
    )
    result = _active_run_terminal_evidence_result(
        workflow_run_id="wr_active",
        run_status="canceled",
        sample=sample,
        requested_block_labels=["search_and_add"],
        executed_block_labels=["search_and_add"],
    )
    result["data"].pop("active_run_terminal_evidence_reason_code")
    ctx = _run_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True))

    contract = build_diagnosis_repair_contract(source_tool="update_and_run_blocks", result=result, ctx=ctx)

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.ACTIVE_RUN_TERMINAL_EVIDENCE
    assert contract.repair_decision.next_action == RepairNextAction.STOP


def test_terminal_challenge_contract_still_stops_when_outcome_fully_verified() -> None:
    ctx = _run_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True))
    result = {
        "ok": False,
        "error": "blocked by human verification",
        "data": {
            "workflow_run_id": "wr_blocked",
            "overall_status": "failed",
            "failure_categories": [
                {"category": "ANTI_BOT_DETECTION", "confidence_float": 1.0, "evidence_source": "challenge_state"}
            ],
        },
    }

    contract = build_diagnosis_repair_contract(source_tool="update_and_run_blocks", result=result, ctx=ctx)

    assert contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.TERMINAL_CHALLENGE_BLOCKER
    assert contract.repair_decision.next_action == RepairNextAction.STOP
    assert contract.verification_result.user_goal_satisfied is False
    assert contract.verification_result.completion_contract_satisfied is False


def test_record_active_run_terminal_evidence_keeps_workflow_unverified() -> None:
    sample = SimpleNamespace(
        current_url="https://example.com/cart",
        page_title="Cart",
        sample_index=2,
        completion_verification=_evaluated(("c0", True)),
        page_evidence={
            "current_url": "https://example.com/cart",
            "page_title": "Cart",
            "visible_text_excerpt": "Cart TESTBRAND PART-001-TEST quantity 1",
        },
    )
    result = _active_run_terminal_evidence_result(
        workflow_run_id="wr_active",
        run_status="canceled",
        sample=sample,
        requested_block_labels=["search_and_add"],
        executed_block_labels=["search_and_add"],
    )
    ctx = _run_ctx()

    _record_run_blocks_result(ctx, result)

    assert ctx.last_test_ok is False
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.last_failure_category_top == ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY
    assert ctx.workflow_verification_evidence.full_workflow_verified is False
    assert ctx.workflow_verification_evidence.live_page_state_verified is True
    assert ctx.workflow_verification_evidence.active_run_terminal_evidence_detected is True
    assert ctx.workflow_verification_evidence.active_run_terminal_evidence_workflow_run_id == "wr_active"
    assert ctx.workflow_verification_evidence.active_run_terminal_evidence_sample_index == 2
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_active_run_terminal_evidence"


def test_active_run_terminal_evidence_blocks_same_turn_mutation_tools() -> None:
    ctx = _run_ctx()
    ctx.last_failure_category_top = ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY
    ctx.workflow_verification_evidence.active_run_terminal_evidence_detected = True
    ctx.workflow_verification_evidence.current_url = "https://example.com/cart"
    ctx.workflow_verification_evidence.workflow_run_id = "wr_active"

    result = _tool_loop_error(ctx, "update_and_run_blocks", {"block_labels": ["search_and_add"]})

    assert result is not None
    assert "ACTIVE_RUN_TERMINAL_EVIDENCE" in result
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "tool_error_active_run_terminal_evidence"


def test_outcome_fully_verified_predicate() -> None:
    ctx = _gate_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert outcome_fully_verified(ctx) is True
    ctx.completion_verification_result = _evaluated(("c0", True), ("c1", False))
    assert outcome_fully_verified(ctx) is False
    ctx.completion_verification_result = None
    assert outcome_fully_verified(ctx) is False


def test_gate_recognizes_canceled_run_on_full_evidence() -> None:
    ctx = _canceled_gate_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert verified_goal_satisfied_context(ctx) is True


def test_gate_does_not_recognize_partial_canceled_run() -> None:
    ctx = _canceled_gate_ctx()
    ctx.completion_verification_result = _evaluated(("c0", True), ("c1", False))
    assert verified_goal_satisfied_context(ctx) is False


def test_tool_completion_recognizes_canceled_run_on_full_evidence() -> None:
    ctx = _canceled_gate_ctx()
    parsed = {"ok": False, "data": {"workflow_run_id": "wr_cancel"}}
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert _tool_completion_satisfies_turn(ctx, "run_blocks_and_collect_debug", parsed) is True
    # A canceled run whose outcome is only partially confirmed does not satisfy the turn.
    ctx.completion_verification_result = _evaluated(("c0", True), ("c1", False))
    assert _tool_completion_satisfies_turn(ctx, "run_blocks_and_collect_debug", parsed) is False


def test_verified_workflow_presented_on_recognized_canceled_run() -> None:
    ctx = _canceled_gate_ctx()
    ctx.last_workflow = SimpleNamespace()
    ctx.last_workflow_yaml = "workflow: {}"
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert _verified_workflow_or_none(ctx) == (ctx.last_workflow, "workflow: {}")
    # Run-status latches false and outcome not fully confirmed: nothing is surfaced.
    ctx.completion_verification_result = _evaluated(("c0", False))
    assert _verified_workflow_or_none(ctx) == (None, None)


@pytest.mark.asyncio
async def test_page_observation_verification_recognizes_budgeted_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_prompt: dict[str, str] = {}

    async def handler(**kwargs: object) -> dict:
        seen_prompt["prompt"] = str(kwargs.get("prompt") or "")
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.last_test_ok = False
    ctx.last_run_blocks_workflow_run_id = "wr_cancel"
    ctx.copilot_run_start_monotonic = time.monotonic()
    _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url="https://example.com/cart",
        title="Shopping Cart",
        observed_data={
            "hasProduct": True,
            "excerpts": ["SKU-12345 is present in the cart"],
            "url": "https://example.com/cart",
            "title": "Shopping Cart",
        },
    )

    result = await _maybe_run_completion_verification_from_page_observation(
        ctx,
        url="https://example.com/cart",
        title="Shopping Cart",
        observed_data={
            "hasProduct": True,
            "excerpts": ["SKU-12345 is present in the cart"],
        },
    )

    assert result is not None
    assert result.is_fully_satisfied() is True
    assert ctx.completion_verification_result is result
    assert outcome_fully_verified(ctx) is True
    assert "current_page_observation" in seen_prompt["prompt"]
    assert "SKU-12345 is present in the cart" in seen_prompt["prompt"]


@pytest.mark.asyncio
async def test_page_observation_validation_classification_cannot_be_judge_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_calls = 0

    async def handler(**_: object) -> dict:
        nonlocal handler_calls
        handler_calls += 1
        return {
            "verdicts": [
                {"criterion_id": "c_validation", "satisfied": True, "reason_code": "evidence_confirms"},
                {"criterion_id": "c_page", "satisfied": True, "reason_code": "evidence_confirms"},
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _validation_classification_criterion("login_gated"),
            _criterion("c_page", "The page observation confirms the path was inspected."),
        ]
    )
    ctx.last_test_ok = False
    ctx.last_run_blocks_workflow_run_id = "wr_cancel"
    ctx.copilot_run_start_monotonic = time.monotonic()
    _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url="https://example.test/login",
        title="Login",
        observed_data={"evidence_text": "The path is login_gated."},
    )

    result = await _maybe_run_completion_verification_from_page_observation(
        ctx,
        url="https://example.test/login",
        title="Login",
        observed_data={"evidence_text": "The path is login_gated."},
    )

    assert handler_calls == 1
    assert result is not None
    assert result.is_fully_satisfied() is False
    verdict_by_id = {verdict.criterion_id: verdict for verdict in result.verdicts}
    assert verdict_by_id["c_validation"] == CriterionVerdict(
        criterion_id="c_validation",
        state="unsatisfied",
        reason_code="no_evidence",
        output_path="path_classification",
        grounding_mode="exact_value",
        has_exact_value=True,
        missing_evidence="missing classification output key path_classification",
    )
    assert verdict_by_id["c_page"].satisfied is True


@pytest.mark.parametrize(
    "criterion",
    [
        _criterion(
            "c_validation",
            "The run classifies whether the path is login gated.",
            kind="validation_classification",
            expected_classification="login_gated",
        ),
        _criterion(
            "c_validation",
            "The run classifies whether the path is login gated.",
            kind="validation_classification",
            classification_output_key="path_classification",
        ),
        _criterion(
            "c_validation",
            "The run classifies whether the path is login gated.",
            kind="validation_classification",
            output_path="output.path_classification",
            expected_output_value="login_gated",
        ),
    ],
)
@pytest.mark.asyncio
async def test_page_observation_validation_classification_incomplete_contract_cannot_be_judge_approved(
    monkeypatch: pytest.MonkeyPatch,
    criterion: CompletionCriterion,
) -> None:
    handler_calls = 0

    async def handler(**_: object) -> dict:
        nonlocal handler_calls
        handler_calls += 1
        return {
            "verdicts": [
                {"criterion_id": "c_validation", "satisfied": True, "reason_code": "evidence_confirms"},
                {"criterion_id": "c_page", "satisfied": True, "reason_code": "evidence_confirms"},
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("path_classification")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            criterion,
            _criterion("c_page", "The page observation confirms the path was inspected."),
        ]
    )
    ctx.last_test_ok = False
    ctx.last_run_blocks_workflow_run_id = "wr_cancel"
    ctx.copilot_run_start_monotonic = time.monotonic()
    _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url="https://example.test/login",
        title="Login",
        observed_data={"path_classification": "login_gated"},
    )

    result = await _maybe_run_completion_verification_from_page_observation(
        ctx,
        url="https://example.test/login",
        title="Login",
        observed_data={"path_classification": "login_gated"},
    )

    assert handler_calls == 1
    assert result is not None
    assert result.is_fully_satisfied() is False
    verdict_by_id = {verdict.criterion_id: verdict for verdict in result.verdicts}
    assert verdict_by_id["c_validation"].state == "unsatisfied"
    assert verdict_by_id["c_validation"].reason_code == "no_evidence"
    assert verdict_by_id["c_validation"].missing_evidence == "incomplete typed classification contract"
    assert verdict_by_id["c_page"].satisfied is True


@pytest.mark.asyncio
async def test_page_observation_verification_does_not_apply_terminal_goal_record_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {
            "verdicts": [
                {
                    "criterion_id": "c0",
                    "satisfied": False,
                    "reason_code": "no_evidence",
                }
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "a commercial water service request is submitted",
                kind="terminal_action",
                terminal_action_family="request",
            )
        ]
    )
    ctx.last_test_ok = False
    ctx.last_run_blocks_workflow_run_id = "wr_cancel"
    ctx.copilot_run_start_monotonic = time.monotonic()
    _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url="https://example.com/confirmation",
        observed_data=_terminal_goal_payload(),
    )

    result = await _maybe_run_completion_verification_from_page_observation(
        ctx,
        url="https://example.com/confirmation",
        observed_data=_terminal_goal_payload(),
    )

    assert result is not None
    assert result.is_fully_satisfied() is False


@pytest.mark.asyncio
async def test_page_observation_verification_does_not_overwrite_satisfied_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        raise AssertionError("handler should not be called once the outcome is verified")

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.last_test_ok = False
    ctx.last_run_blocks_workflow_run_id = "wr_cancel"
    existing = _evaluated(("c0", True))
    ctx.completion_verification_result = existing
    _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url="https://example.com/cart",
        observed_data={"hasProduct": True},
    )

    result = await _maybe_run_completion_verification_from_page_observation(
        ctx,
        url="https://example.com/cart",
        observed_data={"hasProduct": True},
    )

    assert result is existing
    assert ctx.completion_verification_result is existing


@pytest.mark.asyncio
async def test_page_observation_verification_preserves_existing_unsatisfied_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_calls = 0

    async def handler(**_: object) -> dict:
        nonlocal handler_calls
        handler_calls += 1
        return {"verdicts": [{"criterion_id": "c0", "satisfied": False, "reason_code": "no_evidence"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.last_test_ok = False
    ctx.last_run_blocks_workflow_run_id = "wr_cancel"
    existing = _evaluated(("c0", False))
    ctx.completion_verification_result = existing
    _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url="https://example.com/cart",
        observed_data={"hasProduct": False},
    )

    result = await _maybe_run_completion_verification_from_page_observation(
        ctx,
        url="https://example.com/cart",
        observed_data={"hasProduct": False},
    )

    assert handler_calls == 1
    assert result is existing
    assert ctx.completion_verification_result is existing


@pytest.mark.asyncio
async def test_page_observation_verification_can_upgrade_unsatisfied_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.last_test_ok = False
    ctx.last_run_blocks_workflow_run_id = "wr_cancel"
    existing = _evaluated(("c0", False))
    ctx.completion_verification_result = existing
    _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url="https://example.com/cart",
        observed_data={"hasProduct": True},
    )

    result = await _maybe_run_completion_verification_from_page_observation(
        ctx,
        url="https://example.com/cart",
        observed_data={"hasProduct": True},
    )

    assert result is not None
    assert result is not existing
    assert result.is_fully_satisfied() is True
    assert ctx.completion_verification_result is result


def test_failed_test_rewrite_recognizes_post_budget_verified_outcome() -> None:
    ctx = _canceled_gate_ctx()
    ctx.last_workflow = SimpleNamespace()
    ctx.last_workflow_yaml = "workflow: {}"
    ctx.last_update_block_count = 5
    ctx.completion_verification_result = _evaluated(("c0", True))

    response = _rewrite_failed_test_response("The test failed.", ctx)

    assert "verified the requested outcome" in response
    assert "test failed" not in response.lower()


def test_failed_test_rewrite_does_not_render_zero_block_verified_outcome() -> None:
    ctx = _canceled_gate_ctx()
    ctx.last_workflow = SimpleNamespace()
    ctx.last_workflow_yaml = "workflow: {}"
    ctx.last_update_block_count = 0
    ctx.completion_verification_result = _evaluated(("c0", True))

    response = _rewrite_failed_test_response("The test failed.", ctx)

    assert "0 blocks" not in response
    assert "workflow with 0" not in response


# --- SKY-10576: recognition governed by whole-workflow outcome, not per-block prefix ---
#
# A clean ok=True run can reach the goal (its outcome block produced data and the
# browser is on the goal page) while earlier block labels are not in the verified
# end-to-end prefix. Recognition must come from the outcome judge, not from whether
# every block was verified as a prefix; otherwise an achieved goal is hedged as an
# "unverified draft" and the agent overruns (SKY-10576, confirmed in live QA).


def _ctx_unverified_prefix() -> CopilotContext:
    ctx = _run_ctx()
    blocks = [
        SimpleNamespace(block_type="navigation", label="b0"),
        SimpleNamespace(block_type="navigation", label="b1"),
        SimpleNamespace(block_type="navigation", label="b2"),
        SimpleNamespace(block_type="extraction", label="b3"),
    ]
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=blocks))
    # Only the suffix is in the verified prefix; the goal was reached on the final
    # incremental run, but b0/b1 never entered the prefix.
    ctx.verified_prefix_labels = ["b2", "b3"]
    ctx.verified_block_outputs = {"b3": {"one_star_review_text": "For the life of me..."}}
    return ctx


def _empty_data_result() -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_empty",
            "overall_status": "completed",
            "current_url": "https://example.com/reviews",
            "blocks": [{"label": "confirm", "block_type": "EXTRACTION", "status": "completed"}],
        },
    }


def test_outcome_evidence_candidate_admits_clean_run_despite_unverified_prefix() -> None:
    ctx = _ctx_unverified_prefix()
    # A clean run is admitted for the judge even though b0/b1 are not in the verified
    # prefix -- recognition is governed by the outcome judge, not the per-block prefix.
    assert _is_outcome_evidence_candidate(ctx, _clean_success_result()) is True
    # An empty-data completed run is admitted for the judge (the judge requires positive
    # evidence per criterion, so it grades unsatisfied); only ok=False runs are rejected.
    assert _is_outcome_evidence_candidate(ctx, _empty_data_result()) is True
    assert _is_outcome_evidence_candidate(ctx, {"ok": False, "data": {}}) is False


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_runs_on_unverified_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_unverified_prefix()
    result = await _maybe_run_completion_verification(ctx, _clean_success_result(), time.monotonic())
    assert result is not None
    assert result.status == "evaluated"
    assert result.is_fully_satisfied() is True


@pytest.mark.parametrize("reason_code", ["evidence_contradicts", "no_evidence", "unknown"])
@pytest.mark.asyncio
async def test_mixed_completion_verification_preserves_structural_unfired_contingent_ids(
    monkeypatch: pytest.MonkeyPatch, reason_code: str
) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c1", "satisfied": False, "reason_code": reason_code}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _ctx_with_blocks("code")
    _set_workflow_labels(ctx, "submit_water_request")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "a commercial water service request is submitted",
                kind="terminal_action",
                terminal_action_family="request",
            ),
            _criterion(
                "c1",
                "A provider blocker is reported to the user.",
                contingent_on="the provider site blocks online submission",
                contingent_antecedent_output_path="output.blocker",
            ),
        ]
    )

    result = await _maybe_run_completion_verification(
        ctx, _terminal_goal_output_result(blocker_output=None), time.monotonic()
    )

    assert result is not None
    assert result.contingent_criterion_ids == ["c1"]
    assert result.contingent_on_by_criterion_id == {"c1": "the provider site blocks online submission"}
    assert result.contingent_antecedent_output_path_by_criterion_id == {"c1": "output.blocker"}
    assert result.structural_unfired_criterion_ids == ["c1"]
    assert result.is_fully_satisfied() is True


def test_gate_recognizes_clean_run_despite_unverified_prefix() -> None:
    ctx = _ctx_unverified_prefix()
    # The full-workflow run-status latch is False (incremental run), yet the judge
    # confirmed the outcome: recognition must fire on the evidence.
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.completion_verification_result = _evaluated(("c0", True))
    assert outcome_fully_verified(ctx) is True
    assert verified_goal_satisfied_context(ctx) is True


# --- Review hardening: method-mandated criteria, per-run evidence, fail-closed ---


@pytest.mark.asyncio
async def test_method_mandated_criteria_excluded_from_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion("c0", "item in cart"),
            _criterion("c1", "use the search bar", method_mandated=True),
        ]
    )
    result = await _maybe_run_completion_verification(ctx, _clean_success_result(), time.monotonic())
    # The method-mandated criterion is not sent to the end-state judge (it could only
    # ever return no_evidence), so it cannot false-block a legitimate success.
    assert result is not None
    assert result.criterion_ids == ["c0"]
    assert result.is_fully_satisfied() is True


def test_snapshot_uses_current_run_blocks_not_stale_outputs() -> None:
    ctx = _ctx_unverified_prefix()  # verified_block_outputs carries a stale b3 from a prior run
    stale_run = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_now",
            "current_url": "https://example.com/x",
            "executed_block_labels": ["b0"],
            "blocks": [{"label": "b0", "block_type": "NAVIGATION", "status": "completed"}],
        },
    }
    snap = _build_run_evidence_snapshot(ctx, stale_run)
    # A prior run's output must not leak in as this run's evidence.
    assert "b3" not in snap.block_outputs
    assert snap.block_outputs == {}
    fresh_run = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_now",
            "current_url": "https://example.com/x",
            "executed_block_labels": ["b3"],
            "blocks": [
                {
                    "label": "b3",
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {"extracted_information": {"price": "9.99"}},
                }
            ],
        },
    }
    snap2 = _build_run_evidence_snapshot(ctx, fresh_run)
    assert snap2.block_outputs.get("b3") == {"extracted_information": {"price": "9.99"}}


def test_snapshot_indexes_workflow_output_parameter_records() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(
        ctx,
        "open_search_search_page",
        "search_and_open_record_details",
        "extract_record_status_record",
    )
    run = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_structured_record",
            "blocks": [
                {
                    "label": "open_search_search_page",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {
                        "open_search_search_page_output": {"evidence_text": "Opened search search page"}
                    },
                },
                {
                    "label": "search_and_open_record_details",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {
                        "search_and_open_record_details_output": {
                            "entity_found": True,
                            "evidence_text": "Opened Details page",
                        }
                    },
                },
                {
                    "label": "extract_record_status_record",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": {"extract_record_status_record_output": _record_payload(evidence_text=None)},
                },
            ],
        },
    }

    snap = _build_run_evidence_snapshot(ctx, run)

    assert "open_search_search_page_output" in snap.block_outputs
    assert snap.block_outputs["search_and_open_record_details_output"]["evidence_text"] == ("Opened Details page")
    assert snap.block_outputs["extract_record_status_record_output"]["record_number"] == "1234567890"


def test_snapshot_uses_current_run_registered_output_parameters() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_record_status_details")
    run = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_structured_record",
            "overall_status": "completed",
            "blocks": [],
            "registered_output_parameter_values": [
                {
                    "workflow_run_id": "wr_structured_record",
                    "output_parameter_id": "op_record",
                    "output_parameter_key": "extract_record_status_details_output",
                    "block_label": "extract_record_status_details",
                    "block_type": "CODE",
                    "value": _record_payload(evidence_text="Opened Details page"),
                }
            ],
        },
    }

    snap = _build_run_evidence_snapshot(ctx, run)
    verdicts = grade_structured_record_criteria(_structured_record_criteria(), snap)

    assert snap.block_outputs["extract_record_status_details_output"]["record_number"] == "1234567890"
    assert (
        snap.block_outputs["extract_record_status_details"]["extract_record_status_details_output"]["record_number"]
        == "1234567890"
    )
    assert snap.block_output_sources["extract_record_status_details_output"] == "registered_output_parameter"
    assert snap.block_output_sources["extract_record_status_details"] == "registered_output_parameter"
    assert _satisfied_criterion_ids(verdicts) == _STRUCTURED_RECORD_CRITERION_IDS


def test_snapshot_uses_structured_record_top_level_output_parameters() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_record_status_record")

    snap = _build_run_evidence_snapshot(ctx, _structured_record_top_level_output_result())
    verdicts = grade_structured_record_criteria(_structured_record_criteria(), snap)

    assert snap.block_outputs["extract_record_status_record_output"]["record_number"] == "1234567890"
    assert _satisfied_criterion_ids(verdicts) == _STRUCTURED_RECORD_CRITERION_IDS


@pytest.mark.asyncio
async def test_requested_output_path_ignores_evidence_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("requested-output criteria must not reach the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi")]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"evidence_text": "The provider NPI is 1234567890."}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert verification.verdicts[0].reason_code == "no_evidence"
    assert (
        verification.verdicts[0].missing_evidence
        == "requested-output criterion lacks typed expected_output_value or expected_output_shape; "
        "presence-only output cannot confirm value-grounded criterion"
    )


@pytest.mark.asyncio
async def test_requested_output_path_ignores_block_level_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("requested-output criteria must not reach the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi")]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result("The provider NPI is 1234567890."),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert verification.verdicts[0].reason_code == "no_evidence"


@pytest.mark.asyncio
async def test_requested_output_path_exact_runtime_field_with_expected_value_satisfies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("exact requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_npi",
                outcome="The NPI is returned.",
                output_path="output.npi",
                expected_output_value="1234567890",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"npi": "1234567890"}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == "block_outputs:extract_profile.npi"

    trace = verification.to_trace_data()
    assert trace["verdict_0_criterion_id"] == "c_npi"
    assert trace["verdict_0_output_path"] == "output.npi"
    assert trace["verdict_0_grounding_mode"] == "exact_value"
    assert "verdict_0_expected_output_shape" not in trace
    assert trace["verdict_0_has_exact_value"] is True
    assert "1234567890" not in repr(trace)


def test_requested_output_independent_evidence_source_does_not_self_attest() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("best_option_selected")
    criteria = [
        _criterion(
            "c_best_option",
            "The returned record selects the best option.",
            output_path="output.best_option_selected",
            expected_output_value="true",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"extract_profile": {"best_option_selected": True}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].evidence_ref == "block_outputs:extract_profile.best_option_selected"
    assert verdicts[0].requested_output_evidence_source == "independent_run_evidence"
    assert verdicts[0].self_emitted_judgment_not_independent is True

    trace = CompletionVerificationResult(status="evaluated", criterion_ids=["c_best_option"], verdicts=verdicts)
    assert trace.to_trace_data()["verdict_0_self_emitted_judgment_not_independent"] is True


def test_shape_judgment_self_emitted_from_runtime_engages_veto_without_exact_value() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("selected_highest_priority")
    criteria = [
        _criterion(
            "c_selected",
            "The highest-priority document was correctly selected.",
            output_path="output.selected_highest_priority",
            expected_output_shape="goal_judgment_boolean",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"extract_profile": {"selected_highest_priority": True}},
            block_output_sources={"extract_profile": "runtime_output"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].grounding_mode == "judgment_boolean"
    assert verdicts[0].self_emitted_judgment_not_independent is True


def test_presence_only_extraction_typed_independent_by_classifier_stays_veto_exempt() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("document_name")
    criteria = [
        _criterion(
            "c_document_name",
            "The highest-priority document name is returned.",
            output_path="output.document_name",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"extract_profile": {"document_name": "Selected Document.pdf"}},
            block_output_sources={"extract_profile": "runtime_output"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].grounding_mode == "missing"
    assert verdicts[0].self_emitted_judgment_not_independent is False


def _metadata_with_declared_independent_criterion(label: str, output_path: str) -> dict[str, Any]:
    normalized = output_path.removeprefix("output.")
    return {
        label: {
            "claimed_outcomes": [{"goal_value_paths": [normalized]}],
            "completion_criteria": [
                {"output_path": output_path, "requested_output_evidence_source": "independent_run_evidence"}
            ],
        }
    }


def test_producer_declared_independent_source_engages_veto_despite_classifier_string_value() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_with_declared_independent_criterion(
        "select_document", "output.selected_highest_priority"
    )
    criteria = [
        _criterion(
            "c_selected",
            "The highest-priority document was correctly selected.",
            output_path="output.selected_highest_priority",
            expected_output_value="true",
            requested_output_evidence_source="runtime_output",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"select_document": {"selected_highest_priority": True}},
            block_output_sources={"select_document": "runtime_output"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].self_emitted_judgment_not_independent is True
    assert verdicts[0].requested_output_evidence_source == "independent_run_evidence"


def test_presence_only_producer_declared_independent_path_engages_veto() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_with_declared_independent_criterion(
        "select_document", "output.selected_highest_priority"
    )
    criteria = [
        _criterion(
            "c_selected",
            "The highest-priority document was correctly selected.",
            output_path="output.selected_highest_priority",
            requested_output_evidence_source="runtime_output",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"select_document": {"selected_highest_priority": True}},
            block_output_sources={"select_document": "runtime_output"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].grounding_mode == "missing"
    assert verdicts[0].self_emitted_judgment_not_independent is True


def test_producer_declared_boolean_schema_engages_veto_despite_classifier_string_value() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {
        "select_document": {
            "claimed_outcomes": [
                {
                    "goal_value_paths": ["selected_highest_priority"],
                    "extraction_schema": {
                        "type": "object",
                        "properties": {"selected_highest_priority": {"type": "boolean"}},
                    },
                }
            ],
        }
    }
    criteria = [
        _criterion(
            "c_selected",
            "The highest-priority document was correctly selected.",
            output_path="output.selected_highest_priority",
            expected_output_value="true",
            requested_output_evidence_source="runtime_output",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"select_document": {"selected_highest_priority": True}},
            block_output_sources={"select_document": "runtime_output"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].self_emitted_judgment_not_independent is True


def test_schema_boolean_output_paths_captures_array_of_boolean_leaf() -> None:
    schema = {
        "type": "object",
        "properties": {"judgments": {"type": "array", "items": {"type": "boolean"}}},
    }
    assert _schema_boolean_output_paths(schema) == {"judgments[]"}


def test_array_of_boolean_judgment_path_self_emitted_from_runtime_abstains() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {
        "select_document": {
            "claimed_outcomes": [
                {
                    "goal_value_paths": ["judgments[]"],
                    "extraction_schema": {
                        "type": "object",
                        "properties": {"judgments": {"type": "array", "items": {"type": "boolean"}}},
                    },
                }
            ],
        }
    }
    criteria = [
        _criterion(
            "c_selected",
            "Each candidate was judged against the highest-priority rule.",
            output_path="output.judgments[]",
            expected_output_value="true",
            requested_output_evidence_source="runtime_output",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"select_document": {"judgments": [True]}},
            block_output_sources={"select_document": "runtime_output"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].self_emitted_judgment_not_independent is True


def test_producer_declared_independent_criterion_stays_in_requested_output_grader_split() -> None:
    criterion = _criterion(
        "c_selected",
        "The highest-priority document was correctly selected.",
        output_path="output.selected_highest_priority",
        expected_output_value="true",
        requested_output_evidence_source="runtime_output",
    )
    requested, remaining = split_requested_output_criteria([criterion])
    assert requested == [criterion]
    assert remaining == []


def test_producer_declared_independent_source_still_satisfies_with_independent_corroboration() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_with_declared_independent_criterion(
        "select_document", "output.selected_highest_priority"
    )
    criteria = [
        _criterion(
            "c_selected",
            "The highest-priority document was correctly selected.",
            output_path="output.selected_highest_priority",
            expected_output_value="true",
            requested_output_evidence_source="runtime_output",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"page_probe": {"selected_highest_priority": True}},
            block_output_sources={"page_probe": "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_source == "independent_page_evidence"


def test_runtime_site_datum_still_self_confirms_when_other_path_declared_independent() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["status", "selected_highest_priority"]}],
            "completion_criteria": [
                {
                    "output_path": "output.selected_highest_priority",
                    "requested_output_evidence_source": "independent_run_evidence",
                }
            ],
        }
    }
    criteria = [
        _criterion(
            "c_status",
            "The returned record includes status.",
            output_path="output.status",
            expected_output_value="Active",
            requested_output_evidence_source="runtime_output",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"extract_profile": {"status": "Active"}},
            block_output_sources={"extract_profile": "runtime_output"},
        ),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].requested_output_evidence_source == "runtime_output"


@pytest.mark.asyncio
async def test_admitted_code_artifact_metadata_propagates_requested_output_evidence_source() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    criteria = [
        _criterion(
            "c_document_name",
            "The returned document_name is the highest-priority selected document.",
            output_path="output.document_name",
            expected_output_value="Selected Document.pdf",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    _admit_code_artifact_metadata_for_test(
        ctx,
        block_label="extract_profile",
        completion_criteria=criteria,
    )
    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"document_name": "Selected Document.pdf"}),
        time.monotonic(),
    )

    assert verification is not None
    assert ctx.code_artifact_metadata["extract_profile"]["claimed_outcomes"][0]["goal_value_paths"] == ["document_name"]
    assert (
        ctx.code_artifact_metadata["extract_profile"]["completion_criteria"][0]["requested_output_evidence_source"]
        == "independent_run_evidence"
    )
    assert ctx.request_policy.completion_criteria[0].requested_output_evidence_source == "independent_run_evidence"
    verdict = verification.verdicts[0]
    assert verdict.criterion_id == "c_document_name"
    assert verdict.state == "unsatisfied"
    assert verdict.reason_code == "structurally_abstained"
    assert verdict.requested_output_evidence_source == "independent_run_evidence"
    assert verdict.self_emitted_judgment_not_independent is True


def test_requested_output_runtime_output_source_still_self_confirms_site_datum() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("status")
    criteria = [
        _criterion(
            "c_status",
            "The returned record includes status.",
            output_path="output.status",
            expected_output_value="Active",
            requested_output_evidence_source="runtime_output",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"extract_profile": {"status": "Active"}},
            block_output_sources={"extract_profile": "runtime_output"},
        ),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].requested_output_evidence_source == "runtime_output"
    assert verdicts[0].evidence_source == "runtime_output"


def test_registered_output_parameter_source_can_confirm_requested_output() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        _criterion(
            "c_confirmation_number",
            "The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"extract_profile": {"confirmation_number": "WTR-1842-DEMO"}},
            block_output_sources={"extract_profile": "registered_output_parameter"},
        ),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_source == "registered_output_parameter"


def test_registered_artifact_content_source_is_traceable() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_artifact"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_artifact",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="registered_artifact_content:artifact_1",
                evidence_source="registered_artifact_content",
            )
        ],
    )

    assert result.to_trace_data()["verdict_0_evidence_source"] == "registered_artifact_content"


@pytest.mark.parametrize("emitted_value", ["page is visible.", "Water Service Request Submitted"])
def test_requested_output_independent_page_evidence_text_refutes_wrong_confirmation_number(
    emitted_value: str,
) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        _criterion(
            "c_confirmation_number",
            "The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "extract_profile": {
                    "confirmation_number": emitted_value,
                    "evidence_text": "Water Service Request Submitted. Confirmation Number WTR-1842-DEMO.",
                }
            },
            block_output_sources={"extract_profile": "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == "block_outputs:extract_profile.evidence_text"
    assert verdicts[0].evidence_source == "independent_page_evidence"


def test_requested_output_independent_page_evidence_text_alone_does_not_confirm() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        _criterion(
            "c_confirmation_number",
            "The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "extract_profile": {
                    "evidence_text": "Water Service Request Submitted. Confirmation Number WTR-1842-DEMO.",
                }
            },
            block_output_sources={"extract_profile": "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"
    assert verdicts[0].evidence_source is None


def test_requested_output_independent_evidence_source_keeps_contradiction_hard() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("best_option_selected")
    criteria = [
        _criterion(
            "c_best_option",
            "The returned record selects the best option.",
            output_path="output.best_option_selected",
            expected_output_value="true",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"extract_profile": {"best_option_selected": False}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == "block_outputs:extract_profile.best_option_selected"


def test_requested_output_independent_evidence_source_ignores_unrelated_observed_end_state() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_best_option", "c_reach"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_best_option",
                state="unsatisfied",
                reason_code="structurally_abstained",
                requested_output_evidence_source="independent_run_evidence",
                self_emitted_judgment_not_independent=True,
            ),
            CriterionVerdict(
                criterion_id="c_reach",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="observed_end_state_url",
            ),
        ],
    )

    assert result.is_fully_satisfied() is False


def test_requested_output_independent_evidence_source_ignores_unrelated_corroborator() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_best_option", "c_other__requested_output_corroborator_2"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_best_option",
                state="unsatisfied",
                reason_code="structurally_abstained",
                requested_output_evidence_source="independent_run_evidence",
                self_emitted_judgment_not_independent=True,
            ),
            CriterionVerdict(
                criterion_id="c_other__requested_output_corroborator_2",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="run_plane:other_row",
            ),
        ],
    )

    assert result.is_fully_satisfied() is False


@pytest.mark.parametrize("evidence_source", ["runtime_output", "same_record_context"])
def test_requested_output_independent_evidence_source_rejects_non_independent_corroborator(
    evidence_source: str,
) -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_best_option", "c_best_option__requested_output_corroborator"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_best_option",
                state="unsatisfied",
                reason_code="structurally_abstained",
                requested_output_evidence_source="independent_run_evidence",
                self_emitted_judgment_not_independent=True,
            ),
            CriterionVerdict(
                criterion_id="c_best_option__requested_output_corroborator",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="block_outputs:story.best_option_selected",
                evidence_source=evidence_source,  # type: ignore[arg-type]
            ),
        ],
    )

    assert result.is_fully_satisfied() is False


@pytest.mark.parametrize(
    "evidence_source",
    ["independent_page_evidence", "registered_output_parameter", "registered_artifact_content"],
)
def test_requested_output_independent_evidence_source_allows_independent_corroborator(
    evidence_source: str,
) -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_best_option", "c_best_option__requested_output_corroborator"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_best_option",
                state="unsatisfied",
                reason_code="structurally_abstained",
                requested_output_evidence_source="independent_run_evidence",
                self_emitted_judgment_not_independent=True,
            ),
            CriterionVerdict(
                criterion_id="c_best_option__requested_output_corroborator",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="run_plane:best_option_row",
                evidence_source=evidence_source,  # type: ignore[arg-type]
            ),
        ],
    )

    assert result.is_fully_satisfied() is True


def test_requested_output_independent_evidence_source_allows_corroborated_success() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_best_option", "c_best_option__requested_output_corroborator"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_best_option",
                state="unsatisfied",
                reason_code="structurally_abstained",
                requested_output_evidence_source="independent_run_evidence",
                self_emitted_judgment_not_independent=True,
            ),
            CriterionVerdict(
                criterion_id="c_best_option__requested_output_corroborator",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="run_plane:best_option_row",
                evidence_source="independent_page_evidence",
            ),
        ],
    )

    assert result.is_fully_satisfied() is True


@pytest.mark.asyncio
async def test_requested_output_typed_block_fields_do_not_admit_undeclared_returned_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("typed requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("address", "credentialing_status", "locations")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_address",
                outcome="The returned profile includes address.",
                output_path="output.address",
                expected_output_value="101 Example Ave",
            ),
            CompletionCriterion(
                id="c_credentialing_status",
                outcome="The returned profile includes credentialing status.",
                output_path="output.credentialing_status",
                expected_output_value="Active",
            ),
            CompletionCriterion(
                id="c_locations",
                outcome="The returned profile includes location address.",
                output_path="output.locations.address",
                expected_output_value="101 Example Ave",
            ),
            CompletionCriterion(
                id="c_statuses",
                outcome="The returned profile includes statuses.",
                output_path="output.statuses",
                expected_output_value="Active",
            ),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result(
            {
                "address": "101 Example Ave",
                "credentialing_status": "Active",
                "locations": [{"address": "101 Example Ave"}],
                "statuses": ["Active"],
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert {verdict.criterion_id for verdict in verification.verdicts if verdict.satisfied} == {
        "c_address",
        "c_credentialing_status",
        "c_locations",
    }
    statuses = next(verdict for verdict in verification.verdicts if verdict.criterion_id == "c_statuses")
    assert statuses.state == "unsatisfied"
    assert statuses.reason_code == "unproducible"
    assert statuses.evidence_ref is None
    assert {
        verdict.evidence_ref for verdict in verification.verdicts if verdict.criterion_id in {"c_address", "c_statuses"}
    } == {"block_outputs:extract_profile.address", None}


def test_requested_output_nested_declared_output_root_maps_to_requested_output_path() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("profile.address")
    criteria = [
        CompletionCriterion(
            id="c_address",
            outcome="The returned profile includes address.",
            output_path="output.address",
            expected_output_value="101 Example Ave",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"extract_profile": {"profile": {"address": "101 Example Ave"}}}),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_ref == "block_outputs:extract_profile.profile.address"


@pytest.mark.parametrize(
    ("block_outputs", "expected_ref"),
    [
        (
            {"extract_profile": {"status": "Active"}, "extract_profile_output": {"status": "Expired"}},
            "block_outputs:extract_profile_output.status",
        ),
        (
            {"extract_profile": {"status": "Expired"}, "extract_profile_output": {"status": "Active"}},
            "block_outputs:extract_profile.status",
        ),
    ],
)
def test_requested_output_exact_value_same_target_contradiction_beats_matching_value(
    block_outputs: dict[str, dict[str, str]],
    expected_ref: str,
) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("status")
    criteria = [
        CompletionCriterion(
            id="c_status",
            outcome="The returned profile includes status.",
            output_path="output.status",
            expected_output_value="Active",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs=block_outputs),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == expected_ref


@pytest.mark.parametrize(
    ("accepted_payload", "expected_reason", "expected_ref"),
    [
        ({"address": "Wrong Example Ave"}, "evidence_contradicts", "block_outputs:extract_profile.address"),
        ({"status": "active"}, "missing_exact_field", None),
    ],
)
def test_requested_output_ignores_unrelated_block_outputs_when_matching_values(
    accepted_payload: dict[str, str],
    expected_reason: str,
    expected_ref: str | None,
) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("address")
    criteria = [
        CompletionCriterion(
            id="c_address",
            outcome="The returned profile includes address.",
            output_path="output.address",
            expected_output_value="101 Example Ave",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "extract_profile": accepted_payload,
                "unrelated_lookup": {"address": "101 Example Ave"},
            }
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == expected_reason
    assert verdicts[0].evidence_ref == expected_ref


def test_requested_output_string_distinct_independent_page_evidence_refutes_matching_self_emission() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        _criterion(
            "c_confirmation_number",
            "The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "extract_profile": {"confirmation_number": "WTR-1842-DEMO"},
                "current_page_observation": {"confirmation_number": "WTR-9999-WRONG"},
            },
            block_output_sources={
                "extract_profile": "runtime_output",
                "current_page_observation": "independent_page_evidence",
            },
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == "block_outputs:current_page_observation.confirmation_number"
    assert verdicts[0].evidence_source == "independent_page_evidence"


def test_requested_output_string_distinct_independent_corroborator_mismatch_contradicts() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        _criterion(
            "c_confirmation_number",
            "The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-0000-BAD",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_citrus_turn_on": {"confirmation_number": "WTR-0000-BAD"},
                "current_page_observation": {"confirmation_number": "WTR-1842-DEMO"},
            },
            block_output_sources={
                "utility_citrus_turn_on": "runtime_output",
                "current_page_observation": "independent_page_evidence",
            },
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == "block_outputs:current_page_observation.confirmation_number"
    assert verdicts[0].evidence_source == "independent_page_evidence"


def test_requested_output_exact_value_unrelated_sibling_does_not_refute_matching_target() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("status")
    criteria = [
        CompletionCriterion(
            id="c_status",
            outcome="The returned profile includes status.",
            output_path="output.status",
            expected_output_value="Active",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"extract_profile": {"status": "Active", "previous_status": "Expired"}}),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_ref == "block_outputs:extract_profile.status"


def test_requested_output_evidence_text_with_expected_value_does_not_satisfy() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("address")
    criteria = [
        CompletionCriterion(
            id="c_address",
            outcome="The returned profile includes address.",
            output_path="output.address",
            expected_output_value="101 Example Ave",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"extract_profile": {"evidence_text": "Address: 101 Example Ave"}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"


def test_requested_output_roots_do_not_satisfy_from_executed_block_output_without_static_metadata_roots() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {}
    criteria = [
        CompletionCriterion(
            id="c_npi",
            outcome="The returned profile includes NPI.",
            output_path="output.npi",
            expected_output_value="1234567890",
        ),
        CompletionCriterion(
            id="c_address",
            outcome="The returned profile includes address.",
            output_path="output.address",
            expected_output_value="101 Example Ave",
        ),
        CompletionCriterion(
            id="c_credentialing_status",
            outcome="The returned profile includes credentialing status.",
            output_path="output.credentialing_status",
            expected_output_value="Active",
        ),
        CompletionCriterion(
            id="c_locations",
            outcome="The returned profile includes locations.",
            output_path="output.locations.address",
            expected_output_value="101 Example Ave",
        ),
        CompletionCriterion(
            id="c_statuses",
            outcome="The returned profile includes statuses.",
            output_path="output.statuses",
            expected_output_value="Active",
        ),
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            executed_block_labels=["extract_profile"],
            block_outputs={
                "extract_profile": {
                    "npi": "1234567890",
                    "address": "101 Example Ave",
                    "credentialing_status": "Active",
                    "locations": [{"address": "101 Example Ave"}],
                    "statuses": ["Active"],
                }
            },
        ),
    )

    assert {verdict.criterion_id for verdict in verdicts} == {
        "c_npi",
        "c_address",
        "c_credentialing_status",
        "c_locations",
        "c_statuses",
    }
    assert {verdict.state for verdict in verdicts} == {"unsatisfied"}
    assert {verdict.reason_code for verdict in verdicts} == {"unproducible"}
    assert all(verdict.evidence_ref is None for verdict in verdicts)


def test_requested_output_roots_without_expected_values_abstain_from_runtime_presence() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("address", "statuses")
    criteria = [
        CompletionCriterion(
            id="c_address",
            outcome="The returned profile includes address.",
            output_path="output.address",
        ),
        CompletionCriterion(
            id="c_statuses",
            outcome="The returned profile includes statuses.",
            output_path="output.statuses",
        ),
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            executed_block_labels=["extract_profile"],
            block_outputs={"extract_profile": {"address": "101 Example Ave", "statuses": ["Active"]}},
        ),
    )

    assert {verdict.criterion_id for verdict in verdicts} == {"c_address", "c_statuses"}
    assert {verdict.state for verdict in verdicts} == {"unsatisfied"}
    assert {verdict.reason_code for verdict in verdicts} == {"structurally_abstained"}
    assert {verdict.evidence_ref for verdict in verdicts} == {
        "block_outputs:extract_profile.address",
        "block_outputs:extract_profile.statuses",
    }


def test_requested_output_executed_root_admission_ignores_evidence_text_only_values() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {}
    criteria = [
        CompletionCriterion(
            id="c_npi",
            outcome="The returned profile includes NPI.",
            output_path="output.npi",
            expected_output_value="1234567890",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            executed_block_labels=["extract_profile"],
            block_outputs={"extract_profile": {"evidence_text": "The provider NPI is 1234567890."}},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "unproducible"


def test_requested_output_executed_root_admission_ignores_unexecuted_block_outputs() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {}
    criteria = [
        CompletionCriterion(
            id="c_address",
            outcome="The returned profile includes address.",
            output_path="output.address",
            expected_output_value="101 Example Ave",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            executed_block_labels=["extract_profile"],
            block_outputs={
                "extract_profile": {"status": "Active"},
                "unrelated_lookup": {"address": "101 Example Ave"},
            },
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "unproducible"


def test_requested_output_missing_metadata_and_runtime_field_fails_closed() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("address")
    criteria = [
        CompletionCriterion(
            id="c_statuses",
            outcome="The returned profile includes statuses.",
            output_path="output.statuses",
            expected_output_value="Active",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"extract_profile": {"address": "101 Example Ave"}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "unproducible"
    assert verdicts[0].missing_evidence == "accepted code artifact metadata does not declare output.statuses"


def test_requested_output_path_without_expected_value_abstains_for_present_typed_values() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths(
        "request_id",
        "provider_captured_address",
        "requested_date",
        "status",
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_request_id",
                outcome="The returned record includes request id.",
                output_path="output.request_id",
            ),
            CompletionCriterion(
                id="c_provider_captured_address",
                outcome="The returned record includes provider captured address.",
                output_path="output.provider_captured_address",
            ),
            CompletionCriterion(
                id="c_requested_date",
                outcome="The returned record includes requested date.",
                output_path="output.requested_date",
            ),
            CompletionCriterion(
                id="c_status",
                outcome="The returned record includes status.",
                output_path="output.status",
            ),
        ]
    )

    verdicts = grade_requested_output_criteria(
        ctx,
        ctx.request_policy.completion_criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_peach_gas_quickconnect": {
                    "request_id": "100245",
                    "provider_captured_address": "100245",
                    "requested_date": "77 Gaslight Way, Decatur, GA 30030",
                    "status": "2026-06-24",
                    "evidence_text": "The request completed successfully.",
                }
            }
        ),
    )

    assert {verdict.criterion_id for verdict in verdicts} == {
        "c_request_id",
        "c_provider_captured_address",
        "c_requested_date",
        "c_status",
    }
    assert {verdict.state for verdict in verdicts} == {"unsatisfied"}
    assert {verdict.reason_code for verdict in verdicts} == {"structurally_abstained"}
    assert {verdict.grounding_mode for verdict in verdicts} == {"missing"}
    assert all(verdict.has_exact_value is False for verdict in verdicts)


def test_requested_output_shape_only_generated_fields_structurally_abstain_without_exact_values() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths(
        "confirmation_number",
        "account_number",
        "selected_start_date",
    )
    criteria = [
        CompletionCriterion(
            id="c_confirmation_number",
            outcome="The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_shape="reference_code",
        ),
        CompletionCriterion(
            id="c_account_number",
            outcome="The returned record includes account number.",
            output_path="output.account_number",
            expected_output_shape="numeric_identifier",
        ),
        CompletionCriterion(
            id="c_selected_start_date",
            outcome="The returned record includes selected start date.",
            output_path="output.selected_start_date",
            expected_output_shape="date",
        ),
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_citrus_turn_on": {
                    "confirmation_number": "WTR-1842-DEMO",
                    "account_number": "100245",
                    "selected_start_date": "2026-06-22",
                }
            }
        ),
    )

    assert {verdict.criterion_id for verdict in verdicts} == {
        "c_confirmation_number",
        "c_account_number",
        "c_selected_start_date",
    }
    assert {verdict.state for verdict in verdicts} == {"unsatisfied"}
    assert {verdict.reason_code for verdict in verdicts} == {"structurally_abstained"}
    trace = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=[criterion.id for criterion in criteria],
        verdicts=verdicts,
    ).to_trace_data()
    assert trace["verdict_0_output_path"] == "output.confirmation_number"
    assert trace["verdict_0_grounding_mode"] == "shape"
    assert trace["verdict_0_expected_output_shape"] == "reference_code"
    assert trace["verdict_0_has_exact_value"] is False
    assert "WTR-1842-DEMO" not in repr(trace)
    assert trace["fully_satisfied"] is False
    assert trace["unmet_criterion_ids"] == [
        "c_confirmation_number",
        "c_account_number",
        "c_selected_start_date",
    ]


def test_requested_output_shape_abstains_on_p8_scrambled_values_and_ignores_evidence_text_status() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths(
        "confirmation_number",
        "provider_captured_address",
        "requested_date",
        "status",
    )
    criteria = [
        CompletionCriterion(
            id="c_confirmation_number",
            outcome="The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_shape="reference_code",
        ),
        CompletionCriterion(
            id="c_provider_captured_address",
            outcome="The returned record includes provider captured address.",
            output_path="output.provider_captured_address",
            expected_output_shape="address",
        ),
        CompletionCriterion(
            id="c_requested_date",
            outcome="The returned record includes requested date.",
            output_path="output.requested_date",
            expected_output_shape="date",
        ),
        CompletionCriterion(
            id="c_status",
            outcome="The returned record includes status.",
            output_path="output.status",
            expected_output_shape="status_label",
        ),
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_peach_gas_quickconnect": {
                    "confirmation_number": "100245",
                    "provider_captured_address": "100245",
                    "requested_date": "77 Gaslight Way, Decatur, GA 30030",
                    "status": "2026-06-24",
                    "evidence_text": "Submitted / Processing",
                }
            }
        ),
    )

    assert {verdict.criterion_id for verdict in verdicts} == {
        "c_confirmation_number",
        "c_provider_captured_address",
        "c_requested_date",
        "c_status",
    }
    assert {verdict.state for verdict in verdicts} == {"unsatisfied"}
    assert {verdict.reason_code for verdict in verdicts} == {"structurally_abstained"}


@pytest.mark.asyncio
async def test_requested_output_verifier_rejects_reconstructed_p8_scrambled_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("typed requested-output verifier should not delegate reconstructed P8 proof")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths(
        "confirmation_number",
        "account_number",
        "provider_captured_address",
        "requested_date",
        "status",
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_confirmation_number",
                "The output includes the confirmation number.",
                output_path="output.confirmation_number",
                expected_output_shape="reference_code",
            ),
            _criterion(
                "c_account_number",
                "The output includes the account number.",
                output_path="output.account_number",
                expected_output_shape="numeric_identifier",
            ),
            _criterion(
                "c_provider_captured_address",
                "The output includes the provider captured address.",
                output_path="output.provider_captured_address",
                expected_output_shape="address",
            ),
            _criterion(
                "c_requested_date",
                "The output includes the requested date.",
                output_path="output.requested_date",
                expected_output_shape="date",
            ),
            _criterion(
                "c_status",
                "The output includes the request status.",
                output_path="output.status",
                expected_output_shape="status_label",
            ),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result(
            {
                "confirmation_number": "QC-2002-DEMO",
                "account_number": "100245",
                "provider_captured_address": "100245",
                "requested_date": "77 Gaslight Way, Decatur, GA 30030",
                "status": "2026-06-24",
                "evidence_text": "Submitted / Processing",
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.status == "evaluated"
    assert verification.is_fully_satisfied() is False
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_confirmation_number"].reason_code == "structurally_abstained"
    assert verdicts["c_account_number"].reason_code == "structurally_abstained"
    for criterion_id in ("c_provider_captured_address", "c_requested_date", "c_status"):
        assert verdicts[criterion_id].state == "unsatisfied"
        assert verdicts[criterion_id].reason_code == "structurally_abstained"


@pytest.mark.asyncio
async def test_requested_output_verifier_accepts_value_correct_reconstructed_p8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("typed requested-output verifier should not delegate value-correct P8 proof")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths(
        "confirmation_number",
        "account_number",
        "provider_captured_address",
        "requested_date",
        "status",
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_confirmation_number",
                "The output includes the confirmation number.",
                output_path="output.confirmation_number",
                expected_output_shape="reference_code",
            ),
            _criterion(
                "c_account_number",
                "The output includes the account number.",
                output_path="output.account_number",
                expected_output_shape="numeric_identifier",
            ),
            _criterion(
                "c_provider_captured_address",
                "The output includes the provider captured address.",
                output_path="output.provider_captured_address",
                expected_output_shape="address",
            ),
            _criterion(
                "c_requested_date",
                "The output includes the requested date.",
                output_path="output.requested_date",
                expected_output_shape="date",
            ),
            _criterion(
                "c_status",
                "The output includes the request status.",
                output_path="output.status",
                expected_output_shape="status_label",
            ),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result(
            {
                "confirmation_number": "QC-2002-DEMO",
                "account_number": "100245",
                "provider_captured_address": "77 Gaslight Way, Decatur, GA 30030",
                "requested_date": "2026-06-24",
                "status": "Submitted / Processing",
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.status == "evaluated"
    assert verification.is_fully_satisfied() is False
    assert {verdict.reason_code for verdict in verification.verdicts} == {"structurally_abstained"}


@pytest.mark.asyncio
async def test_requested_output_verifier_accepts_p7_with_unfired_blocker_and_fee_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c_submit", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths(
        "confirmation_number",
        "account_number",
        "selected_start_date",
        "deposit_amount",
        "next_owner",
        "blocker",
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_submit",
                "The service request is submitted.",
                kind="terminal_action",
                terminal_action_family="request",
            ),
            _criterion(
                "c_confirmation_number",
                "The output includes the confirmation number.",
                output_path="output.confirmation_number",
                expected_output_shape="reference_code",
            ),
            _criterion(
                "c_account_number",
                "The output includes the account number.",
                output_path="output.account_number",
                expected_output_shape="numeric_identifier",
            ),
            _criterion(
                "c_selected_start_date",
                "The output includes the selected start date.",
                output_path="output.selected_start_date",
                expected_output_shape="date",
            ),
            _criterion(
                "c_deposit_amount",
                "The output includes the deposit amount.",
                output_path="output.deposit_amount",
                expected_output_shape="money_amount",
            ),
            _criterion(
                "c_next_owner",
                "The output includes the next owner.",
                output_path="output.next_owner",
                expected_output_shape="owner_label",
            ),
            _criterion(
                "c_blocker",
                "Any manual service blocker is reported to the user.",
                output_path="output.blocker",
                contingent_on="a manual service blocker exists",
                contingent_antecedent_output_path="output.blocker",
            ),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result(
            {
                "manual_service_blocker": None,
                "output": {
                    "confirmation_number": "WTR-1842-DEMO",
                    "account_number": "100245",
                    "selected_start_date": "2026-06-22",
                    "deposit_amount": "$41.00 plus initiation fee",
                    "next_owner": "Provider",
                },
                "evidence_text": "Water Service Request Submitted. Confirmation Number WTR-1842-DEMO.",
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.status == "evaluated"
    assert verification.is_fully_satisfied() is True
    assert verification.structural_unfired_criterion_ids == ["c_blocker"]
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_submit"].reason_code == "evidence_confirms"
    assert verdicts["c_blocker"].reason_code == "no_evidence"
    assert verdicts["c_deposit_amount"].reason_code == "structurally_abstained"
    assert all(
        verdict.reason_code == "structurally_abstained"
        for cid, verdict in verdicts.items()
        if cid not in {"c_submit", "c_blocker"}
    )


def test_requested_output_canonical_path_precedence_rejects_wrong_top_level_value() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        CompletionCriterion(
            id="c_confirmation_number",
            outcome="The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_citrus_turn_on": {
                    "confirmation_number": "QC-2002-DEMO",
                    "output": {"confirmation_number": "WTR-1842-DEMO"},
                }
            }
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == "block_outputs:utility_citrus_turn_on.confirmation_number"


@pytest.mark.parametrize("canonical_value", [None, ""])
def test_requested_output_exact_value_canonical_placeholder_not_rescued_by_wrapper(
    canonical_value: str | None,
) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        CompletionCriterion(
            id="c_confirmation_number",
            outcome="The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_citrus_turn_on": {
                    "confirmation_number": canonical_value,
                    "output": {"confirmation_number": "WTR-1842-DEMO"},
                }
            }
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == "block_outputs:utility_citrus_turn_on.confirmation_number"


def test_requested_output_one_wrapper_runtime_field_abstains_without_broad_search() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        CompletionCriterion(
            id="c_confirmation_number",
            outcome="The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_shape="reference_code",
        )
    ]

    wrapped_verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"utility_citrus_turn_on": {"output": {"confirmation_number": "WTR-1842-DEMO"}}}
        ),
    )
    unrelated_nested_verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"utility_citrus_turn_on": {"details": {"output": {"confirmation_number": "WTR-1842-DEMO"}}}}
        ),
    )

    assert wrapped_verdicts[0].state == "unsatisfied"
    assert wrapped_verdicts[0].reason_code == "structurally_abstained"
    assert wrapped_verdicts[0].evidence_ref == "block_outputs:utility_citrus_turn_on.output.confirmation_number"
    assert unrelated_nested_verdicts[0].state == "unsatisfied"
    assert unrelated_nested_verdicts[0].reason_code == "missing_exact_field"


def test_requested_output_wrapped_wrong_neighbors_structurally_abstain() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("requested_date", "status")
    criteria = [
        CompletionCriterion(
            id="c_requested_date",
            outcome="The returned record includes requested date.",
            output_path="output.requested_date",
            expected_output_shape="date",
        ),
        CompletionCriterion(
            id="c_status",
            outcome="The returned record includes status.",
            output_path="output.status",
            expected_output_shape="status_label",
        ),
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_peach_gas_quickconnect": {
                    "output": {
                        "requested_date": "77 Gaslight Way, Decatur, GA 30030",
                        "status": "2026-06-24",
                    }
                }
            }
        ),
    )

    assert {verdict.state for verdict in verdicts} == {"unsatisfied"}
    assert {verdict.reason_code for verdict in verdicts} == {"structurally_abstained"}


@pytest.mark.parametrize("canonical_status", [None, ""])
def test_requested_output_shape_canonical_placeholder_not_rescued_by_wrapper(
    canonical_status: str | None,
) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("status")
    criteria = [
        CompletionCriterion(
            id="c_status",
            outcome="The returned record includes status.",
            output_path="output.status",
            expected_output_shape="status_label",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_peach_gas_quickconnect": {
                    "status": canonical_status,
                    "output": {"status": "Submitted / Processing"},
                }
            }
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"
    assert verdicts[0].evidence_ref is None


def test_requested_output_exact_values_reject_p8_scrambled_neighbors() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths(
        "confirmation_number",
        "provider_captured_address",
        "requested_date",
        "status",
    )
    criteria = [
        CompletionCriterion(
            id="c_confirmation_number",
            outcome="The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="QC-2002-DEMO",
        ),
        CompletionCriterion(
            id="c_provider_captured_address",
            outcome="The returned record includes provider captured address.",
            output_path="output.provider_captured_address",
            expected_output_value="77 Gaslight Way, Decatur, GA 30030",
        ),
        CompletionCriterion(
            id="c_requested_date",
            outcome="The returned record includes requested date.",
            output_path="output.requested_date",
            expected_output_value="2026-06-24",
        ),
        CompletionCriterion(
            id="c_status",
            outcome="The returned record includes status.",
            output_path="output.status",
            expected_output_value="Submitted / Processing",
        ),
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_peach_gas_quickconnect": {
                    "confirmation_number": "QC-2002-DEMO",
                    "provider_captured_address": "100245",
                    "requested_date": "77 Gaslight Way, Decatur, GA 30030",
                    "status": "2026-06-24",
                    "evidence_text": "Submitted / Processing",
                }
            }
        ),
    )

    verdicts_by_id = {verdict.criterion_id: verdict for verdict in verdicts}
    assert verdicts_by_id["c_confirmation_number"].reason_code == "evidence_confirms"
    for criterion_id in ("c_provider_captured_address", "c_requested_date", "c_status"):
        assert verdicts_by_id[criterion_id].state == "unsatisfied"
        assert verdicts_by_id[criterion_id].reason_code == "evidence_contradicts"


@pytest.mark.parametrize("status", [None, "None", "null", "n/a", "na", "", "-", "--"])
def test_requested_output_status_shape_treats_null_placeholders_as_missing(status: str | None) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("status")
    criteria = [
        CompletionCriterion(
            id="c_status",
            outcome="The returned record includes status.",
            output_path="output.status",
            expected_output_shape="status_label",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "utility_peach_gas_quickconnect": {
                    "status": status,
                    "evidence_text": "Submitted / Processing",
                }
            }
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code in {"missing_exact_field", "structurally_abstained"}


@pytest.mark.parametrize("status", ["Submitted / Processing", "Approved", "Pending Review", "Not Credentialed"])
def test_requested_output_status_shape_structurally_abstains_for_real_labels(status: str) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("status")
    criteria = [
        CompletionCriterion(
            id="c_status",
            outcome="The returned record includes status.",
            output_path="output.status",
            expected_output_shape="status_label",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"utility_peach_gas_quickconnect": {"status": status}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"


@pytest.mark.parametrize(
    "deposit_amount",
    [
        "2026-06-22",
        "100245",
        "77 Gaslight Way, Decatur, GA 30030",
        "Submitted / Processing",
        "https://utility.example.test/pay",
        "initiation fee applies",
    ],
)
def test_requested_output_money_amount_shape_structurally_abstains_for_present_neighbors(deposit_amount: str) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("deposit_amount")
    criteria = [
        _criterion(
            "c_deposit_amount",
            "The output includes the deposit amount.",
            output_path="output.deposit_amount",
            expected_output_shape="money_amount",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"utility_citrus_turn_on": {"deposit_amount": deposit_amount}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"


def test_requested_output_exact_wrong_value_beats_matching_shape() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        CompletionCriterion(
            id="c_confirmation_number",
            outcome="The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
            expected_output_shape="reference_code",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"utility_citrus_turn_on": {"confirmation_number": "QC-2002-DEMO"}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"


def test_requested_output_shape_missing_field_is_missing_exact_field() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        CompletionCriterion(
            id="c_confirmation_number",
            outcome="The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_shape="reference_code",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"utility_citrus_turn_on": {"evidence_text": "WTR-1842-DEMO"}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"


@pytest.mark.asyncio
async def test_requested_output_path_requires_expected_value_match(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("value-grounded requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("service_address", "requested_start_date")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_address",
                outcome="The returned record includes service address.",
                output_path="output.service_address",
                expected_output_value="1234 Sample Utility Way",
            ),
            CompletionCriterion(
                id="c_date",
                outcome="The returned record includes requested start date.",
                output_path="output.requested_start_date",
                expected_output_value="2026-06-22",
            ),
        ]
    )

    swapped = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result(
            {
                "service_address": "2026-06-22",
                "requested_start_date": "1234 Sample Utility Way",
            }
        ),
        time.monotonic(),
    )

    assert swapped is not None
    assert swapped.is_fully_satisfied() is False
    assert {verdict.reason_code for verdict in swapped.verdicts} == {"evidence_contradicts"}

    matched = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result(
            {
                "service_address": "1234 Sample Utility Way",
                "requested_start_date": "2026-06-22",
            }
        ),
        time.monotonic(),
    )

    assert matched is not None
    assert matched.is_fully_satisfied() is True
    assert {verdict.reason_code for verdict in matched.verdicts} == {"evidence_confirms"}


@pytest.mark.asyncio
async def test_rehydrated_requested_output_expected_value_blocks_scrambled_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("value-grounded requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("service_address", "requested_start_date")
    criteria = (
        CompletionCriterion(
            id="c_address",
            outcome="The returned record includes service address.",
            output_path="output.service_address",
            expected_output_value="1234 Sample Utility Way",
        ),
        CompletionCriterion(
            id="c_date",
            outcome="The returned record includes requested start date.",
            output_path="output.requested_start_date",
            expected_output_value="2026-06-22",
        ),
    )
    ctx.request_policy = RequestPolicy(completion_criteria=list(criteria_from_json(criteria_to_json(criteria))))

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result(
            {
                "service_address": "2026-06-22",
                "requested_start_date": "1234 Sample Utility Way",
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert {verdict.reason_code for verdict in verification.verdicts} == {"evidence_contradicts"}


@pytest.mark.asyncio
async def test_requested_output_path_does_not_match_block_label_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("requested-output criteria must not reach the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "npi")
    ctx.code_artifact_metadata = {"npi": _metadata_for_requested_paths("npi")["extract_profile"]}
    ctx.request_policy = RequestPolicy(
        completion_criteria=[CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi")]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_requested_output",
                "overall_status": "completed",
                "executed_block_labels": ["npi"],
                "current_url": "https://example.test/profile",
                "blocks": [
                    {
                        "label": "npi",
                        "block_type": "CODE",
                        "status": "completed",
                        "extracted_data": {"value": "1234567890"},
                    }
                ],
            },
        },
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert verification.verdicts[0].reason_code == "no_evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("goal_value_path", "requested_path", "runtime_output", "evidence_ref"),
    [
        ("$.npi", "output.npi", {"npi": "1234567890"}, "block_outputs:extract_profile.npi"),
        ("$[*].npi", "output.npi", [{"npi": "1234567890"}], "block_outputs:extract_profile.npi"),
        ("$[0].npi", "output.[].npi", [{"npi": "1234567890"}], "block_outputs:extract_profile.[].npi"),
        ("$[].npi", "output.[].npi", [{"npi": "1234567890"}], "block_outputs:extract_profile.[].npi"),
        (
            "records[0].npi",
            "output.records[].npi",
            {"records": [{"npi": "1234567890"}]},
            "block_outputs:extract_profile.records[].npi",
        ),
        (
            "output.records[0].npi",
            "output.records[].npi",
            {"records": [{"npi": "1234567890"}]},
            "block_outputs:extract_profile.records[].npi",
        ),
    ],
)
async def test_requested_output_path_normalizes_jsonpath_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
    goal_value_path: str,
    requested_path: str,
    runtime_output: Any,
    evidence_ref: str,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("normalized requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths(goal_value_path)
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_npi",
                outcome="The NPI is returned.",
                output_path=requested_path,
                expected_output_value="1234567890",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result(runtime_output),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == evidence_ref


@pytest.mark.asyncio
async def test_requested_output_path_aliases_authored_contract_to_requested_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("aliased requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_npi",
                outcome="The nested record NPI is returned.",
                output_path="output.records[].npi",
                expected_output_value="1234567890",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"records": [{"npi": "1234567890"}]}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == "block_outputs:extract_profile.records[].npi"


@pytest.mark.asyncio
async def test_requested_output_path_requires_exact_nested_runtime_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("requested-output criteria must not reach the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("locations[].address")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_address",
                outcome="Each listed location includes address.",
                output_path="output.locations[].address",
                expected_output_value="100 Main St",
            )
        ]
    )

    missing = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"locations": [{"name": "North Clinic"}]}),
        time.monotonic(),
    )
    assert missing is not None
    assert missing.is_fully_satisfied() is False
    assert missing.verdicts[0].reason_code == "missing_exact_field"
    assert missing.verdicts[0].missing_evidence == (
        "run output did not include exact structured field output.locations[].address"
    )

    satisfied = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"locations": [{"address": "100 Main St"}]}),
        time.monotonic(),
    )
    assert satisfied is not None
    assert satisfied.is_fully_satisfied() is True
    assert satisfied.verdicts[0].evidence_ref == "block_outputs:extract_profile.locations[].address"


@pytest.mark.asyncio
async def test_requested_output_path_uses_only_accepted_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("unproducible requested-output criteria must not reach the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.raw_code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_npi",
                outcome="The NPI is returned.",
                output_path="output.npi",
                expected_output_value="1234567890",
            )
        ]
    )

    rejected = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"npi": "1234567890"}),
        time.monotonic(),
    )
    assert rejected is not None
    assert rejected.is_fully_satisfied() is False
    assert rejected.verdicts[0].reason_code == "unproducible"
    assert "unproducible" in rejected.to_trace_data()["reason_codes"]

    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    accepted = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"npi": "1234567890"}),
        time.monotonic(),
    )
    assert accepted is not None
    assert accepted.is_fully_satisfied() is True


@pytest.mark.asyncio
async def test_requested_output_path_can_use_static_return_key_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("static requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = {"extract_profile": {"claimed_outcomes": [{}]}}
    ctx.last_workflow_yaml = textwrap.dedent(
        """
        workflow_definition:
          blocks:
            - block_type: code
              label: extract_profile
              code: |
                return {"npi": await page.locator("#npi").inner_text()}
        """
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_npi",
                outcome="The NPI is returned.",
                output_path="output.npi",
                expected_output_value="1234567890",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"npi": "1234567890"}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True


@pytest.mark.asyncio
async def test_requested_output_path_can_use_static_return_contract_without_matching_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("static requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "story")
    ctx.code_artifact_metadata = {
        "stale_extract": _metadata_for_requested_paths("top_post_identified")["extract_profile"]
    }
    ctx.last_workflow_yaml = textwrap.dedent(
        """
        workflow_definition:
          blocks:
            - block_type: code
              label: story
              code: |
                return {"top_post_identified": True}
        """
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_top_post",
                outcome="The top Hacker News post is identified.",
                output_path="output.top_post_identified",
                expected_output_value="true",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        {
            "ok": True,
            "data": {
                "workflow_run_id": "wr_hn",
                "overall_status": "completed",
                "executed_block_labels": ["story"],
                "blocks": [
                    {
                        "label": "story",
                        "block_type": "CODE",
                        "status": "completed",
                        "extracted_data": {"top_post_identified": True},
                    }
                ],
            },
        },
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == "block_outputs:story.top_post_identified"


def test_requested_output_path_dynamic_return_without_metadata_does_not_admit_runtime_output() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {}
    ctx.last_workflow_yaml = textwrap.dedent(
        """
        workflow_definition:
          blocks:
            - block_type: code
              label: story
              code: |
                return await extract_top_post()
        """
    )
    criteria = [
        CompletionCriterion(
            id="c_top_post",
            outcome="The top Hacker News post is identified.",
            output_path="output.top_post_identified",
            expected_output_value="true",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"story": {"top_post_identified": True}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "unproducible"
    assert verdicts[0].evidence_ref is None


def test_requested_output_static_contract_scopes_runtime_evidence_to_same_label() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {}
    ctx.last_workflow_yaml = textwrap.dedent(
        """
        workflow_definition:
          blocks:
            - block_type: code
              label: story
              code: |
                return {"top_post_identified": False}
        """
    )
    criteria = [
        CompletionCriterion(
            id="c_top_post",
            outcome="The top Hacker News post is identified.",
            output_path="output.top_post_identified",
            expected_output_value="true",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"unrelated_lookup": {"top_post_identified": True}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"
    assert verdicts[0].evidence_ref is None


def test_requested_output_runtime_debug_output_does_not_create_projection_contract() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {}
    ctx.last_workflow_yaml = textwrap.dedent(
        """
        workflow_definition:
          blocks:
            - block_type: code
              label: extract_profile
              code: |
                return {"npi": await page.locator("#npi").inner_text()}
        """
    )
    criteria = [
        CompletionCriterion(
            id="c_npi",
            outcome="The NPI is returned.",
            output_path="output.npi",
            expected_output_value="1234567890",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(block_outputs={"extract_profile": {"debug_output": {"npi": "1234567890"}}}),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"
    assert verdicts[0].evidence_ref is None


@pytest.mark.asyncio
async def test_requested_output_path_can_use_static_list_return_key_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("static list requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = {"extract_profile": {"claimed_outcomes": [{}]}}
    ctx.last_workflow_yaml = textwrap.dedent(
        """
        workflow_definition:
          blocks:
            - block_type: code
              label: extract_profile
              code: |
                return [{"npi": "1234567890"}]
        """
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_npi",
                outcome="The NPI is returned.",
                output_path="output.[].npi",
                expected_output_value="1234567890",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result([{"npi": "1234567890"}]),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == "block_outputs:extract_profile.[].npi"


@pytest.mark.asyncio
async def test_requested_output_criteria_are_not_sent_to_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_prompts: list[str] = []

    async def handler(**kwargs: object) -> dict:
        prompt = str(kwargs.get("prompt") or "")
        seen_prompts.append(prompt)
        return {"verdicts": [{"criterion_id": "c_cart", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_npi",
                outcome="The NPI is returned.",
                output_path="output.npi",
                expected_output_value="1234567890",
            ),
            CompletionCriterion(id="c_cart", outcome="The cart contains the selected item."),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"npi": "1234567890", "items": ["a"]}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert seen_prompts and "c_npi" not in seen_prompts[0]
    assert "c_cart" in seen_prompts[0]
    assert {verdict.criterion_id for verdict in verification.verdicts} == {"c_npi", "c_cart"}


@pytest.mark.asyncio
async def test_present_generic_requested_output_without_expected_value_structurally_abstains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c_submit", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("customer_name")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_customer_name",
                outcome="The returned record includes customer name.",
                output_path="output.customer_name",
            ),
            _criterion(
                "c_submit",
                "The record extraction completes.",
                kind="terminal_action",
                terminal_action_family="request",
            ),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"customer_name": "Sample Customer"}),
        time.monotonic(),
    )

    assert verification is not None
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_customer_name"].reason_code == "structurally_abstained"
    assert verdicts["c_customer_name"].satisfied is False
    assert verdicts["c_submit"].reason_code == "evidence_confirms"
    assert verification.is_fully_satisfied() is True
    trace = verification.to_trace_data()
    assert trace["unmet_criterion_ids"] == ["c_customer_name"]
    assert trace["verdict_0_missing_evidence"] == (
        "requested-output field is present, but the criterion lacks typed expected_output_value or "
        "expected_output_shape to prove the value"
    )


@pytest.mark.asyncio
async def test_present_requested_output_without_expected_value_cannot_satisfy_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("requested-output only abstention should not reach the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("customer_name")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_customer_name",
                outcome="The returned record includes customer name.",
                output_path="output.customer_name",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"customer_name": "Sample Customer"}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert verification.verdicts[0].reason_code == "structurally_abstained"


@pytest.mark.asyncio
async def test_requested_output_only_terminal_record_corroboration_satisfies_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("requested-output only terminal record corroboration should bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths(
        "confirmation_number",
        "account_number",
        "selected_start_date",
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_confirmation",
                outcome="The returned record includes confirmation number.",
                output_path="output.confirmation_number",
            ),
            CompletionCriterion(
                id="c_account",
                outcome="The returned record includes account number.",
                output_path="output.account_number",
            ),
            CompletionCriterion(
                id="c_start_date",
                outcome="The returned record includes selected start date.",
                output_path="output.selected_start_date",
            ),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result(_terminal_goal_payload()),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert {verdicts[criterion_id].reason_code for criterion_id in ("c_confirmation", "c_account", "c_start_date")} == {
        "structurally_abstained"
    }
    terminal_record_verdicts = [
        verdict for verdict in verification.verdicts if verdict.grounding_mode == "terminal_record"
    ]
    assert len(terminal_record_verdicts) == 1
    assert terminal_record_verdicts[0].evidence_ref == "block_outputs:extract_profile"


@pytest.mark.asyncio
async def test_requested_output_only_generic_output_has_no_terminal_record_corroboration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("requested-output only generic output should bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("customer_name")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_customer_name",
                outcome="The returned record includes customer name.",
                output_path="output.customer_name",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"customer_name": "Sample Customer"}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert all(verdict.grounding_mode != "terminal_record" for verdict in verification.verdicts)


@pytest.mark.asyncio
async def test_requested_output_bypasses_judge_satisfaction_without_exact_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c_npi", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi")]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"evidence_text": "The provider NPI is 1234567890."}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert verification.verdicts[0].reason_code == "no_evidence"


@pytest.mark.asyncio
async def test_requested_output_verdict_survives_unavailable_judge_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {"no_verdicts_key": []}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi"),
            CompletionCriterion(id="c_cart", outcome="The cart contains the selected item."),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"items": ["a"]}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.status == "evaluated"
    assert verification.is_fully_satisfied() is False
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_npi"].reason_code == "no_evidence"
    assert verdicts["c_npi"].satisfied is False
    assert verdicts["c_cart"].state == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_code", ["no_evidence", "unproducible"])
async def test_unfired_contingent_requested_output_miss_does_not_veto_satisfied_run_criterion(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("deterministic requested-output and present-value criteria should bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    if reason_code == "no_evidence":
        ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
        output = {"status": "DONE", "blocker": False}
    else:
        output = {"status": "DONE", "blocker": False}
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_npi",
                "The NPI is returned.",
                output_path="output.npi",
                contingent_on="the provider lookup is available",
                contingent_antecedent_output_path="output.blocker",
            ),
            _criterion("c_status", 'The output includes "DONE".'),
        ]
    )

    verification = await _maybe_run_completion_verification(ctx, _requested_output_result(output), time.monotonic())

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_npi"].reason_code == reason_code
    assert verdicts["c_status"].satisfied is True
    assert verification.structural_unfired_criterion_ids == ["c_npi"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_code", ["no_evidence", "unproducible"])
async def test_fired_contingent_requested_output_miss_still_vetoes(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("deterministic requested-output and present-value criteria should bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    if reason_code == "no_evidence":
        ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
        output = {"status": "DONE", "blocker": True}
    else:
        output = {"status": "DONE", "blocker": True}
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_npi",
                "The NPI is returned.",
                output_path="output.npi",
                contingent_on="the provider lookup is available",
                contingent_antecedent_output_path="output.blocker",
            ),
            _criterion("c_status", 'The output includes "DONE".'),
        ]
    )

    verification = await _maybe_run_completion_verification(ctx, _requested_output_result(output), time.monotonic())

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_npi"].reason_code == reason_code
    assert verdicts["c_status"].satisfied is True
    assert verification.structural_unfired_criterion_ids == []


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_treats_fallback_record_as_criteria_less(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_lookup_calls = 0

    async def fail_handler(**_: object) -> object:
        raise AssertionError("value-agnostic fallback criteria must not call the judge")

    async def handler_lookup(_ctx: object) -> object:
        nonlocal handler_lookup_calls
        handler_lookup_calls += 1
        return fail_handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler",
        handler_lookup,
    )
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=_structured_record_criteria())

    verification = await _maybe_run_completion_verification(
        ctx,
        _structured_record_top_level_output_result(),
        time.monotonic(),
    )

    # Value-agnostic fallback criteria are criteria-less; a well-shaped record is not a
    # verified result, and the path short-circuits before any judge lookup.
    assert verification is None
    assert handler_lookup_calls == 0


@pytest.mark.asyncio
async def test_requested_output_satisfaction_is_not_vetoed_by_fallback_record_abstentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("value-agnostic fallback criteria must not call the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_npi",
                outcome="The NPI is returned.",
                output_path="output.npi",
                expected_output_value="1234567890",
            ),
            *_structured_record_criteria(),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"npi": "1234567890"}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert [verdict.criterion_id for verdict in verification.verdicts] == ["c_npi"]


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_terminal_goal_bypasses_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_lookup_calls = 0

    async def fail_handler(**_: object) -> object:
        raise AssertionError("deterministically covered terminal goal must not call the judge")

    async def handler_lookup(_ctx: object) -> object:
        nonlocal handler_lookup_calls
        handler_lookup_calls += 1
        return fail_handler

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.tools.completion._completion_verification_handler",
        handler_lookup,
    )
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "submit_water_request")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "a commercial water service request is submitted",
                kind="terminal_action",
                terminal_action_family="request",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(ctx, _terminal_goal_output_result(), time.monotonic())

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == "block_outputs:submit_water_request"
    assert handler_lookup_calls == 0


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_terminal_goal_without_boolean_bypasses_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("artifact-backed terminal goal must not call the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "submit_water_request")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "a commercial water service request is submitted",
                kind="terminal_action",
                terminal_action_family="request",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx, _terminal_goal_output_result(submitted=None), time.monotonic()
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == "block_outputs:submit_water_request"


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_fallback_floor_uses_recorded_terminal_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("recorded fallback-floor outcome must not call the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "submit_water_request")
    ctx.request_policy = RequestPolicy(
        completion_criteria=build_classifier_fallback_floor([]),
        classifier_status="fallback",
    )

    verification = await _maybe_run_completion_verification(ctx, _terminal_goal_output_result(), time.monotonic())

    assert verification is not None
    assert verification.no_gradeable_run_plane is False
    assert verification.is_fully_satisfied() is True
    assert verification.to_trace_data()["criterion_count"] == 1
    assert verification.verdicts[0].evidence_ref == "block_outputs:submit_water_request"


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_fallback_floor_uses_validation_review_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("validation review evidence must not call the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "submit_request")
    ctx.request_policy = RequestPolicy(
        completion_criteria=build_classifier_fallback_floor([]),
        classifier_status="fallback",
    )

    verification = await _maybe_run_completion_verification(ctx, _validation_review_output_result(), time.monotonic())

    assert verification is not None
    assert verification.no_gradeable_run_plane is False
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == "block_outputs:submit_request"


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_fallback_floor_uses_live_output_parameter_review_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("live validation review output parameter evidence must not call the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "validate_business_start_service_review")
    ctx.request_policy = RequestPolicy(
        completion_criteria=build_classifier_fallback_floor([]),
        classifier_status="fallback",
    )

    verification = await _maybe_run_completion_verification(
        ctx, _live_validation_review_output_result(), time.monotonic()
    )

    assert verification is not None
    assert verification.no_gradeable_run_plane is False
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == "block_outputs:validate_business_start_service_review_output"


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_terminal_goal_uses_workflow_run_output_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("registered terminal-action evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "submit_water_request")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c1",
                "a commercial water service request is started",
                kind="terminal_action",
                terminal_action_family="request",
            )
        ]
    )
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_terminal_goal",
            "overall_status": "completed",
            "executed_block_labels": ["submit_water_request"],
            "current_url": "https://example.test/confirmation",
            "blocks": [],
            "workflow_run_output_parameters": [
                {
                    "workflow_run_id": "wr_terminal_goal",
                    "output_parameter_id": "op_terminal",
                    "output_parameter_key": "submit_water_request_output",
                    "block_label": "submit_water_request",
                    "block_type": "CODE",
                    "value": _terminal_goal_payload(),
                }
            ],
        },
    }

    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].criterion_id == "c1"
    assert verification.verdicts[0].evidence_ref == "block_outputs:submit_water_request_output"


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_fallback_floor_rejects_clean_url_without_review_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {
            "verdicts": [
                {
                    "criterion_id": "__copilot_fallback_floor__run",
                    "satisfied": True,
                    "reason_code": "evidence_confirms",
                    "evidence_ref": "observed_end_state_url",
                }
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=build_classifier_fallback_floor([]),
        classifier_status="fallback",
    )
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_review",
            "overall_status": "completed",
            "executed_block_labels": [],
            "current_url": "http://localhost:8900/utility_services/peach_electric/",
            "page_title": "Peach Electric - Start Service",
            "blocks": [],
        },
    }

    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())

    assert verification is not None
    assert verification.no_gradeable_run_plane is False
    assert verification.is_fully_satisfied() is False
    assert verification.verdicts[0].state == "unsatisfied"
    assert verification.verdicts[0].reason_code == "no_evidence"
    assert verification.verdicts[0].evidence_ref is None


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_fallback_floor_without_evidence_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("true no-evidence fallback floor must not call the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=build_classifier_fallback_floor([]),
        classifier_status="fallback",
    )
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_empty",
            "overall_status": "completed",
            "executed_block_labels": [],
            "blocks": [],
        },
    }

    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())

    assert verification is not None
    assert verification.no_gradeable_run_plane is False
    assert verification.is_fully_satisfied() is False
    assert verification.verdicts[0].state == "unsatisfied"
    assert verification.verdicts[0].reason_code == "no_evidence"


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_fallback_floor_rejects_typed_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> object:
        return {
            "verdicts": [
                {
                    "criterion_id": "__copilot_fallback_floor__run",
                    "satisfied": True,
                    "reason_code": "evidence_confirms",
                    "evidence_ref": "open_demo_utility_login_output",
                }
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "open_demo_utility_login")
    ctx.request_policy = RequestPolicy(
        completion_criteria=build_classifier_fallback_floor([]),
        classifier_status="fallback",
    )
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_login_only",
            "overall_status": "failed",
            "executed_block_labels": ["open_demo_utility_login"],
            "current_url": "http://localhost:8900/utility_services/demo_utility/",
            "page_title": "Login",
            "blocks": [
                {
                    "label": "open_demo_utility_login",
                    "block_type": "CODE",
                    "status": "failed",
                    "failure_reason": "Page.evaluate: SyntaxError: Unexpected token ')'",
                    "extracted_data": {
                        "login_page_reached": True,
                        "evidence_text": "Sample Utility Portal Log in to your account User ID Password",
                    },
                }
            ],
        },
    }

    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert verification.verdicts[0].criterion_id == "__copilot_fallback_floor__run"
    assert verification.verdicts[0].state == "unsatisfied"
    assert verification.verdicts[0].reason_code == "no_evidence"
    assert verification.verdicts[0].evidence_ref is None


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_mixed_terminal_goal_upgrades_judge_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(**_: object) -> dict:
        return {
            "verdicts": [
                {
                    "criterion_id": "c1",
                    "satisfied": True,
                    "reason_code": "evidence_confirms",
                    "evidence_ref": "block_outputs:submit_water_request",
                }
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "submit_water_request")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c0",
                "a commercial water service request is submitted",
                kind="terminal_action",
                terminal_action_family="request",
            ),
            _criterion("c1", "the selected start date is reported"),
        ]
    )

    verification = await _maybe_run_completion_verification(ctx, _terminal_goal_output_result(), time.monotonic())

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert {verdict.criterion_id: verdict.reason_code for verdict in verification.verdicts} == {
        "c0": "evidence_confirms",
        "c1": "evidence_confirms",
    }


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_fails_closed_on_no_judge_for_judge_needed_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_completion_handler(monkeypatch, None)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[_criterion("c0", "item in cart")])

    verification = await _maybe_run_completion_verification(
        ctx,
        _structured_record_top_level_output_result(),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.status == "unavailable"
    assert verification.is_fully_satisfied() is False


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_fails_closed_on_low_budget_for_judge_needed_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("low-budget verification must not call the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[_criterion("c0", "item in cart")])
    starved = time.monotonic() - 100_000

    verification = await _maybe_run_completion_verification(
        ctx,
        _structured_record_top_level_output_result(),
        starved,
    )

    assert verification is not None
    assert verification.status == "unavailable"
    assert verification.is_fully_satisfied() is False


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_mixed_criteria_still_fail_closed_on_judge_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def slow_handler(**_: object) -> dict:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return {"verdicts": []}

    monkeypatch.setattr(settings, "COPILOT_COMPLETION_JUDGE_TIMEOUT_SECONDS", 0.01)
    _patch_completion_handler(monkeypatch, slow_handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            *_structured_record_criteria(),
            CompletionCriterion(
                id="source_timestamp_visible", outcome="The source page shows the latest update timestamp."
            ),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _structured_record_top_level_output_result(),
        time.monotonic(),
    )

    assert calls == 1
    assert verification is not None
    assert verification.status == "unavailable"
    assert verification.is_fully_satisfied() is False


def test_snapshot_ignores_registered_output_parameters_from_prior_run() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_record_status_details")
    run = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_current",
            "blocks": [],
            "registered_output_parameter_values": [
                {
                    "workflow_run_id": "wr_prior",
                    "output_parameter_key": "extract_record_status_details_output",
                    "block_label": "extract_record_status_details",
                    "value": {"entity_name": "Jordan Example", "record_number": "1234567890"},
                }
            ],
        },
    }

    snap = _build_run_evidence_snapshot(ctx, run)

    assert snap.block_outputs == {}


def test_snapshot_summarizes_registered_download_outputs() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_statement")
    run = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_download",
            "blocks": [
                {
                    "label": "download_statement",
                    "extracted_data": {
                        "page": "<RecordingLocator>",
                        "download": "<Download>",
                        "downloaded_file_name": "apexbiz_100245_2026-05.pdf",
                        "downloaded_files": [{"filename": "apexbiz_100245_2026-05.pdf"}],
                        "downloaded_file_urls": [
                            "https://local.test/downloads/apexbiz_100245_2026-05.pdf?token=secret"
                        ],
                        "downloaded_file_artifact_ids": ["artifact_1"],
                    },
                }
            ],
        },
    }

    snapshot = _build_run_evidence_snapshot(ctx, run)
    rendered = snapshot.render_prompt_block()

    assert snapshot.block_outputs["download_statement"] == {
        "download_registered": True,
        "downloaded_file_count": 1,
        "downloaded_file_url_count": 1,
        "downloaded_file_artifact_count": 1,
        "downloaded_file_names": ["apexbiz_100245_2026-05.pdf"],
    }
    assert "apexbiz_100245_2026-05.pdf" in rendered
    assert "RecordingLocator" not in rendered and "Download" not in rendered and "secret" not in rendered


def _download_result(output: dict[str, Any]) -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_download",
            "overall_status": "completed",
            "executed_block_labels": ["download_document"],
            "current_url": "https://example.test/documents",
            "blocks": [
                {
                    "label": "download_document",
                    "block_type": "CODE",
                    "status": "completed",
                    "extracted_data": output,
                }
            ],
        },
    }


def _registered_download_output_parameter_result(value: dict[str, Any]) -> dict:
    return {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_download",
            "overall_status": "completed",
            "executed_block_labels": [],
            "current_url": "https://example.test/documents",
            "blocks": [],
            "registered_output_parameter_values": [
                {
                    "workflow_run_id": "wr_download",
                    "output_parameter_key": "download_document_output",
                    "block_label": "download_document",
                    "block_type": "CODE",
                    "value": value,
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_download_registered_non_empty_injects_and_verifies_without_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("registered download evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_document")
    ctx.request_policy = RequestPolicy(completion_criteria=[])

    verification = await _maybe_run_completion_verification(
        ctx,
        _download_result(
            {
                "downloaded_files": [{"filename": "statement.pdf"}],
                "downloaded_file_urls": [],
                "downloaded_file_artifact_ids": [],
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.criterion_ids == [REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID]
    assert verification.verdicts == [
        CriterionVerdict(
            criterion_id=REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:download_document",
        )
    ]
    assert ctx.request_policy.completion_criteria == []


@pytest.mark.asyncio
async def test_download_registered_output_parameter_injects_and_verifies_without_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("registered output-parameter download evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_document")
    ctx.request_policy = RequestPolicy(completion_criteria=[])

    verification = await _maybe_run_completion_verification(
        ctx,
        _registered_download_output_parameter_result(
            {
                "downloaded_files": [{"filename": "document.pdf"}],
                "downloaded_file_urls": [],
                "downloaded_file_artifact_ids": [],
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.criterion_ids == [REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID]
    assert verification.verdicts == [
        CriterionVerdict(
            criterion_id=REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:download_document_output",
        )
    ]
    assert ctx.request_policy.completion_criteria == []


@pytest.mark.asyncio
async def test_download_registered_nested_output_corroborates_requested_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: str) -> str:
        raise AssertionError("nested registered download evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("account_number")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_account_number",
                "The returned record includes account number.",
                output_path="output.account_number",
            ),
            registered_download_completion_criterion(),
        ]
    )

    run_result = _download_result(
        {
            "output": {
                "account_number": "100245",
                "downloaded_files": [{"filename": "statement.pdf"}],
                "downloaded_file_urls": [],
                "downloaded_file_artifact_ids": [],
            }
        }
    )
    run_result["data"]["executed_block_labels"] = ["extract_profile"]
    run_result["data"]["blocks"] = [
        {
            "label": "extract_profile",
            "block_type": "CODE",
            "status": "completed",
            "extracted_data": {
                "output": {
                    "account_number": "100245",
                    "downloaded_files": [{"filename": "statement.pdf"}],
                    "downloaded_file_urls": [],
                    "downloaded_file_artifact_ids": [],
                }
            },
        }
    ]

    verification = await _maybe_run_completion_verification(
        ctx,
        run_result,
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_account_number"].reason_code == "structurally_abstained"
    assert verdicts["c_account_number"].evidence_ref == "block_outputs:extract_profile.output.account_number"
    assert verdicts[REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID].reason_code == "evidence_confirms"


@pytest.mark.asyncio
async def test_marked_requested_output_id_without_expected_value_is_no_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("marked requested-output failure must be deterministic")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_document")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("output_id")
    requested = _criterion(
        "c_output_id",
        "The returned record includes output id.",
        output_path="output.output_id",
        deliverable_kind="registered_download",
    )
    ctx.request_policy = RequestPolicy(completion_criteria=[requested])

    verification = await _maybe_run_completion_verification(
        ctx,
        _download_result(
            {
                "downloaded_files": [{"filename": "statement.pdf"}],
                "downloaded_file_urls": [],
                "downloaded_file_artifact_ids": [],
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_output_id"].reason_code == "no_evidence"
    assert REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID in verdicts
    assert ctx.request_policy.completion_criteria == [requested]


@pytest.mark.asyncio
async def test_marked_requested_output_npi_without_expected_value_is_no_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("marked requested-output failure must be deterministic")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_document")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    requested = _criterion(
        "c_npi",
        "The returned record includes NPI.",
        output_path="output.npi",
        deliverable_kind="registered_download",
    )
    ctx.request_policy = RequestPolicy(completion_criteria=[requested])

    verification = await _maybe_run_completion_verification(
        ctx,
        _download_result(
            {
                "downloaded_files": [{"filename": "statement.pdf"}],
                "downloaded_file_urls": [],
                "downloaded_file_artifact_ids": [],
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_npi"].reason_code == "no_evidence"
    assert REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID in verdicts
    assert ctx.request_policy.completion_criteria == [requested]


@pytest.mark.asyncio
async def test_unmarked_requested_output_id_without_expected_value_is_no_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("unmarked requested-output failure must be deterministic")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_document")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("output_id")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_output_id",
                "The returned record includes output id.",
                output_path="output.output_id",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _download_result(
            {
                "downloaded_files": [{"filename": "statement.pdf"}],
                "downloaded_file_urls": [],
                "downloaded_file_artifact_ids": [],
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_output_id"].reason_code == "no_evidence"


@pytest.mark.asyncio
async def test_marked_download_deliverable_does_not_remove_mixed_extraction_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("mixed requested-output failure must be deterministic")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_document")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("output_id", "npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_output_id",
                "The returned record includes output id.",
                output_path="output.output_id",
                deliverable_kind="registered_download",
            ),
            _criterion("c_npi", "The returned record includes NPI.", output_path="output.npi"),
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _download_result(
            {
                "downloaded_files": [{"filename": "statement.pdf"}],
                "downloaded_file_urls": [],
                "downloaded_file_artifact_ids": [],
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_output_id"].reason_code == "no_evidence"
    assert verdicts["c_npi"].reason_code == "no_evidence"
    assert REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID in verdicts


@pytest.mark.parametrize(
    ("output_path", "criterion_id"),
    [
        ("output.downloaded_files", "c_downloaded_files"),
        ("output.downloaded_file_urls", "c_downloaded_file_urls"),
        ("output.downloaded_file_artifact_ids", "c_downloaded_file_artifact_ids"),
    ],
)
@pytest.mark.asyncio
async def test_download_output_path_reconciles_with_registered_download_evidence(
    monkeypatch: pytest.MonkeyPatch,
    output_path: str,
    criterion_id: str,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("registered download output path must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_document")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                criterion_id,
                "The returned record includes registered download output.",
                output_path=output_path,
            )
        ]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _download_result(
            {
                "downloaded_files": [{"filename": "statement.pdf"}],
                "downloaded_file_urls": [],
                "downloaded_file_artifact_ids": [],
            }
        ),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.criterion_ids == [REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID]


@pytest.mark.asyncio
async def test_marked_download_deliverable_without_registered_evidence_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("missing registered download evidence must be deterministic")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_document")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("output_id")
    ctx.reached_download_target = ReachedDownloadTarget(
        selector='a[href="/files/statement.pdf"]',
        affordance_text="Download",
        download_kind="extension",
        source_step="trajectory_recency",
        already_registered=False,
    )
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion(
                "c_output_id",
                "The returned record includes output id.",
                output_path="output.output_id",
                deliverable_kind="registered_download",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(ctx, _goto_only_result(), time.monotonic())

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    verdicts = {verdict.criterion_id: verdict for verdict in verification.verdicts}
    assert verdicts["c_output_id"].reason_code == "no_evidence"
    assert verdicts[REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID].reason_code == "no_evidence"


@pytest.mark.asyncio
async def test_download_typed_affordance_injects_and_fails_without_registered_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("missing registered download evidence must be deterministic")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[])
    ctx.reached_download_target = ReachedDownloadTarget(
        selector='a[href="/files/statement.pdf"]',
        affordance_text="Download",
        download_kind="extension",
        source_step="trajectory_recency",
        already_registered=False,
    )

    verification = await _maybe_run_completion_verification(ctx, _goto_only_result(), time.monotonic())

    assert verification is not None
    assert verification.is_fully_satisfied() is False
    assert verification.criterion_ids == [REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID]
    assert verification.verdicts == [
        CriterionVerdict(
            criterion_id=REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
            state="unsatisfied",
            reason_code="no_evidence",
            missing_evidence="run output did not include a non-empty registered browser download",
        )
    ]
    assert ctx.request_policy.completion_criteria == []


@pytest.mark.asyncio
async def test_definition_only_non_download_remains_no_gradeable_run_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("definition-only criteria must not reach the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="definition_inputs",
                outcome="The workflow accepts the account number as a reusable input.",
                level="definition",
            )
        ]
    )

    verification = await _maybe_run_completion_verification(ctx, _goto_only_result(), time.monotonic())

    assert verification is not None
    assert verification.criterion_ids == ["definition_inputs"]
    assert verification.verdicts == [
        CriterionVerdict(
            criterion_id="definition_inputs",
            state="unknown",
            reason_code="definition_parameters_absent",
        )
    ]
    assert REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID not in [
        criterion.id for criterion in ctx.request_policy.completion_criteria
    ]


def test_download_reconciliation_is_idempotent_when_criterion_exists() -> None:
    ctx = _run_ctx()
    criterion = registered_download_completion_criterion()
    ctx.request_policy = RequestPolicy(completion_criteria=[criterion])
    criteria = [criterion]

    reconciled = _reconcile_download_completion_criterion(
        ctx,
        {"ok": True, "data": {"reached_download_target": {"download_kind": "extension"}}},
        criteria,
    )

    assert reconciled is criteria
    assert [existing.id for existing in ctx.request_policy.completion_criteria] == [
        REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID
    ]


def test_download_reconciliation_does_not_mutate_request_policy() -> None:
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(completion_criteria=[])

    reconciled = _reconcile_download_completion_criterion(
        ctx,
        {"ok": True, "data": {"reached_download_target": {"download_kind": "extension"}}},
        [],
    )

    assert [criterion.id for criterion in reconciled] == [REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID]
    assert ctx.request_policy.completion_criteria == []


def test_download_grader_requires_non_empty_registered_surface() -> None:
    criteria = [registered_download_completion_criterion()]

    missing = grade_registered_download_criteria(
        criteria,
        RunEvidenceSnapshot(block_outputs={"download_document": {"download_registered": True}}),
    )
    present = grade_registered_download_criteria(
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "download_document": {
                    "download_registered": True,
                    "downloaded_file_count": 0,
                    "downloaded_file_url_count": 1,
                    "downloaded_file_artifact_count": 0,
                }
            }
        ),
    )

    assert missing == [
        CriterionVerdict(
            criterion_id=REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
            state="unsatisfied",
            reason_code="no_evidence",
            missing_evidence="run output did not include a non-empty registered browser download",
        )
    ]
    assert (
        run_plane_all_no_evidence(
            CompletionVerificationResult(
                status="evaluated",
                criterion_ids=[REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID],
                verdicts=missing,
            )
        )
        is True
    )
    assert present == [
        CriterionVerdict(
            criterion_id=REGISTERED_DOWNLOAD_COMPLETION_CRITERION_ID,
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:download_document",
        )
    ]


def test_snapshot_normalizes_registered_download_output_parameter_values() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "download_document")

    snapshot = _build_run_evidence_snapshot(
        ctx,
        _registered_download_output_parameter_result(
            {
                "page": "<RecordingLocator>",
                "download": "<Download>",
                "downloaded_file_name": "document.pdf",
                "downloaded_files": [{"filename": "document.pdf"}],
                "downloaded_file_urls": ["https://example.test/downloads/document.pdf?token=secret"],
                "downloaded_file_artifact_ids": ["artifact_1"],
            }
        ),
    )
    rendered = snapshot.render_prompt_block()

    assert snapshot.block_outputs["download_document_output"] == {
        "download_registered": True,
        "downloaded_file_count": 1,
        "downloaded_file_url_count": 1,
        "downloaded_file_artifact_count": 1,
        "downloaded_file_names": ["document.pdf"],
    }
    assert "RecordingLocator" not in rendered and "Download" not in rendered and "secret" not in rendered


def test_snapshot_includes_verified_context_labels_without_prior_outputs() -> None:
    ctx = _run_ctx()
    labels = [
        "open_bacb_homepage",
        "click_find_a_certificant",
        "search_noor_assi_rbt",
        "expand_noor_assi_result",
        "extract_credential_details",
    ]
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[SimpleNamespace(label=label, block_type="task") for label in labels]
        )
    )
    ctx.verified_prefix_labels = list(labels)
    ctx.verified_block_outputs["expand_noor_assi_result"] = {"stale": "prior run output"}

    run = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_extract",
            "current_url": "https://www.bacb.com/services/o.php?page=101135",
            "executed_block_labels": ["extract_credential_details"],
            "blocks": [
                {
                    "label": "extract_credential_details",
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {
                        "extracted_information": {
                            "credentials": [
                                {
                                    "credential_type": "Registered Behavior Technician",
                                    "credential_number": "RBT-19-98341",
                                    "expiration_date": "09/06/2022",
                                }
                            ]
                        }
                    },
                }
            ],
        },
    }

    snapshot = _build_run_evidence_snapshot(ctx, run)

    assert snapshot.verified_context_block_labels == labels[:-1]
    assert snapshot.block_outputs == {
        "extract_credential_details": {
            "extracted_information": {
                "credentials": [
                    {
                        "credential_type": "Registered Behavior Technician",
                        "credential_number": "RBT-19-98341",
                        "expiration_date": "09/06/2022",
                    }
                ]
            }
        }
    }
    assert "expand_noor_assi_result" not in snapshot.block_outputs
    rendered = snapshot.render_prompt_block()
    assert "verified_context_block_labels: open_bacb_homepage" in rendered
    assert "expand_noor_assi_result" in rendered


@pytest.mark.asyncio
async def test_completion_verification_receives_verified_context_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_prompt: dict[str, str] = {}

    async def handler(**kwargs: object) -> dict:
        seen_prompt["prompt"] = str(kwargs.get("prompt") or "")
        return {
            "verdicts": [
                {"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"},
                {"criterion_id": "c1", "satisfied": True, "reason_code": "evidence_confirms"},
            ]
        }

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            _criterion("c0", "credential type, number, and expiration date are reported"),
            _criterion("c1", "the data came from the expanded certificant result"),
        ]
    )
    labels = [
        "open_bacb_homepage",
        "click_find_a_certificant",
        "search_noor_assi_rbt",
        "expand_noor_assi_result",
        "extract_credential_details",
    ]
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[
                SimpleNamespace(label=label, block_type="extraction" if label.startswith("extract") else "task")
                for label in labels
            ]
        )
    )
    ctx.verified_prefix_labels = list(labels)
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_extract",
            "current_url": "https://www.bacb.com/services/o.php?page=101135",
            "executed_block_labels": ["extract_credential_details"],
            "blocks": [
                {
                    "label": "extract_credential_details",
                    "block_type": "EXTRACTION",
                    "status": "completed",
                    "extracted_data": {
                        "extracted_information": {
                            "person_name": "NOOR ASSI",
                            "credentials": [
                                {
                                    "credential_type": "Registered Behavior Technician",
                                    "credential_number": "RBT-19-98341",
                                    "expiration_date": "09/06/2022",
                                }
                            ],
                        }
                    },
                }
            ],
        },
    }

    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert "verified_context_block_labels" in seen_prompt["prompt"]
    assert "expand_noor_assi_result" in seen_prompt["prompt"]
    assert "RBT-19-98341" in seen_prompt["prompt"]


@pytest.mark.asyncio
async def test_maybe_run_completion_verification_unavailable_on_low_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(**_: object) -> dict:
        return {"verdicts": [{"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"}]}

    _patch_completion_handler(monkeypatch, handler)
    ctx = _run_ctx()
    starved = time.monotonic() - 100_000  # no budget left to verify this candidate run
    result = await _maybe_run_completion_verification(ctx, _clean_success_result(), starved)
    # Fail closed: a candidate run we could not verify must not fall back to the
    # run-status proxy and claim success.
    assert result is not None
    assert result.status == "unavailable"
    assert result.is_fully_satisfied() is False

    # A missing judge handler means the required completion contract could not be
    # verified, so the run must not pass through on status alone.
    _patch_completion_handler(monkeypatch, None)
    no_handler_result = await _maybe_run_completion_verification(ctx, _clean_success_result(), time.monotonic())
    assert no_handler_result is not None
    assert no_handler_result.status == "unavailable"
    assert no_handler_result.is_fully_satisfied() is False


@pytest.mark.asyncio
async def test_completion_verification_still_fails_closed_with_author_time_log_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    _patch_completion_handler(monkeypatch, None)

    result = await _maybe_run_completion_verification(_run_ctx(), _clean_success_result(), time.monotonic())

    assert result is not None
    assert result.status == "unavailable"
    assert result.is_fully_satisfied() is False


def test_completion_contract_not_violated_unavailable_blocks_surfacing() -> None:
    ctx = SimpleNamespace(
        completion_verification_result=CompletionVerificationResult("unavailable"),
        last_artifact_health_blocker_reason=None,
    )
    # An unavailable verdict means the outcome could not be verified: do not surface
    # the workflow as verified on run status alone.
    assert _completion_contract_not_violated(ctx) is False  # type: ignore[arg-type]


def test_judgment_boolean_self_emitted_from_runtime_source_abstains() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("selected_highest_priority")
    criteria = [
        _criterion(
            "c_selected",
            "The returned record reports the highest-priority selection judgment.",
            output_path="output.selected_highest_priority",
            expected_output_value=True,
            requested_output_evidence_source="runtime_output",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"extract_profile": {"selected_highest_priority": True}},
            block_output_sources={"extract_profile": "runtime_output"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].self_emitted_judgment_not_independent is True
    assert verdicts[0].grounding_mode == "judgment_boolean"


def test_judgment_boolean_confirmed_by_independent_page_evidence() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("selected_highest_priority")
    criteria = [
        _criterion(
            "c_selected",
            "The returned record reports the highest-priority selection judgment.",
            output_path="output.selected_highest_priority",
            expected_output_value=True,
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"current_page_observation": {"selected_highest_priority": True}},
            block_output_sources={"current_page_observation": "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_source == "independent_page_evidence"


def test_judgment_boolean_refuted_by_independent_page_evidence_at_structured_path() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("selected_highest_priority")
    criteria = [
        _criterion(
            "c_selected",
            "The returned record reports the highest-priority selection judgment.",
            output_path="output.selected_highest_priority",
            expected_output_value=True,
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"current_page_observation": {"selected_highest_priority": False}},
            block_output_sources={"current_page_observation": "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == "block_outputs:current_page_observation.selected_highest_priority"
    assert verdicts[0].evidence_source == "independent_page_evidence"


def test_bool_expected_never_matches_container_with_unrelated_nested_boolean() -> None:
    assert _value_matches_expected({"answer": False, "reviewed": True}, True) is False
    assert _value_matches_expected([{"answer": False}, {"other": True}], True) is False
    assert _value_matches_expected(True, True) is True
    assert _value_matches_expected("true", True) is True
    assert _value_matches_expected(False, True) is False


def test_judgment_boolean_refuted_when_independent_evidence_resolves_false_leaf() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("answer")
    criteria = [
        _criterion(
            "c_answer",
            "The returned record reports the affirmative judgment.",
            output_path="output.answer",
            expected_output_value=True,
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"current_page_observation": {"answer": False, "reviewed": True}},
            block_output_sources={"current_page_observation": "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == "block_outputs:current_page_observation.answer"
    assert verdicts[0].evidence_source == "independent_page_evidence"


def _login_gate_packet() -> dict[str, object]:
    return {
        "forms": [
            {
                "fields": [{"type": "password", "selector": "#password", "disabled": False}],
                "submit_controls": [{"type": "submit", "selector": "button[type=submit]", "disabled": False}],
            }
        ],
        "navigation_targets": [{"href": "https://example.test/account", "selector": "a.account"}],
        "result_containers": [],
    }


def _gated_login_packet() -> dict[str, object]:
    return {
        "forms": [
            {
                "fields": [{"type": "password", "selector": "#password", "disabled": False}],
                "submit_controls": [{"type": "submit", "selector": "button[type=submit]", "disabled": True}],
            }
        ],
        "navigation_targets": [{"href": "https://example.test/account", "selector": "a.account"}],
        "result_containers": [],
        "challenge_state": {
            "gates_submit_controls": True,
            "gated_submit_controls": [{"selector": "button[type=submit]", "disabled": True}],
        },
    }


def _target_reached_packet() -> dict[str, object]:
    return {
        "forms": [],
        "navigation_targets": [{"href": "https://example.test/account", "selector": "a.account"}],
        "result_containers": [{"selector": "#account-summary", "row_count": 1}],
    }


def test_judgment_truth_condition_confirms_login_gate_from_independent_packet() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("login_gate_blocks_target")
    criteria = [
        replace(
            _criterion(
                "c_login_gate",
                "The returned record reports whether the login gate blocks the target.",
                output_path="output.login_gate_blocks_target",
                expected_output_value=True,
                requested_output_evidence_source="independent_run_evidence",
            ),
            judgment_truth_condition=JudgmentTruthCondition(
                predicate="login_gate_blocks_target", polarity_when_holds=True
            ),
        )
    ]

    with capture_logs() as logs:
        verdicts = grade_requested_output_criteria(
            ctx,
            criteria,
            RunEvidenceSnapshot(
                block_outputs={_POST_RUN_PAGE_OBSERVATION_LABEL: _login_gate_packet()},
                block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
            ),
        )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_ref == f"block_outputs:{_POST_RUN_PAGE_OBSERVATION_LABEL}"
    assert verdicts[0].evidence_source == "independent_page_evidence"
    assert logs[-1]["event"] == "copilot_judgment_evidence_verdict"
    assert logs[-1]["predicate"] == "login_gate_blocks_target"
    assert logs[-1]["packet_label"] == _POST_RUN_PAGE_OBSERVATION_LABEL
    assert logs[-1]["verdict"] == "evidence_confirms"
    assert logs[-1]["origin"] == "criterion"


def test_bound_gated_submit_login_packet_wins_over_self_emitted_judgment() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "output.login_gate_blocks_target",
                    "requested_output_evidence_source": "independent_run_evidence",
                    "judgment_predicate": "login_gate_blocks_target",
                    "judgment_polarity_when_holds": True,
                }
            ],
        }
    }
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="",
        **_gated_login_packet(),
    )
    criteria = [
        _criterion(
            "c_login_gate",
            "The returned record reports whether the login gate blocks the target.",
            output_path="output.login_gate_blocks_target",
            expected_output_value=True,
            expected_output_shape="goal_judgment_boolean",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    snapshot = _build_run_evidence_snapshot(
        ctx,
        _requested_output_result({"login_gate_blocks_target": True}),
    )
    with capture_logs() as logs:
        verdicts = grade_requested_output_criteria(ctx, criteria, snapshot)

    assert snapshot.block_output_sources[_POST_RUN_PAGE_OBSERVATION_LABEL] == "independent_page_evidence"
    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_source == "independent_page_evidence"
    assert verdicts[0].self_emitted_judgment_not_independent is False
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict" and log["verdict"] == "evidence_confirms" for log in logs
    )


def test_bound_independent_page_output_wins_over_self_emitted_judgment_value() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("login_gate_blocks_target")
    criteria = [
        _criterion(
            "c_login_gate",
            "The returned record reports whether the login gate blocks the target.",
            output_path="output.login_gate_blocks_target",
            expected_output_value=True,
            expected_output_shape="goal_judgment_boolean",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "extract_profile": {"login_gate_blocks_target": True},
                _POST_RUN_PAGE_OBSERVATION_LABEL: {"login_gate_blocks_target": True},
            },
            block_output_sources={
                "extract_profile": "runtime_output",
                _POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence",
            },
        ),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_ref == f"block_outputs:{_POST_RUN_PAGE_OBSERVATION_LABEL}.login_gate_blocks_target"
    assert verdicts[0].evidence_source == "independent_page_evidence"
    assert verdicts[0].self_emitted_judgment_not_independent is False


def test_value_less_emitted_false_judgment_is_refuted_by_login_gate_packet() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "output.login_gate_blocks_target",
                    "requested_output_evidence_source": "independent_run_evidence",
                    "judgment_predicate": "login_gate_blocks_target",
                    "judgment_polarity_when_holds": True,
                }
            ],
        }
    }
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="",
        **_gated_login_packet(),
    )
    criteria = [
        _criterion(
            "c_login_gate",
            "The returned record reports whether the login gate blocks the target.",
            output_path="output.login_gate_blocks_target",
            expected_output_shape="goal_judgment_boolean",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    snapshot = _build_run_evidence_snapshot(
        ctx,
        _requested_output_result({"login_gate_blocks_target": False}),
    )
    with capture_logs() as logs:
        verdicts = grade_requested_output_criteria(ctx, criteria, snapshot)

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == f"block_outputs:{_POST_RUN_PAGE_OBSERVATION_LABEL}"
    assert verdicts[0].evidence_source == "independent_page_evidence"
    assert verdicts[0].has_exact_value is False
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict"
        and log["predicate"] == "login_gate_blocks_target"
        and log["packet_label"] == _POST_RUN_PAGE_OBSERVATION_LABEL
        and log["verdict"] == "evidence_contradicts"
        for log in logs
    )


def test_producer_declared_value_less_judgment_reaches_packet_verdict() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "output.login_gate_blocks_target",
                    "requested_output_evidence_source": "independent_run_evidence",
                    "judgment_predicate": "login_gate_blocks_target",
                    "judgment_polarity_when_holds": True,
                }
            ],
        }
    }
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="",
        **_gated_login_packet(),
    )
    criteria = [
        _criterion(
            "c_login_gate",
            "The returned record reports whether the login gate blocks the target.",
            output_path="output.login_gate_blocks_target",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    snapshot = _build_run_evidence_snapshot(
        ctx,
        _requested_output_result({"login_gate_blocks_target": True}),
    )
    with capture_logs() as logs:
        verdicts = grade_requested_output_criteria(ctx, criteria, snapshot)

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].grounding_mode == "judgment_boolean"
    assert verdicts[0].has_exact_value is False
    assert verdicts[0].evidence_ref == f"block_outputs:{_POST_RUN_PAGE_OBSERVATION_LABEL}"
    assert verdicts[0].evidence_source == "independent_page_evidence"
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict"
        and log["predicate"] == "login_gate_blocks_target"
        and log["packet_label"] == _POST_RUN_PAGE_OBSERVATION_LABEL
        and log["verdict"] == "evidence_confirms"
        and log["origin"] == "producer_metadata"
        for log in logs
    )


def test_producer_declared_truth_condition_without_metadata_source_reaches_packet_verdict() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "output.login_gate_blocks_target",
                    "judgment_predicate": "login_gate_blocks_target",
                    "judgment_polarity_when_holds": True,
                }
            ],
        }
    }
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="",
        **_gated_login_packet(),
    )
    criteria = [
        _criterion(
            "c_login_gate",
            "The returned record reports whether the login gate blocks the target.",
            output_path="output.login_gate_blocks_target",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    snapshot = _build_run_evidence_snapshot(
        ctx,
        _requested_output_result({"login_gate_blocks_target": True}),
    )
    with capture_logs() as logs:
        verdicts = grade_requested_output_criteria(ctx, criteria, snapshot)

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].grounding_mode == "judgment_boolean"
    assert verdicts[0].has_exact_value is False
    assert verdicts[0].evidence_source == "independent_page_evidence"
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict"
        and log["predicate"] == "login_gate_blocks_target"
        and log["origin"] == "producer_metadata"
        for log in logs
    )


@pytest.mark.asyncio
async def test_definition_level_judgment_output_reaches_independent_packet_grader() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "output.login_gate_blocks_target",
                    "requested_output_evidence_source": "independent_run_evidence",
                    "judgment_predicate": "login_gate_blocks_target",
                    "judgment_polarity_when_holds": True,
                }
            ],
        }
    }
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="",
        **_gated_login_packet(),
    )
    criteria = [
        replace(
            _criterion(
                "c_login_gate",
                "The returned record reports whether the login gate blocks the target.",
                level="definition",
                output_path="output.login_gate_blocks_target",
                expected_output_value=True,
                expected_output_shape="goal_judgment_boolean",
                requested_output_evidence_source="independent_run_evidence",
            ),
            judgment_truth_condition=JudgmentTruthCondition(
                predicate="login_gate_blocks_target", polarity_when_holds=True
            ),
        )
    ]

    with capture_logs() as logs:
        verification = await _completion_verification_from_run_result(
            ctx,
            _requested_output_result({"login_gate_blocks_target": True}),
            time.monotonic(),
            criteria,
        )

    assert verification is not None
    assert verification.verdicts[0].state == "satisfied"
    assert verification.verdicts[0].reason_code == "evidence_confirms"
    assert verification.verdicts[0].evidence_source == "independent_page_evidence"
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict" and log["verdict"] == "evidence_confirms" for log in logs
    )


@pytest.mark.asyncio
async def test_authored_output_fallback_preserves_staged_judgment_truth_condition() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "output.login_gate_blocks_target",
                    "requested_output_evidence_source": "independent_run_evidence",
                    "judgment_predicate": "login_gate_blocks_target",
                    "judgment_polarity_when_holds": True,
                }
            ],
        }
    }
    ctx.workflow_verification_evidence = SimpleNamespace(code_artifact_metadata=metadata)
    ctx.code_artifact_metadata = {}
    ctx.request_policy = RequestPolicy(completion_criteria=build_classifier_fallback_floor([]))
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="",
        **_gated_login_packet(),
    )

    with capture_logs() as logs:
        verification = await _maybe_run_completion_verification(
            ctx,
            _requested_output_result({"login_gate_blocks_target": True}),
            time.monotonic(),
        )

    assert verification is not None
    verdict = verification.verdicts[0]
    assert verdict.criterion_id == "__copilot_authored_output__output_login_gate_blocks_target"
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "evidence_confirms"
    assert verdict.grounding_mode == "judgment_boolean"
    assert verdict.evidence_source == "independent_page_evidence"
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict"
        and log["predicate"] == "login_gate_blocks_target"
        and log["origin"] == "producer_metadata"
        for log in logs
    )


@pytest.mark.asyncio
async def test_authored_output_fallback_recovers_path_declared_judgment_truth_condition() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "output.login_gate_blocks_target",
                    "requested_output_evidence_source": "independent_run_evidence",
                }
            ],
        }
    }
    ctx.workflow_verification_evidence = SimpleNamespace(code_artifact_metadata=metadata)
    ctx.code_artifact_metadata = {}
    ctx.request_policy = RequestPolicy(completion_criteria=build_classifier_fallback_floor([]))
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="",
        **_gated_login_packet(),
    )

    with capture_logs() as logs:
        verification = await _maybe_run_completion_verification(
            ctx,
            _requested_output_result({"login_gate_blocks_target": True}),
            time.monotonic(),
        )

    assert verification is not None
    verdict = verification.verdicts[0]
    assert verdict.criterion_id == "__copilot_authored_output__output_login_gate_blocks_target"
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "evidence_confirms"
    assert verdict.grounding_mode == "judgment_boolean"
    assert verdict.evidence_source == "independent_page_evidence"
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict"
        and log["predicate"] == "login_gate_blocks_target"
        and log["origin"] == "producer_metadata"
        for log in logs
    )


@pytest.mark.asyncio
async def test_authored_output_duplicate_path_judgment_row_reaches_packet_grader() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "validate_login_gate_blocks_target")
    metadata = {
        "validate_login_gate_blocks_target": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "login_gate_blocks_target",
                    "requested_output_evidence_source": "runtime_output",
                },
                {
                    "output_path": "login_gate_blocks_target",
                    "requested_output_evidence_source": "independent_run_evidence",
                    "judgment_predicate": "login_gate_blocks_target",
                    "judgment_polarity_when_holds": True,
                },
            ],
        }
    }
    ctx.workflow_verification_evidence = SimpleNamespace(code_artifact_metadata=metadata)
    ctx.code_artifact_metadata = {}
    ctx.request_policy = RequestPolicy(completion_criteria=build_classifier_fallback_floor([]))
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="",
        **_gated_login_packet(),
    )

    with capture_logs() as logs:
        verification = await _maybe_run_completion_verification(
            ctx,
            {
                "ok": True,
                "data": {
                    "workflow_run_id": "wr_requested_output",
                    "overall_status": "completed",
                    "executed_block_labels": ["validate_login_gate_blocks_target"],
                    "current_url": "https://example.test/profile",
                    "blocks": [
                        {
                            "label": "validate_login_gate_blocks_target",
                            "block_type": "CODE",
                            "status": "completed",
                            "extracted_data": {"login_gate_blocks_target": False},
                        }
                    ],
                },
            },
            time.monotonic(),
        )

    assert verification is not None
    verdict = verification.verdicts[0]
    assert verdict.criterion_id == "__copilot_authored_output__output_login_gate_blocks_target"
    assert verdict.state == "unsatisfied"
    assert verdict.reason_code == "evidence_contradicts"
    assert verdict.grounding_mode == "judgment_boolean"
    assert verdict.evidence_source == "independent_page_evidence"
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict"
        and log["predicate"] == "login_gate_blocks_target"
        and log["verdict"] == "evidence_contradicts"
        for log in logs
    )


def test_judgment_truth_condition_refutes_inverted_login_gate_packet() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("login_gate_blocks_target")
    criteria = [
        replace(
            _criterion(
                "c_login_gate",
                "The returned record reports whether the login gate blocks the target.",
                output_path="output.login_gate_blocks_target",
                expected_output_value=True,
                requested_output_evidence_source="independent_run_evidence",
            ),
            judgment_truth_condition=JudgmentTruthCondition(
                predicate="login_gate_blocks_target", polarity_when_holds=True
            ),
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={_POST_RUN_PAGE_OBSERVATION_LABEL: _target_reached_packet()},
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == f"block_outputs:{_POST_RUN_PAGE_OBSERVATION_LABEL}"
    assert verdicts[0].evidence_source == "independent_page_evidence"


def _dispatched_judgment_criterion() -> CompletionCriterion:
    return replace(
        _criterion(
            "c_login_gate",
            "The returned record reports whether the login gate blocks the target.",
            output_path="output.login_gate_blocks_target",
            expected_output_value=True,
            requested_output_evidence_source="independent_run_evidence",
        ),
        judgment_truth_condition=JudgmentTruthCondition(predicate="login_gate_blocks_target", polarity_when_holds=True),
    )


async def _dispatched_chain_snapshot(
    monkeypatch: pytest.MonkeyPatch, ctx: CopilotContext, html: str
) -> RunEvidenceSnapshot:
    stub_artifact_app(
        monkeypatch,
        [make_stub_html_artifact("art_terminal", ArtifactType.HTML_ACTION)],
        {"art_terminal": html.encode()},
    )
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_requested_output", organization_id="o", current_url=""
    )
    return _build_run_evidence_snapshot(ctx, _requested_output_result({"login_gate_blocks_target": True}))


@pytest.mark.asyncio
async def test_dispatched_chain_login_gate_packet_confirms_judgment_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("login_gate_blocks_target")
    snapshot = await _dispatched_chain_snapshot(monkeypatch, ctx, DISPATCHED_LOGIN_GATE_HTML)

    with capture_logs() as logs:
        verdicts = grade_requested_output_criteria(ctx, [_dispatched_judgment_criterion()], snapshot)

    bound = snapshot.block_outputs[_POST_RUN_PAGE_OBSERVATION_LABEL]
    assert snapshot.block_output_sources[_POST_RUN_PAGE_OBSERVATION_LABEL] == "independent_page_evidence"
    assert bound["forms"]
    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_ref == f"block_outputs:{_POST_RUN_PAGE_OBSERVATION_LABEL}"
    assert verdicts[0].evidence_source == "independent_page_evidence"
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict"
        and log["predicate"] == "login_gate_blocks_target"
        and log["packet_label"] == _POST_RUN_PAGE_OBSERVATION_LABEL
        and log["verdict"] == "evidence_confirms"
        for log in logs
    )


@pytest.mark.asyncio
async def test_dispatched_chain_result_container_packet_contradicts_login_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("login_gate_blocks_target")
    snapshot = await _dispatched_chain_snapshot(monkeypatch, ctx, DISPATCHED_RESULTS_HTML)

    with capture_logs() as logs:
        verdicts = grade_requested_output_criteria(ctx, [_dispatched_judgment_criterion()], snapshot)

    bound = snapshot.block_outputs[_POST_RUN_PAGE_OBSERVATION_LABEL]
    assert bound["result_containers"]
    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"
    assert verdicts[0].evidence_ref == f"block_outputs:{_POST_RUN_PAGE_OBSERVATION_LABEL}"
    assert verdicts[0].evidence_source == "independent_page_evidence"
    assert all(verdict.reason_code != "structurally_abstained" for verdict in verdicts)
    assert any(
        log["event"] == "copilot_judgment_evidence_verdict"
        and log["predicate"] == "login_gate_blocks_target"
        and log["verdict"] == "evidence_contradicts"
        for log in logs
    )


@pytest.mark.asyncio
async def test_dispatched_chain_nav_only_page_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dispatched captures parse with no current URL, so the same-origin filter drops every
    # navigation target and a nav-only terminal page yields an undecidable hollow packet.
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("login_gate_blocks_target")
    snapshot = await _dispatched_chain_snapshot(monkeypatch, ctx, DISPATCHED_NAV_ONLY_HTML)

    verdicts = grade_requested_output_criteria(ctx, [_dispatched_judgment_criterion()], snapshot)

    bound = snapshot.block_outputs[_POST_RUN_PAGE_OBSERVATION_LABEL]
    assert bound["navigation_targets"] == []
    assert bound["forms"] == []
    assert bound["result_containers"] == []
    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].evidence_source == "independent_page_evidence"


def test_judgment_truth_condition_hollow_packet_abstains_before_self_emitted_output() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("login_gate_blocks_target")
    criteria = [
        replace(
            _criterion(
                "c_login_gate",
                "The returned record reports whether the login gate blocks the target.",
                output_path="output.login_gate_blocks_target",
                expected_output_value=True,
                requested_output_evidence_source="independent_run_evidence",
            ),
            judgment_truth_condition=JudgmentTruthCondition(
                predicate="login_gate_blocks_target", polarity_when_holds=True
            ),
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "extract_profile": {"login_gate_blocks_target": True},
                _POST_RUN_PAGE_OBSERVATION_LABEL: {
                    "forms": [],
                    "navigation_targets": [],
                    "result_containers": [],
                },
            },
            block_output_sources={
                "extract_profile": "runtime_output",
                _POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence",
            },
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "structurally_abstained"
    assert verdicts[0].evidence_ref == f"block_outputs:{_POST_RUN_PAGE_OBSERVATION_LABEL}"
    assert verdicts[0].evidence_source == "independent_page_evidence"
    assert verdicts[0].self_emitted_judgment_not_independent is False


def test_judgment_truth_condition_refuses_fabricated_packet_from_registered_output() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("login_gate_blocks_target")
    criteria = [
        replace(
            _criterion(
                "c_login_gate",
                "The returned record reports whether the login gate blocks the target.",
                output_path="output.login_gate_blocks_target",
                expected_output_value=True,
                requested_output_evidence_source="independent_run_evidence",
            ),
            judgment_truth_condition=JudgmentTruthCondition(
                predicate="login_gate_blocks_target", polarity_when_holds=True
            ),
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "extract_profile": {"login_gate_blocks_target": True},
                "fake_packet_output": _gated_login_packet(),
            },
            block_output_sources={
                "extract_profile": "runtime_output",
                "fake_packet_output": "registered_output_parameter",
            },
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code != "evidence_confirms"
    assert verdicts[0].evidence_ref != "block_outputs:fake_packet_output"


def test_judgment_truth_condition_ignores_packet_label_and_visible_text() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("login_gate_blocks_target")
    criteria = [
        replace(
            _criterion(
                "c_login_gate",
                "The returned record reports whether the login gate blocks the target.",
                output_path="output.login_gate_blocks_target",
                expected_output_value=True,
                requested_output_evidence_source="independent_run_evidence",
            ),
            judgment_truth_condition=JudgmentTruthCondition(
                predicate="login_gate_blocks_target", polarity_when_holds=True
            ),
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                "login_gate_blocks_target": {
                    "visible_text_excerpt": "Login required. Enter password to continue.",
                    "evidence_text": "Login required.",
                }
            },
            block_output_sources={"login_gate_blocks_target": "independent_page_evidence"},
        ),
    )

    assert verdicts[0].reason_code != "evidence_confirms"


def test_producer_declared_judgment_truth_condition_fallback_confirms_packet() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "output.login_gate_blocks_target",
                    "requested_output_evidence_source": "independent_run_evidence",
                    "judgment_predicate": "login_gate_blocks_target",
                    "judgment_polarity_when_holds": True,
                }
            ],
        }
    }
    criteria = [
        _criterion(
            "c_login_gate",
            "The returned record reports whether the login gate blocks the target.",
            output_path="output.login_gate_blocks_target",
            expected_output_value=True,
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={_POST_RUN_PAGE_OBSERVATION_LABEL: _login_gate_packet()},
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"


def test_criterion_judgment_truth_condition_takes_precedence_over_metadata() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = {
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": ["login_gate_blocks_target"]}],
            "completion_criteria": [
                {
                    "output_path": "output.login_gate_blocks_target",
                    "requested_output_evidence_source": "independent_run_evidence",
                    "judgment_predicate": "login_gate_blocks_target",
                    "judgment_polarity_when_holds": True,
                }
            ],
        }
    }
    criteria = [
        replace(
            _criterion(
                "c_login_gate",
                "The returned record reports whether the login gate blocks the target.",
                output_path="output.login_gate_blocks_target",
                expected_output_value=False,
                requested_output_evidence_source="independent_run_evidence",
            ),
            judgment_truth_condition=JudgmentTruthCondition(
                predicate="login_gate_blocks_target", polarity_when_holds=False
            ),
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={_POST_RUN_PAGE_OBSERVATION_LABEL: _login_gate_packet()},
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"


def _judgment_shape_abstention() -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id="c_judgment",
        state="unsatisfied",
        reason_code="structurally_abstained",
        evidence_ref="block_outputs:extract_profile.selected_highest_priority",
        output_path="output.selected_highest_priority",
        grounding_mode="judgment_boolean",
        has_exact_value=False,
        requested_output_evidence_source="independent_run_evidence",
    )


def test_judgment_abstention_gets_no_floor_credit_from_generic_corroboration() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_judgment"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_reach",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="observed_end_state_url",
            ),
            _judgment_shape_abstention(),
        ],
    )

    assert result.is_fully_satisfied() is False


def test_value_less_judgment_abstention_never_certifies_via_independent_corroborator() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_judgment"],
        verdicts=[
            _judgment_shape_abstention(),
            CriterionVerdict(
                criterion_id="c_judgment__requested_output_corroborator",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_source="independent_page_evidence",
            ),
        ],
    )

    assert result.is_fully_satisfied() is False


def test_judgment_abstention_rejects_non_independent_corroborator() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_judgment"],
        verdicts=[
            _judgment_shape_abstention(),
            CriterionVerdict(
                criterion_id="c_judgment__requested_output_corroborator",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_source="runtime_output",
            ),
        ],
    )

    assert result.is_fully_satisfied() is False


def test_coerce_result_stamps_evidence_source_from_snapshot_sources() -> None:
    raw = {
        "verdicts": [
            {
                "criterion_id": "c_corroborator",
                "satisfied": True,
                "reason_code": "evidence_confirms",
                "evidence_ref": "block_outputs:current_page_observation.selected_highest_priority",
            }
        ]
    }

    result = _coerce_result(
        raw,
        ["c_corroborator"],
        block_output_sources={"current_page_observation": "independent_page_evidence"},
    )

    assert result.verdicts[0].evidence_source == "independent_page_evidence"


def _verdict_for(result: CompletionVerificationResult, criterion_id: str) -> CriterionVerdict:
    return next(verdict for verdict in result.verdicts if verdict.criterion_id == criterion_id)


def test_requested_output_corroborator_same_record_self_confirmation_fails_closed() -> None:
    run_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", "c1", "c1__requested_output_corroborator"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c0",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="block_outputs:submit_turn_on_request",
                grounding_mode="terminal_record",
                evidence_source="terminal_record",
            ),
            CriterionVerdict(
                criterion_id="c1",
                state="unsatisfied",
                reason_code="structurally_abstained",
                evidence_ref="block_outputs:submit_turn_on_request.confirmation_number",
                output_path="output.confirmation_number",
                grounding_mode="shape",
                requested_output_evidence_source="runtime_output",
                evidence_source="runtime_output",
            ),
            CriterionVerdict(
                criterion_id="c1__requested_output_corroborator",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="submit_turn_on_request_output",
            ),
        ],
    )

    result = combine_verification_results(["c0", "c1", "c1__requested_output_corroborator"], run_result, [])

    corroborator = _verdict_for(result, "c1__requested_output_corroborator")
    assert corroborator.state == "unsatisfied"
    assert corroborator.reason_code == "evidence_contradicts"
    assert result.is_fully_satisfied() is False


def test_requested_output_corroborator_distinct_record_still_satisfies() -> None:
    run_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c1", "c1__requested_output_corroborator"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c1",
                state="unsatisfied",
                reason_code="structurally_abstained",
                evidence_ref="block_outputs:submit_turn_on_request.confirmation_number",
                output_path="output.confirmation_number",
                grounding_mode="shape",
                requested_output_evidence_source="runtime_output",
                evidence_source="runtime_output",
            ),
            CriterionVerdict(
                criterion_id="c1__requested_output_corroborator",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="observed_end_state_url",
                evidence_source="independent_page_evidence",
            ),
        ],
    )

    result = combine_verification_results(["c1", "c1__requested_output_corroborator"], run_result, [])

    corroborator = _verdict_for(result, "c1__requested_output_corroborator")
    assert corroborator.state == "satisfied"
    assert corroborator.reason_code == "evidence_confirms"


def test_coerce_result_stamps_evidence_source_from_bare_label_ref() -> None:
    raw = {
        "verdicts": [
            {
                "criterion_id": "c_corroborator",
                "satisfied": True,
                "reason_code": "evidence_confirms",
                "evidence_ref": "current_page_observation",
            }
        ]
    }

    result = _coerce_result(
        raw,
        ["c_corroborator"],
        block_output_sources={"current_page_observation": "independent_page_evidence"},
    )

    assert result.verdicts[0].evidence_source == "independent_page_evidence"


def test_bare_label_corroborator_certifies_self_emitted_judgment() -> None:
    coerced_corroborator = _coerce_result(
        {
            "verdicts": [
                {
                    "criterion_id": "c_judgment__requested_output_corroborator",
                    "satisfied": True,
                    "reason_code": "evidence_confirms",
                    "evidence_ref": "current_page_observation",
                }
            ]
        },
        ["c_judgment__requested_output_corroborator"],
        block_output_sources={"current_page_observation": "independent_page_evidence"},
    ).verdicts[0]
    run_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_reach", "c_judgment", "c_judgment__requested_output_corroborator"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_reach",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="observed_end_state_url",
            ),
            CriterionVerdict(
                criterion_id="c_judgment",
                state="unsatisfied",
                reason_code="structurally_abstained",
                evidence_ref="block_outputs:select_document.selected_highest_priority",
                output_path="output.selected_highest_priority",
                grounding_mode="judgment_boolean",
                requested_output_evidence_source="independent_run_evidence",
                evidence_source="runtime_output",
                self_emitted_judgment_not_independent=True,
            ),
            coerced_corroborator,
        ],
    )

    result = combine_verification_results(
        ["c_reach", "c_judgment", "c_judgment__requested_output_corroborator"], run_result, []
    )

    assert coerced_corroborator.evidence_source == "independent_page_evidence"
    assert result.is_fully_satisfied() is True


def test_requested_output_corroborator_same_record_not_flipped_when_source_is_independent() -> None:
    run_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c1", "c1__requested_output_corroborator"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c1",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="block_outputs:submit_turn_on_request.confirmation_number",
                output_path="output.confirmation_number",
                grounding_mode="shape",
                requested_output_evidence_source="independent_run_evidence",
                evidence_source="independent_page_evidence",
            ),
            CriterionVerdict(
                criterion_id="c1__requested_output_corroborator",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="submit_turn_on_request_output",
                evidence_source="independent_page_evidence",
            ),
        ],
    )

    result = combine_verification_results(["c1", "c1__requested_output_corroborator"], run_result, [])

    corroborator = _verdict_for(result, "c1__requested_output_corroborator")
    assert corroborator.state == "satisfied"
    assert corroborator.reason_code == "evidence_confirms"


def _post_run_page_evidence(*, run_id: str, visible_text_excerpt: str, **structured: object) -> dict[str, Any]:
    return {
        "workflow_run_id": run_id,
        "observed_after_workflow_run": True,
        "current_url": "https://example.test/confirmation",
        "visible_text_excerpt": visible_text_excerpt,
        **structured,
    }


def test_same_run_post_run_page_evidence_binds_structured_under_reserved_independent_label() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="Confirmation Number WTR-1842-DEMO",
        confirmation_number="WTR-1842-DEMO",
    )

    snapshot = _build_run_evidence_snapshot(ctx, _requested_output_result({"note": "ok"}))

    bound = snapshot.block_outputs[_POST_RUN_PAGE_OBSERVATION_LABEL]
    assert snapshot.block_output_sources[_POST_RUN_PAGE_OBSERVATION_LABEL] == "independent_page_evidence"
    assert bound["confirmation_number"] == "WTR-1842-DEMO"
    assert bound["visible_text_excerpt"] == "Confirmation Number WTR-1842-DEMO"
    assert "workflow_run_id" not in bound
    assert "observed_after_workflow_run" not in bound
    assert snapshot.block_output_sources["extract_profile"] == "runtime_output"


def test_same_run_post_run_page_evidence_positive_arm_confirms_reached_value() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_requested_output",
        visible_text_excerpt="Confirmation Number WTR-1842-DEMO",
        confirmation_number="WTR-1842-DEMO",
    )

    snapshot = _build_run_evidence_snapshot(ctx, _requested_output_result({"note": "ok"}))
    verdicts = grade_requested_output_criteria(
        ctx,
        [
            _criterion(
                "c_confirmation_number",
                "The returned record includes confirmation number.",
                output_path="output.confirmation_number",
                expected_output_value="WTR-1842-DEMO",
                requested_output_evidence_source="independent_run_evidence",
            )
        ],
        snapshot,
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_source == "independent_page_evidence"
    result = CompletionVerificationResult(
        status="evaluated", criterion_ids=["c_confirmation_number"], verdicts=verdicts
    )
    assert result.is_fully_satisfied() is True


def test_post_run_page_evidence_from_a_different_run_is_not_bound() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.composition_page_evidence = _post_run_page_evidence(
        run_id="wr_stale_prior_run",
        visible_text_excerpt="Confirmation Number WTR-1842-DEMO",
    )

    snapshot = _build_run_evidence_snapshot(ctx, _requested_output_result({"note": "ok"}))

    assert _POST_RUN_PAGE_OBSERVATION_LABEL not in snapshot.block_outputs
    assert _POST_RUN_PAGE_OBSERVATION_LABEL not in snapshot.block_output_sources


def test_pre_run_page_evidence_without_post_run_stamp_is_not_bound() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_requested_output",
        "visible_text_excerpt": "Confirmation Number WTR-1842-DEMO",
    }

    snapshot = _build_run_evidence_snapshot(ctx, _requested_output_result({"note": "ok"}))

    assert _POST_RUN_PAGE_OBSERVATION_LABEL not in snapshot.block_outputs


def test_independent_page_text_substring_alone_never_confirms_reached_value() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        _criterion(
            "c_confirmation_number",
            "The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                _POST_RUN_PAGE_OBSERVATION_LABEL: {
                    "visible_text_excerpt": "Water Service Request Submitted. Confirmation Number WTR-1842-DEMO.",
                }
            },
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"
    assert verdicts[0].evidence_source is None


def test_generic_static_page_chrome_phrase_never_confirms_on_non_reached_goal() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("status")
    criteria = [
        _criterion(
            "c_status",
            "The submission status is returned.",
            output_path="output.status",
            expected_output_value="Application submitted successfully",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                _POST_RUN_PAGE_OBSERVATION_LABEL: {
                    "visible_text_excerpt": "Home  Application submitted successfully  Contact us",
                }
            },
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"
    assert verdicts[0].evidence_source is None


def test_independent_page_evidence_text_alone_still_does_not_confirm_authored_prose() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        _criterion(
            "c_confirmation_number",
            "The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                _POST_RUN_PAGE_OBSERVATION_LABEL: {
                    "evidence_text": "Water Service Request Submitted. Confirmation Number WTR-1842-DEMO.",
                }
            },
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"


def test_independent_page_text_never_confirms_a_boolean_via_prose() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("passed_validation")
    criteria = [
        replace(
            _criterion(
                "c_passed_validation",
                "Validation passed.",
                output_path="output.passed_validation",
                expected_output_shape="goal_judgment_boolean",
                requested_output_evidence_source="independent_run_evidence",
            ),
            expected_output_value=True,
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                _POST_RUN_PAGE_OBSERVATION_LABEL: {"visible_text_excerpt": "Status: true. Everything looks true."}
            },
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code != "evidence_confirms"


def test_structured_contradiction_masks_the_text_confirmation_door() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        _criterion(
            "c_confirmation_number",
            "The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                _POST_RUN_PAGE_OBSERVATION_LABEL: {
                    "confirmation_number": "WTR-9999-OTHER",
                    "visible_text_excerpt": "Confirmation Number WTR-1842-DEMO",
                }
            },
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "evidence_contradicts"


def test_registered_artifact_content_confirms_structured_value() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("confirmation_number")
    criteria = [
        _criterion(
            "c_confirmation_number",
            "The returned record includes confirmation number.",
            output_path="output.confirmation_number",
            expected_output_value="WTR-1842-DEMO",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"artifact_content": {"confirmation_number": "WTR-1842-DEMO"}},
            block_output_sources={"artifact_content": "registered_artifact_content"},
        ),
    )

    assert verdicts[0].state == "satisfied"
    assert verdicts[0].reason_code == "evidence_confirms"
    assert verdicts[0].evidence_source == "registered_artifact_content"


def test_self_emitted_judgment_without_independent_corroborator_still_vetoes() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("selected_highest_priority")
    criteria = [
        _criterion(
            "c_selected",
            "The highest-priority document was correctly selected.",
            output_path="output.selected_highest_priority",
            expected_output_shape="goal_judgment_boolean",
            requested_output_evidence_source="independent_run_evidence",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={"extract_profile": {"selected_highest_priority": True}},
            block_output_sources={"extract_profile": "runtime_output"},
        ),
    )

    assert verdicts[0].self_emitted_judgment_not_independent is True
    result = CompletionVerificationResult(status="evaluated", criterion_ids=["c_selected"], verdicts=verdicts)
    assert result.is_fully_satisfied() is False


def test_producer_floor_rekeys_presence_only_requested_output_to_run_outcome() -> None:
    criteria = [
        _criterion(
            "c_presence_only",
            "The confirmation number is returned.",
            output_path="output.confirmation_number",
        )
    ]

    floored, rekeyed_paths = apply_requested_output_producer_floor(criteria)

    assert rekeyed_paths == ("output.confirmation_number",)
    assert floored[0].output_path is None
    assert floored[0].level == "run"
    assert floored[0].kind == "outcome"
    assert floored[0].outcome == "The confirmation number is returned."
    requested, _remaining = split_requested_output_criteria(list(floored))
    assert requested == []


def test_producer_floor_leaves_typed_value_shape_and_judgment_booleans_untouched() -> None:
    criteria = [
        _criterion(
            "c_value",
            "The NPI is returned.",
            output_path="output.npi",
            expected_output_value="1234567890",
        ),
        _criterion(
            "c_shape",
            "The confirmation number is returned.",
            output_path="output.confirmation_number",
            expected_output_shape="reference_code",
        ),
        _criterion(
            "c_bool",
            "Validation passed.",
            output_path="output.passed_validation",
            expected_output_shape="goal_judgment_boolean",
            requested_output_evidence_source="independent_run_evidence",
        ),
    ]

    floored, rekeyed_paths = apply_requested_output_producer_floor(criteria)

    assert rekeyed_paths == ()
    assert floored == tuple(criteria)


def test_producer_floor_is_idempotent() -> None:
    criteria = [
        _criterion(
            "c_presence_only",
            "The confirmation number is returned.",
            output_path="output.confirmation_number",
        )
    ]

    once, _rekeyed = apply_requested_output_producer_floor(criteria)
    twice, rekeyed_twice = apply_requested_output_producer_floor(once)

    assert rekeyed_twice == ()
    assert twice == once


@pytest.mark.parametrize("generic_expected", ["Yes", "Submitted", "Approved", "7", "in progress", "not found"])
def test_short_generic_expected_value_does_not_confirm_via_page_chrome(generic_expected: str) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("status")
    criteria = [
        _criterion(
            "c_status",
            "The returned record includes status.",
            output_path="output.status",
            expected_output_value=generic_expected,
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                _POST_RUN_PAGE_OBSERVATION_LABEL: {
                    "visible_text_excerpt": f"Home Submitted Approved Yes No 7 items {generic_expected}",
                }
            },
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code != "evidence_confirms"


@pytest.mark.parametrize(
    "distinctive_expected, page_text",
    [
        ("Order 84213 confirmed", "Your Order 84213 confirmed. Thank you for your purchase."),
        ("INV-2024-001", "Invoice INV-2024-001 has been generated and emailed."),
    ],
)
def test_distinctive_expected_value_in_page_text_alone_no_longer_confirms(
    distinctive_expected: str, page_text: str
) -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_for_requested_paths("reference")
    criteria = [
        _criterion(
            "c_reference",
            "The returned record includes reference.",
            output_path="output.reference",
            expected_output_value=distinctive_expected,
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={_POST_RUN_PAGE_OBSERVATION_LABEL: {"visible_text_excerpt": page_text}},
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code == "missing_exact_field"
    assert verdicts[0].evidence_source is None


def test_judgment_declared_string_value_does_not_confirm_via_page_text_door() -> None:
    ctx = _run_ctx()
    ctx.code_artifact_metadata = _metadata_with_declared_independent_criterion("select_plan", "output.selected_plan")
    criteria = [
        _criterion(
            "c_selected_plan",
            "The cheapest plan was selected.",
            output_path="output.selected_plan",
            expected_output_value="Plan-Gold-4000",
            requested_output_evidence_source="runtime_output",
        )
    ]

    verdicts = grade_requested_output_criteria(
        ctx,
        criteria,
        RunEvidenceSnapshot(
            block_outputs={
                _POST_RUN_PAGE_OBSERVATION_LABEL: {
                    "visible_text_excerpt": "Compare plans: Plan-Silver-2000, Plan-Gold-4000, Plan-Bronze-1000.",
                }
            },
            block_output_sources={_POST_RUN_PAGE_OBSERVATION_LABEL: "independent_page_evidence"},
        ),
    )

    assert verdicts[0].state == "unsatisfied"
    assert verdicts[0].reason_code != "evidence_confirms"


def test_bound_post_run_page_evidence_drops_only_stamp_keys() -> None:
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.composition_page_evidence = {
        "workflow_run_id": "wr_requested_output",
        "observed_after_workflow_run": True,
        "current_url": "https://example.test/confirmation",
        "visible_text_excerpt": "Confirmation Number WTR-1842-DEMO",
        "status": "shipped",
    }

    snapshot = _build_run_evidence_snapshot(ctx, _requested_output_result({"note": "ok"}))

    bound = snapshot.block_outputs[_POST_RUN_PAGE_OBSERVATION_LABEL]
    assert bound == {
        "current_url": "https://example.test/confirmation",
        "visible_text_excerpt": "Confirmation Number WTR-1842-DEMO",
        "status": "shipped",
    }
    assert "workflow_run_id" not in bound
    assert "observed_after_workflow_run" not in bound
