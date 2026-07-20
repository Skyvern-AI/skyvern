from __future__ import annotations

import time
from dataclasses import replace

from skyvern.forge.sdk.copilot.build_test_outcome import recorded_outcome_from_run_blocks_result
from skyvern.forge.sdk.copilot.completion_criteria_store import (
    _criterion_reconcile_key,
    apply_requested_output_producer_floor,
    criteria_from_json,
    criteria_to_json,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    RunEvidenceSnapshot,
    carry_degraded_criterion_ids,
    combine_verification_results,
    grade_fallback_floor_reached_end_state_criteria,
    only_degraded_blocking,
    structural_unfired_contingent_criterion_ids,
)
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    _classifier_fallback_policy,
    _fallback_literal_candidates_for_field,
    build_classifier_fallback_floor,
    is_contingent_missing_antecedent_degraded,
    is_turn_unsatisfiable_fallback_degraded,
    resolve_mint_degrade,
)
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.tools.run_execution import _terminal_challenge_completion_verification

_DEGRADE = "turn_unsatisfiable_fallback"


def _fallback_policy(user_message: str):
    return _classifier_fallback_policy(
        [], raw_secret_present=False, failure_kind="provider_error", user_message=user_message
    )


def _requested_criterion(policy, suffix: str) -> CompletionCriterion | None:
    for criterion in policy.completion_criteria:
        if criterion.id.endswith(suffix):
            return criterion
    return None


def _validation_review_payload() -> dict[str, object]:
    return {
        "all_checks_passed": True,
        "validation_only": True,
        "review_page_visible": True,
        "submit_or_finalize_clicked": False,
        "submitted_request": False,
        "confirmation_page_visible": False,
        "review_values": {
            "visible_reference": "Reference AB123456",
            "visible_start_date": "2026-06-22",
            "visible_account_holder": "EXAMPLE LABS INC",
        },
        "evidence_text": (
            "Visible Review page showed Reference AB123456, start date 2026-06-22, "
            "and account holder EXAMPLE LABS INC. No final control was clicked."
        ),
    }


def test_quoted_literal_mints_typed_value_and_leaves_floor_degraded() -> None:
    policy = _fallback_policy('Go to the portal and return the order number "ABC12345" and the status')

    minted = _requested_criterion(policy, "output_order_number")
    assert minted is not None
    assert minted.expected_output_value == "ABC12345"
    assert minted.mint_degrade is None

    floor = _requested_criterion(policy, "__copilot_fallback_floor__run")
    assert floor is not None
    assert floor.mint_degrade == _DEGRADE


def test_unquoted_value_noun_degrades_instead_of_minting() -> None:
    policy = _fallback_policy("read the record; the status should be active for each location")

    minted = _requested_criterion(policy, "output_status")
    assert minted is not None
    assert minted.expected_output_value is None
    assert minted.mint_degrade == _DEGRADE


def test_extractor_mints_quoted_literal_after_binder() -> None:
    assert set(_fallback_literal_candidates_for_field('the status should be "active"', "status", "Status")) == {
        "active"
    }


def test_extractor_rejects_unquoted_participle_predicate() -> None:
    assert _fallback_literal_candidates_for_field("the status should be updated", "status", "Status") == []


def test_extractor_rejects_unquoted_verb_predicate() -> None:
    assert _fallback_literal_candidates_for_field("the status should be filled", "status", "Status") == []


def test_extractor_rejects_unquoted_code_after_binder() -> None:
    assert _fallback_literal_candidates_for_field("order_id must be ABC12345", "order_id", "Order Id") == []


def test_quoted_field_name_is_not_minted_as_value() -> None:
    policy = _fallback_policy('return the "status" field and the name for the record')

    minted = _requested_criterion(policy, "output_status")
    assert minted is None or minted.expected_output_value is None


def test_multiple_literal_candidates_mint_nothing() -> None:
    policy = _fallback_policy("the status should be active but the status should be pending")

    minted = _requested_criterion(policy, "output_status")
    assert minted is None or minted.expected_output_value is None


def test_short_literal_degrades_instead_of_minting() -> None:
    policy = _fallback_policy('return the status "ok" for the record')

    minted = _requested_criterion(policy, "output_status")
    assert minted is None or minted.expected_output_value is None


def test_value_less_fallback_marks_floor_and_requested_outputs_degraded() -> None:
    policy = _fallback_policy("return the status for the record")

    degraded = [c for c in policy.completion_criteria if is_turn_unsatisfiable_fallback_degraded(c)]
    assert any(c.id == "__copilot_fallback_floor__run" for c in degraded)
    assert any(c.id.endswith("output_status") for c in degraded)


