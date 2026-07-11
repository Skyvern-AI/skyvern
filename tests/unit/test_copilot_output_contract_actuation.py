"""Tests for the SKY-11879 output-contract actuation primitive: the total lattice
resolver, blocker-family classifier, Option-A single-mapping-local static return,
progress-gated counter reset, and the arm-D no-observable-source terminal.

OSS-synced: only synthetic labels and RFC-2606 placeholder identifiers.
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot import enforcement
from skyvern.forge.sdk.copilot.blocker_signal import (
    assert_clean_user_facing_text,
    build_output_source_unobservable_blocker_signal,
)
from skyvern.forge.sdk.copilot.build_test_outcome import RecordedBuildTestOutcome
from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, CopilotContext
from skyvern.forge.sdk.copilot.output_contracts import (
    OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
    OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
    OutputContractActuation,
    OutputContractActuationEvidence,
    OutputContractActuationKind,
    OutputContractAdvisoryState,
    OutputContractBailFamily,
    classify_output_contract_bail_family,
    resolve_output_contract_actuation,
)
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.result_evidence import loaded_result_composition_evidence_from_page
from skyvern.forge.sdk.copilot.runtime import (
    output_contract_ladder_unresolved,
    record_author_time_gate_ablation_event,
)
from skyvern.forge.sdk.copilot.tools import workflow_update as wu
from skyvern.forge.sdk.copilot.turn_halt import TurnHaltKind, turn_halt_from_blocker_signal
from tests.unit.copilot_test_helpers import make_copilot_ctx

_ALL_SPLIT_BLOCKERS = [
    "static_return_envelope_unavailable",
    "parameter_reconciliation_failed",
    "extraction_boundary_ambiguous",
    "extraction_suffix_contains_browser_actions",
    "extraction_retains_full_spine",
    "insufficient_durable_stages",
    "target_block_not_resolved_in_parsed",
]


@pytest.fixture(autouse=True)
def _disable_author_time_gate_log_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", False)


def test_resolver_is_total_over_the_evidence_family_matrix() -> None:
    kinds = set()
    for (
        family,
        imposed,
        click,
        observed,
        prior,
        unconsumed,
        advisory,
        exhausted,
        declick,
        grantable,
        run_observed,
        run_bound,
        carried_page,
    ) in itertools.product(
        list(OutputContractBailFamily),
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        list(OutputContractAdvisoryState),
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
    ):
        evidence = OutputContractActuationEvidence(
            imposed_available=imposed,
            click_only_spine=click,
            observed_required_values=observed,
            prior_actuation=prior,
            prior_directive_unconsumed=unconsumed,
            advisory_state=advisory,
            actuation_progress_exhausted=exhausted,
            declick_attempt_failed=declick,
            advisory_run_grantable=grantable,
            consumed_run_output_observed=run_observed,
            consumed_run_bound_required_path=run_bound,
            consumed_run_carried_page_extraction=carried_page,
        )
        actuation = resolve_output_contract_actuation(family=family, evidence=evidence)
        assert actuation is not None
        kinds.add(actuation.kind)
        if actuation.kind == OutputContractActuationKind.IMPOSED:
            assert evidence.imposed_available
        if actuation.kind == OutputContractActuationKind.ADVISORY_RUN:
            grantable_run = (
                evidence.advisory_run_grantable and evidence.advisory_state != OutputContractAdvisoryState.CONSUMED
            )
            assert grantable_run or (
                (evidence.observed_required_values or not evidence.click_only_spine)
                and evidence.advisory_state in {OutputContractAdvisoryState.UNUSED, OutputContractAdvisoryState.GRANTED}
            )
        if actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL:
            assert not (
                evidence.advisory_run_grantable and evidence.advisory_state != OutputContractAdvisoryState.CONSUMED
            )
            assert actuation.reason_code in {
                OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
                OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
            }
            if actuation.reason_code == OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE:
                assert (
                    evidence.click_only_spine
                    and not evidence.observed_required_values
                    and evidence.declick_attempt_failed
                )
            else:
                assert (
                    evidence.advisory_state == OutputContractAdvisoryState.CONSUMED
                    and evidence.consumed_run_output_observed
                    and evidence.consumed_run_carried_page_extraction
                    and not evidence.consumed_run_bound_required_path
                )
    assert OutputContractActuationKind.BLOCKED_TERMINAL in kinds
    assert OutputContractActuationKind.IMPOSED in kinds
    assert OutputContractActuationKind.ADVISORY_RUN in kinds


def test_flaky_no_observed_without_declick_attempt_routes_to_actuation_not_terminal() -> None:
    flaky_first_pass = OutputContractActuationEvidence(
        imposed_available=False,
        click_only_spine=True,
        observed_required_values=False,
        prior_actuation=True,
        prior_directive_unconsumed=False,
        advisory_state=OutputContractAdvisoryState.UNUSED,
        actuation_progress_exhausted=True,
        declick_attempt_failed=False,
    )
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=flaky_first_pass)
    assert actuation.kind != OutputContractActuationKind.BLOCKED_TERMINAL


def test_no_source_terminal_requires_failed_declick_attempt() -> None:
    declick_failed = OutputContractActuationEvidence(
        imposed_available=False,
        click_only_spine=True,
        observed_required_values=False,
        prior_actuation=False,
        prior_directive_unconsumed=False,
        advisory_state=OutputContractAdvisoryState.UNUSED,
        actuation_progress_exhausted=False,
        declick_attempt_failed=True,
    )
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=declick_failed)
    assert actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert actuation.reason_code == OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE


def test_grantable_advisory_preempts_no_source_terminal() -> None:
    producible_but_flaked = OutputContractActuationEvidence(
        imposed_available=False,
        click_only_spine=True,
        observed_required_values=False,
        prior_actuation=True,
        prior_directive_unconsumed=False,
        advisory_state=OutputContractAdvisoryState.UNUSED,
        actuation_progress_exhausted=True,
        declick_attempt_failed=True,
        advisory_run_grantable=True,
    )
    actuation = resolve_output_contract_actuation(
        family=OutputContractBailFamily.STRUCTURAL, evidence=producible_but_flaked
    )
    assert actuation.kind == OutputContractActuationKind.ADVISORY_RUN


def test_grantable_advisory_yields_to_consumed_exhaustion() -> None:
    consumed = OutputContractActuationEvidence(
        imposed_available=False,
        click_only_spine=True,
        observed_required_values=False,
        prior_actuation=True,
        prior_directive_unconsumed=False,
        advisory_state=OutputContractAdvisoryState.CONSUMED,
        actuation_progress_exhausted=True,
        declick_attempt_failed=False,
        advisory_run_grantable=True,
        consumed_run_output_observed=True,
        consumed_run_carried_page_extraction=True,
        consumed_run_bound_required_path=False,
    )
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=consumed)
    assert actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert actuation.reason_code == OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE


def test_progress_exhaustion_terminals_a_no_source_structural_bail() -> None:
    no_source = OutputContractActuationEvidence(
        imposed_available=False,
        click_only_spine=True,
        observed_required_values=False,
        prior_actuation=False,
        prior_directive_unconsumed=False,
        advisory_state=OutputContractAdvisoryState.UNUSED,
        actuation_progress_exhausted=True,
        declick_attempt_failed=True,
    )
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=no_source)
    assert actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert actuation.reason_code == OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE


def test_observable_source_structural_exhaustion_routes_to_advisory_not_terminal() -> None:
    observable = OutputContractActuationEvidence(
        imposed_available=False,
        click_only_spine=False,
        observed_required_values=False,
        prior_actuation=False,
        prior_directive_unconsumed=False,
        advisory_state=OutputContractAdvisoryState.UNUSED,
        actuation_progress_exhausted=True,
    )
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=observable)
    assert actuation.kind == OutputContractActuationKind.ADVISORY_RUN


def test_click_only_but_observed_structural_exhaustion_routes_to_advisory() -> None:
    p5_shape = OutputContractActuationEvidence(
        imposed_available=False,
        click_only_spine=True,
        observed_required_values=True,
        prior_actuation=True,
        prior_directive_unconsumed=False,
        advisory_state=OutputContractAdvisoryState.UNUSED,
        actuation_progress_exhausted=True,
    )
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=p5_shape)
    assert actuation.kind == OutputContractActuationKind.ADVISORY_RUN


def test_exhaustion_takes_one_advisory_run_then_terminals_only_after_consumed() -> None:
    unused = OutputContractActuationEvidence(
        False, False, False, False, False, OutputContractAdvisoryState.UNUSED, True
    )
    run = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=unused)
    assert run.kind == OutputContractActuationKind.ADVISORY_RUN
    granted_no_run = OutputContractActuationEvidence(
        False, False, False, False, False, OutputContractAdvisoryState.GRANTED, True
    )
    still_advisory = resolve_output_contract_actuation(
        family=OutputContractBailFamily.STATIC_RETURN, evidence=granted_no_run
    )
    assert still_advisory.kind == OutputContractActuationKind.ADVISORY_RUN
    consumed = OutputContractActuationEvidence(
        False,
        False,
        False,
        False,
        False,
        OutputContractAdvisoryState.CONSUMED,
        True,
        consumed_run_output_observed=True,
        consumed_run_carried_page_extraction=True,
        consumed_run_bound_required_path=False,
    )
    terminal = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=consumed)
    assert terminal.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert terminal.reason_code == OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE


def test_consumed_exhaustion_without_declick_names_actuation_exhausted_not_unobservable() -> None:
    click_only_no_declick = OutputContractActuationEvidence(
        False,
        True,
        False,
        False,
        False,
        OutputContractAdvisoryState.CONSUMED,
        True,
        False,
        consumed_run_output_observed=True,
        consumed_run_carried_page_extraction=True,
        consumed_run_bound_required_path=False,
    )
    actuation = resolve_output_contract_actuation(
        family=OutputContractBailFamily.STATIC_RETURN, evidence=click_only_no_declick
    )
    assert actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert actuation.reason_code == OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE


def test_progress_exhaustion_names_source_unobservable_for_click_only_after_declick() -> None:
    click_only = OutputContractActuationEvidence(
        False, True, False, False, False, OutputContractAdvisoryState.CONSUMED, True, True
    )
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=click_only)
    assert actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert actuation.reason_code == OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE


def test_resolver_prefers_impose_then_directive_then_source_terminal() -> None:
    imposed = OutputContractActuationEvidence(True, True, False, True, True, OutputContractAdvisoryState.UNUSED)
    assert (
        resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=imposed).kind
        == OutputContractActuationKind.IMPOSED
    )
    no_source_declick_failed = OutputContractActuationEvidence(
        False, True, False, True, True, OutputContractAdvisoryState.UNUSED, False, True
    )
    source_terminal = resolve_output_contract_actuation(
        family=OutputContractBailFamily.STRUCTURAL, evidence=no_source_declick_failed
    )
    assert source_terminal.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert source_terminal.reason_code == OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE
    fresh_directive = OutputContractActuationEvidence(
        False, False, False, False, False, OutputContractAdvisoryState.UNUSED
    )
    assert (
        resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=fresh_directive).kind
        == OutputContractActuationKind.STRUCTURE_DIRECTIVE
    )


def test_dispatchable_spine_never_terminals_before_its_run() -> None:
    unscouted = OutputContractActuationEvidence(False, False, False, False, False, OutputContractAdvisoryState.UNUSED)
    first = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=unscouted)
    assert first.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE
    unconsumed = OutputContractActuationEvidence(False, False, False, False, True, OutputContractAdvisoryState.UNUSED)
    second = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=unconsumed)
    assert second.kind == OutputContractActuationKind.ADVISORY_RUN
    consumed = OutputContractActuationEvidence(
        False,
        False,
        False,
        True,
        True,
        OutputContractAdvisoryState.CONSUMED,
        consumed_run_output_observed=True,
        consumed_run_carried_page_extraction=True,
        consumed_run_bound_required_path=False,
    )
    third = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=consumed)
    assert third.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert third.reason_code == OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE


def test_no_source_terminal_requires_progression() -> None:
    no_progression_yet = OutputContractActuationEvidence(
        False, True, False, False, False, OutputContractAdvisoryState.UNUSED
    )
    actuation = resolve_output_contract_actuation(
        family=OutputContractBailFamily.STRUCTURAL, evidence=no_progression_yet
    )
    assert actuation.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE


def test_structural_unconsumed_directive_gets_advisory_run_family_uniform() -> None:
    stuck = OutputContractActuationEvidence(False, False, True, True, True, OutputContractAdvisoryState.UNUSED)
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=stuck)
    assert actuation.kind == OutputContractActuationKind.ADVISORY_RUN


def test_fresh_bail_without_triggers_rearms_a_directive() -> None:
    early = OutputContractActuationEvidence(False, False, False, False, False, OutputContractAdvisoryState.UNUSED)
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STRUCTURAL, evidence=early)
    assert actuation.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE


def test_static_return_unconsumed_directive_gets_advisory_run() -> None:
    stuck = OutputContractActuationEvidence(False, False, True, True, True, OutputContractAdvisoryState.UNUSED)
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=stuck)
    assert actuation.kind == OutputContractActuationKind.ADVISORY_RUN


def test_no_source_terminal_requires_prior_actuation() -> None:
    first_pass = OutputContractActuationEvidence(False, True, False, False, False, OutputContractAdvisoryState.UNUSED)
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=first_pass)
    assert actuation.kind != OutputContractActuationKind.BLOCKED_TERMINAL


def test_arm_d_never_fires_on_p5_shape_observed_source() -> None:
    for family, imposed, click, prior, unconsumed, advisory in itertools.product(
        list(OutputContractBailFamily),
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        list(OutputContractAdvisoryState),
    ):
        evidence = OutputContractActuationEvidence(
            imposed_available=imposed,
            click_only_spine=click,
            observed_required_values=True,
            prior_actuation=prior,
            prior_directive_unconsumed=unconsumed,
            advisory_state=advisory,
        )
        actuation = resolve_output_contract_actuation(family=family, evidence=evidence)
        assert actuation.reason_code != OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE


def test_observed_values_suppresses_no_source_terminal() -> None:
    has_source = OutputContractActuationEvidence(False, True, True, True, False, OutputContractAdvisoryState.UNUSED)
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=has_source)
    assert actuation.kind != OutputContractActuationKind.BLOCKED_TERMINAL
    loaded_source = OutputContractActuationEvidence(
        False, True, False, True, False, OutputContractAdvisoryState.UNUSED, loaded_result_source_producible=True
    )
    actuation = resolve_output_contract_actuation(family=OutputContractBailFamily.STATIC_RETURN, evidence=loaded_source)
    assert actuation.kind != OutputContractActuationKind.BLOCKED_TERMINAL


def test_classifier_maps_every_emitted_blocker_string() -> None:
    assert (
        classify_output_contract_bail_family(["static_return_envelope_unavailable"])
        == OutputContractBailFamily.STATIC_RETURN
    )
    for blocker in _ALL_SPLIT_BLOCKERS:
        if blocker == "static_return_envelope_unavailable":
            continue
        assert classify_output_contract_bail_family([blocker]) == OutputContractBailFamily.STRUCTURAL
    assert (
        classify_output_contract_bail_family(["static_return_envelope_unavailable", "insufficient_durable_stages"])
        == OutputContractBailFamily.STRUCTURAL
    )
    assert classify_output_contract_bail_family(["some_unknown_blocker"]) == OutputContractBailFamily.STRUCTURAL
    assert classify_output_contract_bail_family([]) == OutputContractBailFamily.STRUCTURAL


def test_option_a_rekeys_single_mapping_local() -> None:
    code = (
        "value = page.inner_text('#confirmation')\n"
        'result = {"confirmation_number": value, "account_number": "100245", "start_date": "2026-06-22"}'
    )
    required = {"output.confirmation_number", "output.account_number", "output.start_date"}
    keyed, violations = wu._extraction_code_with_required_static_return(code, required_paths=required)
    assert not violations
    assert required <= wu._code_block_produced_output_paths(keyed)


def test_option_a_falls_through_on_two_mapping_locals() -> None:
    code = 'first = {"confirmation_number": "a"}\nsecond = {"account_number": "b"}'
    required = {"output.confirmation_number", "output.account_number"}
    _, violations = wu._extraction_code_with_required_static_return(code, required_paths=required)
    assert violations


def _counter_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        turn_id="turn_example",
        output_contract_reject_count_by_signature={},
        output_contract_last_reject_fingerprint_by_signature={},
        output_contract_imposed_since_last_reject_by_signature={},
    )


def test_counter_resets_on_structural_fingerprint_delta() -> None:
    ctx = _counter_ctx()
    required = {"output.confirmation_number"}
    first = wu._record_output_contract_family_reject(
        ctx, required, reject_family="metadata_reject", authored_structural_fingerprint="fp_a"
    )
    second = wu._record_output_contract_family_reject(
        ctx, required, reject_family="metadata_reject", authored_structural_fingerprint="fp_a"
    )
    assert (first, second) == (1, 2)
    changed = wu._record_output_contract_family_reject(
        ctx, required, reject_family="metadata_reject", authored_structural_fingerprint="fp_b"
    )
    assert changed == 1


def test_counter_holds_streak_on_cosmetic_churn() -> None:
    ctx = _counter_ctx()
    required = {"output.confirmation_number"}
    for expected in (1, 2, 3):
        count = wu._record_output_contract_family_reject(
            ctx, required, reject_family="metadata_reject", authored_structural_fingerprint="fp_same"
        )
        assert count == expected


def test_counter_resets_after_imposition_marker() -> None:
    ctx = _counter_ctx()
    required = {"output.confirmation_number"}
    wu._record_output_contract_family_reject(
        ctx, required, reject_family="metadata_reject", authored_structural_fingerprint="fp_same"
    )
    signature = next(iter(ctx.output_contract_reject_count_by_signature))
    ctx.output_contract_imposed_since_last_reject_by_signature[signature] = True
    count = wu._record_output_contract_family_reject(
        ctx, required, reject_family="metadata_reject", authored_structural_fingerprint="fp_same"
    )
    assert count == 1
    assert ctx.output_contract_imposed_since_last_reject_by_signature[signature] is False


_STATIC_RETURN_BLOCKERS = ["static_return_envelope_unavailable"]
_PAGE_READ_CODE = "value = page.inner_text('#confirmation')\nresult = value"
_FINGERPRINT = "fp_stage_topology"


def _advisory_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        output_contract_reject_count_by_signature={},
        output_contract_imposed_since_last_reject_by_signature={},
        output_contract_armed_directive_fingerprint_by_signature={},
        output_contract_output_owner_directive_candidates_by_signature={},
        output_contract_actuation_by_signature={},
        output_contract_actuation_count_by_signature={},
        output_contract_declick_attempted_by_signature={},
        output_contract_dispatch_reopened_by_signature={},
        output_contract_page_extraction_imposed_by_signature={},
        output_contract_pending_run_evidence={},
        output_contract_run_output_observed_by_signature={},
        output_contract_run_bound_required_path_by_signature={},
        output_contract_bail_actuated_this_call=False,
        author_time_gate_ablation_events=[],
        latest_recorded_build_test_outcome=None,
        recorded_build_test_outcome_history=[],
        scouted_output_covered_paths=set(),
        composition_page_evidence=None,
        recorded_outcome_binding_constraint=None,
        latest_evaluate_result_composition_steer=None,
        latest_evaluate_result_composition_signature=None,
    )


def _mark_consumed_empty_run(ctx: SimpleNamespace, signature: str, *, page_extraction: bool) -> None:
    ctx.output_contract_actuation_by_signature[signature] = OutputContractAdvisoryState.CONSUMED
    ctx.output_contract_actuation_count_by_signature[signature] = wu._MAX_OUTPUT_CONTRACT_ACTUATIONS_WITHOUT_RUN
    ctx.output_contract_run_output_observed_by_signature[signature] = True
    ctx.output_contract_run_bound_required_path_by_signature[signature] = False
    ctx.output_contract_page_extraction_imposed_by_signature[signature] = page_extraction


def _arm_static_return_advisory_ctx(signature: str) -> SimpleNamespace:
    ctx = _advisory_ctx()
    ctx.output_contract_reject_count_by_signature[signature] = 1
    ctx.output_contract_armed_directive_fingerprint_by_signature[signature] = _FINGERPRINT
    return ctx


def _actuate(ctx: SimpleNamespace, signature: str) -> OutputContractActuationKind:
    actuation = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STATIC_RETURN_BLOCKERS),
        target_code=_PAGE_READ_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint=_FINGERPRINT,
    )
    return actuation.kind


def test_author_time_gate_log_only_requires_local_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _advisory_ctx()
    monkeypatch.setattr(settings, "ENV", "prod")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    assert (
        record_author_time_gate_ablation_event(
            ctx,
            gate_id="metadata_run_preflight_reject",
            reason_code="metadata_contract_required_before_run",
            fingerprint="sig",
            blocked_tool="update_and_run_blocks",
        )
        is False
    )
    assert ctx.author_time_gate_ablation_events == []

    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", False)
    assert (
        record_author_time_gate_ablation_event(
            ctx,
            gate_id="metadata_run_preflight_reject",
            reason_code="metadata_contract_required_before_run",
            fingerprint="sig",
            blocked_tool="update_and_run_blocks",
        )
        is False
    )
    assert ctx.author_time_gate_ablation_events == []

    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    assert (
        record_author_time_gate_ablation_event(
            ctx,
            gate_id="metadata_run_preflight_reject",
            reason_code="metadata_contract_required_before_run",
            fingerprint="sig",
            blocked_tool="update_and_run_blocks",
        )
        is True
    )
    assert ctx.author_time_gate_ablation_events[-1].log_only is True


def test_log_only_output_contract_actuation_records_without_granting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    signature = "sig_log_only"
    ctx = _arm_static_return_advisory_ctx(signature)

    assert _actuate(ctx, signature) == OutputContractActuationKind.ADVISORY_RUN

    assert ctx.output_contract_actuation_by_signature == {}
    assert ctx.output_contract_actuation_count_by_signature == {}
    assert ctx.output_contract_declick_attempted_by_signature == {}
    event = ctx.author_time_gate_ablation_events[-1]
    assert event.gate_id == "output_contract_actuation"
    assert event.reason_code == "advisory_run"
    assert event.blocked_tool == "update_workflow"
    assert event.fingerprint == _FINGERPRINT
    assert event.log_only is True


def test_log_only_terminal_stash_does_not_duplicate_output_contract_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    signature = "sig_terminal_log_only"
    ctx = _advisory_ctx()
    ctx.turn_halt = None
    _mark_consumed_empty_run(ctx, signature, page_extraction=True)

    actuation = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STATIC_RETURN_BLOCKERS),
        target_code=_PAGE_READ_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint=_FINGERPRINT,
    )
    assert actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    wu._stash_output_source_unobservable_terminal(
        ctx,
        reason_code=actuation.reason_code,
        required_paths={"output.confirmation_number"},
        block_label="collect_confirmation",
        signature=signature,
        blockers=list(_STATIC_RETURN_BLOCKERS),
    )

    assert len(ctx.author_time_gate_ablation_events) == 1
    assert ctx.author_time_gate_ablation_events[0].fingerprint == _FINGERPRINT
    assert ctx.turn_halt is None


def test_log_only_metadata_preflight_records_without_rejecting_or_consuming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    signature = "sig_metadata"
    ctx = _advisory_ctx()
    ctx.output_contract_actuation_by_signature[signature] = OutputContractAdvisoryState.GRANTED
    evaluation = SimpleNamespace(
        has_deficiencies=True,
        canonical_signature=signature,
        can_attempt_run=False,
        payload={"reason_code": "output_contract_required"},
        reason_code="output_contract_required",
        block_label="collect_confirmation",
        required_paths={"output.confirmation_number"},
        missing_metadata_paths=["output.confirmation_number"],
        missing_schema_paths=[],
        missing_return_paths=[],
        shape_violations=[],
    )
    monkeypatch.setattr(wu, "_impose_output_contract_envelope_after_steering", lambda *args: (args[1], args[2], False))
    monkeypatch.setattr(wu, "_scaffold_metadata_contract_for_update", lambda *args: (args[2], False))
    monkeypatch.setattr(wu, "_evaluate_output_contract_for_code_block", lambda *args, **kwargs: evaluation)

    result = wu._metadata_contract_run_preflight_reject(ctx, "workflow: yaml", {})

    assert result is None
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED
    assert ctx.output_contract_reject_count_by_signature == {}
    event = ctx.author_time_gate_ablation_events[-1]
    assert event.gate_id == "metadata_run_preflight_reject"
    assert event.reason_code == "output_contract_required"
    assert event.blocked_tool == "update_and_run_blocks"
    assert event.fingerprint == wu._output_contract_structural_fingerprint("workflow: yaml", signature)
    assert event.log_only is True


def test_log_only_metadata_preflight_skips_run_attemptable_without_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    ctx = _advisory_ctx()
    evaluation = SimpleNamespace(
        has_deficiencies=True,
        canonical_signature="sig_run_attemptable",
        can_attempt_run=True,
        payload={"reason_code": "output_contract_required"},
        reason_code="output_contract_required",
        block_label="collect_confirmation",
        required_paths={"output.confirmation_number"},
        missing_metadata_paths=["output.confirmation_number"],
        missing_schema_paths=[],
        missing_return_paths=[],
        shape_violations=[],
    )
    monkeypatch.setattr(wu, "_impose_output_contract_envelope_after_steering", lambda *args: (args[1], args[2], False))
    monkeypatch.setattr(wu, "_scaffold_metadata_contract_for_update", lambda *args: (args[2], False))
    monkeypatch.setattr(wu, "_evaluate_output_contract_for_code_block", lambda *args, **kwargs: evaluation)

    result = wu._metadata_contract_run_preflight_reject(ctx, "workflow: yaml", {})

    assert result is None
    assert ctx.author_time_gate_ablation_events == []
    assert ctx.output_contract_reject_count_by_signature == {}


def test_credential_scout_submit_gate_still_blocks_with_author_time_log_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY", True)
    ctx = SimpleNamespace(
        block_authoring_policy=wu.BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        workflow_yaml="",
        scout_trajectory=[
            {
                "tool_name": "fill_credential_field",
                "credential_id": "cred_safe",
                "credential_field": "password",
                "source_url": "https://login.example.test/login",
            }
        ],
    )
    workflow_yaml = """
