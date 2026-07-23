from __future__ import annotations

from types import SimpleNamespace
from typing import Any, get_args

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.agent import (
    _FALLBACK_BLOCKER_REPLY,
    _RAW_SECRET_LEAK_REFUSAL,
    _VERIFIED_WORKFLOW_SUCCESS_REPLY,
    _build_goal_satisfied_exit_result,
    _build_output_policy_blocked_result,
)
from skyvern.forge.sdk.copilot.agent import _build_turn_halt_exit_result as _real_build_turn_halt_exit_result
from skyvern.forge.sdk.copilot.agent import _build_wip_exit_result as _real_build_wip_exit_result
from skyvern.forge.sdk.copilot.agent import (
    _finalize_result_with_blocker_override,
    _render_blocker_reply,
    _verified_workflow_success_reply,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    _LEAK_DENY_TOKENS,
    CREDENTIAL_SCOUT_VERIFY_REPLY,
    BlockerKind,
    CopilotToolBlockerSignal,
    RecoveryHint,
    build_definition_contract_unsatisfied_blocker_signal,
)
from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult, CriterionVerdict
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext, DeliveredUnverifiedPublicOutputs
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.output_policy import (
    ACTUATION_OBLIGATION_STEER_REASON_CODE,
    CopilotOutputKind,
    OutputPolicyReason,
    OutputPolicyVerdict,
)
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.run_outcome import TERMINAL_CHALLENGE_BLOCKER_REASON_CODE, RecordedRunOutcome
from skyvern.forge.sdk.copilot.turn_halt import TurnHalt, TurnHaltKind
from skyvern.forge.sdk.copilot.turn_ownership import TurnClaimant, claim_and_stash_blocker_signal
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest
from tests.unit.conftest import make_copilot_context as _ctx

# Source-of-truth deny list lives in blocker_signal.py. Re-importing here
# (instead of hand-copying) guarantees the test stays in sync if a new token
# is added to the module's deny list.
_LEAK_TOKENS_FULL = _LEAK_DENY_TOKENS

# Pull the actual Literal members so parametrize stays exhaustive: if a new
# BlockerKind / RecoveryHint is added to the model, the test grid expands
# automatically rather than silently passing with stale values.
_ALL_BLOCKER_KINDS: tuple[BlockerKind, ...] = get_args(BlockerKind)
_ALL_RECOVERY_HINTS: tuple[RecoveryHint, ...] = get_args(RecoveryHint)


def _mark_public_outputs(ctx: CopilotContext) -> None:
    if ctx.delivered_unverified_observed_outputs and not isinstance(
        ctx.delivered_unverified_observed_outputs, DeliveredUnverifiedPublicOutputs
    ):
        ctx.delivered_unverified_observed_outputs = DeliveredUnverifiedPublicOutputs(
            ctx.delivered_unverified_observed_outputs
        )


def _build_wip_exit_result(ctx: CopilotContext, *args: Any, **kwargs: Any) -> AgentResult:
    _mark_public_outputs(ctx)
    return _real_build_wip_exit_result(ctx, *args, **kwargs)


def _build_turn_halt_exit_result(ctx: CopilotContext, *args: Any, **kwargs: Any) -> AgentResult:
    _mark_public_outputs(ctx)
    return _real_build_turn_halt_exit_result(ctx, *args, **kwargs)


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


def test_blocker_signal_wins_over_demonstrated_recorded_outcome() -> None:
    ctx = _ctx()
    ctx.blocker_signal = _signal(user_facing="I need one more detail before I can continue.")
    ctx.last_run_outcome = RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_hidden")
    fake_workflow = SimpleNamespace(name="verified")
    result = AgentResult(
        user_response="I created and tested the workflow successfully.",
        updated_workflow=fake_workflow,
        global_llm_context=None,
        workflow_yaml="title: verified\n",
    )

    overridden = _finalize_result_with_blocker_override(ctx, result)

    assert overridden.user_response == "I need one more detail before I can continue."
    assert "created and tested" not in overridden.user_response.lower()
    assert "wr_hidden" not in overridden.user_response
    assert overridden.updated_workflow is None
    assert overridden.proposal_disposition == "no_proposal"


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


def test_output_policy_hard_block_priority_over_actuation_steer() -> None:
    ctx = _ctx()

    result = _blocked_result(ctx, OutputPolicyReason.RAW_SECRET_LEAK, OutputPolicyReason.ACTUATION_OBLIGATION_STEER)

    assert result.user_response == _RAW_SECRET_LEAK_REFUSAL
    assert result.turn_outcome is not None
    assert result.turn_outcome.reason_code == "output_policy_block"
    assert result.turn_outcome.terminal_reason == "output_policy_block"
    assert result.turn_outcome.reason_code != ACTUATION_OBLIGATION_STEER_REASON_CODE


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


