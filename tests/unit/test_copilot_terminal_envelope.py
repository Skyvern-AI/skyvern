from __future__ import annotations

import time
from typing import Any

import pytest
import structlog.testing

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    clear_terminal_evidence_on_workflow_edit,
)
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.terminal_envelope import (
    TerminalOutcomeEnvelope,
    assemble_terminal_envelope,
    finalize_applied_state,
    reason_in_reply_shadow,
    render_terminal_message,
)
from skyvern.forge.sdk.copilot.tools.run_execution import _stash_recorded_run_outcome
from tests.unit.copilot_test_helpers import make_copilot_ctx


def _run_outcome(verdict: str, display_reason: str | None = None) -> RecordedRunOutcome:
    return RecordedRunOutcome(verdict=verdict, display_reason=display_reason)


def _assemble(**overrides: Any):
    defaults = {
        "response_type": "REPLY",
        "verified": False,
        "workflow_applied": False,
        "proposal_disposition": "no_proposal",
        "run_outcomes": [],
        "blocker_reason": None,
        "halt_kind": None,
        "attempted": None,
        "workflow_mutated": False,
        "turn_outcome_response_kind": None,
    }
    defaults.update(overrides)
    envelope = assemble_terminal_envelope(**defaults)
    assert envelope is not None
    return envelope


def test_run_anchor_prefers_last_not_demonstrated_even_if_later_run_demonstrated() -> None:
    envelope = _assemble(
        run_outcomes=[
            _run_outcome("not_demonstrated", "The checkout did not reach confirmation."),
            _run_outcome("demonstrated", "A later scout run succeeded."),
        ]
    )

    assert envelope.run_verdict == "not_demonstrated"
    assert envelope.run_display_reason == "The checkout did not reach confirmation."


def test_run_anchor_falls_back_to_latest_final_verdict_when_no_not_demonstrated() -> None:
    envelope = _assemble(
        run_outcomes=[
            _run_outcome("not_evaluated", "Could not evaluate."),
            _run_outcome("demonstrated", "Confirmed."),
        ]
    )

    assert envelope.run_verdict == "demonstrated"
    assert envelope.run_display_reason == "Confirmed."


def test_run_anchor_empty_when_no_recorded_outcomes() -> None:
    envelope = _assemble(run_outcomes=[])
    assert envelope.run_verdict is None
    assert envelope.run_display_reason is None


def test_unknown_halt_kind_degrades_to_stopped_never_question() -> None:
    envelope = _assemble(
        halt_kind="capture_obligation_reopen",
        blocker_reason="Capture obligation reopened mid-turn.",
    )

    assert envelope.response_kind == "stopped"
    assert envelope.next_state == "stopped"
    assert envelope.halt_kind == "capture_obligation_reopen"

    finalized = finalize_applied_state(envelope, applied=False)
    assert finalized.response_kind == "stopped"
    assert finalized.next_state == "stopped"


def test_anchor_supersession_divergence_is_logged() -> None:
    with structlog.testing.capture_logs() as logs:
        _assemble(
            run_outcomes=[
                _run_outcome("not_demonstrated", "The checkout did not reach confirmation."),
                _run_outcome("demonstrated", "A later scout run succeeded."),
            ]
        )
    assert any("anchored a not_demonstrated verdict" in log["event"] for log in logs)

    with structlog.testing.capture_logs() as logs:
        _assemble(run_outcomes=[_run_outcome("not_demonstrated", "No later run.")])
    assert not any("anchored a not_demonstrated verdict" in log["event"] for log in logs)


def _interim_outcome(verdict: str, display_reason: str | None = None) -> RecordedRunOutcome:
    return RecordedRunOutcome(verdict=verdict, display_reason=display_reason, role="interim_build_test")


