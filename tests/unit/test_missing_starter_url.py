"""Tests for when the first browser block has no starter URL

Covers:
- MissingStarterUrl exception message formatting.
- The fail-early branch in TaskBlock.execute() for a first task block with no URL.
- The negative case: URL=None but the page already navigated (e.g. browser profile
  loaded a homepage) must NOT raise.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import InvalidWorkflowTaskURLState, MissingStarterUrl
from skyvern.forge.agent import ForgeAgent, resolve_inherited_workflow_task_page
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.webeye.real_browser_state import RealBrowserState


def _output_parameter(key: str = "task_output") -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test output",
        output_parameter_id="op_missing_starter_url_test",
        workflow_id="w_missing_starter_url_test",
        created_at=now,
        modified_at=now,
    )


def _workflow_run_context() -> WorkflowRunContext:
    return WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_missing_starter_url_test",
        workflow_permanent_id="wpid_missing_starter_url_test",
        workflow_run_id="wr_missing_starter_url_test",
        aws_client=MagicMock(),
    )


@contextmanager
def _mock_block_execute_deps(working_page_url: str) -> Iterator[dict[str, Any]]:
    """Patch the app-level singletons used by TaskBlock.execute() for a first-task
    scenario and hand the test back the mocks it needs to assert on."""

    workflow_run = SimpleNamespace(
        workflow_run_id="wr_missing_starter_url_test",
        workflow_permanent_id="wpid_missing_starter_url_test",
        organization_id="o_test",
        browser_profile_id=None,
        browser_address=None,
    )

    working_page = SimpleNamespace(url=working_page_url)
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=working_page)
    browser_state.take_fullpage_screenshot = AsyncMock(return_value=None)
    browser_state.navigate_to_url = AsyncMock()

    browser_manager = MagicMock()
    browser_manager.get_or_create_for_workflow_run = AsyncMock(return_value=browser_state)

    workflow_service = MagicMock()
    workflow_service.get_workflow_run = AsyncMock(return_value=workflow_run)
    workflow_service.get_workflow_by_permanent_id = AsyncMock(
        return_value=MagicMock(workflow_id="w_missing_starter_url_test")
    )

    organization = SimpleNamespace(organization_id="o_test")
    organizations_db = MagicMock()
    organizations_db.get_organization = AsyncMock(return_value=organization)

    tasks_db = MagicMock()
    tasks_db.update_task = AsyncMock()
    tasks_db.get_last_task_for_workflow_run = AsyncMock(return_value=None)

    observer_db = MagicMock()
    observer_db.update_workflow_run_block = AsyncMock(return_value=MagicMock())

    database = MagicMock()
    database.tasks = tasks_db
    database.organizations = organizations_db
    database.observer = observer_db

    task = SimpleNamespace(task_id="tsk_test", status=TaskStatus.failed)
    step = SimpleNamespace(step_id="stp_test")

    agent = MagicMock()
    agent.create_task_and_step_from_block = AsyncMock(return_value=(task, step))
    agent.execute_step = AsyncMock()

    with (
        patch("skyvern.forge.sdk.workflow.models.task_blocks.app") as mock_app,
        patch(
            "skyvern.forge.sdk.workflow.models.block.Block.get_workflow_run_context",
            return_value=_workflow_run_context(),
        ),
        patch(
            "skyvern.forge.sdk.workflow.models.task_blocks.capture_block_download_baseline",
            new=AsyncMock(),
        ),
    ):
        mock_app.BROWSER_MANAGER = browser_manager
        mock_app.WORKFLOW_SERVICE = workflow_service
        mock_app.DATABASE = database
        mock_app.agent = agent

        yield {
            "task": task,
            "tasks_db": tasks_db,
            "agent": agent,
            "browser_state": browser_state,
        }


def test_missing_starter_url_message_uses_block_label() -> None:
    exc = MissingStarterUrl(block_label="open_vendor_url")
    assert "open_vendor_url" in str(exc)
    assert "starting URL" in str(exc)
    assert "workflow parameter" in str(exc)


def test_missing_starter_url_message_without_label() -> None:
    exc = MissingStarterUrl()
    assert "first browser block" in str(exc)
    assert "starting URL" in str(exc)


@pytest.mark.asyncio
@pytest.mark.parametrize("blank_url", ["about:blank", "", ":"])
async def test_execute_fails_early_when_first_block_has_no_url(blank_url: str) -> None:
    """A first browser block with no URL landing on any blank-page marker
    (``about:blank``, empty string, or the rare ``":"`` Playwright reports for
    brand-new pages) should raise MissingStarterUrl before scraping starts,
    instead of the confusing downstream ScrapingFailedBlankPage."""

    block = TaskBlock(
        label="open_vendor_url",
        output_parameter=_output_parameter(),
        title="Open vendor URL",
        url=None,
    )

    with _mock_block_execute_deps(working_page_url=blank_url) as deps:
        with pytest.raises(MissingStarterUrl) as excinfo:
            await block.execute(
                workflow_run_id="wr_missing_starter_url_test",
                workflow_run_block_id="wrb_test",
                organization_id="o_test",
            )

        assert "open_vendor_url" in str(excinfo.value)
        deps["tasks_db"].update_task.assert_any_call(
            deps["task"].task_id,
            status=TaskStatus.failed,
            organization_id="o_test",
            failure_reason=str(excinfo.value),
        )


def _workflow_run_for_create_task() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_missing_starter_url_test",
        parent_workflow_run_id=None,
        organization_id="o_test",
        proxy_location=None,
        max_screenshot_scrolls=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
        browser_session_id=None,
    )


def _task_block_without_url() -> TaskBlock:
    return TaskBlock(
        label="validate_download",
        output_parameter=_output_parameter(),
        title="Validate downloaded file",
        url=None,
    )


@pytest.mark.asyncio
async def test_create_task_rejects_about_blank_url_inherited_from_later_block() -> None:
    """Keep the historical behavior for about:blank inherited task URLs."""

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=SimpleNamespace(url="about:blank"))
    browser_state.list_valid_pages = AsyncMock()

    browser_manager = MagicMock()
    browser_manager.get_for_workflow_run.return_value = browser_state

    workflow = SimpleNamespace(workflow_id="w_missing_starter_url_test")
    workflow_run = _workflow_run_for_create_task()
    workflow_run_context = SimpleNamespace(get_value=MagicMock())

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER = browser_manager

        with pytest.raises(InvalidWorkflowTaskURLState):
            await ForgeAgent().create_task_and_step_from_block(
                task_block=_task_block_without_url(),
                workflow=workflow,
                workflow_run=workflow_run,
                workflow_run_context=workflow_run_context,
                task_order=5,
                task_retry=0,
            )

    browser_state.get_working_page.assert_awaited_once()
    browser_state.list_valid_pages.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_task_uses_latest_non_blank_page_for_inherited_marker_url() -> None:
    """Later blocks with url=None inherit the current browser page URL. The
    ':' marker URL can be left behind after downloads; use the latest non-blank
    page so scrape/reload runs against the real page."""

    real_page = SimpleNamespace(url="https://example.test/invoices")
    blank_page = SimpleNamespace(url=":")

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=blank_page)
    browser_state.list_valid_pages = AsyncMock(return_value=[real_page, blank_page])
    browser_state.set_active_page = AsyncMock()

    browser_manager = MagicMock()
    browser_manager.get_for_workflow_run.return_value = browser_state

    created_task = SimpleNamespace(
        task_id="tsk_missing_starter_url_test",
        organization_id="o_test",
        url=real_page.url,
        title="Validate downloaded file",
        proxy_location=None,
    )
    step = SimpleNamespace(order=0, retry_index=0)
    tasks_db = MagicMock()
    tasks_db.create_task = AsyncMock(return_value=created_task)
    tasks_db.update_task = AsyncMock(return_value=created_task)
    tasks_db.create_step = AsyncMock(return_value=step)

    database = MagicMock()
    database.tasks = tasks_db

    workflow = SimpleNamespace(workflow_id="w_missing_starter_url_test")
    workflow_run = _workflow_run_for_create_task()
    workflow_run_context = SimpleNamespace(get_value=MagicMock())

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER = browser_manager
        mock_app.DATABASE = database

        task, _ = await ForgeAgent().create_task_and_step_from_block(
            task_block=_task_block_without_url(),
            workflow=workflow,
            workflow_run=workflow_run,
            workflow_run_context=workflow_run_context,
            task_order=5,
            task_retry=0,
        )

    assert task is created_task
    assert tasks_db.create_task.await_args.kwargs["url"] == real_page.url
    browser_state.get_working_page.assert_awaited_once()
    browser_state.list_valid_pages.assert_awaited_once()
    browser_state.set_active_page.assert_awaited_once_with(real_page)


@pytest.mark.asyncio
async def test_resolved_inherited_marker_page_stays_active() -> None:
    real_page = MagicMock()
    real_page.url = "https://example.test/invoices"
    real_page.is_closed = MagicMock(return_value=False)
    blank_page = MagicMock()
    blank_page.url = ":"
    blank_page.is_closed = MagicMock(return_value=False)

    browser_state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock())
    browser_state.list_valid_pages = AsyncMock(return_value=[real_page, blank_page])
    await browser_state.set_working_page(blank_page)

    assert await resolve_inherited_workflow_task_page(browser_state, "wr_missing_starter_url_test") is real_page
    assert await browser_state.get_working_page() is real_page


@pytest.mark.asyncio
async def test_resolved_inherited_marker_page_raises_when_no_non_blank_page_exists() -> None:
    about_blank_page = MagicMock()
    about_blank_page.url = "about:blank"
    about_blank_page.is_closed = MagicMock(return_value=False)
    blank_marker_page = MagicMock()
    blank_marker_page.url = ":"
    blank_marker_page.is_closed = MagicMock(return_value=False)

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=blank_marker_page)
    browser_state.list_valid_pages = AsyncMock(return_value=[about_blank_page, blank_marker_page])
    browser_state.set_active_page = AsyncMock()

    with pytest.raises(InvalidWorkflowTaskURLState):
        await resolve_inherited_workflow_task_page(browser_state, "wr_missing_starter_url_test")

    browser_state.set_active_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolved_inherited_marker_page_preserves_selected_tab() -> None:
    selected_page = MagicMock()
    selected_page.url = "https://example.test/selected"
    selected_page.is_closed = MagicMock(return_value=False)
    other_page = MagicMock()
    other_page.url = "https://example.test/other"
    other_page.is_closed = MagicMock(return_value=False)
    blank_page = MagicMock()
    blank_page.url = ":"
    blank_page.is_closed = MagicMock(return_value=False)

    browser_state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock())
    browser_state.list_valid_pages = AsyncMock(return_value=[selected_page, other_page])
    await browser_state.set_active_page(selected_page)
    browser_state.list_valid_pages = AsyncMock(return_value=[selected_page, other_page, blank_page])

    assert await resolve_inherited_workflow_task_page(browser_state, "wr_missing_starter_url_test") is selected_page
    assert await browser_state.get_working_page() is selected_page


@pytest.mark.asyncio
async def test_execute_does_not_raise_when_profile_loaded_a_page() -> None:
    """If the browser session/profile navigated the page away from about:blank before
    the first task starts, the block has a meaningful page to scrape — the missing-URL
    check must NOT fire."""

    block = TaskBlock(
        label="use_existing_session",
        output_parameter=_output_parameter(),
        title="Use existing session",
        url=None,
    )

    with _mock_block_execute_deps(working_page_url="https://example.com/dashboard") as deps:
        # execute_step is mocked to no-op; we rely on the task-status lookup below to
        # short-circuit the rest of the block runner.
        deps["tasks_db"].get_task = AsyncMock(
            return_value=SimpleNamespace(
                task_id=deps["task"].task_id,
                status=TaskStatus.completed,
                failure_reason=None,
            )
        )
        # The extra downstream services the block touches for a completed task — stub
        # enough of them to let execute() return without crashing.
        with (
            patch(
                "skyvern.forge.sdk.workflow.models.task_blocks.app.STORAGE",
                new=MagicMock(get_downloaded_files=AsyncMock(return_value=[])),
                create=True,
            ),
            patch(
                "skyvern.forge.sdk.workflow.models.task_blocks.app.ARTIFACT_MANAGER",
                new=MagicMock(create_workflow_run_block_artifact=AsyncMock()),
                create=True,
            ),
        ):
            try:
                await block.execute(
                    workflow_run_id="wr_missing_starter_url_test",
                    workflow_run_block_id="wrb_test",
                    organization_id="o_test",
                )
            except MissingStarterUrl:
                pytest.fail("MissingStarterUrl should not be raised when the page has already navigated")
            except Exception:
                # Other downstream failures (e.g. artifact lookup) are fine — we only
                # care that MissingStarterUrl is NOT raised in this configuration.
                pass
