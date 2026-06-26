from __future__ import annotations

import asyncio
import textwrap
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot.agent import (
    _completion_contract_not_violated,
    _rewrite_failed_test_response,
    _verified_workflow_or_none,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    RunEvidenceSnapshot,
    _coerce_result,
    _structured_record_has_identifier,
    combine_verification_results,
    evaluate_completion_criteria,
    grade_definition_criteria,
    grade_present_value_criteria,
    grade_record_semantic_consistency,
    grade_structured_record_criteria,
    grade_terminal_goal_record_criteria,
    structural_unfired_contingent_criterion_ids,
    structured_record_has_goal_content,
    structured_record_has_identity,
    summarize_unsatisfied_outcomes,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairNextAction,
    VerificationResult,
    _verification_satisfaction,
)
from skyvern.forge.sdk.copilot.enforcement import (
    outcome_fully_verified,
    verified_goal_satisfied_context,
)
from skyvern.forge.sdk.copilot.hooks import _tool_completion_satisfies_turn
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    RequestPolicy,
    _parse_completion_criteria,
    build_classifier_fallback_floor,
)
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
from skyvern.forge.sdk.copilot.tools.completion import _artifact_health_blocker_from_result


def _criterion(
    cid: str,
    outcome: str,
    *,
    method_mandated: bool = False,
    output_path: str | None = None,
    contingent_on: str | None = None,
    contingent_antecedent_output_path: str | None = None,
) -> CompletionCriterion:
    return CompletionCriterion(
        id=cid,
        outcome=outcome,
        method_mandated=method_mandated,
        output_path=output_path,
        contingent_on=contingent_on,
        contingent_antecedent_output_path=contingent_antecedent_output_path,
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
        [_criterion("c0", "a commercial water service request is submitted")], snapshot
    )

    assert verdicts == [
        CriterionVerdict(
            criterion_id="c0",
            state="satisfied",
            reason_code="evidence_confirms",
            evidence_ref="block_outputs:submit_water_request",
        )
    ]


def test_terminal_goal_record_accepts_family_artifact_without_self_asserted_boolean() -> None:
    payload = _terminal_goal_payload(submitted=None)
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_water_request": payload})

    verdicts = grade_terminal_goal_record_criteria(
        [_criterion("c0", "a commercial water service request is submitted")], snapshot
    )

    assert _satisfied_criterion_ids(verdicts) == {"c0"}


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
            [_criterion("c0", "a commercial water service request is submitted")], snapshot
        )
        == []
    )


@pytest.mark.parametrize(
    ("payload", "outcome"),
    [
        (
            _terminal_goal_payload(confirmation_number="WTR-1842-DEMO"),
            "a commercial water service request is submitted",
        ),
        (_terminal_goal_payload(order_placed=True, submitted=None, order_number="ORD-1842"), "an order is placed"),
        (
            _terminal_goal_payload(application_submitted=True, submitted=None, application_id="APP-1842"),
            "an application is submitted",
        ),
        (
            _terminal_goal_payload(form_submitted=True, submitted=None, submission_id="SUB-1842"),
            "the form is submitted",
        ),
        (
            _terminal_goal_payload(request_submitted=True, submitted=None, request_id="REQ-1842"),
            "a service request is submitted",
        ),
    ],
)
def test_terminal_goal_record_accepts_narrow_terminal_artifacts(payload: dict[str, Any], outcome: str) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    verdicts = grade_terminal_goal_record_criteria([_criterion("c0", outcome)], snapshot)

    assert _satisfied_criterion_ids(verdicts) == {"c0"}


@pytest.mark.parametrize(
    ("payload", "outcome"),
    [
        (_terminal_goal_payload(), "an order is placed"),
        (_terminal_goal_payload(order_placed=True, submitted=None, order_number="ORD-1842"), "a request is submitted"),
        (
            _terminal_goal_payload(application_submitted=True, submitted=None, application_id="APP-1842"),
            "an order is placed",
        ),
    ],
)
def test_terminal_goal_record_rejects_mismatched_families(payload: dict[str, Any], outcome: str) -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": payload})

    assert grade_terminal_goal_record_criteria([_criterion("c0", outcome)], snapshot) == []


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

    assert grade_terminal_goal_record_criteria([_criterion("c0", "a request is submitted")], snapshot) == []


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
            [_criterion("c0", "a commercial water service request is submitted")], snapshot
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
            [_criterion("c0", "a commercial water service request is submitted")], snapshot
        )
        == []
    )


def test_terminal_goal_record_does_not_take_literal_criteria_from_present_value() -> None:
    criteria = [_criterion("c0", "the confirmation number WTR-1842-DEMO is reported")]
    snapshot = RunEvidenceSnapshot(block_outputs={"terminal_result": _terminal_goal_payload()})

    assert grade_terminal_goal_record_criteria(criteria, snapshot) == []
    assert _satisfied_criterion_ids(grade_present_value_criteria(criteria, snapshot)) == {"c0"}


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


def test_verification_satisfaction_no_cvr_uses_prior_proxy() -> None:
    assert _verification_satisfaction(True, False, "completed", None) == (True, True)
    assert _verification_satisfaction(True, True, "completed", None) == (False, False)
    assert _verification_satisfaction(False, False, None, None) == (None, None)


