"""Reporting facts survive from their owning records, with no terminal-envelope plane."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.agent import _build_exit_result, _build_timeout_exit_result, _make_agent_result
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.build_test_connect_failure import BuildTestConnectFailure
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestFailedOperation,
    RecordedBuildTestOutcome,
    record_build_test_outcome,
)
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext
from skyvern.forge.sdk.copilot.interruption import (
    CANCEL_ACCEPT_OR_DISCARD,
    CANCEL_STOP_AT_USER_REQUEST,
    CANONICAL_ROLLED_BACK,
    DRAFT_PRESERVED,
    INTERRUPTED_TERMINAL_RETRY,
    INTERRUPTED_TERMINAL_SUPERSEDED_HEADLINE,
    MINIMAL_CANCEL_STOP,
    TESTED_DRAFT_AVAILABLE,
    TESTED_DRAFT_PRESERVED,
    UNTESTED_DRAFT_AVAILABLE,
    UNTESTED_DRAFT_PRESERVED,
    InterruptedTurnFacts,
    cancel_notice,
    render_interrupted_message,
)
from skyvern.forge.sdk.copilot.run_outcome import (
    RecordedRunOutcome,
    interim_run_start_outcome,
    run_start_unresolved,
    select_run_outcome_anchor,
)
from skyvern.forge.sdk.routes import workflow_copilot as workflow_copilot_route
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from tests.unit.copilot_test_helpers import make_copilot_ctx as _ctx
from tests.unit.copilot_test_helpers import two_page_login_yaml

SAFE_REPLY = "The test failed. I can adjust the selector next."


def _payload(**overrides: object) -> dict:
    base: dict = {
        "turnId": "turn-1",
        "turnIndex": 0,
        "mode": "build",
        "designStarted": True,
        "designEnded": True,
        "draft": None,
        "blocks": [],
        "terminal": "response",
        "terminalMessage": SAFE_REPLY,
        "narrativeSummary": SAFE_REPLY,
        "priorBlockCount": None,
        "designActivity": [],
        "startedAt": None,
        "endedAt": None,
    }
    base.update(overrides)
    return base


def _result(ctx: CopilotContext | None, **kwargs: object) -> AgentResult:
    kwargs.setdefault("user_response", SAFE_REPLY)
    kwargs.setdefault("updated_workflow", None)
    kwargs.setdefault("global_llm_context", None)
    return _make_agent_result(ctx, **kwargs)


def _extract_failed_operation() -> BuildTestFailedOperation:
    return BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_extract",
        workflow_run_block_id="wrb_extract",
        block_label="extract",
        failing_line=11,
    )


def _record_failed_operation(ctx: CopilotContext, failed_operation: BuildTestFailedOperation) -> None:
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            attempted_block_label=failed_operation.block_label,
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id=failed_operation.workflow_run_id,
            block_labels=[failed_operation.block_label] if failed_operation.block_label else [],
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )


def _record_connect_failure(ctx: CopilotContext, connect_failure: BuildTestConnectFailure) -> None:
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            attempted_block_label="extract",
            verdict="repairable_failure",
            reason_code="unrecoverable_tool_error",
            workflow_run_id=connect_failure.workflow_run_id,
            block_labels=["extract"],
            structural_failure_identity="connect-failure",
            connect_failure=connect_failure,
        ),
    )


class TestApprovalIsNeverWidened:
    """A recorded failure must keep a draft in review, whatever the packet said."""

    def test_failure_record_with_unset_disposition_never_auto_applies(self) -> None:
        ctx = _ctx()
        _record_failed_operation(ctx, _extract_failed_operation())

        result = _result(
            ctx,
            updated_workflow=SimpleNamespace(name="untested draft"),
            workflow_yaml=two_page_login_yaml(),
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=_payload(),
        )

        assert result.proposal_disposition == "review_untested"
        assert workflow_copilot_route._effective_auto_accept(True, result) is False

    def test_connect_failure_with_unset_disposition_never_auto_applies(self) -> None:
        ctx = _ctx()
        _record_connect_failure(
            ctx,
            BuildTestConnectFailure(
                state="already_closed",
                workflow_run_id="wr_connect",
                browser_session_id="pbs_connect",
                retry_action="test_end_to_end",
            ),
        )

        result = _result(
            ctx,
            updated_workflow=SimpleNamespace(name="untested draft"),
            workflow_yaml=two_page_login_yaml(),
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=_payload(),
        )

        assert result.proposal_disposition == "review_untested"
        assert workflow_copilot_route._effective_auto_accept(True, result) is False

    def test_an_explicit_auto_applicable_is_downgraded_by_a_failed_operation(self) -> None:
        ctx = _ctx()
        _record_failed_operation(ctx, _extract_failed_operation())

        result = _result(
            ctx,
            updated_workflow=SimpleNamespace(name="untested draft"),
            workflow_yaml=two_page_login_yaml(),
            proposal_disposition="auto_applicable",
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=_payload(),
        )

        assert result.proposal_disposition == "review_untested"
        assert workflow_copilot_route._effective_auto_accept(True, result) is False

    def test_an_explicit_auto_applicable_is_downgraded_by_a_connect_failure(self) -> None:
        ctx = _ctx()
        _record_connect_failure(
            ctx,
            BuildTestConnectFailure(
                state="already_closed",
                workflow_run_id="wr_connect",
                browser_session_id="pbs_connect",
                retry_action="test_end_to_end",
            ),
        )

        result = _result(
            ctx,
            updated_workflow=SimpleNamespace(name="untested draft"),
            workflow_yaml=two_page_login_yaml(),
            proposal_disposition="auto_applicable",
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=_payload(),
        )

        assert result.proposal_disposition == "review_untested"
        assert workflow_copilot_route._effective_auto_accept(True, result) is False

    def test_the_exit_builders_auto_applicable_default_is_downgraded_by_a_failure_record(self) -> None:
        ctx = _ctx(
            last_workflow=SimpleNamespace(name="verified draft"),
            last_workflow_yaml=two_page_login_yaml(),
            last_test_ok=True,
            last_full_workflow_test_ok=True,
        )
        _record_failed_operation(ctx, _extract_failed_operation())

        result = _build_exit_result(ctx, SAFE_REPLY, None)

        assert result.updated_workflow is not None
        assert result.proposal_disposition == "review_untested"
        assert workflow_copilot_route._effective_auto_accept(True, result) is False

    def test_failure_record_with_no_draft_reports_no_proposal(self) -> None:
        ctx = _ctx()
        _record_failed_operation(ctx, _extract_failed_operation())

        result = _result(ctx, turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD), narrative_payload=_payload())

        assert result.proposal_disposition == "no_proposal"
        assert workflow_copilot_route._effective_auto_accept(True, result) is False

    def test_a_proposal_less_turn_is_never_persisted_as_auto_applicable(self) -> None:
        # The route gate refuses this result either way; this pins the record it leaves behind,
        # which is what the review gate reads back on reload.
        result = _result(
            _ctx(),
            proposal_disposition="auto_applicable",
            turn_outcome=TurnOutcome(response_kind=ResponseKind.ANSWER),
            narrative_payload=_payload(),
        )

        assert result.proposal_disposition == "no_proposal"
        assert (result.narrative_payload or {})["proposalDisposition"] == "no_proposal"
        assert workflow_copilot_route._effective_auto_accept(True, result) is False

    def test_clean_auto_applicable_proposal_still_writes(self) -> None:
        result = _result(
            _ctx(),
            updated_workflow=SimpleNamespace(name="tested draft"),
            workflow_yaml=two_page_login_yaml(),
            proposal_disposition="auto_applicable",
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=_payload(),
        )

        assert result.proposal_disposition == "auto_applicable"
        assert workflow_copilot_route._effective_auto_accept(True, result) is True

    def test_auto_applicable_proposal_without_chat_opt_in_does_not_write(self) -> None:
        result = _result(
            _ctx(),
            updated_workflow=SimpleNamespace(name="tested draft"),
            workflow_yaml=two_page_login_yaml(),
            proposal_disposition="auto_applicable",
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=_payload(),
        )

        assert workflow_copilot_route._effective_auto_accept(None, result) is False

    def test_cancelled_result_never_auto_applies(self) -> None:
        result = _result(
            _ctx(),
            updated_workflow=SimpleNamespace(name="tested draft"),
            workflow_yaml=two_page_login_yaml(),
            proposal_disposition="auto_applicable",
            cancelled=True,
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=_payload(),
        )

        assert workflow_copilot_route._effective_auto_accept(True, result) is False


class TestTheModelsReplyIsDelivered:
    """The model's safe reply survives finalization, with no envelope keys anywhere."""

    def test_a_failure_record_and_a_preserved_draft_do_not_rewrite_the_reply(self) -> None:
        ctx = _ctx()
        _record_failed_operation(ctx, _extract_failed_operation())

        result = _result(
            ctx,
            updated_workflow=SimpleNamespace(name="untested draft"),
            workflow_yaml=two_page_login_yaml(),
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=_payload(),
        )

        assert result.user_response == SAFE_REPLY
        assert result.narrative_payload is not None
        assert result.narrative_payload["terminalMessage"] == SAFE_REPLY
        assert result.narrative_payload["narrativeSummary"] == SAFE_REPLY

    def test_a_plain_turn_carries_none_of_the_retired_keys(self) -> None:
        result = _result(
            _ctx(), turn_outcome=TurnOutcome(response_kind=ResponseKind.ANSWER), narrative_payload=_payload()
        )

        assert result.narrative_payload is not None
        assert not hasattr(result, "terminal_envelope")
        for key in ("terminalEnvelope", "renderedFromEnvelope", "nextState"):
            assert key not in result.narrative_payload

    def test_a_success_claiming_reply_ships_as_written_and_the_failure_still_reaches_the_facts(self) -> None:
        optimistic = "Destination write completed successfully."
        ctx = _ctx()
        _record_failed_operation(ctx, _extract_failed_operation())

        result = _result(
            ctx,
            user_response=optimistic,
            updated_workflow=SimpleNamespace(name="untested draft"),
            workflow_yaml=two_page_login_yaml(),
            turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD),
            narrative_payload=_payload(terminalMessage=optimistic, narrativeSummary=optimistic),
        )

        assert result.user_response == optimistic
        facts = (result.narrative_payload or {})["turnFacts"]
        assert facts["terminalCause"] == "browser_operation_failed"
        assert facts["ranCleanOnCurrentSource"] is False
        assert result.proposal_disposition == "review_untested"

    def test_the_failure_is_reported_beside_the_reply_not_inside_it(self) -> None:
        ctx = _ctx()
        _record_failed_operation(ctx, _extract_failed_operation())

        result = _result(ctx, turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD), narrative_payload=_payload())

        assert result.narrative_payload is not None
        assert result.narrative_payload["turnFacts"]["terminalCause"] == "browser_operation_failed"
        assert result.narrative_payload["turnFacts"]["ranCleanOnCurrentSource"] is False

    def test_an_empty_completion_reports_its_cause_and_never_claims_a_clean_run(self) -> None:
        ctx = _ctx()
        ctx.empty_completion = True

        result = _result(ctx, turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD), narrative_payload=_payload())

        facts = (result.narrative_payload or {})["turnFacts"]
        assert facts["terminalCause"] == "empty_completion"
        assert facts["ranCleanOnCurrentSource"] is False