def test_run_anchor_ignores_interim_not_demonstrated_when_adjudicated_run_follows() -> None:
    # Happy turn: an interim scout test run goes not_demonstrated mid-build, then the
    # completed workflow demonstrates the goal. The envelope must not resurface the
    # interim amber over the later adjudicated success.
    envelope = _assemble(
        run_outcomes=[
            _interim_outcome("not_demonstrated", "The scout has not produced the goal yet."),
            _run_outcome("demonstrated", "The extraction returned the value."),
        ]
    )

    assert envelope.run_verdict == "demonstrated"
    assert envelope.run_display_reason == "The extraction returned the value."


def test_run_anchor_keeps_interim_amber_when_no_adjudicated_outcome() -> None:
    # Repair ceiling: the loop stops after a suspicious-success run without ever
    # producing an adjudicated outcome, so the interim not_demonstrated is the turn's
    # honest terminal verdict and must still anchor amber.
    envelope = _assemble(
        run_outcomes=[
            _interim_outcome("not_demonstrated", "The run completed but did not demonstrate the goal."),
        ]
    )

    assert envelope.run_verdict == "not_demonstrated"
    assert envelope.run_display_reason == "The run completed but did not demonstrate the goal."


def test_run_anchor_prefers_adjudicated_not_demonstrated_over_earlier_interim() -> None:
    # A genuine adjudicated failure on the completed workflow anchors amber even when an
    # earlier interim run also went not_demonstrated.
    envelope = _assemble(
        run_outcomes=[
            _interim_outcome("not_demonstrated", "Interim scout, still building."),
            _run_outcome("not_demonstrated", "The extraction returned no value."),
        ]
    )

    assert envelope.run_verdict == "not_demonstrated"
    assert envelope.run_display_reason == "The extraction returned no value."


@pytest.mark.parametrize(
    ("response_type", "verified", "workflow_applied", "proposal_disposition", "expected_next_state"),
    [
        ("ASK_QUESTION", False, False, "no_proposal", "awaiting_user_input"),
        ("REPLY", True, True, "no_proposal", "completed"),
        ("REPLY", False, False, "review_tested", "proposal_pending"),
        ("REPLY", False, False, "review_required", "stopped"),
        ("REPLY", True, False, "auto_applicable", "stopped"),
    ],
)
def test_next_state_derivation(
    response_type: str,
    verified: bool,
    workflow_applied: bool,
    proposal_disposition: str,
    expected_next_state: str,
) -> None:
    envelope = _assemble(
        response_type=response_type,
        verified=verified,
        workflow_applied=workflow_applied,
        proposal_disposition=proposal_disposition,
    )
    assert envelope.next_state == expected_next_state


@pytest.mark.parametrize(
    ("kwargs", "expected_response_kind"),
    [
        ({"response_type": "ASK_QUESTION"}, "question"),
        ({"verified": True, "workflow_applied": True}, "update"),
        ({"proposal_disposition": "review_untested"}, "update"),
        ({"turn_outcome_response_kind": "answer", "workflow_mutated": False}, "answer"),
        ({"turn_outcome_response_kind": "diagnose", "workflow_mutated": False}, "answer"),
        ({"turn_outcome_response_kind": "diagnose", "workflow_mutated": True}, "stopped"),
        ({"turn_outcome_response_kind": "refuse", "workflow_mutated": False}, "answer"),
        ({"turn_outcome_response_kind": "refuse", "workflow_mutated": True}, "stopped"),
        ({"turn_outcome_response_kind": "recover", "workflow_mutated": False}, "answer"),
        ({"turn_outcome_response_kind": "recover", "workflow_mutated": True}, "stopped"),
        ({"turn_outcome_response_kind": "build", "workflow_mutated": False}, "stopped"),
    ],
)
def test_response_kind_derivation(kwargs: dict[str, Any], expected_response_kind: str) -> None:
    envelope = _assemble(**kwargs)
    assert envelope.response_kind == expected_response_kind


def test_user_action_required_derivation() -> None:
    assert _assemble(response_type="ASK_QUESTION").user_action_required is True
    assert _assemble(response_type="REPLY").user_action_required is False