def test_definition_contract_halt_renders_sorted_parameter_names_without_internal_machinery() -> None:
    ctx = _ctx()
    signal = build_definition_contract_unsatisfied_blocker_signal(
        unresolved_parameter_keys=["service_address", "business_name", "contact_email"]
    )
    halt = TurnHalt(kind=TurnHaltKind.DEFINITION_CONTRACT_UNSATISFIED, blocker_signal=signal)

    result = _build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

    assert result.user_response == signal.user_facing_reason
    assert result.user_response.index("business_name") < result.user_response.index("contact_email")
    assert result.user_response.index("contact_email") < result.user_response.index("service_address")
    assert "update_and_run_blocks" not in result.user_response


def test_turn_halt_exit_renders_code_authoring_churn_reason() -> None:
    ctx = _ctx()
    signal = _signal(
        kind="loop_detected",
        user_facing="I kept rewriting the generated code, but the safety checks rejected each version.",
        internal_reason_code="code_authoring_guardrail_churn",
        blocked_tool="update_workflow",
    )
    ctx.blocker_signal = signal
    halt = TurnHalt(kind=TurnHaltKind.LOOP_DETECTED, blocker_signal=signal)

    result = _build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

    assert "safety checks rejected" in result.user_response
    assert "update_workflow" not in result.user_response


def test_turn_halt_exit_renders_no_forward_progress_interaction_reason() -> None:
    ctx = _ctx()
    signal = _signal(
        kind="loop_detected",
        user_facing="I couldn't get past this step. Tell me what to change and I'll try a different approach.",
        internal_reason_code="loop_detected_no_forward_progress_interaction",
        blocked_tool="click",
    )
    ctx.blocker_signal = signal
    halt = TurnHalt(kind=TurnHaltKind.LOOP_DETECTED, blocker_signal=signal)

    result = _build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

    assert "couldn't get past this step" in result.user_response
    assert "click" not in result.user_response


def test_turn_halt_exit_keeps_credential_scout_reply_through_refresh_with_draft() -> None:
    ctx = _ctx()
    ctx.last_workflow_yaml = "title: Draft\nworkflow_definition:\n  blocks: []\n"
    signal = _signal(
        kind="loop_detected",
        user_facing=CREDENTIAL_SCOUT_VERIFY_REPLY,
        internal_reason_code="credential_priority_authoring_churn",
        blocked_tool="update_workflow",
    )
    ctx.blocker_signal = signal
    halt = TurnHalt(kind=TurnHaltKind.LOOP_DETECTED, blocker_signal=signal)

    result = _build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

    assert result.user_response == CREDENTIAL_SCOUT_VERIFY_REPLY
    assert "update_workflow" not in result.user_response


def test_turn_halt_exit_renders_terminal_reason_when_terminal_blocker_held() -> None:
    ctx = _ctx()
    terminal = _signal(
        kind="tool_error",
        user_facing="The site's verification challenge blocked the run.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="tool_error_terminal_challenge_blocker",
        blocked_tool="update_and_run_blocks",
    )
    ctx.blocker_signal = terminal
    halt = TurnHalt(kind=TurnHaltKind.ACTIVE_TERMINAL_CHALLENGE, blocker_signal=terminal)

    result = _build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

    assert result.user_response == terminal.user_facing_reason


def _fully_satisfied_result() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms")],
    )


def _fully_satisfied_classification_result() -> CompletionVerificationResult:
    return CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_validation"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_validation",
                state="satisfied",
                reason_code="evidence_confirms",
                output_path="login_only",
                grounding_mode="exact_value",
                has_exact_value=True,
            )
        ],
    )


def _classification_policy() -> RequestPolicy:
    return RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c_validation",
                outcome="The run classifies the path as login gated.",
                kind="validation_classification",
                classification_output_key="login_only",
                expected_classification=True,
            )
        ]
    )


def _seed_verified_outcome(ctx: CopilotContext) -> None:
    ctx.completion_verification_result = _fully_satisfied_result()
    ctx.last_artifact_health_blocker_reason = None
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "title: built\nblocks: []\n"


