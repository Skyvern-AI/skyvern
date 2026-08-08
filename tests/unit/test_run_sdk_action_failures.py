from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from skyvern.exceptions import ScrapingFailed, SkyvernActionFailed
from skyvern.forge.sdk.routes.sdk import _sdk_action_context_refcounts, run_sdk_action


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


@pytest.mark.asyncio
async def test_client_provided_context_removed_when_last_sibling_finishes(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    # A client-provided workflow_run_id is reference-counted. With no concurrent sibling holding it,
    # this lone call is the last one, so its run-context IS removed on cleanup — otherwise it would
    # persist as a permanent liveness ghost that vetoes a real run's terminal browser close.
    # The refcount must return to empty for the run.
    mock_request.workflow_run_id = "wr_shared"
    mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
        return_value=MagicMock(workflow_run_id="wr_shared", workflow_id="w_test")
    )
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop inside try"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(RuntimeError, match="stop inside try"):
            await run_sdk_action(mock_request, organization=mock_organization)

    mock_app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context.assert_called_once_with("wr_shared")
    assert "wr_shared" not in _sdk_action_context_refcounts


@pytest.mark.asyncio
async def test_client_provided_context_retained_while_sibling_in_flight(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    # A concurrent sibling run_action already holds the shared context (refcount pre-seeded). This
    # call must NOT remove it on cleanup — tearing it down would yank the context out from under the
    # in-flight sibling. The refcount drops back to the sibling's hold, not to zero.
    mock_request.workflow_run_id = "wr_shared"
    mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
        return_value=MagicMock(workflow_run_id="wr_shared", workflow_id="w_test")
    )
    _sdk_action_context_refcounts["wr_shared"] = 1  # a sibling call is in flight
    try:
        with (
            patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
            patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
            patch(
                "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
                new_callable=AsyncMock,
                side_effect=RuntimeError("stop inside try"),
            ),
        ):
            mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
            with pytest.raises(RuntimeError, match="stop inside try"):
                await run_sdk_action(mock_request, organization=mock_organization)

        mock_app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context.assert_not_called()
        assert _sdk_action_context_refcounts["wr_shared"] == 1
    finally:
        _sdk_action_context_refcounts.pop("wr_shared", None)


@pytest.mark.asyncio
async def test_owned_workflow_run_context_is_removed_on_cleanup(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    # A workflow_run we created ourselves is unique to this request, so removing it in cleanup
    # both prevents the SKY-12524 leak and is safe (no sibling call can share the id).
    mock_request.workflow_run_id = None
    workflow = MagicMock(workflow_id="w_test", workflow_permanent_id="wpid_test", title="t")
    workflow_run = MagicMock(workflow_run_id="wr_owned", workflow_id="w_test")
    mock_app.WORKFLOW_SERVICE.create_empty_workflow = AsyncMock(return_value=workflow)
    mock_app.WORKFLOW_SERVICE.setup_workflow_run = AsyncMock(return_value=workflow_run)
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_completed = AsyncMock(return_value=workflow_run)
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop inside try"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(RuntimeError, match="stop inside try"):
            await run_sdk_action(mock_request, organization=mock_organization)

    mock_app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context.assert_called_once_with("wr_owned")


@pytest.mark.asyncio
async def test_minted_workflow_run_is_marked_synthetic_on_context(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    # SKY-13518: the minted run never begins the browser session, so the context must mark it
    # synthetic — downstream browser acquisition must not present it as the expected session owner.
    mock_request.workflow_run_id = None
    workflow = MagicMock(workflow_id="w_test", workflow_permanent_id="wpid_test", title="t")
    workflow_run = MagicMock(workflow_run_id="wr_owned", workflow_id="w_test")
    mock_app.WORKFLOW_SERVICE.create_empty_workflow = AsyncMock(return_value=workflow)
    mock_app.WORKFLOW_SERVICE.setup_workflow_run = AsyncMock(return_value=workflow_run)
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_completed = AsyncMock(return_value=workflow_run)
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop inside try"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(RuntimeError, match="stop inside try"):
            await run_sdk_action(mock_request, organization=mock_organization)

    replaced_context = mock_ctx.replace.call_args.args[0]
    assert replaced_context.workflow_run_is_synthetic is True


@pytest.mark.asyncio
async def test_client_provided_workflow_run_is_not_marked_synthetic(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    # A client-provided run may genuinely own the session (it can have begun it), so ownership
    # must still be asserted for it.
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop inside try"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(RuntimeError, match="stop inside try"):
            await run_sdk_action(mock_request, organization=mock_organization)

    replaced_context = mock_ctx.replace.call_args.args[0]
    assert replaced_context.workflow_run_is_synthetic is False


@pytest.mark.asyncio
async def test_context_refcount_released_when_initialization_fails(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    # F1: the refcount acquire sits before initialize_workflow_run_context. If init RAISES, the
    # release must still run (outer finally), or the client-provided run id leaks its refcount and
    # becomes a permanent liveness ghost that vetoes a real run's terminal browser close.
    mock_request.workflow_run_id = "wr_test"
    mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
        return_value=MagicMock(workflow_run_id="wr_test", workflow_id="w_test")
    )
    mock_app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context = AsyncMock(side_effect=RuntimeError("init boom"))
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context"),
    ):
        with pytest.raises(RuntimeError, match="init boom"):
            await run_sdk_action(mock_request, organization=mock_organization)

    assert "wr_test" not in _sdk_action_context_refcounts
    mock_app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context.assert_called_once_with("wr_test")


@pytest.mark.asyncio
async def test_context_refcount_released_when_initialization_is_cancelled(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    # F1 (cancellation): a CancelledError during init must not leak the refcount either — the outer
    # finally releases it before the cancellation propagates.
    mock_request.workflow_run_id = "wr_test"
    mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
        return_value=MagicMock(workflow_run_id="wr_test", workflow_id="w_test")
    )
    mock_app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context = AsyncMock(side_effect=asyncio.CancelledError())
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context"),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_sdk_action(mock_request, organization=mock_organization)

    assert "wr_test" not in _sdk_action_context_refcounts
    mock_app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context.assert_called_once_with("wr_test")


@pytest.mark.asyncio
async def test_context_refcount_released_when_upload_drain_is_cancelled(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    # F1 (drain cancellation): the release used to sit AFTER the upload-drain await inside the same
    # finally, so a CancelledError during the drain skipped it — recreating the ghost. With the
    # release in the OUTER finally it still runs when the drain await is cancelled.
    mock_request.workflow_run_id = "wr_test"
    mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(
        return_value=MagicMock(workflow_run_id="wr_test", workflow_id="w_test")
    )
    mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks = AsyncMock(side_effect=asyncio.CancelledError())
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop inside try"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        # The drain's CancelledError (raised in the inner finally) supersedes the body error.
        with pytest.raises(asyncio.CancelledError):
            await run_sdk_action(mock_request, organization=mock_organization)

    assert "wr_test" not in _sdk_action_context_refcounts
    mock_app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context.assert_called_once_with("wr_test")