class TestRunFactsComeFromTheRecordedTrace:
    """Run identity, lifecycle and block counts read the recorded run outcomes."""

    def test_a_completed_run_reports_its_identity_and_lifecycle(self) -> None:
        ctx = _ctx()
        ctx.last_run_outcome = RecordedRunOutcome(
            verdict="not_demonstrated",
            workflow_run_id="wr_done",
            run_completed=True,
            display_reason="Checkout never reached confirmation.",
        )

        result = _result(ctx, turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD), narrative_payload=_payload())

        facts = (result.narrative_payload or {})["turnFacts"]
        assert facts["runId"] == "wr_done"
        assert facts["runCompleted"] is True
        assert facts["evaluationState"] == "not_demonstrated"

    def test_a_started_run_with_no_result_never_reports_zero_blocks(self) -> None:
        ctx = _ctx()
        ctx.run_outcome_trace.append(interim_run_start_outcome("wr_started"))

        result = _result(ctx, turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD), narrative_payload=_payload())

        facts = (result.narrative_payload or {})["turnFacts"]
        assert facts["runId"] == "wr_started"
        assert facts["blocksRunThisTurn"] is None
        assert facts["evaluationState"] is None

    def test_a_result_supersedes_that_runs_own_start(self) -> None:
        started = interim_run_start_outcome("wr_1")
        resolved = RecordedRunOutcome(verdict="not_demonstrated", workflow_run_id="wr_1", run_completed=True)

        assert select_run_outcome_anchor([started, resolved]) == resolved
        assert run_start_unresolved([started, resolved]) is False
        assert run_start_unresolved([started]) is True

    def test_the_latest_run_is_the_anchor_even_after_an_earlier_failure(self) -> None:
        first = RecordedRunOutcome(verdict="not_demonstrated", workflow_run_id="wr_1", run_completed=True)
        second = RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_2", run_completed=True)

        assert select_run_outcome_anchor([first, second]) == second

    def test_a_demonstrated_run_anchors_over_an_earlier_not_demonstrated_one(self) -> None:
        earlier = RecordedRunOutcome(verdict="not_demonstrated", workflow_run_id="wr_1", run_completed=True)
        latest = RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_2", run_completed=True)

        assert select_run_outcome_anchor([earlier, latest]) == latest

    def test_a_demonstrated_run_reaches_the_turn_facts_as_the_evaluation_state(self) -> None:
        ctx = _ctx()
        ctx.last_run_outcome = RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_ok", run_completed=True)

        result = _result(ctx, turn_outcome=TurnOutcome(response_kind=ResponseKind.BUILD), narrative_payload=_payload())

        facts = (result.narrative_payload or {})["turnFacts"]
        assert facts["evaluationState"] == "demonstrated"


