from __future__ import annotations

from types import SimpleNamespace
from typing import Any, get_args

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.agent import (
    _FALLBACK_BLOCKER_REPLY,
    _RAW_SECRET_LEAK_REFUSAL,
    _build_output_policy_blocked_result,
    _build_turn_halt_exit_result,
    _finalize_result_with_blocker_override,
    _render_blocker_reply,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    _LEAK_DENY_TOKENS,
    BlockerKind,
    CopilotToolBlockerSignal,
    RecoveryHint,
)
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext
from skyvern.forge.sdk.copilot.output_policy import CopilotOutputKind, OutputPolicyReason, OutputPolicyVerdict
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.turn_halt import TurnHalt, TurnHaltKind

# Source-of-truth deny list lives in blocker_signal.py. Re-importing here
# (instead of hand-copying) guarantees the test stays in sync if a new token
# is added to the module's deny list.
_LEAK_TOKENS_FULL = _LEAK_DENY_TOKENS

# Pull the actual Literal members so parametrize stays exhaustive: if a new
# BlockerKind / RecoveryHint is added to the model, the test grid expands
# automatically rather than silently passing with stale values.
_ALL_BLOCKER_KINDS: tuple[BlockerKind, ...] = get_args(BlockerKind)
_ALL_RECOVERY_HINTS: tuple[RecoveryHint, ...] = get_args(RecoveryHint)


def _ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="org",
        workflow_id="wf",
        workflow_permanent_id="wfp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _signal(
    *,
    kind: BlockerKind = "authority_denied",
    user_facing: str = "I can't update or run this workflow on this turn.",
    recovery_hint: RecoveryHint = "report_blocker_to_user",
    blocked_tool: str = "update_workflow",
    internal_reason_code: str = "turn_intent_no_mutation_run_blocked",
    classifier_mode: str = "docs_answer",
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind=kind,
        agent_steering_text="Reply to the user without updating the workflow.",
        user_facing_reason=user_facing,
        recovery_hint=recovery_hint,
        internal_reason_code=internal_reason_code,
        blocked_tool=blocked_tool,
        classifier_mode=classifier_mode,
    )


def _agent_result(user_response: str = "Agent prose reply with leaked TurnIntent vocab.") -> AgentResult:
    return AgentResult(
        user_response=user_response,
        updated_workflow=None,
        global_llm_context=None,
    )


def _blocked_result(
    ctx: CopilotContext,
    *reason_codes: OutputPolicyReason,
    output_kind: CopilotOutputKind = CopilotOutputKind.REFUSAL,
) -> AgentResult:
    return _build_output_policy_blocked_result(
        ctx,
        OutputPolicyVerdict(
            allowed=False,
            output_kind=output_kind,
            reason_codes=list(reason_codes),
        ),
        prior_global_llm_context=None,
        prior_workflow_yaml=None,
    )


def _seed_terminal_evidence(ctx: CopilotContext, run_id: str = "wr_latest") -> None:
    ctx.last_run_blocks_workflow_run_id = run_id
    ctx.last_run_outcome = RecordedRunOutcome(
        verdict="not_demonstrated",
        reason_code="outcome_not_demonstrated",
        display_reason="The requested record was not verified.",
        workflow_run_id=run_id,
    )
    ctx.last_outcome_gate_reason = (
        "Failed: The run completed but did not demonstrate the goal outcome(s): "
        "the requested record was not verified. Add an end-state confirmation, then re-run."
    )
    ctx.last_outcome_gate_workflow_run_id = run_id


@pytest.mark.parametrize("recovery_hint", _ALL_RECOVERY_HINTS)
def test_render_picks_response_type_from_hint(recovery_hint: RecoveryHint) -> None:
    signal = _signal(recovery_hint=recovery_hint)
    user_response, resp_type = _render_blocker_reply(signal)
    expected_resp_type = "ASK_QUESTION" if recovery_hint == "ask_user_clarifying" else "REPLY"
    assert resp_type == expected_resp_type
    assert user_response == signal.user_facing_reason


def test_render_falls_back_when_template_leaks() -> None:
    # `model_construct` bypasses the @model_validator that blocks leaks at construction so we can exercise the renderer's defense-in-depth fallback.
    signal = CopilotToolBlockerSignal.model_construct(
        blocker_kind="authority_denied",
        agent_steering_text="Reply without updating.",
        user_facing_reason="LOOP DETECTED: this should have been curated out.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=False,
        internal_reason_code="turn_intent_no_mutation_run_blocked",
        blocked_tool="update_workflow",
        classifier_mode="docs_answer",
        exception_type=None,
        extra={},
    )
    user_response, _ = _render_blocker_reply(signal)
    assert user_response == _FALLBACK_BLOCKER_REPLY


