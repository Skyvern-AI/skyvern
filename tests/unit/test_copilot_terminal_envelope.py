from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing

from skyvern.exceptions import get_user_facing_exception_message
from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.agent import _CANCEL_REPLY_UNVALIDATED
from skyvern.forge.sdk.copilot.build_test_connect_failure import build_test_connect_failure_sentence
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestConnectFailure,
    BuildTestFailedOperation,
    RecordedBuildTestOutcome,
)
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.secret_scrub import clear_session_scrub_values, register_secret_scrub_value
from skyvern.forge.sdk.copilot.terminal_envelope import (
    CANCEL_STOP_AT_USER_REQUEST,
    INTERRUPTED_TERMINAL_HEADLINE,
    INTERRUPTED_TERMINAL_RETRY,
    INTERRUPTED_TERMINAL_SUPERSEDED_HEADLINE,
    MINIMAL_CANCEL_STOP,
    MINIMAL_HONEST_STOP,
    UNTESTED_DRAFT_PRESERVED,
    InterruptedTurnFacts,
    TerminalOutcomeEnvelope,
    assemble_terminal_envelope,
    chat_awaits_user_input,
    finalize_applied_state,
    interim_run_start_outcome,
    interrupted_terminal_envelope,
    is_interim_run_outcome,
    reason_in_reply_shadow,
    render_interrupted_message,
    render_terminal_message,
    run_start_unresolved,
    select_run_outcome_anchor,
)
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _commit_run_blocks_record,
    _forget_interim_run_start,
    _record_run_blocks_result,
    _stamp_run_side_connect_failure,
    _stash_recorded_run_outcome,
)
from skyvern.forge.sdk.copilot.tools.workflow_update import _record_workflow_update_result
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from tests.unit.copilot_test_helpers import (
    HANDBACK_WORKFLOW_YAML,
    install_run_blocks_harness,
    make_copilot_ctx,
)

RUN_SIDE_SHARED_REASON = get_user_facing_exception_message(
    Exception("connect_over_cdp failed: WebSocket error: connection closed")
)
RUN_SIDE_TYPED_REASON = build_test_connect_failure_sentence(
    BuildTestConnectFailure(state="already_closed", workflow_run_id="wr_run_side")
)


def _run_side_failed_result(
    *, failure_reason: str = RUN_SIDE_SHARED_REASON, reason_code: str | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "workflow_run_id": "wr_run_side",
        "browser_session_id": "pbs_run_side",
        "overall_status": "failed",
        "blocks": [],
        "failure_reason": failure_reason,
    }
    if reason_code is not None:
        data["failure_category"] = [{"category": "BROWSER_ERROR", "confidence_float": 1.0, "reason_code": reason_code}]
    return {"ok": False, "error": failure_reason, "data": data}


def _install_session_record(monkeypatch: pytest.MonkeyPatch, session: PersistentBrowserSession | None) -> None:
    mock_app = MagicMock()
    mock_app.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(return_value=session)
    monkeypatch.setattr(run_execution_module, "app", mock_app)


def _session_record(*, closed: bool) -> PersistentBrowserSession:
    now = datetime.now(UTC)
    return PersistentBrowserSession(
        persistent_browser_session_id="pbs_run_side",
        organization_id="org-1",
        status="completed" if closed else "running",
        completed_at=now if closed else None,
        created_at=now,
        modified_at=now,
    )


def test_a_persisted_session_closed_reason_types_the_stop_without_touching_the_runs_own_text() -> None:
    result = _run_side_failed_result(reason_code="browser_session_closed")

    sentence = _stamp_run_side_connect_failure(make_copilot_ctx(), result)

    assert sentence == RUN_SIDE_TYPED_REASON
    assert result["data"]["build_test_connect_failure"]["state"] == "already_closed"
    assert result["error"] == RUN_SIDE_SHARED_REASON
    assert result["data"]["failure_reason"] == RUN_SIDE_SHARED_REASON


def test_a_persisted_startup_timeout_types_provisioning_unavailable() -> None:
    result = _run_side_failed_result(reason_code="browser_session_startup_timeout")

    sentence = _stamp_run_side_connect_failure(make_copilot_ctx(), result)

    assert sentence is not None
    assert result["data"]["build_test_connect_failure"]["state"] == "provisioning_unavailable"


