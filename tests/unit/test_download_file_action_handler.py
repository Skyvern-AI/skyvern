import os
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.errors.errors import UserDefinedError
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import ClickAction, DownloadFileAction
from skyvern.webeye.actions.handler import ActionHandler, _remove_download_listener, handle_download_file_action
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import make_organization, make_step, make_task


def _download_wait_span_attrs(span_exporter: InMemorySpanExporter) -> dict:
    span = next(
        (span for span in span_exporter.get_finished_spans() if span.name == "skyvern.agent.action.download_wait"),
        None,
    )
    assert span is not None, "expected download_wait span to be recorded"
    return dict(span.attributes or {})


def _make_download_click_context(
    *,
    now: datetime,
    organization,
    page_url: str,
    task_overrides: dict | None = None,
) -> tuple:
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
        download_timeout=30.0,
        **(task_overrides or {}),
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)
    page = MagicMock()
    page.url = page_url
    page.context.browser = None
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )
    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )
    return task, step, page, browser_state, scraped_page, action


def test_remove_download_listener_uses_playwright_remove_listener_when_off_unavailable() -> None:
    page = MagicMock(spec=["remove_listener"])
    callback = MagicMock()

    _remove_download_listener(page, callback)

    page.remove_listener.assert_called_once_with("download", callback)


def test_remove_download_listener_logs_when_page_lacks_cleanup_api() -> None:
    page = MagicMock(spec=[])
    callback = MagicMock()

    with patch("skyvern.webeye.actions.handler.LOG.warning") as warning:
        _remove_download_listener(page, callback)

    warning.assert_called_once_with("Page does not support removing download listeners")


@pytest.mark.asyncio
async def test_handle_download_file_action_with_byte_data() -> None:
    """Test that when byte data is provided, the file should be saved directly"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    # Create test byte data
    test_bytes = b"test file content"
    action = DownloadFileAction(
        file_name="test_file.txt",
        byte=test_bytes,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    # Mock initialize_download_dir to return a temporary directory
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=temp_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Verify result (download_triggered is set by outer handle action flow when in context)
            assert len(result) == 1
            assert isinstance(result[0], ActionSuccess)

            # Verify file was created
            expected_file_path = os.path.join(temp_dir, "test_file.txt")
            assert os.path.exists(expected_file_path)

            # Verify file content
            with open(expected_file_path, "rb") as f:
                assert f.read() == test_bytes


@pytest.mark.asyncio
async def test_handle_download_file_action_with_download_url() -> None:
    """Test that when download_url is provided, page.goto is called and returns ActionSuccess"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="downloaded_file.pdf",
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

        # Verify page.goto was called with the correct URL (handler uses browser navigation for download_url)
        page.goto.assert_called_once()
        assert page.goto.call_args[0][0] == "https://example.com/file.pdf"

        # Verify result
        assert len(result) == 1
        assert isinstance(result[0], ActionSuccess)


@pytest.mark.asyncio
async def test_handle_download_file_action_with_download_url_same_filename() -> None:
    """Test that when download_url is provided, page.goto is called with the URL and returns ActionSuccess"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="same_name.pdf",
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

        page.goto.assert_called_once()
        assert page.goto.call_args[0][0] == "https://example.com/file.pdf"

        assert len(result) == 1
        assert isinstance(result[0], ActionSuccess)


@pytest.mark.asyncio
async def test_handle_download_file_action_without_byte_or_url() -> None:
    """Test that when neither byte data nor download_url is provided, should return ActionSuccess (no download triggered)."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="test_file.txt",
        byte=None,
        download_url=None,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=temp_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Verify result (download_triggered is set by outer handle action flow when in context)
            assert len(result) == 1
            assert isinstance(result[0], ActionSuccess)