# Cartesian product of every BlockerKind × RecoveryHint pulled from the
# Literal definitions so the leak-deny-list guard stays exhaustive when
# either set grows.
@pytest.mark.parametrize(
    "kind,recovery_hint",
    [(k, h) for k in _ALL_BLOCKER_KINDS for h in _ALL_RECOVERY_HINTS],
)
def test_finalization_shim_renders_clean_reply(kind: BlockerKind, recovery_hint: RecoveryHint) -> None:
    ctx = _ctx()
    signal = _signal(kind=kind, recovery_hint=recovery_hint)
    ctx.blocker_signal = signal
    result = _agent_result()
    overridden = _finalize_result_with_blocker_override(ctx, result)
    assert overridden.updated_workflow is None
    assert overridden.workflow_yaml is None
    assert overridden.clear_proposed_workflow is True
    assert overridden.proposal_disposition == "no_proposal"
    if recovery_hint == "ask_user_clarifying":
        assert overridden.response_type == "ASK_QUESTION"
    else:
        assert overridden.response_type == "REPLY"
    for token in _LEAK_TOKENS_FULL:
        assert token.lower() not in overridden.user_response.lower()
    # Tool name (non-English token) must not leak either.
    assert signal.blocked_tool is not None
    assert signal.blocked_tool not in overridden.user_response


def test_shim_no_op_when_no_signal() -> None:
    ctx = _ctx()
    result = _agent_result("agent reply")
    overridden = _finalize_result_with_blocker_override(ctx, result)
    assert overridden is result


def test_shim_persists_narrative_payload_for_blocker_terminal() -> None:
    ctx = _ctx()
    ctx.turn_id = "turn-1"
    ctx.blocker_signal = _signal()
    result = _agent_result()
    overridden = _finalize_result_with_blocker_override(ctx, result)
    assert overridden.narrative_payload is not None
    assert overridden.narrative_payload["turnId"] == "turn-1"
    assert overridden.narrative_payload["terminal"] == "response"
    assert overridden.narrative_payload["terminalMessage"] == overridden.user_response


def test_shim_overrides_proposal_even_when_pre_override_result_carries_workflow() -> None:
    ctx = _ctx()
    ctx.blocker_signal = _signal()
    # Mock workflow surface to make sure the shim zeroes it.
    fake_workflow: Any = object()
    result = AgentResult(
        user_response="agent reply that proposes a workflow",
        updated_workflow=fake_workflow,
        global_llm_context=None,
        workflow_yaml="title: X\n",
    )
    overridden = _finalize_result_with_blocker_override(ctx, result)
    assert overridden.updated_workflow is None
    assert overridden.workflow_yaml is None


def test_shim_recomputes_turn_outcome_from_rendered_reply() -> None:
    ctx = _ctx()
    ctx.blocker_signal = _signal()
    result = _agent_result()
    overridden = _finalize_result_with_blocker_override(ctx, result)
    # turn_outcome is recomputed via apply_repeated_reply_guard on the
    # rendered reply, so it must not be None and must align with the new text.
    assert overridden.turn_outcome is not None
    # apply_repeated_reply_guard returns the rendered reply as final_text on a
    # clean (non-blocked) signature, so user_response should match the
    # rendered signal's user_facing_reason.
    assert overridden.user_response == ctx.blocker_signal.user_facing_reason


def test_output_policy_blocked_result_zeroes_proposal_when_blocker_active() -> None:
    """OutputPolicy hard-block must not surface a workflow proposal when a
    blocker is set, even though the shim is intentionally skipped on that path.
    """
    ctx = _ctx()
    # Mock a workflow on ctx to confirm the builder zeros it.
    fake_workflow: Any = object()
    ctx.last_workflow = fake_workflow
    ctx.last_workflow_yaml = "title: X\nworkflow_definition:\n  blocks: []\n"
    ctx.blocker_signal = _signal(internal_reason_code="tool_error_pending_reconciliation_no_input")

    result = _blocked_result(ctx, OutputPolicyReason.RAW_SECRET_LEAK)
    assert result.updated_workflow is None
    assert result.workflow_yaml is None
    assert result.proposal_disposition == "no_proposal"


def test_output_policy_generic_block_uses_recorded_terminal_evidence() -> None:
    ctx = _ctx()
    fake_workflow: Any = object()
    ctx.last_workflow = fake_workflow
    ctx.last_workflow_yaml = "title: Draft\n"
    ctx.workflow_persisted = True
    ctx.last_test_ok = True
    _seed_terminal_evidence(ctx)
    ctx.last_test_anti_bot = "challenge-gated disabled submit/search control"

    result = _blocked_result(ctx, OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK)

    assert result.response_type == "ASK_QUESTION"
    assert result.updated_workflow is fake_workflow
    assert result.proposal_disposition == "review_tested"
    assert "latest run recorded workflow output" in result.user_response
    assert "did not demonstrate the goal outcome" in result.user_response
    assert "verification challenge" in result.user_response
    assert "workflow draft is still saved" in result.user_response
    assert "wr_latest" not in result.user_response
    assert result.narrative_payload is not None
    assert result.narrative_payload["terminalMessage"] == result.user_response
    assert result.narrative_payload["responseType"] == "ASK_QUESTION"


