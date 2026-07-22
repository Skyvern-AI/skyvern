"""Typed terminal halt contract for Copilot turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.forge.sdk.copilot.blocker_signal import (
    DEFINITION_CONTRACT_UNSATISFIED_REASON_CODE,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    GENUINELY_TERMINAL_BLOCKER_REASON_CODES as GENUINELY_TERMINAL_BLOCKER_REASON_CODES,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    OUTPUT_CONTRACT_REJECT_BUDGET_EXHAUSTED_REASON_CODE,
    CopilotToolBlockerSignal,
    blocker_signal_is_genuinely_terminal,
    build_output_source_unobservable_blocker_signal,
    clear_tool_blocker_signals_for_reason_codes,
)
from skyvern.forge.sdk.copilot.blocker_signal import to_trace_data as blocker_signal_to_trace_data
from skyvern.forge.sdk.copilot.failure_tracking import ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
from skyvern.forge.sdk.copilot.output_contracts import (
    OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
    OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
    OutputContractAdvisoryState,
)
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODE
from skyvern.forge.sdk.copilot.runtime import output_contract_ladder_unresolved
from skyvern.forge.sdk.copilot.schema_incompatibility import SCHEMA_INCOMPATIBILITY_REASON_CODE
from skyvern.forge.sdk.copilot.turn_ownership import (
    TURN_HALT_RETIRED_LOG_EVENT,
    TurnClaimant,
    claim_and_stash_blocker_signal,
    claim_turn,
    claimant_outranks,
    current_turn_owner,
    effective_signal_claimant,
    record_gate_precedence_conflict,
    release_turn_claim,
    turn_halt_is_genuinely_terminal,
)

LOG = structlog.get_logger()

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.runtime import AgentContext

REPAIR_CEILING_REASON_CODE = "repair_ceiling_reached"
ADVISORY_DISPATCH_STALLED_REASON_CODE = "advisory_dispatch_stalled"


class TurnHaltKind(StrEnum):
    LOOP_DETECTED = "loop_detected"
    ACTIVE_TERMINAL_CHALLENGE = "active_terminal_challenge"
    PROBABLE_SITE_BLOCK = "probable_site_block"
    REPAIR_CEILING_REACHED = "repair_ceiling_reached"
    SCHEMA_INCOMPATIBILITY = "schema_incompatibility"
    OUTPUT_SOURCE_UNOBSERVABLE = "output_source_unobservable"
    DELIVERED_UNVERIFIED = "delivered_unverified"
    DEFINITION_CONTRACT_UNSATISFIED = "definition_contract_unsatisfied"


class TurnHaltVerdict(StrEnum):
    BLOCKED = "blocked"
    DELIVERED_UNVERIFIED = "delivered_unverified"


_LOOP_TERMINAL_REASON_CODES = frozenset(
    {
        "loop_detected_credential_or_parameter_misconfig",
        "loop_detected_repeated_failed_step",
        "loop_detected_consecutive_same_tool",
        "loop_detected_generic",
        "code_authoring_guardrail_churn",
        "credential_priority_authoring_churn",
        "loop_detected_no_forward_progress_interaction",
    }
)
_ACTIVE_TERMINAL_CHALLENGE_REASON_CODES = frozenset(
    {
        ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
        TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        # Back-compat sentinel for pre-TERMINAL_CHALLENGE_BLOCKER traces.
        "tool_error_run_output_terminal_blocker",
        "tool_error_post_budget_challenge_blocker",
        "tool_error_challenge_gated_submit_disabled",
    }
)
_PROBABLE_SITE_BLOCK_REASON_CODES = frozenset({"probable_site_block_stop"})
_SCHEMA_INCOMPATIBILITY_REASON_CODES = frozenset({SCHEMA_INCOMPATIBILITY_REASON_CODE})
_OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODES = frozenset(
    {
        OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODE,
        OUTPUT_CONTRACT_ACTUATION_EXHAUSTED_REASON_CODE,
        OUTPUT_CONTRACT_REJECT_BUDGET_EXHAUSTED_REASON_CODE,
        ADVISORY_DISPATCH_STALLED_REASON_CODE,
    }
)
_DEFINITION_CONTRACT_UNSATISFIED_REASON_CODES = frozenset({DEFINITION_CONTRACT_UNSATISFIED_REASON_CODE})

# Halts the agent did not choose: a verified outcome may suppress these.
# ACTIVE_TERMINAL_CHALLENGE is voluntary and is deliberately excluded so a
# future terminal kind defaults to raising rather than being suppressed.
_INVOLUNTARY_TURN_HALT_KINDS = frozenset(
    {
        TurnHaltKind.LOOP_DETECTED,
        TurnHaltKind.PROBABLE_SITE_BLOCK,
        TurnHaltKind.REPAIR_CEILING_REACHED,
        TurnHaltKind.SCHEMA_INCOMPATIBILITY,
        TurnHaltKind.OUTPUT_SOURCE_UNOBSERVABLE,
        TurnHaltKind.DEFINITION_CONTRACT_UNSATISFIED,
    }
)
_INVOLUNTARY_BLOCKER_REASON_CODES = (
    _LOOP_TERMINAL_REASON_CODES
    | _PROBABLE_SITE_BLOCK_REASON_CODES
    | _SCHEMA_INCOMPATIBILITY_REASON_CODES
    | _OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODES
    | _DEFINITION_CONTRACT_UNSATISFIED_REASON_CODES
    | frozenset({REPAIR_CEILING_REASON_CODE})
)
_VERIFIED_SUPPRESSIBLE_ACTIVE_TERMINAL_REASON_CODES = frozenset({ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE})
_VERIFIED_SUPPRESSIBLE_ACTIVE_TERMINAL_SOURCES = frozenset({"run_execution"})


@dataclass(frozen=True)
class TurnHalt:
    kind: TurnHaltKind
    verdict: TurnHaltVerdict = TurnHaltVerdict.BLOCKED
    blocker_signal: CopilotToolBlockerSignal | None = None
    draft_state: dict[str, Any] = field(default_factory=dict)
    run_refs: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class CopilotTurnHalt(Exception):
    def __init__(self, halt: TurnHalt) -> None:
        self.halt = halt
        super().__init__(f"Copilot turn halted: {halt.kind.value}")


def _kind_for_blocker_signal(signal: CopilotToolBlockerSignal) -> TurnHaltKind | None:
    reason = signal.internal_reason_code
    if signal.blocker_kind == "loop_detected" and reason in _LOOP_TERMINAL_REASON_CODES:
        return TurnHaltKind.LOOP_DETECTED
    if reason in _ACTIVE_TERMINAL_CHALLENGE_REASON_CODES:
        return TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    if reason in _PROBABLE_SITE_BLOCK_REASON_CODES:
        return TurnHaltKind.PROBABLE_SITE_BLOCK
    if reason in _SCHEMA_INCOMPATIBILITY_REASON_CODES:
        return TurnHaltKind.SCHEMA_INCOMPATIBILITY
    if reason in _OUTPUT_SOURCE_UNOBSERVABLE_REASON_CODES:
        return TurnHaltKind.OUTPUT_SOURCE_UNOBSERVABLE
    if reason in _DEFINITION_CONTRACT_UNSATISFIED_REASON_CODES:
        return TurnHaltKind.DEFINITION_CONTRACT_UNSATISFIED
    return None


def turn_halt_from_blocker_signal(signal: object, *, source: str) -> TurnHalt | None:
    if not isinstance(signal, CopilotToolBlockerSignal):
        return None
    if not signal.renders_final_reply:
        return None
    kind = _kind_for_blocker_signal(signal)
    if kind is None:
        return None
    return TurnHalt(
        kind=kind,
        blocker_signal=signal,
        draft_state={"preserves_workflow_draft": signal.preserves_workflow_draft},
        extra={**signal.extra, "source": source},
    )


_ADVISORY_STATE_PROGRESS_ORDINAL = {
    OutputContractAdvisoryState.UNUSED: 0,
    OutputContractAdvisoryState.EXPIRED: 0,
    OutputContractAdvisoryState.GRANTED: 1,
    OutputContractAdvisoryState.CONSUMED: 2,
}


def _output_contract_defer_progress_token(ctx: Any) -> tuple[int, int, int, int]:
    states = getattr(ctx, "output_contract_actuation_by_signature", {}) or {}
    counts = getattr(ctx, "output_contract_actuation_count_by_signature", {}) or {}
    observed = getattr(ctx, "output_contract_run_output_observed_by_signature", {}) or {}
    imposed = getattr(ctx, "output_contract_page_extraction_imposed_by_signature", {}) or {}
    return (
        sum(_ADVISORY_STATE_PROGRESS_ORDINAL.get(state, 0) for state in states.values()),
        sum(int(count or 0) for count in counts.values()),
        sum(1 for value in observed.values() if value),
        sum(1 for value in imposed.values() if value),
    )


def _expire_unresolved_output_contract_signatures(ctx: AgentContext, *, include_granted: bool) -> list[str]:
    states = ctx.output_contract_actuation_by_signature
    counts = ctx.output_contract_actuation_count_by_signature
    resolved = {OutputContractAdvisoryState.CONSUMED, OutputContractAdvisoryState.EXPIRED}
    expired: list[str] = []
    if include_granted:
        for signature, state in states.items():
            if state == OutputContractAdvisoryState.GRANTED:
                states[signature] = OutputContractAdvisoryState.EXPIRED
                expired.append(signature)
    for signature in set(states) | set(counts):
        if int(counts.get(signature, 0) or 0) >= 1 and states.get(signature) not in resolved:
            states[signature] = OutputContractAdvisoryState.EXPIRED
            expired.append(signature)
    return expired


def _emit_advisory_dispatch_stalled_terminal(ctx: AgentContext, expired: list[str], *, source: str) -> TurnHalt | None:
    required_paths = sorted({path for paths in ctx.output_contract_pending_run_evidence.values() for path in paths})
    signal = build_output_source_unobservable_blocker_signal(
        reason_code=ADVISORY_DISPATCH_STALLED_REASON_CODE,
        required_paths=required_paths,
        block_label="",
    )
    claim_and_stash_blocker_signal(ctx, TurnClaimant.GENUINELY_TERMINAL, signal)
    halt = turn_halt_from_blocker_signal(signal, source=source)
    if halt is None:
        return None
    ctx.turn_halt = halt
    LOG.info("copilot_output_contract_advisory_dispatch_stalled", canonical_output_contract_signatures=expired)
    return halt


def _expire_stalled_output_contract_ladder(ctx: AgentContext) -> None:
    if ctx.turn_halt is not None:
        return
    states = ctx.output_contract_actuation_by_signature
    # A GRANTED grant is awaiting its forced run dispatch, not stalled: the dispatch lane consumes it
    # next iteration, so expiring it here would false-fire arm-D on a source-producible shape and re-arm
    # the loop guards before the granted run runs. Only a directive ladder with no live grant expires here.
    if any(state == OutputContractAdvisoryState.GRANTED for state in states.values()):
        return
    expired = _expire_unresolved_output_contract_signatures(ctx, include_granted=False)
    if not expired:
        return
    _emit_advisory_dispatch_stalled_terminal(ctx, expired, source="turn_halt_defer_expiry")


def expire_output_contract_ladder_at_turn_end(
    ctx: AgentContext, *, emit_stalled_terminal: bool = True
) -> TurnHalt | None:
    """Turn-end obligation: every GRANTED advisory signature expires here, and when no other halt
    ended the turn the existing advisory-dispatch-stalled terminal is emitted for the silent grant."""
    states = ctx.output_contract_actuation_by_signature
    if not any(state == OutputContractAdvisoryState.GRANTED for state in states.values()):
        return None
    expired = _expire_unresolved_output_contract_signatures(ctx, include_granted=True)
    existing = ctx.turn_halt
    if existing is not None or not emit_stalled_terminal:
        LOG.info(
            "copilot_output_contract_advisory_turn_end_reconcile_noop",
            canonical_output_contract_signatures=expired,
            existing_turn_halt_kind=existing.kind.value if existing is not None else None,
        )
        return None
    return _emit_advisory_dispatch_stalled_terminal(ctx, expired, source="turn_end_ownership_reconcile")


def _defer_loop_detected_while_output_contract_ladder_unresolved(ctx: Any, signal: object) -> bool:
    """The single choke-point every loop_detected emitter (enforcement backstop, hook re-raise, tool-loop
    guards, MCP) flows through: while a typed output-contract actuation ladder is live the bounded ladder
    owns the turn, so a loop/churn stop is not promoted, while genuinely-terminal and non-loop signals are.
    The defer carries its own termination proof: each swallow must show a lifecycle-progress advance since
    the last one. A GRANTED grant is owned by the forced run dispatch and is never stall-expired here; only a
    directive ladder with no live grant expires on a second swallow with no advance, so it cannot ride to the
    timeout wall."""
    if not isinstance(signal, CopilotToolBlockerSignal):
        return False
    if blocker_signal_is_genuinely_terminal(signal):
        return False
    if _kind_for_blocker_signal(signal) != TurnHaltKind.LOOP_DETECTED:
        return False
    if not hasattr(ctx, "output_contract_actuation_by_signature"):
        return False
    if not output_contract_ladder_unresolved(ctx):
        return False
    token = _output_contract_defer_progress_token(ctx)
    last_token = getattr(ctx, "output_contract_defer_progress_token", None)
    if last_token is not None and token == last_token:
        _expire_stalled_output_contract_ladder(ctx)
        return True
    ctx.output_contract_defer_progress_token = token
    return True


def stash_turn_halt_from_blocker_signal(ctx: Any, signal: object, *, source: str) -> TurnHalt | None:
    existing = getattr(ctx, "turn_halt", None)
    if isinstance(existing, TurnHalt):
        return existing
    if _defer_loop_detected_while_output_contract_ladder_unresolved(ctx, signal):
        return None
    halt = turn_halt_from_blocker_signal(signal, source=source)
    if halt is None:
        return None
    ctx.turn_halt = halt
    if blocker_signal_is_genuinely_terminal(halt.blocker_signal):
        claim_turn(ctx, TurnClaimant.GENUINELY_TERMINAL, renders_final_reply=True)
    LOG.info("copilot turn halt stashed", **turn_halt_to_trace_data(halt))
    return halt


def stash_repair_ceiling_turn_halt(
    ctx: Any,
    signal: CopilotToolBlockerSignal,
    *,
    consecutive_identical_repair_count: int,
) -> TurnHalt | None:
    existing = getattr(ctx, "turn_halt", None)
    if isinstance(existing, TurnHalt):
        return existing
    halt = TurnHalt(
        kind=TurnHaltKind.REPAIR_CEILING_REACHED,
        blocker_signal=signal,
        draft_state={"preserves_workflow_draft": signal.preserves_workflow_draft},
        extra={
            "source": "enforcement",
            "consecutive_identical_repair_count": consecutive_identical_repair_count,
        },
    )
    ctx.turn_halt = halt
    claim_turn(ctx, TurnClaimant.GENUINELY_TERMINAL, renders_final_reply=True)
    LOG.info("copilot turn halt stashed", **turn_halt_to_trace_data(halt))
    return halt


def stash_delivered_unverified_turn_halt(ctx: AgentContext, *, workflow_run_id: str | None) -> TurnHalt | None:
    if isinstance(ctx.turn_halt, TurnHalt):
        return ctx.turn_halt
    run_refs = {"workflow_run_id": workflow_run_id} if workflow_run_id else {}
    halt = TurnHalt(
        kind=TurnHaltKind.DELIVERED_UNVERIFIED,
        verdict=TurnHaltVerdict.DELIVERED_UNVERIFIED,
        run_refs=run_refs,
        extra={"source": "run_execution"},
    )
    ctx.turn_halt = halt
    claim_turn(ctx, TurnClaimant.GENUINELY_TERMINAL, renders_final_reply=True)
    LOG.info("copilot turn halt stashed", **turn_halt_to_trace_data(halt))
    return halt


def _restored_owner_halt(ctx: AgentContext, owner_claimant: TurnClaimant) -> TurnHalt | None:
    held = ctx.blocker_signal
    if held is None or effective_signal_claimant(ctx, held) is not owner_claimant:
        return None
    return turn_halt_from_blocker_signal(held, source="precedence_owner_restored")


def retire_outranked_turn_halt(ctx: AgentContext) -> bool:
    """Storage follows ownership at the halt's consumption points: a non-terminal halt outranked by
    the live owner is retired (conflict recorded) and the owner's halt is restored by re-emission
    from the held signal, never resurrected from the retired object."""
    halt = ctx.turn_halt
    if halt is None or turn_halt_is_genuinely_terminal(halt):
        return False
    owner = current_turn_owner(ctx)
    if owner is None:
        return False
    signal = halt.blocker_signal
    halt_claimant = effective_signal_claimant(ctx, signal) if signal is not None else None
    if halt_claimant is None or halt_claimant is owner.claimant or not claimant_outranks(owner.claimant, halt_claimant):
        return False
    record_gate_precedence_conflict(
        ctx,
        owner=owner.claimant.value,
        yielded=halt_claimant.value,
        owner_renders_final_reply=owner.renders_final_reply,
        yielded_renders_final_reply=bool(signal is not None and signal.renders_final_reply),
        site="turn_halt",
    )
    restored = _restored_owner_halt(ctx, owner.claimant)
    ctx.turn_halt = restored
    LOG.info(
        TURN_HALT_RETIRED_LOG_EVENT,
        owner=owner.claimant.value,
        retired=halt_claimant.value,
        retired_reason_code=signal.internal_reason_code if signal is not None else None,
        restored_turn_halt_kind=restored.kind.value if restored is not None else None,
    )
    return True


def raise_if_turn_halt(ctx: Any, *, verified: bool = False) -> None:
    """Raise the stashed turn halt unless a verified outcome suppresses it.

    A judge-confirmed outcome suppresses an involuntary halt and consumes both
    ``ctx.turn_halt`` and the matching involuntary ``ctx.blocker_signal``;
    ``verified`` defaults False so an un-updated caller raises rather than
    falsely suppressing.
    """
    halt = getattr(ctx, "turn_halt", None)
    if not isinstance(halt, TurnHalt):
        return
    if retire_outranked_turn_halt(ctx):
        halt = getattr(ctx, "turn_halt", None)
        if not isinstance(halt, TurnHalt):
            return
    suppressible_reason_codes = _INVOLUNTARY_BLOCKER_REASON_CODES
    if (
        halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
        and halt.blocker_signal is not None
        and halt.blocker_signal.internal_reason_code in _VERIFIED_SUPPRESSIBLE_ACTIVE_TERMINAL_REASON_CODES
        and halt.extra.get("source") in _VERIFIED_SUPPRESSIBLE_ACTIVE_TERMINAL_SOURCES
    ):
        suppressible_reason_codes = (
            _INVOLUNTARY_BLOCKER_REASON_CODES | _VERIFIED_SUPPRESSIBLE_ACTIVE_TERMINAL_REASON_CODES
        )
    elif halt.kind not in _INVOLUNTARY_TURN_HALT_KINDS:
        suppressible_reason_codes = frozenset()
    if verified and suppressible_reason_codes:
        ctx.turn_halt = None
        clear_tool_blocker_signals_for_reason_codes(ctx, suppressible_reason_codes)
        release_turn_claim(ctx, TurnClaimant.GENUINELY_TERMINAL)
        LOG.info(
            "copilot turn halt suppressed by verified outcome",
            **turn_halt_to_trace_data(halt),
        )
        return
    raise CopilotTurnHalt(halt)


def turn_halt_to_trace_data(halt: TurnHalt) -> dict[str, Any]:
    data: dict[str, Any] = {
        "turn_halt_kind": halt.kind.value,
        "turn_halt_verdict": halt.verdict.value,
        "turn_halt_extra": dict(halt.extra),
    }
    if halt.blocker_signal is not None:
        data.update(blocker_signal_to_trace_data(halt.blocker_signal))
    return data