def test_verified_outcome_preserves_tested_proposal_over_blocker() -> None:
    ctx = _ctx()
    _seed_verified_outcome(ctx)
    ctx.blocker_signal = _signal(
        kind="loop_detected",
        user_facing="I'm stuck retrying the same step.",
        internal_reason_code="loop_detected_repeated_failed_step",
        blocked_tool="update_and_run_blocks",
    )
    result = _agent_result()

    overridden = _finalize_result_with_blocker_override(ctx, result)

    assert overridden.user_response == _VERIFIED_WORKFLOW_SUCCESS_REPLY
    assert overridden.updated_workflow is ctx.last_workflow
    assert overridden.workflow_yaml == ctx.last_workflow_yaml
    assert overridden.clear_proposed_workflow is False
    assert overridden.proposal_disposition == "review_tested"
    assert overridden.response_type == "REPLY"
    assert "stuck retrying" not in overridden.user_response


def test_verified_outcome_preserve_sets_verified_success_payload() -> None:
    ctx = _ctx()
    ctx.turn_id = "turn-verified"
    _seed_verified_outcome(ctx)
    ctx.blocker_signal = _signal(
        kind="loop_detected",
        user_facing="I'm stuck retrying the same step.",
        internal_reason_code="loop_detected_repeated_failed_step",
        blocked_tool="update_and_run_blocks",
    )

    overridden = _finalize_result_with_blocker_override(ctx, _agent_result())

    assert overridden.narrative_payload is not None
    assert overridden.narrative_payload["verifiedSuccess"] is True
    assert overridden.narrative_payload["proposalDisposition"] == "review_tested"


@pytest.mark.asyncio
async def test_verified_classification_success_surfaces_terminal_verdict_and_reload_payload() -> None:
    ctx = _ctx()
    ctx.stream = None
    ctx.turn_id = "turn-classified"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = True
    _seed_verified_outcome(ctx)
    ctx.request_policy = _classification_policy()
    ctx.completion_verification_result = _fully_satisfied_classification_result()
    ctx.verified_block_outputs = {
        "inspect_access_path": {
            "login_only": True,
            "visible_page_path_label": "Start service sign-in gate",
            "safest_reachable_next_step": "Stop before account-specific setup.",
            "recommended_next_action": "Ask the user for online account access before continuing.",
            "evidence_text": "The page says Sign in or register to continue.",
        }
    }

    result = await _build_goal_satisfied_exit_result(ctx, global_llm_context=None)

    assert "login-gated" in result.user_response
    assert "login_only=true" in result.user_response
    assert "visible_page_path_label=Start service sign-in gate" in result.user_response
    assert "safest_reachable_next_step=Stop before account-specific setup." in result.user_response
    assert "recommended_next_action=Ask the user for online account access before continuing." in result.user_response
    assert "observed_gate_phrase=Sign in or register to continue" in result.user_response
    assert result.narrative_payload is not None
    assert result.narrative_payload["terminalMessage"] == result.user_response


def test_verified_classification_summary_does_not_display_unobserved_expected_value() -> None:
    ctx = _ctx()
    _seed_verified_outcome(ctx)
    ctx.request_policy = _classification_policy()
    ctx.completion_verification_result = _fully_satisfied_classification_result()
    ctx.verified_block_outputs = {}

    response = _verified_workflow_success_reply(ctx)

    assert response == "I created and tested the workflow successfully."
    assert "login_only=true" not in response
    assert "login-gated" not in response
    assert "observed_gate_phrase=Sign in or register to continue" not in response


def test_verified_classification_summary_uses_terminal_snapshot_when_frontier_outputs_clear() -> None:
    ctx = _ctx()
    _seed_verified_outcome(ctx)
    ctx.request_policy = _classification_policy()
    ctx.completion_verification_result = _fully_satisfied_classification_result()
    ctx.verified_block_outputs = {}
    ctx.verified_terminal_block_outputs = {
        "inspect_access_path": {
            "login_only": True,
            "visible_page_path_label": "Start, Stop or Move Service",
            "matched_gate_phrases": ["sign in or register to continue"],
        }
    }

    response = _verified_workflow_success_reply(ctx)

    assert "login_only=true" in response
    assert "login-gated" in response
    assert "visible_page_path_label=Start, Stop or Move Service" in response
    assert "observed_gate_phrase=Sign in or register to continue" in response


def test_verified_classification_summary_labels_login_gate_from_verified_gate_phrase() -> None:
    ctx = _ctx()
    _seed_verified_outcome(ctx)
    ctx.request_policy = RequestPolicy(completion_criteria=[CompletionCriterion(id="c0", outcome="Reached gate")])
    ctx.completion_verification_result = _fully_satisfied_result()
    ctx.verified_terminal_block_outputs = {
        "inspect_access_path": {
            "visible_page_path_label": "Start, Stop or Move Service > Start Service",
            "observed_gate_phrase": "Sign in or register to continue",
        }
    }

    response = _verified_workflow_success_reply(ctx)

    assert "visible_page_path_label=Start, Stop or Move Service > Start Service" in response
    assert "observed_gate_phrase=Sign in or register to continue" in response
    assert "login-gated" in response