def test_output_policy_recorded_evidence_recheck_keeps_original_output_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    _seed_terminal_evidence(ctx)
    seen_output_kinds: list[CopilotOutputKind] = []

    def allow_policy(**kwargs: Any) -> OutputPolicyVerdict:
        seen_output_kinds.append(kwargs["output_kind"])
        return OutputPolicyVerdict(allowed=True, output_kind=kwargs["output_kind"], reason_codes=[])

    monkeypatch.setattr(agent_module, "evaluate_output_policy", allow_policy)

    result = _blocked_result(
        ctx,
        OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK,
        output_kind=CopilotOutputKind.REFUSAL,
    )

    assert result.response_type == "ASK_QUESTION"
    assert seen_output_kinds == [CopilotOutputKind.REFUSAL]


def test_output_policy_generic_block_requires_clean_terminal_evidence() -> None:
    no_recorded = _ctx()
    adversarial = _ctx()
    adversarial.last_run_blocks_workflow_run_id = "wr_hidden"
    adversarial.last_outcome_gate_reason = "update_and_run_blocks failed for wr_hidden; do not retry this step."
    adversarial.last_outcome_gate_workflow_run_id = "wr_hidden"

    for ctx in (no_recorded, adversarial):
        result = _blocked_result(ctx, OutputPolicyReason.INTERNAL_TOOL_INSTRUCTION_LEAK)
        assert (
            result.user_response
            == "I could not safely return that chat reply. Please adjust the request and try again."
        )
        assert result.response_type == "ASK_QUESTION"
        assert "update_and_run_blocks" not in result.user_response
        assert "wr_hidden" not in result.user_response
        assert "do not retry" not in result.user_response.lower()


def test_output_policy_specific_branches_bypass_recorded_terminal_fallback() -> None:
    ctx = _ctx()
    _seed_terminal_evidence(ctx)
    raw_secret = _blocked_result(ctx, OutputPolicyReason.RAW_SECRET_LEAK)
    assert raw_secret.user_response == _RAW_SECRET_LEAK_REFUSAL
    assert "latest run" not in raw_secret.user_response.lower()

    clarification_ctx = _ctx()
    clarification_ctx.request_policy = RequestPolicy(
        user_response_policy="ask_clarification",
        clarification_question="Please confirm which saved credential should be used.",
    )
    result = _blocked_result(
        clarification_ctx,
        OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS,
        output_kind=CopilotOutputKind.CLARIFICATION_REQUEST,
    )

    assert result.response_type == "ASK_QUESTION"
    assert result.user_response == "Please confirm which saved credential should be used."
    assert result.narrative_payload is not None
    assert result.narrative_payload["terminalMessage"] == result.user_response
    assert result.narrative_payload["responseType"] == "ASK_QUESTION"


def test_shim_preserves_workflow_draft_when_signal_opts_in() -> None:
    """Late-block-running blockers say 'wrap up with what you have' — they
    surface the saved draft alongside the rendered chat reply."""
    ctx = _ctx()
    fake_workflow: Any = object()
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="Stop and reply with the draft.",
        user_facing_reason="I'm running out of time on this turn. I'll wrap up with what I have so far.",
        recovery_hint="stop",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        internal_reason_code="tool_error_late_block_running",
        blocked_tool="update_and_run_blocks",
    )
    result = AgentResult(
        user_response="leaky agent prose",
        updated_workflow=fake_workflow,
        global_llm_context=None,
        workflow_yaml="title: Draft\n",
        proposal_disposition="review_untested",
    )
    overridden = _finalize_result_with_blocker_override(ctx, result)
    assert overridden.updated_workflow is fake_workflow
    assert overridden.workflow_yaml == "title: Draft\n"
    assert overridden.clear_proposed_workflow is False
    assert overridden.proposal_disposition == "review_untested"
    # Reply still leads with the rendered text; the shim appends the unvalidated-proposal affordance because preserves_workflow_draft is True.
    assert overridden.user_response.startswith(ctx.blocker_signal.user_facing_reason)
    assert overridden.user_response != ctx.blocker_signal.user_facing_reason


