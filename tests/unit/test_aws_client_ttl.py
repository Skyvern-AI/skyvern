import asyncio
import contextlib
import datetime
import ssl
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from botocore.exceptions import ClientError, ProfileNotFound
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from skyvern.forge.sdk.api import aws

_EXPIRED_TOKEN_ERROR = ClientError(
    {"Error": {"Code": "ExpiredTokenException", "Message": "Token expired"}},
    "S3Operation",
)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


@pytest.mark.parametrize("code", ["NoSuchKey", "NotFound", "404"])
def test_is_not_found_error_true_for_missing_object_codes(code: str) -> None:
    client = aws.AsyncAWSClient()
    assert client._is_not_found_error(_client_error(code)) is True


@pytest.mark.parametrize("error", [_client_error("AccessDenied"), Exception("boom")])
def test_is_not_found_error_false_for_other_errors(error: Exception) -> None:
    client = aws.AsyncAWSClient()
    assert client._is_not_found_error(error) is False


@pytest.mark.asyncio
async def test_download_file_missing_key_returns_none_without_traceback() -> None:
    client = aws.AsyncAWSClient()
    with (
        patch.object(client, "_s3_with_retry", AsyncMock(side_effect=_client_error("NoSuchKey"))),
        patch.object(aws, "LOG") as mock_log,
    ):
        result = await client.download_file("s3://bucket/missing.zip")
    assert result is None
    mock_log.exception.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_real_error_still_logs_exception() -> None:
    client = aws.AsyncAWSClient()
    with (
        patch.object(client, "_s3_with_retry", AsyncMock(side_effect=_client_error("AccessDenied"))),
        patch.object(aws, "LOG") as mock_log,
    ):
        result = await client.download_file("s3://bucket/denied.zip")
    assert result is None
    mock_log.exception.assert_called_once()


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


def test_client_session_reused_within_ttl():
    with patch.object(aws.aioboto3, "Session", side_effect=lambda **_: MagicMock()) as mock_session:
        client = aws.AsyncAWSClient()
        first = client.session
        second = client.session

    assert first is second
    assert mock_session.call_count == 1


def test_client_session_recreated_after_ttl():
    """Any holder of AsyncAWSClient (e.g. the storage singleton on a long-lived worker) gets a
    fresh session past the TTL, not just callers of the module-level get_aws_client() factory."""
    with patch.object(aws.aioboto3, "Session", side_effect=lambda **_: MagicMock()) as mock_session:
        client = aws.AsyncAWSClient()
        first = client.session
        client._session_created_at = time.monotonic() - (aws._SESSION_TTL_SECONDS + 1)
        second = client.session

    assert first is not second
    assert mock_session.call_count == 2


def test_setter_stamps_session_ttl_clock():
    """A backdated clock plus a setter-installed session must not trigger recreation: the setter
    stamps the TTL clock. Deterministic regardless of host uptime, unlike the compat test."""
    client = aws.AsyncAWSClient()
    client._session_created_at = -(aws._SESSION_TTL_SECONDS + 1)
    session = MagicMock()

    client.session = session

    assert client._session_created_at > 0
    assert client.session is session


def test_no_profile_session_creation_uses_default_credential_chain(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    session = MagicMock()

    with patch.object(aws.aioboto3, "Session", return_value=session) as mock_session:
        client = aws.AsyncAWSClient()
        mock_session.assert_not_called()

        assert client.session is session

        mock_session.assert_called_once_with(
            aws_access_key_id=None,
            aws_secret_access_key=None,
            profile_name=None,
        )


def test_session_attribute_remains_settable_for_compatibility():
    session = MagicMock()
    client = aws.AsyncAWSClient()

    client.session = session

    assert client.session is session


@pytest.mark.asyncio
async def test_missing_profile_is_deferred_until_aws_operation():
    profile_name = "__skyvern_missing_profile__"

    with patch.object(aws.aioboto3, "Session", side_effect=ProfileNotFound(profile=profile_name)) as mock_session:
        client = aws.AsyncAWSClient(profile_name=profile_name)
        mock_session.assert_not_called()

        with patch.object(aws.LOG, "exception") as mock_log_exception:
            result = await client.get_secret("example-secret")

        assert result is None
        mock_session.assert_called_once_with(
            aws_access_key_id=None,
            aws_secret_access_key=None,
            profile_name=profile_name,
        )
        mock_log_exception.assert_called_once()
        assert mock_log_exception.call_args.kwargs["error_code"] == "AWSSessionConfigurationError"


@pytest.mark.asyncio
async def test_missing_profile_raise_path_surfaces_scoped_error():
    profile_name = "__skyvern_missing_profile__"

    with patch.object(aws.aioboto3, "Session", side_effect=ProfileNotFound(profile=profile_name)):
        client = aws.AsyncAWSClient(profile_name=profile_name)

        with pytest.raises(aws.AWSSessionConfigurationError, match=f"AWS profile '{profile_name}'.*s3 client"):
            await client.upload_file_from_path(
                uri="s3://test-bucket/test-key.png",
                file_path="/tmp/test.png",
                raise_exception=True,
            )


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


@pytest.mark.parametrize("endpoint_url", ["", "   "], ids=["empty", "whitespace"])
@pytest.mark.asyncio
async def test_blank_endpoint_url_falls_back_to_aws(endpoint_url: str) -> None:
    """A blank endpoint must behave like an unset one.

    Workflow blocks can carry endpoint_url="" from the editor or from a template that renders
    empty, and botocore raises "Invalid endpoint:" rather than defaulting to AWS the way an
    empty region_name does.
    """
    client = aws.AsyncAWSClient(
        aws_access_key_id="AKIA-test",
        aws_secret_access_key="secret-test",
        region_name="us-east-1",
        endpoint_url=endpoint_url,
    )

    assert client._endpoint_url is None
    async with client._s3_client() as s3_client:
        assert s3_client.meta.endpoint_url == "https://s3.amazonaws.com"


@pytest.mark.asyncio
async def test_pinned_resolver_refuses_hosts_other_than_the_endpoint() -> None:
    """A connector shares one resolver, so anything but the pinned host must fail closed."""
    resolver = aws._PinnedIPResolver("storage.example.com", ("198.51.100.7",))

    resolved = await resolver.resolve("storage.example.com", 443)
    assert [entry["host"] for entry in resolved] == ["198.51.100.7"]
    # Echoing the requested name back is the resolver contract. It is not what makes TLS verify
    # against the hostname — aiohttp takes that from the request URL.
    assert resolved[0]["hostname"] == "storage.example.com"

    with pytest.raises(OSError):
        await resolver.resolve("attacker.example.com", 443)


def test_no_connector_config_without_pinned_ips() -> None:
    """The default AWS path must not have its connector altered."""
    assert aws.AsyncAWSClient()._config is None
    assert aws.AsyncAWSClient(endpoint_url="https://storage.example.com")._config is None


@pytest.mark.asyncio
async def test_resolved_ips_pin_the_connection_and_preserve_the_host_header() -> None:
    """The S3 client dials the validated address instead of re-resolving the name.

    ``pinned.invalid`` never resolves, so reaching the local server at all proves the
    connection went to the pinned IP rather than through DNS.
    """
    seen: list[str | None] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.headers.get("Host"))
        return web.Response(status=200)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        client = aws.AsyncAWSClient(
            aws_access_key_id="AKIA-test",
            aws_secret_access_key="secret-test",
            region_name="us-east-1",
            endpoint_url=f"http://pinned.invalid:{port}",
            endpoint_resolved_ips=("127.0.0.1",),
        )
        async with client._s3_client() as s3_client:
            await s3_client.put_object(Bucket="bucket", Key="k.txt", Body=b"hello")
    finally:
        await runner.cleanup()

    assert seen == [f"pinned.invalid:{port}"]


