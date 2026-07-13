from __future__ import annotations

from skyvern.forge.sdk.copilot.blocker_signal import (
    GENUINELY_TERMINAL_BLOCKER_REASON_CODES,
    SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE,
    CopilotToolBlockerSignal,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.turn_halt import (
    retire_outranked_turn_halt,
    stash_turn_halt_from_blocker_signal,
)
from skyvern.forge.sdk.copilot.turn_ownership import (
    _PRECEDENCE_ORDER,
    CLAIMANT_REASON_CODE_FAMILIES,
    ClaimOutcome,
    TurnClaimant,
    blocker_signal_render_allowed,
    claim_and_stash_blocker_signal,
    claim_turn,
    claimant_for_blocker_signal,
    current_turn_owner,
    emit_blocker_signal_payload,
    release_turn_claim,
)
from tests.unit.conftest import make_copilot_context


def _signal(
    reason: str,
    *,
    blocker_kind: str = "loop_detected",
    renders_final_reply: bool = True,
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind=blocker_kind,  # type: ignore[arg-type]
        agent_steering_text="steering",
        user_facing_reason="I could not continue this step.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        renders_final_reply=renders_final_reply,
        internal_reason_code=reason,
        blocked_tool="update_workflow",
    )


def _churn_signal() -> CopilotToolBlockerSignal:
    return _signal("code_authoring_guardrail_churn")


def _grant_ladder(ctx: CopilotContext) -> None:
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED


def _resolve_ladder(ctx: CopilotContext) -> None:
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.CONSUMED
    ctx.output_contract_actuation_count_by_signature.clear()


def _conflict_fingerprints(ctx: CopilotContext) -> list[str]:
    return [event.fingerprint for event in ctx.gate_precedence_conflict_events]


def test_two_claimants_one_owner_loser_recorded() -> None:
    ctx = make_copilot_context()
    signal = _churn_signal()
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, signal) is not None
    assert ctx.blocker_signal is signal

    loop_signal = _signal("loop_detected_generic")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.LOOP_DETECTED, loop_signal) is None

    owner = current_turn_owner(ctx)
    assert owner is not None
    assert owner.claimant is TurnClaimant.CODE_AUTHORING_CHURN
    assert ctx.blocker_signal is signal
    assert _conflict_fingerprints(ctx) == ["code_authoring_guardrail_churn>loop_detected"]


def test_r31_three_way_contradiction_yields_single_owner() -> None:
    ctx = make_copilot_context()
    _grant_ladder(ctx)
    claim_turn(ctx, TurnClaimant.OUTPUT_CONTRACT_ACTUATION)

    rescout = claim_turn(ctx, TurnClaimant.UNCOVERED_OUTPUT_RESCOUT_STEER)
    persistence_payload = claim_and_stash_blocker_signal(
        ctx,
        TurnClaimant.SYNTHESIZED_BLOCK_PERSISTENCE_FORCE,
        _signal(SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE, blocker_kind="tool_error", renders_final_reply=False),
    )

    assert rescout is ClaimOutcome.YIELDED
    assert persistence_payload is None
    assert ctx.blocker_signal is None
    owner = current_turn_owner(ctx)
    assert owner is not None
    assert owner.claimant is TurnClaimant.OUTPUT_CONTRACT_ACTUATION
    assert sorted(_conflict_fingerprints(ctx)) == [
        "output_contract_actuation>synthesized_block_persistence_force",
        "output_contract_actuation>uncovered_output_rescout_steer",
    ]


def test_precedence_order_covers_every_claimant_once() -> None:
    assert len(set(_PRECEDENCE_ORDER)) == len(_PRECEDENCE_ORDER)
    assert set(_PRECEDENCE_ORDER) == set(TurnClaimant)
    assert set(CLAIMANT_REASON_CODE_FAMILIES) == set(TurnClaimant)


def test_rescout_outranks_grounding_and_persistence_per_cascade_order() -> None:
    ctx = make_copilot_context()
    assert claim_turn(ctx, TurnClaimant.RECORDED_OUTCOME_GROUNDING) is ClaimOutcome.OWNED
    assert claim_turn(ctx, TurnClaimant.UNCOVERED_OUTPUT_RESCOUT_STEER) is ClaimOutcome.OWNED

    ctx2 = make_copilot_context()
    assert claim_and_stash_blocker_signal(
        ctx2,
        TurnClaimant.SYNTHESIZED_BLOCK_PERSISTENCE_FORCE,
        _signal(SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE, blocker_kind="tool_error", renders_final_reply=False),
    )
    assert claim_turn(ctx2, TurnClaimant.UNCOVERED_OUTPUT_RESCOUT_STEER) is ClaimOutcome.OWNED