@pytest.mark.asyncio
async def test_handle_download_file_action_with_byte_priority() -> None:
    """Test that when both byte and download_url are provided, byte data should take priority"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    # Create test byte data
    test_bytes = b"byte data content"
    action = DownloadFileAction(
        file_name="test_file.txt",
        byte=test_bytes,
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    page.goto = AsyncMock(return_value=None)

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=temp_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Byte data takes priority: page.goto should not be called
            page.goto.assert_not_called()

            assert len(result) == 1
            assert isinstance(result[0], ActionSuccess)

            expected_file_path = os.path.join(temp_dir, "test_file.txt")
            assert os.path.exists(expected_file_path)
            with open(expected_file_path, "rb") as f:
                assert f.read() == test_bytes


@pytest.mark.asyncio
async def test_handle_download_file_action_with_file_name_empty() -> None:
    """Test that when file_name is empty string, UUID should be used as filename"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    test_bytes = b"test content"
    action = DownloadFileAction(
        file_name="",  # Empty string, handler will use UUID
        byte=test_bytes,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=temp_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Verify result (download_triggered is set by outer handle action flow when in context)
            assert len(result) == 1
            assert isinstance(result[0], ActionSuccess)

            # Verify file was created (filename should be UUID)
            files = os.listdir(temp_dir)
            assert len(files) == 1
            # Verify file content
            file_path = os.path.join(temp_dir, files[0])
            with open(file_path, "rb") as f:
                assert f.read() == test_bytes


@pytest.mark.asyncio
async def test_handle_download_file_action_download_url_error() -> None:
    """Test that when download_url download fails, should return ActionFailure"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="test_file.txt",
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    page.goto = AsyncMock(side_effect=Exception("Download failed"))

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

        assert len(result) == 1
        assert isinstance(result[0], ActionFailure)
        assert result[0].exception_type == "Exception"
        assert result[0].exception_message == "Download failed"


@pytest.mark.asyncio
async def test_handle_download_file_action_file_write_error() -> None:
    """Test that when file write fails, should return ActionFailure"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    # Create mock objects
    page = MagicMock()
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    test_bytes = b"test content"
    action = DownloadFileAction(
        file_name="test_file.txt",
        byte=test_bytes,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    # Mock initialize_download_dir to return an invalid path (e.g., read-only directory)
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a read-only directory to simulate write failure
        read_only_dir = os.path.join(temp_dir, "readonly")
        os.makedirs(read_only_dir, mode=0o555)

        with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value=read_only_dir):
            result = await handle_download_file_action(action, page, scraped_page, task, step)

            # Verify result should be ActionFailure
            assert len(result) == 1
            assert isinstance(result[0], ActionFailure)


@pytest.mark.asyncio
async def test_handle_download_file_action_download_url_err_aborted_swallowed() -> None:
    """Test that when page.goto raises net::ERR_ABORTED (browser download flow), error is swallowed and returns ActionSuccess"""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.goto = AsyncMock(side_effect=Exception("net::ERR_ABORTED at https://example.com/file.pdf"))
    browser_state = MagicMock()
    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = DownloadFileAction(
        file_name="test_file.txt",
        download_url="https://example.com/file.pdf",
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with patch("skyvern.webeye.actions.handler.initialize_download_dir", return_value="/tmp"):
        result = await handle_download_file_action(action, page, scraped_page, task, step)

        assert len(result) == 1
        assert isinstance(result[0], ActionSuccess)


@pytest.mark.asyncio
async def test_handle_action_navigates_back_from_blank_page_after_download(
    span_exporter: InMemorySpanExporter,
) -> None:
    """After a print/download click the working page sometimes navigates to about:blank.
    handle_action should detect this and navigate back to the original URL so the
    next step is not stuck on a blank page."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    original_url = "https://example.com/document/123"

    # Page starts at a real URL; the mocked action will navigate it to about:blank
    page = MagicMock()
    page.url = original_url

    browser_state = MagicMock()
    # Same page count before and after (no extra tab opened by the print action)
    browser_state.list_valid_pages = AsyncMock(return_value=[page])
    browser_state.navigate_to_url = AsyncMock()

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="btn-print",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    # _handle_action simulates the page navigating to about:blank during the print download
    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        page.url = "about:blank"
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        dummy_file = os.path.join(temp_dir, "doc.pdf")
        with open(dummy_file, "w") as f:
            f.write("dummy")

        # list_files_in_directory: empty before action, one file after action, re-scan after wait
        list_files_side_effect = [[], [dummy_file], [dummy_file]]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files_side_effect),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    # The blank-page recovery should have navigated back to the original URL
    browser_state.navigate_to_url.assert_called_once_with(page=page, url=original_url)
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "download_file_detected"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert 0 <= span_attrs["download_signal_elapsed_seconds"] < 1


@pytest.mark.asyncio
async def test_handle_action_does_not_navigate_back_when_page_url_unchanged() -> None:
    """When the page URL does not change to blank after a download, navigate_to_url should NOT be called."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    original_url = "https://example.com/document/123"

    page = MagicMock()
    page.url = original_url  # URL stays the same after download

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])
    browser_state.navigate_to_url = AsyncMock()

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="btn-print",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    # _handle_action does NOT change the page URL (normal case)
    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        dummy_file = os.path.join(temp_dir, "doc.pdf")
        with open(dummy_file, "w") as f:
            f.write("dummy")

        list_files_side_effect = [[], [dummy_file], [dummy_file]]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", side_effect=list_files_side_effect),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    # Page URL is unchanged; no navigation back should occur
    browser_state.navigate_to_url.assert_not_called()


