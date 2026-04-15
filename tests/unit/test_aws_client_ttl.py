import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from skyvern.forge.sdk.api import aws

_EXPIRED_TOKEN_ERROR = ClientError(
    {"Error": {"Code": "ExpiredTokenException", "Message": "Token expired"}},
    "S3Operation",
)


@pytest.fixture(autouse=True)
def reset_aws_client():
    """Reset the global singleton before each test."""
    aws._aws_client = None
    aws._aws_client_created_at = 0.0
    yield
    aws._aws_client = None
    aws._aws_client_created_at = 0.0


def test_get_aws_client_returns_same_instance_within_ttl():
    client1 = aws.get_aws_client()
    client2 = aws.get_aws_client()
    assert client1 is client2


def test_get_aws_client_recreates_after_ttl():
    client1 = aws.get_aws_client()
    # Simulate TTL expiry by backdating the creation time
    aws._aws_client_created_at = time.monotonic() - (aws._AWS_CLIENT_TTL_SECONDS + 1)
    client2 = aws.get_aws_client()
    assert client1 is not client2


def test_refresh_session_creates_new_session():
    client = aws.get_aws_client()
    old_session = client.session
    client.refresh_session()
    assert client.session is not old_session


@pytest.mark.asyncio
async def test_upload_file_retries_on_expired_token():
    """upload_file_from_path should refresh the session and retry once on ExpiredTokenException."""
    mock_upload = AsyncMock(side_effect=[_EXPIRED_TOKEN_ERROR, None])

    client = aws.get_aws_client()

    with patch.object(client, "_s3_client") as mock_s3_ctx:
        mock_s3_client = AsyncMock()
        mock_s3_client.upload_file = mock_upload
        mock_s3_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_s3_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "refresh_session") as mock_refresh:
            await client.upload_file_from_path(
                uri="s3://test-bucket/test-key.png",
                file_path="/tmp/test.png",
            )
            mock_refresh.assert_called_once()
            assert mock_upload.call_count == 2


@pytest.mark.asyncio
async def test_upload_file_stream_resets_cursor_on_retry():
    """upload_file_stream should seek(0) before retrying to avoid truncated uploads."""
    from io import BytesIO

    mock_upload = AsyncMock(side_effect=[_EXPIRED_TOKEN_ERROR, None])
    file_obj = BytesIO(b"test data")

    client = aws.get_aws_client()

    with patch.object(client, "_s3_client") as mock_s3_ctx:
        mock_s3_client = AsyncMock()
        mock_s3_client.upload_fileobj = mock_upload
        mock_s3_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_s3_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "refresh_session"):
            result = await client.upload_file_stream(
                uri="s3://test-bucket/test-key.bin",
                file_obj=file_obj,
            )
            assert result == "s3://test-bucket/test-key.bin"
            assert mock_upload.call_count == 2


@pytest.mark.asyncio
async def test_upload_file_stream_fails_if_stream_not_seekable():
    """Non-seekable streams should not retry (would produce truncated uploads)."""
    import io

    mock_upload = AsyncMock(side_effect=[_EXPIRED_TOKEN_ERROR, None])

    # Create a stream that raises on seek
    class NonSeekableStream(io.RawIOBase):
        def read(self, n=-1):
            return b"test data"

        def seek(self, offset, whence=0):
            raise io.UnsupportedOperation("seek")

    file_obj = NonSeekableStream()
    client = aws.get_aws_client()

    with patch.object(client, "_s3_client") as mock_s3_ctx:
        mock_s3_client = AsyncMock()
        mock_s3_client.upload_fileobj = mock_upload
        mock_s3_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_s3_client)
        mock_s3_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client, "refresh_session"):
            result = await client.upload_file_stream(
                uri="s3://test-bucket/test-key.bin",
                file_obj=file_obj,
            )
            assert result is None
            assert mock_upload.call_count == 1


def _make_s3_client_mock(client_obj: AsyncMock) -> MagicMock:
    """Helper to create a mock _s3_client context manager wrapping a mock boto client."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client_obj)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.asyncio
async def test_upload_file_retries_on_expired_token_put_object():
    """upload_file (put_object) should now retry on expired token."""
    mock_put = AsyncMock(side_effect=[_EXPIRED_TOKEN_ERROR, None])
    client = aws.get_aws_client()

    mock_s3 = AsyncMock()
    mock_s3.put_object = mock_put

    with patch.object(client, "_s3_client", return_value=_make_s3_client_mock(mock_s3)):
        with patch.object(client, "refresh_session") as mock_refresh:
            result = await client.upload_file(
                uri="s3://test-bucket/test-key.png",
                data=b"image bytes",
            )
            mock_refresh.assert_called_once()
            assert mock_put.call_count == 2
            assert result == "s3://test-bucket/test-key.png"


@pytest.mark.asyncio
async def test_download_file_retries_on_expired_token():
    """download_file should retry on expired token."""
    body_mock = AsyncMock()
    body_mock.read = AsyncMock(return_value=b"file contents")
    mock_get = AsyncMock(side_effect=[_EXPIRED_TOKEN_ERROR, {"Body": body_mock}])
    client = aws.get_aws_client()

    mock_s3 = AsyncMock()
    mock_s3.get_object = mock_get

    with patch.object(client, "_s3_client", return_value=_make_s3_client_mock(mock_s3)):
        with patch.object(client, "refresh_session") as mock_refresh:
            result = await client.download_file(uri="s3://test-bucket/test-key.bin")
            mock_refresh.assert_called_once()
            assert mock_get.call_count == 2
            assert result == b"file contents"


@pytest.mark.asyncio
async def test_delete_file_retries_on_expired_token():
    """delete_file should retry on expired token."""
    mock_delete = AsyncMock(side_effect=[_EXPIRED_TOKEN_ERROR, None])
    client = aws.get_aws_client()

    mock_s3 = AsyncMock()
    mock_s3.delete_object = mock_delete

    with patch.object(client, "_s3_client", return_value=_make_s3_client_mock(mock_s3)):
        with patch.object(client, "refresh_session") as mock_refresh:
            await client.delete_file(uri="s3://test-bucket/test-key.bin")
            mock_refresh.assert_called_once()
            assert mock_delete.call_count == 2


@pytest.mark.asyncio
async def test_get_object_info_retries_on_expired_token():
    """get_object_info should retry on expired token."""
    mock_head = AsyncMock(side_effect=[_EXPIRED_TOKEN_ERROR, {"ContentLength": 42}])
    client = aws.get_aws_client()

    mock_s3 = AsyncMock()
    mock_s3.head_object = mock_head

    with patch.object(client, "_s3_client", return_value=_make_s3_client_mock(mock_s3)):
        with patch.object(client, "refresh_session") as mock_refresh:
            result = await client.get_object_info(uri="s3://test-bucket/test-key.bin")
            mock_refresh.assert_called_once()
            assert mock_head.call_count == 2
            assert result == {"ContentLength": 42}
