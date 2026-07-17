"""SKY-10931: persisted completion-criteria lifecycle (epoch/supersede/tripwire),
definition-plane grading, tri-state verdict semantics, and the claim-side closure
of the judge-unavailable success bypass."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.agent import (
    RequestPolicyGuardrailInputs,
    _reconcile_completion_criteria_on_context,
    _stored_active_completion_criteria,
)
from skyvern.forge.sdk.copilot.completion_criteria_store import (
    CompletionCriteriaTurnState,
    ReconcileDecision,
    StoredCriteriaSet,
    StoredCriteriaSnapshot,
    build_turn_state,
    criteria_from_json,
    criteria_to_json,
    note_adjudication_on_turn_state,
    plan_persistence,
    reconcile_completion_criteria,
    split_requested_output_criteria,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    RunEvidenceSnapshot,
    _coerce_result,
    _render_criteria,
    combine_verification_results,
    evaluate_completion_criteria,
    grade_definition_criteria,
    grade_present_value_criteria,
    run_plane_all_no_evidence,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairNextAction,
    VerificationResult,
)
from skyvern.forge.sdk.copilot.enforcement import (
    built_unverified_repair_inert_context,
    verified_goal_claim_authorized,
    verified_goal_satisfied_context,
)
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    JudgmentTruthCondition,
    RequestPolicy,
    _parse_completion_criteria,
    normalized_criterion_outcome_key,
)
from skyvern.forge.sdk.copilot.tools.completion import (
    _apply_present_value_upgrades,
    _outcome_failure_warrants_repair,
    _split_criteria_by_plane,
)
from tests.unit.copilot_test_helpers import make_completion_criterion as _criterion


def _stored(
    *outcomes: str,
    set_id: str = "wccs_1",
    epoch: int = 1,
    counter: int = 0,
    fired: bool = False,
    known_good: str | None = None,
) -> StoredCriteriaSet:
    return StoredCriteriaSet(
        set_id=set_id,
        goal_epoch=epoch,
        criteria=tuple(_criterion(f"c{i}", outcome) for i, outcome in enumerate(outcomes)),
        consecutive_all_no_evidence=counter,
        tripwire_fired=fired,
        last_fully_satisfied_workflow_yaml=known_good,
    )


def _verdict(cid: str, state: str, reason: str) -> CriterionVerdict:
    return CriterionVerdict(criterion_id=cid, state=state, reason_code=reason)  # type: ignore[arg-type]


def _evaluated(*verdicts: CriterionVerdict) -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=[v.criterion_id for v in verdicts],
        verdicts=list(verdicts),
    )


def _all_no_evidence(*cids: str) -> CompletionVerificationResult:
    return _evaluated(*[_verdict(cid, "unsatisfied", "no_evidence") for cid in cids])


def test_reconcile_first_derivation_creates_epoch_one() -> None:
    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(), [_criterion("c0", "the item is in the cart")], actionable=True
    )
    assert decision.action == "create"
    assert decision.reason == "first"
    assert decision.epoch == 1


def test_reconcile_subset_keeps_stored_set() -> None:
    stored = _stored("the item is in the cart", "the order total is extracted")
    snapshot = StoredCriteriaSnapshot(active=stored, next_epoch=2)
    decision = reconcile_completion_criteria(snapshot, [_criterion("x0", "The item is in the cart")], actionable=True)
    assert decision.action == "adopt_stored"
    assert decision.reason == "kept"
    assert decision.epoch == 1
    assert decision.criteria == stored.criteria


def test_normalized_outcome_key_ignores_trailing_punctuation() -> None:
    assert normalized_criterion_outcome_key("The heading is extracted.") == "the heading is extracted"
    assert normalized_criterion_outcome_key("  The   heading is extracted!  ") == "the heading is extracted"


def test_reconcile_keeps_stored_set_on_trailing_punctuation_and_case_variance() -> None:
    stored = _stored("The page's main heading is extracted from https://example.com")
    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        [_criterion("c0", "the page's main heading is extracted from https://example.com.")],
        actionable=True,
    )
    assert decision.action == "adopt_stored"
    assert decision.reason == "kept"
    assert decision.criteria == stored.criteria


def test_reconcile_empty_fresh_never_supersedes() -> None:
    stored = _stored("the item is in the cart")
    decision = reconcile_completion_criteria(StoredCriteriaSnapshot(active=stored, next_epoch=2), [], actionable=True)
    assert decision.action == "adopt_stored"
    assert decision.reason == "empty_fresh"
    assert decision.criteria == stored.criteria


def test_reconcile_non_subset_supersedes_wholesale() -> None:
    stored = _stored("the item is in the cart", set_id="wccs_old", epoch=3)
    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=4),
        [_criterion("c0", "the invoice totals are extracted")],
        actionable=True,
    )
    assert decision.action == "create"
    assert decision.reason == "not_subset"
    assert decision.epoch == 4
    assert decision.superseded_set_id == "wccs_old"


def test_reconcile_keeps_stored_requested_output_when_fresh_is_generic_rephrase() -> None:
    stored = StoredCriteriaSet(
        set_id="wccs_existing",
        goal_epoch=1,
        criteria=(
            _criterion("c0", "The profile details are captured."),
            _criterion("c1", "The returned record includes ID.", output_path="output.id"),
        ),
    )

    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        [_criterion("c0", "The full profile information is extracted.")],
        actionable=True,
    )

    assert decision.action == "adopt_stored"
    assert decision.reason == "kept"
    assert decision.criteria == stored.criteria


def test_reconcile_keeps_grounded_stored_requested_output_when_fresh_rephrases_path() -> None:
    stored = StoredCriteriaSet(
        set_id="wccs_existing",
        goal_epoch=1,
        criteria=(_criterion("c0", "The returned record includes ID.", output_path="output.id"),),
    )

    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        [_criterion("c1", "The final response captures the ID.", output_path="output.id")],
        actionable=True,
    )

    assert decision.action == "adopt_stored"
    assert decision.reason == "kept"
    assert decision.criteria == stored.criteria


def test_reconcile_keeps_grounded_stored_requested_output_when_fresh_id_is_ungrounded() -> None:
    stored = StoredCriteriaSet(
        set_id="wccs_existing",
        goal_epoch=1,
        criteria=(_criterion("c0", "The returned record includes ID.", output_path="output.id"),),
    )

    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        [_criterion("c1", "The returned record includes ID.")],
        actionable=True,
    )

    assert decision.action == "adopt_stored"
    assert decision.reason == "kept"
    assert decision.criteria == stored.criteria


def test_reconcile_epoch_stays_monotonic_after_supersede_chain() -> None:
    # A superseded latest row still advances next_epoch, so a new set never
    # reuses an epoch number.
    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=None, next_epoch=7),
        [_criterion("c0", "the report is downloaded")],
        actionable=True,
    )
    assert decision.epoch == 7


def test_reconcile_clarification_turn_never_creates_or_supersedes() -> None:
    no_store = reconcile_completion_criteria(
        StoredCriteriaSnapshot(), [_criterion("c0", "the item is in the cart")], actionable=False
    )
    assert no_store.action == "none"

    stored = _stored("the item is in the cart")
    with_store = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2),
        [_criterion("c0", "something entirely different")],
        actionable=False,
    )
    assert with_store.action == "adopt_stored"
    assert with_store.criteria == stored.criteria


def test_reconcile_no_criteria_anywhere_is_noop() -> None:
    decision = reconcile_completion_criteria(StoredCriteriaSnapshot(), [], actionable=True)
    assert decision.action == "none"
    assert plan_persistence(build_turn_state(StoredCriteriaSnapshot(), decision)) is None


@pytest.mark.parametrize(
    "criteria",
    [
        pytest.param(
            (
                CompletionCriterion(
                    id="c0",
                    outcome="the returned record includes ID",
                    implicit=True,
                    output_path="output.id",
                ),
                CompletionCriterion(id="c1", outcome="inputs are reusable", method_mandated=True, level="definition"),
            ),
            id="level-and-flags",
        ),
        pytest.param(
            (
                CompletionCriterion(
                    id="c0",
                    outcome="a commercial water service request is started",
                    kind="terminal_action",
                    terminal_action_family="request",
                ),
            ),
            id="terminal-action-fields",
        ),
        pytest.param(
            (
                _criterion(
                    "c0",
                    "The requested download is returned.",
                    output_path="output.output_id",
                    deliverable_kind="registered_download",
                ),
            ),
            id="deliverable-kind",
        ),
        pytest.param(
            (
                _criterion(
                    "c0",
                    "A blocker is reported to the user.",
                    contingent_on="the site blocks submission",
                    mint_degrade="contingent_missing_antecedent",
                ),
            ),
            id="contingent-mint-degrade",
        ),
        pytest.param(
            (
                CompletionCriterion(
                    id="slot-id",
                    outcome="The requested status is returned.",
                    output_path="output.status",
                    request_slot_id="a" * 64,
                    pinability="pinned",
                    mint_disposition="pending",
                ),
            ),
            id="request-slot-contract-fields",
        ),
    ],
)
def test_criteria_json_round_trip_preserves_fields(criteria: tuple[CompletionCriterion, ...]) -> None:
    assert criteria_from_json(criteria_to_json(criteria)) == criteria


def test_terminal_action_verification_mode_round_trips() -> None:
    criterion = CompletionCriterion(
        id="c0",
        outcome="The service request is created.",
        kind="terminal_action",
        terminal_action_family="request",
        terminal_action_verification_mode="semantic_outcome_v1",
    )

    raw = criteria_to_json([criterion])

    assert raw[0]["terminal_action_verification_mode"] == "semantic_outcome_v1"
    assert criteria_from_json(raw) == (criterion,)
    legacy = {key: value for key, value in raw[0].items() if key != "terminal_action_verification_mode"}
    assert criteria_from_json([legacy])[0].terminal_action_verification_mode == "family_record_v1"
    stored = StoredCriteriaSnapshot(
        active=StoredCriteriaSet(
            set_id="wccs_1",
            goal_epoch=1,
            criteria=(replace(criterion, terminal_action_verification_mode="family_record_v1"),),
        ),
        next_epoch=2,
    )
    decision = reconcile_completion_criteria(stored, [criterion], actionable=True)
    assert decision.action == "create"
    assert decision.criteria[0].terminal_action_verification_mode == "semantic_outcome_v1"


def test_terminal_action_reconciliation_cannot_regress_semantic_authority() -> None:
    semantic = CompletionCriterion(
        id="c0",
        outcome="The service request is created.",
        kind="terminal_action",
        terminal_action_family="request",
        terminal_action_verification_mode="semantic_outcome_v1",
    )
    stored = StoredCriteriaSnapshot(
        active=StoredCriteriaSet(set_id="wccs_1", goal_epoch=1, criteria=(semantic,)),
        next_epoch=2,
    )

    decision = reconcile_completion_criteria(
        stored,
        [replace(semantic, terminal_action_verification_mode="family_record_v1")],
        actionable=True,
    )

    assert decision.action == "adopt_stored"
    assert decision.criteria == (semantic,)


def test_terminal_action_reconciliation_abstention_cannot_drop_stored_semantic_authority() -> None:
    semantic = CompletionCriterion(
        id="c0",
        outcome="The service request is created.",
        kind="terminal_action",
        terminal_action_family="request",
        terminal_action_verification_mode="semantic_outcome_v1",
    )
    stored = StoredCriteriaSnapshot(
        active=StoredCriteriaSet(set_id="wccs_1", goal_epoch=1, criteria=(semantic,)),
        next_epoch=2,
    )

    decision = reconcile_completion_criteria(
        stored,
        [
            replace(
                semantic,
                kind="outcome",
                terminal_action_family=None,
                terminal_action_verification_mode="family_record_v1",
            )
        ],
        actionable=True,
    )

    assert decision.action == "adopt_stored"
    assert decision.criteria == (semantic,)


def test_terminal_action_semantic_authority_survives_fresh_criterion_reindexing() -> None:
    semantic = CompletionCriterion(
        id="c0",
        outcome="The service request is created.",
        kind="terminal_action",
        terminal_action_family="request",
        terminal_action_verification_mode="semantic_outcome_v1",
    )
    stored = StoredCriteriaSnapshot(
        active=StoredCriteriaSet(set_id="wccs_1", goal_epoch=1, criteria=(semantic,)),
        next_epoch=2,
    )
    authenticated = CompletionCriterion(id="c0", outcome="The user is authenticated.")
    reindexed = replace(
        semantic,
        id="c1",
        kind="outcome",
        terminal_action_family=None,
        terminal_action_verification_mode="family_record_v1",
    )

    decision = reconcile_completion_criteria(stored, [authenticated, reindexed], actionable=True)

    assert decision.action == "create"
    assert [
        (criterion.id, criterion.kind, criterion.terminal_action_verification_mode) for criterion in decision.criteria
    ] == [
        ("c0", "outcome", "family_record_v1"),
        ("c1", "terminal_action", "semantic_outcome_v1"),
    ]


def test_expanded_criteria_preserve_stored_terminal_action_semantic_authority() -> None:
    semantic = CompletionCriterion(
        id="c0",
        outcome="The service request is created.",
        kind="terminal_action",
        terminal_action_family="request",
        terminal_action_verification_mode="semantic_outcome_v1",
    )
    added = CompletionCriterion(id="c1", outcome="The confirmation number is returned.")
    stored = StoredCriteriaSnapshot(
        active=StoredCriteriaSet(set_id="wccs_1", goal_epoch=1, criteria=(semantic,)),
        next_epoch=2,
    )

    decision = reconcile_completion_criteria(
        stored,
        [replace(semantic, terminal_action_verification_mode="family_record_v1"), added],
        actionable=True,
    )

    assert decision.action == "create"
    assert decision.criteria == (semantic, added)


def test_typed_criteria_list_round_trip_preserves_grading_metadata() -> None:
    criterion = CompletionCriterion(
        id="c0",
        outcome="The visible page path label is returned.",
        output_path="output.visible_page_path_label",
        expected_output_value="Public start-service path",
        pinability="unpinnable",
        mint_degrade="undecidable_judgment",
        mint_disposition="degraded",
    )

    raw = criteria_to_json([criterion])

    assert isinstance(raw, list)
    assert raw[0]["pinability"] == "unpinnable"
    assert criteria_from_json(raw) == (criterion,)


def test_typed_boolean_validation_classification_round_trip_preserves_shape_and_evidence() -> None:
    """Typed boolean classifications persist goal_judgment_boolean / independent_run_evidence."""
    criterion = CompletionCriterion(
        id="c0",
        outcome="The run classifies whether a public form exists.",
        kind="validation_classification",
        classification_output_key="public_form_exists",
        expected_output_shape="goal_judgment_boolean",
        requested_output_evidence_source="independent_run_evidence",
        judgment_truth_condition=JudgmentTruthCondition(
            predicate="login_gate_blocks_target", polarity_when_holds=False
        ),
        pinability="pinned",
        mint_disposition="pending",
    )

    raw = criteria_to_json([criterion])

    assert raw[0]["expected_output_shape"] == "goal_judgment_boolean"
    assert raw[0]["requested_output_evidence_source"] == "independent_run_evidence"
    reloaded = criteria_from_json(raw)
    assert reloaded == (criterion,)
    assert reloaded[0].expected_output_shape == "goal_judgment_boolean"
    assert reloaded[0].requested_output_evidence_source == "independent_run_evidence"


def test_untyped_boolean_validation_classification_reload_normalizes_shape_and_evidence() -> None:
    """Rows without typed mint metadata carry no reliable shape/evidence markers."""
    legacy = {
        "id": "c0",
        "outcome": "The run classifies whether a public form exists.",
        "kind": "validation_classification",
        "classification_output_key": "public_form_exists",
        "expected_output_shape": "goal_judgment_boolean",
        "requested_output_evidence_source": "independent_run_evidence",
    }

    reloaded = criteria_from_json([legacy])

    assert reloaded[0].expected_output_shape is None
    assert reloaded[0].requested_output_evidence_source == "runtime_output"


def test_typed_metadata_is_self_describing_without_a_storage_version() -> None:
    legacy = {"id": "c0", "outcome": "done", "output_path": "output.done"}
    decorated = {
        **legacy,
        "pinability": "unpinnable",
        "mint_disposition": "degraded",
    }

    assert criteria_from_json([legacy]) == (CompletionCriterion(id="c0", outcome="done", output_path="output.done"),)
    assert criteria_from_json([decorated]) == (
        CompletionCriterion(
            id="c0",
            outcome="done",
            output_path="output.done",
            pinability="unpinnable",
            mint_disposition="degraded",
        ),
    )


def test_reconciliation_identity_ignores_pinability_and_derived_mint_state() -> None:
    stored = StoredCriteriaSet(
        set_id="wccs_1",
        goal_epoch=1,
        criteria=(
            CompletionCriterion(
                id="s0",
                outcome="done",
                output_path="output.done",
                expected_output_value=True,
                pinability="pinned",
            ),
        ),
    )
    fresh = CompletionCriterion(
        id="f0",
        outcome="done",
        output_path="output.done",
        expected_output_value=True,
        pinability="unpinnable",
        mint_degrade="undecidable_judgment",
        mint_disposition="degraded",
    )

    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2), [fresh], actionable=True
    )

    assert decision.action == "adopt_stored"


def test_criteria_from_json_degrades_stored_pathless_contingent() -> None:
    (criterion,) = criteria_from_json(
        [
            {
                "id": "c0",
                "outcome": "A blocker is reported to the user.",
                "contingent_on": "the site blocks submission",
            }
        ]
    )
    assert criterion.mint_degrade == "contingent_missing_antecedent"


def test_criteria_from_json_keeps_wellformed_contingent_undegraded() -> None:
    (criterion,) = criteria_from_json(
        [
            {
                "id": "c0",
                "outcome": "A blocker is reported to the user.",
                "contingent_on": "the site blocks submission",
                "contingent_antecedent_output_path": "output.blocker",
            }
        ]
    )
    assert criterion.mint_degrade is None


def test_criteria_from_json_coerces_unknown_mint_degrade_to_none() -> None:
    (criterion,) = criteria_from_json([{"id": "c0", "outcome": "done", "mint_degrade": "bogus"}])
    assert criterion.mint_degrade is None


def test_adopt_stored_rehydrates_pathless_contingent_as_degraded() -> None:
    stored_criteria = criteria_from_json(
        [
            {"id": "c0", "outcome": "The request is submitted."},
            {
                "id": "c1",
                "outcome": "A blocker is reported to the user.",
                "contingent_on": "the site blocks submission",
            },
        ]
    )
    stored = StoredCriteriaSet(set_id="set_1", goal_epoch=1, criteria=stored_criteria)

    decision = reconcile_completion_criteria(StoredCriteriaSnapshot(active=stored, next_epoch=2), [], actionable=True)

    assert decision.action == "adopt_stored"
    degrades = {criterion.id: criterion.mint_degrade for criterion in decision.criteria}
    assert degrades == {"c0": None, "c1": "contingent_missing_antecedent"}


def test_criteria_from_json_normalizes_unknown_deliverable_kind() -> None:
    (criterion,) = criteria_from_json(
        [
            {
                "id": "c0",
                "outcome": "The requested download is returned.",
                "deliverable_kind": "download",
            }
        ]
    )

    assert criterion.deliverable_kind is None


def test_criteria_from_json_coerces_invalid_level_to_run() -> None:
    (criterion,) = criteria_from_json([{"id": "c0", "outcome": "done", "level": "bogus"}])
    assert criterion.level == "run"


def _adopted_turn_state(stored: StoredCriteriaSet) -> CompletionCriteriaTurnState:
    snapshot = StoredCriteriaSnapshot(active=stored, next_epoch=stored.goal_epoch + 1)
    decision = reconcile_completion_criteria(snapshot, [], actionable=True)
    return build_turn_state(snapshot, decision)


def test_tripwire_fires_after_two_consecutive_all_no_evidence_adjudications() -> None:
    stored = _stored("the item is in the cart", counter=1)
    turn_state = _adopted_turn_state(stored)
    note_adjudication_on_turn_state(turn_state, _all_no_evidence("c0"))
    plan = plan_persistence(turn_state)
    assert plan is not None
    assert plan.counter_value == 2
    assert plan.tripwire_fired is True
    assert plan.supersede_set_id == stored.set_id
    assert plan.supersede_reason == "tripwire"


def test_tripwire_fires_at_most_once_per_epoch() -> None:
    stored = _stored("the item is in the cart", counter=2, fired=True)
    turn_state = _adopted_turn_state(stored)
    note_adjudication_on_turn_state(turn_state, _all_no_evidence("c0"))
    plan = plan_persistence(turn_state)
    assert plan is not None
    assert plan.counter_value == 3
    assert plan.supersede_set_id is None
    assert plan.supersede_reason is None


def test_tripwire_counter_resets_on_evidence() -> None:
    stored = _stored("the item is in the cart", counter=1)
    turn_state = _adopted_turn_state(stored)
    note_adjudication_on_turn_state(turn_state, _evaluated(_verdict("c0", "satisfied", "evidence_confirms")))
    plan = plan_persistence(turn_state)
    assert plan is not None
    assert plan.counter_value == 0
    assert plan.supersede_set_id is None


def test_tripwire_never_fires_on_a_set_created_this_turn() -> None:
    snapshot = StoredCriteriaSnapshot()
    decision = reconcile_completion_criteria(snapshot, [_criterion("c0", "the item is in the cart")], actionable=True)
    turn_state = build_turn_state(snapshot, decision)
    note_adjudication_on_turn_state(turn_state, _all_no_evidence("c0"))
    note_adjudication_on_turn_state(turn_state, _all_no_evidence("c0"))
    plan = plan_persistence(turn_state)
    assert plan is not None
    assert plan.creates_set is True
    assert plan.counter_value == 2
    assert plan.tripwire_fired is False
    assert plan.supersede_reason is None


def test_fully_satisfied_adjudication_records_known_good_yaml() -> None:
    stored = _stored("the item is in the cart")
    turn_state = _adopted_turn_state(stored)
    note_adjudication_on_turn_state(
        turn_state,
        _evaluated(_verdict("c0", "satisfied", "evidence_confirms")),
        fully_satisfied_workflow_yaml="title: test workflow",
    )
    plan = plan_persistence(turn_state)
    assert plan is not None
    assert plan.fully_satisfied_workflow_yaml == "title: test workflow"


def test_build_turn_state_exposes_known_good_availability() -> None:
    stored = _stored("the item is in the cart", known_good="title: known good")
    assert _adopted_turn_state(stored).known_good_yaml_available is True
    assert _adopted_turn_state(_stored("the item is in the cart")).known_good_yaml_available is False


def test_run_plane_all_no_evidence_ignores_definition_verdicts() -> None:
    mixed = _evaluated(
        _verdict("c0", "unsatisfied", "no_evidence"),
        _verdict("c1", "satisfied", "definition_parameters_referenced"),
    )
    assert run_plane_all_no_evidence(mixed) is True
    only_definition = _evaluated(_verdict("c0", "unknown", "definition_unknown"))
    assert run_plane_all_no_evidence(only_definition) is False
    confirmed = _evaluated(_verdict("c0", "satisfied", "evidence_confirms"))
    assert run_plane_all_no_evidence(confirmed) is False


def test_coerce_result_tristate_mapping() -> None:
    raw = {
        "verdicts": [
            {"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"},
            {"criterion_id": "c1", "satisfied": False, "reason_code": "no_evidence"},
            {"criterion_id": "c2", "satisfied": False, "reason_code": "evidence_contradicts"},
            {"criterion_id": "c3", "satisfied": False, "reason_code": "unknown"},
            {"criterion_id": "c4", "satisfied": True, "reason_code": "no_evidence"},
        ]
    }
    result = _coerce_result(raw, ["c0", "c1", "c2", "c3", "c4", "c5"])
    states = {v.criterion_id: v.state for v in result.verdicts}
    assert states == {
        "c0": "satisfied",
        "c1": "unsatisfied",
        "c2": "unsatisfied",
        "c3": "unknown",
        "c4": "unsatisfied",
        "c5": "unknown",
    }
    assert result.is_fully_satisfied() is False
    assert result.verdict_state_counts() == {"satisfied": 1, "unsatisfied": 3, "unknown": 2}


def test_explicit_output_path_criterion_prevents_full_verification_when_absent() -> None:
    id_criterion = _criterion("c1", "The returned record includes ID.", output_path="output.id")
    result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0", id_criterion.id],
        verdicts=[
            _verdict("c0", "satisfied", "evidence_confirms"),
            _verdict(id_criterion.id, "unsatisfied", "no_evidence"),
        ],
    )

    assert id_criterion.output_path == "output.id"
    assert id_criterion.outcome == "The returned record includes ID."
    assert result.is_fully_satisfied() is False


def test_completion_verifier_render_includes_required_output_path() -> None:
    rendered = _render_criteria([_criterion("c0", "The returned record includes ID.", output_path="output.id")])

    assert "required_output_path=output.id" in rendered


def test_completion_verifier_render_includes_deliverable_kind() -> None:
    rendered = _render_criteria(
        [
            _criterion(
                "c0",
                "The requested download is returned.",
                output_path="output.output_id",
                deliverable_kind="registered_download",
            )
        ]
    )

    assert "deliverable_kind=registered_download" in rendered


def test_required_output_path_missing_is_not_satisfied_by_evidence_text() -> None:
    id_criterion = _criterion("c1", "The returned record includes ID.", output_path="output.id")
    criteria = [
        _criterion(
            "c0",
            "The returned record includes status.",
            output_path="output.status",
        ),
        id_criterion,
    ]
    snapshot = RunEvidenceSnapshot(
        workflow_run_id="wr_test",
        block_outputs={
            "extract_profile": {
                "output": {
                    "id": None,
                    "evidence_text": "Search result text mentions ID 1457803926.",
                    "status": "active",
                }
            }
        },
    )

    async def _handler(**kwargs: object) -> dict:
        prompt = str(kwargs["prompt"])
        assert "required_output_path=output.id" in prompt
        assert '"id": null' in prompt
        assert "evidence_text" in prompt
        assert "1457803926" in prompt
        return {
            "verdicts": [
                {"criterion_id": "c0", "satisfied": True, "reason_code": "evidence_confirms"},
                {
                    "criterion_id": "c1",
                    "satisfied": False,
                    "reason_code": "no_evidence",
                    "missing_evidence": "output.id is missing/null.",
                },
            ]
        }

    result = asyncio.run(evaluate_completion_criteria(criteria, snapshot, _handler))

    assert id_criterion.output_path == "output.id"
    assert grade_present_value_criteria([id_criterion], snapshot) == []
    assert result.verdicts[1].criterion_id == "c1"
    assert result.verdicts[1].state == "unsatisfied"
    assert result.is_fully_satisfied() is False


_PARAMETERIZED_YAML = """\
title: account lookup
workflow_definition:
  parameters:
    - parameter_type: workflow
      key: first_name
    - parameter_type: workflow
      key: account_id
  blocks:
    - block_type: goto_url
      label: open_portal
      url: https://example.com/search
    - block_type: navigation
      label: fill_search
      navigation_goal: "Search for {{ first_name }} with account ID {{ account_id }}"