def test_verified_classification_summary_requires_completion_verification() -> None:
    ctx = _ctx()
    ctx.request_policy = _classification_policy()
    ctx.verified_block_outputs = {
        "inspect_access_path": {
            "login_only": True,
            "visible_page_path_label": "Start service sign-in gate",
            "evidence_text": "The page prose says login-gated and login_only=true.",
        }
    }

    response = _verified_workflow_success_reply(ctx)

    assert response == _VERIFIED_WORKFLOW_SUCCESS_REPLY
    assert "login-gated" not in response
    assert "login_only=true" not in response


def test_verified_classification_summary_requires_fully_satisfied_verification() -> None:
    ctx = _ctx()
    ctx.request_policy = _classification_policy()
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c_validation"],
        verdicts=[
            CriterionVerdict(
                criterion_id="c_validation",
                state="unsatisfied",
                reason_code="evidence_contradicts",
                output_path="login_only",
            )
        ],
    )
    ctx.verified_block_outputs = {
        "inspect_access_path": {
            "login_only": True,
            "visible_page_path_label": "Start service sign-in gate",
        }
    }

    response = _verified_workflow_success_reply(ctx)

    assert response == _VERIFIED_WORKFLOW_SUCCESS_REPLY
    assert "login-gated" not in response
    assert "login_only=true" not in response


def test_unverified_blocker_still_renders_blocker_text() -> None:
    ctx = _ctx()
    ctx.completion_verification_result = None
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "title: built\nblocks: []\n"
    ctx.blocker_signal = _signal(
        kind="loop_detected",
        user_facing="I'm stuck retrying the same step.",
        internal_reason_code="loop_detected_repeated_failed_step",
        blocked_tool="update_and_run_blocks",
    )

    overridden = _finalize_result_with_blocker_override(ctx, _agent_result())

    assert overridden.user_response != _VERIFIED_WORKFLOW_SUCCESS_REPLY
    assert overridden.proposal_disposition != "review_tested"


def test_delivered_unverified_exit_renders_observed_output_as_unvalidated() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"document_name": "Resale Demand Package"}

    result = _build_wip_exit_result(
        ctx,
        None,
        default_reply="default",
        unvalidated_reply="unvalidated",
        tested_reply="tested",
        terminal_reason="turn_halt:delivered_unverified",
    )

    assert "document_name: Resale Demand Package" in result.user_response
    assert "not independently verified" in result.user_response
    assert result.proposal_disposition == "review_untested"
    assert result.narrative_payload is not None
    assert result.narrative_payload["deliveredUnverifiedObservedOutputs"]["document_name"] == "Resale Demand Package"
    assert result.workflow_yaml == ctx.last_workflow_yaml


def test_delivered_unverified_exit_precedes_distinct_last_good_workflow_fallback() -> None:
    ctx = _ctx()
    ctx.last_good_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_good_workflow_yaml = "workflow_definition:\n  blocks:\n    - label: tested-prior\n"
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks:\n    - label: delivered-current\n"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"result": {"confirmation": "confirm-current"}}

    result = _build_wip_exit_result(
        ctx,
        None,
        default_reply="default",
        unvalidated_reply="unvalidated",
        tested_reply="tested prior workflow",
        terminal_reason="turn_halt:delivered_unverified",
    )

    assert 'result["confirmation"]: confirm-current' in result.user_response
    assert "tested prior workflow" not in result.user_response
    assert result.updated_workflow is ctx.last_workflow
    assert result.workflow_yaml == ctx.last_workflow_yaml
    assert result.proposal_disposition == "review_untested"
    assert result.narrative_payload is not None
    assert result.narrative_payload["deliveredUnverifiedObservedOutputs"]["result"] == {
        "confirmation": "confirm-current"
    }


def test_delivered_unverified_halt_without_workflow_renders_observed_output() -> None:
    ctx = _ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_fallback"
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {
        "result": {"account": "acct-fallback", "confirmation": "confirm-fallback", "amount": 0}
    }

    result = _build_turn_halt_exit_result(
        ctx,
        global_llm_context=None,
        halt=TurnHalt(kind=TurnHaltKind.DELIVERED_UNVERIFIED),
    )

    assert 'result["amount"]: 0' in result.user_response
    assert 'result["account"]: acct-fallback' in result.user_response
    assert 'result["confirmation"]: confirm-fallback' in result.user_response
    assert "not independently verified" in result.user_response
    assert result.updated_workflow is None
    assert result.narrative_payload is not None
    observed_outputs = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed_outputs["result"] == ctx.delivered_unverified_observed_outputs["result"]