def _self_signed_cert(directory: Path, hostname: str) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        # The SAN deliberately omits 127.0.0.1, so verification can only succeed if the
        # hostname (not the pinned IP) was used as the TLS identity.
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.mark.asyncio
async def test_pinned_connection_verifies_tls_against_the_hostname(tmp_path: Path) -> None:
    """Certificate verification must bind to the hostname, not the pinned IP.

    Cloud requires https, so this is the production path. The hostname binding is aiohttp's
    behaviour, taken from the request URL, so this guards a dependency contract: an upgrade
    that changed SNI handling to follow the connected address would fail verification for
    every real customer endpoint, and nothing else in the suite would notice.
    """
    hostname = "pinned.invalid"
    cert_path, key_path = _self_signed_cert(tmp_path, hostname)
    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.headers.get("Host", ""))
        return web.Response(text="ok")

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    server_ctx.load_cert_chain(cert_path, key_path)
    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ctx)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        # Trust only this cert; verification stays fully on.
        client_ctx = ssl.create_default_context(cafile=str(cert_path))
        connector = aiohttp.TCPConnector(
            resolver=aws._PinnedIPResolver(hostname, ("127.0.0.1",)),
            ssl=client_ctx,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(f"https://{hostname}:{port}/") as response:
                assert response.status == 200
                assert await response.text() == "ok"

            # A different host must fail closed rather than fall back to real DNS.
            with pytest.raises(aiohttp.ClientError):
                await session.get(f"https://example.com:{port}/")
    finally:
        await runner.cleanup()

    assert seen == [f"{hostname}:{port}"]


@pytest.mark.asyncio
async def test_upload_file_from_path_cancelled_mid_multipart_aborts_upload_and_strands_no_uploaders(
    tmp_path: Path,
) -> None:
    from aioboto3.s3 import inject as s3_inject

    big_file = tmp_path / "big.bin"
    with big_file.open("wb") as f:
        f.truncate(9 * 1024 * 1024)  # past aioboto3's 8 MiB multipart threshold; sparse, so instant

    parts_started = asyncio.Event()
    release_parts = asyncio.Event()
    aborted_upload_ids: list[str] = []

    class FakeS3:
        async def create_multipart_upload(self, **kwargs: object) -> dict[str, str]:
            return {"UploadId": "upload-1"}

        async def upload_part(self, **kwargs: object) -> None:
            parts_started.set()
            await release_parts.wait()
            raise RuntimeError("part upload failed")

        async def abort_multipart_upload(self, **kwargs: object) -> None:
            aborted_upload_ids.append(str(kwargs["UploadId"]))

        async def complete_multipart_upload(self, **kwargs: object) -> None:
            raise AssertionError("a failed multipart upload must not be completed")

    fake_s3 = FakeS3()
    fake_s3.upload_file = types.MethodType(s3_inject.upload_file, fake_s3)  # type: ignore[attr-defined]

    @contextlib.asynccontextmanager
    async def fake_s3_client():  # type: ignore[no-untyped-def]
        yield fake_s3

    client = aws.AsyncAWSClient()
    with patch.object(client, "_s3_client", fake_s3_client):
        caller = asyncio.ensure_future(
            client.upload_file_from_path("s3://bucket/key.bin", str(big_file), raise_exception=True)
        )
        await asyncio.wait_for(parts_started.wait(), timeout=5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        release_parts.set()
        async with asyncio.timeout(5):
            while not aborted_upload_ids or len(asyncio.all_tasks()) > 1:
                await asyncio.sleep(0.01)

    assert aborted_upload_ids == ["upload-1"]
    assert not [t for t in asyncio.all_tasks() if "upload_fileobj.<locals>.uploader" in repr(t)]
