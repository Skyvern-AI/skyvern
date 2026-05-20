from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.tools import _turn_intent_tool_error, _update_workflow
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentAuthority, TurnIntentMode


def _ctx(turn_intent: TurnIntent) -> CopilotContext:
    return CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        turn_intent=turn_intent,
    )


@pytest.mark.parametrize(
    ("mode", "tool_name"),
    [
        (TurnIntentMode.DOCS_ANSWER, "update_workflow"),
        (TurnIntentMode.DIAGNOSE, "run_blocks_and_collect_debug"),
        (TurnIntentMode.CLARIFY, "update_and_run_blocks"),
        (TurnIntentMode.REFUSE, "update_and_run_blocks"),
    ],
)
def test_no_mutation_turn_intent_blocks_mutating_tools(mode: TurnIntentMode, tool_name: str) -> None:
    intent = TurnIntent(
        mode=mode,
        authority=TurnIntentAuthority(
            may_update_workflow=False,
            may_run_blocks=False,
            requires_user_input=mode in {TurnIntentMode.CLARIFY, TurnIntentMode.REFUSE},
        ),
        missing_context_question="Which target should I use?" if mode == TurnIntentMode.CLARIFY else None,
    )

    error = _turn_intent_tool_error(_ctx(intent), tool_name)

    assert error is not None
    assert f"`{mode.value}`" in error
    assert f"`{tool_name}`" in error
    assert "Do not update workflow YAML or run browser blocks" in error
    assert "safe_reason_code=turn_intent_no_mutation" in error


def test_turn_intent_gate_allows_draft_update_without_run_authority() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DRAFT_ONLY,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False),
    )

    assert _turn_intent_tool_error(_ctx(intent), "update_workflow") is None


def test_turn_intent_gate_defers_partial_authority_to_request_policy() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.CLARIFY,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False, requires_user_input=True),
    )

    assert _turn_intent_tool_error(_ctx(intent), "update_and_run_blocks") is None


@pytest.mark.asyncio
async def test_update_workflow_stops_before_persisting_for_answer_only_intent() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DOCS_ANSWER,
        authority=TurnIntentAuthority(may_update_workflow=False, may_run_blocks=False),
    )
    ctx = _ctx(intent)

    with patch("skyvern.forge.sdk.copilot.tools.app") as mock_app:
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": ctx.workflow_yaml}, ctx)

    assert result["ok"] is False
    assert "`docs_answer`" in result["error"]
    mock_app.WORKFLOW_SERVICE.update_workflow_definition.assert_not_called()


def test_update_and_run_blocks_reports_run_blocked_priority() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DIAGNOSE,
        authority=TurnIntentAuthority(may_update_workflow=False, may_run_blocks=False),
    )
    error = _turn_intent_tool_error(_ctx(intent), "update_and_run_blocks")

    assert error is not None
    assert "turn_intent_no_mutation_run_blocked" in error


def test_answer_only_diagnose_blocks_get_run_results_tool() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DIAGNOSE,
        authority=TurnIntentAuthority(may_update_workflow=False, may_run_blocks=False),
    )

    error = _turn_intent_tool_error(_ctx(intent), "get_run_results")

    assert error is not None
    assert "`diagnose`" in error
    assert "`get_run_results`" in error
    assert "fetch additional run context with tools" in error
    assert "turn_intent_no_mutation_context_read_blocked" in error