def test_blocker_fields_attempted_and_envelope_version() -> None:
    envelope = _assemble(
        blocker_reason="  Need account credentials.  ",
        halt_kind="  loop_detected  ",
        attempted="  Attempted full checkout run.  ",
    )

    assert envelope.blocker_reason == "Need account credentials."
    assert envelope.halt_kind == "loop_detected"
    assert envelope.attempted == "Attempted full checkout run."
    assert envelope.rendered_from_envelope is False
    assert envelope.envelope_version == 1


def test_finalize_applied_state_promotes_completed_when_verified_and_applied() -> None:
    envelope = _assemble(verified=True, workflow_applied=False, proposal_disposition="no_proposal")

    finalized = finalize_applied_state(envelope, applied=True)

    assert finalized.workflow_applied is True
    assert finalized.next_state == "completed"
    assert finalized.response_kind == "update"


def test_finalize_applied_state_blocks_completed_when_not_applied() -> None:
    envelope = _assemble(verified=True, workflow_applied=True, proposal_disposition="no_proposal")

    finalized = finalize_applied_state(envelope, applied=False)

    assert finalized.workflow_applied is False
    assert finalized.next_state == "stopped"
    assert finalized.response_kind == "stopped"


def test_finalize_applied_state_marks_unapplied_proposal_pending() -> None:
    # A verified code-only fix is auto_applicable but no longer auto-commits; when
    # not applied it must render as a pending proposal (ReviewGateCard), not stopped.
    envelope = _assemble(verified=True, workflow_applied=False, proposal_disposition="auto_applicable")
    assert envelope.next_state == "stopped"

    finalized = finalize_applied_state(envelope, applied=False, proposal_present=True)

    assert finalized.workflow_applied is False
    assert finalized.next_state == "proposal_pending"
    assert finalized.response_kind == "update"

    # No proposal present (a genuine stop) stays stopped.
    assert finalize_applied_state(envelope, applied=False).next_state == "stopped"


def test_finalize_applied_state_keeps_question_for_user_action_required() -> None:
    envelope = _assemble(
        response_type="ASK_QUESTION", verified=True, workflow_applied=False, proposal_disposition="no_proposal"
    )

    finalized = finalize_applied_state(envelope, applied=True)

    assert finalized.workflow_applied is True
    assert finalized.next_state == "awaiting_user_input"
    assert finalized.response_kind == "question"


def test_finalize_applied_state_preserves_answer_when_not_promoted_to_update() -> None:
    envelope = _assemble(turn_outcome_response_kind="answer", workflow_mutated=False)
    assert envelope.response_kind == "answer"

    finalized = finalize_applied_state(envelope, applied=False)

    assert finalized.next_state == "stopped"
    assert finalized.response_kind == "answer"


def test_terminal_envelope_outcomes_survive_per_run_pointer_reset() -> None:
    ctx = make_copilot_ctx()
    first = RecordedRunOutcome(
        verdict="not_demonstrated",
        display_reason="Checkout never reached confirmation.",
        workflow_run_id="wr_first",
    )
    second = RecordedRunOutcome(
        verdict="demonstrated",
        display_reason="A later scout replay succeeded.",
        workflow_run_id="wr_second",
    )

    _stash_recorded_run_outcome(ctx, first)
    # _record_run_blocks_result resets the pointer before processing each new
    # run in the turn; the trace must survive it or the anchor never sees the
    # earlier failure.
    ctx.last_run_outcome = None
    ctx.last_run_outcome_block_labels = []
    _stash_recorded_run_outcome(ctx, second)
    outcomes = agent_module._terminal_envelope_run_outcomes(ctx)

    assert [outcome.verdict for outcome in outcomes] == ["not_demonstrated", "demonstrated"]
    assert outcomes[0].display_reason == "Checkout never reached confirmation."
    assert outcomes[1].display_reason == "A later scout replay succeeded."

    envelope = _assemble(run_outcomes=outcomes)

    assert envelope.run_verdict == "not_demonstrated"
    assert envelope.run_display_reason == "Checkout never reached confirmation."