def _degraded_floor() -> list[CompletionCriterion]:
    return [replace(criterion, mint_degrade=_DEGRADE) for criterion in build_classifier_fallback_floor([])]


def _carrier_verdict() -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id="__copilot_requested_output__output_reference",
        state="satisfied",
        reason_code="evidence_confirms",
        evidence_ref="block_outputs:lookup",
        evidence_source="independent_page_evidence",
    )


def test_degraded_floor_still_satisfies_via_carrier_arm() -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={})
    verdicts = grade_fallback_floor_reached_end_state_criteria(
        _degraded_floor(), snapshot, carrier_verdicts=(_carrier_verdict(),)
    )
    assert [verdict.state for verdict in verdicts] == ["satisfied"]


def test_degraded_floor_blocks_reached_end_state_arm() -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_request": _validation_review_payload()})
    assert grade_fallback_floor_reached_end_state_criteria(_degraded_floor(), snapshot) == []


def test_non_degraded_floor_accepts_reached_end_state_arm() -> None:
    snapshot = RunEvidenceSnapshot(block_outputs={"submit_request": _validation_review_payload()})
    verdicts = grade_fallback_floor_reached_end_state_criteria(build_classifier_fallback_floor([]), snapshot)
    assert [verdict.state for verdict in verdicts] == ["satisfied"]


def test_structural_unfired_skips_degraded_criterion() -> None:
    criterion = CompletionCriterion(
        id="c_degraded",
        outcome="captured blocker",
        level="run",
        contingent_antecedent_output_path="output.blocker",
        mint_degrade=_DEGRADE,
    )
    snapshot = RunEvidenceSnapshot(block_outputs={"blocker_output": None})
    assert structural_unfired_contingent_criterion_ids([criterion], snapshot) == []


def _degraded_abstention_result() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["floor", "req"],
        verdicts=[
            CriterionVerdict(criterion_id="floor", state="satisfied", reason_code="evidence_confirms"),
            CriterionVerdict(
                criterion_id="req",
                state="unsatisfied",
                reason_code="structurally_abstained",
                evidence_ref="block_outputs:lookup",
                output_path="output.status",
            ),
        ],
        degraded_criterion_ids=["req"],
    )


def test_degraded_abstention_is_not_credited_even_with_corroboration() -> None:
    assert _degraded_abstention_result().is_fully_satisfied() is False


def test_non_degraded_abstention_credit_path_is_not_broken() -> None:
    result = replace(_degraded_abstention_result(), degraded_criterion_ids=[])
    # A satisfied observed-end-state corroborator credits the structural abstention when NOT degraded.
    result = replace(
        result,
        verdicts=[
            CriterionVerdict(
                criterion_id="floor",
                state="satisfied",
                reason_code="evidence_confirms",
                evidence_ref="observed_end_state_url",
            ),
            result.verdicts[1],
        ],
    )
    assert result.is_fully_satisfied() is True


def test_sky11842_zero_evidence_non_fallback_still_fails_closed() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c1"],
        verdicts=[CriterionVerdict(criterion_id="c1", state="unsatisfied", reason_code="no_evidence")],
    )
    assert result.is_fully_satisfied() is False
    assert only_degraded_blocking(result) is False


def test_degraded_criterion_ids_survive_combine_and_carry() -> None:
    run_result = carry_degraded_criterion_ids(
        CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["req"],
            verdicts=[CriterionVerdict(criterion_id="req", state="unsatisfied", reason_code="no_evidence")],
        ),
        [CompletionCriterion(id="req", outcome="x", level="run", mint_degrade=_DEGRADE)],
    )
    assert run_result is not None
    combined = combine_verification_results(["req"], run_result, [])
    carried = carry_degraded_criterion_ids(
        combined, [CompletionCriterion(id="req", outcome="x", level="run", mint_degrade=_DEGRADE)]
    )
    assert carried is not None
    assert "req" in carried.degraded_criterion_ids


def test_combine_alone_propagates_degraded_criterion_ids() -> None:
    run_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["req"],
        verdicts=[CriterionVerdict(criterion_id="req", state="unsatisfied", reason_code="no_evidence")],
        degraded_criterion_ids=["req"],
    )
    combined = combine_verification_results(["req"], run_result, [])
    assert combined.degraded_criterion_ids == ["req"]
    assert only_degraded_blocking(combined) is True


def test_fallback_literal_candidates_bounded_runtime_on_adversarial_message() -> None:
    adversarial = "status" + (" " * 20 + "=") * 4000
    start = time.perf_counter()
    _fallback_literal_candidates_for_field(adversarial, "status", "Status")
    assert time.perf_counter() - start < 2.0


