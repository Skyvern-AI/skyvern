from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from skyvern.exceptions import ScrapingFailed, SkyvernActionFailed
from skyvern.forge.sdk.routes.sdk import run_sdk_action


@pytest.fixture
def mock_request() -> Any:
    request = MagicMock()
    request.workflow_run_id = "wr_test"
    request.browser_session_id = None
    request.browser_address = None
    request.url = "https://example.com"
    request.action = MagicMock()
    request.action.type = "ai_click"
    request.action.selector = None
    request.action.intention = "Click the button"
    request.action.data = None
    request.action.timeout = 30000
    request.action.get_navigation_goal = MagicMock(return_value="Click the button")
    request.action.get_navigation_payload = MagicMock(return_value=None)
    return request


@pytest.fixture
def mock_organization() -> Any:
    org = MagicMock()
    org.organization_id = "o_test"
    return org


@pytest.fixture
def mock_app() -> Any:
    app = MagicMock()
    workflow_run = MagicMock(workflow_run_id="wr_test", workflow_id="w_test")
    workflow = MagicMock(workflow_id="w_test", workflow_permanent_id="wpid_test", title="t")
    task = MagicMock(task_id="tsk_test", organization_id="o_test", max_screenshot_scrolls=None)
    step = MagicMock(step_id="stp_test")
    app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=workflow_run)
    app.DATABASE.workflows.get_workflow = AsyncMock(return_value=workflow)
    app.DATABASE.tasks.create_task = AsyncMock(return_value=task)
    app.DATABASE.tasks.create_step = AsyncMock(return_value=step)
    app.DATABASE.tasks.update_task = AsyncMock()
    app.DATABASE.observer.create_workflow_run_block = AsyncMock()
    app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context = AsyncMock()
    return app


@pytest.mark.asyncio
async def test_auto_generated_run_is_completed_through_workflow_service(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    mock_request.workflow_run_id = None
    workflow = MagicMock(workflow_id="w_test", workflow_permanent_id="wpid_test", title="t")
    workflow_run = MagicMock(workflow_run_id="wr_test", workflow_id="w_test")
    mock_app.WORKFLOW_SERVICE.create_empty_workflow = AsyncMock(return_value=workflow)
    mock_app.WORKFLOW_SERVICE.setup_workflow_run = AsyncMock(return_value=workflow_run)
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_completed = AsyncMock(return_value=workflow_run)
    mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock(return_value=workflow_run)
    mock_app.DATABASE.tasks.create_task = AsyncMock(side_effect=RuntimeError("stop after completion"))

    with patch("skyvern.forge.sdk.routes.sdk.app", mock_app):
        with pytest.raises(RuntimeError, match="stop after completion"):
            await run_sdk_action(mock_request, organization=mock_organization)

    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_completed.assert_awaited_once_with(workflow_run_id="wr_test")
    mock_app.DATABASE.workflow_runs.update_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_returns_422_when_action_raises_skyvern_action_failed(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=SkyvernActionFailed("AI click failed and no fallback selector available"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(HTTPException) as exc_info:
            await run_sdk_action(mock_request, organization=mock_organization)

    assert exc_info.value.status_code == 422
    assert "AI click failed" in str(exc_info.value.detail)
    mock_app.DATABASE.tasks.update_task.assert_awaited()


@pytest.mark.asyncio
async def test_handler_returns_400_when_action_raises_scraping_failed(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=ScrapingFailed(reason="page is blank"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(HTTPException) as exc_info:
            await run_sdk_action(mock_request, organization=mock_organization)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_handler_propagates_unknown_exception(mock_request: Any, mock_organization: Any, mock_app: Any) -> None:
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db connection pool exhausted"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(RuntimeError):
            await run_sdk_action(mock_request, organization=mock_organization)

    mock_app.DATABASE.tasks.update_task.assert_awaited()