workflow_definition:
  parameters:
    - parameter_type: workflow
      workflow_parameter_type: credential_id
      key: login_credentials
      default_value: cred_safe
  blocks:
    - block_type: code
      label: login
      parameter_keys:
        - login_credentials
      code: |
        await page.locator("#password").fill(login_credentials.password)
        await page.get_by_role("button", name="Submit").click()
"""

    errors = wu._credentialed_code_block_scout_gate_errors(workflow_yaml, ctx)

    assert len(errors) == 1
    assert "a later submit action on the same page" in errors[0]


def test_advisory_grant_survives_double_preflight_pass() -> None:
    signature = "sig_double_pass"
    ctx = _arm_static_return_advisory_ctx(signature)
    first = _actuate(ctx, signature)
    assert first == OutputContractActuationKind.ADVISORY_RUN
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED
    second = _actuate(ctx, signature)
    assert second == OutputContractActuationKind.ADVISORY_RUN
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED


def test_advisory_grant_yields_exactly_one_adjudicating_run_per_signature() -> None:
    signature = "sig_one_run"
    ctx = _arm_static_return_advisory_ctx(signature)
    assert _actuate(ctx, signature) == OutputContractActuationKind.ADVISORY_RUN
    consumed = wu.consume_output_contract_advisory_grant_for_run_result(
        ctx, {"data": {"workflow_run_id": "wr_dispatched"}}
    )
    assert consumed == [signature]
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.CONSUMED
    assert (
        wu.consume_output_contract_advisory_grant_for_run_result(ctx, {"data": {"workflow_run_id": "wr_dispatched"}})
        == []
    )
    _mark_consumed_empty_run(ctx, signature, page_extraction=True)
    assert _actuate(ctx, signature) == OutputContractActuationKind.BLOCKED_TERMINAL


def test_advisory_grant_is_not_consumed_without_workflow_run_id() -> None:
    signature = "sig_null_run"
    ctx = _arm_static_return_advisory_ctx(signature)
    assert _actuate(ctx, signature) == OutputContractActuationKind.ADVISORY_RUN

    consumed = wu.consume_output_contract_advisory_grant_for_run_result(ctx, {"data": {"workflow_run_id": None}})

    assert consumed == []
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED


def test_advisory_grant_arms_pending_run_output_evidence() -> None:
    signature = "sig_pending_output"
    ctx = _arm_static_return_advisory_ctx(signature)

    assert _actuate(ctx, signature) == OutputContractActuationKind.ADVISORY_RUN

    assert ctx.output_contract_pending_run_evidence[signature] == ["output.confirmation_number"]


def test_model_churn_keeps_grant_until_run_dispatch_consumes_it() -> None:
    signature = "sig_churn_then_dispatch"
    ctx = _arm_static_return_advisory_ctx(signature)
    assert _actuate(ctx, signature) == OutputContractActuationKind.ADVISORY_RUN
    assert _actuate(ctx, signature) == OutputContractActuationKind.ADVISORY_RUN
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED
    assert wu.consume_output_contract_advisory_grant_for_run_result(ctx, {"data": {"workflow_run_id": "wr_1"}}) == [
        signature
    ]
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.CONSUMED


def test_advisory_terminal_requires_consumed_not_granted() -> None:
    signature = "sig_terminal"
    ctx = _arm_static_return_advisory_ctx(signature)
    ctx.output_contract_actuation_by_signature[signature] = OutputContractAdvisoryState.GRANTED
    assert _actuate(ctx, signature) == OutputContractActuationKind.ADVISORY_RUN
    _mark_consumed_empty_run(ctx, signature, page_extraction=True)
    actuation = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STATIC_RETURN_BLOCKERS),
        target_code=_PAGE_READ_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint=_FINGERPRINT,
    )
    assert actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert actuation.reason_code == OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE


def test_arm_d_signal_maps_to_output_source_unobservable_halt() -> None:
    signal = build_output_source_unobservable_blocker_signal(
        reason_code=OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
        required_paths={"output.confirmation_number"},
        block_label="collect_confirmation",
    )
    assert signal.renders_final_reply
    assert signal.preserves_workflow_draft
    assert_clean_user_facing_text(signal.user_facing_reason)
    halt = turn_halt_from_blocker_signal(signal, source="workflow_update")
    assert halt is not None
    assert halt.kind == TurnHaltKind.OUTPUT_SOURCE_UNOBSERVABLE


def test_arm_d_actuation_exhausted_signal_also_maps() -> None:
    signal = build_output_source_unobservable_blocker_signal(
        reason_code=OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
        required_paths={"output.account_number"},
        block_label="collect_account",
    )
    halt = turn_halt_from_blocker_signal(signal, source="workflow_update")
    assert halt is not None
    assert halt.kind == TurnHaltKind.OUTPUT_SOURCE_UNOBSERVABLE


_STRUCTURAL_BLOCKERS = ["insufficient_durable_stages"]
_CLICK_ONLY_CODE = "page.click('#toggle-service')"


def _actuate_structural(ctx: SimpleNamespace, signature: str, fingerprint: str) -> OutputContractActuationKind:
    return wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STRUCTURAL_BLOCKERS),
        target_code=_PAGE_READ_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint=fingerprint,
    ).kind


def _actuate_click_only(ctx: SimpleNamespace, signature: str, fingerprint: str) -> OutputContractActuation:
    return wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STRUCTURAL_BLOCKERS),
        target_code=_CLICK_ONLY_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint=fingerprint,
    )


def test_click_only_no_source_bail_terminals_after_one_declick_cycle() -> None:
    signature = "sig_never_converges"
    ctx = _advisory_ctx()
    first = _actuate_click_only(ctx, signature, "fp_0")
    assert first.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE
    assert ctx.output_contract_declick_attempted_by_signature[signature] is True
    terminal = _actuate_click_only(ctx, signature, "fp_1")
    assert terminal.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert terminal.reason_code == OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE


def test_grantable_split_failure_suppresses_arm_d_and_escalates_to_advisory() -> None:
    signature = "sig_flaky_split"
    ctx = _arm_static_return_advisory_ctx(signature)
    ctx.output_contract_declick_attempted_by_signature[signature] = True
    actuation = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STRUCTURAL_BLOCKERS),
        target_code=_CLICK_ONLY_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint=_FINGERPRINT,
        advisory_run_grantable=True,
    )
    assert actuation.kind == OutputContractActuationKind.ADVISORY_RUN
    assert actuation.reason_code != OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED


def test_grantable_split_failure_first_pass_arms_directive_not_arm_d() -> None:
    signature = "sig_flaky_split_first"
    ctx = _advisory_ctx()
    ctx.output_contract_declick_attempted_by_signature[signature] = True
    actuation = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STRUCTURAL_BLOCKERS),
        target_code=_CLICK_ONLY_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint="fp_first",
        advisory_run_grantable=True,
    )
    assert actuation.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE


def test_click_only_declick_flag_clears_when_source_becomes_observable() -> None:
    signature = "sig_scout_recovers"
    ctx = _advisory_ctx()
    first = _actuate_click_only(ctx, signature, "fp_0")
    assert first.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE
    assert ctx.output_contract_declick_attempted_by_signature.get(signature) is True
    ctx.scouted_output_covered_paths = {"output.confirmation_number"}
    recovered = _actuate_click_only(ctx, signature, "fp_1")
    assert recovered.kind != OutputContractActuationKind.BLOCKED_TERMINAL
    assert signature not in ctx.output_contract_declick_attempted_by_signature


def test_observed_required_values_exact_and_lineage_match() -> None:
    ctx = SimpleNamespace(scouted_output_covered_paths={"output.order.id"}, composition_page_evidence=None)
    assert wu._observed_required_output_values(ctx, {"output.order.id"}) is True
    assert wu._observed_required_output_values(ctx, {"output.order"}) is True
    assert wu._observed_required_output_values(ctx, {"output.order.id.raw"}) is True


def test_observed_required_values_rejects_sibling_root_only_overlap() -> None:
    ctx = SimpleNamespace(scouted_output_covered_paths={"output.confirmation_number"}, composition_page_evidence=None)
    assert wu._observed_required_output_values(ctx, {"output.account_number"}) is False


def test_observable_structural_exhaustion_advises_then_terminals_only_after_consumed() -> None:
    signature = "sig_structural_advisory"
    ctx = _advisory_ctx()
    ctx.output_contract_reject_count_by_signature[signature] = 1
    ctx.output_contract_armed_directive_fingerprint_by_signature[signature] = _FINGERPRINT
    advisory = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STRUCTURAL_BLOCKERS),
        target_code=_PAGE_READ_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint=_FINGERPRINT,
    )
    assert advisory.kind == OutputContractActuationKind.ADVISORY_RUN
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED
    wu.consume_output_contract_advisory_grant_for_run(ctx)
    _mark_consumed_empty_run(ctx, signature, page_extraction=True)
    terminal = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STRUCTURAL_BLOCKERS),
        target_code=_PAGE_READ_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint=_FINGERPRINT,
    )
    assert terminal.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert terminal.reason_code == OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE


def test_progress_gate_does_not_double_count_an_already_armed_fingerprint() -> None:
    signature = "sig_same_turn"
    ctx = _advisory_ctx()
    ctx.output_contract_armed_directive_fingerprint_by_signature[signature] = "fp_armed"
    kind = _actuate_structural(ctx, signature, "fp_armed")
    assert kind == OutputContractActuationKind.ADVISORY_RUN
    assert signature not in ctx.output_contract_actuation_count_by_signature


def test_progress_gate_resets_on_executed_run() -> None:
    signature = "sig_run_resets"
    ctx = _advisory_ctx()
    for attempt in range(wu._MAX_OUTPUT_CONTRACT_ACTUATIONS_WITHOUT_RUN):
        _actuate_structural(ctx, signature, f"fp_{attempt}")
    assert ctx.output_contract_actuation_count_by_signature[signature] == wu._MAX_OUTPUT_CONTRACT_ACTUATIONS_WITHOUT_RUN
    wu.consume_output_contract_advisory_grant_for_run(ctx)
    assert not ctx.output_contract_actuation_count_by_signature
    assert _actuate_structural(ctx, signature, "fp_post_run") == OutputContractActuationKind.STRUCTURE_DIRECTIVE


def _ladder_ctx() -> SimpleNamespace:
    ctx = _advisory_ctx()
    ctx.turn_halt = None
    ctx.output_contract_bail_actuated_this_call = False
    return ctx


def _make_evaluation(signature: str, *, block_label: str = "", shape_violations: list[str] | None = None) -> object:
    return wu._OutputContractEvaluation(
        block_label=block_label,
        artifact_id="",
        required_paths={"output.confirmation_number"},
        observation_paths={"output.confirmation_number"},
        declaration_paths=set(),
        source="requested_output",
        reason_code="output_contract_required",
        missing_metadata_paths=[],
        missing_schema_paths=[],
        missing_return_paths=[],
        shape_violations=shape_violations or [],
        canonical_signature=signature,
        payload={},
        repair_context=None,
    )


_PAGE_READ_YAML = (
    "workflow_definition:\n"
    "  blocks:\n"
    "    - block_type: code\n"
    "      label: collect\n"
    "      code: \"value = page.inner_text('#confirmation')\\nresult = value\"\n"
)


def test_ladder_unresolved_true_for_landed_actuation() -> None:
    ctx = _ladder_ctx()
    ctx.output_contract_actuation_count_by_signature["sig_run2"] = 1
    assert output_contract_ladder_unresolved(ctx) is True


def test_ladder_resolved_for_rejects_only_state_without_actuation() -> None:
    ctx = _ladder_ctx()
    ctx.output_contract_reject_count_by_signature["sig_owner_ambiguous"] = 3
    assert output_contract_ladder_unresolved(ctx) is False


def test_ladder_unresolved_true_for_granted_advisory() -> None:
    ctx = _ladder_ctx()
    ctx.output_contract_actuation_by_signature["sig_granted"] = OutputContractAdvisoryState.GRANTED
    assert output_contract_ladder_unresolved(ctx) is True


def test_ladder_resolved_once_advisory_consumed() -> None:
    ctx = _ladder_ctx()
    ctx.output_contract_actuation_count_by_signature["sig_done"] = 3
    ctx.output_contract_actuation_by_signature["sig_done"] = OutputContractAdvisoryState.CONSUMED
    assert output_contract_ladder_unresolved(ctx) is False


def test_ladder_resolved_when_no_output_contract_activity() -> None:
    assert output_contract_ladder_unresolved(_ladder_ctx()) is False


def test_reject_seam_adjudication_grants_advisory_within_caps_when_imposition_early_outs() -> None:
    ctx = _ladder_ctx()
    signature = "sig_early_out"
    evaluation = _make_evaluation(signature, block_label="collect")
    for _ in range(wu._MAX_OUTPUT_CONTRACT_ACTUATIONS_WITHOUT_RUN + 1):
        ctx.output_contract_bail_actuated_this_call = False
        wu._adjudicate_output_contract_ladder_after_reject(
            ctx, evaluation, workflow_yaml=_PAGE_READ_YAML, current_fingerprint="fp_early_out"
        )
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED
    assert ctx.turn_halt is None


def test_same_fresh_signature_emits_one_reject_before_typed_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ladder_ctx()
    signature = "sig_first_contact_example"
    evaluation = _make_evaluation(signature, block_label="collect")
    monkeypatch.setattr(wu, "_record_code_authoring_guardrail_reject", lambda _ctx: None)

    first = wu._record_output_contract_reject(
        ctx,
        evaluation,
        summary="First typed deficiency.",
        authored_structural_fingerprint="fp_first_contact",
        workflow_yaml=_PAGE_READ_YAML,
    )
    first_outcome = ctx.latest_recorded_build_test_outcome
    ctx.output_contract_bail_actuated_this_call = False
    second = wu._record_output_contract_reject(
        ctx,
        evaluation,
        summary="Same typed deficiency.",
        authored_structural_fingerprint="fp_first_contact",
        workflow_yaml=_PAGE_READ_YAML,
    )

    assert first["output_contract_actuation"] == OutputContractActuationKind.STRUCTURE_DIRECTIVE.value
    assert first_outcome is not None
    assert first_outcome.phase == "author_time_reject"
    assert second["output_contract_actuation"] == OutputContractActuationKind.ADVISORY_RUN.value
    assert ctx.latest_recorded_build_test_outcome is None
    assert [entry["phase"] for entry in ctx.recorded_build_test_outcome_history] == ["author_time_reject"]
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED


def test_reject_seam_metadata_required_reaches_advisory_before_no_source_terminal() -> None:
    ctx = _ladder_ctx()
    signature = "sig_metadata_required"
    ctx.output_contract_declick_attempted_by_signature[signature] = True
    for _ in range(wu._MAX_OUTPUT_CONTRACT_ACTUATIONS_WITHOUT_RUN):
        wu._record_output_contract_actuation_progress(ctx, signature)
    workflow_yaml = (
        "workflow_definition:\n"
        "  blocks:\n"
        "    - block_type: code\n"
        "      label: collect\n"
        "      code: \"page.click('#submit')\"\n"
    )

    wu._adjudicate_output_contract_ladder_after_reject(
        ctx, _make_evaluation(signature, block_label="collect"), workflow_yaml=workflow_yaml, current_fingerprint="fp"
    )

    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.GRANTED
    assert ctx.turn_halt is None


def test_reject_seam_adjudication_skips_when_imposition_already_actuated_this_call() -> None:
    ctx = _ladder_ctx()
    ctx.output_contract_bail_actuated_this_call = True
    signature = "sig_already_actuated"
    wu._adjudicate_output_contract_ladder_after_reject(
        ctx, _make_evaluation(signature, block_label="collect"), workflow_yaml=_PAGE_READ_YAML, current_fingerprint="fp"
    )
    assert signature not in ctx.output_contract_actuation_count_by_signature
    assert signature not in ctx.output_contract_actuation_by_signature


def test_reject_seam_adjudication_skips_owner_ambiguity_without_owner_block() -> None:
    ctx = _ladder_ctx()
    signature = "sig_owner_ambiguous"
    wu._adjudicate_output_contract_ladder_after_reject(
        ctx, _make_evaluation(signature, block_label=""), workflow_yaml="", current_fingerprint="fp"
    )
    assert signature not in ctx.output_contract_actuation_count_by_signature
    assert signature not in ctx.output_contract_actuation_by_signature


def test_reject_seam_adjudication_stashes_terminal_after_declick_cycle(monkeypatch: object) -> None:
    ctx = _ladder_ctx()
    signature = "sig_no_source"
    ctx.output_contract_declick_attempted_by_signature[signature] = True
    stashed: list[str] = []

    def capture(_ctx: object, *, reason_code: str, **_kwargs: object) -> None:
        stashed.append(reason_code)

    monkeypatch.setattr(wu, "_stash_output_source_unobservable_terminal", capture)  # type: ignore[attr-defined]
    workflow_yaml = (
        "workflow_definition:\n"
        "  blocks:\n"
        "    - block_type: code\n"
        "      label: collect\n"
        "      code: \"page.click('#toggle-service')\"\n"
    )
    wu._adjudicate_output_contract_ladder_after_reject(
        ctx,
        _make_evaluation(signature, block_label="collect", shape_violations=list(_STRUCTURAL_BLOCKERS)),
        workflow_yaml=workflow_yaml,
        current_fingerprint="fp_no_source",
    )
    assert stashed == [OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE]


def test_observed_but_unbound_run_without_page_extraction_is_not_exhaustion() -> None:
    signature = "sig_empty_run"
    ctx = _advisory_ctx()
    _mark_consumed_empty_run(ctx, signature, page_extraction=False)
    actuation = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STRUCTURAL_BLOCKERS),
        target_code=_PAGE_READ_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint="fp_empty",
    )
    assert actuation.kind != OutputContractActuationKind.BLOCKED_TERMINAL


def test_reopen_requires_observed_unbound_run_and_reenters_the_ladder() -> None:
    signature = "sig_reopen"
    ctx = _advisory_ctx()
    _mark_consumed_empty_run(ctx, signature, page_extraction=False)
    assert wu._reopen_dispatch_lacked_bound_extraction(ctx, signature) is True
    assert ctx.output_contract_dispatch_reopened_by_signature[signature] is True
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.UNUSED


def test_reopen_skips_when_run_bound_a_required_path() -> None:
    signature = "sig_bound"
    ctx = _advisory_ctx()
    _mark_consumed_empty_run(ctx, signature, page_extraction=False)
    ctx.output_contract_run_bound_required_path_by_signature[signature] = True
    assert wu._reopen_dispatch_lacked_bound_extraction(ctx, signature) is False


def test_dispatch_reopen_is_one_shot_per_signature() -> None:
    signature = "sig_one_shot"
    ctx = _advisory_ctx()
    _mark_consumed_empty_run(ctx, signature, page_extraction=False)
    ctx.output_contract_dispatch_reopened_by_signature[signature] = True
    assert wu._reopen_dispatch_lacked_bound_extraction(ctx, signature) is False
    assert ctx.output_contract_actuation_by_signature[signature] == OutputContractAdvisoryState.CONSUMED


def test_page_extraction_carried_consumed_run_terminals_as_exhausted() -> None:
    signature = "sig_carried"
    ctx = _advisory_ctx()
    _mark_consumed_empty_run(ctx, signature, page_extraction=True)
    actuation = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STRUCTURAL_BLOCKERS),
        target_code=_PAGE_READ_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint="fp_carried",
    )
    assert actuation.kind == OutputContractActuationKind.BLOCKED_TERMINAL
    assert actuation.reason_code == OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE


def test_run_output_evidence_covering_required_path_binds() -> None:
    signature = "sig_bound_run"
    ctx = _advisory_ctx()
    wu._arm_pending_run_evidence(ctx, signature, {"output.confirmation_number"})
    result = {"data": {"blocks": [{"label": "collect", "extracted_data": {"confirmation_number": "WTR-1842-DEMO"}}]}}
    wu.record_output_contract_run_output_evidence(ctx, result)
    assert ctx.output_contract_run_output_observed_by_signature[signature] is True
    assert ctx.output_contract_run_bound_required_path_by_signature[signature] is True
    assert not ctx.output_contract_pending_run_evidence


def test_run_output_evidence_empty_output_is_observed_but_unbound() -> None:
    signature = "sig_empty_out"
    ctx = _advisory_ctx()
    wu._arm_pending_run_evidence(ctx, signature, {"output.confirmation_number"})
    wu.record_output_contract_run_output_evidence(
        ctx, {"data": {"blocks": [{"label": "collect", "extracted_data": {}}]}}
    )
    assert ctx.output_contract_run_output_observed_by_signature[signature] is True
    assert ctx.output_contract_run_bound_required_path_by_signature[signature] is False


def test_run_output_evidence_wrong_keys_is_observed_but_unbound() -> None:
    signature = "sig_wrong_keys"
    ctx = _advisory_ctx()
    wu._arm_pending_run_evidence(ctx, signature, {"output.confirmation_number"})
    result = {"data": {"registered_output_parameter_values": [{"value": {"unrelated_field": "x"}}]}}
    wu.record_output_contract_run_output_evidence(ctx, result)
    assert ctx.output_contract_run_output_observed_by_signature[signature] is True
    assert ctx.output_contract_run_bound_required_path_by_signature[signature] is False


def test_run_output_evidence_sibling_under_output_root_is_observed_but_unbound() -> None:
    signature = "sig_output_root_sibling"
    ctx = _advisory_ctx()
    wu._arm_pending_run_evidence(ctx, signature, {"output.confirmation_number"})
    result = {"data": {"blocks": [{"label": "collect", "extracted_data": {"output": {"top_post": "x"}}}]}}
    wu.record_output_contract_run_output_evidence(ctx, result)
    assert ctx.output_contract_run_output_observed_by_signature[signature] is True
    assert ctx.output_contract_run_bound_required_path_by_signature[signature] is False


def test_run_output_evidence_binds_when_value_keyed_under_output_root() -> None:
    signature = "sig_output_root_hit"
    ctx = _advisory_ctx()
    wu._arm_pending_run_evidence(ctx, signature, {"output.confirmation_number"})
    result = {
        "data": {"blocks": [{"label": "collect", "extracted_data": {"output": {"confirmation_number": "WTR-1"}}}]}
    }
    wu.record_output_contract_run_output_evidence(ctx, result)
    assert ctx.output_contract_run_bound_required_path_by_signature[signature] is True


def test_run_output_evidence_null_required_value_is_observed_but_unbound() -> None:
    signature = "sig_null_required"
    ctx = _advisory_ctx()
    wu._arm_pending_run_evidence(ctx, signature, {"output.confirmation_number"})
    result = {
        "data": {
            "blocks": [
                {
                    "label": "collect",
                    "extracted_data": {
                        "output": {"confirmation_number": None},
                        "blocker_type": "portal_unavailable",
                    },
                }
            ]
        }
    }
    wu.record_output_contract_run_output_evidence(ctx, result)
    assert ctx.output_contract_run_output_observed_by_signature[signature] is True
    assert ctx.output_contract_run_bound_required_path_by_signature[signature] is False


def test_run_output_evidence_partial_required_outputs_are_observed_but_unbound() -> None:
    signature = "sig_partial_required"
    ctx = _advisory_ctx()
    wu._arm_pending_run_evidence(
        ctx,
        signature,
        {"output.confirmation_number", "output.account_number", "output.selected_start_date"},
    )
    result = {
        "data": {
            "blocks": [
                {
                    "label": "collect",
                    "extracted_data": {
                        "output": {
                            "account_number": None,
                            "confirmation_number": None,
                            "selected_start_date": "2026-06-22",
                        },
                        "blocker_type": "portal_unavailable",
                        "http_status": 404,
                    },
                }
            ]
        }
    }
    wu.record_output_contract_run_output_evidence(ctx, result)
    assert ctx.output_contract_run_output_observed_by_signature[signature] is True
    assert ctx.output_contract_run_bound_required_path_by_signature[signature] is False


def test_run_output_evidence_deep_sibling_does_not_over_credit_ancestor() -> None:
    signature = "sig_deep_sibling"
    ctx = _advisory_ctx()
    wu._arm_pending_run_evidence(ctx, signature, {"output.order.confirmation.code"})
    result = {"data": {"blocks": [{"label": "collect", "extracted_data": {"output": {"order": {"other": "x"}}}}]}}
    wu.record_output_contract_run_output_evidence(ctx, result)
    assert ctx.output_contract_run_bound_required_path_by_signature[signature] is False


def test_run_output_evidence_deep_leaf_binds() -> None:
    signature = "sig_deep_leaf"
    ctx = _advisory_ctx()
    wu._arm_pending_run_evidence(ctx, signature, {"output.order.confirmation.code"})
    result = {
        "data": {
            "blocks": [{"label": "collect", "extracted_data": {"output": {"order": {"confirmation": {"code": "C"}}}}}]
        }
    }
    wu.record_output_contract_run_output_evidence(ctx, result)
    assert ctx.output_contract_run_bound_required_path_by_signature[signature] is True


def test_output_contract_never_imposes_a_page_extract_call() -> None:
    """Code blocks run on a raw Playwright page, so no output-contract rung may author
    page.extract -- it would resolve to the LLM extraction path and fail at runtime."""
    assert not hasattr(wu, "_impose_page_source_extraction")
    assert not hasattr(wu, "_page_source_extraction_code")


def test_advisory_grant_downgraded_to_directive_when_run_authority_forbids_dispatch() -> None:
    signature = "sig_no_run_authority"
    ctx = _arm_static_return_advisory_ctx(signature)
    ctx.turn_intent = SimpleNamespace(authority=SimpleNamespace(may_run_blocks=False, requires_user_input=False))
    actuation = wu._actuate_output_contract_bail(
        ctx,
        blockers=list(_STATIC_RETURN_BLOCKERS),
        target_code=_PAGE_READ_CODE,
        required_paths={"output.confirmation_number"},
        signature=signature,
        current_fingerprint=_FINGERPRINT,
    )
    assert actuation.kind == OutputContractActuationKind.STRUCTURE_DIRECTIVE
    assert signature not in ctx.output_contract_actuation_by_signature


def test_loaded_result_carrier_is_selector_bound_and_claimed_by_one_contract_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _advisory_ctx()
    events: list[dict[str, object]] = []

    def capture_event(_ctx: object, **kwargs: object) -> bool:
        events.append(kwargs)
        return False

    monkeypatch.setattr(wu, "record_author_time_gate_ablation_event", capture_event)
    ctx.latest_evaluate_result_composition_steer = loaded_result_composition_evidence_from_page(
        {"result_containers": [{"selector": "#results", "row_count": 1, "sample_rows": ["Example customer record"]}]},
        source_tool="evaluate",
        source_url="https://example.com/results",
    )

    first = wu._actuate_output_contract_bail(
        ctx,
        blockers=["static_return_envelope_unavailable"],
        target_code='await page.locator("#results").click()',
        required_paths={"output.record_id"},
        signature="sig-a",
        current_fingerprint="fp-a",
    )

    assert ctx.latest_evaluate_result_composition_signature == "sig-a"

    ctx.output_contract_bail_actuated_this_call = False
    second = wu._actuate_output_contract_bail(
        ctx,
        blockers=["static_return_envelope_unavailable"],
        target_code='await page.locator("#results").click()',
        required_paths={"output.other_id"},
        signature="sig-b",
        current_fingerprint="fp-b",
    )

    assert ctx.latest_evaluate_result_composition_signature == "sig-a"
    assert first.kind is not OutputContractActuationKind.BLOCKED_TERMINAL
    assert second.kind is OutputContractActuationKind.STRUCTURE_DIRECTIVE
    first_payload = events[0]["payload"]
    second_payload = events[1]["payload"]
    assert isinstance(first_payload, dict)
    assert isinstance(second_payload, dict)
    assert first_payload["loaded_result_source_producible"] is True
    assert second_payload["loaded_result_source_producible"] is False


def _antecedent_ctx(*criteria: CompletionCriterion) -> CopilotContext:
    ctx = make_copilot_ctx()
    ctx.block_authoring_policy = wu.BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.request_policy = RequestPolicy(completion_criteria=list(criteria))
    return ctx


def _blocker_contingent_criterion() -> CompletionCriterion:
    return CompletionCriterion(
        id="c_blocker",
        outcome="A blocker is reported when the site blocks submission.",
        contingent_on="the site blocks submission",
        contingent_antecedent_output_path="output.blocker",
    )


def test_contingent_antecedent_joins_declaration_lane_schema_and_skeleton() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_record", outcome="The returned record includes record id.", output_path="output.record_id"
        ),
        _blocker_contingent_criterion(),
    )

    contract = wu._output_contract_required_paths_source(ctx)

    assert contract.observation_paths == {"output.record_id"}
    assert contract.declaration_paths == {"output.blocker"}
    assert contract.union == {"output.record_id", "output.blocker"}
    assert contract.source == "requested_output_contract"
    assert contract.reason_code == "requested_output_contract_missing_output_coverage"
    schema = wu._schema_template_for_required_paths(contract.union, contract.declaration_paths)
    output_properties = schema["properties"]["output"]
    assert output_properties["properties"]["blocker"] == {"type": ["string", "null"]}
    assert output_properties["properties"]["record_id"] == {}
    assert set(output_properties["required"]) == {"record_id", "blocker"}
    skeleton = wu._return_skeleton_for_required_paths(contract.union, contract.declaration_paths)
    assert skeleton == 'return {"output": {"blocker": None, "record_id": record_id}}'


def test_contingent_antecedent_alone_forms_declaration_only_contract() -> None:
    ctx = _antecedent_ctx(_blocker_contingent_criterion())

    contract = wu._output_contract_required_paths_source(ctx)

    assert contract.observation_paths == set()
    assert contract.declaration_paths == {"output.blocker"}
    assert contract.union == {"output.blocker"}


def test_antecedent_overlapping_requested_output_stays_observation() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_record", outcome="The returned record includes record id.", output_path="output.record_id"
        ),
        CompletionCriterion(
            id="c_overlap",
            outcome="The record id is reported when lookup is blocked.",
            contingent_on="the lookup is blocked",
            contingent_antecedent_output_path="output.record_id",
        ),
    )

    contract = wu._output_contract_required_paths_source(ctx)

    assert contract.observation_paths == {"output.record_id"}
    assert contract.declaration_paths == set()


def test_judgment_and_classification_criteria_antecedents_still_contribute() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_judgment",
            outcome="The run reports whether a login gate blocks the target.",
            output_path="output.login_gate_blocks_target",
            expected_output_shape="goal_judgment_boolean",
            requested_output_evidence_source="independent_run_evidence",
            contingent_on="a login gate blocks the target",
            contingent_antecedent_output_path="output.login_blocker",
        ),
        CompletionCriterion(
            id="c_classify",
            outcome="The run classifies the reached path.",
            kind="validation_classification",
            classification_output_key="path_classification",
            expected_classification="login_gated",
            contingent_on="the flow is blocked",
            contingent_antecedent_output_path="output.flow_blocker",
        ),
    )

    contract = wu._output_contract_required_paths_source(ctx)

    assert contract.observation_paths == set()
    assert contract.declaration_paths == {"output.login_blocker", "output.flow_blocker"}


def test_definition_level_antecedent_does_not_contribute() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_definition",
            outcome="The workflow definition names a blocker report.",
            level="definition",
            contingent_on="the site blocks submission",
            contingent_antecedent_output_path="output.blocker",
        )
    )

    assert wu._contingent_antecedent_child_paths(ctx) == set()


def test_mint_degraded_antecedent_does_not_contribute() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_degraded",
            outcome="A blocker is reported when the site blocks submission.",
            contingent_on="the site blocks submission",
            contingent_antecedent_output_path="output.blocker",
            mint_degrade="turn_unsatisfiable_fallback",
        )
    )

    assert wu._contingent_antecedent_child_paths(ctx) == set()


def test_recorded_outcome_source_unions_antecedent() -> None:
    ctx = _antecedent_ctx(_blocker_contingent_criterion())
    ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="repairable_failure",
        reason_code="outcome_not_demonstrated",
        structural_failure_identity="completion:typed-output",
        missing_requested_output_facts=[
            {"output_path": "output.recorded_value", "output_root": "output", "value_status": "no_typed_value"},
        ],
    )

    contract = wu._output_contract_required_paths_source(ctx)

    assert contract.observation_paths == {"output.recorded_value"}
    assert contract.declaration_paths == {"output.blocker"}
    assert contract.source == "recorded_outcome"


def test_metadata_reject_merge_still_fires_with_antecedent_declaration() -> None:
    ctx = _antecedent_ctx(_blocker_contingent_criterion())
    ctx.last_code_authoring_repair_context = CodeAuthoringRepairContext(
        block_label="collect",
        reason_code="metadata_reject",
        required_goal_value_paths=["output.record_id"],
        metadata_contract_source="metadata_reject",
        metadata_contract_reason_code="metadata_contract_missing",
    )

    contract = wu._output_contract_required_paths_source(ctx)

    assert contract.observation_paths == {"output.record_id"}
    assert contract.declaration_paths == {"output.blocker"}
    assert contract.source == "metadata_reject"
    assert contract.reason_code == "metadata_contract_missing"


def test_metadata_reject_round_trip_preserves_lanes() -> None:
    repair = wu._metadata_output_repair_context(
        block_labels=["collect"],
        required_paths={"output.record_id"},
        coverage_reason_code="metadata_contract_missing",
        source="metadata_reject",
        summary="missing contract",
        declaration_paths={"output.blocker"},
    )
    assert repair is not None
    assert repair.required_goal_value_paths == ["output.record_id"]
    assert repair.required_extraction_schema_paths == ["output.blocker", "output.record_id"]
    assert repair.required_code_return_paths == ["output.blocker", "output.record_id"]
    rehydration_ctx = _antecedent_ctx(_blocker_contingent_criterion())
    rehydration_ctx.last_code_authoring_repair_context = repair

    contract = wu._output_contract_required_paths_source(rehydration_ctx)

    assert contract.observation_paths == {"output.record_id"}
    assert contract.declaration_paths == {"output.blocker"}


def test_evaluation_routes_lanes_per_consumer() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_record", outcome="The returned record includes record id.", output_path="output.record_id"
        ),
        _blocker_contingent_criterion(),
    )
    workflow_yaml = (
        "title: Record lookup\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: extract_record\n"
        "    code: |\n"
        '      record_id = await page.inner_text("#record")\n'
        "      return record_id\n"
    )

    evaluation = wu._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

    assert evaluation is not None
    assert evaluation.block_label == "extract_record"
    assert evaluation.required_paths == {"output.record_id", "output.blocker"}
    assert evaluation.observation_paths == {"output.record_id"}
    assert evaluation.declaration_paths == {"output.blocker"}
    assert "output.blocker" not in evaluation.missing_metadata_paths
    assert "output.blocker" in evaluation.missing_schema_paths  # nosemgrep: incomplete-url-substring-sanitization
    assert "output.blocker" in evaluation.missing_return_paths  # nosemgrep: incomplete-url-substring-sanitization
    payload = evaluation.payload
    assert payload["canonical_required_child_paths"] == ["output.blocker", "output.record_id"]
    assert payload["declaration_only_child_paths"] == ["output.blocker"]
    assert payload["missing_goal_value_paths"] == ["output.record_id"]
    assert payload["satisfying_templates"]["return_skeleton"] == (
        'return {"output": {"blocker": None, "record_id": record_id}}'
    )
    template = payload["satisfying_templates"]["code_artifact_metadata"]
    assert template["claimed_outcomes"][0]["goal_value_paths"] == ["output.record_id"]
    blocker_facts = [
        fact for fact in payload["missing_requested_output_facts"] if fact["output_path"] == "output.blocker"
    ]
    assert blocker_facts and blocker_facts[0]["value_status"] == "declaration_required_default_none"
    record_facts = [
        fact for fact in payload["missing_requested_output_facts"] if fact["output_path"] == "output.record_id"
    ]
    assert record_facts and record_facts[0]["value_status"] == "no_typed_value"
    assert evaluation.repair_context is not None
    assert evaluation.repair_context.required_goal_value_paths == ["output.record_id"]
    assert evaluation.repair_context.required_code_return_paths == ["output.blocker", "output.record_id"]
    assert evaluation.canonical_signature == wu._stable_output_contract_key(
        wu._output_contract_scope_key(ctx), evaluation.required_paths
    )


def test_declaration_only_contract_evaluates_with_single_block_owner() -> None:
    ctx = _antecedent_ctx(_blocker_contingent_criterion())
    workflow_yaml = (
        "title: Blocker only\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: submit\n"
        "    code: |\n"
        '      await page.click("#submit")\n'
        "      return {}\n"
    )

    evaluation = wu._evaluate_output_contract_for_code_block(ctx, workflow_yaml, [])

    assert evaluation is not None
    assert evaluation.block_label == "submit"
    assert evaluation.observation_paths == set()
    assert "output.blocker" in evaluation.missing_return_paths  # nosemgrep: incomplete-url-substring-sanitization
    assert evaluation.missing_metadata_paths == []


def test_repair_context_goal_role_never_feeds_enforcement_uncovered_paths() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_record", outcome="The returned record includes record id.", output_path="output.record_id"
        ),
        _blocker_contingent_criterion(),
    )
    ctx.last_code_authoring_repair_context = wu._metadata_output_repair_context(
        block_labels=["extract_record"],
        required_paths={"output.record_id"},
        coverage_reason_code="metadata_contract_missing",
        source="metadata_reject",
        summary="missing contract",
        declaration_paths={"output.blocker"},
    )

    uncovered = enforcement.uncovered_requested_output_paths(ctx)

    assert "output.blocker" not in uncovered
    assert "output.record_id" in uncovered


def test_static_return_imposition_renders_declaration_as_none_literal() -> None:
    code = 'record_id = await page.inner_text("#record")'

    keyed, violations = wu._extraction_code_with_required_static_return(
        code,
        required_paths={"output.record_id"},
        declaration_paths={"output.blocker"},
    )

    assert violations == []
    assert keyed.endswith('return {"output": {"blocker": None, "record_id": record_id}}')
    produced = wu._code_block_produced_output_paths(keyed)
    assert {"output.record_id", "output.blocker"} <= produced


def test_scaffolded_metadata_goal_rows_exclude_declaration_paths() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_record", outcome="The returned record includes record id.", output_path="output.record_id"
        ),
        _blocker_contingent_criterion(),
    )
    workflow_yaml = (
        "title: Record lookup\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: extract_record\n"
        "    code: |\n"
        '      record_id = await page.inner_text("#record")\n'
        '      return {"output": {"record_id": record_id, "blocker": None}}\n'
    )

    scaffolded, applied = wu._scaffold_metadata_contract_for_update(ctx, workflow_yaml, [])

    assert applied is True
    row = scaffolded[0]["claimed_outcomes"][0]
    assert row["goal_value_paths"] == ["output.record_id"]
    schema = json.loads(row["extraction_schema"])
    assert schema["properties"]["output"]["properties"]["blocker"] == {"type": ["string", "null"]}
    assert set(schema["properties"]["output"]["required"]) == {"record_id", "blocker"}


def test_mint_degraded_output_path_leaves_observation_lane() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_degraded",
            outcome="The returned record includes record id.",
            output_path="output.record_id",
            contingent_on="the lookup is blocked",
            mint_degrade="contingent_missing_antecedent",
        )
    )

    assert wu._requested_output_child_paths(ctx) == set()
    assert wu._output_contract_required_paths_source(ctx).union == set()


def test_runtime_repair_contract_carries_declaration_lane_with_stable_signature() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_record", outcome="The returned record includes record id.", output_path="output.record_id"
        ),
        _blocker_contingent_criterion(),
    )
    pre_run = wu._output_contract_required_paths_source(ctx)
    pre_signature = wu._stable_output_contract_key(wu._output_contract_scope_key(ctx), pre_run.union)
    ctx.latest_recorded_build_test_outcome = RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="repairable_failure",
        reason_code="outcome_not_demonstrated",
        workflow_run_id="wr_run_1",
        runtime_output_repair_facts=[
            {"workflow_run_id": "wr_run_1", "output_path": "output.record_id", "block_label": "extract_record"}
        ],
    )

    post_run = wu._output_contract_required_paths_source(ctx)

    assert post_run.source == "runtime_output_repair"
    assert post_run.observation_paths == {"output.record_id"}
    assert post_run.declaration_paths == {"output.blocker"}
    assert wu._stable_output_contract_key(wu._output_contract_scope_key(ctx), post_run.union) == pre_signature


def _declaration_waiver_yaml(code_body: str) -> str:
    indented = "\n".join(f"      {line}" for line in code_body.splitlines())
    return (
        "title: Record lookup\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: extract_record\n"
        "    code: |\n"
        f"{indented}\n"
    )


def _declaration_waiver_metadata(union: set[str]) -> list[dict[str, object]]:
    return [
        wu._metadata_contract_template(
            block_label="extract_record",
            required_paths=union,
            source="requested_output_contract",
            reason_code="requested_output_contract_missing_output_coverage",
            declaration_paths={"output.blocker"},
        )
    ]


def test_typed_advisory_grant_never_waives_declaration_return_miss() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_record", outcome="The returned record includes record id.", output_path="output.record_id"
        ),
        _blocker_contingent_criterion(),
    )
    union = {"output.record_id", "output.blocker"}
    signature = wu._stable_output_contract_key(wu._output_contract_scope_key(ctx), union)
    wu._grant_output_contract_advisory_run(ctx, signature)
    workflow_yaml = _declaration_waiver_yaml("result = await collect()\nreturn result")

    evaluation = wu._evaluate_output_contract_for_code_block(
        ctx, workflow_yaml, _declaration_waiver_metadata(union), allow_static_return_advisory=True
    )

    assert evaluation is not None
    assert evaluation.payload["actuated_static_return_advisory"] is True
    assert evaluation.payload["static_return_advisory_paths"] == ["output.record_id"]
    assert evaluation.missing_return_paths == ["output.blocker"]
    assert evaluation.can_attempt_run is False


def test_stamped_declaration_with_advisory_accepts_run() -> None:
    ctx = _antecedent_ctx(
        CompletionCriterion(
            id="c_record", outcome="The returned record includes record id.", output_path="output.record_id"
        ),
        _blocker_contingent_criterion(),
    )
    union = {"output.record_id", "output.blocker"}
    signature = wu._stable_output_contract_key(wu._output_contract_scope_key(ctx), union)
    wu._grant_output_contract_advisory_run(ctx, signature)
    workflow_yaml = _declaration_waiver_yaml('await page.click("#submit")\nreturn {"output": {"blocker": None}}')

    evaluation = wu._evaluate_output_contract_for_code_block(
        ctx, workflow_yaml, _declaration_waiver_metadata(union), allow_static_return_advisory=True
    )

    assert evaluation is not None
    assert evaluation.missing_return_paths == []
    assert evaluation.payload["static_return_advisory_paths"] == ["output.record_id"]
    assert evaluation.can_attempt_run is True


def test_merge_declaration_children_into_empty_literal_return() -> None:
    merged = wu._merge_declaration_children_into_literal_returns("return {}", {"output.blocker"})

    assert merged == 'return {"output": {"blocker": None}}'


def test_merge_declaration_children_into_existing_output_literal() -> None:
    code = 'value = await page.inner_text("#r")\nreturn {"output": {"record_id": value}}'

    merged = wu._merge_declaration_children_into_literal_returns(code, {"output.blocker"})

    assert merged.endswith('return {"output": {"blocker": None, "record_id": value}}')
    assert {"output.blocker", "output.record_id"} <= wu._code_block_produced_output_paths(merged)


def test_code_with_declared_contract_defaults_appends_return_when_abstained() -> None:
    stamped = wu._code_with_declared_contract_defaults('await page.click("#submit")', {"output.blocker"})

    assert stamped.endswith('return {"output": {"blocker": None}}')


def test_code_with_declared_contract_defaults_is_idempotent() -> None:
    stamped = wu._code_with_declared_contract_defaults('await page.click("#submit")', {"output.blocker"})

    assert wu._code_with_declared_contract_defaults(stamped, {"output.blocker"}) == ""


def test_code_with_declared_contract_defaults_skips_dynamic_return() -> None:
    assert wu._code_with_declared_contract_defaults("result = await collect()\nreturn result", {"output.blocker"}) == ""


def test_stamped_declaration_keeps_click_only_spine_classification() -> None:
    code = 'await page.click("#submit")\nreturn {"output": {"blocker": None}}'

    assert wu._output_contract_click_only_spine(code, {"output.blocker"}) is True
    assert wu._output_contract_click_only_spine(code) is False


def test_observation_production_is_not_click_only_spine() -> None:
    code = 'await page.click("#submit")\nreturn {"output": {"record_id": "x"}}'

    assert wu._output_contract_click_only_spine(code, {"output.blocker"}) is False