class TestStopAndInterruptionNotices:
    """Harness-authored notices for turns with no model reply of their own."""

    def test_an_interrupted_turn_reports_what_is_known_and_never_why(self) -> None:
        message = render_interrupted_message(
            InterruptedTurnFacts(
                iteration=4,
                workflow_permanent_id="wpid-1",
                workflow_version=7,
                authored_edits_saved=False,
                last_recorded_build_test_phase="persisted_block_run",
            )
        )

        assert "iteration 4" in message
        assert "wpid-1" in message and "version 7" in message
        assert "were not saved to the workflow" in message
        assert INTERRUPTED_TERMINAL_RETRY in message
        for cause in ("failed", "navigated", "disconnected", "connection lost", "timed out"):
            assert cause not in message.lower()

    def test_a_superseded_turn_does_not_ask_for_the_message_again(self) -> None:
        message = render_interrupted_message(InterruptedTurnFacts(superseded_by_newer_test=True))

        assert INTERRUPTED_TERMINAL_SUPERSEDED_HEADLINE in message
        assert INTERRUPTED_TERMINAL_RETRY not in message

    def test_a_preserved_draft_is_named_so_the_card_and_the_text_agree(self) -> None:
        message = render_interrupted_message(InterruptedTurnFacts(), preserved_draft="review_untested")

        assert UNTESTED_DRAFT_AVAILABLE in message

    def test_an_interrupted_turn_holding_a_tested_draft_still_announces_it(self) -> None:
        message = render_interrupted_message(InterruptedTurnFacts(), preserved_draft="review_tested")

        assert TESTED_DRAFT_AVAILABLE in message
        assert UNTESTED_DRAFT_AVAILABLE not in message

    def test_a_draft_kept_by_an_exit_that_staged_no_proposal_is_still_announced(self) -> None:
        notice = cancel_notice(
            stop_button=False,
            preserved_draft="no_proposal",
            canonical_rolled_back=False,
        )

        assert DRAFT_PRESERVED in notice
        assert CANCEL_ACCEPT_OR_DISCARD in notice

    def test_the_cancel_notice_is_composed_from_the_turns_recorded_facts(self) -> None:
        notice = cancel_notice(
            base=None,
            stop_button=True,
            preserved_draft="review_untested",
            canonical_rolled_back=True,
        )

        assert notice.startswith(CANCEL_STOP_AT_USER_REQUEST)
        assert UNTESTED_DRAFT_PRESERVED in notice
        assert notice.endswith(CANONICAL_ROLLED_BACK)

    def test_a_cancel_with_no_draft_and_no_rollback_claims_neither(self) -> None:
        notice = cancel_notice(
            stop_button=False,
            preserved_draft=None,
            canonical_rolled_back=False,
        )

        assert notice == MINIMAL_CANCEL_STOP
        for claim in (TESTED_DRAFT_PRESERVED, UNTESTED_DRAFT_PRESERVED, CANONICAL_ROLLED_BACK):
            assert claim not in notice

    def test_a_tested_draft_is_never_reported_as_untested(self) -> None:
        notice = cancel_notice(
            stop_button=False,
            preserved_draft="review_tested",
            canonical_rolled_back=False,
        )

        assert TESTED_DRAFT_PRESERVED in notice
        assert UNTESTED_DRAFT_PRESERVED not in notice

    def test_a_reply_the_turn_already_wrote_is_kept_and_the_stop_facts_are_added_beside_it(self) -> None:
        blocker_reply = "I can't open that site because the connected account was never approved."

        notice = cancel_notice(
            base=blocker_reply,
            stop_button=True,
            preserved_draft="review_untested",
            canonical_rolled_back=True,
        )

        assert notice.startswith(blocker_reply)
        assert CANCEL_STOP_AT_USER_REQUEST not in notice
        assert UNTESTED_DRAFT_PRESERVED in notice
        assert notice.endswith(CANONICAL_ROLLED_BACK)

    def test_a_cancel_exit_with_no_reply_of_its_own_falls_back_to_the_stop_opening(self) -> None:
        notice = cancel_notice(
            base="   ",
            stop_button=True,
            preserved_draft=None,
            canonical_rolled_back=False,
        )

        assert notice == CANCEL_STOP_AT_USER_REQUEST


