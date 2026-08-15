from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODE
from skyvern.forge.sdk.copilot.turn_halt import (
    CopilotTurnHalt,
    TurnHaltKind,
    raise_if_turn_halt,
    stash_turn_halt_from_blocker_signal,
)


def _signal(reason_code: str, *, blocker_kind: str = "tool_error") -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind=blocker_kind,  # type: ignore[arg-type]
        agent_steering_text="stop",
        user_facing_reason="blocked",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        renders_final_reply=True,
        internal_reason_code=reason_code,
    )


def test_terminal_challenge_stashes_local_halt() -> None:
    ctx = SimpleNamespace(turn_halt=None)

    halt = stash_turn_halt_from_blocker_signal(
        ctx,
        _signal(TERMINAL_CHALLENGE_BLOCKER_REASON_CODE),
        source="test",
    )

    assert halt is not None
    assert halt.kind is TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx)
