import os
import tempfile
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import DownloadFileAction
from skyvern.webeye.actions.handler import handle_download_file_action
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
