from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from skyvern.exceptions import (
    MissingBrowserStatePage,
    ScrapingFailed,
    ScreenshotTargetClosed,
    SkyvernActionFailed,
)
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.routes.sdk import _sdk_action_context_refcounts, run_sdk_action
from tests.unit.conftest import LEGACY_DOWNLOAD_ESCAPE_CASES


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
async def test_auto_generated_run_commits_marker_before_completion(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    mock_request.workflow_run_id = None
    workflow = MagicMock(workflow_id="w_test", workflow_permanent_id="wpid_test", title="t")
    workflow_run = MagicMock(workflow_run_id="wr_test", workflow_id="w_test")
    task = mock_app.DATABASE.tasks.create_task.return_value
    step = mock_app.DATABASE.tasks.create_step.return_value
    events: list[str] = []

    mock_app.WORKFLOW_SERVICE.create_empty_workflow = AsyncMock(return_value=workflow)
    mock_app.WORKFLOW_SERVICE.setup_workflow_run = AsyncMock(return_value=workflow_run)

    mock_app.DATABASE.workflow_runs.update_workflow_run = AsyncMock()

    async def create_task(**_: Any) -> Any:
        events.append("task")
        return task

    async def mark_workflow_run_as_completed(**_: Any) -> Any:
        events.append("complete")
        return workflow_run

    async def create_step(*_: Any, **__: Any) -> Any:
        events.append("step")
        return step

    async def create_workflow_run_block(**_: Any) -> None:
        events.append("block")

    mock_app.DATABASE.tasks.create_task = AsyncMock(side_effect=create_task)
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_completed = AsyncMock(side_effect=mark_workflow_run_as_completed)
    mock_app.DATABASE.tasks.create_step = AsyncMock(side_effect=create_step)
    mock_app.DATABASE.observer.create_workflow_run_block = AsyncMock(side_effect=create_workflow_run_block)

    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop after setup"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(RuntimeError, match="stop after setup"):
            await run_sdk_action(mock_request, organization=mock_organization)

    assert events[:4] == ["task", "complete", "step", "block"]
    assert mock_app.DATABASE.tasks.create_task.await_args.kwargs["task_type"] == TaskType.synthetic_sdk_action
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_completed.assert_awaited_once_with(workflow_run_id="wr_test")
    mock_app.DATABASE.workflow_runs.update_workflow_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_caller_provided_run_keeps_general_task_type(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_completed = AsyncMock()

    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop inside action"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(RuntimeError, match="stop inside action"):
            await run_sdk_action(mock_request, organization=mock_organization)

    assert mock_app.DATABASE.tasks.create_task.await_args.kwargs["task_type"] == TaskType.general
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_minted_sdk_action_uses_inline_dispatch_without_generic_executor(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    mock_request.workflow_run_id = None
    workflow = MagicMock(workflow_id="w_test", workflow_permanent_id="wpid_test", title="t")
    workflow_run = MagicMock(workflow_run_id="wr_test", workflow_id="w_test")
    mock_app.WORKFLOW_SERVICE.create_empty_workflow = AsyncMock(return_value=workflow)
    mock_app.WORKFLOW_SERVICE.setup_workflow_run = AsyncMock(return_value=workflow_run)
    mock_app.WORKFLOW_SERVICE.mark_workflow_run_as_completed = AsyncMock(return_value=workflow_run)
    mock_app.WORKFLOW_SERVICE.create_workflow_from_prompt = AsyncMock()
    scraped_page = MagicMock(_browser_state=MagicMock(must_get_working_page=AsyncMock(return_value=MagicMock())))
    page_ai = MagicMock(ai_click=AsyncMock(return_value={"clicked": True}))
    generic_executor = MagicMock()
    generic_executor.execute_task = AsyncMock()
    generic_executor.execute_workflow = AsyncMock()

    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            return_value=scraped_page,
        ),
        patch("skyvern.forge.sdk.routes.sdk.RealSkyvernPageAi", return_value=page_ai),
        patch(
            "skyvern.forge.sdk.executor.factory.AsyncExecutorFactory.get_executor",
            return_value=generic_executor,
        ) as get_executor,
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        response = await run_sdk_action(mock_request, organization=mock_organization)

    assert response.result == {"clicked": True}
    get_executor.assert_not_called()
    generic_executor.execute_task.assert_not_awaited()
    generic_executor.execute_workflow.assert_not_awaited()
    mock_app.WORKFLOW_SERVICE.create_workflow_from_prompt.assert_not_awaited()


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
async def test_handler_returns_422_when_browser_target_closed(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=ScreenshotTargetClosed(
                error_message="Page.screenshot: Target page, context or browser has been closed"
            ),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(HTTPException) as exc_info:
            await run_sdk_action(mock_request, organization=mock_organization)

    assert exc_info.value.status_code == 422
    mock_app.DATABASE.tasks.update_task.assert_awaited()


@pytest.mark.asyncio
async def test_handler_returns_422_when_browser_state_page_is_missing(
    mock_request: Any, mock_organization: Any, mock_app: Any
) -> None:
    """A closed browser context is the same caller-visible condition as a closed target, so it
    must not fall through to the generic handler and surface as a 500."""
    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            side_effect=MissingBrowserStatePage(task_id="tsk_gone"),
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        with pytest.raises(HTTPException) as exc_info:
            await run_sdk_action(mock_request, organization=mock_organization)

    assert exc_info.value.status_code == 422
    mock_app.DATABASE.tasks.update_task.assert_awaited()


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
    assert replaced_context.is_sdk_inline_action is True


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
    # Even though the reused run is not synthetic, it is still an inline SDK action the caller drives,
    # so the context must mark it so its allocation classifies client_owned (never an early-reap input).
    assert replaced_context.is_sdk_inline_action is True


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


async def _run_upload_action(
    mock_request: Any, mock_organization: Any, mock_app: Any, file_url: str
) -> tuple[Any, HTTPException | None]:
    """Drive the ai_upload_file route branch and report the upload sink plus any rejection."""
    page_ai = MagicMock(ai_upload_file=AsyncMock(return_value=file_url))
    scraped_page = MagicMock(_browser_state=MagicMock(must_get_working_page=AsyncMock()))
    mock_request.action.type = "ai_upload_file"
    mock_request.action.file_url = file_url

    with (
        patch("skyvern.forge.sdk.routes.sdk.app", mock_app),
        patch("skyvern.forge.sdk.routes.sdk.skyvern_context") as mock_ctx,
        patch("skyvern.forge.sdk.routes.sdk.RealSkyvernPageAi", return_value=page_ai),
        patch(
            "skyvern.core.script_generations.script_skyvern_page.ScriptSkyvernPage.create_scraped_page",
            new_callable=AsyncMock,
            return_value=scraped_page,
        ),
    ):
        mock_ctx.ensure_context.return_value = MagicMock(request_id="req_test", tz_info=None, prompt=None)
        try:
            await run_sdk_action(mock_request, organization=mock_organization)
        except HTTPException as exc:
            return page_ai, exc
    return page_ai, None


@pytest.mark.asyncio
async def test_ai_upload_file_route_accepts_canonical_legacy_file_url(
    mock_request: Any, mock_organization: Any, mock_app: Any, legacy_download_uris: dict[str, str]
) -> None:
    """Positive control: the guard must still let a real in-root file through to the upload."""
    page_ai, rejection = await _run_upload_action(
        mock_request, mock_organization, mock_app, legacy_download_uris["canonical"]
    )

    assert rejection is None
    page_ai.ai_upload_file.assert_awaited_once()
    assert page_ai.ai_upload_file.await_args.kwargs["files"] == legacy_download_uris["canonical"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", LEGACY_DOWNLOAD_ESCAPE_CASES)
async def test_ai_upload_file_route_rejects_legacy_file_url_escape(
    mock_request: Any, mock_organization: Any, mock_app: Any, legacy_download_uris: dict[str, str], case: str
) -> None:
    page_ai, rejection = await _run_upload_action(mock_request, mock_organization, mock_app, legacy_download_uris[case])

    assert rejection is not None
    assert rejection.status_code == 400
    page_ai.ai_upload_file.assert_not_awaited()
