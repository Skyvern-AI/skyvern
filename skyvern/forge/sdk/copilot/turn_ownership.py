"""Single-owner turn-precedence contract: one mechanism owns a Copilot turn's steering at a time,
computed as the strongest live claim; weaker claims yield and are recorded as gate-precedence
conflicts, and storage (held blocker signal and turn halt) follows ownership."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from skyvern.forge.sdk.copilot.blocker_signal import (
    GENUINELY_TERMINAL_BLOCKER_REASON_CODES,
    RECORDED_OUTCOME_GROUNDING_REASON_CODE,
    SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE,
    UNCOVERED_OUTPUT_RESCOUT_STEER_REASON_CODE,
    CopilotToolBlockerSignal,
    blocker_signal_is_genuinely_terminal,
    build_llm_tool_error_payload,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext, output_contract_ladder_unresolved

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.turn_halt import TurnHalt

LOG = structlog.get_logger()

GATE_PRECEDENCE_CONFLICT_LOG_EVENT = "copilot_gate_precedence_conflict"
TURN_HALT_RETIRED_LOG_EVENT = "copilot_turn_halt_retired_by_precedence_owner"
BLOCKER_SIGNAL_REPLACED_LOG_EVENT = "copilot_blocker_signal_replaced_by_precedence_owner"

_MAX_CONFLICT_EVENTS = 20


class TurnClaimant(StrEnum):
    GENUINELY_TERMINAL = "genuinely_terminal"
    OUTPUT_CONTRACT_ACTUATION = "output_contract_actuation"
    CREDENTIAL_PRIORITY_CHURN = "credential_priority_authoring_churn"
    METADATA_RUN_PREFLIGHT_REJECT = "metadata_run_preflight_reject"
    ACTUATION_OBLIGATION_FILL = "actuation_obligation_fill"
    CREDENTIAL_SCOUT_REOPEN = "credential_scout_reopen"
    UNCOVERED_OUTPUT_RESCOUT_STEER = "uncovered_output_rescout_steer"
    RECORDED_OUTCOME_GROUNDING = "recorded_outcome_grounding"
    SYNTHESIZED_BLOCK_PERSISTENCE_FORCE = "synthesized_block_persistence_force"
    CODE_AUTHORING_CHURN = "code_authoring_guardrail_churn"
    LOOP_DETECTED = "loop_detected"


class ClaimOutcome(StrEnum):
    OWNED = "owned"
    YIELDED = "yielded"


# Strongest first. Each row's position derives from an observed cascade order, a ratified
# carve-out, or the ladder defer; pairs that never co-occur at a seam are newly defined.
_PRECEDENCE_ORDER: tuple[TurnClaimant, ...] = (
    # Genuinely-terminal blockers keep both the rendered reply and the halt kind: the loop-defer
    # choke-point excludes them and the churn floor defers to a held terminal signal.
    TurnClaimant.GENUINELY_TERMINAL,
    # Ladder-over-loop defer at the turn-halt choke-point; ladder-over-rescout; the inline churn
    # defer keyed on output_contract_ladder_unresolved.
    TurnClaimant.OUTPUT_CONTRACT_ACTUATION,
    # Bound-8 credential-priority churn defers only to terminal evidence and the actuation ladder,
    # preserving the credential-scout reply.
    TurnClaimant.CREDENTIAL_PRIORITY_CHURN,
    # Author-time preflight reject steers before any churn or loop floor accumulates.
    TurnClaimant.METADATA_RUN_PREFLIGHT_REJECT,
    # Actuation-obligation fill carve-out admits the required fill tool through the persistence
    # gate; predicate-gated at its call site, never an unconditional rank flip.
    TurnClaimant.ACTUATION_OBLIGATION_FILL,
    # One-shot credential-scout reopen admits evaluate through the persistence gate;
    # predicate-gated at its call site.
    TurnClaimant.CREDENTIAL_SCOUT_REOPEN,
    # First-match cascade order: the uncovered-output rescout steer is evaluated before the
    # persistence force and the grounding gate.
    TurnClaimant.UNCOVERED_OUTPUT_RESCOUT_STEER,
    # Grounding-over-persistence nested exception: a live grounding requirement is emitted instead
    # of the persistence force when both hold.
    TurnClaimant.RECORDED_OUTCOME_GROUNDING,
    # A budget-exhausted churn halt is the loop exit and outranks the persistence force, whose
    # accepted-save channel has already failed by then; the pair only co-occurs on synthesized turns.
    TurnClaimant.CODE_AUTHORING_CHURN,
    # Cascade position after the rescout steer and below the grounding exception; defers to the
    # exhausted churn halt above.
    TurnClaimant.SYNTHESIZED_BLOCK_PERSISTENCE_FORCE,
    # Loop detectors defer to every steering gate above them.
    TurnClaimant.LOOP_DETECTED,
)
_PRECEDENCE_RANK: dict[TurnClaimant, int] = {claimant: index for index, claimant in enumerate(_PRECEDENCE_ORDER)}

# The one shared claimant table: every cascade (tool-side and MCP) resolves a
# signal's claimant here so the two paths cannot diverge.
CLAIMANT_REASON_CODE_FAMILIES: dict[TurnClaimant, frozenset[str]] = {
    TurnClaimant.GENUINELY_TERMINAL: GENUINELY_TERMINAL_BLOCKER_REASON_CODES,
    TurnClaimant.OUTPUT_CONTRACT_ACTUATION: frozenset(),
    TurnClaimant.CREDENTIAL_PRIORITY_CHURN: frozenset({"credential_priority_authoring_churn"}),
    TurnClaimant.METADATA_RUN_PREFLIGHT_REJECT: frozenset(),
    TurnClaimant.ACTUATION_OBLIGATION_FILL: frozenset(),
    TurnClaimant.CREDENTIAL_SCOUT_REOPEN: frozenset(),
    TurnClaimant.UNCOVERED_OUTPUT_RESCOUT_STEER: frozenset({UNCOVERED_OUTPUT_RESCOUT_STEER_REASON_CODE}),
    TurnClaimant.RECORDED_OUTCOME_GROUNDING: frozenset({RECORDED_OUTCOME_GROUNDING_REASON_CODE}),
    TurnClaimant.SYNTHESIZED_BLOCK_PERSISTENCE_FORCE: frozenset({SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE}),
    TurnClaimant.CODE_AUTHORING_CHURN: frozenset({"code_authoring_guardrail_churn"}),
    TurnClaimant.LOOP_DETECTED: frozenset(
        {
            "loop_detected_credential_or_parameter_misconfig",
            "loop_detected_repeated_failed_step",
            "loop_detected_consecutive_same_tool",
            "loop_detected_generic",
            "loop_detected_no_forward_progress_interaction",
        }
    ),
}

_CLAIMANT_BY_REASON_CODE: dict[str, TurnClaimant] = {
    reason_code: claimant
    for claimant, reason_codes in CLAIMANT_REASON_CODE_FAMILIES.items()
    for reason_code in reason_codes
}

# Claimants whose ownership lasts only for the duration of the claiming call: the claim records
# reachability and conflicts, but never suppresses a later gate or render on the same turn.
_TRANSIENT_CLAIMANTS = frozenset(
    {
        TurnClaimant.METADATA_RUN_PREFLIGHT_REJECT,
        TurnClaimant.ACTUATION_OBLIGATION_FILL,
        TurnClaimant.CREDENTIAL_SCOUT_REOPEN,
    }
)

_SIGNALLESS_TERMINAL_TURN_HALT_KIND_VALUES = frozenset({"repair_ceiling_reached", "delivered_unverified"})


def claimant_outranks(candidate: TurnClaimant, incumbent: TurnClaimant) -> bool:
    return _PRECEDENCE_RANK[candidate] < _PRECEDENCE_RANK[incumbent]


def claimant_for_blocker_signal(signal: CopilotToolBlockerSignal) -> TurnClaimant | None:
    if not signal.internal_reason_code:
        return None
    return _CLAIMANT_BY_REASON_CODE.get(signal.internal_reason_code)


@dataclass(frozen=True)
class PrecedenceClaim:
    claimant: TurnClaimant
    renders_final_reply: bool = False


@dataclass(frozen=True)
class GatePrecedenceConflictEvent:
    owner: str
    yielded: str
    owner_renders_final_reply: bool
    yielded_renders_final_reply: bool
    site: str = "claim"

    @property
    def fingerprint(self) -> str:
        return f"{self.owner}>{self.yielded}"


@dataclass
class TurnOwnership:
    claims: dict[TurnClaimant, PrecedenceClaim] = field(default_factory=dict)


_LADDER_CLAIM = PrecedenceClaim(claimant=TurnClaimant.OUTPUT_CONTRACT_ACTUATION)


def _ownership_registry(ctx: AgentContext) -> TurnOwnership:
    if ctx.turn_ownership is None:
        ctx.turn_ownership = TurnOwnership()
    return ctx.turn_ownership


def record_gate_precedence_conflict(
    ctx: AgentContext,
    *,
    owner: str,
    yielded: str,
    owner_renders_final_reply: bool,
    yielded_renders_final_reply: bool,
    site: str,
) -> GatePrecedenceConflictEvent:
    event = GatePrecedenceConflictEvent(
        owner=owner,
        yielded=yielded,
        owner_renders_final_reply=owner_renders_final_reply,
        yielded_renders_final_reply=yielded_renders_final_reply,
        site=site,
    )
    events = ctx.gate_precedence_conflict_events
    events.append(event)
    if len(events) > _MAX_CONFLICT_EVENTS:
        del events[:-_MAX_CONFLICT_EVENTS]
    LOG.info(
        GATE_PRECEDENCE_CONFLICT_LOG_EVENT,
        owner=event.owner,
        yielded=event.yielded,
        fingerprint=event.fingerprint,
        owner_renders_final_reply=event.owner_renders_final_reply,
        yielded_renders_final_reply=event.yielded_renders_final_reply,
        site=event.site,
    )
    return event


def turn_halt_is_genuinely_terminal(halt: TurnHalt | None) -> bool:
    if halt is None:
        return False
    if halt.blocker_signal is not None:
        return blocker_signal_is_genuinely_terminal(halt.blocker_signal)
    return halt.kind.value in _SIGNALLESS_TERMINAL_TURN_HALT_KIND_VALUES


def _held_signal_claimant(ctx: AgentContext) -> TurnClaimant | None:
    held = ctx.blocker_signal
    if held is None:
        return None
    if ctx.blocker_signal_claimant is not None:
        return ctx.blocker_signal_claimant
    return claimant_for_blocker_signal(held)


def effective_signal_claimant(ctx: AgentContext, signal: CopilotToolBlockerSignal) -> TurnClaimant | None:
    if ctx.blocker_signal is signal and ctx.blocker_signal_claimant is not None:
        return ctx.blocker_signal_claimant
    return claimant_for_blocker_signal(signal)


def _claim_is_live(ctx: AgentContext, claim: PrecedenceClaim) -> bool:
    if claim.claimant in _TRANSIENT_CLAIMANTS:
        return False
    if claim.claimant is TurnClaimant.OUTPUT_CONTRACT_ACTUATION:
        return output_contract_ladder_unresolved(ctx)
    if claim.claimant is TurnClaimant.GENUINELY_TERMINAL and turn_halt_is_genuinely_terminal(ctx.turn_halt):
        return True
    return _held_signal_claimant(ctx) is claim.claimant


def current_turn_owner(ctx: AgentContext) -> PrecedenceClaim | None:
    claims = dict(ctx.turn_ownership.claims) if ctx.turn_ownership is not None else {}
    # The actuation ladder's ownership is state-backed: a live ladder owns the
    # turn even when no explicit claim was registered before it went live.
    if TurnClaimant.OUTPUT_CONTRACT_ACTUATION not in claims and output_contract_ladder_unresolved(ctx):
        claims[TurnClaimant.OUTPUT_CONTRACT_ACTUATION] = _LADDER_CLAIM
    live = [claim for claim in claims.values() if _claim_is_live(ctx, claim)]
    if not live:
        return None
    return min(live, key=lambda claim: _PRECEDENCE_RANK[claim.claimant])


def claim_turn(
    ctx: AgentContext,
    claimant: TurnClaimant,
    *,
    renders_final_reply: bool = False,
) -> ClaimOutcome:
    owner = current_turn_owner(ctx)
    if owner is not None and owner.claimant is not claimant and not claimant_outranks(claimant, owner.claimant):
        record_gate_precedence_conflict(
            ctx,
            owner=owner.claimant.value,
            yielded=claimant.value,
            owner_renders_final_reply=owner.renders_final_reply,
            yielded_renders_final_reply=renders_final_reply,
            site="claim",
        )
        return ClaimOutcome.YIELDED
    registry = _ownership_registry(ctx)
    existing = registry.claims.get(claimant)
    if existing is None:
        claim = PrecedenceClaim(claimant=claimant, renders_final_reply=renders_final_reply)
    else:
        # Same-claimant re-claims are first-wins: the original instance stays live; only the metadata widens.
        claim = replace(existing, renders_final_reply=existing.renders_final_reply or renders_final_reply)
    registry.claims[claimant] = claim
    if owner is not None and owner.claimant is not claimant:
        record_gate_precedence_conflict(
            ctx,
            owner=claim.claimant.value,
            yielded=owner.claimant.value,
            owner_renders_final_reply=claim.renders_final_reply,
            yielded_renders_final_reply=owner.renders_final_reply,
            site="claim",
        )
    return ClaimOutcome.OWNED


def release_turn_claim(ctx: AgentContext, claimant: TurnClaimant) -> None:
    if ctx.turn_ownership is not None:
        ctx.turn_ownership.claims.pop(claimant, None)


def _owned_claim_replaces_held(ctx: AgentContext, claimant: TurnClaimant, held: CopilotToolBlockerSignal) -> bool:
    held_claimant = effective_signal_claimant(ctx, held)
    return held_claimant is not None and claimant_outranks(claimant, held_claimant)


def claim_and_stash_blocker_signal(
    ctx: AgentContext,
    claimant: TurnClaimant,
    signal: CopilotToolBlockerSignal,
    *,
    force_stash: bool = False,
) -> str | None:
    """Atomic claim-then-stash: a yielded claim stashes nothing and returns None; an owned claim
    stashes with storage-follows-ownership replacement and takes over the held-signal association."""
    if claim_turn(ctx, claimant, renders_final_reply=signal.renders_final_reply) is ClaimOutcome.YIELDED:
        return None
    payload = stash_blocker_signal(ctx, signal)
    held = ctx.blocker_signal
    if (
        held is not None
        and held is not signal
        and not blocker_signal_is_genuinely_terminal(held)
        and (force_stash or _owned_claim_replaces_held(ctx, claimant, held))
    ):
        ctx.blocker_signal = signal
        LOG.info(
            BLOCKER_SIGNAL_REPLACED_LOG_EVENT,
            owner=claimant.value,
            replaced_reason_code=held.internal_reason_code,
            stashed_reason_code=signal.internal_reason_code,
        )
    if ctx.blocker_signal is signal:
        ctx.blocker_signal_claimant = claimant
    return payload


def emit_blocker_signal_payload(ctx: AgentContext, signal: CopilotToolBlockerSignal) -> str:
    """Claim-gated stash that always returns the tool-block payload: a yielded claim suppresses the
    stash (reply/halt ownership), never the tool block itself."""
    claimant = claimant_for_blocker_signal(signal)
    if claimant is None:
        return stash_blocker_signal(ctx, signal)
    payload = claim_and_stash_blocker_signal(ctx, claimant, signal)
    if payload is not None:
        return payload
    return build_llm_tool_error_payload(signal)


def blocker_signal_render_allowed(ctx: AgentContext, signal: CopilotToolBlockerSignal) -> bool:
    """A held signal mapped to a claimant outranked by the live owner is denied the final-reply
    render and the denial is recorded as a gate-precedence conflict; unclaimed signals fail open."""
    owner = current_turn_owner(ctx)
    if owner is None:
        return True
    claimant = effective_signal_claimant(ctx, signal)
    if claimant is None or claimant is owner.claimant or not claimant_outranks(owner.claimant, claimant):
        return True
    record_gate_precedence_conflict(
        ctx,
        owner=owner.claimant.value,
        yielded=claimant.value,
        owner_renders_final_reply=owner.renders_final_reply,
        yielded_renders_final_reply=signal.renders_final_reply,
        site="final_reply_render",
    )
    return False
