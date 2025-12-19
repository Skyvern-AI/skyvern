from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.api.azure import StandardBlobTier
from skyvern.forge.sdk.api.real_azure import RealAsyncAzureStorageClient
from skyvern.forge.sdk.artifact.storage.azure import AzureStorage

# Test constants
TEST_CONTAINER = "test-azure-container"
TEST_ORGANIZATION_ID = "test-org-123"
TEST_BROWSER_SESSION_ID = "bs_test_123"


class AzureStorageForTests(AzureStorage):
    """Test subclass that overrides org-specific methods and bypasses client init."""

    async_client: Any  # Allow mock attribute access

    def __init__(self, container: str) -> None:
        # Don't call super().__init__ to avoid creating real RealAsyncAzureStorageClient
        self.container = container
        self.async_client = AsyncMock()

    async def _get_storage_tier_for_org(self, organization_id: str) -> StandardBlobTier:
        return StandardBlobTier.HOT

    async def _get_tags_for_org(self, organization_id: str) -> dict[str, str]:
        return {"test": "tag"}


@pytest.fixture
def azure_storage() -> AzureStorageForTests:
    """Create AzureStorage with mocked async_client."""
    return AzureStorageForTests(container=TEST_CONTAINER)


@pytest.mark.asyncio
class TestAzureStorageBrowserSessionFiles:
    """Test AzureStorage browser session file methods."""

    async def test_sync_browser_session_file_with_date(
        self, azure_storage: AzureStorageForTests, tmp_path: Path
    ) -> None:
        """Test syncing a file with date in path (videos/har)."""
        test_file = tmp_path / "recording.webm"
        test_file.write_bytes(b"fake video data")

        uri = await azure_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            local_file_path=str(test_file),
            remote_path="recording.webm",
            date="2025-01-15",
        )

        expected_uri = f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/videos/2025-01-15/recording.webm"
        assert uri == expected_uri
        azure_storage.async_client.upload_file_from_path.assert_called_once()

    async def test_sync_browser_session_file_without_date(
        self, azure_storage: AzureStorageForTests, tmp_path: Path
    ) -> None:
        """Test syncing a file without date (downloads category)."""
        test_file = tmp_path / "document.pdf"
        test_file.write_bytes(b"fake download data")

        uri = await azure_storage.sync_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="downloads",
            local_file_path=str(test_file),
            remote_path="document.pdf",
            date=None,
        )

        expected_uri = f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/downloads/document.pdf"
        assert uri == expected_uri

    async def test_browser_session_file_exists_returns_true(self, azure_storage: AzureStorageForTests) -> None:
        """Test browser_session_file_exists returns True when file exists."""
        azure_storage.async_client.get_object_info.return_value = {"LastModified": "2025-01-15"}

        exists = await azure_storage.browser_session_file_exists(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="exists.webm",
            date="2025-01-15",
        )

        assert exists is True

    async def test_browser_session_file_exists_returns_false_on_exception(
        self, azure_storage: AzureStorageForTests
    ) -> None:
        """Test browser_session_file_exists returns False when exception is raised."""
        azure_storage.async_client.get_object_info.side_effect = Exception("Not found")

        exists = await azure_storage.browser_session_file_exists(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="nonexistent.webm",
            date="2025-01-15",
        )

        assert exists is False

    async def test_delete_browser_session_file(self, azure_storage: AzureStorageForTests) -> None:
        """Test deleting a browser session file."""
        await azure_storage.delete_browser_session_file(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="to_delete.webm",
            date="2025-01-15",
        )

        expected_uri = f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/videos/2025-01-15/to_delete.webm"
        azure_storage.async_client.delete_file.assert_called_once_with(expected_uri)

    async def test_file_exists_returns_true(self, azure_storage: AzureStorageForTests) -> None:
        """Test file_exists returns True when file exists."""
        azure_storage.async_client.get_object_info.return_value = {"LastModified": "2025-01-15"}
        uri = f"azure://{TEST_CONTAINER}/test/file.txt"

        exists = await azure_storage.file_exists(uri)

        assert exists is True

    async def test_file_exists_returns_false_on_exception(self, azure_storage: AzureStorageForTests) -> None:
        """Test file_exists returns False when exception is raised (404)."""
        azure_storage.async_client.get_object_info.side_effect = Exception("Not found")
        uri = f"azure://{TEST_CONTAINER}/nonexistent/file.txt"

        exists = await azure_storage.file_exists(uri)

        assert exists is False

    async def test_download_uploaded_file(self, azure_storage: AzureStorageForTests) -> None:
        """Test downloading an uploaded file."""
        test_data = b"uploaded file content"
        azure_storage.async_client.download_file.return_value = test_data
        uri = f"azure://{TEST_CONTAINER}/uploads/file.pdf"

        downloaded = await azure_storage.download_uploaded_file(uri)

        assert downloaded == test_data
        azure_storage.async_client.download_file.assert_called_once_with(uri, log_exception=False)

    async def test_download_uploaded_file_returns_none(self, azure_storage: AzureStorageForTests) -> None:
        """Test downloading a non-existent file returns None."""
        azure_storage.async_client.download_file.return_value = None
        uri = f"azure://{TEST_CONTAINER}/nonexistent/file.txt"

        downloaded = await azure_storage.download_uploaded_file(uri)

        assert downloaded is None

    def test_storage_type_property(self, azure_storage: AzureStorageForTests) -> None:
        """Test storage_type returns 'azure'."""
        assert azure_storage.storage_type == "azure"