"""

_UNREFERENCED_YAML = """\
title: account lookup
workflow_definition:
  parameters:
    - parameter_type: workflow
      key: first_name
  blocks:
    - block_type: goto_url
      label: open_portal
      url: https://example.com/search
"""

_NO_PARAMS_YAML = """\
title: account lookup
workflow_definition:
  parameters: []
  blocks:
    - block_type: goto_url
      label: open_portal
      url: https://example.com/search
"""


def test_definition_grading_satisfied_when_parameters_exist_and_are_referenced() -> None:
    criteria = [
        _criterion("c0", "the workflow accepts first name and account ID as reusable inputs", level="definition")
    ]
    (verdict,) = grade_definition_criteria(criteria, _PARAMETERIZED_YAML)
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "definition_parameters_referenced"


def test_definition_grading_unsatisfied_when_parameters_missing_or_unreferenced() -> None:
    criteria = [
        _criterion("c0", "the workflow accepts first name and account ID as reusable inputs", level="definition")
    ]
    (missing,) = grade_definition_criteria(criteria, _NO_PARAMS_YAML)
    assert missing.state == "unsatisfied"
    assert missing.reason_code == "definition_parameters_missing"
    (unreferenced,) = grade_definition_criteria(criteria, _UNREFERENCED_YAML)
    assert unreferenced.state == "unsatisfied"
    assert unreferenced.reason_code == "definition_parameters_unreferenced"


def test_definition_grading_abstains_when_no_specific_inputs_named() -> None:
    criteria = [_criterion("c0", "the workflow accepts reusable inputs", level="definition")]
    (no_params,) = grade_definition_criteria(criteria, _NO_PARAMS_YAML)
    assert no_params.state == "unknown"
    assert no_params.reason_code == "definition_parameters_absent"
    (unreferenced,) = grade_definition_criteria(criteria, _UNREFERENCED_YAML)
    assert unreferenced.state == "unknown"
    assert unreferenced.reason_code == "definition_parameters_absent"


def test_definition_grading_requires_every_named_input_to_match() -> None:
    criteria = [
        _criterion(
            "c0",
            "the workflow accepts first name, last name, and account ID as reusable inputs",
            level="definition",
        )
    ]
    # _PARAMETERIZED_YAML defines first_name + account_id but not last_name:
    # a multi-input ask must not be satisfied by a partial parameter set.
    (verdict,) = grade_definition_criteria(criteria, _PARAMETERIZED_YAML)
    assert verdict.state == "unknown"
    assert verdict.reason_code == "definition_parameters_unmatched"

    full_yaml = _PARAMETERIZED_YAML.replace(
        "    - parameter_type: workflow\n      key: account_id",
        "    - parameter_type: workflow\n      key: last_name\n    - parameter_type: workflow\n      key: account_id",
    )
    (full,) = grade_definition_criteria(criteria, full_yaml)
    assert full.state == "satisfied"


def test_parse_completion_criteria_tolerates_unhashable_level() -> None:
    parsed = _parse_completion_criteria(
        [
            {"outcome": "the item is in the cart", "level": ["definition"]},
            {"outcome": "the order is placed", "level": {"value": "run"}},
        ]
    )
    assert [c.level for c in parsed] == ["run", "run"]


def test_parse_completion_criteria_tolerates_unhashable_terminal_action_fields() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "a commercial water service request is started",
                "kind": ["terminal_action"],
                "terminal_action_family": ["request"],
            },
            {
                "outcome": "a permit application is submitted",
                "kind": "terminal_action",
                "terminal_action_family": ["application"],
            },
        ]
    )

    assert [c.kind for c in parsed] == ["outcome", "terminal_action"]
    assert [c.terminal_action_family for c in parsed] == [None, None]


def test_parse_completion_criteria_dedupes_by_deliverable_kind() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "The returned record includes output id.",
                "output_path": "output.output_id",
            },
            {
                "outcome": "The returned record includes output id.",
                "output_path": "output.output_id",
                "deliverable_kind": "registered_download",
            },
            {
                "outcome": "The returned record includes output id.",
                "output_path": "output.output_id",
                "deliverable_kind": "registered_download",
            },
        ]
    )

    assert [(criterion.id, criterion.deliverable_kind) for criterion in parsed] == [
        ("c0", None),
        ("c1", "registered_download"),
    ]


def test_parse_completion_criteria_preserves_typed_terminal_action_fields() -> None:
    (criterion,) = _parse_completion_criteria(
        [
            {
                "outcome": "a commercial water service request is started",
                "kind": "terminal_action",
                "terminal_action_family": "request",
            }
        ]
    )

    assert criterion.kind == "terminal_action"
    assert criterion.terminal_action_family == "request"


def test_parse_completion_criteria_defaults_invalid_terminal_action_fields() -> None:
    parsed = _parse_completion_criteria(
        [
            {
                "outcome": "a commercial water service request is started",
                "kind": "terminal_action",
                "terminal_action_family": "invoice",
            },
            {
                "outcome": "the item is in the cart",
                "kind": "done",
                "terminal_action_family": "request",
            },
        ]
    )

    assert parsed[0].kind == "terminal_action"
    assert parsed[0].terminal_action_family is None
    assert parsed[1].kind == "outcome"
    assert parsed[1].terminal_action_family is None


def test_adopted_active_criteria_retain_deliverable_kind() -> None:
    criterion = _criterion(
        "c0",
        "The requested download is returned.",
        output_path="output.output_id",
        deliverable_kind="registered_download",
    )
    stored = StoredCriteriaSet(set_id="wccs_1", goal_epoch=1, criteria=(criterion,))
    state = _adopted_turn_state(stored)

    assert state.decision is not None
    assert state.decision.criteria[0].deliverable_kind == "registered_download"


def test_marked_deliverable_criterion_supersedes_unmarked_stored_criterion() -> None:
    stored = StoredCriteriaSet(
        set_id="wccs_1",
        goal_epoch=1,
        criteria=(
            _criterion(
                "s0",
                "The requested download is returned.",
                output_path="output.output_id",
            ),
        ),
    )
    fresh = [
        _criterion(
            "c0",
            "The requested download is returned.",
            output_path="output.output_id",
            deliverable_kind="registered_download",
        )
    ]

    decision = reconcile_completion_criteria(
        StoredCriteriaSnapshot(active=stored, next_epoch=2), fresh, actionable=True
    )

    assert decision.action == "create"
    assert decision.criteria[0].deliverable_kind == "registered_download"


def test_definition_grading_unknown_when_no_deterministic_check_applies() -> None:
    criteria = [_criterion("c0", "the summary email is well written", level="definition")]
    (verdict,) = grade_definition_criteria(criteria, _PARAMETERIZED_YAML)
    assert verdict.state == "unknown"
    assert verdict.reason_code == "definition_unknown"


def test_definition_grading_never_emits_no_evidence() -> None:
    criteria = [
        _criterion("c0", "inputs are reusable", level="definition"),
        _criterion("c1", "something unmappable", level="definition"),
    ]
    for yaml_text in (_PARAMETERIZED_YAML, _UNREFERENCED_YAML, _NO_PARAMS_YAML, "not: [valid", ""):
        for verdict in grade_definition_criteria(criteria, yaml_text):
            assert verdict.reason_code != "no_evidence"


def test_combine_results_keeps_unavailable_run_result_authoritative() -> None:
    unavailable = CompletionVerificationResult(status="unavailable")
    combined = combine_verification_results(
        ["c0", "c1"], unavailable, [_verdict("c1", "satisfied", "definition_parameters_referenced")]
    )
    assert combined.status == "unavailable"
    assert combined.verdicts == []


def test_combine_results_merges_planes_and_fills_unknown() -> None:
    run_result = _evaluated(_verdict("c0", "satisfied", "evidence_confirms"))
    combined = combine_verification_results(
        ["c0", "c1", "c2"], run_result, [_verdict("c1", "unknown", "definition_unknown")]
    )
    assert combined.status == "evaluated"
    assert [v.state for v in combined.verdicts] == ["satisfied", "unknown", "unknown"]
    assert combined.criterion_ids == ["c0", "c1", "c2"]


def test_split_criteria_by_plane() -> None:
    criteria = [
        _criterion("c0", "the item is in the cart"),
        _criterion("c1", "inputs are reusable", level="definition"),
    ]
    run_criteria, definition_criteria = _split_criteria_by_plane(criteria)
    assert [c.id for c in run_criteria] == ["c0"]
    assert [c.id for c in definition_criteria] == ["c1"]


def _snapshot(block_outputs: dict) -> RunEvidenceSnapshot:
    return RunEvidenceSnapshot(workflow_run_id="wr_test", block_outputs=block_outputs)


def test_present_value_credits_quoted_currency_literal_verbatim() -> None:
    criteria = [_criterion("c0", "the May 2026 statement total reads $4,210.55")]
    snapshot = _snapshot({"read_statement": {"month": "May 2026", "total": "$4,210.55"}})
    (verdict,) = grade_present_value_criteria(criteria, snapshot)
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "present_value_verbatim"
    assert verdict.evidence_ref == "block_outputs:read_statement"


@pytest.mark.parametrize(
    ("outcome", "block_outputs"),
    [
        pytest.param(
            "the May 2026 statement total reads $4,210.55",
            {"read_statement": {"total": "$1,000.00"}},
            id="literal-absent",
        ),
        pytest.param(
            "the May 2026 statement total reads $4,210.55",
            {},
            id="empty-block-outputs",
        ),
        pytest.param(
            "the statement total reads $4,210.55",
            {"download": {"blocker": "verify you are human to continue"}},
            id="blocked-run",
        ),
        pytest.param(
            "the invoice was downloaded successfully",
            {"download": {"file": "invoice downloaded successfully"}},
            id="no-high-specificity-literal",
        ),
        # A 4-digit year is too low-specificity: a coincidental output mention of the
        # year must not credit the criterion.
        pytest.param(
            "the 2026 billing summary is shown",
            {"footer": {"copyright": "© 2026 Example Corp"}},
            id="bare-year-token",
        ),
    ],
)
def test_present_value_abstains(outcome: str, block_outputs: dict) -> None:
    criteria = [_criterion("c0", outcome)]
    assert grade_present_value_criteria(criteria, _snapshot(block_outputs)) == []


def test_present_value_credits_multi_digit_account_identifier() -> None:
    criteria = [_criterion("c0", "Billing is open for account 100245")]
    snapshot = _snapshot({"open_billing": {"account_number": "100245", "tab": "Billing"}})
    (verdict,) = grade_present_value_criteria(criteria, snapshot)
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "present_value_verbatim"


def test_present_value_requires_every_named_literal_present() -> None:
    # A criterion naming both a date and a total must not be credited when only the
    # date is present: a partial match is not the named outcome.
    criteria = [_criterion("c0", "the 2026-05-05 statement totaling $4,210.55 is downloaded")]
    partial = _snapshot({"observe": {"statement_date": "2026-05-05"}})
    assert grade_present_value_criteria(criteria, partial) == []

    full = _snapshot({"download": {"date": "2026-05-05", "total": "$4,210.55"}})
    (verdict,) = grade_present_value_criteria(criteria, full)
    assert verdict.state == "satisfied"

    # Both literals must land in the SAME block output, not split across two.
    split = _snapshot({"observe": {"date": "2026-05-05"}, "summary": {"total": "$4,210.55"}})
    assert grade_present_value_criteria(criteria, split) == []


def test_present_value_rejects_substring_of_longer_token() -> None:
    # Plain containment would over-credit a short/numeric literal against a sibling
    # value; a bounded match must reject these.
    embedded_quote = [_criterion("c0", 'the vendor is "acme"')]
    assert grade_present_value_criteria(embedded_quote, _snapshot({"x": {"vendor": "acmecorp"}})) == []

    currency = [_criterion("c0", "the total is $10")]
    assert grade_present_value_criteria(currency, _snapshot({"x": {"total": "$100"}})) == []
    assert grade_present_value_criteria(currency, _snapshot({"x": {"total": "$10.50"}})) == []

    identifier = [_criterion("c0", "account 100245")]
    assert grade_present_value_criteria(identifier, _snapshot({"x": {"account": "1002450"}})) == []
    assert grade_present_value_criteria(identifier, _snapshot({"x": {"account": "9100245"}})) == []
    assert grade_present_value_criteria(identifier, _snapshot({"x": {"ratio": "0.100245"}})) == []


def test_present_value_credits_exact_bounded_literal() -> None:
    currency = [_criterion("c0", "the total is $10")]
    (verdict,) = grade_present_value_criteria(currency, _snapshot({"x": {"total": "$10"}}))
    assert verdict.reason_code == "present_value_verbatim"

    identifier = [_criterion("c0", "account 100245")]
    (verdict2,) = grade_present_value_criteria(identifier, _snapshot({"x": {"account": "100245"}}))
    assert verdict2.reason_code == "present_value_verbatim"


def test_present_value_quoted_literal_specificity_floor() -> None:
    # A 2-3 char quoted literal is too low-specificity to credit even when present
    # verbatim (it collides with incidental prose); >=4 chars credits.
    two_char = [_criterion("c0", 'the state is "ca"')]
    assert grade_present_value_criteria(two_char, _snapshot({"x": {"state": "ca"}})) == []

    four_char = [_criterion("c0", 'the vendor is "acme"')]
    (verdict,) = grade_present_value_criteria(four_char, _snapshot({"x": {"vendor": "acme"}}))
    assert verdict.reason_code == "present_value_verbatim"


def test_present_value_upgrade_is_upgrade_only() -> None:
    criteria = [_criterion("c0", 'the confirmation number is "ABC12345"')]
    snapshot = _snapshot({"confirm": {"number": "ABC12345"}})

    run_result = _evaluated(_verdict("c0", "unsatisfied", "no_evidence"))
    upgraded = _apply_present_value_upgrades(run_result, criteria, snapshot)
    assert upgraded.verdicts[0].state == "satisfied"
    assert upgraded.verdicts[0].reason_code == "present_value_verbatim"


def test_present_value_credits_unquoted_structured_identifier() -> None:
    criteria = [_criterion("c0", "the submitted request returns confirmation number WTR-1842-DEMO")]
    snapshot = _snapshot({"submit_request": {"confirmation_number": "WTR-1842-DEMO", "status": "submitted"}})
    (verdict,) = grade_present_value_criteria(criteria, snapshot)
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "present_value_verbatim"
    assert verdict.evidence_ref == "block_outputs:submit_request"


def test_present_value_structured_identifier_abstains_when_absent() -> None:
    criteria = [_criterion("c0", "the submitted request returns confirmation number WTR-1842-DEMO")]
    snapshot = _snapshot({"submit_request": {"confirmation_number": "WTR-9999-PROD"}})
    assert grade_present_value_criteria(criteria, snapshot) == []


def test_present_value_structured_identifier_requires_letter_and_digit() -> None:
    # A bare word or a bare year-length token is not a high-specificity identifier.
    plain_word = [_criterion("c0", "the page shows submitted")]
    assert grade_present_value_criteria(plain_word, _snapshot({"x": {"state": "submitted"}})) == []

    hyphen_word = [_criterion("c0", "the request is read-only")]
    assert grade_present_value_criteria(hyphen_word, _snapshot({"x": {"mode": "read-only"}})) == []


def test_present_value_structured_identifier_rejects_substring_of_longer_token() -> None:
    criteria = [_criterion("c0", "confirmation number WTR-1842-DEMO")]
    assert grade_present_value_criteria(criteria, _snapshot({"x": {"number": "WTR-1842-DEMO2"}})) == []


def test_present_value_structured_identifier_upgrade_only() -> None:
    criteria = [_criterion("c0", "confirmation number WTR-1842-DEMO")]
    snapshot = _snapshot({"confirm": {"number": "WTR-1842-DEMO"}})

    judge_unknown = _evaluated(_verdict("c0", "unknown", "unknown"))
    upgraded = _apply_present_value_upgrades(judge_unknown, criteria, snapshot)
    assert upgraded.verdicts[0].state == "satisfied"
    assert upgraded.verdicts[0].reason_code == "present_value_verbatim"

    contradicts = _evaluated(_verdict("c0", "unsatisfied", "evidence_contradicts"))
    kept = _apply_present_value_upgrades(contradicts, criteria, snapshot)
    assert kept.verdicts[0].reason_code == "evidence_contradicts"


def test_parse_completion_criteria_coerces_level() -> None:
    parsed = _parse_completion_criteria(
        [
            {"outcome": "the item is in the cart", "level": "run"},
            {"outcome": "inputs are reusable", "level": "definition"},
            {"outcome": "the order is placed", "level": "nonsense"},
            {"outcome": "the page is reached"},
        ]
    )
    assert [c.level for c in parsed] == ["run", "definition", "run", "run"]


def _ctx(**overrides: object) -> CopilotContext:
    defaults: dict = dict(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
    )
    defaults.update(overrides)
    return CopilotContext(**defaults)


def _legacy_verified_ctx() -> CopilotContext:
    return _ctx(
        last_test_ok=True,
        last_full_workflow_test_ok=True,
        latest_diagnosis_repair_contract=DiagnosisRepairContract(
            diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
            diagnosis_result=DiagnosisResult(),
            repair_decision=RepairDecision(next_action=RepairNextAction.NO_CHANGE),
            verification_result=VerificationResult(
                user_goal_satisfied=True,
                completion_contract_satisfied=True,
            ),
        ),
    )


def _no_repair_unverified_contract() -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=RepairNextAction.NO_CHANGE),
        verification_result=VerificationResult(
            user_goal_satisfied=False,
            completion_contract_satisfied=False,
        ),
    )


def test_claim_closure_turn_still_ends_but_claim_downgrades() -> None:
    ctx = _legacy_verified_ctx()
    # Turn completion is unchanged: the legacy conjunction still satisfies the gate.
    assert verified_goal_satisfied_context(ctx) is True
    # The claim tier is closed: no adjudicated evidence, no tested-success claim.
    assert verified_goal_claim_authorized(ctx) is False


def test_structural_abstention_no_repair_terminalizes_without_authorizing_success_claim() -> None:
    ctx = _ctx(
        last_test_ok=True,
        last_full_workflow_test_ok=True,
        latest_diagnosis_repair_contract=_no_repair_unverified_contract(),
        completion_verification_result=_evaluated(_verdict("c0", "unsatisfied", "structurally_abstained")),
    )

    assert ctx.completion_verification_result is not None
    assert ctx.completion_verification_result.is_fully_satisfied() is False
    assert verified_goal_satisfied_context(ctx) is False
    assert built_unverified_repair_inert_context(ctx) is True
    assert verified_goal_claim_authorized(ctx) is False


def test_structural_abstention_terminalization_rejects_real_unsatisfied_verdicts() -> None:
    ctx = _ctx(
        last_test_ok=True,
        last_full_workflow_test_ok=True,
        latest_diagnosis_repair_contract=_no_repair_unverified_contract(),
        completion_verification_result=_evaluated(_verdict("c0", "unsatisfied", "no_evidence")),
    )

    assert verified_goal_satisfied_context(ctx) is False
    assert built_unverified_repair_inert_context(ctx) is False


def test_structural_abstention_terminalization_rejects_contradictions() -> None:
    ctx = _ctx(
        last_test_ok=True,
        last_full_workflow_test_ok=True,
        latest_diagnosis_repair_contract=_no_repair_unverified_contract(),
        completion_verification_result=_evaluated(_verdict("c0", "unsatisfied", "evidence_contradicts")),
    )

    assert verified_goal_satisfied_context(ctx) is False
    assert built_unverified_repair_inert_context(ctx) is False


def test_claim_authorized_with_adjudicated_evidence() -> None:
    ctx = _legacy_verified_ctx()
    ctx.completion_verification_result = _evaluated(_verdict("c0", "satisfied", "evidence_confirms"))
    assert verified_goal_claim_authorized(ctx) is True


def test_unknown_only_verdicts_never_route_to_repair() -> None:
    ctx = _ctx(last_workflow_yaml="workflow_definition:\n  blocks:\n    - block_type: extraction\n      label: e\n")
    all_unknown = _evaluated(_verdict("c0", "unknown", "unknown"))
    assert _outcome_failure_warrants_repair(ctx, all_unknown) is False


def _policy_inputs(snapshot: StoredCriteriaSnapshot | None) -> RequestPolicyGuardrailInputs:
    return RequestPolicyGuardrailInputs(
        user_message="build a workflow",
        workflow_yaml="",
        chat_history_text="",
        chat_history_messages=[],
        global_llm_context="",
        organization_id="org-1",
        request_policy_handler=None,
        turn_intent_handler=None,
        stored_completion_criteria=snapshot,
    )


def test_reconcile_on_context_adopts_stored_criteria_onto_policy() -> None:
    stored = _stored("the item is in the cart", "the order total is extracted")
    policy = RequestPolicy(completion_criteria=[_criterion("x0", "the item is in the cart")])
    ctx = _ctx()
    _reconcile_completion_criteria_on_context(
        ctx, policy, _policy_inputs(StoredCriteriaSnapshot(active=stored, next_epoch=2))
    )
    assert [c.outcome for c in policy.completion_criteria] == [c.outcome for c in stored.criteria]
    assert ctx.completion_criteria_turn_state is not None
    assert ctx.completion_criteria_turn_state.decision is not None
    assert ctx.completion_criteria_turn_state.decision.reason == "kept"


def test_reconcile_on_context_creates_typed_criteria_without_a_contract_version() -> None:
    policy = RequestPolicy(completion_criteria=[_criterion("c0", "the item is in the cart")])
    ctx = _ctx()

    _reconcile_completion_criteria_on_context(ctx, policy, _policy_inputs(StoredCriteriaSnapshot()))

    plan = plan_persistence(ctx.completion_criteria_turn_state)
    assert plan is not None
    assert plan.create_criteria == tuple(policy.completion_criteria)


def test_reconcile_on_context_skips_without_snapshot() -> None:
    policy = RequestPolicy(completion_criteria=[_criterion("c0", "the item is in the cart")])
    ctx = _ctx()
    _reconcile_completion_criteria_on_context(ctx, policy, _policy_inputs(None))
    assert ctx.completion_criteria_turn_state is None


def test_reconcile_on_context_floors_presence_only_requested_output_on_degraded_snapshot() -> None:
    policy = RequestPolicy(completion_criteria=[_presence_only_requested_output()])
    ctx = _ctx()
    _reconcile_completion_criteria_on_context(ctx, policy, _policy_inputs(None))
    assert ctx.completion_criteria_turn_state is None
    assert [c.output_path for c in policy.completion_criteria] == [None]
    assert [c.kind for c in policy.completion_criteria] == ["outcome"]
    requested, _remaining = split_requested_output_criteria(list(policy.completion_criteria))
    assert requested == []


def test_stored_active_criteria_forwarded_only_with_active_set() -> None:
    stored = _stored("the item is in the cart")
    snapshot = StoredCriteriaSnapshot(active=stored, next_epoch=2)
    assert _stored_active_completion_criteria(_policy_inputs(snapshot)) == list(stored.criteria)
    assert _stored_active_completion_criteria(_policy_inputs(StoredCriteriaSnapshot())) is None
    assert _stored_active_completion_criteria(_policy_inputs(None)) is None


def _presence_only_requested_output() -> CompletionCriterion:
    return _criterion(
        "c_conf",
        "the confirmation number is returned",
        output_path="output.confirmation_number",
        kind="terminal_action",
        terminal_action_family="request",
    )


def test_reconcile_floors_grading_plane_but_persists_typed_originals() -> None:
    policy = RequestPolicy(completion_criteria=[_presence_only_requested_output()])
    ctx = _ctx()

    _reconcile_completion_criteria_on_context(
        ctx, policy, _policy_inputs(StoredCriteriaSnapshot(active=None, next_epoch=1))
    )

    assert [c.output_path for c in policy.completion_criteria] == [None]
    assert [c.kind for c in policy.completion_criteria] == ["outcome"]
    turn_state = ctx.completion_criteria_turn_state
    assert turn_state is not None and turn_state.decision is not None
    assert [c.output_path for c in turn_state.decision.criteria] == ["output.confirmation_number"]
    assert [c.kind for c in turn_state.decision.criteria] == ["terminal_action"]
    plan = plan_persistence(turn_state)
    assert plan is not None and plan.creates_set is True
    assert [c.output_path for c in plan.create_criteria] == ["output.confirmation_number"]
    assert [c.kind for c in plan.create_criteria] == ["terminal_action"]


def test_reconcile_presence_only_floor_does_not_churn_across_turns() -> None:
    policy1 = RequestPolicy(completion_criteria=[_presence_only_requested_output()])
    ctx1 = _ctx()
    _reconcile_completion_criteria_on_context(
        ctx1, policy1, _policy_inputs(StoredCriteriaSnapshot(active=None, next_epoch=1))
    )
    plan1 = plan_persistence(ctx1.completion_criteria_turn_state)
    assert plan1 is not None and plan1.create_epoch == 1

    stored = StoredCriteriaSet(set_id="wccs_1", goal_epoch=1, criteria=plan1.create_criteria)
    policy2 = RequestPolicy(completion_criteria=[_presence_only_requested_output()])
    ctx2 = _ctx()
    _reconcile_completion_criteria_on_context(
        ctx2, policy2, _policy_inputs(StoredCriteriaSnapshot(active=stored, next_epoch=2))
    )

    turn_state2 = ctx2.completion_criteria_turn_state
    assert turn_state2 is not None and turn_state2.decision is not None
    assert turn_state2.decision.action == "adopt_stored"
    assert turn_state2.decision.reason == "kept"
    assert plan_persistence(turn_state2) is None
    assert [c.output_path for c in policy2.completion_criteria] == [None]
    assert [c.kind for c in policy2.completion_criteria] == ["outcome"]


def test_reconcile_clarification_turn_floors_grading_but_never_persists() -> None:
    presence_only = _presence_only_requested_output()
    stored = StoredCriteriaSet(set_id="wccs_1", goal_epoch=1, criteria=(presence_only,))
    policy = RequestPolicy(
        completion_criteria=[presence_only],
        user_response_policy="ask_clarification",
    )
    ctx = _ctx()

    _reconcile_completion_criteria_on_context(
        ctx, policy, _policy_inputs(StoredCriteriaSnapshot(active=stored, next_epoch=2))
    )

    turn_state = ctx.completion_criteria_turn_state
    assert turn_state is not None and turn_state.decision is not None
    assert turn_state.decision.action == "adopt_stored"
    assert [c.output_path for c in policy.completion_criteria] == [None]
    assert [c.kind for c in policy.completion_criteria] == ["outcome"]
    assert [c.output_path for c in turn_state.decision.criteria] == ["output.confirmation_number"]
    assert plan_persistence(turn_state) is None


def test_plan_persistence_for_supersede_creates_new_epoch_and_points_back() -> None:
    stored = _stored("the item is in the cart", set_id="wccs_old", epoch=2)
    snapshot = StoredCriteriaSnapshot(active=stored, next_epoch=3)
    decision = reconcile_completion_criteria(
        snapshot, [_criterion("c0", "the invoice totals are extracted")], actionable=True
    )
    turn_state = build_turn_state(snapshot, decision)
    plan = plan_persistence(turn_state)
    assert plan is not None
    assert plan.creates_set is True
    assert plan.create_epoch == 3
    assert plan.supersede_set_id == "wccs_old"
    assert plan.supersede_reason == "not_subset"


def test_plan_persistence_none_for_none_decision() -> None:
    assert plan_persistence(None) is None
    assert plan_persistence(CompletionCriteriaTurnState()) is None
    none_state = CompletionCriteriaTurnState(
        decision=ReconcileDecision(action="none", reason="no_criteria", epoch=0, criteria=())
    )
    assert plan_persistence(none_state) is None