def test_terminal_envelope_outcomes_seed_from_constructor_last_run_outcome() -> None:
    first = RecordedRunOutcome(
        verdict="not_demonstrated",
        display_reason="Seeded from constructor.",
        workflow_run_id="wr_ctor",
    )
    second = RecordedRunOutcome(
        verdict="demonstrated",
        display_reason="Appended after construction.",
        workflow_run_id="wr_runtime",
    )
    ctx = make_copilot_ctx(last_run_outcome=first)

    assert ctx.terminal_envelope_run_outcomes == [first]

    ctx.last_run_outcome = second

    assert ctx.terminal_envelope_run_outcomes == [first, second]


def test_terminal_envelope_outcomes_clear_on_workflow_edit_evidence_reset() -> None:
    ctx = make_copilot_ctx()
    _stash_recorded_run_outcome(
        ctx,
        RecordedRunOutcome(
            verdict="not_demonstrated",
            display_reason="Checkout never reached confirmation.",
            workflow_run_id="wr_before_reset",
        ),
    )

    clear_terminal_evidence_on_workflow_edit(ctx)
    outcomes = agent_module._terminal_envelope_run_outcomes(ctx)
    envelope = _assemble(run_outcomes=outcomes)

    assert ctx.terminal_envelope_run_outcomes == []
    assert outcomes == []
    assert envelope.run_verdict is None


def test_terminal_envelope_outcomes_reanchor_to_new_outcome_after_workflow_edit() -> None:
    ctx = make_copilot_ctx()
    _stash_recorded_run_outcome(
        ctx,
        RecordedRunOutcome(
            verdict="not_demonstrated",
            display_reason="Old failed run.",
            workflow_run_id="wr_old",
        ),
    )
    clear_terminal_evidence_on_workflow_edit(ctx)

    _stash_recorded_run_outcome(
        ctx,
        RecordedRunOutcome(
            verdict="not_demonstrated",
            display_reason="New failed run after edit.",
            workflow_run_id="wr_new",
        ),
    )
    outcomes = agent_module._terminal_envelope_run_outcomes(ctx)
    envelope = _assemble(run_outcomes=outcomes)

    assert len(outcomes) == 1
    assert outcomes[0].workflow_run_id == "wr_new"
    assert envelope.run_verdict == "not_demonstrated"
    assert envelope.run_display_reason == "New failed run after edit."


def test_reason_in_reply_shadow_normalization() -> None:
    assert reason_in_reply_shadow(
        "Run completed but did not demonstrate the requested outcome.",
        "The latest run completed but did not demonstrate the requested outcome, so I paused.",
    )


def test_safe_wrapper_returns_none_when_assembly_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(**_: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_module, "assemble_terminal_envelope", _raise)
    envelope = agent_module._assemble_terminal_envelope_safe(
        response_type="REPLY",
        verified=False,
        workflow_applied=False,
        proposal_disposition="no_proposal",
        run_outcomes=[],
        blocker_reason=None,
        halt_kind=None,
        attempted=None,
        workflow_mutated=False,
        turn_outcome_response_kind=None,
        final_message="reply",
    )

    assert envelope is None


def test_render_terminal_message_stopped_not_demonstrated_contains_verbatim_reason_without_continuation() -> None:
    reason = "The submit button never enabled after entering all required fields."
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        run_verdict="not_demonstrated",
        run_display_reason=reason,
        response_kind="stopped",
    )

    rendered, replaced = render_terminal_message(envelope, "legacy", cancelled=False)

    assert replaced is True
    assert reason in rendered
    forbidden_phrases = (
        "i'll keep working",
        "i will keep working",
        "i'm still working",
        "keep working on it",
        "next i will",
        "next, i will",
        "going to try again",
    )
    lowered = rendered.lower()
    assert all(phrase not in lowered for phrase in forbidden_phrases)