def test_degraded_criterion_ids_survive_terminal_challenge() -> None:
    criteria = [
        CompletionCriterion(id="__copilot_fallback_floor__run", outcome="x", level="run", mint_degrade=_DEGRADE)
    ]
    result = carry_degraded_criterion_ids(
        CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["__copilot_fallback_floor__run"],
            verdicts=[
                CriterionVerdict(
                    criterion_id="__copilot_fallback_floor__run", state="unsatisfied", reason_code="no_evidence"
                )
            ],
        ),
        criteria,
    )
    assert result is not None
    assert result.degraded_criterion_ids == ["__copilot_fallback_floor__run"]

    challenged = _terminal_challenge_completion_verification(result, "run did not reach a verifiable terminal state")
    assert challenged is not None
    assert challenged.degraded_criterion_ids == ["__copilot_fallback_floor__run"]
    assert only_degraded_blocking(challenged) is True


def test_only_degraded_blocking_true_for_degraded_only() -> None:
    assert only_degraded_blocking(_degraded_abstention_result()) is True


def test_only_degraded_blocking_false_on_empty_blocking_set() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["req"],
        verdicts=[CriterionVerdict(criterion_id="req", state="satisfied", reason_code="evidence_confirms")],
        degraded_criterion_ids=["req"],
    )
    assert only_degraded_blocking(result) is False


def test_only_degraded_blocking_false_when_mixed_with_legitimate_unsatisfied() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["req", "legit"],
        verdicts=[
            CriterionVerdict(criterion_id="req", state="unsatisfied", reason_code="no_evidence"),
            CriterionVerdict(criterion_id="legit", state="unsatisfied", reason_code="evidence_contradicts"),
        ],
        degraded_criterion_ids=["req"],
    )
    assert only_degraded_blocking(result) is False


def test_mint_degrade_survives_json_round_trip() -> None:
    criteria = (CompletionCriterion(id="req", outcome="return the status", level="run", mint_degrade=_DEGRADE),)
    restored = criteria_from_json(criteria_to_json(criteria))
    assert restored[0].mint_degrade == _DEGRADE


def test_mint_degrade_is_not_identity_bearing_in_reconcile_key() -> None:
    base = CompletionCriterion(id="req", outcome="return the status", level="run", output_path="output.status")
    degraded = replace(base, mint_degrade=_DEGRADE)
    assert _criterion_reconcile_key(base) == _criterion_reconcile_key(degraded)


def test_producer_floor_excludes_degraded_from_rekey() -> None:
    degraded = CompletionCriterion(
        id="__copilot_requested_output__output_status",
        outcome="return the status",
        level="run",
        output_path="output.status",
        mint_degrade=_DEGRADE,
    )
    floored, rekeyed = apply_requested_output_producer_floor([degraded])
    assert rekeyed == ()
    assert floored[0].output_path == "output.status"


def _degraded_verification() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["__copilot_fallback_floor__run"],
        verdicts=[
            CriterionVerdict(
                criterion_id="__copilot_fallback_floor__run", state="unsatisfied", reason_code="no_evidence"
            )
        ],
        degraded_criterion_ids=["__copilot_fallback_floor__run"],
    )


def test_recorded_branch_degraded_only_is_not_repairable() -> None:
    result = {
        "data": {"workflow_run_id": "wr_x", "blocks": [{"label": "lookup", "extracted_data": {"status": "done"}}]}
    }
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_demonstrated", reason_code="outcome_not_demonstrated"),
        completion_verification=_degraded_verification(),
    )
    assert outcome is not None
    assert outcome.verdict == "not_authoritative"
    assert outcome.reason_code == "fallback_floor_turn_unsatisfiable"


def test_raw_branch_degraded_only_is_not_repairable() -> None:
    result = {
        "ok": False,
        "data": {"workflow_run_id": "wr_x", "blocks": [{"label": "lookup", "extracted_data": {"status": "done"}}]},
    }
    outcome = recorded_outcome_from_run_blocks_result(result, completion_verification=_degraded_verification())
    assert outcome is not None
    assert outcome.verdict == "not_authoritative"
    assert outcome.reason_code == "fallback_floor_turn_unsatisfiable"


def test_raw_branch_runtime_failure_stays_repairable() -> None:
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_x",
            "failure_type": "TERMINATED",
            "blocks": [{"label": "lookup", "status": "failed", "extracted_data": {"status": "done"}}],
        },
    }
    outcome = recorded_outcome_from_run_blocks_result(result, completion_verification=_degraded_verification())
    assert outcome is not None
    assert outcome.verdict == "repairable_failure"