@pytest.mark.asyncio
async def test_handle_action_download_no_signal_fails_fast(span_exporter: InMemorySpanExporter) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
        download_timeout=30.0,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/no-download"
    page.context.browser = None

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        started_at = time.monotonic()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0.01),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )
        elapsed = time.monotonic() - started_at

    assert elapsed < 1.0
    assert results[-1].download_triggered is False
    assert action.download_triggered is False
    assert wait_for_downloads.await_count == 0
    page.off.assert_called_once()
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is False
    assert "download_signal_source" not in span_attrs
    assert "download_signal_elapsed_seconds" not in span_attrs
    assert "download_signal_poll_iterations" not in span_attrs


@pytest.mark.asyncio
async def test_handle_action_download_fails_on_transient_user_defined_error_text(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now,
        organization=organization,
        page_url="https://example.com/portal/invoices",
        task_overrides={
            "error_code_mapping": {
                "data_not_downloadable": (
                    "Return this error if the page displays "
                    "download failure says the generated archive could not be saved"
                ),
            },
        },
    )
    existing_error = UserDefinedError(
        error_code="previous_error",
        reasoning="Earlier action error",
        confidence_float=0.8,
    )
    action.errors = [existing_error]
    page.evaluate = AsyncMock()

    async def expose_binding(_name: str, callback: Callable[[dict, dict], None]) -> None:
        page._transient_text_callback = callback

    page.expose_binding = AsyncMock(side_effect=expose_binding)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        page._transient_text_callback(
            {},
            {
                "text": "Example download failure says the generated archive could not be saved",
                "timestamp_ms": 1,
                "tag": "DIV",
                "role": "alert",
            },
        )
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        started_at = time.monotonic()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=temp_dir),
            patch("skyvern.webeye.actions.handler.list_files_in_directory", return_value=[]),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=None),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )
        elapsed = time.monotonic() - started_at

    assert elapsed < 1.0
    assert isinstance(results[-1], ActionFailure)
    assert results[-1].download_triggered is False
    assert "download failure says the generated archive could not be saved" in (results[-1].exception_message or "")
    assert action.download_triggered is False
    assert action.errors is not None
    assert [error.error_code for error in action.errors] == ["previous_error", "data_not_downloadable"]
    assert action.terminal_user_errors is True
    assert wait_for_downloads.await_count == 0
    page.off.assert_called_once()
    assert page.expose_binding.await_count == 1
    observer_install_count = sum(
        "new MutationObserver" in call.args[0] for call in page.evaluate.await_args_list if call.args
    )
    assert observer_install_count == 2
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is False
    assert span_attrs["download_wait_observed_text_count"] == 1
    assert span_attrs["download_wait_user_error_detected"] is True
    assert span_attrs["download_wait_user_error_codes"] == "data_not_downloadable"


