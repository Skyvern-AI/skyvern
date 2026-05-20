from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools import _turn_intent_tool_error, _update_workflow
from skyvern.forge.sdk.copilot.turn_intent import (
    UNRESOLVED_BLOCK_REF_TARGET_ENTITY,
    TurnIntent,
    TurnIntentAuthority,
    TurnIntentMode,
)


def _ctx(turn_intent: TurnIntent, request_policy: RequestPolicy | None = None) -> CopilotContext:
    return CopilotContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="title: Existing\nworkflow_definition:\n  blocks: []\n",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        turn_intent=turn_intent,
        request_policy=request_policy,
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


def test_turn_intent_gate_blocks_draft_only_run_tools() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.DRAFT_ONLY,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False),
    )

    error = _turn_intent_tool_error(_ctx(intent), "update_and_run_blocks")

    assert error is not None
    assert "Use `update_workflow` only" in error
    assert "safe_reason_code=turn_intent_run_blocked" in error


def test_turn_intent_gate_allows_build_update_and_run_authority() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.BUILD,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )

    assert _turn_intent_tool_error(_ctx(intent), "update_and_run_blocks") is None


def test_turn_intent_gate_blocks_edit_without_target_context() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )

    error = _turn_intent_tool_error(_ctx(intent), "update_workflow")

    assert error is not None
    assert "could not identify a specific workflow edit target" in error
    assert "safe_reason_code=turn_intent_missing_edit_target" in error


def test_turn_intent_gate_allows_edit_with_target_context() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        target_entities={"workflow": ["wfp-1"]},
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )

    assert _turn_intent_tool_error(_ctx(intent), "update_and_run_blocks") is None


def test_turn_intent_gate_blocks_edit_with_unresolved_label_reference() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        target_entities={
            "workflow": ["current_workflow"],
            UNRESOLVED_BLOCK_REF_TARGET_ENTITY: ["WF_trigger_SSO_login", "update_card"],
        },
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx = _ctx(intent)
    ctx.user_message = "WF_trigger_SSO_login worked but update_card is not receiving browser state."
    ctx.workflow_yaml = """
title: Public SSO login cleanup
workflow_definition:
  parameters:
    - parameter_type: workflow
      key: account_number
  blocks:
    - block_type: goto_url
      label: navigate_to_SSO
      url: https://the-internet.herokuapp.com/login
    - block_type: navigation
      label: block_placeholder
      navigation_goal: Confirm success.
"""

    error = _turn_intent_tool_error(ctx, "update_and_run_blocks")

    assert error is not None
    assert "WF_trigger_SSO_login" in error
    assert "update_card" in error
    assert "navigate_to_SSO" in error
    assert "safe_reason_code=turn_intent_unresolved_edit_target" in error


def test_turn_intent_gate_does_not_scan_raw_user_message_for_snake_case_refs() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        target_entities={"workflow": ["current_workflow"]},
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx = _ctx(intent)
    ctx.user_message = "Update the workflow so last_name is extracted as a required field."

    assert _turn_intent_tool_error(ctx, "update_and_run_blocks") is None


def test_turn_intent_gate_allows_edit_with_parameter_reference() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.EDIT,
        target_entities={"workflow": ["current_workflow"]},
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx = _ctx(intent)
    ctx.user_message = "Update the current workflow so account_number is used in the search step."
    ctx.workflow_yaml = """
title: Existing
workflow_definition:
  parameters:
    - parameter_type: workflow
      key: account_number
  blocks:
    - block_type: navigation
      label: search_account
      navigation_goal: Search for the account.
"""

    assert _turn_intent_tool_error(ctx, "update_and_run_blocks") is None


def test_turn_intent_gate_preserves_request_policy_update_skip_path() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.BUILD,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=False),
    )
    policy = RequestPolicy(
        allow_update_workflow=True,
        allow_run_blocks=False,
        allow_missing_credentials_in_draft=True,
        clarification_reason="workflow_credential_inputs_unbound",
    )

    assert _turn_intent_tool_error(_ctx(intent, policy), "update_and_run_blocks") is None


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


@pytest.mark.asyncio
async def test_request_policy_refusal_wins_even_when_turn_intent_allows_update() -> None:
    intent = TurnIntent(
        mode=TurnIntentMode.BUILD,
        authority=TurnIntentAuthority(may_update_workflow=True, may_run_blocks=True),
    )
    ctx = _ctx(intent, RequestPolicy(allow_update_workflow=False, allow_run_blocks=False))

    with patch("skyvern.forge.sdk.copilot.tools.app") as mock_app:
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": ctx.workflow_yaml}, ctx)

    assert result["ok"] is False
    assert result["error"].startswith("Request policy blocks workflow updates")
    mock_app.WORKFLOW_SERVICE.update_workflow_definition.assert_not_called()


def test_update_and_run_blocks_reports_both_blocked_authorities() -> None:
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