def test_repair_ceiling_give_up_halt_names_demonstrated_missing_steps() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.has_staged_proposal = True
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.impose_synthesized_code_block = True
    ctx.persisted_draft_browser_calls = []
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/search",
            "trajectory_index": 0,
        }
    ]

    result = _build_turn_halt_exit_result(
        ctx,
        global_llm_context=None,
        halt=TurnHalt(kind=TurnHaltKind.REPAIR_CEILING_REACHED),
    )

    assert "#search-submit" in result.user_response


def test_delivered_unverified_halt_names_demonstrated_missing_steps() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.last_test_ok = True
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"result": {"amount": 0}}
    ctx.has_staged_proposal = True
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.impose_synthesized_code_block = True
    ctx.persisted_draft_browser_calls = []
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/search",
            "trajectory_index": 0,
        }
    ]

    result = _build_turn_halt_exit_result(
        ctx,
        global_llm_context=None,
        halt=TurnHalt(kind=TurnHaltKind.DELIVERED_UNVERIFIED),
    )

    assert "#search-submit" in result.user_response


def _scouted_obligation_ctx() -> CopilotContext:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.has_staged_proposal = True
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.impose_synthesized_code_block = True
    ctx.persisted_draft_browser_calls = []
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/search",
            "trajectory_index": 0,
        }
    ]
    return ctx


def test_timeout_wip_exit_names_demonstrated_missing_steps() -> None:
    ctx = _scouted_obligation_ctx()

    result = agent_module._build_timeout_exit_result(ctx, global_llm_context=None)

    assert "#search-submit" in result.user_response


def test_output_policy_blocked_result_names_demonstrated_missing_steps() -> None:
    ctx = _scouted_obligation_ctx()
    verdict = OutputPolicyVerdict(
        allowed=False,
        output_kind=CopilotOutputKind.REFUSAL,
        reason_codes=[OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE],
    )

    result = _build_output_policy_blocked_result(ctx, verdict, prior_global_llm_context=None, prior_workflow_yaml=None)

    assert "#search-submit" in result.user_response


def test_wip_exit_render_path_names_missing_steps_under_unrelated_blocker() -> None:
    ctx = _scouted_obligation_ctx()
    unrelated_reason = "I couldn't finish this after several attempts. Tell me what to change and I'll try again."
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="An unrelated blocker owns the turn.",
        user_facing_reason=unrelated_reason,
        recovery_hint="report_blocker_to_user",
        internal_reason_code="output_contract_actuation_exhausted",
        blocked_tool="update_workflow",
    )

    result = _build_wip_exit_result(ctx, None, default_reply="wip", unvalidated_reply="wip", tested_reply="wip")

    assert "#search-submit" in result.user_response
    assert result.user_response.count("This draft is still missing steps you demonstrated:") == 1
    assert ctx.blocker_signal.user_facing_reason == unrelated_reason


def test_delivered_unverified_exit_prioritizes_nested_scalars_and_accounts_for_omissions() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {
        "result": {
            "evidence": "e" * 240,
            "account": "acct-314159",
            "confirmation": "confirm-271828",
            "amount": 0,
            **{f"long_{index}": str(index) * 240 for index in range(64)},
        }
    }

    result = _build_wip_exit_result(
        ctx,
        None,
        default_reply="default",
        unvalidated_reply="unvalidated",
        tested_reply="tested",
        terminal_reason="turn_halt:delivered_unverified",
    )

    assert 'result["amount"]: 0' in result.user_response
    assert 'result["account"]: acct-314159' in result.user_response
    assert 'result["confirmation"]: confirm-271828' in result.user_response
    assert 'result["evidence"]: ' in result.user_response
    assert "..." in result.user_response
    assert "more fields available in structured output" in result.user_response
    assert result.user_response.index('result["amount"]') < result.user_response.index('result["evidence"]')
    assert "not independently verified" in result.user_response
    assert result.proposal_disposition == "review_untested"
    assert result.narrative_payload is not None
    observed_outputs = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed_outputs["result"]["account"] == "acct-314159"
    assert observed_outputs["result"]["confirmation"] == "confirm-271828"
    assert observed_outputs["result"]["amount"] == 0
    assert observed_outputs["result"]["evidence"] == "e" * 240
    assert observed_outputs["$skyvernOutput"]["omitted"]["node"] >= 1