@pytest.mark.asyncio
async def test_handle_action_prefers_observed_file_over_download_event_copy(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When the active run directory receives the file normally, the Playwright
    download event should only act as a signal and should not create a duplicate."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.save_as = AsyncMock()

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            download_callbacks["download"](download)
            with open(os.path.join(primary_dir, "report.pdf"), "w") as f:
                f.write("dummy")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1"),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]
    assert action.download_triggered is True
    assert action.downloaded_files == results[-1].downloaded_files
    assert wait_for_downloads.await_count == 1
    download.save_as.assert_not_awaited()
    page.off.assert_called_once_with("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert 0 <= span_attrs["download_signal_elapsed_seconds"] < 1


@pytest.mark.asyncio
async def test_handle_action_copies_download_event_when_no_observed_file_appears(
    span_exporter: InMemorySpanExporter,
) -> None:
    """A browser launched before a task/run id may still emit downloads in its
    original directory; after a grace period, copy the event into the active run directory."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"

    async def save_download(target_path: str | os.PathLike[str]) -> None:
        with open(target_path, "w") as f:
            f.write("dummy")

    download.save_as = AsyncMock(side_effect=save_download)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1"),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS", 0),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

    assert results[-1].download_triggered is True
    assert len(results[-1].downloaded_files) == 1
    assert results[-1].downloaded_files[0].endswith("-report.pdf")
    assert action.download_triggered is True
    assert action.downloaded_files == results[-1].downloaded_files
    assert wait_for_downloads.await_count == 1
    download.save_as.assert_awaited_once()
    saved_path = download.save_as.await_args.args[0]
    assert os.path.dirname(saved_path) == primary_dir
    page.off.assert_called_once_with("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert 0 <= span_attrs["download_signal_elapsed_seconds"] < 1


@pytest.mark.asyncio
async def test_handle_action_ignores_empty_download_event_fallback_file(
    span_exporter: InMemorySpanExporter,
) -> None:
    """An empty event fallback artifact should not be reported as a downloaded file."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
        download_timeout=0.01,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"

    async def save_empty_download(target_path: str | os.PathLike[str]) -> None:
        open(target_path, "w").close()

    download.save_as = AsyncMock(side_effect=save_empty_download)

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1"),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS", 0),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        remaining_files = os.listdir(primary_dir)

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files is None
    assert action.download_triggered is True
    assert action.downloaded_files is None
    assert remaining_files == []
    assert wait_for_downloads.await_count == 1
    download.save_as.assert_awaited_once()
    page.off.assert_called_once_with("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_signal_poll_iterations"] == 1
    assert 0 <= span_attrs["download_signal_elapsed_seconds"] < 1


@pytest.mark.asyncio
async def test_handle_action_stops_after_download_event_fallback_failure(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
        download_timeout=30.0,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.save_as = AsyncMock(side_effect=RuntimeError("copy failed"))

    async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
        download_callbacks["download"](download)
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()
        wait_for_downloads = AsyncMock()

        started_at = time.monotonic()
        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1"),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=wait_for_downloads,
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch("skyvern.webeye.actions.handler.DOWNLOAD_EVENT_ACTIVE_DIR_GRACE_SECONDS", 0),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )
        elapsed = time.monotonic() - started_at

    assert elapsed < 1.0
    assert results[-1].download_triggered is False
    assert action.download_triggered is False
    assert wait_for_downloads.await_count == 0
    download.save_as.assert_awaited_once()
    page.off.assert_called_once_with("download", download_callbacks["download"])
    span_attrs = _download_wait_span_attrs(span_exporter)
    assert span_attrs["download_signal_observed"] is True
    assert span_attrs["download_signal_source"] == "browser_download_event"
    assert span_attrs["download_event_fallback_attempted"] is True
    assert span_attrs["download_event_fallback_used"] is False
    assert span_attrs["download_event_fallback_failed"] is True


@pytest.mark.asyncio
async def test_handle_action_removes_late_zero_byte_duplicate_after_download_wait() -> None:
    """A 0-byte duplicate that appears after the first download signal should be removed.

    The polling loop exits as soon as one new file appears. Browser-native
    downloads can still surface a second empty duplicate artifact while waiting
    for ``.crdownload`` files to settle; that junk file must not be left for
    task cleanup to upload.
    """
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    page.context.browser = None

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        good_file = os.path.join(primary_dir, "report.pdf")
        empty_file = os.path.join(primary_dir, "report_1.pdf")

        async def mock_inner_handle_action(*args: object, **kwargs: object) -> list[ActionSuccess]:
            with open(good_file, "wb") as f:
                f.write(b"valid report")
            return [ActionSuccess()]

        async def wait_then_create_empty_file(*args: object, **kwargs: object) -> None:
            with open(empty_file, "wb"):
                pass

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner_handle_action),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1"),
            ),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(side_effect=wait_then_create_empty_file),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        remaining_files = sorted(os.listdir(primary_dir))

    assert results[-1].download_triggered is True
    assert results[-1].downloaded_files == ["report.pdf"]
    assert action.downloaded_files == ["report.pdf"]
    assert remaining_files == ["report.pdf"]
    page.off.assert_called_once()


