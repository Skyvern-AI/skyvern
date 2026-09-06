from __future__ import annotations

from types import SimpleNamespace
from typing import Any, get_args

import pytest

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.agent import (
    _FALLBACK_BLOCKER_REPLY,
    _RAW_SECRET_LEAK_REFUSAL,
    _build_output_policy_blocked_result,
)
from skyvern.forge.sdk.copilot.agent import _build_turn_halt_exit_result as _build_turn_halt_exit_result
from skyvern.forge.sdk.copilot.agent import (
    _finalize_result_with_blocker_override,
    _render_blocker_reply,
    _runtime_self_heal_success_reply,
)
from skyvern.forge.sdk.copilot.blocker_signal import (
    _LEAK_DENY_TOKENS,
    BlockerKind,
    CopilotToolBlockerSignal,
    RecoveryHint,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext
from skyvern.forge.sdk.copilot.interruption import MINIMAL_CANCEL_STOP
from skyvern.forge.sdk.copilot.output_policy import (
    CopilotOutputKind,
    OutputPolicyReason,
    OutputPolicyVerdict,
)
from skyvern.forge.sdk.copilot.request_policy import LivePageResolutionRecord, RequestPolicy
from skyvern.forge.sdk.copilot.review_gate import workflow_block_fingerprints
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.turn_halt import TurnHalt, TurnHaltKind
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ConnectedAccountChoice, ResponseKind, TurnOutcome
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


_COVERED_DRAFT_YAML = """title: Draft
workflow_definition:
  parameters: []
  blocks:
  - block_type: task
    label: step
    prompt: Do it
"""


def _signal(
    *,
    kind: BlockerKind = "authority_denied",
    user_facing: str = "I can't update or run this workflow on this turn.",
    recovery_hint: RecoveryHint = "report_blocker_to_user",
    blocked_tool: str = "update_workflow",
    internal_reason_code: str = "no_mutation_run_blocked",
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


def _agent_result(user_response: str = "Agent prose reply with leaked internal vocab.") -> AgentResult:
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
        reason_code="no_meaningful_output",
        display_reason="The requested record was not verified.",
        workflow_run_id=run_id,
    )


@pytest.mark.parametrize("recovery_hint", _ALL_RECOVERY_HINTS)
def test_render_picks_response_type_from_hint(recovery_hint: RecoveryHint) -> None:
    signal = _signal(recovery_hint=recovery_hint)
    user_response, resp_type = _render_blocker_reply(signal)
    expected_resp_type = "ASK_QUESTION" if recovery_hint == "ask_user_clarifying" else "REPLY"
    assert resp_type == expected_resp_type
    assert user_response == signal.user_facing_reason


def test_google_run_gate_blocker_attaches_fresh_server_verified_choices() -> None:
    ctx = _ctx()
    ctx.connected_account_recovery_choices = [
        ConnectedAccountChoice(connection_id="goac_first", name="First account", state="active")
    ]
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Ask the user to choose a connected Google account.",
        user_facing_reason="Choose one of the connected Google accounts below so I can run the workflow.",
        recovery_hint="ask_user_clarifying",
        internal_reason_code="unapproved_google_connection_reference",
        blocked_tool="update_and_run_blocks",
        preserves_workflow_draft=True,
    )

    result = _finalize_result_with_blocker_override(ctx, _agent_result())

    assert result.user_response == "Choose one of the connected Google accounts below so I can run the workflow."
    assert result.turn_outcome is not None
    assert result.turn_outcome.connected_account_choices == ctx.connected_account_recovery_choices
    assert "goac_" not in result.user_response
    assert "unapproved_credential_reference" not in result.user_response


