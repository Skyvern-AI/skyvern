"""Typed local halt contract for genuinely terminal Copilot outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
)
from skyvern.forge.sdk.copilot.blocker_signal import to_trace_data as blocker_signal_to_trace_data
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODES

LOG = structlog.get_logger()


class TurnHaltKind(StrEnum):
    ACTIVE_TERMINAL_CHALLENGE = "active_terminal_challenge"


class TurnHaltVerdict(StrEnum):
    BLOCKED = "blocked"


_ACTIVE_TERMINAL_CHALLENGE_REASON_CODES = frozenset(
    {
        *TERMINAL_CHALLENGE_BLOCKER_REASON_CODES,
        "tool_error_run_output_terminal_blocker",
    }
)


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


def turn_halt_from_blocker_signal(signal: object, *, source: str) -> TurnHalt | None:
    if not isinstance(signal, CopilotToolBlockerSignal) or not signal.renders_final_reply:
        return None
    if signal.internal_reason_code not in _ACTIVE_TERMINAL_CHALLENGE_REASON_CODES:
        return None
    return TurnHalt(
        kind=TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE,
        blocker_signal=signal,
        draft_state={"preserves_workflow_draft": signal.preserves_workflow_draft},
        extra={**signal.extra, "source": source},
    )


def stash_turn_halt_from_blocker_signal(ctx: Any, signal: object, *, source: str) -> TurnHalt | None:
    existing = getattr(ctx, "turn_halt", None)
    if isinstance(existing, TurnHalt):
        return existing
    halt = turn_halt_from_blocker_signal(signal, source=source)
    if halt is not None:
        ctx.turn_halt = halt
        LOG.info("copilot turn halt stashed", **turn_halt_to_trace_data(halt))
    return halt


def raise_if_turn_halt(ctx: Any, *, verified: bool = False) -> None:
    del verified
    halt = getattr(ctx, "turn_halt", None)
    if not isinstance(halt, TurnHalt):
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
