from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import (
    BROWSER_SESSION_LOST_BLOCKER_REASON_CODE,
    CopilotToolBlockerSignal,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    merge_visual_composition_evidence,
    parse_composition_html,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import terminal_challenge_blocker_signal_from_page_evidence
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.run_outcome import (
    DEVICE_APPROVAL_BLOCKER_REASON_CODE,
    TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
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


def test_device_approval_challenge_stashes_local_halt() -> None:
    ctx = SimpleNamespace(turn_halt=None)

    halt = stash_turn_halt_from_blocker_signal(
        ctx,
        _signal(DEVICE_APPROVAL_BLOCKER_REASON_CODE),
        source="test",
    )

    assert halt is not None
    assert halt.kind is TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx)


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


_GENUINE_CHALLENGE_PAGES = {
    "captcha": (
        "<html><head><title>Security Verification</title></head><body>"
        "<form><input id='lastName' name='lastName' type='text' />"
        "<div class='captcha-box'><p id='captchaInstruction'>Enter all the digits from 'c7MDRxt'</p>"
        "<input id='captchaAnswer' name='captchaAnswer' type='text' /></div>"
        "<button type='submit'>Search</button></form></body></html>"
    ),
    "access_denied": (
        "<html><head><title>Access Denied</title></head><body>"
        "<h1>Access denied</h1><p>You do not have permission to view this page.</p></body></html>"
    ),
    "device_approval": (
        "<html><head><title>Approve this sign-in</title></head><body>"
        "<p>Open your authenticator app and approve this sign-in to complete verification.</p>"
        "<form><button type='submit'>Resend request</button></form></body></html>"
    ),
}
_SATISFIABLE_TOTP_PAGE = (
    "<html><head><title>Two-Factor Authentication</title></head><body>"
    "<p>Complete the challenge to continue.</p>"
    "<form><label for='token'>Authenticator token</label>"
    "<input id='token' name='token' type='text' placeholder='123456' />"
    "<button type='submit' class='btn--login'>Login</button></form></body></html>"
)
_VISION_CHALLENGE_SUMMARY = {
    "summary": "A centered verification card is shown; the primary submit control is described as blocked.",
    "challenge_detected": True,
    "challenge_kind": "other",
    "challenge_location": "Centered page card",
    "submit_blocked": True,
    "blocked_submit_controls": ["Login button requires successful two-factor authentication"],
}


def _page_evidence_ctx(html: str) -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        request_policy=RequestPolicy(allow_update_workflow=True, allow_run_blocks=True),
    )
    ctx.last_run_blocks_workflow_run_id = "wr_failed"
    ctx.composition_page_evidence = merge_visual_composition_evidence(
        parse_composition_html(html, inspected_url="https://example.test/x", current_url="https://example.test/x"),
        visual_summary=dict(_VISION_CHALLENGE_SUMMARY),
    )
    return ctx


@pytest.mark.parametrize("page", sorted(_GENUINE_CHALLENGE_PAGES))
def test_page_with_no_satisfiable_path_still_halts_the_turn(page: str) -> None:
    ctx = _page_evidence_ctx(_GENUINE_CHALLENGE_PAGES[page])

    signal = terminal_challenge_blocker_signal_from_page_evidence(ctx, blocked_tool="update_and_run_blocks")
    halt = stash_turn_halt_from_blocker_signal(ctx, signal, source="test")

    assert signal is not None
    assert halt is not None
    assert halt.kind is TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE
    with pytest.raises(CopilotTurnHalt):
        raise_if_turn_halt(ctx)


def test_editable_one_time_code_page_leaves_the_turn_running() -> None:
    ctx = _page_evidence_ctx(_SATISFIABLE_TOTP_PAGE)

    assert terminal_challenge_blocker_signal_from_page_evidence(ctx, blocked_tool="update_and_run_blocks") is None
    assert stash_turn_halt_from_blocker_signal(ctx, None, source="test") is None
    raise_if_turn_halt(ctx)
