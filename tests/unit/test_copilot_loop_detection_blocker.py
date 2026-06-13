from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.agent import _ensure_unvalidated_proposal_affordance
from skyvern.forge.sdk.copilot.blocker_signal import (
    _INTERNAL_TOOL_NAME_TOKENS,
    _LOOP_PROGRESS_TOOLS,
    CopilotToolBlockerSignal,
    LoopBlockerEvidence,
    assert_clean_user_facing_text,
    loop_blocker_evidence_from_ctx,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer, _stash_and_emit_loop_blocker
from skyvern.forge.sdk.copilot.output_policy import CopilotOutputKind, evaluate_output_policy
from skyvern.forge.sdk.copilot.tools import _build_loop_blocker_signal, _tool_loop_error
from skyvern.forge.sdk.copilot.tools.mcp_hooks import get_skyvern_mcp_alias_map
from skyvern.forge.sdk.copilot.turn_halt import TurnHaltKind

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
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.LOOP_DETECTED
    assert ctx.turn_halt.blocker_signal is ctx.blocker_signal


def test_native_dispatch_consecutive_tool_loop_sets_signal() -> None:
    ctx = _ctx(consecutive_tool_tracker=["list_credentials", "list_credentials"])
    payload = _tool_loop_error(ctx, "list_credentials", None)
    assert payload is not None
    assert isinstance(ctx.blocker_signal, CopilotToolBlockerSignal)
    assert ctx.blocker_signal.internal_reason_code == "loop_detected_consecutive_same_tool"
    assert ctx.blocker_signal.cleared_by_tools == frozenset()
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind == TurnHaltKind.LOOP_DETECTED


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


_FULL_EVIDENCE_REASON = (
    "Failed: The run completed but did not demonstrate the goal outcome(s): The credential information for the "
    "requested person is checked on a public registry site with a search form and expandable result rows. "
    "Add an end-state confirmation (an extraction or validation block) that observes the outcome, then re-run."
)
_FULL_EVIDENCE = LoopBlockerEvidence(
    outcome_gate_reason=_FULL_EVIDENCE_REASON,
    anti_bot_blocked=True,
    has_draft=True,
)
_BRANCH_MESSAGES = {
    "loop_detected_credential_or_parameter_misconfig": (
        "LOOP DETECTED: 'update_and_run_blocks' has already failed 3 times with CREDENTIAL_ERROR; blocking attempt #4."
    ),
    "loop_detected_repeated_failed_step": (
        "LOOP DETECTED: 'update_workflow' has already failed 3 consecutive times with these arguments; "
        "blocking attempt #4."
    ),
    "loop_detected_consecutive_same_tool": "LOOP DETECTED: 'evaluate' has been called 3 times consecutively.",
    "loop_detected_generic": "something unfamiliar happened",
}
_BRANCH_TEMPLATES = {
    "loop_detected_credential_or_parameter_misconfig": (
        "I couldn't run this with the current credential or parameter setup. Update them and ask me to try again."
    ),
    "loop_detected_repeated_failed_step": (
        "I retried without making progress. Tell me what to change and I'll try a different approach."
    ),
    "loop_detected_consecutive_same_tool": (
        "I'm stuck retrying the same step. Tell me what to change and I'll try a different approach."
    ),
    "loop_detected_generic": "I couldn't keep going on this turn. Tell me what to change and I'll try again.",
}
_FULL_TIER_BRANCHES = (
    "loop_detected_repeated_failed_step",
    "loop_detected_consecutive_same_tool",
    "loop_detected_generic",
)


@pytest.mark.parametrize("reason_code", _FULL_TIER_BRANCHES)
def test_full_evidence_composition_names_verdict_blocker_and_draft(reason_code: str) -> None:
    signal = _build_loop_blocker_signal(_BRANCH_MESSAGES[reason_code], tool_name="evaluate", evidence=_FULL_EVIDENCE)
    assert signal.internal_reason_code == reason_code
    assert "did not demonstrate the goal outcome" in signal.user_facing_reason
    assert "verification challenge" in signal.user_facing_reason
    assert "Add an end-state confirmation" not in signal.user_facing_reason
    assert "Failed:" not in signal.user_facing_reason
    assert signal.preserves_workflow_draft is True
    assert dict(signal.extra) == {"loop_evidence_tiers": ["verdict", "anti_bot", "draft"]}
    assert signal.agent_steering_text == _BRANCH_MESSAGES[reason_code]
    assert_clean_user_facing_text(signal.user_facing_reason, blocked_tool="evaluate")


@pytest.mark.parametrize("reason_code", sorted(_BRANCH_MESSAGES))
@pytest.mark.parametrize("evidence", [None, LoopBlockerEvidence()], ids=["none", "all_empty"])
def test_no_recorded_state_keeps_templates_byte_identical(
    reason_code: str, evidence: LoopBlockerEvidence | None
) -> None:
    message = _BRANCH_MESSAGES[reason_code]
    baseline = _build_loop_blocker_signal(message, tool_name="update_and_run_blocks")
    signal = _build_loop_blocker_signal(message, tool_name="update_and_run_blocks", evidence=evidence)
    assert signal.user_facing_reason == _BRANCH_TEMPLATES[reason_code]
    assert signal.user_facing_reason == baseline.user_facing_reason
    assert signal.internal_reason_code == reason_code
    assert signal.recovery_hint == baseline.recovery_hint
    assert signal.agent_steering_text == message
    assert signal.cleared_by_tools == frozenset()
    assert signal.preserves_workflow_draft is False
    assert dict(signal.extra) == {}


def test_credential_branch_keeps_copy_and_gains_only_the_draft_flag() -> None:
    signal = _build_loop_blocker_signal(
        _BRANCH_MESSAGES["loop_detected_credential_or_parameter_misconfig"],
        tool_name="update_and_run_blocks",
        evidence=_FULL_EVIDENCE,
    )
    assert signal.user_facing_reason == _BRANCH_TEMPLATES["loop_detected_credential_or_parameter_misconfig"]
    assert signal.recovery_hint == "ask_user_clarifying"
    assert signal.preserves_workflow_draft is True
    assert dict(signal.extra) == {"loop_evidence_tiers": ["draft"]}


@pytest.mark.parametrize(
    "adversarial_reason",
    [
        "Run wr_123456789012345678 did not finish; outcome unknown.",
        "update_and_run_blocks exhausted its retries against the search form.",
        "do not retry this step; the form stayed blocked.",
        "The run exceeded the per-tool-call budget while the search stayed disabled.",
    ],
)
def test_adversarial_recorded_reason_drops_free_text_tier_without_raising(adversarial_reason: str) -> None:
    evidence = LoopBlockerEvidence(outcome_gate_reason=adversarial_reason, anti_bot_blocked=True, has_draft=False)
    signal = _build_loop_blocker_signal(
        _BRANCH_MESSAGES["loop_detected_consecutive_same_tool"], tool_name="evaluate", evidence=evidence
    )
    lowered = signal.user_facing_reason.lower()
    for token in ("wr_", "update_and_run_blocks", "do not retry", "per-tool-call budget"):
        assert token not in lowered
    assert "verification challenge" in signal.user_facing_reason
    assert dict(signal.extra) == {"loop_evidence_tiers": ["anti_bot"]}
    assert_clean_user_facing_text(signal.user_facing_reason, blocked_tool="evaluate")


@pytest.mark.parametrize(
    "raw_error_reason",
    [
        (
            "Failed to execute code block. Reason: TimeoutError: Timeout 30000ms exceeded. "
            '=========================== logs =========================== "load" event fired '
            "============================================================"
        ),
        "code block failed. failure reason: Failed to execute code block.",
        "Traceback (most recent call last): ValueError: bad input",
        "ElementNotFoundException: selector did not resolve",
    ],
)
def test_raw_runtime_error_reason_drops_verdict_tier(raw_error_reason: str) -> None:
    evidence = LoopBlockerEvidence(outcome_gate_reason=raw_error_reason, anti_bot_blocked=True, has_draft=True)
    signal = _build_loop_blocker_signal(
        _BRANCH_MESSAGES["loop_detected_consecutive_same_tool"], tool_name="evaluate", evidence=evidence
    )
    for fragment in ("TimeoutError", "Failed to execute", "===", "Traceback", "Exception"):
        assert fragment not in signal.user_facing_reason
    assert "verification challenge" in signal.user_facing_reason
    assert signal.preserves_workflow_draft is True
    assert dict(signal.extra) == {"loop_evidence_tiers": ["anti_bot", "draft"]}
    assert_clean_user_facing_text(signal.user_facing_reason, blocked_tool="evaluate")


def test_evidence_verdict_sources_only_from_the_outcome_gate_field() -> None:
    ctx = _ctx()
    ctx.last_test_failure_reason = "Failed to execute code block. Reason: TimeoutError: Timeout 30000ms exceeded."
    ctx.last_test_anti_bot = "challenge-gated disabled submit/search control"
    assert loop_blocker_evidence_from_ctx(ctx).outcome_gate_reason is None

    gate_reason = "The run completed but did not demonstrate the goal outcome(s): the requested record is checked."
    ctx.last_outcome_gate_reason = gate_reason
    assert loop_blocker_evidence_from_ctx(ctx).outcome_gate_reason == gate_reason


def test_blocked_tool_named_by_common_word_collides_safely() -> None:
    evidence = LoopBlockerEvidence(
        outcome_gate_reason=(
            "The run completed but did not demonstrate the goal outcome(s): each result row is expanded with a click."
        ),
        anti_bot_blocked=True,
        has_draft=True,
    )
    signal = _build_loop_blocker_signal(
        "LOOP DETECTED: 'click' has been called 3 times consecutively.", tool_name="click", evidence=evidence
    )
    assert "click" not in signal.user_facing_reason.lower()
    assert "verification challenge" in signal.user_facing_reason
    assert signal.preserves_workflow_draft is True
    assert dict(signal.extra) == {"loop_evidence_tiers": ["anti_bot", "draft"]}


_LOOP_PRONE_TOOL_NAMES = sorted(
    set(_LOOP_PROGRESS_TOOLS) | set(_INTERNAL_TOOL_NAME_TOKENS) | set(get_skyvern_mcp_alias_map())
)


@pytest.mark.parametrize("reason_code", sorted(_BRANCH_MESSAGES))
@pytest.mark.parametrize("blocked_tool", _LOOP_PRONE_TOOL_NAMES)
def test_fixed_tier_copy_is_clean_for_every_loop_prone_tool(blocked_tool: str, reason_code: str) -> None:
    evidence = LoopBlockerEvidence(anti_bot_blocked=True, has_draft=True)
    signal = _build_loop_blocker_signal(_BRANCH_MESSAGES[reason_code], tool_name=blocked_tool, evidence=evidence)
    assert_clean_user_facing_text(signal.user_facing_reason, blocked_tool=blocked_tool)


def test_native_and_mcp_paths_carry_equivalent_evidence_bearing_signals() -> None:
    def _prepped_ctx() -> CopilotContext:
        ctx = _ctx(consecutive_tool_tracker=["list_credentials", "list_credentials"])
        ctx.last_outcome_gate_reason = (
            "The run completed but did not demonstrate the goal outcome(s): the requested record is checked "
            "on a public registry site with a search form and expandable result rows."
        )
        ctx.last_test_anti_bot = "challenge-gated disabled submit/search control"
        ctx.has_staged_proposal = True
        return ctx

    native_ctx = _prepped_ctx()
    native_payload = _tool_loop_error(native_ctx, "list_credentials", None)
    assert native_payload is not None
    native_signal = native_ctx.blocker_signal
    assert isinstance(native_signal, CopilotToolBlockerSignal)

    mcp_ctx = _prepped_ctx()
    mcp_payload = _stash_and_emit_loop_blocker(mcp_ctx, native_signal.agent_steering_text, "list_credentials")
    mcp_signal = mcp_ctx.blocker_signal
    assert isinstance(mcp_signal, CopilotToolBlockerSignal)

    assert mcp_payload == native_payload
    assert mcp_signal.user_facing_reason == native_signal.user_facing_reason
    assert "did not demonstrate the goal outcome" in mcp_signal.user_facing_reason
    assert "verification challenge" in mcp_signal.user_facing_reason
    assert native_signal.preserves_workflow_draft is True
    assert mcp_signal.preserves_workflow_draft is True
    assert dict(native_signal.extra) == {"loop_evidence_tiers": ["verdict", "anti_bot", "draft"]}
    assert dict(mcp_signal.extra) == dict(native_signal.extra)
    assert mcp_signal.internal_reason_code == native_signal.internal_reason_code
    assert mcp_signal.agent_steering_text == native_signal.agent_steering_text
    assert mcp_signal.recovery_hint == native_signal.recovery_hint
    assert mcp_signal.blocked_tool == native_signal.blocked_tool


def test_tool_loop_error_entry_refreshes_stale_held_loop_signal() -> None:
    ctx = _ctx()
    stale = _build_loop_blocker_signal(_BRANCH_MESSAGES["loop_detected_consecutive_same_tool"], tool_name="evaluate")
    ctx.blocker_signal = stale
    ctx.last_outcome_gate_reason = (
        "The run completed but did not demonstrate the goal outcome(s): the requested record is checked."
    )
    ctx.last_test_anti_bot = "challenge-gated disabled submit/search control"
    ctx.staged_workflow_yaml = "blocks: []"

    assert _tool_loop_error(ctx, "list_credentials", None) is None

    refreshed = ctx.blocker_signal
    assert isinstance(refreshed, CopilotToolBlockerSignal)
    assert refreshed is not stale
    assert "did not demonstrate the goal outcome" in refreshed.user_facing_reason
    assert "verification challenge" in refreshed.user_facing_reason
    assert refreshed.preserves_workflow_draft is True
    assert refreshed.agent_steering_text == stale.agent_steering_text
    assert refreshed.internal_reason_code == stale.internal_reason_code
    assert refreshed.blocked_tool == stale.blocked_tool


@pytest.mark.asyncio
async def test_mcp_call_tool_entry_refreshes_stale_held_loop_signal() -> None:
    ctx = _ctx()
    stale = _build_loop_blocker_signal(_BRANCH_MESSAGES["loop_detected_consecutive_same_tool"], tool_name="evaluate")
    ctx.blocker_signal = stale
    ctx.last_test_anti_bot = "challenge-gated disabled submit/search control"
    ctx.has_staged_proposal = True

    class _StubResult:
        structured_content = {"ok": False, "error": "page state unchanged"}
        is_error = False
        content: list = []

    class _StubClient:
        async def call_tool(self, name: str, args: dict, raise_on_error: bool = False) -> _StubResult:
            return _StubResult()

    server = SkyvernOverlayMCPServer(
        transport=None,
        overlays={},
        alias_map={},
        allowlist=frozenset({"evaluate"}),
        context_provider=lambda: ctx,
    )
    server._client = _StubClient()  # type: ignore[assignment]

    await server.call_tool("evaluate", {})

    refreshed = ctx.blocker_signal
    assert isinstance(refreshed, CopilotToolBlockerSignal)
    assert refreshed is not stale
    assert "verification challenge" in refreshed.user_facing_reason
    assert refreshed.preserves_workflow_draft is True


def test_composed_loop_reply_passes_output_policy_allow_verdict() -> None:
    signal = _build_loop_blocker_signal(
        _BRANCH_MESSAGES["loop_detected_consecutive_same_tool"],
        tool_name="evaluate",
        evidence=LoopBlockerEvidence(outcome_gate_reason=_FULL_EVIDENCE_REASON, anti_bot_blocked=True, has_draft=False),
    )
    verdict = evaluate_output_policy(
        request_policy=None,
        response_type="REPLY",
        user_response=signal.user_facing_reason,
        global_llm_context=None,
        workflow_yaml=None,
        has_workflow_proposal=False,
        workflow_was_persisted=False,
        workflow_attempted=False,
        unvalidated=False,
        output_kind=CopilotOutputKind.INFORMATIONAL_ANSWER,
    )
    assert verdict.allowed, [code.value for code in verdict.reason_codes]


def test_composed_loop_reply_with_draft_affordance_passes_output_policy() -> None:
    signal = _build_loop_blocker_signal(
        _BRANCH_MESSAGES["loop_detected_consecutive_same_tool"], tool_name="evaluate", evidence=_FULL_EVIDENCE
    )
    reply = _ensure_unvalidated_proposal_affordance(signal.user_facing_reason)
    verdict = evaluate_output_policy(
        request_policy=None,
        response_type="REPLY",
        user_response=reply,
        global_llm_context=None,
        workflow_yaml="title: Example workflow\nblocks: []",
        has_workflow_proposal=True,
        workflow_was_persisted=False,
        workflow_attempted=False,
        unvalidated=True,
        output_kind=CopilotOutputKind.INFORMATIONAL_ANSWER,
    )
    assert verdict.allowed, [code.value for code in verdict.reason_codes]