def test_a_run_that_failed_for_its_own_reason_stays_untyped_however_its_session_row_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mistyped block label fails before any block runs and its session row closes normally
    afterwards; the run persisted no browser reason code, so the author must see the typo, not
    a browser-loss retry."""
    _install_session_record(monkeypatch, _session_record(closed=True))
    typo = "Unable to find block with label extract_invoice_total"
    result = _run_side_failed_result(failure_reason=typo)

    sentence = _stamp_run_side_connect_failure(make_copilot_ctx(), result)

    assert sentence is None
    assert result["data"].get("build_test_connect_failure") is None
    assert result["error"] == typo
    assert result["data"]["failure_reason"] == typo


@pytest.mark.asyncio
async def test_a_typed_run_side_stop_keeps_the_shared_prose_out_of_the_chat_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session_record(monkeypatch, _session_record(closed=True))
    ctx = make_copilot_ctx()
    ctx.last_update_block_count = 2
    result = _run_side_failed_result(reason_code="browser_session_closed")

    sentence = _stamp_run_side_connect_failure(ctx, result)
    recorded = _record_run_blocks_result(ctx, result, connect_failure_reason=sentence)
    reply = agent_module._rewrite_failed_test_response("The test failed.", ctx)

    assert recorded is not None and recorded.display_reason == RUN_SIDE_TYPED_REASON
    assert RUN_SIDE_TYPED_REASON in reply
    assert RUN_SIDE_SHARED_REASON not in reply
    assert "high demand" not in reply.lower()


@pytest.mark.asyncio
async def test_a_typed_run_side_stop_keeps_the_shared_prose_out_of_the_recorded_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_session_record(monkeypatch, _session_record(closed=True))
    ctx = make_copilot_ctx()
    result = _run_side_failed_result(reason_code="browser_session_closed")

    sentence = _stamp_run_side_connect_failure(ctx, result)
    _record_run_blocks_result(ctx, result, connect_failure_reason=sentence)
    reason, _ = agent_module._recorded_failure_summary(ctx)

    assert reason and reason in RUN_SIDE_TYPED_REASON
    assert RUN_SIDE_SHARED_REASON not in reason
    assert "high demand" not in reason.lower()


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
        "workflow_attempted": True,
    }
    defaults.update(overrides)
    envelope = assemble_terminal_envelope(**defaults)
    assert envelope is not None
    return envelope


def test_run_anchor_reports_the_actual_latest_run() -> None:
    envelope = _assemble(
        run_outcomes=[
            _run_outcome("not_demonstrated", "The checkout did not reach confirmation."),
            _run_outcome("not_evaluated", "A later scout run completed."),
        ]
    )

    assert envelope.run_verdict == "not_evaluated"
    assert envelope.run_display_reason == "A later scout run completed."


@pytest.mark.parametrize("state", ["already_closed", "provisioning_unavailable", "cdp_connect_failed"])
def test_connect_failure_terminal_is_typed_preserves_identity_and_offers_fresh_retry(state: str) -> None:
    failure = BuildTestConnectFailure(
        state=state,
        workflow_run_id="wr_1",
        workflow_run_block_id="wrb_1",
        task_id="tsk_1",
        browser_session_id="pbs_1",
    )
    envelope = _assemble(
        proposal_disposition="review_untested",
        connect_failure=failure,
        proposal_present=True,
    )

    message, replaced = render_terminal_message(envelope, "unsupported model copy", cancelled=False)

    assert envelope.terminal_cause == state
    assert envelope.connect_failure == failure
    assert envelope.next_state == "stopped"
    assert replaced is True
    assert state in message
    assert all(identity in message for identity in ("wr_1", "wrb_1", "tsk_1", "pbs_1"))
    assert "untested draft was preserved" in message
    assert "fresh browser session" in message
    assert "high demand" not in message.lower()


def test_connect_failure_terminal_preserves_pending_question() -> None:
    failure = BuildTestConnectFailure(state="cdp_connect_failed", browser_session_id="pbs_1")
    envelope = _assemble(
        response_type="ASK_QUESTION",
        proposal_disposition="review_untested",
        connect_failure=failure,
        proposal_present=True,
    )

    message, replaced = render_terminal_message(envelope, "Which credential should I use?", cancelled=False)

    assert envelope.next_state == "awaiting_user_input"
    assert envelope.response_kind == "question"
    assert "Which credential should I use?" in message
    assert "premise is not confirmed" in message
    assert replaced is True


def test_connect_failure_owns_terminal_over_earlier_failed_operation() -> None:
    failed_operation = BuildTestFailedOperation(kind="browser_operation_failed")
    connect_failure = BuildTestConnectFailure(state="cdp_connect_failed", browser_session_id="pbs_1")

    envelope = _assemble(
        proposal_disposition="review_untested",
        failed_operation=failed_operation,
        connect_failure=connect_failure,
        proposal_present=True,
    )
    message, replaced = render_terminal_message(envelope, "The code run failed.", cancelled=False)

    assert envelope.terminal_cause == "cdp_connect_failed"
    assert replaced is True
    assert "cdp_connect_failed" in message
    assert "fresh browser session" in message


def test_crash_exit_renders_interrupted_not_the_failed_operation_sentence() -> None:
    envelope = _assemble(
        proposal_disposition="review_untested",
        failed_operation=BuildTestFailedOperation(kind="browser_operation_failed"),
        proposal_present=True,
        interruption=InterruptedTurnFacts(workflow_permanent_id="wpid_1", workflow_version=4),
    )
    message, replaced = render_terminal_message(envelope, "The code run failed.", cancelled=False)

    assert replaced is True
    assert message.startswith(INTERRUPTED_TERMINAL_HEADLINE)
    assert "browser operation failed" not in message.lower()
    assert "wpid_1" in message
    # The Accept/Discard card is still on screen, so the copy must not read as if it were gone.
    assert "The untested draft is available for review." in message
    # The latch is not dropped: it still drives the unverified/unapplied terminal state.
    assert envelope.failed_operation is not None
    assert envelope.verified is False
    assert envelope.workflow_applied is False


def test_failed_operation_without_a_crash_keeps_its_own_sentence() -> None:
    envelope = _assemble(
        proposal_disposition="review_untested",
        failed_operation=BuildTestFailedOperation(kind="browser_operation_failed"),
        proposal_present=True,
    )
    message, replaced = render_terminal_message(envelope, "The code run failed.", cancelled=False)

    assert replaced is True
    assert message.startswith("I stopped after a browser operation failed while testing the workflow.")
    assert "`" not in message
    assert INTERRUPTED_TERMINAL_HEADLINE not in message


def test_failed_operation_sentence_names_the_block_that_failed() -> None:
    envelope = _assemble(
        proposal_disposition="review_untested",
        failed_operation=BuildTestFailedOperation(
            kind="browser_operation_failed",
            block_label="continue_to_payment",
        ),
        proposal_present=True,
    )
    message, replaced = render_terminal_message(envelope, "The code run failed.", cancelled=False)

    assert replaced is True
    assert message.startswith(
        "I stopped after a browser operation failed in `continue_to_payment` while testing the workflow."
    )


def test_unexpected_error_exit_renders_interrupted_and_keeps_its_terminal_reason() -> None:
    ctx = make_copilot_ctx(
        latest_recorded_build_test_outcome=RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            failed_operation=BuildTestFailedOperation(kind="browser_operation_failed"),
        ),
        # The YAML parse stamps this constant whatever the workflow's real version is, so reading
        # it here would report version 1 on every crash.
        last_workflow=SimpleNamespace(version=1),
    )

    result = agent_module._build_unexpected_error_exit_result(ctx, None, error=RuntimeError("boom"))

    assert result.turn_outcome is not None
    assert result.turn_outcome.terminal_reason == "unexpected_error"
    assert result.user_response.startswith(INTERRUPTED_TERMINAL_HEADLINE)
    assert "browser operation failed" not in result.user_response.lower()
    # Neither fact is observable at a crash site, so the copy must not state either one.
    assert "version 1" not in result.user_response
    assert "were not saved" not in result.user_response


def test_empty_completion_is_a_supported_failed_terminal_cause() -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        workflow_applied=False,
        response_kind="stopped",
        terminal_cause="empty_completion",
        proposal_present=False,
        proposal_disposition="no_proposal",
    )

    assert envelope.terminal_cause == "empty_completion"
    assert envelope.verified is False
    assert envelope.workflow_applied is False
    assert envelope.proposal_present is False


def test_interrupted_envelope_names_the_run_the_turn_was_testing() -> None:
    envelope = interrupted_terminal_envelope(InterruptedTurnFacts(workflow_permanent_id="wpid_1", run_id="wr_prior"))

    assert envelope.run_id == "wr_prior"
    assert envelope.interruption is not None
    assert envelope.interruption.run_id == "wr_prior"


def test_superseded_interruption_never_asks_for_the_message_again() -> None:
    facts = InterruptedTurnFacts(workflow_permanent_id="wpid_1", run_id="wr_prior")

    assert render_interrupted_message(facts).endswith(INTERRUPTED_TERMINAL_RETRY)

    superseded = render_interrupted_message(facts.model_copy(update={"superseded_by_newer_test": True}))

    assert superseded.startswith(INTERRUPTED_TERMINAL_SUPERSEDED_HEADLINE)
    assert INTERRUPTED_TERMINAL_RETRY not in superseded
    assert INTERRUPTED_TERMINAL_HEADLINE not in superseded
    assert "Cancelled by user." not in superseded


def test_crash_exit_interruption_names_the_run_it_was_testing() -> None:
    ctx = make_copilot_ctx(
        latest_recorded_build_test_outcome=RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_crash",
            failed_operation=BuildTestFailedOperation(kind="browser_operation_failed"),
        ),
        last_workflow=SimpleNamespace(version=1),
    )

    result = agent_module._build_unexpected_error_exit_result(ctx, None, error=RuntimeError("boom"))

    assert result.terminal_envelope is not None
    assert result.terminal_envelope["interruption"]["run_id"] == "wr_crash"


def test_connect_failure_still_outranks_a_crash_exit() -> None:
    envelope = _assemble(
        proposal_disposition="review_untested",
        connect_failure=BuildTestConnectFailure(state="cdp_connect_failed", browser_session_id="pbs_1"),
        proposal_present=True,
        interruption=InterruptedTurnFacts(workflow_permanent_id="wpid_1"),
    )
    message, replaced = render_terminal_message(envelope, "The code run failed.", cancelled=False)

    assert replaced is True
    assert "cdp_connect_failed" in message
    assert INTERRUPTED_TERMINAL_HEADLINE not in message


@pytest.mark.parametrize("capacity_cause", ["deadline_expired", "max_turns_exceeded"])
def test_connect_failure_does_not_overwrite_capacity_terminal_cause(capacity_cause: str) -> None:
    failure = BuildTestConnectFailure(state="cdp_connect_failed", browser_session_id="pbs_1")

    envelope = _assemble(
        proposal_disposition="review_untested",
        terminal_cause=capacity_cause,
        connect_failure=failure,
        proposal_present=True,
    )

    assert envelope.terminal_cause == capacity_cause


def test_run_anchor_falls_back_to_latest_final_verdict_when_no_not_demonstrated() -> None:
    envelope = _assemble(
        run_outcomes=[
            _run_outcome("not_evaluated", "First run completed."),
            _run_outcome("not_evaluated", "Later run completed."),
        ]
    )

    assert envelope.run_verdict == "not_evaluated"
    assert envelope.run_display_reason == "Later run completed."


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


def test_browser_operation_failure_is_a_typed_unverified_terminal() -> None:
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_browser_operation",
        workflow_run_block_id="wrb_capture_failure",
        block_label="collect_failure_rate",
        failing_line=11,
    )

    envelope = _assemble(
        verified=True,
        workflow_applied=True,
        proposal_disposition="review_untested",
        failed_operation=failed_operation,
        proposal_present=True,
    )
    message, replaced = render_terminal_message(
        envelope,
        "Destination write completed successfully.",
        cancelled=False,
    )

    assert envelope.next_state == "stopped"
    assert envelope.response_kind == "stopped"
    assert envelope.verified is False
    assert envelope.workflow_applied is False
    assert envelope.terminal_cause == "browser_operation_failed"
    assert envelope.failed_operation == failed_operation
    finalized = finalize_applied_state(envelope, applied=True, proposal_present=True)
    assert finalized.workflow_applied is False
    assert finalized.next_state == "stopped"
    assert replaced is True
    assert "browser operation failed" in message.lower()
    assert "draft" in message.lower()
    assert "write completed" not in message.lower()


def test_browser_operation_failure_without_proposal_does_not_claim_draft_available() -> None:
    envelope = _assemble(
        proposal_disposition="no_proposal",
        failed_operation=BuildTestFailedOperation(kind="browser_operation_failed"),
        proposal_present=False,
    )

    message, replaced = render_terminal_message(envelope, "Destination write completed.", cancelled=False)

    assert replaced is True
    assert "browser operation failed" in message.lower()
    assert "draft" not in message.lower()
    assert "requested work was not confirmed" in message.lower()


def test_browser_operation_failure_preserves_required_question_precedence() -> None:
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_browser_operation",
        workflow_run_block_id="wrb_capture_failure",
    )

    envelope = _assemble(
        response_type="ASK_QUESTION",
        verified=True,
        workflow_applied=True,
        proposal_disposition="auto_applicable",
        failed_operation=failed_operation,
    )
    model_message = "The destination write completed. Which account should I use?"
    message, replaced = render_terminal_message(envelope, model_message, cancelled=False)
    finalized = finalize_applied_state(envelope, applied=False)

    assert envelope.user_action_required is True
    assert envelope.next_state == "awaiting_user_input"
    assert envelope.response_kind == "question"
    assert envelope.verified is False
    assert envelope.workflow_applied is False
    assert finalized.next_state == "awaiting_user_input"
    assert finalized.response_kind == "question"
    assert "browser operation failed" in message.lower()
    assert "requested work was not confirmed" in message.lower()
    assert model_message in message
    assert replaced is True

    rerendered, rerendered_replaced = render_terminal_message(envelope, message, cancelled=False)

    assert rerendered == message
    assert rerendered_replaced is False


def test_hydrated_question_with_failed_operation_normalizes_privileged_flags() -> None:
    envelope = TerminalOutcomeEnvelope.model_validate(
        {
            "next_state": "completed",
            "verified": True,
            "workflow_applied": True,
            "user_action_required": True,
            "response_kind": "question",
            "failed_operation": {
                "kind": "browser_operation_failed",
                "workflow_run_id": "wr_browser_operation",
            },
        }
    )
    finalized = finalize_applied_state(envelope, applied=True, proposal_present=True)

    assert envelope.verified is False
    assert envelope.workflow_applied is False
    assert finalized.verified is False
    assert finalized.workflow_applied is False
    assert finalized.next_state == "awaiting_user_input"
    assert finalized.response_kind == "question"


def test_terminal_operation_serialization_and_logging_redact_registered_block_label_secret() -> None:
    session_id = "pbs_terminal_operation_redaction"
    secret = "terminal-label-secret-value"
    ctx = SimpleNamespace(browser_session_id=session_id, secret_scrub_values=[])
    register_secret_scrub_value(ctx, secret)
    try:
        with structlog.testing.capture_logs() as logs:
            payload = agent_module._assemble_terminal_envelope_safe(
                response_type="REPLY",
                verified=False,
                workflow_applied=False,
                proposal_disposition="review_untested",
                run_outcomes=[],
                blocker_reason=None,
                halt_kind=None,
                attempted=None,
                workflow_mutated=True,
                workflow_attempted=True,
                final_message="I stopped.",
                failed_operation=BuildTestFailedOperation(
                    kind="browser_operation_failed",
                    block_label=f"collect_{secret}_rate",
                ),
            )
    finally:
        clear_session_scrub_values(session_id)

    assert payload is not None
    terminal_log = next(log for log in logs if log["event"] == "copilot_terminal_envelope")
    assert secret not in str(payload)
    assert secret not in str(terminal_log)
    assert payload["failed_operation"]["block_label"] == "collect_[REDACTED_SECRET]_rate"


def test_terminal_operation_hydration_redacts_registered_block_label_secret() -> None:
    session_id = "pbs_terminal_operation_hydration_redaction"
    secret = "hydrated-label-secret-value"
    ctx = SimpleNamespace(browser_session_id=session_id, secret_scrub_values=[])
    register_secret_scrub_value(ctx, secret)
    try:
        envelope = TerminalOutcomeEnvelope.model_validate(
            {
                "next_state": "stopped",
                "verified": False,
                "response_kind": "stopped",
                "failed_operation": {
                    "kind": "browser_operation_failed",
                    "block_label": f"collect_{secret}_rate",
                },
            }
        )
    finally:
        clear_session_scrub_values(session_id)

    assert secret not in envelope.model_dump_json()
    assert envelope.failed_operation is not None
    assert envelope.failed_operation.block_label == "collect_[REDACTED_SECRET]_rate"


def test_anchor_uses_the_latest_final_run_fact() -> None:
    envelope = _assemble(
        run_outcomes=[
            _run_outcome("not_demonstrated", "The checkout did not reach confirmation."),
            _run_outcome("not_evaluated", "A later run completed."),
        ]
    )

    assert envelope.run_verdict == "not_evaluated"
    assert envelope.run_display_reason == "A later run completed."


def test_a_run_start_is_recognised_as_interim_by_its_role_alone() -> None:
    # The role is the whole marker, so a surface cannot be fooled into reading a run start
    # as resolved by any lifecycle or verdict value carried alongside it.
    start = interim_run_start_outcome("wr_1")

    assert start.run_completed is None
    assert is_interim_run_outcome(start)
    assert is_interim_run_outcome(replace(start, run_completed=False, verdict="not_demonstrated"))
    assert not is_interim_run_outcome(RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_1"))


def test_a_result_for_the_same_run_supersedes_that_runs_own_start() -> None:
    envelope = _assemble(
        run_outcomes=[
            interim_run_start_outcome("wr_1"),
            RecordedRunOutcome(
                verdict="not_demonstrated",
                workflow_run_id="wr_1",
                display_reason="The extraction returned no value.",
                run_completed=True,
            ),
        ]
    )

    assert envelope.run_outcome_role == "recorded"
    assert envelope.run_verdict == "not_demonstrated"
    assert envelope.run_display_reason == "The extraction returned no value."
    assert envelope.run_completed is True


def test_an_unresolved_run_start_leaves_every_lifecycle_facet_unknown() -> None:
    envelope = _assemble(run_outcomes=[interim_run_start_outcome("wr_1")], blocks_run_this_turn=None)

    assert envelope.run_outcome_role == "interim_build_test"
    assert envelope.run_id == "wr_1"
    assert envelope.run_completed is None
    assert envelope.blocks_run_this_turn is None
    assert envelope.run_display_reason is None
    assert envelope.run_output_report is None
    assert envelope.run_verdict == "not_evaluated"

    message, _ = render_terminal_message(envelope, MINIMAL_CANCEL_STOP, cancelled=True)

    assert "A run was started, and how many of its blocks ran was not confirmed." in message
    assert "The recorded run's outcome was not evaluated." in message
    assert "completed" not in message


@pytest.mark.parametrize(
    ("response_type", "verified", "workflow_applied", "proposal_disposition", "expected_next_state"),
    [
        ("ASK_QUESTION", False, False, "no_proposal", "awaiting_user_input"),
        ("REPLY", True, True, "no_proposal", "completed"),
        ("REPLY", False, False, "review_tested", "proposal_pending"),
        ("REPLY", False, False, "review_untested", "proposal_pending"),
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
        ({"workflow_attempted": False, "workflow_mutated": False}, "answer"),
        ({"workflow_attempted": True, "workflow_mutated": False}, "stopped"),
        ({"workflow_attempted": False, "workflow_mutated": True}, "stopped"),
        ({"workflow_attempted": False, "blocker_reason": "blocked"}, "stopped"),
        ({"workflow_attempted": False, "halt_kind": "halted"}, "stopped"),
        ({"workflow_attempted": False, "terminal_cause": "max_turns_exceeded"}, "stopped"),
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
    envelope = _assemble(workflow_attempted=False, workflow_mutated=False)
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
        verdict="not_evaluated",
        display_reason="A later scout replay completed.",
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

    assert [outcome.verdict for outcome in outcomes] == ["not_demonstrated", "not_evaluated"]
    assert outcomes[0].display_reason == "Checkout never reached confirmation."
    assert outcomes[1].display_reason == "A later scout replay completed."

    envelope = _assemble(run_outcomes=outcomes)

    assert envelope.run_verdict == "not_evaluated"
    assert envelope.run_display_reason == "A later scout replay completed."


def test_terminal_envelope_outcomes_seed_from_constructor_last_run_outcome() -> None:
    first = RecordedRunOutcome(
        verdict="not_demonstrated",
        display_reason="Seeded from constructor.",
        workflow_run_id="wr_ctor",
    )
    second = RecordedRunOutcome(
        verdict="not_evaluated",
        display_reason="Appended after construction.",
        workflow_run_id="wr_runtime",
    )
    ctx = make_copilot_ctx(last_run_outcome=first)

    assert ctx.terminal_envelope_run_outcomes == [first]

    ctx.last_run_outcome = second

    assert ctx.terminal_envelope_run_outcomes == [first, second]


def test_terminal_envelope_outcomes_survive_workflow_edit() -> None:
    ctx = make_copilot_ctx()
    _stash_recorded_run_outcome(
        ctx,
        RecordedRunOutcome(
            verdict="not_demonstrated",
            display_reason="Checkout never reached confirmation.",
            workflow_run_id="wr_before_reset",
        ),
    )

    edited_workflow = SimpleNamespace(proxy_location=None, workflow_definition=SimpleNamespace(blocks=[]))
    _record_workflow_update_result(
        ctx,
        {"ok": True, "_workflow": edited_workflow, "data": {"block_count": 1}},
        prior_definition=SimpleNamespace(blocks=[]),
    )
    outcomes = agent_module._terminal_envelope_run_outcomes(ctx)
    envelope = _assemble(run_outcomes=outcomes)

    assert [outcome.workflow_run_id for outcome in ctx.terminal_envelope_run_outcomes] == ["wr_before_reset"]
    assert [outcome.workflow_run_id for outcome in outcomes] == ["wr_before_reset"]
    assert envelope.run_verdict == "not_demonstrated"
    assert envelope.run_display_reason == "Checkout never reached confirmation."


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

    assert [outcome.workflow_run_id for outcome in outcomes] == ["wr_old", "wr_new"]
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
        workflow_attempted=False,
        final_message="reply",
    )

    assert envelope is None


def test_safe_wrapper_omits_recorded_output_from_telemetry() -> None:
    output_report = 'Recorded output from the latest completed run: {"customer_record":"synthetic"}'

    with structlog.testing.capture_logs() as logs:
        envelope = agent_module._assemble_terminal_envelope_safe(
            response_type="REPLY",
            verified=False,
            workflow_applied=False,
            proposal_disposition="no_proposal",
            run_outcomes=[RecordedRunOutcome(verdict="not_evaluated", output_report=output_report)],
            blocker_reason=None,
            halt_kind=None,
            attempted="ran the workflow",
            workflow_mutated=True,
            workflow_attempted=True,
            final_message="I tested it.",
        )

    assert envelope is not None
    assert envelope["run_output_report"] == output_report
    terminal_log = next(log for log in logs if log["event"] == "copilot_terminal_envelope")
    assert "run_output_report" not in terminal_log
    assert output_report not in str(terminal_log)


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


def test_render_terminal_message_stopped_without_recorded_facts_keeps_the_agent_text() -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        run_verdict=None,
        run_display_reason=None,
        response_kind="stopped",
    )
    message = "The portal does not expose invoices; archived statements are emailed instead."

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    # The agent's own explanation survives intact, but a stopped turn with no run behind
    # it still says so -- the renderer cannot tell an honest explanation from a claim.
    assert replaced is True
    assert rendered.startswith(message)
    assert rendered.endswith(MINIMAL_HONEST_STOP)


def test_render_terminal_message_stopped_falls_back_to_honest_stop_without_agent_text() -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        run_verdict=None,
        run_display_reason=None,
        response_kind="stopped",
    )

    rendered, replaced = render_terminal_message(envelope, "   ", cancelled=False)

    assert replaced is True
    assert rendered == MINIMAL_HONEST_STOP


def test_render_terminal_message_stopped_with_a_recorded_run_reports_that_it_ran() -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        run_verdict="not_evaluated",
        run_completed=True,
        run_display_reason=None,
        response_kind="stopped",
    )

    rendered, replaced = render_terminal_message(envelope, "legacy", cancelled=False)

    assert replaced is True
    assert rendered == "legacy. The recorded run completed, and its outcome was not evaluated."

    # Lifecycle is read from the recorded run, never inferred from the verdict: without a
    # recorded completion the same verdict may not claim the run finished.
    unknown_lifecycle = envelope.model_copy(update={"run_completed": None})
    rendered_unknown, _ = render_terminal_message(unknown_lifecycle, "legacy", cancelled=False)
    assert rendered_unknown == "legacy. The recorded run's outcome was not evaluated."
    assert "completed" not in rendered_unknown


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

    message = "The portal does not expose invoices; archived statements are emailed instead."

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert replaced is True
    assert rendered.startswith(message)
    assert blocker in rendered


def test_render_terminal_message_appends_exact_recorded_output_to_completed_run() -> None:
    output_report = (
        'Recorded output from the latest completed run: {"extract_document_output":'
        '{"document_name":"Resale Demand Package (Required Statement of Fees - Demand)"}}'
    )
    envelope = _assemble(
        proposal_disposition="review_tested",
        run_outcomes=[RecordedRunOutcome(verdict="not_evaluated", output_report=output_report)],
    )

    rendered, replaced = render_terminal_message(
        envelope,
        "I created and tested the reusable workflow.",
        cancelled=False,
    )

    assert output_report in rendered
    assert "Resale Demand Package (Required Statement of Fees - Demand)" in rendered
    assert replaced is True


def test_render_terminal_message_omits_unsafe_recorded_output_report() -> None:
    envelope = _assemble(
        proposal_disposition="review_tested",
        run_outcomes=[
            RecordedRunOutcome(
                verdict="not_evaluated",
                output_report=(
                    'Recorded output from the latest completed run: {"access_token":"sk-example-secret-value"}'
                ),
            )
        ],
    )
    message = "I created and tested the reusable workflow."

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert rendered == message
    assert replaced is False


@pytest.mark.parametrize("next_state", ["completed", "proposal_pending", "awaiting_user_input"])
def test_render_terminal_message_passthrough_for_non_stopped(next_state: str) -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state=next_state, verified=False, run_verdict="not_demonstrated", response_kind="stopped"
    )
    message = "keep-agent-message"

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert rendered == message
    assert replaced is False


@pytest.mark.parametrize("next_state", ["stopped", "proposal_pending"])
def test_render_terminal_message_cancelled_reports_recorded_facts_in_any_state(next_state: str) -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state=next_state,
        verified=False,
        run_verdict="not_demonstrated",
        response_kind="stopped",
        blocks_run_this_turn=2,
        proposal_present=next_state == "proposal_pending",
    )

    rendered, replaced = render_terminal_message(envelope, "keep-agent-message", cancelled=True)

    assert rendered.startswith("keep-agent-message")
    assert "2 blocks ran this turn." in rendered
    assert "outcome did not confirm the goal was met." in rendered
    assert replaced is True


def test_render_terminal_message_cancelled_mid_run_reports_unknown_never_zero() -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        response_kind="stopped",
        blocks_run_this_turn=None,
        run_id="wr_1",
        run_verdict="not_evaluated",
    )

    rendered, _ = render_terminal_message(envelope, MINIMAL_CANCEL_STOP, cancelled=True)

    assert "A run was started, and how many of its blocks ran was not confirmed." in rendered
    assert "0 blocks ran" not in rendered


def test_render_terminal_message_cancelled_names_a_tested_draft_from_the_seed_disposition() -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state="proposal_pending",
        verified=False,
        response_kind="update",
        blocks_run_this_turn=0,
        proposal_present=True,
        proposal_disposition="review_tested",
    )
    seed = "Stopped. The tested draft from this turn was preserved for review."

    rendered, _ = render_terminal_message(envelope, seed, cancelled=True)

    assert rendered.count("The tested draft from this turn was preserved for review.") == 1
    assert "untested draft" not in rendered


def test_render_terminal_message_cancelled_never_ships_two_draft_dispositions() -> None:
    # The seed reply and the envelope disagree; one bubble still carries one disposition.
    envelope = TerminalOutcomeEnvelope(
        next_state="proposal_pending",
        verified=True,
        response_kind="update",
        blocks_run_this_turn=0,
        proposal_present=True,
        proposal_disposition="review_untested",
    )
    seed = "Stopped. The tested draft from this turn was preserved for review."

    rendered, _ = render_terminal_message(envelope, seed, cancelled=True)

    assert rendered.count("draft from this turn was preserved for review.") == 1


def test_render_terminal_message_cancelled_is_stable_across_a_second_render() -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        response_kind="stopped",
        run_verdict="not_demonstrated",
        run_display_reason="The sign-in never reached the dashboard.",
        blocks_run_this_turn=2,
        proposal_present=True,
        proposal_disposition="review_untested",
        canonical_rolled_back=True,
        failed_operation=BuildTestFailedOperation(kind="browser_operation_failed"),
    )

    first, _ = render_terminal_message(envelope, "keep-agent-message", cancelled=True)
    second, replaced = render_terminal_message(envelope, first, cancelled=True)

    assert second == first
    assert replaced is False
    assert first.count("2 blocks ran this turn.") == 1
    assert first.count("The saved workflow was rolled back to its state before this turn.") == 1


def test_cancelled_render_appends_to_agent_text_even_with_a_failure_recorded() -> None:
    """Every replacing branch is guarded ``not cancelled``, which is why the stop report ships ungated."""
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        response_kind="stopped",
        blocks_run_this_turn=2,
        terminal_cause="cdp_connect_failed",
        connect_failure=BuildTestConnectFailure(state="cdp_connect_failed", browser_session_id="pbs_1"),
        failed_operation=BuildTestFailedOperation(kind="browser_operation_failed"),
    )

    rendered, replaced = render_terminal_message(envelope, "keep-agent-message", cancelled=True)

    assert rendered.startswith("keep-agent-message")
    assert "Build testing stopped with browser connection state" not in rendered
    assert "I stopped after a browser operation failed" not in rendered
    assert "2 blocks ran this turn." in rendered
    assert replaced is True


def test_render_terminal_message_cancelled_names_the_rollback() -> None:
    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        response_kind="stopped",
        blocks_run_this_turn=1,
        canonical_rolled_back=True,
    )

    rendered, _ = render_terminal_message(envelope, MINIMAL_CANCEL_STOP, cancelled=True)

    assert "The saved workflow was rolled back to its state before this turn." in rendered


def test_an_unresolved_run_start_is_not_an_explicit_stop() -> None:
    envelope = _assemble(
        workflow_attempted=False,
        run_outcomes=[interim_run_start_outcome("wr_1")],
    )

    assert envelope.run_outcome_role == "interim_build_test"
    assert envelope.response_kind == "answer"


def test_a_resolved_run_outcome_still_reports_an_explicit_stop() -> None:
    envelope = _assemble(
        workflow_attempted=False,
        run_outcomes=[RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_1")],
    )

    assert envelope.run_outcome_role == "recorded"
    assert envelope.response_kind == "stopped"


def test_an_unresolved_run_start_leaves_the_turn_facts_evaluation_empty() -> None:
    ctx = make_copilot_ctx()
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_1"))

    facts = agent_module._turn_facts_for_context(ctx, None, None)

    assert facts["runId"] == "wr_1"
    assert facts["evaluationState"] is None
    assert facts["runCompleted"] is None


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


@pytest.mark.parametrize("terminal_cause", ["deadline_expired", "max_turns_exceeded"])
@pytest.mark.parametrize("message", ["I saved the draft after the browser failed.", ""])
def test_budget_report_preserved_after_browser_failure(terminal_cause: str, message: str) -> None:
    envelope = _assemble(
        terminal_cause=terminal_cause,
        failed_operation=BuildTestFailedOperation(kind="browser_operation_failed"),
    )

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert rendered == message
    assert replaced is False


def test_render_terminal_message_keeps_model_budget_report_unchanged() -> None:
    envelope = _assemble(terminal_cause="deadline_expired")
    message = "I saved the useful draft and stopped before another run."

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert rendered == message
    assert replaced is False


def test_render_terminal_message_under_budget_stop_keeps_the_reply_and_names_the_unevaluated_outcome() -> None:
    envelope = _assemble(
        proposal_disposition="auto_applicable",
        run_outcomes=[RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_1", run_completed=True)],
        blocks_run_this_turn=1,
    )
    message = "I built and end-to-end tested the workflow, and verified the account and date range match."

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert envelope.next_state == "stopped"
    assert envelope.response_kind == "stopped"
    assert replaced is True
    assert rendered.startswith(message)
    assert "1 block ran this turn." in rendered
    assert "The recorded run completed, and its outcome was not evaluated." in rendered
    assert "The latest recorded run completed." not in rendered


def test_render_terminal_message_cancelled_turn_ignores_deadline_cause() -> None:
    envelope = _assemble(proposal_disposition="auto_applicable", terminal_cause="deadline_expired")

    rendered, _ = render_terminal_message(envelope, "cancelled-text", cancelled=True)

    assert rendered.startswith("cancelled-text")
    assert "time limit" not in rendered


def test_deadline_cause_survives_envelope_round_trip() -> None:
    envelope = _assemble(proposal_disposition="auto_applicable", terminal_cause="deadline_expired")

    payload = envelope.model_dump(mode="json")
    finalized = finalize_applied_state(TerminalOutcomeEnvelope.model_validate(payload), applied=False)

    assert payload["terminal_cause"] == "deadline_expired"
    assert finalized.model_dump(mode="json")["terminal_cause"] == "deadline_expired"


def test_max_turns_drain_exhaustion_types_the_cause_and_logs_the_backstop_fields() -> None:
    ctx = make_copilot_ctx()
    ctx.copilot_config = CopilotConfig()
    ctx.copilot_run_start_monotonic = time.monotonic() - 12.0
    ctx.enforcement_pass_count = 3
    ctx.model_calls_this_turn = 12

    with structlog.testing.capture_logs() as logs:
        ctx.budget_expiry_state.source = "max_turns"
        result = agent_module._handle_budget_drain_exhausted(ctx, global_llm_context=None)

    assert ctx.copilot_max_turns_exceeded is True
    assert result.terminal_envelope is not None
    assert result.terminal_envelope["terminal_cause"] == "max_turns_exceeded"

    backstop_logs = [entry for entry in logs if entry.get("event") == "copilot_max_turns_exceeded"]
    assert len(backstop_logs) == 1
    assert backstop_logs[0]["limit"] == 200
    assert backstop_logs[0]["iteration"] == 3
    assert backstop_logs[0]["model_call_count"] == 12
    assert backstop_logs[0]["elapsed_seconds"] == pytest.approx(12.0, abs=1.0)


def test_max_turns_exit_is_typed_no_report() -> None:
    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False
    ctx.copilot_max_turns_exceeded = True

    result = agent_module._build_max_turns_exit_result(ctx, global_llm_context=None)

    assert result.terminal_envelope is not None
    assert result.terminal_envelope["terminal_cause"] == "max_turns_exceeded"
    assert result.user_response == ""
    assert result.turn_outcome is not None
    assert result.turn_outcome.budget_expiry_report_produced is False


def test_the_envelope_carries_the_recorded_run_id() -> None:
    envelope = _assemble(run_outcomes=[RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_1")])

    assert envelope.run_id == "wr_1"


def test_run_lifecycle_and_run_id_name_the_same_archived_outcome() -> None:
    completed = _assemble(
        run_outcomes=[RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_1", run_completed=True)]
    )

    assert completed.run_id == "wr_1"
    assert completed.run_completed is True

    halted = _assemble(
        run_outcomes=[
            RecordedRunOutcome(verdict="not_demonstrated", workflow_run_id="wr_2", run_completed=False),
        ]
    )

    assert halted.run_id == "wr_2"
    assert halted.run_completed is False


def test_blocks_run_this_turn_is_absent_while_a_started_run_has_no_result() -> None:
    ctx = make_copilot_ctx()
    ctx.executed_block_labels = set()
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_1"))

    assert agent_module._blocks_run_this_turn(ctx) is None


def test_blocks_run_this_turn_reports_the_count_once_the_run_result_lands() -> None:
    ctx = make_copilot_ctx()
    ctx.executed_block_labels = {"sign_in", "extract"}
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_1"))
    _stash_recorded_run_outcome(ctx, RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_1"))

    assert agent_module._blocks_run_this_turn(ctx) == 2


def test_an_unwound_run_start_is_dropped_and_a_recorded_outcome_is_kept() -> None:
    ctx = make_copilot_ctx()
    _stash_recorded_run_outcome(ctx, RecordedRunOutcome(verdict="not_evaluated", workflow_run_id="wr_1"))
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_2"))

    _forget_interim_run_start(ctx, "wr_2")

    assert [outcome.workflow_run_id for outcome in ctx.terminal_envelope_run_outcomes] == ["wr_1"]
    assert agent_module._blocks_run_this_turn(ctx) == 0


def test_one_stop_report_reads_its_verdict_and_block_count_from_the_same_run() -> None:
    ctx = make_copilot_ctx()
    ctx.executed_block_labels = {"sign_in", "extract"}
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_a"))
    _stash_recorded_run_outcome(ctx, RecordedRunOutcome(verdict="not_demonstrated", workflow_run_id="wr_a"))
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_b"))

    anchor = select_run_outcome_anchor(ctx.terminal_envelope_run_outcomes)

    assert anchor is not None
    assert anchor.workflow_run_id == "wr_b"
    assert run_start_unresolved(ctx.terminal_envelope_run_outcomes)
    assert agent_module._blocks_run_this_turn(ctx) is None


def test_a_run_started_after_a_demonstrated_run_reports_the_new_run_unresolved() -> None:
    ctx = make_copilot_ctx()
    ctx.executed_block_labels = {"sign_in", "extract"}
    _stash_recorded_run_outcome(ctx, RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_a"))
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_b"))

    anchor = select_run_outcome_anchor(ctx.terminal_envelope_run_outcomes)

    assert anchor is not None
    assert anchor.workflow_run_id == "wr_b"
    assert run_start_unresolved(ctx.terminal_envelope_run_outcomes)
    assert agent_module._blocks_run_this_turn(ctx) is None


def test_a_run_that_demonstrated_the_goal_reports_its_real_block_count() -> None:
    ctx = make_copilot_ctx()
    ctx.executed_block_labels = {"sign_in"}
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_1"))
    _stash_recorded_run_outcome(ctx, RecordedRunOutcome(verdict="demonstrated", workflow_run_id="wr_1"))

    assert agent_module._blocks_run_this_turn(ctx) == 1


def test_a_run_that_lost_its_browser_session_supersedes_its_own_run_start() -> None:
    ctx = make_copilot_ctx()
    ctx.executed_block_labels = set()
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_run_side"))

    _commit_run_blocks_record(ctx, _run_side_failed_result(reason_code="browser_session_closed"))

    assert not run_start_unresolved(ctx.terminal_envelope_run_outcomes)
    assert agent_module._blocks_run_this_turn(ctx) == 0


def test_a_stop_on_a_run_that_lost_its_browser_session_never_says_the_count_was_unconfirmed() -> None:
    ctx = make_copilot_ctx()
    ctx.executed_block_labels = set()
    ctx.terminal_envelope_run_outcomes.append(interim_run_start_outcome("wr_run_side"))
    _commit_run_blocks_record(ctx, _run_side_failed_result(reason_code="browser_session_closed"))

    envelope = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        response_kind="stopped",
        blocks_run_this_turn=agent_module._blocks_run_this_turn(ctx),
        run_id="wr_run_side",
        run_verdict="not_evaluated",
    )
    rendered, _ = render_terminal_message(envelope, MINIMAL_CANCEL_STOP, cancelled=True)

    assert "how many of its blocks ran was not confirmed" not in rendered
    assert "0 blocks ran this turn." in rendered


def test_blocks_run_this_turn_reports_zero_when_no_run_was_started() -> None:
    ctx = make_copilot_ctx()
    ctx.executed_block_labels = set()

    assert agent_module._blocks_run_this_turn(ctx) == 0


async def _run_blocks_cancelling_at(monkeypatch: pytest.MonkeyPatch, *, cancel_at: str | None) -> CopilotContext:
    harness = await install_run_blocks_harness(
        monkeypatch,
        workflow_yaml=HANDBACK_WORKFLOW_YAML,
        polled_status="running",
        dispatch_to_worker=cancel_at is None,
    )
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"

    async def _cancel(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    if cancel_at is None:
        harness["worker_execute"].side_effect = _cancel
    else:
        monkeypatch.setattr(run_execution_module, cancel_at, _cancel)
    with pytest.raises(asyncio.CancelledError):
        await run_execution_module._run_blocks_and_collect_debug(
            {"block_labels": ["extract_heading"], "parameters": {}}, ctx
        )
    return ctx


@pytest.mark.asyncio
async def test_a_cancel_before_the_inline_run_starts_leaves_no_run_start_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _run_blocks_cancelling_at(monkeypatch, cancel_at="initialize_skyvern_state_file")

    assert ctx.terminal_envelope_run_outcomes == []
    assert agent_module._blocks_run_this_turn(ctx) == 0


@pytest.mark.asyncio
async def test_a_cancel_once_the_run_is_submitted_keeps_its_unresolved_run_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await _run_blocks_cancelling_at(monkeypatch, cancel_at=None)

    assert [outcome.role for outcome in ctx.terminal_envelope_run_outcomes] == ["interim_build_test"]
    assert agent_module._blocks_run_this_turn(ctx) is None


def test_only_a_stop_click_is_reported_as_the_user_asking() -> None:
    """A Stop click is the user asking; an ambient Escape is the very thing this
    ticket says the product cannot attribute, so it must not claim their intent."""

    def render(source: str | None) -> str:
        envelope = TerminalOutcomeEnvelope(
            next_state="stopped",
            verified=False,
            response_kind="stopped",
            blocks_run_this_turn=0,
            cancel_source=source,
        )
        return render_terminal_message(envelope, MINIMAL_CANCEL_STOP, cancelled=True)[0]

    assert render("stop_button").startswith(CANCEL_STOP_AT_USER_REQUEST)
    for unattributed in ("escape_key", "api", None):
        assert render(unattributed).startswith(MINIMAL_CANCEL_STOP)
        assert "your request" not in render(unattributed)

    # The ordinary build stop preserves a draft, so its seed reply is longer than the
    # bare opening; the upgrade has to replace that opening rather than skip the turn.
    with_draft = TerminalOutcomeEnvelope(
        next_state="stopped",
        verified=False,
        response_kind="stopped",
        blocks_run_this_turn=0,
        cancel_source="stop_button",
        proposal_present=True,
        proposal_disposition="review_untested",
    )
    drafted = render_terminal_message(with_draft, _CANCEL_REPLY_UNVALIDATED, cancelled=True)[0]
    assert drafted.startswith(CANCEL_STOP_AT_USER_REQUEST)
    assert UNTESTED_DRAFT_PRESERVED in drafted


def _asking_row(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cancelled": False,
        "terminalEnvelope": {
            "next_state": "awaiting_user_input",
            "response_kind": "question",
            "user_action_required": True,
            "rendered_from_envelope": True,
        },
    }
    payload.update(overrides)
    return payload


def test_the_last_message_asking_marks_the_chat_awaiting() -> None:
    assert chat_awaits_user_input(sender="ai", narrative_payload=_asking_row()) is True


def test_a_user_reply_after_the_ask_clears_it() -> None:
    # The user answered; the tail of the chat is theirs, so nothing is pending.
    assert chat_awaits_user_input(sender="user", narrative_payload=_asking_row()) is False


def test_a_stop_is_not_an_ask() -> None:
    # The cancel path persists the pre-cancel envelope verbatim.
    assert chat_awaits_user_input(sender="ai", narrative_payload=_asking_row(cancelled=True)) is False


def test_an_unstamped_envelope_is_not_display_authority() -> None:
    row = _asking_row()
    row["terminalEnvelope"]["rendered_from_envelope"] = False
    assert chat_awaits_user_input(sender="ai", narrative_payload=row) is False


def test_a_terminal_state_other_than_awaiting_is_not_an_ask() -> None:
    row = _asking_row()
    row["terminalEnvelope"]["next_state"] = "completed"
    assert chat_awaits_user_input(sender="ai", narrative_payload=row) is False


def test_rows_without_an_envelope_are_not_asks() -> None:
    assert chat_awaits_user_input(sender="ai", narrative_payload=None) is False
    assert chat_awaits_user_input(sender="ai", narrative_payload={}) is False
    assert chat_awaits_user_input(sender="ai", narrative_payload={"terminalEnvelope": None}) is False
    assert chat_awaits_user_input(sender="ai", narrative_payload={"terminalEnvelope": "nonsense"}) is False
