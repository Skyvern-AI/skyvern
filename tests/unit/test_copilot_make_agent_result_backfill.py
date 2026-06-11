"""`_make_agent_result` back-fills the typed terminal adjudication onto the
narrative payload: ``responseKind`` from ``TurnOutcome.response_kind`` and
``verifiedSuccess`` from ``enforcement.verified_goal_satisfied_context``."""

from __future__ import annotations

from unittest.mock import MagicMock

from skyvern.forge.sdk.copilot.agent import _make_agent_result
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairNextAction,
    VerificationResult,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome


def _ctx(**overrides: object) -> CopilotContext:
    defaults: dict = dict(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
    )
    defaults.update(overrides)
    return CopilotContext(**defaults)


def _verified_goal_ctx() -> CopilotContext:
    return _ctx(
        last_test_ok=True,
        last_full_workflow_test_ok=True,
        latest_diagnosis_repair_contract=DiagnosisRepairContract(
            diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
            diagnosis_result=DiagnosisResult(),
            repair_decision=RepairDecision(next_action=RepairNextAction.NO_CHANGE),
            verification_result=VerificationResult(
                user_goal_satisfied=True,
                completion_contract_satisfied=True,
            ),
        ),
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
        "narrativeSummary": None,
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


def test_backfill_verified_success_true_when_gate_passes() -> None:
    result = _result(_verified_goal_ctx(), turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "build"
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
    result = _result(_ctx(), turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=None)
    assert result.narrative_payload is None