def test_delivered_unverified_exit_uses_collision_safe_paths() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {
        "result.value": "top-level",
        "result": {"value": "nested", "items": [{"value": True}]},
    }

    result = _build_wip_exit_result(
        ctx,
        None,
        default_reply="default",
        unvalidated_reply="unvalidated",
        tested_reply="tested",
        terminal_reason="turn_halt:delivered_unverified",
    )

    assert "result.value: top-level" in result.user_response
    assert 'result["value"]: nested' in result.user_response
    assert 'result["items"][0]["value"]: true' in result.user_response


def test_delivered_unverified_exit_preserves_sanitized_key_collisions() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.extend(["registered-secret-a", "registered-secret-b"])
    ctx.delivered_unverified_observed_outputs = {
        "result": {
            "field-registered-secret-a": "first-captured-value",
            "field-registered-secret-b": "second-captured-value",
        }
    }

    result = _build_wip_exit_result(
        ctx,
        None,
        default_reply="default",
        unvalidated_reply="unvalidated",
        tested_reply="tested",
        terminal_reason="turn_halt:delivered_unverified",
    )

    assert "first-captured-value" in result.user_response
    assert "second-captured-value" in result.user_response
    assert result.narrative_payload is not None
    observed_result = result.narrative_payload["deliveredUnverifiedObservedOutputs"]["result"]
    assert list(observed_result) == ["field-[REDACTED_SECRET]", "field-[REDACTED_SECRET] [2]"]
    assert list(observed_result.values()) == ["first-captured-value", "second-captured-value"]
    assert len(observed_result) == 2


@pytest.mark.parametrize("has_workflow", [True, False], ids=["with-workflow", "without-workflow"])
def test_delivered_unverified_exit_omits_depth_over_budget_without_recursion(has_workflow: bool) -> None:
    ctx = _ctx()
    if has_workflow:
        ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
        ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    else:
        ctx.last_run_blocks_workflow_run_id = "wr_deep_fallback"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.delivered_unverified_terminal = True
    deeply_nested: dict[str, Any] = {"leaf": "deep-captured-value"}
    for index in range(1_101):
        deeply_nested = {f"level_{index}": deeply_nested}
    ctx.delivered_unverified_observed_outputs = {"result": deeply_nested}

    result = _build_wip_exit_result(
        ctx,
        None,
        default_reply="default",
        unvalidated_reply="unvalidated",
        tested_reply="tested",
        terminal_reason="turn_halt:delivered_unverified",
    )

    assert result.user_response
    assert "not independently verified" in result.user_response
    assert result.narrative_payload is not None
    observed_outputs = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed_outputs["$skyvernOutput"]["omitted"]["depth"] >= 1
    assert "deep-captured-value" not in str(observed_outputs)


def test_delivered_unverified_exit_sanitizes_both_output_surfaces() -> None:
    ctx = _ctx()
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.append("registered-secret-value")
    deeply_nested: dict[str, Any] = {"password": "deep-must-not-persist"}
    for index in range(24):
        deeply_nested = {f"level_{index}": deeply_nested}
    ctx.delivered_unverified_observed_outputs = {
        "result": {
            "empty": "",
            "password": "must-not-persist",
            "registered": "prefix registered-secret-value suffix",
            "raw": "api_key=sk-1234567890abcdefghijkl",
            "internal": "run wr_internal_123 with update_workflow",
            "deep": deeply_nested,
            "api_key=sk-raw-secret-key-1234567890": "safe-value",
            7: "non-string-key-value",
        }
    }

    result = _build_wip_exit_result(
        ctx,
        None,
        default_reply="default",
        unvalidated_reply="unvalidated",
        tested_reply="tested",
        terminal_reason="turn_halt:delivered_unverified",
    )

    assert 'result["empty"]: ""' in result.user_response
    assert 'result["password"]: ****' in result.user_response
    assert "registered-secret-value" not in result.user_response
    assert "sk-1234567890abcdefghijkl" not in result.user_response
    assert "wr_internal_123" not in result.user_response
    assert "update_workflow" not in result.user_response
    assert "deep-must-not-persist" not in result.user_response
    assert "sk-raw-secret-key-1234567890" not in result.user_response
    assert "non-string-key-value" not in result.user_response
    assert result.narrative_payload is not None
    assert "must-not-persist" not in str(result.narrative_payload)
    assert "registered-secret-value" not in str(result.narrative_payload)
    assert "sk-1234567890abcdefghijkl" not in str(result.narrative_payload)
    assert "wr_internal_123" not in str(result.narrative_payload)
    assert "update_workflow" not in str(result.narrative_payload)
    assert "deep-must-not-persist" not in str(result.narrative_payload)
    assert "sk-raw-secret-key-1234567890" not in str(result.narrative_payload)
    assert "non-string-key-value" not in str(result.narrative_payload)


