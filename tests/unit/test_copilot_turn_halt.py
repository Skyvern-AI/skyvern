from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal, stash_blocker_signal
from skyvern.forge.sdk.copilot.enforcement import _check_enforcement
from skyvern.forge.sdk.copilot.failure_tracking import ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE
from skyvern.forge.sdk.copilot.turn_halt import (
    CopilotTurnHalt,
    TurnHaltKind,
    raise_if_turn_halt,
    stash_turn_halt_from_blocker_signal,
    turn_halt_from_blocker_signal,
)


def _signal(
    *,
    blocker_kind: str = "tool_error",
    internal_reason_code: str = ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE,
    renders_final_reply: bool = True,
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind=blocker_kind,
        agent_steering_text="terminal blocker",
        user_facing_reason="The page appears blocked by a site challenge.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=renders_final_reply,
        internal_reason_code=internal_reason_code,
        blocked_tool="update_and_run_blocks",
    )


@pytest.mark.parametrize(
    ("signal", "expected_kind"),
    [
        (
            _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_repeated_failed_step"),
            TurnHaltKind.LOOP_DETECTED,
        ),
        (
            _signal(internal_reason_code=ACTIVE_RUN_TERMINAL_EVIDENCE_REASON_CODE),
            TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE,
        ),
        (
            _signal(internal_reason_code="tool_error_run_output_terminal_blocker"),
            TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE,
        ),
        (_signal(internal_reason_code="probable_site_block_stop"), TurnHaltKind.PROBABLE_SITE_BLOCK),
    ],
)
def test_terminal_blockers_map_to_halts(signal: CopilotToolBlockerSignal, expected_kind: TurnHaltKind) -> None:
    halt = turn_halt_from_blocker_signal(signal, source="hook")

    assert halt is not None
    assert halt.kind == expected_kind
    assert halt.blocker_signal is signal


def test_stash_and_raise_turn_halt_sets_context_once() -> None:
    ctx = SimpleNamespace(turn_halt=None)
    signal = _signal(blocker_kind="loop_detected", internal_reason_code="loop_detected_repeated_failed_step")

    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="stream")

    assert halt is ctx.turn_halt
    assert halt is not None
    with pytest.raises(CopilotTurnHalt) as exc_info:
        raise_if_turn_halt(ctx)
    assert exc_info.value.halt is ctx.turn_halt


def test_enforcement_backstop_converts_existing_terminal_blocker_signal() -> None:
    ctx = SimpleNamespace(turn_halt=None, latest_tool_blocker_signal=None, blocker_signal=None)
    signal = _signal()
    stash_blocker_signal(ctx, signal)

    with pytest.raises(CopilotTurnHalt) as exc_info:
        _check_enforcement(ctx)

    assert ctx.turn_halt is exc_info.value.halt
    assert exc_info.value.halt.kind == TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