def test_shim_preserves_staged_workflow_draft_when_verified_result_is_empty() -> None:
    """Active-run terminal evidence can leave the verified workflow empty while
    the keepable draft remains staged. That draft must still surface for
    explicit user review instead of clearing the proposal controls."""
    ctx = _ctx()
    fake_workflow: Any = object()
    ctx.staged_workflow = fake_workflow
    ctx.staged_workflow_yaml = "title: Staged\n"
    ctx.has_staged_proposal = True
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="Stop and reply with the staged draft.",
        user_facing_reason=(
            "I reached the requested browser state, but the reusable workflow "
            "still needs a clean verification run before it is ready."
        ),
        recovery_hint="stop",
        preserves_workflow_draft=True,
        internal_reason_code="tool_error_active_run_terminal_evidence",
        blocked_tool="update_and_run_blocks",
    )
    result = AgentResult(
        user_response="agent reply",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
        staged_workflow_yaml=ctx.staged_workflow_yaml,
        staged_workflow=fake_workflow,
        has_staged_proposal=True,
    )
    overridden = _finalize_result_with_blocker_override(ctx, result)
    assert overridden.updated_workflow is fake_workflow
    assert overridden.workflow_yaml == ctx.staged_workflow_yaml
    assert overridden.clear_proposed_workflow is False
    assert overridden.proposal_disposition == "review_untested"
    assert overridden.narrative_payload is not None
    assert overridden.narrative_payload["proposalDisposition"] == "review_untested"
    assert "Accept to save" in overridden.user_response


def test_shim_keeps_model_reply_when_signal_opts_out_of_final_rendering() -> None:
    """Some tool guards are steering-only: they block a tool call but still let
    the model answer from evidence already gathered in the turn."""
    ctx = _ctx()
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="Stop tool use and answer from gathered evidence.",
        user_facing_reason="I'm running out of time on this turn. I'll wrap up with what I have so far.",
        recovery_hint="stop",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=False,
        internal_reason_code="tool_error_late_block_running",
        blocked_tool="update_and_run_blocks",
    )
    result = AgentResult(
        user_response="Observed result: TEST-CRED-123 expired on 01/01/2030.",
        updated_workflow=None,
        global_llm_context=None,
        workflow_yaml=None,
    )

    overridden = _finalize_result_with_blocker_override(ctx, result)

    assert overridden is result


def test_preserved_draft_forces_review_untested_even_when_input_was_auto_applicable() -> None:
    """A blocker turn must never auto-apply the draft, even if the pre-override
    result was tagged ``auto_applicable``. The user has to explicitly accept."""
    ctx = _ctx()
    fake_workflow: Any = object()
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text="Stop and reply.",
        user_facing_reason="I'm running out of time. I'll wrap up with what I have so far.",
        recovery_hint="stop",
        preserves_workflow_draft=True,
        internal_reason_code="tool_error_late_block_running",
        blocked_tool="update_and_run_blocks",
    )
    result = AgentResult(
        user_response="agent reply",
        updated_workflow=fake_workflow,
        global_llm_context=None,
        workflow_yaml="title: D\n",
        proposal_disposition="auto_applicable",
    )
    overridden = _finalize_result_with_blocker_override(ctx, result)
    assert overridden.updated_workflow is fake_workflow
    assert overridden.proposal_disposition == "review_untested"


def test_output_policy_blocked_result_surfaces_workflow_when_no_blocker() -> None:
    """Sanity check: the proposal-zeroing only fires when blocker_signal is set."""
    from skyvern.forge.sdk.copilot.agent import _build_output_policy_blocked_result
    from skyvern.forge.sdk.copilot.output_policy import (
        CopilotOutputKind,
        OutputPolicyReason,
        OutputPolicyVerdict,
    )

    ctx = _ctx()
    fake_workflow: Any = object()
    ctx.last_workflow = fake_workflow
    ctx.last_workflow_yaml = "title: X\nworkflow_definition:\n  blocks: []\n"
    # No blocker_signal.

    verdict = OutputPolicyVerdict(
        allowed=False,
        output_kind=CopilotOutputKind.REFUSAL,
        reason_codes=[OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE],
    )
    result = _build_output_policy_blocked_result(
        ctx,
        verdict,
        prior_global_llm_context=None,
        prior_workflow_yaml=None,
    )
    assert result.updated_workflow is fake_workflow
    assert result.workflow_yaml == ctx.last_workflow_yaml


def test_turn_halt_exit_keeps_halt_signal_when_context_signal_is_cleared() -> None:
    ctx = _ctx()
    signal = _signal(
        kind="loop_detected",
        user_facing="I'm stuck retrying the same step. Tell me what to change and I'll try a different approach.",
        internal_reason_code="loop_detected_repeated_failed_step",
        blocked_tool="run_blocks_and_collect_debug",
    )
    halt = TurnHalt(kind=TurnHaltKind.LOOP_DETECTED, blocker_signal=signal)
    ctx.blocker_signal = None

    result = _build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

    assert result.user_response == signal.user_facing_reason
