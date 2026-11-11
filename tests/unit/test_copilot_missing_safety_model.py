"""Regression coverage for self-hosted Copilot without a configured safety model."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot.agent import run_copilot_agent
from skyvern.forge.sdk.copilot.request_policy import (
    SAFETY_MODEL_NOT_CONFIGURED_QUESTION,
    build_request_policy_trust_floor,
)
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest
from tests.unit.copilot_test_helpers import stub_copilot_agent_loop

_SCREEN_UNAVAILABLE_TURN = "[INPUT_UNAVAILABLE_SAFETY_SCREEN_INCOMPLETE]"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Hello", "Open https://google.com and tell me the title"])
async def test_missing_safety_model_explains_setup_and_withholds_message(message: str) -> None:
    policy = await build_request_policy_trust_floor(
        user_message=message,
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=None,
    )

    assert policy.user_response_policy == "ask_clarification"
    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "missing_handler"
    assert policy.clarification_reason == "safety_screen_unavailable"
    assert policy.clarification_question == SAFETY_MODEL_NOT_CONFIGURED_QUESTION
    assert "WORKFLOW_COPILOT_LITE_LLM_KEY" in policy.clarification_question
    assert "send it again" not in policy.clarification_question
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN
    assert policy.raw_secret_detected is False
    assert policy.allow_update_workflow is False
    assert policy.allow_run_blocks is False


@pytest.mark.asyncio
async def test_missing_safety_model_returns_configuration_error_before_acting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acting_loop = AsyncMock()
    stub_copilot_agent_loop(monkeypatch, acting_loop)
    browser_setup = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.agent._resolve_live_browser_session_id",
        browser_setup,
    )
    persist_message = AsyncMock()

    result = await run_copilot_agent(
        stream=MagicMock(send=AsyncMock()),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="Open https://google.com and tell me the title",
            workflow_yaml="",
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=None,
        api_key="sk-test",
        persist_canonical_user_message=persist_message,
    )

    assert result.user_response == SAFETY_MODEL_NOT_CONFIGURED_QUESTION
    assert result.updated_workflow is None
    persist_message.assert_awaited_once_with(_SCREEN_UNAVAILABLE_TURN)
    acting_loop.assert_not_awaited()
    browser_setup.assert_not_awaited()