def test_render_falls_back_when_template_leaks() -> None:
    # `model_construct` bypasses the @model_validator that blocks leaks at construction so we can exercise the renderer's defense-in-depth fallback.
    signal = CopilotToolBlockerSignal.model_construct(
        blocker_kind="authority_denied",
        agent_steering_text="Reply without updating.",
        user_facing_reason="LOOP DETECTED: this should have been curated out.",
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=False,
        internal_reason_code="no_mutation_run_blocked",
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


def test_output_policy_generic_block_uses_only_safety_and_draft_evidence() -> None:
    ctx = _ctx()
    fake_workflow: Any = object()
    ctx.last_workflow = fake_workflow
    ctx.last_workflow_yaml = _COVERED_DRAFT_YAML
    ctx.executed_block_fingerprints = {
        label: set(values) for label, values in workflow_block_fingerprints(_COVERED_DRAFT_YAML).items()
    }
    ctx.workflow_persisted = True
    ctx.last_test_ok = True
    _seed_terminal_evidence(ctx)
    ctx.last_test_anti_bot = "challenge-gated disabled submit/search control"

    result = _blocked_result(ctx, OutputPolicyReason.PERSISTENCE_STATE_MISMATCH)

    assert result.response_type == "ASK_QUESTION"
    assert result.updated_workflow is fake_workflow
    assert result.proposal_disposition == "review_untested"
    assert "latest run recorded workflow output" not in result.user_response
    assert "did not demonstrate" not in result.user_response
    assert "verification challenge" in result.user_response
    assert "workflow draft is still saved" in result.user_response
    assert "wr_latest" not in result.user_response
    assert result.narrative_payload is not None
    assert result.narrative_payload["terminalMessage"] == result.user_response
    assert result.narrative_payload["responseType"] == "ASK_QUESTION"


def test_output_policy_recorded_evidence_does_not_create_a_policy_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    _seed_terminal_evidence(ctx)
    seen_output_kinds: list[CopilotOutputKind] = []

    def allow_policy(**kwargs: Any) -> OutputPolicyVerdict:
        seen_output_kinds.append(kwargs["output_kind"])
        return OutputPolicyVerdict(allowed=True, output_kind=kwargs["output_kind"], reason_codes=[])

    monkeypatch.setattr(agent_module, "evaluate_output_policy", allow_policy)

    result = _blocked_result(
        ctx,
        OutputPolicyReason.PERSISTENCE_STATE_MISMATCH,
        output_kind=CopilotOutputKind.REFUSAL,
    )

    assert result.response_type == "ASK_QUESTION"
    assert seen_output_kinds == []


def test_output_policy_generic_block_requires_clean_terminal_evidence() -> None:
    no_recorded = _ctx()
    adversarial = _ctx()
    adversarial.last_run_blocks_workflow_run_id = "wr_hidden"

    for ctx in (no_recorded, adversarial):
        result = _blocked_result(ctx, OutputPolicyReason.PERSISTENCE_STATE_MISMATCH)
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


def test_output_policy_raw_secret_hard_block_uses_safety_reply() -> None:
    ctx = _ctx()

    result = _blocked_result(ctx, OutputPolicyReason.RAW_SECRET_LEAK)

    assert result.user_response == _RAW_SECRET_LEAK_REFUSAL
    assert result.turn_outcome is not None
    assert result.turn_outcome.reason_code == "output_policy_block"
    assert result.turn_outcome.terminal_reason == "output_policy_block"


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


def test_turn_halt_exit_renders_terminal_reason_when_terminal_blocker_held() -> None:
    ctx = _ctx()
    terminal = _signal(
        kind="tool_error",
        user_facing="The browser session was lost before the run finished.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="tool_error_browser_session_lost",
        blocked_tool="update_and_run_blocks",
    )
    ctx.blocker_signal = terminal
    halt = TurnHalt(kind=TurnHaltKind.BROWSER_SESSION_LOST, blocker_signal=terminal)

    result = _build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

    assert result.user_response == terminal.user_facing_reason


def test_turn_halt_exit_never_makes_an_interactive_tested_draft_auto_applicable() -> None:
    ctx = _ctx()
    workflow = object()
    ctx.last_workflow = workflow
    ctx.last_workflow_yaml = "title: tested\nworkflow_definition:\n  blocks: []\n"
    ctx.last_test_ok = True
    ctx.last_full_workflow_test_ok = True
    terminal = _signal(
        kind="tool_error",
        user_facing="The browser session was lost after the test.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="tool_error_browser_session_lost",
        blocked_tool="update_and_run_blocks",
    )
    halt = TurnHalt(kind=TurnHaltKind.BROWSER_SESSION_LOST, blocker_signal=terminal)

    result = _build_turn_halt_exit_result(ctx, global_llm_context=None, halt=halt)

    assert result.updated_workflow is workflow
    assert result.proposal_disposition == "review_untested"


def _seed_verified_outcome(ctx: CopilotContext) -> None:
    ctx.last_run_blocks_workflow_run_id = "wr_test"
    ctx.last_run_outcome = RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_test")
    ctx.last_artifact_health_blocker_reason = None
    ctx.last_workflow = SimpleNamespace(workflow_definition=SimpleNamespace(blocks=[SimpleNamespace()]))
    ctx.last_workflow_yaml = "title: built\nblocks: []\n"


def test_runtime_self_heal_reply_never_echoes_run_output() -> None:
    ctx = _ctx()
    ctx.turn_origin = TurnOrigin.runtime_self_heal

    response = _runtime_self_heal_success_reply(ctx)

    assert response == "The unattended recovery check completed."
    assert "secret-value" not in response


def test_interactive_authoring_cannot_request_a_server_authored_success_reply() -> None:
    ctx = _ctx()
    with pytest.raises(RuntimeError, match="interactive authoring"):
        _runtime_self_heal_success_reply(ctx)


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


def test_verified_outcome_does_not_suppress_voluntary_terminal_challenge() -> None:
    ctx = _ctx()
    _seed_verified_outcome(ctx)
    challenge_text = "The site requires a verification challenge I can't complete on my own."
    ctx.blocker_signal = _signal(
        kind="tool_error",
        user_facing=challenge_text,
        recovery_hint="stop",
        internal_reason_code="tool_error_terminal_challenge_blocker",
        blocked_tool="update_and_run_blocks",
    )

    overridden = _finalize_result_with_blocker_override(ctx, _agent_result())

    assert overridden.user_response == challenge_text
    assert overridden.proposal_disposition != "review_tested"


def test_unapproved_credential_reference_asks_without_naming_the_credential_inventory() -> None:
    """`discovered_credentials` holds everything `list_credentials` returned, not page matches,
    so the reply must not present it as a match set."""
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        discovered_credentials=[SimpleNamespace(credential_id="cred_unrelated", name="hr portal", tested_url=None)]
    )

    result = _blocked_result(ctx, OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE)

    assert "I need an approved credential to continue" in result.user_response
    # Only the observation seam records a verdict, so no-verdict must not be rendered as "no match":
    # a fill-seam ambiguity reaches this branch with a real match behind it.
    assert "could not match" not in result.user_response
    assert "Credentials UI" in result.user_response
    assert "cred_unrelated" not in result.user_response
    assert "hr portal" not in result.user_response


