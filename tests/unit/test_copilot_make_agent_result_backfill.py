"""`_make_agent_result` back-fills concrete terminal facts onto the narrative payload."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.agent import (
    _finalize_result_with_blocker_override,
    _make_agent_result,
    _terminal_failed_operation,
)
from skyvern.forge.sdk.copilot.agent_context import CredentialOriginRecovery
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.build_test_connect_failure import BuildTestConnectFailure
from skyvern.forge.sdk.copilot.build_test_outcome import (
    BuildTestFailedOperation,
    RecordedBuildTestOutcome,
    record_build_test_outcome,
)
from skyvern.forge.sdk.copilot.context import (
    AgentResult,
    CopilotContext,
    StructuredContext,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatMessage, WorkflowCopilotChatSender
from tests.unit.copilot_test_helpers import failed_second_factor_run
from tests.unit.copilot_test_helpers import make_copilot_ctx as _ctx
from tests.unit.copilot_test_helpers import make_missing_credential_ask_contract, passing_run, two_page_login_yaml


def _outcome(kind: ResponseKind) -> TurnOutcome:
    return TurnOutcome(response_kind=kind)


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
        "terminalMessage": "done",
        "narrativeSummary": "Built it.",
        "priorBlockCount": None,
        "designActivity": [],
        "startedAt": None,
        "endedAt": None,
    }
    base.update(overrides)
    return base


def _result(ctx: CopilotContext | None, **kwargs: object):
    kwargs.setdefault("user_response", "ok")
    kwargs.setdefault("updated_workflow", None)
    kwargs.setdefault("global_llm_context", None)
    return _make_agent_result(ctx, **kwargs)


def test_plain_reply_backfill_uses_concrete_answer_kind() -> None:
    result = _result(_ctx(), turn_outcome=_outcome(ResponseKind.CLARIFY), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "answer"


def test_backfill_never_overwrites_explicit_values() -> None:
    payload = _payload(responseKind="refuse")
    result = _result(_ctx(), turn_outcome=_outcome(ResponseKind.CLARIFY), narrative_payload=payload)
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "refuse"


def test_backfill_tolerates_turn_outcome_none() -> None:
    result = _result(_ctx(), turn_outcome=None, narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert "responseKind" not in result.narrative_payload


def test_backfill_adds_connection_cards_to_the_terminal_payload() -> None:
    ctx = _ctx()
    card = {
        "integrationId": "outlook",
        "integrationName": "Outlook",
        "provider": "microsoft",
        "scopeProfile": "outlook_mail",
        "requiredScopes": ["Mail.Read"],
        "reconnectConnectionId": None,
    }
    ctx.connection_cards = [card]

    result = _result(ctx, turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())
    empty = _result(_ctx(), turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())

    assert result.narrative_payload is not None
    assert result.narrative_payload["connectionCards"] == [card]
    assert empty.narrative_payload is not None
    assert "connectionCards" not in empty.narrative_payload


def test_a_legacy_outcome_with_google_setup_offers_still_loads() -> None:
    legacy = TurnOutcome.model_validate(
        {"response_kind": "clarify", "google_setup_offers": [{"scope_profile": "gmail", "connection_id": None}]}
    )

    assert legacy.response_kind == ResponseKind.CLARIFY


@pytest.mark.parametrize(
    "classifier_kind",
    [ResponseKind.ANSWER, ResponseKind.BUILD, ResponseKind.CLARIFY, ResponseKind.DIAGNOSE, ResponseKind.REFUSE],
)
def test_classifier_response_kind_does_not_terminalize_identical_plain_reply(
    classifier_kind: ResponseKind,
) -> None:
    result = _result(
        _ctx(),
        turn_outcome=_outcome(classifier_kind),
        response_type="REPLY",
        narrative_payload=_payload(),
    )

    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "answer"


@pytest.mark.parametrize(
    "classifier_kind",
    [ResponseKind.ANSWER, ResponseKind.BUILD, ResponseKind.CLARIFY, ResponseKind.DIAGNOSE, ResponseKind.REFUSE],
)
def test_concrete_workflow_attempt_terminalizes_independently_of_classifier_kind(
    classifier_kind: ResponseKind,
) -> None:
    ctx = _ctx()
    ctx.update_workflow_called = True

    result = _result(
        ctx,
        turn_outcome=_outcome(classifier_kind),
        response_type="REPLY",
        narrative_payload=_payload(),
    )

    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "build"


def test_backfill_tolerates_ctx_none() -> None:
    result = _result(None, turn_outcome=_outcome(ResponseKind.REFUSE), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "refuse"


def test_recorded_browser_operation_failure_overrides_success_prose_but_keeps_draft() -> None:
    ctx = _ctx()
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_browser_operation",
        workflow_run_block_id="wrb_capture_failure",
        block_label="collect_failure_rate",
        failing_line=11,
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            attempted_block_label="collect_failure_rate",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_browser_operation",
            block_labels=["collect_failure_rate"],
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )
    draft = SimpleNamespace(name="untested draft")
    assert _terminal_failed_operation(ctx) == failed_operation

    result = _result(
        ctx,
        user_response="Destination write completed successfully.",
        updated_workflow=draft,
        workflow_yaml=two_page_login_yaml(),
        proposal_disposition="auto_applicable",
        turn_outcome=_outcome(ResponseKind.BUILD),
        narrative_payload=_payload(
            terminalMessage="Destination write completed successfully.",
            narrativeSummary="Destination write completed successfully.",
        ),
    )

    assert result.updated_workflow == draft
    assert result.proposal_disposition == "review_untested"
    assert _terminal_failed_operation(ctx) == failed_operation
    # The model's own report is delivered unchanged; the failure rides its own record.
    assert result.user_response == "Destination write completed successfully."
    assert result.narrative_payload is not None
    assert result.narrative_payload["turnFacts"]["terminalCause"] == "browser_operation_failed"
    assert result.narrative_payload["turnFacts"]["ranCleanOnCurrentSource"] is False


def test_later_connect_failure_owns_terminal_copy_over_failed_operation() -> None:
    ctx = _ctx()
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_failed_operation",
        workflow_run_block_id="wrb_failed_operation",
        block_label="collect_failure_rate",
        failing_line=11,
    )
    connect_failure = BuildTestConnectFailure(
        state="cdp_connect_failed",
        browser_session_id="pbs_connect_failure",
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            attempted_block_label="collect_failure_rate",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_failed_operation",
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
            connect_failure=connect_failure,
        ),
    )

    result = _result(
        ctx,
        user_response="The code run failed.",
        updated_workflow=SimpleNamespace(name="untested draft"),
        workflow_yaml=two_page_login_yaml(),
        proposal_disposition="review_untested",
        turn_outcome=_outcome(ResponseKind.BUILD),
        narrative_payload=_payload(),
    )

    assert result.user_response == "The code run failed."
    assert result.narrative_payload is not None
    assert result.narrative_payload["turnFacts"]["terminalCause"] == "cdp_connect_failed"


def test_recorded_browser_operation_failure_survives_later_non_clearing_outcome() -> None:
    ctx = _ctx()
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_browser_operation",
        workflow_run_block_id="wrb_capture_failure",
        block_label="collect_failure_rate",
        failing_line=11,
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            attempted_tool="update_and_run_blocks",
            attempted_block_label="collect_failure_rate",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_browser_operation",
            requested_block_labels=["collect_failure_rate"],
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="scout_evaluate",
            attempted_tool="inspect_page_for_composition",
            verdict="progress_observed",
            reason_code="verified_success",
        ),
    )

    assert _terminal_failed_operation(ctx) == failed_operation


def test_recorded_browser_operation_failure_survives_auto_applicable_question_precedence() -> None:
    ctx = _ctx()
    failed_operation = BuildTestFailedOperation(
        kind="browser_operation_failed",
        workflow_run_id="wr_browser_operation",
        block_label="collect_failure_rate",
        failing_line=1,
    )
    record_build_test_outcome(
        ctx,
        RecordedBuildTestOutcome(
            phase="persisted_block_run",
            verdict="repairable_failure",
            reason_code="runtime_block_failure",
            workflow_run_id="wr_browser_operation",
            structural_failure_identity="browser-operation",
            failed_operation=failed_operation,
        ),
    )

    result = _result(
        ctx,
        response_type="ASK_QUESTION",
        user_response="The destination write completed. Which account should I use?",
        proposal_disposition="auto_applicable",
        turn_outcome=_outcome(ResponseKind.CLARIFY),
        narrative_payload=_payload(),
    )

    assert _terminal_failed_operation(ctx) == failed_operation
    # A failure record forces review even on a question turn, and never rewrites the ask.
    assert result.proposal_disposition == "no_proposal"
    assert result.user_response == "The destination write completed. Which account should I use?"


def test_result_carries_exact_model_contract_deletion_to_auto_accept() -> None:
    ctx = _ctx()
    ctx.clear_persisted_completion_contract = True

    result = _result(ctx, turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())

    assert result.clear_persisted_completion_contract is True


def test_backfill_tolerates_missing_payload() -> None:
    with pytest.raises(ValueError, match="narrative_payload"):
        _result(_ctx(), turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=None)


def test_missing_payload_is_allowed_without_ctx() -> None:
    result = _result(None, turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=None)
    assert result.narrative_payload is None


def test_backfill_adds_credential_prompt_for_typed_clarification_reason() -> None:
    ctx = _ctx(request_policy=RequestPolicy(clarification_reason="credential_name_unresolved"))
    result = _result(ctx, turn_outcome=_outcome(ResponseKind.CLARIFY), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["credentialPrompt"] == {"reason": "credential_name_unresolved"}


def test_blocker_override_path_adds_credential_prompt_from_request_policy() -> None:
    ctx = _ctx(request_policy=RequestPolicy(clarification_reason="credential_name_unresolved"))
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Reply to the user without updating the workflow.",
        user_facing_reason="I couldn't find the required credentials for the existing workflow.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="no_mutation_run_blocked",
        blocked_tool="update_workflow",
    )
    pre_override = AgentResult(user_response="agent reply", updated_workflow=None, global_llm_context=None)

    overridden = _finalize_result_with_blocker_override(ctx, pre_override)

    assert overridden.narrative_payload is not None
    assert overridden.narrative_payload["credentialPrompt"] == {"reason": "credential_name_unresolved"}


def test_backfill_omits_credential_prompt_for_prose_link_without_policy_signal() -> None:
    result = _result(
        _ctx(),
        user_response="To remove saved credentials, select them on [Credentials](/credentials) and confirm.",
        narrative_payload=_payload(),
    )
    assert result.narrative_payload is not None
    assert "credentialPrompt" not in result.narrative_payload


def test_backfill_adds_credential_prompt_from_a_run_derived_ask_without_clarification_or_pause() -> None:
    ctx = _ctx(request_policy=RequestPolicy())
    ctx.latest_diagnosis_repair_contract = make_missing_credential_ask_contract()
    result = _result(
        ctx,
        user_response="Add a credential at [Credentials](/credentials) so I can test the sign-in.",
        narrative_payload=_payload(),
    )
    assert result.narrative_payload is not None
    assert result.narrative_payload["credentialPrompt"] == {"reason": "missing_credential_run_failure"}
    assert "credentialPause" not in result.narrative_payload


def test_backfill_omits_credential_prompt_once_the_run_derived_ask_was_answered() -> None:
    ctx = _ctx(request_policy=RequestPolicy())
    ctx.latest_diagnosis_repair_contract = make_missing_credential_ask_contract()
    ctx.credential_pause_outcome = "connected"
    result = _result(ctx, narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert "credentialPrompt" not in result.narrative_payload


def test_backfill_omits_run_derived_credential_prompt_while_origin_recovery_is_pending() -> None:
    ctx = _ctx(request_policy=RequestPolicy())
    ctx.latest_diagnosis_repair_contract = make_missing_credential_ask_contract()
    ctx.credential_origin_recovery = CredentialOriginRecovery("https://idp.example.test", "pending", "cred_service")
    result = _result(ctx, narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert "credentialPrompt" not in result.narrative_payload


def test_backfill_emits_credential_auto_bound_receipt() -> None:
    ctx = _ctx(
        request_policy=RequestPolicy(
            auto_bound_credentials=[SimpleNamespace(credential_id="cred_work", name="Work login")]
        )
    )
    result = _result(ctx, turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["credentialAutoBound"] == {"credentialId": "cred_work", "name": "Work login"}


def test_backfill_omits_credential_auto_bound_when_nothing_bound() -> None:
    result = _result(
        _ctx(request_policy=RequestPolicy()),
        turn_outcome=_outcome(ResponseKind.BUILD),
        narrative_payload=_payload(),
    )
    assert result.narrative_payload is not None
    assert "credentialAutoBound" not in result.narrative_payload


def test_backfill_credential_auto_bound_names_the_most_recent_bind() -> None:
    # Turn-start bound one credential and a later live page bound another; the receipt names the most
    # recent — the credential the run is actually signing in with now.
    ctx = _ctx(
        request_policy=RequestPolicy(
            auto_bound_credentials=[
                SimpleNamespace(credential_id="cred_turn_start", name="First"),
                SimpleNamespace(credential_id="cred_live_page", name="Second"),
            ]
        )
    )
    result = _result(ctx, turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["credentialAutoBound"] == {"credentialId": "cred_live_page", "name": "Second"}


def test_credential_auto_bound_survives_narrative_payload_serialization() -> None:
    # The persisted/streamed wire model validates narrative_payload against the TurnNarrativePayload
    # TypedDict and drops any key it does not declare, so the credentialAutoBound line is load-bearing:
    # without it the field silently vanishes from model_dump and the FE loses the receipt on reload.
    message = WorkflowCopilotChatMessage(
        workflow_copilot_chat_message_id="m1",
        workflow_copilot_chat_id="c1",
        sender=WorkflowCopilotChatSender.AI,
        content="done",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        narrative_payload=_payload(credentialAutoBound={"credentialId": "cred_x", "name": "Work login"}),
    )
    dumped = message.model_dump()["narrative_payload"]
    assert dumped["credentialAutoBound"] == {"credentialId": "cred_x", "name": "Work login"}


def test_backfill_stamps_where_the_credential_pause_was_raised() -> None:
    ctx = _ctx()
    ctx.credential_pause_outcome = "connected"
    ctx.credential_pause_connected_credential_id = "cred_work"
    ctx.credential_pause_anchor_tool_call_id = "call-run"
    result = _result(ctx, turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["credentialPause"] == {
        "outcome": "connected",
        "credentialId": "cred_work",
        "anchorToolCallId": "call-run",
    }


def test_a_turns_plan_and_pause_anchor_survive_narrative_payload_serialization() -> None:
    # The wire model drops keys the TypedDicts do not declare, which would lose the turn's plan and
    # the pause's position on reload while the live stream still showed them.
    message = WorkflowCopilotChatMessage(
        workflow_copilot_chat_message_id="m1",
        workflow_copilot_chat_id="c1",
        sender=WorkflowCopilotChatSender.AI,
        content="done",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        narrative_payload=_payload(
            workPlan={"toolCallId": "p1", "items": ["Open the admin page", "Create the user"]},
            credentialPause={"outcome": "skipped", "anchorToolCallId": "p1"},
        ),
    )
    dumped = message.model_dump()["narrative_payload"]
    assert dumped["workPlan"] == {"toolCallId": "p1", "items": ["Open the admin page", "Create the user"]}
    assert dumped["credentialPause"]["anchorToolCallId"] == "p1"


def test_backfill_omits_credential_prompt_when_no_signal_present() -> None:
    result = _result(_ctx(), user_response="Done, the workflow is ready.", narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert "credentialPrompt" not in result.narrative_payload


def test_make_agent_result_records_resolved_credentials_as_durable_approval() -> None:
    ctx = _ctx(request_policy=RequestPolicy(resolved_credentials=[SimpleNamespace(credential_id="cred_portal")]))

    result = _result(ctx, narrative_payload=_payload())

    approved = StructuredContext.from_json_str(result.global_llm_context).approved_credentials
    assert [record.credential_id for record in approved] == ["cred_portal"]


def test_a_pass_after_a_recorded_failure_leaves_the_supplied_reply_untouched() -> None:
    ctx = _ctx(workflow_yaml=two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", ["sign_in_and_read"]))
    ctx.last_run_blocks_workflow_run_id = "wr_2"
    ctx.dispatched_run_ids_this_turn.add("wr_2")
    reply = "Updated the workflow and the retest passed. The first run failed at sign-in, so that step is unproven."

    result = _result(
        ctx,
        user_response=reply,
        turn_outcome=_outcome(ResponseKind.BUILD),
        narrative_payload=_payload(terminalMessage=reply, narrativeSummary=reply),
    )

    assert result.user_response == reply
    assert result.narrative_payload is not None
    assert result.narrative_payload["terminalMessage"] == reply
    assert result.narrative_payload["narrativeSummary"] == reply
    assert [entry["workflow_run_id"] for entry in ctx.recorded_build_test_outcome_history] == ["wr_1", "wr_2"]