@pytest.mark.parametrize("has_workflow", [True, False], ids=["with-workflow", "without-workflow"])
def test_delivered_unverified_exit_threads_one_snapshot_to_prose_and_payload(
    monkeypatch: pytest.MonkeyPatch,
    has_workflow: bool,
) -> None:
    ctx = _ctx()
    if has_workflow:
        ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
        ctx.last_workflow_yaml = "workflow_definition:\n  blocks: []\n"
    else:
        ctx.last_run_blocks_workflow_run_id = "wr_single_snapshot_fallback"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = False
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"result": {"amount": 0}}
    sanitize = agent_module._delivered_unverified_observed_outputs
    make_agent_result = agent_module._make_agent_result
    delivered_unverified_reply = agent_module._delivered_unverified_reply
    calls = 0
    prose_snapshot: dict[str, Any] | None = None
    payload_snapshot: dict[str, Any] | None = None

    def count_sanitization(context: CopilotContext) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return sanitize(context)

    def capture_prose_snapshot(context: CopilotContext, snapshot: dict[str, Any]) -> str | None:
        nonlocal prose_snapshot
        prose_snapshot = snapshot
        return delivered_unverified_reply(context, snapshot)

    def capture_payload_snapshot(*args: Any, **kwargs: Any) -> AgentResult:
        nonlocal payload_snapshot
        payload_snapshot = kwargs.get("_delivered_unverified_snapshot")
        return make_agent_result(*args, **kwargs)

    monkeypatch.setattr(agent_module, "_delivered_unverified_observed_outputs", count_sanitization)
    monkeypatch.setattr(agent_module, "_delivered_unverified_reply", capture_prose_snapshot)
    monkeypatch.setattr(agent_module, "_make_agent_result", capture_payload_snapshot)

    result = _build_wip_exit_result(
        ctx,
        None,
        default_reply="default",
        unvalidated_reply="unvalidated",
        tested_reply="tested",
        terminal_reason="turn_halt:delivered_unverified",
    )

    assert calls == 1
    assert prose_snapshot is payload_snapshot
    assert 'result["amount"]: 0' in result.user_response
    assert result.proposal_disposition == "review_untested"
    assert result.narrative_payload is not None
    assert result.narrative_payload["deliveredUnverifiedObservedOutputs"]["result"] == {"amount": 0}


def test_verified_outcome_does_not_suppress_voluntary_terminal_challenge() -> None:
    ctx = _ctx()
    _seed_verified_outcome(ctx)
    challenge_text = "The site requires a verification challenge I can't complete on my own."
    ctx.blocker_signal = _signal(
        kind="tool_error",
        user_facing=challenge_text,
        recovery_hint="stop",
        internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        blocked_tool="update_and_run_blocks",
    )

    overridden = _finalize_result_with_blocker_override(ctx, _agent_result())

    assert overridden.user_response == challenge_text
    assert overridden.user_response != _VERIFIED_WORKFLOW_SUCCESS_REPLY
    assert overridden.proposal_disposition != "review_tested"


def _churn_render_signal() -> CopilotToolBlockerSignal:
    return _signal(
        kind="loop_detected",
        user_facing="I kept rewriting the generated code, but the safety checks rejected each version.",
        internal_reason_code="code_authoring_guardrail_churn",
    )


def test_shim_denies_churn_render_while_ladder_owns_and_records_conflict() -> None:
    ctx = _ctx()
    signal = _churn_render_signal()
    claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, signal)
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED
    original = _agent_result("Model reply describing progress.")

    result = _finalize_result_with_blocker_override(ctx, original)

    assert result is original
    assert any(
        event.site == "final_reply_render"
        and event.fingerprint == "output_contract_actuation>code_authoring_guardrail_churn"
        for event in ctx.gate_precedence_conflict_events
    )


def test_shim_renders_churn_after_ladder_resolves() -> None:
    ctx = _ctx()
    signal = _churn_render_signal()
    claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, signal)
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.CONSUMED
    original = _agent_result("Model reply describing progress.")

    result = _finalize_result_with_blocker_override(ctx, original)

    assert result is not original
    assert signal.user_facing_reason in result.user_response


def test_credential_priority_churn_still_renders_when_no_ladder_owns() -> None:
    ctx = _ctx()
    signal = _signal(
        kind="loop_detected",
        user_facing=CREDENTIAL_SCOUT_VERIFY_REPLY,
        internal_reason_code="credential_priority_authoring_churn",
    )
    claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, signal)

    result = _finalize_result_with_blocker_override(ctx, _agent_result())

    assert CREDENTIAL_SCOUT_VERIFY_REPLY in result.user_response


