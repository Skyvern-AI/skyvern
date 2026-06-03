"""Regression test for the v1.0.34 code-block browser-context split (Goal 1).

In script-execution mode the agent blocks act on a single live ``ScriptSkyvernPage``
held in the script run context. A code/print block that re-derives a page via the
browser manager could land on a DIFFERENT, unauthenticated BrowserContext, so
``page.goto(<authed url>)`` bounced to the sign-in page even though every agent block
was authenticated.

``Block.resolve_live_page`` must therefore return the live script run-context page
(the exact authenticated page the agent used) when a script run is active, and only
fall back to the workflow-run browser when there is none (live/agent mode).
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.block import CodeBlock, OutputParameter
from skyvern.forge.sdk.workflow.models.parameter import ParameterType


def _code_block() -> CodeBlock:
    output_parameter = OutputParameter(
        output_parameter_id="op_1",
        key="code_output",
        workflow_id="w_1",
        parameter_type=ParameterType.OUTPUT,
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
    )
    return CodeBlock(label="audit_gallery", code="result = 1", output_parameter=output_parameter)


@pytest.mark.asyncio
async def test_resolve_live_page_prefers_script_runcontext_page() -> None:
    """When a script run is active, use its live page — NOT a re-derived browser."""
    block = _code_block()

    agent_page = MagicMock(name="agent_authenticated_playwright_page")
    run_ctx = MagicMock()
    run_ctx.page = MagicMock()
    run_ctx.page.page = agent_page  # underlying Playwright page in the agent's authed context

    with (
        patch(
            "skyvern.core.script_generations.script_skyvern_page.script_run_context_manager"
        ) as mock_mgr,
        patch.object(CodeBlock, "get_or_create_browser_state", new_callable=AsyncMock) as mock_get_bs,
    ):
        mock_mgr.get_run_context.return_value = run_ctx

        page = await block.resolve_live_page(workflow_run_id="wr_1", organization_id="o_1")

        assert page is agent_page, "code block must reuse the agent's live script run-context page"
        mock_get_bs.assert_not_called(), "must not re-derive a separate browser when a script run is active"


@pytest.mark.asyncio
async def test_resolve_live_page_falls_back_when_no_script_run() -> None:
    """Live/agent mode (no script run context) falls back to the workflow-run browser."""
    block = _code_block()

    fallback_page = MagicMock(name="workflow_run_working_page")
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=fallback_page)

    with (
        patch(
            "skyvern.core.script_generations.script_skyvern_page.script_run_context_manager"
        ) as mock_mgr,
        patch.object(CodeBlock, "get_or_create_browser_state", new_callable=AsyncMock) as mock_get_bs,
    ):
        mock_mgr.get_run_context.return_value = None
        mock_get_bs.return_value = browser_state

        page = await block.resolve_live_page(workflow_run_id="wr_1", organization_id="o_1")

        assert page is fallback_page
        mock_get_bs.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_live_page_falls_back_when_runcontext_has_no_page() -> None:
    block = _code_block()

    fallback_page = MagicMock(name="workflow_run_working_page")
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=fallback_page)

    run_ctx = MagicMock()
    run_ctx.page = None

    with (
        patch(
            "skyvern.core.script_generations.script_skyvern_page.script_run_context_manager"
        ) as mock_mgr,
        patch.object(CodeBlock, "get_or_create_browser_state", new_callable=AsyncMock) as mock_get_bs,
    ):
        mock_mgr.get_run_context.return_value = run_ctx
        mock_get_bs.return_value = browser_state

        page = await block.resolve_live_page(workflow_run_id="wr_1", organization_id="o_1")

        assert page is fallback_page
        mock_get_bs.assert_awaited_once()