def test_render_terminal_message_stopped_degraded_envelope_uses_minimal_honest_stop() -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        run_verdict=None,
        run_display_reason=None,
        response_kind="stopped",
    )

    rendered, replaced = render_terminal_message(envelope, "legacy", cancelled=False)

    assert replaced is True
    assert rendered == "I stopped without confirming the goal was met."


def test_render_terminal_message_no_run_blocker_stop_keeps_blocker_evidence() -> None:
    blocker = "The site demands SSO before any page loads."
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        run_verdict=None,
        run_display_reason=None,
        blocker_reason=blocker,
        response_kind="stopped",
    )

    rendered, replaced = render_terminal_message(envelope, "legacy", cancelled=False)

    assert replaced is True
    assert rendered.startswith("I stopped without confirming the goal was met.")
    assert blocker in rendered


@pytest.mark.parametrize(
    ("next_state", "cancelled"),
    [
        ("completed", False),
        ("proposal_pending", False),
        ("awaiting_user_input", False),
        ("stopped", True),
    ],
)
def test_render_terminal_message_passthrough_for_non_stopped_or_cancelled(next_state: str, cancelled: bool) -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state=next_state, verified=False, run_verdict="not_demonstrated", response_kind="stopped"
    )
    message = "keep-agent-message"

    rendered, replaced = render_terminal_message(envelope, message, cancelled=cancelled)

    assert rendered == message
    assert replaced is False


def test_render_terminal_message_keeps_answer_kind_replies_on_stopped_state() -> None:
    # Diagnose/refuse turns end next_state="stopped" with response_kind="answer";
    # their specific reply text is the deliverable and must survive flag-on.
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        run_verdict=None,
        run_display_reason=None,
        response_kind="answer",
    )
    message = "The run failed because the export needs admin rights; here is what that means."

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert rendered == message
    assert replaced is False


def test_render_terminal_message_keeps_deadline_copy_on_stopped_fallthrough() -> None:
    envelope = _assemble(
        proposal_disposition="auto_applicable",
        run_outcomes=[],
        terminal_cause="deadline_expired",
    )
    message = agent_module._TIMEOUT_REPLY_DEFAULT

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert envelope.next_state == "stopped"
    assert envelope.response_kind == "stopped"
    assert rendered == message
    assert rendered != "I stopped without confirming the goal was met."
    assert replaced is True


def test_render_terminal_message_still_generic_without_deadline_cause() -> None:
    envelope = _assemble(proposal_disposition="auto_applicable", run_outcomes=[])

    rendered, replaced = render_terminal_message(envelope, agent_module._TIMEOUT_REPLY_DEFAULT, cancelled=False)

    assert rendered == "I stopped without confirming the goal was met."
    assert replaced is True


def test_render_terminal_message_held_draft_text_survives_with_deadline_cause() -> None:
    envelope = _assemble(proposal_disposition="review_untested", terminal_cause="deadline_expired")
    message = agent_module._TIMEOUT_REPLY_UNVALIDATED

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert envelope.next_state == "proposal_pending"
    assert rendered == message
    assert replaced is True


def test_render_terminal_message_held_draft_unreplaced_without_deadline_cause() -> None:
    envelope = _assemble(proposal_disposition="review_untested")
    message = agent_module._TIMEOUT_REPLY_UNVALIDATED

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert rendered == message
    assert replaced is False


def test_render_terminal_message_cancelled_turn_ignores_deadline_cause() -> None:
    envelope = _assemble(proposal_disposition="auto_applicable", terminal_cause="deadline_expired")

    rendered, replaced = render_terminal_message(envelope, "cancelled-text", cancelled=True)

    assert rendered == "cancelled-text"
    assert replaced is False