def test_grounding_outranks_persistence_per_nested_exception() -> None:
    ctx = make_copilot_context()
    persisted = claim_and_stash_blocker_signal(
        ctx,
        TurnClaimant.SYNTHESIZED_BLOCK_PERSISTENCE_FORCE,
        _signal(SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE, blocker_kind="tool_error", renders_final_reply=False),
    )
    assert persisted is not None
    grounding = claim_and_stash_blocker_signal(
        ctx,
        TurnClaimant.RECORDED_OUTCOME_GROUNDING,
        _signal(
            "recorded_outcome_grounding_required", blocker_kind="missing_required_context", renders_final_reply=False
        ),
    )
    assert grounding is not None
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "recorded_outcome_grounding_required"
    assert "recorded_outcome_grounding>synthesized_block_persistence_force" in _conflict_fingerprints(ctx)


def test_genuinely_terminal_family_is_the_shared_blocker_signal_set() -> None:
    assert CLAIMANT_REASON_CODE_FAMILIES[TurnClaimant.GENUINELY_TERMINAL] is GENUINELY_TERMINAL_BLOCKER_REASON_CODES


def test_claimant_table_resolves_signals_for_both_cascades() -> None:
    assert claimant_for_blocker_signal(_churn_signal()) is TurnClaimant.CODE_AUTHORING_CHURN
    assert (
        claimant_for_blocker_signal(_signal("credential_priority_authoring_churn"))
        is TurnClaimant.CREDENTIAL_PRIORITY_CHURN
    )
    assert claimant_for_blocker_signal(_signal("loop_detected_generic")) is TurnClaimant.LOOP_DETECTED
    assert (
        claimant_for_blocker_signal(_signal("probable_site_block_stop", blocker_kind="tool_error"))
        is TurnClaimant.GENUINELY_TERMINAL
    )
    assert claimant_for_blocker_signal(_signal("tool_error_repeated_action_abort", blocker_kind="tool_error")) is None


def test_same_claimant_reclaim_is_first_wins_and_widens_metadata() -> None:
    ctx = make_copilot_context()
    assert claim_turn(ctx, TurnClaimant.CODE_AUTHORING_CHURN) is ClaimOutcome.OWNED
    assert claim_turn(ctx, TurnClaimant.CODE_AUTHORING_CHURN, renders_final_reply=True) is ClaimOutcome.OWNED
    assert ctx.turn_ownership is not None
    claim = ctx.turn_ownership.claims[TurnClaimant.CODE_AUTHORING_CHURN]
    assert claim.renders_final_reply is True
    assert ctx.gate_precedence_conflict_events == []


def test_stale_owner_releases_when_ladder_resolves() -> None:
    ctx = make_copilot_context()
    _grant_ladder(ctx)
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, _churn_signal()) is None

    _resolve_ladder(ctx)
    assert current_turn_owner(ctx) is None
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, _churn_signal()) is not None
    owner = current_turn_owner(ctx)
    assert owner is not None
    assert owner.claimant is TurnClaimant.CODE_AUTHORING_CHURN


def test_stronger_owned_claim_replaces_held_signal() -> None:
    ctx = make_copilot_context()
    loop_signal = _signal("loop_detected_generic")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.LOOP_DETECTED, loop_signal) is not None
    assert ctx.blocker_signal is loop_signal

    terminal_signal = _signal("probable_site_block_stop", blocker_kind="tool_error")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.GENUINELY_TERMINAL, terminal_signal) is not None
    assert ctx.blocker_signal is terminal_signal


def test_genuinely_terminal_held_signal_is_never_replaced() -> None:
    ctx = make_copilot_context()
    terminal_signal = _signal("probable_site_block_stop", blocker_kind="tool_error")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.GENUINELY_TERMINAL, terminal_signal) is not None

    churn_payload = claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, _churn_signal())
    assert churn_payload is None
    assert ctx.blocker_signal is terminal_signal


def test_plain_stash_replacement_clears_stale_association() -> None:
    ctx = make_copilot_context()
    loop_signal = _signal("loop_detected_generic")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.LOOP_DETECTED, loop_signal) is not None
    assert ctx.blocker_signal_claimant is TurnClaimant.LOOP_DETECTED

    terminal_signal = _signal("output_source_unobservable", blocker_kind="tool_error")
    stash_blocker_signal(ctx, terminal_signal)

    assert ctx.blocker_signal is terminal_signal
    assert ctx.blocker_signal_claimant is None
    assert current_turn_owner(ctx) is None
    assert blocker_signal_render_allowed(ctx, terminal_signal) is True