def test_verification_satisfaction_evaluated_drives_contract_signal() -> None:
    assert _verification_satisfaction(True, False, "completed", _evaluated(("c0", True))) == (True, True)
    _, contract = _verification_satisfaction(True, False, "completed", _evaluated(("c0", False)))
    assert contract is False


def test_verification_satisfaction_unavailable_fails_closed() -> None:
    _, contract = _verification_satisfaction(True, False, "completed", CompletionVerificationResult("unavailable"))
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
        "extract_profile": {
            "claimed_outcomes": [{"goal_value_paths": list(paths)}],
            "terminal_verifier_expectations": [{"goal_value_paths": list(paths)}],
        }
    }


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
    assert _outcome_failure_warrants_repair(nav_only, None) is False
    # Contradiction is a real failure regardless of which blocks exist.
    assert _outcome_failure_warrants_repair(nav_only, _contradicted("c0")) is True
    # Absence of evidence: failure only once a confirmation block exists.
    assert _outcome_failure_warrants_repair(has_block, _evaluated(("c0", False))) is True
    assert _outcome_failure_warrants_repair(nav_only, _evaluated(("c0", False))) is False


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
    assert result["data"]["full_workflow_verified"] is False
    assert result["data"]["failure_categories"][0]["category"] == ACTIVE_RUN_TERMINAL_EVIDENCE_FAILURE_CATEGORY


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
        completion_criteria=[_criterion("c0", "a commercial water service request is submitted")]
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
            _criterion("c0", "a commercial water service request is submitted"),
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
    assert verification.verdicts[0].reason_code == "missing_exact_field"


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
    assert verification.verdicts[0].reason_code == "missing_exact_field"


@pytest.mark.asyncio
async def test_requested_output_path_exact_runtime_field_satisfies(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("exact requested-output evidence must bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
    ctx.request_policy = RequestPolicy(
        completion_criteria=[CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi")]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"npi": "1234567890"}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True
    assert verification.verdicts[0].evidence_ref == "block_outputs:extract_profile.npi"


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
    assert verification.verdicts[0].reason_code == "missing_exact_field"


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
            CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path=requested_path)
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
        completion_criteria=[CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi")]
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
        completion_criteria=[CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi")]
    )

    verification = await _maybe_run_completion_verification(
        ctx,
        _requested_output_result({"npi": "1234567890"}),
        time.monotonic(),
    )

    assert verification is not None
    assert verification.is_fully_satisfied() is True


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
            CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.[].npi")
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
            CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi"),
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
    assert verification.verdicts[0].reason_code == "missing_exact_field"


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
    assert verdicts["c_npi"].reason_code == "missing_exact_field"
    assert verdicts["c_npi"].satisfied is False
    assert verdicts["c_cart"].state == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_code", ["missing_exact_field", "unproducible"])
async def test_unfired_contingent_requested_output_miss_does_not_veto_satisfied_run_criterion(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("deterministic requested-output and present-value criteria should bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    if reason_code == "missing_exact_field":
        ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
        output = {"status": "DONE", "blocker": False}
    else:
        output = {"status": "DONE", "blocker": False, "npi": "1234567890"}
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
@pytest.mark.parametrize("reason_code", ["missing_exact_field", "unproducible"])
async def test_fired_contingent_requested_output_miss_still_vetoes(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    async def fail_handler(**_: object) -> object:
        raise AssertionError("deterministic requested-output and present-value criteria should bypass the judge")

    _patch_completion_handler(monkeypatch, fail_handler)
    ctx = _run_ctx()
    _set_workflow_labels(ctx, "extract_profile")
    if reason_code == "missing_exact_field":
        ctx.code_artifact_metadata = _metadata_for_requested_paths("npi")
        output = {"status": "DONE", "blocker": True}
    else:
        output = {"status": "DONE", "blocker": True, "npi": "1234567890"}
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
            CompletionCriterion(id="c_npi", outcome="The NPI is returned.", output_path="output.npi"),
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
        completion_criteria=[_criterion("c0", "a commercial water service request is submitted")]
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
        completion_criteria=[_criterion("c0", "a commercial water service request is submitted")]
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
async def test_maybe_run_completion_verification_fallback_floor_uses_page_end_state_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def handler(*, prompt: str, prompt_name: str) -> dict:
        captured["prompt"] = prompt
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
            "current_url": "https://example.test/review",
            "page_title": "Start Service - Review",
            "blocks": [],
        },
    }

    verification = await _maybe_run_completion_verification(ctx, result, time.monotonic())

    assert verification is not None
    assert verification.no_gradeable_run_plane is False
    assert verification.is_fully_satisfied() is True
    assert "observed_end_state_url: https://example.test/review" in captured["prompt"]
    assert "observed_end_state_page_title: Start Service - Review" in captured["prompt"]


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
            _criterion("c0", "a commercial water service request is submitted"),
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


def test_completion_contract_not_violated_unavailable_blocks_surfacing() -> None:
    ctx = SimpleNamespace(
        completion_verification_result=CompletionVerificationResult("unavailable"),
        last_artifact_health_blocker_reason=None,
    )
    # An unavailable verdict means the outcome could not be verified: do not surface
    # the workflow as verified on run status alone.
    assert _completion_contract_not_violated(ctx) is False  # type: ignore[arg-type]