class TestDeadlineExitsReportOnlyTheirFacts:
    """A turn that hits its deadline persists the typed expiry facts; no reply is invented for it."""

    def test_a_deadline_exit_with_no_report_persists_empty_content_and_typed_facts(self) -> None:
        result = _build_timeout_exit_result(_ctx(), global_llm_context=None)

        assert result.user_response == ""
        outcome = result.turn_outcome
        assert outcome is not None
        assert outcome.budget_expired is True
        assert outcome.budget_expiry_source == "deadline"
        assert outcome.budget_expiry_report_produced is False
        assert outcome.terminal_reason == "timeout"
        payload = result.narrative_payload
        assert payload is not None
        assert payload["terminal"] == "error"
        assert payload["terminalMessage"] == ""
        assert payload["budgetExpiry"]["reportProduced"] is False
        assert result.proposal_disposition == "no_proposal"

    def test_a_deadline_exit_after_a_model_report_keeps_the_report_as_written(self) -> None:
        result = _result(
            _ctx(),
            turn_outcome=TurnOutcome(
                response_kind=ResponseKind.BUILD,
                budget_expired=True,
                budget_expiry_source="deadline",
                budget_expiry_report_produced=True,
            ),
            narrative_payload=_payload(),
        )

        assert result.user_response == SAFE_REPLY
        assert result.narrative_payload is not None
        assert result.narrative_payload["terminalMessage"] == SAFE_REPLY