def test_signal_only_stronger_claim_retires_outranked_halt_on_consult() -> None:
    ctx = make_copilot_context()
    loop_signal = _signal("loop_detected_generic")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.LOOP_DETECTED, loop_signal) is not None
    stash_turn_halt_from_blocker_signal(ctx, loop_signal, source="test")
    assert ctx.turn_halt is not None

    _grant_ladder(ctx)
    assert claim_turn(ctx, TurnClaimant.OUTPUT_CONTRACT_ACTUATION) is ClaimOutcome.OWNED
    assert retire_outranked_turn_halt(ctx) is True

    assert ctx.turn_halt is None
    assert ctx.blocker_signal is loop_signal
    assert any(
        event.site == "turn_halt" and event.fingerprint == "output_contract_actuation>loop_detected"
        for event in ctx.gate_precedence_conflict_events
    )


def test_retirement_restores_owner_halt_by_re_emission() -> None:
    ctx = make_copilot_context()
    terminal_signal = _signal("probable_site_block_stop", blocker_kind="tool_error")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.GENUINELY_TERMINAL, terminal_signal) is not None

    loop_signal = _signal("loop_detected_generic")
    loop_halt = stash_turn_halt_from_blocker_signal(ctx, loop_signal, source="test")
    assert loop_halt is not None
    assert ctx.turn_halt is loop_halt

    assert retire_outranked_turn_halt(ctx) is True
    restored = ctx.turn_halt
    assert restored is not None
    assert restored is not loop_halt
    assert restored.blocker_signal is terminal_signal


def test_genuinely_terminal_halt_is_never_retired() -> None:
    ctx = make_copilot_context()
    terminal_signal = _signal("probable_site_block_stop", blocker_kind="tool_error")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.GENUINELY_TERMINAL, terminal_signal) is not None
    stash_turn_halt_from_blocker_signal(ctx, terminal_signal, source="test")
    halt = ctx.turn_halt
    assert halt is not None

    _grant_ladder(ctx)
    claim_turn(ctx, TurnClaimant.OUTPUT_CONTRACT_ACTUATION)
    assert retire_outranked_turn_halt(ctx) is False
    assert ctx.turn_halt is halt


def test_render_denied_while_ladder_owns_and_restores_on_resolution() -> None:
    ctx = make_copilot_context()
    churn = _churn_signal()
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, churn) is not None
    assert blocker_signal_render_allowed(ctx, churn) is True

    _grant_ladder(ctx)
    assert blocker_signal_render_allowed(ctx, churn) is False
    assert any(
        event.site == "final_reply_render"
        and event.fingerprint == "output_contract_actuation>code_authoring_guardrail_churn"
        for event in ctx.gate_precedence_conflict_events
    )

    _resolve_ladder(ctx)
    assert blocker_signal_render_allowed(ctx, churn) is True


def test_association_survives_held_signal_refresh() -> None:
    ctx = make_copilot_context()
    churn = _churn_signal()
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, churn) is not None

    refreshed = churn.model_copy(update={"user_facing_reason": "I stopped rewriting the generated code."})
    ctx.blocker_signal = refreshed

    owner = current_turn_owner(ctx)
    assert owner is not None
    assert owner.claimant is TurnClaimant.CODE_AUTHORING_CHURN
    assert blocker_signal_render_allowed(ctx, refreshed) is True


def test_unclaimed_signal_with_no_live_owner_still_renders() -> None:
    ctx = make_copilot_context()
    signal = _signal("tool_error_repeated_action_abort", blocker_kind="tool_error")
    ctx.blocker_signal = signal
    assert blocker_signal_render_allowed(ctx, signal) is True


def test_unclaimed_signal_fails_open_to_render_while_owner_live() -> None:
    ctx = make_copilot_context()
    signal = _signal("tool_error_repeated_action_abort", blocker_kind="tool_error")
    ctx.blocker_signal = signal
    _grant_ladder(ctx)

    assert blocker_signal_render_allowed(ctx, signal) is True
    assert ctx.gate_precedence_conflict_events == []


