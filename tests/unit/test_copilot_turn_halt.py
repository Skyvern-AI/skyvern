from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import (
    BROWSER_SESSION_LOST_BLOCKER_REASON_CODE,
    CopilotToolBlockerSignal,
)
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


def test_failed_browser_session_recovery_stashes_session_loss_halt() -> None:
    ctx = SimpleNamespace(turn_halt=None)

    halt = stash_turn_halt_from_blocker_signal(
        ctx,
        _signal(BROWSER_SESSION_LOST_BLOCKER_REASON_CODE),
        source="test",
    )

    assert halt is not None
    assert halt.kind is TurnHaltKind.BROWSER_SESSION_LOST
    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx)


@pytest.mark.parametrize(
    "reason_code",
    [
        "tool_error_terminal_challenge_blocker",
        "tool_error_device_approval_challenge_blocker",
        "tool_error_anything_a_later_change_invents",
    ],
)
def test_only_a_lost_browser_session_ends_the_turn(reason_code: str) -> None:
    """A challenge is a fact the model reads off page evidence and acts on, not a verdict the harness
    imposes. Nothing but a lost browser session halts a turn now, so this holds for the codes the old
    mechanism used and for any a later change introduces."""
    ctx = SimpleNamespace(turn_halt=None)

    assert stash_turn_halt_from_blocker_signal(ctx, _signal(reason_code), source="test") is None
    assert ctx.turn_halt is None
    raise_if_turn_halt(ctx)


def test_a_turn_halts_only_on_a_lost_browser_session_or_a_superseded_build_test() -> None:
    """Both are outcomes the harness observes out of process and the model cannot read off the page."""
    assert [kind.value for kind in TurnHaltKind] == ["browser_session_lost", "build_test_superseded"]