class TestNoWaitingStateWithoutARecipient:
    """An ask is a question record, never a claim derived from the turn."""

    def test_a_question_turn_delivers_the_models_ask_unchanged(self) -> None:
        ask = "Which sign-in should I use?"
        result = _result(
            _ctx(),
            user_response=ask,
            response_type="ASK_QUESTION",
            turn_outcome=TurnOutcome(response_kind=ResponseKind.CLARIFY),
            narrative_payload=_payload(terminalMessage=ask, narrativeSummary=ask),
        )

        assert result.user_response == ask

    def test_no_turn_payload_claims_the_chat_is_waiting(self) -> None:
        ask = "Which sign-in should I use?"
        result = _result(
            _ctx(),
            user_response=ask,
            response_type="ASK_QUESTION",
            turn_outcome=TurnOutcome(response_kind=ResponseKind.CLARIFY),
            narrative_payload=_payload(terminalMessage=ask, narrativeSummary=ask),
        )

        payload = result.narrative_payload or {}
        for key in ("awaitingUserInput", "awaiting_user_input", "nextState"):
            assert key not in payload


class TestTheSafetyBoundaryIsUnchanged:
    """Exact-text parity applies only after the retained safety stage."""

    def test_a_secret_bearing_reply_is_still_refused_at_finalization(self) -> None:
        secret = "hunter2-not-a-real-secret"

        result = _build_exit_result(_ctx(), f"I signed you in with password={secret}.", None)

        assert secret not in result.user_response
        assert result.output_policy_diagnostics is not None
        assert result.output_policy_diagnostics["final_output_policy_allowed"] is False

    def test_a_safe_reply_reaches_finalization_unchanged(self) -> None:
        result = _build_exit_result(_ctx(), SAFE_REPLY, None)

        assert result.user_response == SAFE_REPLY

    def test_a_blocker_reason_carrying_internal_machinery_is_refused_at_its_boundary(self) -> None:
        with pytest.raises(ValueError):
            CopilotToolBlockerSignal(
                blocker_kind="tool_error",
                agent_steering_text="the run service rejected the call",
                user_facing_reason="I called update_and_run_blocks for workflow_run_id wr_1.",
                recovery_hint="report_blocker_to_user",
            )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