@pytest.mark.asyncio
async def test_handle_action_removes_download_listener_when_inner_action_raises() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        workflow_run_id="wr-1",
        browser_session_id=None,
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    page = MagicMock()
    page.url = "https://example.com/download"
    download_callbacks: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event, callback: download_callbacks.__setitem__(event, callback)

    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page])

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )

    action = ClickAction(
        element_id="download-link",
        download=True,
        organization_id=task.organization_id,
        task_id=task.task_id,
        step_id=step.step_id,
    )

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=RuntimeError("boom")),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch(
                "skyvern.webeye.actions.handler.skyvern_context.current",
                return_value=MagicMock(run_id="pbs-1"),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )

    page.off.assert_called_once_with("download", download_callbacks["download"])


@pytest.mark.asyncio
async def test_handle_action_discards_xhr_staging_when_native_file_present(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )

    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    download = MagicMock()
    download.suggested_filename = "report.pdf"
    download.save_as = AsyncMock()

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "report.pdf"), "wb") as f:
                f.write(b"xhr content")
            callbacks["download"](download)
            with open(os.path.join(primary_dir, "native-guid.pdf"), "wb") as f:
                f.write(b"native content")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=MagicMock(run_id="pbs-1")),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        assert results[-1].download_triggered is True
        assert results[-1].downloaded_files == ["native-guid.pdf"]
        assert not os.path.exists(staging)


@pytest.mark.asyncio
async def test_handle_action_uses_xhr_staging_fallback_when_no_native_file(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )

    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "report.pdf"), "wb") as f:
                f.write(b"xhr-only content")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=MagicMock(run_id="pbs-1")),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        assert results[-1].download_triggered is True
        assert results[-1].downloaded_files == ["report.pdf"]
        assert os.path.isfile(os.path.join(primary_dir, "report.pdf"))
        assert not os.path.exists(staging)


