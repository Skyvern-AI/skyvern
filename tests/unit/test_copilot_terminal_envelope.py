from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing

from skyvern.exceptions import get_user_facing_exception_message
from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
)
from skyvern.forge.sdk.copilot.build_test_connect_failure import build_test_connect_failure_sentence
from skyvern.forge.sdk.copilot.build_test_outcome import BuildTestConnectFailure, BuildTestFailedOperation
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
from skyvern.forge.sdk.copilot.secret_scrub import clear_session_scrub_values, register_secret_scrub_value
from skyvern.forge.sdk.copilot.terminal_envelope import (
    MINIMAL_HONEST_STOP,
    TerminalOutcomeEnvelope,
    assemble_terminal_envelope,
    finalize_applied_state,
    reason_in_reply_shadow,
    render_terminal_message,
)
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _record_run_blocks_result,
    _stamp_run_side_connect_failure,
    _stash_recorded_run_outcome,
)
from skyvern.forge.sdk.copilot.tools.workflow_update import _record_workflow_update_result
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from tests.unit.copilot_test_helpers import make_copilot_ctx

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


def _interim_outcome(verdict: str, display_reason: str | None = None) -> RecordedRunOutcome:
    return RecordedRunOutcome(verdict=verdict, display_reason=display_reason, role="interim_build_test")


def test_run_anchor_ignores_interim_not_demonstrated_when_recorded_run_follows() -> None:
    envelope = _assemble(
        run_outcomes=[
            _interim_outcome("not_demonstrated", "The scout has not produced the goal yet."),
            _run_outcome("not_evaluated", "The later run completed."),
        ]
    )

    assert envelope.run_verdict == "not_evaluated"
    assert envelope.run_display_reason == "The later run completed."


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
    assert rendered.startswith(message)
    assert rendered != "I stopped without confirming the goal was met."
    assert replaced is True


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


def test_render_terminal_message_held_draft_text_survives_with_deadline_cause() -> None:
    envelope = _assemble(proposal_disposition="review_untested", terminal_cause="deadline_expired")
    message = agent_module._TIMEOUT_REPLY_UNVALIDATED

    rendered, replaced = render_terminal_message(envelope, message, cancelled=False)

    assert envelope.next_state == "proposal_pending"
    assert rendered.startswith(message)
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


def test_run_without_output_projects_unverified_blocker_never_success() -> None:
    blocker = "The run stayed queued and produced no terminal result before the deadline."
    envelope = _assemble(
        proposal_disposition="auto_applicable",
        run_outcomes=[RecordedRunOutcome(verdict="not_evaluated", output_report=None)],
        blocker_reason=blocker,
        halt_kind="deadline_expired",
        terminal_cause="deadline_expired",
    )
    finalized = finalize_applied_state(envelope, applied=False)

    rendered, replaced = render_terminal_message(finalized, agent_module._TIMEOUT_REPLY_DEFAULT, cancelled=False)

    assert finalized.verified is False
    assert finalized.workflow_applied is False
    assert finalized.next_state == "stopped"
    assert finalized.response_kind == "stopped"
    assert finalized.run_output_report is None
    assert finalized.blocker_reason == blocker
    assert rendered.startswith(agent_module._TIMEOUT_REPLY_DEFAULT)
    assert "The recorded run's outcome was not evaluated." in rendered
    assert "The turn reached its time limit." in rendered
    assert replaced is True
    assert "completed" not in rendered.lower()
    assert "success" not in rendered.lower()


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
        internal_reason_code="no_mutation_run_blocked",
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


def test_a_deadline_turn_states_blocks_run_evaluation_and_the_time_limit() -> None:
    envelope = _assemble(
        proposal_disposition="review_tested",
        run_outcomes=[RecordedRunOutcome(verdict="not_evaluated", run_completed=True)],
        terminal_cause="deadline_expired",
        blocks_run_this_turn=1,
    )

    rendered, replaced = render_terminal_message(envelope, agent_module._TIMEOUT_REPLY_TESTED, cancelled=False)

    assert "1 block ran this turn." in rendered
    assert "The recorded run completed, and its outcome was not evaluated." in rendered
    assert "reached its time limit" in rendered
    assert "failed" not in rendered.lower()
    assert replaced is True


def test_an_unevaluated_outcome_alone_never_claims_the_run_finished() -> None:
    envelope = _assemble(
        proposal_disposition="review_tested",
        run_outcomes=[_run_outcome("not_evaluated")],
        terminal_cause="deadline_expired",
        blocks_run_this_turn=1,
    )

    rendered, _ = render_terminal_message(envelope, agent_module._TIMEOUT_REPLY_TESTED, cancelled=False)

    assert "The recorded run's outcome was not evaluated." in rendered
    assert "run completed" not in rendered


@pytest.mark.parametrize("run_outcomes", [[], [_run_outcome("evaluating")]])
def test_a_deadline_turn_without_a_settled_verdict_states_only_what_it_knows(
    run_outcomes: list[RecordedRunOutcome],
) -> None:
    envelope = _assemble(
        proposal_disposition="review_tested",
        run_outcomes=run_outcomes,
        terminal_cause="deadline_expired",
        blocks_run_this_turn=2,
    )

    rendered, _ = render_terminal_message(envelope, agent_module._TIMEOUT_REPLY_TESTED, cancelled=False)

    assert "2 blocks ran this turn." in rendered
    assert "reached its time limit" in rendered
    assert "not evaluated" not in rendered


def test_a_deadline_turn_with_no_run_facts_leaves_the_agent_copy_alone() -> None:
    envelope = _assemble(proposal_disposition="review_tested", terminal_cause="deadline_expired")

    rendered, replaced = render_terminal_message(envelope, agent_module._TIMEOUT_REPLY_UNVALIDATED, cancelled=False)

    assert rendered == agent_module._TIMEOUT_REPLY_UNVALIDATED
    assert replaced is True


def test_a_deadline_turn_omits_the_block_count_it_was_never_given() -> None:
    envelope = _assemble(
        proposal_disposition="review_tested",
        run_outcomes=[_run_outcome("not_evaluated")],
        terminal_cause="deadline_expired",
    )

    rendered, _ = render_terminal_message(envelope, agent_module._TIMEOUT_REPLY_TESTED, cancelled=False)

    assert "ran this turn" not in rendered
    assert "reached its time limit" in rendered


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