def test_recorded_branch_blocker_reported_stays_repairable() -> None:
    result = {
        "data": {"workflow_run_id": "wr_x", "blocks": [{"label": "lookup", "extracted_data": {"status": "done"}}]}
    }
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_demonstrated", reason_code="blocker_reported"),
        completion_verification=_degraded_verification(),
    )
    assert outcome is not None
    assert outcome.verdict == "repairable_failure"
    assert outcome.reason_code == "blocker_reported"


def test_recorded_branch_no_reason_code_with_failed_block_stays_repairable() -> None:
    result = {
        "data": {
            "workflow_run_id": "wr_x",
            "blocks": [{"label": "lookup", "status": "failed", "extracted_data": {"status": "done"}}],
        }
    }
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_demonstrated"),
        completion_verification=_degraded_verification(),
    )
    assert outcome is not None
    assert outcome.verdict == "repairable_failure"


def test_recorded_branch_no_reason_code_without_failed_block_degrades() -> None:
    result = {
        "data": {"workflow_run_id": "wr_x", "blocks": [{"label": "lookup", "extracted_data": {"status": "done"}}]}
    }
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_demonstrated"),
        completion_verification=_degraded_verification(),
    )
    assert outcome is not None
    assert outcome.verdict == "not_authoritative"
    assert outcome.reason_code == "fallback_floor_turn_unsatisfiable"


def test_recorded_branch_no_meaningful_output_degrades() -> None:
    result = {
        "data": {"workflow_run_id": "wr_x", "blocks": [{"label": "lookup", "extracted_data": {"status": "done"}}]}
    }
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_demonstrated", reason_code="no_meaningful_output"),
        completion_verification=_degraded_verification(),
    )
    assert outcome is not None
    assert outcome.verdict == "not_authoritative"
    assert outcome.reason_code == "fallback_floor_turn_unsatisfiable"


def test_contingent_missing_antecedent_routes_to_its_own_degrade_lane() -> None:
    criterion = CompletionCriterion(
        id="c1",
        outcome="A blocker is reported to the user.",
        level="run",
        contingent_on="the site blocks submission",
        mint_degrade="contingent_missing_antecedent",
    )
    assert is_contingent_missing_antecedent_degraded(criterion) is True
    assert is_turn_unsatisfiable_fallback_degraded(criterion) is False
    assert is_contingent_missing_antecedent_degraded(replace(criterion, mint_degrade=None)) is False


def test_contingent_missing_antecedent_carries_and_blocks_credit_when_unsatisfied() -> None:
    result = carry_degraded_criterion_ids(
        CompletionVerificationResult(
            status="evaluated",
            criterion_ids=["c1", "floor"],
            verdicts=[
                CriterionVerdict(criterion_id="c1", state="unsatisfied", reason_code="no_evidence"),
                CriterionVerdict(criterion_id="floor", state="satisfied", reason_code="evidence_confirms"),
            ],
        ),
        [
            CompletionCriterion(
                id="c1",
                outcome="A blocker is reported to the user.",
                level="run",
                contingent_on="the site blocks submission",
                mint_degrade="contingent_missing_antecedent",
            )
        ],
    )
    assert "c1" in result.contingent_degraded_criterion_ids
    assert "c1" not in result.degraded_criterion_ids
    assert result.is_fully_satisfied() is False
    assert only_degraded_blocking(result) is True


def test_satisfied_degraded_criterion_still_credits_on_merits() -> None:
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c1"],
        verdicts=[CriterionVerdict(criterion_id="c1", state="satisfied", reason_code="evidence_confirms")],
        degraded_criterion_ids=["c1"],
    )
    assert result.is_fully_satisfied() is True


def test_recorded_branch_ambiguous_code_with_failed_block_stays_repairable() -> None:
    result = {
        "data": {
            "workflow_run_id": "wr_x",
            "blocks": [{"label": "lookup", "status": "failed", "extracted_data": {"status": "done"}}],
        }
    }
    outcome = recorded_outcome_from_run_blocks_result(
        result,
        recorded_run_outcome=RecordedRunOutcome(verdict="not_demonstrated", reason_code="outcome_not_demonstrated"),
        completion_verification=_degraded_verification(),
    )
    assert outcome is not None
    assert outcome.verdict == "repairable_failure"
    assert outcome.reason_code != "fallback_floor_turn_unsatisfiable"


def test_turn_unsatisfiable_fallback_stamp_precedes_pathless_contingent_degrade() -> None:
    resolved = resolve_mint_degrade(
        "turn_unsatisfiable_fallback",
        "the site blocks submission",
        None,
    )
    assert resolved == "turn_unsatisfiable_fallback"