@pytest.mark.asyncio
async def test_handle_action_moves_multiple_staged_xhr_files_as_fallback(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )

    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "file_a.pdf"), "wb") as f:
                f.write(b"content a")
            with open(os.path.join(staging, "file_b.zip"), "wb") as f:
                f.write(b"content b")
            return [ActionSuccess()]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=MagicMock(run_id="pbs-1")),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        assert results[-1].download_triggered is True
        assert sorted(results[-1].downloaded_files) == ["file_a.pdf", "file_b.zip"]


@pytest.mark.asyncio
async def test_handle_action_cleans_staging_on_exception(
    span_exporter: InMemorySpanExporter,
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )

    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "orphan.pdf"), "wb") as f:
                f.write(b"data")
            raise RuntimeError("simulated crash")

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=MagicMock(run_id="pbs-1")),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
        ):
            with pytest.raises(RuntimeError, match="simulated crash"):
                await ActionHandler.handle_action(
                    scraped_page=scraped_page,
                    task=task,
                    step=step,
                    page=page,
                    action=action,
                )

        assert not os.path.exists(staging)


@pytest.mark.asyncio
async def test_handle_action_logs_warning_when_late_native_appears_after_xhr_fallback(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When XHR fallback moves staged files and a late native file appears during
    the settle wait, a warning log should be emitted for observability."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task, step, page, browser_state, scraped_page, action = _make_download_click_context(
        now=now, organization=organization, page_url="https://example.com/download"
    )

    callbacks: dict[str, object] = {}
    page.on.side_effect = lambda event, cb: callbacks.__setitem__(event, cb)

    with tempfile.TemporaryDirectory() as temp_root:
        primary_dir = os.path.join(temp_root, "pbs-1")
        os.makedirs(primary_dir)
        staging = os.path.join(temp_root, "xhr_staging")
        os.makedirs(staging)

        async def mock_inner(*args, **kw):
            with open(os.path.join(staging, "report.zip"), "wb") as f:
                f.write(b"xhr zip content")
            return [ActionSuccess()]

        async def mock_settle(**kw):
            with open(os.path.join(primary_dir, "native-late.zip"), "wb") as f:
                f.write(b"native zip content different")

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
        mock_app.STORAGE = MagicMock()

        log_warnings: list[tuple] = []
        original_log = __import__("skyvern.webeye.actions.handler", fromlist=["LOG"]).LOG

        def capture_warning(*args, **kwargs):
            log_warnings.append((args, kwargs))

        with (
            patch.object(ActionHandler, "_handle_action", side_effect=mock_inner),
            patch("skyvern.webeye.actions.handler.get_download_dir", return_value=primary_dir),
            patch("skyvern.webeye.actions.handler.make_temp_directory", return_value=staging),
            patch("skyvern.webeye.actions.handler.BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME", 0),
            patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=MagicMock(run_id="pbs-1")),
            patch(
                "skyvern.webeye.actions.handler.check_downloading_files_and_wait_for_download_to_complete",
                new=AsyncMock(side_effect=mock_settle),
            ),
            patch("skyvern.webeye.actions.handler.app", mock_app),
            patch.object(original_log, "warning", side_effect=capture_warning),
        ):
            results = await ActionHandler.handle_action(
                scraped_page=scraped_page,
                task=task,
                step=step,
                page=page,
                action=action,
            )

        assert results[-1].download_triggered is True
        assert sorted(results[-1].downloaded_files) == ["native-late.zip", "report.zip"]

        race_warnings = [
            (args, kwargs)
            for args, kwargs in log_warnings
            if args and "additional download files appeared" in str(args[0])
        ]
        assert len(race_warnings) == 1
        _, kwargs = race_warnings[0]
        assert kwargs["workflow_run_id"] == "wr-1"
        assert kwargs["xhr_fallback_file_count"] == 1
        assert kwargs["xhr_fallback_files"] == ["report.zip"]
        assert kwargs["post_settle_extra_file_count"] == 1
        assert kwargs["post_settle_extra_files"] == ["native-late.zip"]