class TestAzureStorageBuildUri:
    """Test Azure URI building methods."""

    def test_build_browser_session_uri_with_date(self, azure_storage: AzureStorageForTests) -> None:
        """Test building URI with date."""
        uri = azure_storage._build_browser_session_uri(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="videos",
            remote_path="file.webm",
            date="2025-01-15",
        )

        expected = f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/videos/2025-01-15/file.webm"
        assert uri == expected

    def test_build_browser_session_uri_without_date(self, azure_storage: AzureStorageForTests) -> None:
        """Test building URI without date."""
        uri = azure_storage._build_browser_session_uri(
            organization_id=TEST_ORGANIZATION_ID,
            browser_session_id=TEST_BROWSER_SESSION_ID,
            artifact_type="downloads",
            remote_path="file.pdf",
            date=None,
        )

        expected = f"azure://{TEST_CONTAINER}/v1/{settings.ENV}/{TEST_ORGANIZATION_ID}/browser_sessions/{TEST_BROWSER_SESSION_ID}/downloads/file.pdf"
        assert uri == expected


AZURE_CONTENT_TYPE_TEST_CASES = [
    # (filename, expected_content_type, artifact_type, date)
    ("video.webm", "video/webm", "videos", "2025-01-15"),
    ("data.json", "application/json", "har", "2025-01-15"),
    ("network.har", "application/json", "har", "2025-01-15"),
    ("screenshot.png", "image/png", "downloads", None),
    ("output.txt", "text/plain", "downloads", None),
    ("debug.log", "text/plain", "downloads", None),
]


@pytest.mark.asyncio
class TestAzureStorageContentType:
    """Test Azure Storage content type guessing.

    Tests at two levels:
    1. High-level: sync_browser_session_file interface with artifact_type/date
    2. Low-level: RealAsyncAzureStorageClient to verify ContentSettings is passed
    """

    @pytest.mark.parametrize("filename,expected_content_type,artifact_type,date", AZURE_CONTENT_TYPE_TEST_CASES)
    async def test_content_type_guessing(
        self,
        tmp_path: Path,
        filename: str,
        expected_content_type: str,
        artifact_type: str,
        date: str | None,
    ) -> None:
        """Test that RealAsyncAzureStorageClient sets correct content type based on extension."""
        test_file = tmp_path / filename
        test_file.write_bytes(b"test content")

        with patch.object(RealAsyncAzureStorageClient, "_get_blob_service_client") as mock_get_client:
            mock_container_client = MagicMock()
            mock_container_client.upload_blob = AsyncMock()
            mock_container_client.exists = AsyncMock(return_value=True)
            mock_blob_service = MagicMock()
            mock_blob_service.get_container_client.return_value = mock_container_client
            mock_get_client.return_value = mock_blob_service

            client = RealAsyncAzureStorageClient(account_name="test", account_key="testkey")
            client._verified_containers.add("test-container")

            await client.upload_file_from_path(
                uri=f"azure://test-container/path/{filename}",
                file_path=str(test_file),
            )

            call_kwargs = mock_container_client.upload_blob.call_args.kwargs
            assert call_kwargs["content_settings"] is not None
            assert call_kwargs["content_settings"].content_type == expected_content_type
