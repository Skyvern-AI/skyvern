"""Regression test for the self-hosted code-block authentication regression.

Symptom (v1.0.34 → v1.0.36): agent blocks (login/navigation/extraction)
authenticate correctly, but the next Code Block's ``page.goto(<authed url>)``
landed on the sign-in page because the code block acquired a *different*
BrowserContext than the one the agent authenticated into.

Root cause: ``Block.get_or_create_browser_state`` consulted
``PERSISTENT_SESSIONS_MANAGER`` first whenever a ``browser_session_id`` was set
on the run context, instead of reusing the live browser the agent had already
bound to the workflow run (registered under ``workflow_run_id`` by
``RealBrowserManager.get_or_create_for_task`` / ``get_or_create_for_workflow_run``).

Fix: prefer the live workflow-run browser; fall back to a persistent session
only when no live browser exists for the run.

These tests pin that ordering so the regression cannot silently return.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


def _output_parameter(key: str = "code_output") -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test output",
        output_parameter_id="op_code_block_authed_test",
        workflow_id="w_code_block_authed_test",
        created_at=now,
        modified_at=now,
    )


def _code_block() -> CodeBlock:
    return CodeBlock(
        label="my_code_block",
        code="await page.goto('https://app.example.com/dashboard')",
        output_parameter=_output_parameter(),
    )


WORKFLOW_RUN_ID = "wr_authed_test"
ORG_ID = "o_authed_test"
SESSION_ID = "pbs_authed_test"


@pytest.mark.asyncio
async def test_code_block_reuses_live_workflow_run_browser_over_persistent_session() -> None:
    """When the agent's authenticated browser is bound to the run AND a
    browser_session_id is present, the code block must reuse the live browser,
    not the (possibly stale/unauthenticated) persistent-session handle."""
    agent_authed_browser = MagicMock(name="agent_authed_browser_state")
    other_persistent_browser = MagicMock(name="persistent_session_browser_state")

    browser_manager = MagicMock()
    browser_manager.get_for_workflow_run = MagicMock(return_value=agent_authed_browser)

    persistent_manager = MagicMock()
    persistent_manager.get_browser_state = AsyncMock(return_value=other_persistent_browser)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.BROWSER_MANAGER = browser_manager
        mock_app.PERSISTENT_SESSIONS_MANAGER = persistent_manager

        result = await _code_block().get_or_create_browser_state(
            workflow_run_id=WORKFLOW_RUN_ID,
            organization_id=ORG_ID,
            browser_session_id=SESSION_ID,
        )

    assert result is agent_authed_browser, "code block must reuse the agent's live authenticated browser"
    browser_manager.get_for_workflow_run.assert_called_once_with(WORKFLOW_RUN_ID)
    # The persistent-session lookup must NOT be consulted when a live browser exists.
    persistent_manager.get_browser_state.assert_not_called()


@pytest.mark.asyncio
async def test_code_block_falls_back_to_persistent_session_when_no_live_browser() -> None:
    """Code-only / debugger runs have no agent-bound browser; the persistent
    session is the correct fallback and must still be honored."""
    persistent_browser = MagicMock(name="persistent_session_browser_state")

    browser_manager = MagicMock()
    browser_manager.get_for_workflow_run = MagicMock(return_value=None)

    persistent_manager = MagicMock()
    persistent_manager.get_browser_state = AsyncMock(return_value=persistent_browser)

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.BROWSER_MANAGER = browser_manager
        mock_app.PERSISTENT_SESSIONS_MANAGER = persistent_manager

        result = await _code_block().get_or_create_browser_state(
            workflow_run_id=WORKFLOW_RUN_ID,
            organization_id=ORG_ID,
            browser_session_id=SESSION_ID,
        )

    assert result is persistent_browser
    persistent_manager.get_browser_state.assert_awaited_once_with(SESSION_ID, ORG_ID)


@pytest.mark.asyncio
async def test_code_block_uses_live_browser_when_no_session_id() -> None:
    """The common self-hosted case (persistbrowsersession off, no session id):
    the code block reuses the live workflow-run browser."""
    agent_authed_browser = MagicMock(name="agent_authed_browser_state")

    browser_manager = MagicMock()
    browser_manager.get_for_workflow_run = MagicMock(return_value=agent_authed_browser)

    persistent_manager = MagicMock()
    persistent_manager.get_browser_state = AsyncMock()

    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        mock_app.BROWSER_MANAGER = browser_manager
        mock_app.PERSISTENT_SESSIONS_MANAGER = persistent_manager

        result = await _code_block().get_or_create_browser_state(
            workflow_run_id=WORKFLOW_RUN_ID,
            organization_id=ORG_ID,
            browser_session_id=None,
        )

    assert result is agent_authed_browser
    persistent_manager.get_browser_state.assert_not_called()