def test_output_policy_blocked_result_surfaces_draft_when_blocker_render_denied() -> None:
    ctx = _ctx()
    fake_workflow: Any = object()
    ctx.last_workflow = fake_workflow
    ctx.last_workflow_yaml = "title: X\nworkflow_definition:\n  blocks: []\n"
    claim_and_stash_blocker_signal(ctx, TurnClaimant.CODE_AUTHORING_CHURN, _churn_render_signal())
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED

    result = _blocked_result(ctx, OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE)

    assert result.updated_workflow is fake_workflow
    assert any(event.site == "final_reply_render" for event in ctx.gate_precedence_conflict_events)


def test_reconcile_turn_end_replaces_result_with_stalled_terminal() -> None:
    ctx = _ctx()
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED
    ctx.output_contract_pending_run_evidence["sig_a"] = ["output.confirmation_number"]
    original = _agent_result("Model reply describing progress.")

    result = agent_module._reconcile_turn_end_ownership(ctx, original)

    assert result is not original
    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind is TurnHaltKind.OUTPUT_SOURCE_UNOBSERVABLE
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.EXPIRED
    assert "I couldn't shape this workflow" in result.user_response


def test_reconcile_turn_end_noop_without_live_grant() -> None:
    ctx = _ctx()
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.CONSUMED
    original = _agent_result()

    assert agent_module._reconcile_turn_end_ownership(ctx, original) is original


def test_reconcile_turn_end_failure_never_masks_original_result(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED

    def _boom(_: Any) -> None:
        raise RuntimeError("reconcile failure")

    monkeypatch.setattr(agent_module, "expire_output_contract_ladder_at_turn_end", _boom)
    original = _agent_result()

    assert agent_module._reconcile_turn_end_ownership(ctx, original) is original


def _stalled_chat_request() -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_xyz",
        workflow_id="w_001",
        workflow_copilot_chat_id="chat_abc",
        message="build the workflow",
        workflow_yaml="",
    )


@pytest.mark.asyncio
async def test_turn_end_obligation_transforms_returned_result_on_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[CopilotContext] = []

    async def stub_impl(*, ctx_sink: list[CopilotContext], **_: Any) -> AgentResult:
        ctx = _ctx()
        ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED
        ctx_sink.append(ctx)
        captured.append(ctx)
        return _agent_result("Model reply describing progress.")

    monkeypatch.setattr(agent_module, "_run_copilot_turn_impl", stub_impl)

    result = await agent_module.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=_stalled_chat_request(),
        chat_history=[],
        global_llm_context=None,
        debug_run_info_text="",
        llm_api_handler=None,
    )

    ctx = captured[0]
    assert ctx.turn_halt is not None
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.EXPIRED
    assert "I couldn't shape this workflow" in result.user_response


@pytest.mark.asyncio
async def test_turn_end_obligation_preserves_error_terminal_and_expires_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[CopilotContext] = []

    async def stub_impl(*, ctx_sink: list[CopilotContext], **_: Any) -> AgentResult:
        ctx = _ctx()
        ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED
        ctx_sink.append(ctx)
        captured.append(ctx)
        raise RuntimeError("mid-turn failure")

    monkeypatch.setattr(agent_module, "_run_copilot_turn_impl", stub_impl)

    result = await agent_module.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=_stalled_chat_request(),
        chat_history=[],
        global_llm_context=None,
        debug_run_info_text="",
        llm_api_handler=None,
    )

    ctx = captured[0]
    assert ctx.turn_halt is None
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.EXPIRED
    assert "I couldn't shape this workflow" not in result.user_response


def test_reconcile_turn_end_keeps_ask_question_result_and_expires_grant() -> None:
    ctx = _ctx()
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED
    original = AgentResult(
        user_response="Which site should this run against?",
        updated_workflow=None,
        global_llm_context=None,
        response_type="ASK_QUESTION",
    )

    result = agent_module._reconcile_turn_end_ownership(ctx, original)

    assert result is original
    assert ctx.turn_halt is None
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.EXPIRED


def test_reconcile_turn_end_keeps_cancelled_result_and_expires_grant() -> None:
    ctx = _ctx()
    ctx.output_contract_actuation_by_signature["sig_a"] = OutputContractAdvisoryState.GRANTED
    original = AgentResult(
        user_response="Cancelled by user.",
        updated_workflow=None,
        global_llm_context=None,
        cancelled=True,
    )

    result = agent_module._reconcile_turn_end_ownership(ctx, original)

    assert result is original
    assert ctx.turn_halt is None
    assert ctx.output_contract_actuation_by_signature["sig_a"] == OutputContractAdvisoryState.EXPIRED
