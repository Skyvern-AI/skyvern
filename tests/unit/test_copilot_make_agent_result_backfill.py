"""`_make_agent_result` back-fills the typed terminal adjudication onto the
narrative payload: ``responseKind`` from ``TurnOutcome.response_kind`` and
``verifiedSuccess`` from ``enforcement.verified_goal_satisfied_context``."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.agent import _finalize_result_with_blocker_override, _make_agent_result
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.build_test_outcome import record_build_test_outcome
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
from tests.unit.copilot_test_helpers import make_verified_goal_contract, passing_run, two_page_login_yaml


def _verified_goal_ctx() -> CopilotContext:
    return _ctx(
        last_test_ok=True,
        last_full_workflow_test_ok=True,
        latest_diagnosis_repair_contract=make_verified_goal_contract(),
    )


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


def test_backfill_writes_both_fields_together() -> None:
    result = _result(_ctx(), turn_outcome=_outcome(ResponseKind.CLARIFY), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "clarify"
    assert result.narrative_payload["verifiedSuccess"] is False


def test_backfill_verified_success_requires_adjudicated_evidence() -> None:
    # The legacy run-status conjunction still ends the turn but no longer backs
    # a verified-success claim: without judge-confirmed outcome evidence the
    # claim tier renders built-but-unverified.
    result = _result(_verified_goal_ctx(), turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "build"
    assert result.narrative_payload["verifiedSuccess"] is False


def test_backfill_verified_success_true_when_outcome_fully_verified() -> None:
    from skyvern.forge.sdk.copilot.completion_verification import (
        CompletionVerificationResult,
        CriterionVerdict,
    )

    ctx = _verified_goal_ctx()
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms")],
    )
    result = _result(ctx, turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["verifiedSuccess"] is True


def test_backfill_never_overwrites_explicit_values() -> None:
    payload = _payload(responseKind="refuse", verifiedSuccess=True)
    result = _result(_ctx(), turn_outcome=_outcome(ResponseKind.CLARIFY), narrative_payload=payload)
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "refuse"
    assert result.narrative_payload["verifiedSuccess"] is True


def test_backfill_tolerates_turn_outcome_none() -> None:
    result = _result(_ctx(), turn_outcome=None, narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert "responseKind" not in result.narrative_payload
    assert result.narrative_payload["verifiedSuccess"] is False


def test_backfill_tolerates_ctx_none() -> None:
    result = _result(None, turn_outcome=_outcome(ResponseKind.REFUSE), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "refuse"
    assert "verifiedSuccess" not in result.narrative_payload


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
    ctx = _ctx(request_policy=RequestPolicy(clarification_reason="workflow_credential_inputs_unbound"))
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Reply to the user without updating the workflow.",
        user_facing_reason="I couldn't find the required credentials for the existing workflow.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="turn_intent_no_mutation_run_blocked",
        blocked_tool="update_workflow",
    )
    pre_override = AgentResult(user_response="agent reply", updated_workflow=None, global_llm_context=None)

    overridden = _finalize_result_with_blocker_override(ctx, pre_override)

    assert overridden.narrative_payload is not None
    assert overridden.narrative_payload["credentialPrompt"] == {"reason": "workflow_credential_inputs_unbound"}


def test_backfill_adds_credential_prompt_from_text_marker_when_no_policy_signal() -> None:
    result = _result(
        _ctx(),
        user_response="You can add one at https://app.skyvern.com/credentials.",
        narrative_payload=_payload(),
    )
    assert result.narrative_payload is not None
    assert result.narrative_payload["credentialPrompt"] == {"reason": "assistant_directed"}


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


def test_backfill_omits_credential_prompt_when_no_signal_present() -> None:
    result = _result(_ctx(), user_response="Done, the workflow is ready.", narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert "credentialPrompt" not in result.narrative_payload


def test_make_agent_result_records_resolved_credentials_as_durable_approval() -> None:
    ctx = _ctx(request_policy=RequestPolicy(resolved_credentials=[SimpleNamespace(credential_id="cred_portal")]))

    result = _result(ctx, narrative_payload=_payload())

    approved = StructuredContext.from_json_str(result.global_llm_context).approved_credentials
    assert [record.credential_id for record in approved] == ["cred_portal"]


def _ctx_with_open_second_factor_failure(
    *, later_run_labels: list[str], final_selector: str = "Login"
) -> CopilotContext:
    ctx = _ctx(workflow_yaml=two_page_login_yaml())
    record_build_test_outcome(ctx, failed_second_factor_run("wr_1"))
    record_build_test_outcome(ctx, passing_run("wr_2", later_run_labels))
    ctx.workflow_yaml = two_page_login_yaml(submit_selector=final_selector)
    return ctx


def test_build_turn_reports_the_failure_no_later_run_re_exercised() -> None:
    ctx = _ctx_with_open_second_factor_failure(later_run_labels=["read_metric"])

    result = _result(
        ctx,
        user_response="Built it. The workflow reads the visitor count.",
        turn_outcome=_outcome(ResponseKind.BUILD),
        narrative_payload=_payload(),
    )

    assert result.turn_outcome is not None
    unresolved = result.turn_outcome.unresolved_runtime_failure
    assert unresolved is not None
    assert unresolved.workflow_run_id == "wr_1"
    assert unresolved.block_label == "sign_in_and_read"
    assert "wr_1" in result.user_response
    assert "sign_in_and_read" in result.user_response
    assert "Built it. The workflow reads the visitor count." in result.user_response


def test_the_qualified_turn_is_still_a_success() -> None:
    ctx = _ctx_with_open_second_factor_failure(later_run_labels=["read_metric"])

    result = _result(
        ctx,
        user_response="Built it.",
        turn_outcome=_outcome(ResponseKind.BUILD),
        narrative_payload=_payload(),
    )

    assert result.turn_outcome is not None
    assert result.turn_outcome.unresolved_runtime_failure is not None
    assert result.turn_outcome.response_kind == ResponseKind.BUILD
    assert result.turn_outcome.terminal_reason is None


def test_a_re_exercised_failure_leaves_the_reply_and_outcome_unqualified() -> None:
    ctx = _ctx_with_open_second_factor_failure(later_run_labels=["sign_in_and_read"], final_selector="Continue")

    result = _result(
        ctx,
        user_response="Built it.",
        turn_outcome=_outcome(ResponseKind.BUILD),
        narrative_payload=_payload(),
    )

    assert result.turn_outcome is not None
    assert result.turn_outcome.unresolved_runtime_failure is None
    assert result.user_response == "Built it."


def test_a_clarifying_turn_is_never_qualified() -> None:
    ctx = _ctx_with_open_second_factor_failure(later_run_labels=["read_metric"])

    result = _result(
        ctx,
        user_response="Which metric did you want?",
        turn_outcome=_outcome(ResponseKind.CLARIFY),
        narrative_payload=_payload(),
    )

    assert result.turn_outcome is not None
    assert result.turn_outcome.unresolved_runtime_failure is None
    assert result.user_response == "Which metric did you want?"


def test_the_qualification_also_rides_the_narrative_terminal_message() -> None:
    """The chat panel renders the narrative card, not the raw reply, so both carry the note."""
    ctx = _ctx_with_open_second_factor_failure(later_run_labels=["read_metric"])

    result = _result(
        ctx,
        user_response="Built it.",
        turn_outcome=_outcome(ResponseKind.BUILD),
        narrative_payload=_payload(terminalMessage="All done.", narrativeSummary="All done."),
    )

    assert result.narrative_payload is not None
    for key in ("terminalMessage", "narrativeSummary"):
        assert "wr_1" in result.narrative_payload[key], key
        assert "sign_in_and_read" in result.narrative_payload[key], key