def test_render_terminal_message_completed_turn_is_not_stamped_by_deadline_cause() -> None:
    # replaced=True overwrites a distinct narrativeSummary downstream, so an
    # applied turn that happens to expire must not be stamped with its own text.
    envelope = _assemble(
        verified=True,
        workflow_applied=True,
        proposal_disposition="auto_applicable",
        terminal_cause="deadline_expired",
    )
    message = agent_module._TIMEOUT_REPLY_TESTED

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert envelope.next_state == "completed"
    assert rendered == message
    assert replaced is False


def test_deadline_cause_survives_envelope_round_trip() -> None:
    envelope = _assemble(proposal_disposition="auto_applicable", terminal_cause="deadline_expired")

    payload = envelope.model_dump(mode="json")
    finalized = finalize_applied_state(TerminalOutcomeEnvelope.model_validate(payload), applied=False)

    assert payload["terminal_cause"] == "deadline_expired"
    assert finalized.model_dump(mode="json")["terminal_cause"] == "deadline_expired"


def test_envelope_carries_deadline_cause_when_blocker_override_rewrote_the_reason() -> None:
    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = True
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Reply without updating the workflow.",
        user_facing_reason="I can't update or run this workflow on this turn.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="turn_intent_no_mutation_run_blocked",
        blocked_tool="update_workflow",
    )

    result = agent_module._build_timeout_exit_result(ctx, global_llm_context=None)

    assert result.turn_outcome is not None
    assert result.turn_outcome.terminal_reason != "timeout"
    assert result.terminal_envelope is not None
    assert result.terminal_envelope["terminal_cause"] == "deadline_expired"


def test_envelope_has_no_cause_when_deadline_did_not_expire() -> None:
    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False

    result = agent_module._build_timeout_exit_result(ctx, global_llm_context=None)

    assert result.terminal_envelope is not None
    assert result.terminal_envelope["terminal_cause"] is None


def test_max_turns_exit_types_the_cause_and_logs_the_backstop_fields() -> None:
    ctx = make_copilot_ctx()
    ctx.copilot_config = CopilotConfig()
    ctx.copilot_run_start_monotonic = time.monotonic() - 12.0
    ctx.enforcement_pass_count = 3
    ctx.model_calls_this_turn = 12

    with structlog.testing.capture_logs() as logs:
        result = agent_module._handle_max_turns_exceeded(ctx, global_llm_context=None)

    assert ctx.copilot_max_turns_exceeded is True
    assert result.terminal_envelope is not None
    assert result.terminal_envelope["terminal_cause"] == "max_turns_exceeded"

    backstop_logs = [entry for entry in logs if entry.get("event") == "copilot_max_turns_exceeded"]
    assert len(backstop_logs) == 1
    assert backstop_logs[0]["limit"] == 200
    assert backstop_logs[0]["iteration"] == 3
    assert backstop_logs[0]["model_call_count"] == 12
    assert backstop_logs[0]["elapsed_seconds"] == pytest.approx(12.0, abs=1.0)


def test_max_turns_exit_without_deadline_is_not_an_untyped_stop() -> None:
    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False
    ctx.copilot_max_turns_exceeded = True

    result = agent_module._build_max_turns_exit_result(ctx, global_llm_context=None)

    assert result.terminal_envelope is not None
    assert result.terminal_envelope["terminal_cause"] is not None
    assert result.terminal_envelope["terminal_cause"] == "max_turns_exceeded"


def test_deadline_wins_when_both_capacity_latches_are_set() -> None:
    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = True
    ctx.copilot_max_turns_exceeded = True

    result = agent_module._build_max_turns_exit_result(ctx, global_llm_context=None)

    assert result.terminal_envelope is not None
    assert result.terminal_envelope["terminal_cause"] == "deadline_expired"


def test_max_turns_exit_has_no_cause_when_neither_latch_is_set() -> None:
    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False
    ctx.copilot_max_turns_exceeded = False

    result = agent_module._build_max_turns_exit_result(ctx, global_llm_context=None)

    assert result.terminal_envelope is not None
    assert result.terminal_envelope["terminal_cause"] is None
