"""Test for the StreamingService integration."""

from unittest.mock import AsyncMock, MagicMock, patch

import asyncio
import platform
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.services.streaming.service import StreamingService


@pytest.mark.asyncio
async def test_streaming_service_initialization():
    """Test that StreamingService can be initialized."""
    # Initialize ForgeApp before creating service
    start_forge_app()

    service = StreamingService()
    assert service is not None
    assert service._monitoring_task is None
    assert not service._stop_event.is_set()


@pytest.mark.asyncio
async def test_streaming_service_start_stop_monitoring():
    """Test starting and stopping the monitoring loop."""
    # Initialize ForgeApp before creating service
    start_forge_app()

    service = StreamingService()

    # Start monitoring (should create a task)
    with patch.object(service, "_monitor_loop", new_callable=AsyncMock):
        task = service.start_monitoring()
        assert task is not None
        # The loop gets called immediately since we're not patching asyncio.create_task

    # Stop monitoring (should set event and cancel task)
    await service.stop_monitoring()
    assert service._monitoring_task is None


@pytest.mark.asyncio
async def test_streaming_service_capture_screenshot_success():
    """Test successful screenshot capture."""
    # Initialize ForgeApp before creating service
    start_forge_app()

    service = StreamingService()

    # Mock asyncio.create_subprocess_shell to return a successful process
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"", b"")
    
    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        result = await service._capture_screenshot("/tmp/test.png")
        assert result is True
        mock_proc.communicate.assert_awaited_once()


@pytest.mark.asyncio
async def test_streaming_service_capture_screenshot_failure():
    """Test failed screenshot capture."""
    # Initialize ForgeApp before creating service
    start_forge_app()

    service = StreamingService()

    # Mock asyncio.create_subprocess_shell to return a failed process
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"error")
    
    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        result = await service._capture_screenshot("/tmp/test.png")
        assert result is False
        mock_proc.communicate.assert_awaited_once()


@pytest.mark.asyncio
async def test_streaming_service_capture_once():
    """Test one-time screenshot capture and upload."""
    # Initialize ForgeApp before creating service
    start_forge_app()

    service = StreamingService()

    with patch.object(service, "_capture_screenshot", new_callable=AsyncMock) as mock_capture:
        with patch("skyvern.forge.app.STORAGE.save_streaming_file", new_callable=AsyncMock) as mock_upload:
            mock_capture.return_value = True
            mock_upload.return_value = None

            result = await service.capture_screenshot_once("org_123", "task_456")
            assert result == "task_456.png"
            mock_capture.assert_called_once()
            mock_upload.assert_called_once_with("org_123", "task_456.png")


@pytest.mark.asyncio
async def test_streaming_service_monitor_loop_basic_flow():
    """Test the monitoring loop basic flow."""
    # Initialize ForgeApp before creating service
    start_forge_app()

    service = StreamingService()

    with patch("skyvern.utils.files.get_json_from_file") as mock_get_json:
        with patch("skyvern.utils.files.get_skyvern_state_file_path") as mock_get_path:
            with patch.object(service, "_capture_screenshot", new_callable=AsyncMock):
                with patch("skyvern.forge.app.STORAGE.save_streaming_file", new_callable=AsyncMock):
                    with patch("os.makedirs"):
                        mock_get_path.return_value = "/tmp/state.json"
                        mock_get_json.return_value = {
                            "task_id": "task_123",
                            "workflow_run_id": None,
                            "organization_id": "org_456",
                        }

                        with patch("skyvern.forge.app.DATABASE.get_task", new_callable=AsyncMock) as mock_get_task:
                            task_mock = MagicMock()
                            task_mock.status.is_final.return_value = False
                            mock_get_task.return_value = task_mock

                        service._stop_event.set()  # Stop after one iteration
                        await service._monitor_loop()


@pytest.mark.asyncio
async def test_streaming_service_platform_compatibility():
    """Test platform compatibility check."""
    # Initialize ForgeApp before creating service
    start_forge_app()
    
    service = StreamingService()
    
    # Test that the platform check is called and doesn't raise an error on Linux/WSL
    # Mock the is_linux_or_wsl function to return True
    with patch("skyvern.services.streaming.service.is_linux_or_wsl", return_value=True):
        # This should not raise an error
        await service.start_monitoring()
        # If we get here, the test passed
        assert True


@pytest.mark.asyncio
async def test_streaming_service_platform_incompatibility():
    """Test platform incompatibility raises error."""
    # Initialize ForgeApp before creating service
    start_forge_app()
    
    service = StreamingService()
    
    # Mock the is_linux_or_wsl function to return False
    with patch("skyvern.services.streaming.service.is_linux_or_wsl", return_value=False):
        # This should raise a RuntimeError
        with pytest.raises(RuntimeError, match="Streaming service is only supported on Linux or WSL"):
            await service.start_monitoring()


@pytest.mark.asyncio
async def test_streaming_service_async_subprocess_capture():
    """Test async subprocess screenshot capture."""
    # Initialize ForgeApp before creating service
    start_forge_app()
    
    service = StreamingService()
    
    # Mock asyncio.create_subprocess_shell to return a successful process
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"", b"")
    
    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        result = await service._capture_screenshot("/tmp/test.png")
        assert result is True
        mock_proc.communicate.assert_awaited_once()


@pytest.mark.asyncio
async def test_streaming_service_async_subprocess_capture_failure():
    """Test async subprocess screenshot capture failure."""
    # Initialize ForgeApp before creating service
    start_forge_app()
    
    service = StreamingService()
    
    # Mock asyncio.create_subprocess_shell to return a failed process
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"error")
    
    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        result = await service._capture_screenshot("/tmp/test.png")
        assert result is False
        mock_proc.communicate.assert_awaited_once()