def test_preflight_claim_never_outlives_the_rejecting_call() -> None:
    ctx = make_copilot_context()
    assert claim_turn(ctx, TurnClaimant.METADATA_RUN_PREFLIGHT_REJECT) is ClaimOutcome.OWNED
    assert current_turn_owner(ctx) is None

    churn = _churn_signal()
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, churn) is not None
    assert blocker_signal_render_allowed(ctx, churn) is True


def test_preflight_claim_yields_to_live_ladder_and_records_conflict() -> None:
    ctx = make_copilot_context()
    _grant_ladder(ctx)
    assert claim_turn(ctx, TurnClaimant.METADATA_RUN_PREFLIGHT_REJECT) is ClaimOutcome.YIELDED
    assert _conflict_fingerprints(ctx) == ["output_contract_actuation>metadata_run_preflight_reject"]


def test_carve_out_claimants_are_transient() -> None:
    ctx = make_copilot_context()
    assert claim_turn(ctx, TurnClaimant.ACTUATION_OBLIGATION_FILL) is ClaimOutcome.OWNED
    assert claim_turn(ctx, TurnClaimant.CREDENTIAL_SCOUT_REOPEN) is ClaimOutcome.OWNED
    assert current_turn_owner(ctx) is None


def test_credential_priority_churn_yields_only_to_ladder_and_terminal() -> None:
    ctx = make_copilot_context()
    _grant_ladder(ctx)
    signal = _signal("credential_priority_authoring_churn")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.CREDENTIAL_PRIORITY_CHURN, signal) is None

    ctx2 = make_copilot_context()
    assert claim_and_stash_blocker_signal(
        ctx2,
        TurnClaimant.SYNTHESIZED_BLOCK_PERSISTENCE_FORCE,
        _signal(SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE, blocker_kind="tool_error", renders_final_reply=False),
    )
    credential_signal = _signal("credential_priority_authoring_churn")
    assert claim_and_stash_blocker_signal(ctx2, TurnClaimant.CREDENTIAL_PRIORITY_CHURN, credential_signal) is not None
    assert ctx2.blocker_signal is credential_signal
    assert blocker_signal_render_allowed(ctx2, credential_signal) is True


def test_emit_blocker_signal_payload_returns_payload_even_on_yield() -> None:
    ctx = make_copilot_context()
    _grant_ladder(ctx)
    loop_signal = _signal("loop_detected_generic")

    payload = emit_blocker_signal_payload(ctx, loop_signal)

    assert payload == loop_signal.agent_steering_text
    assert ctx.blocker_signal is None
    assert any(
        event.fingerprint == "output_contract_actuation>loop_detected" for event in ctx.gate_precedence_conflict_events
    )


def test_dispatch_force_reclaim_of_live_ladder_is_owned() -> None:
    ctx = make_copilot_context()
    _grant_ladder(ctx)
    claim_turn(ctx, TurnClaimant.OUTPUT_CONTRACT_ACTUATION)
    assert claim_turn(ctx, TurnClaimant.OUTPUT_CONTRACT_ACTUATION) is ClaimOutcome.OWNED
    assert ctx.gate_precedence_conflict_events == []


def test_state_backed_ladder_owns_without_explicit_claim_and_query_does_not_register() -> None:
    ctx = make_copilot_context()
    _grant_ladder(ctx)
    owner = current_turn_owner(ctx)
    assert owner is not None
    assert owner.claimant is TurnClaimant.OUTPUT_CONTRACT_ACTUATION
    assert ctx.turn_ownership is None


def test_release_turn_claim_removes_registered_claim() -> None:
    ctx = make_copilot_context()
    terminal_signal = _signal("probable_site_block_stop", blocker_kind="tool_error")
    assert claim_and_stash_blocker_signal(ctx, TurnClaimant.GENUINELY_TERMINAL, terminal_signal) is not None
    assert ctx.turn_ownership is not None
    assert TurnClaimant.GENUINELY_TERMINAL in ctx.turn_ownership.claims

    release_turn_claim(ctx, TurnClaimant.GENUINELY_TERMINAL)
    assert TurnClaimant.GENUINELY_TERMINAL not in ctx.turn_ownership.claims


def test_conflict_events_are_capped() -> None:
    ctx = make_copilot_context()
    _grant_ladder(ctx)
    for _ in range(30):
        claim_turn(ctx, TurnClaimant.LOOP_DETECTED)
    assert len(ctx.gate_precedence_conflict_events) == 20


def test_fresh_context_has_no_owner() -> None:
    ctx = make_copilot_context()
    assert current_turn_owner(ctx) is None
    assert ctx.gate_precedence_conflict_events == []
