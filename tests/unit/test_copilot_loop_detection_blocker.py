from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.tools import _build_loop_blocker_signal, _tool_loop_error

_LEAK_TOKENS = ("safe_reason_code", "LOOP DETECTED:")


def _ctx(
    *,
    failed_tool_step_tracker: dict[str, int] | None = None,
    consecutive_tool_tracker: list[str] | None = None,
) -> CopilotContext:
    ctx = CopilotContext(
        organization_id="org",
        workflow_id="wf",
        workflow_permanent_id="wfp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )
    if failed_tool_step_tracker is not None:
        ctx.failed_tool_step_tracker = failed_tool_step_tracker
    if consecutive_tool_tracker is not None:
        ctx.consecutive_tool_tracker = consecutive_tool_tracker
    return ctx


@pytest.mark.parametrize(
    ("loop_message", "expected_reason"),
    [
        (
            "LOOP DETECTED: 'update_and_run_blocks' has already failed 3 times with CREDENTIAL_ERROR; blocking attempt #4.",
            "loop_detected_credential_or_parameter_misconfig",
        ),
        (
            "LOOP DETECTED: 'update_and_run_blocks' has already failed 3 times with PARAMETER_BINDING_ERROR; blocking attempt #4.",
            "loop_detected_credential_or_parameter_misconfig",
        ),
        (
            "LOOP DETECTED: 'update_workflow' has already failed 3 consecutive times with these arguments; blocking attempt #4.",
            "loop_detected_repeated_failed_step",
        ),
        (
            "LOOP DETECTED: 'list_credentials' has been called 3 times consecutively.",
            "loop_detected_consecutive_same_tool",
        ),
    ],
)
def test_loop_blocker_dispatches_on_substring(loop_message: str, expected_reason: str) -> None:
    signal = _build_loop_blocker_signal(loop_message, tool_name="update_and_run_blocks")
    assert signal.blocker_kind == "loop_detected"
    assert signal.internal_reason_code == expected_reason
    # LLM-visible string keeps the raw marker so output_utils sanitization fires.
    assert signal.agent_steering_text == loop_message
    # User-facing string must NOT carry the marker.
    for token in _LEAK_TOKENS:
        assert token not in signal.user_facing_reason
    assert signal.cleared_by_tools == frozenset()


def test_loop_blocker_falls_back_to_generic_on_novel_message() -> None:
    # Pins the catch-all branch so a new loop format from detect_*_loop doesn't
    # silently slip into one of the substring-matched buckets.
    signal = _build_loop_blocker_signal("something unfamiliar happened", tool_name="update_workflow")
    assert signal.internal_reason_code == "loop_detected_generic"
    assert signal.recovery_hint == "report_blocker_to_user"


def test_native_dispatch_failed_step_loop_sets_signal_and_returns_payload() -> None:
    # Tracker simulates 2 prior failures of (update_workflow, {workflow_yaml: 'y'}).
    # detect_failed_tool_step_loop_for_ctx uses tool_step_identity to look up the
    # threshold; mimicking the real identity is brittle, so call the dispatcher
    # with a tracker that already has the same identity prepopulated at threshold-1.
    from skyvern.forge.sdk.copilot.loop_detection import tool_step_identity

    identity = tool_step_identity("update_workflow", {"workflow_yaml": "yaml-1"})
    ctx = _ctx(failed_tool_step_tracker={identity: 3})
    payload = _tool_loop_error(ctx, "update_workflow", {"workflow_yaml": "yaml-1"})
    assert payload is not None
    assert isinstance(ctx.blocker_signal, CopilotToolBlockerSignal)
    assert ctx.blocker_signal.blocker_kind == "loop_detected"
    assert ctx.blocker_signal.internal_reason_code == "loop_detected_repeated_failed_step"
    # LLM payload is agent_steering_text only — raw LOOP DETECTED: marker is in it.
    assert payload.startswith("LOOP DETECTED:")
    # Renderer-side string is clean.
    for token in _LEAK_TOKENS:
        assert token not in ctx.blocker_signal.user_facing_reason


def test_native_dispatch_consecutive_tool_loop_sets_signal() -> None:
    ctx = _ctx(consecutive_tool_tracker=["list_credentials", "list_credentials"])
    payload = _tool_loop_error(ctx, "list_credentials", None)
    assert payload is not None
    assert isinstance(ctx.blocker_signal, CopilotToolBlockerSignal)
    assert ctx.blocker_signal.internal_reason_code == "loop_detected_consecutive_same_tool"
    assert ctx.blocker_signal.cleared_by_tools == frozenset()


def test_native_and_mcp_paths_produce_equivalent_loop_signal() -> None:
    """Native dispatch and the MCP adapter must produce the same signal shape
    for the same loop-detection message so a regression in one path can't
    diverge from the other.
    """
    from skyvern.forge.sdk.copilot.mcp_adapter import _stash_and_emit_loop_blocker

    loop_message = (
        "LOOP DETECTED: 'update_workflow' has already failed 3 consecutive times with these arguments; "
        "blocking attempt #4."
    )
    native_ctx = _ctx()
    mcp_ctx = _ctx()

    # Native path: build the signal via _build_loop_blocker_signal directly
    # (same as _tool_loop_error's first branch).
    native_signal = _build_loop_blocker_signal(loop_message, tool_name="update_workflow")
    native_ctx.blocker_signal = native_signal

    # MCP path: _stash_and_emit_loop_blocker uses _build_loop_blocker_signal
    # under the hood, stashes on ctx, returns LLM payload.
    mcp_payload = _stash_and_emit_loop_blocker(mcp_ctx, loop_message, "update_workflow")

    assert mcp_payload == native_signal.agent_steering_text
    assert isinstance(mcp_ctx.blocker_signal, CopilotToolBlockerSignal)
    # Signal fields match exactly across paths.
    assert mcp_ctx.blocker_signal.blocker_kind == native_signal.blocker_kind
    assert mcp_ctx.blocker_signal.internal_reason_code == native_signal.internal_reason_code
    assert mcp_ctx.blocker_signal.user_facing_reason == native_signal.user_facing_reason
    assert mcp_ctx.blocker_signal.agent_steering_text == native_signal.agent_steering_text
    assert mcp_ctx.blocker_signal.recovery_hint == native_signal.recovery_hint
    assert mcp_ctx.blocker_signal.cleared_by_tools == native_signal.cleared_by_tools
    assert mcp_ctx.blocker_signal.blocked_tool == native_signal.blocked_tool
