"""Typed terminal halt contract for Copilot turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.blocker_signal import to_trace_data as blocker_signal_to_trace_data
from skyvern.forge.sdk.copilot.failure_tracking import ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODE

LOG = structlog.get_logger()


class TurnHaltKind(StrEnum):
    LOOP_DETECTED = "loop_detected"
    ACTIVE_TERMINAL_CHALLENGE = "active_terminal_challenge"
    PROBABLE_SITE_BLOCK = "probable_site_block"
    REPAIR_CEILING_REACHED = "repair_ceiling_reached"


class TurnHaltVerdict(StrEnum):
    BLOCKED = "blocked"


_LOOP_TERMINAL_REASON_CODES = frozenset(
    {
        "loop_detected_credential_or_parameter_misconfig",
        "loop_detected_repeated_failed_step",
        "loop_detected_consecutive_same_tool",
        "loop_detected_generic",
        "code_authoring_guardrail_churn",
        "credential_priority_authoring_churn",
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

# A held blocker whose reason code is in this set must win both the rendered
# reply and the typed halt kind over a later non-terminal trip (e.g. the
# code-authoring churn backstop), which defers entirely when one is present.
GENUINELY_TERMINAL_BLOCKER_REASON_CODES = (
    _ACTIVE_TERMINAL_CHALLENGE_REASON_CODES | _PROBABLE_SITE_BLOCK_REASON_CODES | frozenset({"repair_ceiling_reached"})
)


def blocker_signal_is_genuinely_terminal(signal: CopilotToolBlockerSignal | None) -> bool:
    return signal is not None and signal.internal_reason_code in GENUINELY_TERMINAL_BLOCKER_REASON_CODES


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


def stash_turn_halt_from_blocker_signal(ctx: Any, signal: object, *, source: str) -> TurnHalt | None:
    existing = getattr(ctx, "turn_halt", None)
    if isinstance(existing, TurnHalt):
        return existing
    halt = turn_halt_from_blocker_signal(signal, source=source)
    if halt is None:
        return None
    ctx.turn_halt = halt
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
    LOG.info("copilot turn halt stashed", **turn_halt_to_trace_data(halt))
    return halt


def raise_if_turn_halt(ctx: Any) -> None:
    halt = getattr(ctx, "turn_halt", None)
    if isinstance(halt, TurnHalt):
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