def test_unapproved_credential_reference_ambiguous_does_not_enumerate_candidates() -> None:
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        live_page_resolution=LivePageResolutionRecord(
            verdict="ambiguous",
            tier="url_path",
            candidates=(
                SimpleNamespace(credential_id="cred_first", name="first login"),
                SimpleNamespace(credential_id="cred_second", name="second login"),
            ),
        )
    )

    result = _blocked_result(ctx, OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE)

    # The ambiguous branch used to list candidate ids; the FE now renders the full org selector, so the
    # reply is one sentence with the Credentials UI marker and no prose dump.
    assert "I need an approved credential to continue" in result.user_response
    assert "Credentials UI" in result.user_response
    assert "cred_first" not in result.user_response
    assert "cred_second" not in result.user_response


def test_unapproved_credential_reference_points_at_credentials_ui_when_nothing_matched() -> None:
    """A no-match verdict must not borrow the ambiguous reply's candidate list."""
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(
        live_page_resolution=LivePageResolutionRecord(
            verdict="no_match",
            page_url="https://analytics.example.com/login",
            candidates=(SimpleNamespace(credential_id="cred_unmatched", name="unmatched login"),),
        )
    )

    result = _blocked_result(ctx, OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE)

    assert "Credentials UI" in result.user_response
    # Candidates ride on the record whatever the verdict; only an ambiguous one may name them.
    assert "cred_unmatched" not in result.user_response
    assert "More than one saved credential" not in result.user_response


def test_unapproved_google_connection_preserves_verified_clickable_choices() -> None:
    choices = [
        ConnectedAccountChoice(connection_id="goac_active", name="Sheets", state="active"),
        ConnectedAccountChoice(connection_id="goac_inactive", name="Sheets", state="error"),
    ]
    ctx = _ctx()
    ctx.prior_turn_outcome = TurnOutcome(
        response_kind=ResponseKind.CLARIFY,
        connected_account_choices=choices,
    )
    ctx.request_policy = RequestPolicy(existing_workflow_credential_ids=["goac_active"])

    result = _blocked_result(ctx, OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE)

    assert "connected Google account" in result.user_response
    assert "Credentials UI" not in result.user_response
    assert "unapproved_credential_reference" not in result.user_response
    assert "goac_" not in result.user_response
    assert result.turn_outcome is not None
    assert result.turn_outcome.connected_account_choices == choices


def test_password_blocker_does_not_reuse_prior_google_choices() -> None:
    choices = [ConnectedAccountChoice(connection_id="goac_active", name="Sheets", state="active")]
    ctx = _ctx()
    ctx.prior_turn_outcome = TurnOutcome(
        response_kind=ResponseKind.CLARIFY,
        connected_account_choices=choices,
    )
    ctx.request_policy = RequestPolicy(
        existing_workflow_credential_ids=["goac_active", "cred_password"],
        run_approved_google_connection_ids=["goac_active"],
    )

    result = _blocked_result(ctx, OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE)

    assert "Credentials UI" in result.user_response
    assert "connected Google account" not in result.user_response
    assert result.turn_outcome is not None
    assert result.turn_outcome.connected_account_choices is None


def test_shim_over_a_cancelled_turn_keeps_the_stop_label() -> None:
    ctx = _ctx()
    ctx.blocker_signal = _signal()
    result = AgentResult(
        user_response=MINIMAL_CANCEL_STOP,
        updated_workflow=None,
        global_llm_context=None,
        cancelled=True,
    )

    overridden = _finalize_result_with_blocker_override(ctx, result)

    assert overridden.turn_outcome is not None
    assert overridden.turn_outcome.response_kind is ResponseKind.RECOVER
    assert overridden.turn_outcome.response_kind is not ResponseKind.CLARIFY
    assert overridden.turn_outcome.terminal_reason == "cancel"


def test_shim_over_an_uncancelled_turn_still_records_clarify() -> None:
    ctx = _ctx()
    ctx.blocker_signal = _signal()

    overridden = _finalize_result_with_blocker_override(ctx, _agent_result())

    assert overridden.turn_outcome is not None
    assert overridden.turn_outcome.response_kind is ResponseKind.CLARIFY
    assert overridden.turn_outcome.terminal_reason is None
