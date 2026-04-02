import os
import tempfile
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import ClickAction, DownloadFileAction
from skyvern.webeye.actions.handler import ActionHandler, handle_download_file_action
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import make_organization, make_step, make_task


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
async def test_handle_action_navigates_back_from_blank_page_after_download() -> None:
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
    page.is_closed.return_value = False

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
    async def mock_inner_handle_action(*args, **kwargs) -> list[ActionSuccess]:
        page.url = "about:blank"
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        dummy_file = os.path.join(temp_dir, "doc.pdf")
        with open(dummy_file, "w") as f:
            f.write("dummy")

        # list_files_in_directory: empty before action, one file after action
        list_files_side_effect = [[], [dummy_file]]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.create_action = AsyncMock(return_value=action)
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
    async def mock_inner_handle_action(*args, **kwargs) -> list[ActionSuccess]:
        return [ActionSuccess()]

    with tempfile.TemporaryDirectory() as temp_dir:
        dummy_file = os.path.join(temp_dir, "doc.pdf")
        with open(dummy_file, "w") as f:
            f.write("dummy")

        list_files_side_effect = [[], [dummy_file]]

        mock_app = MagicMock()
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.DATABASE.create_action = AsyncMock(return_value=action)
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
