import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import libcst as cst
import pytest

from skyvern.core.script_generations.generate_script import _build_download_statement, _build_file_upload_statement
from skyvern.exceptions import BlockedHost, UnresolvableHost
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.services import sftp_service
from skyvern.schemas.workflows import FileStorageType, FileUploadDestination


def _acm(enter_value):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=enter_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _write_file(tmp_path, name: str = "f.csv") -> str:
    file_path = tmp_path / name
    file_path.write_text("data")
    return str(file_path)


def _mock_sftp_asyncssh(mock_asyncssh):
    sftp = AsyncMock()
    conn = MagicMock()
    conn.start_sftp_client = MagicMock(return_value=_acm(sftp))
    mock_asyncssh.connect = MagicMock(return_value=_acm(conn))
    mock_asyncssh.import_private_key = MagicMock(return_value="KEYOBJ")
    return sftp


def _sftp_destination(**overrides) -> FileUploadDestination:
    fields = {
        "storage_type": FileStorageType.SFTP,
        "customer_uri": "sftp://h:22/incoming/f.csv",
        "sdk_uri": "sftp://h:22/incoming/f.csv",
        "sftp_host": "h",
        "sftp_port": 22,
        "sftp_username": "u",
        "sftp_password": "pw",
        "sftp_remote_path": "/incoming",
    }
    fields.update(overrides)
    return FileUploadDestination(**fields)


@asynccontextmanager
async def _sftp_test_server(root: Path, password: str = "pw"):
    class Server(asyncssh.SSHServer):
        def begin_auth(self, username: str) -> bool:
            return True

        def password_auth_supported(self) -> bool:
            return True

        def validate_password(self, username: str, supplied_password: str) -> bool:
            return supplied_password == password

    server_key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(
        Server,
        "127.0.0.1",
        0,
        server_host_keys=[server_key],
        sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(root)),
    )
    try:
        yield server, server_key
    finally:
        server.close()
        await server.wait_closed()


def _sftp_public_key(key) -> str:
    return key.export_public_key().decode().strip()


async def _download_from_server(server, host_key: str | None, **overrides) -> str:
    args = {
        "host": "sftp.test",
        "port": server.get_port(),
        "username": "user",
        "password": "pw",
        "remote_path": "/report.csv",
        "output_dir": ".",
        "host_key": host_key,
    }
    args.update(overrides)
    with patch.object(sftp_service, "resolve_fetch_host_ips", return_value=("127.0.0.1",)):
        return await sftp_service.download_file(**args)


@pytest.mark.asyncio
async def test_sftp_service_download_file_reads_and_pins_host_key(tmp_path, monkeypatch):
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    (remote_root / "report.csv").write_bytes(b"id,value\n1,ok\n")
    output_dir = tmp_path / "downloads"
    monkeypatch.setattr(sftp_service.settings, "ALLOW_SFTP_INTERNAL_HOSTS", False)

    async with _sftp_test_server(remote_root) as (server, server_key):
        path = await _download_from_server(
            server,
            _sftp_public_key(server_key),
            output_dir=str(output_dir),
        )

    assert Path(path).read_bytes() == b"id,value\n1,ok\n"
    assert Path(path).name == "report.csv"


@pytest.mark.asyncio
async def test_sftp_service_download_file_preserves_same_basename_reads(tmp_path, monkeypatch):
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    remote_file = remote_root / "report.csv"
    remote_file.write_bytes(b"first report")
    output_dir = tmp_path / "downloads"
    monkeypatch.setattr(sftp_service.settings, "ALLOW_SFTP_INTERNAL_HOSTS", False)

    async with _sftp_test_server(remote_root) as (server, server_key):
        first_path = await _download_from_server(
            server,
            _sftp_public_key(server_key),
            output_dir=str(output_dir),
        )
        remote_file.write_bytes(b"second report")
        second_path = await _download_from_server(
            server,
            _sftp_public_key(server_key),
            output_dir=str(output_dir),
        )

    assert first_path != second_path
    assert Path(first_path).name == "report.csv"
    assert Path(second_path).name == "report (1).csv"
    assert Path(first_path).read_bytes() == b"first report"
    assert Path(second_path).read_bytes() == b"second report"


@pytest.mark.asyncio
async def test_sftp_service_download_file_enforces_overall_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(sftp_service, "BROWSER_DOWNLOAD_TIMEOUT", 0.05)
    monkeypatch.setattr(sftp_service.settings, "ALLOW_SFTP_INTERNAL_HOSTS", True)

    async def slow_read(_size: int) -> bytes:
        await asyncio.sleep(1)
        return b"report"

    remote_file = MagicMock()
    remote_file.read = AsyncMock(side_effect=slow_read)
    sftp = MagicMock()
    sftp.stat = AsyncMock(return_value=SimpleNamespace(size=6))
    sftp.open = MagicMock(return_value=_acm(remote_file))
    conn = MagicMock()
    conn.start_sftp_client = MagicMock(return_value=_acm(sftp))

    with (
        patch.object(sftp_service.asyncssh, "connect", return_value=_acm(conn)),
        pytest.raises(sftp_service.SFTPDownloadError) as error,
    ):
        await sftp_service.download_file(
            output_dir=str(tmp_path / "downloads"),
            remote_path="/report.csv",
            host="sftp.test",
            port=22,
            username="user",
            password="pw",
            host_key="ssh-ed25519 AAAA",
        )

    assert error.value.category == sftp_service.SFTPDownloadErrorCategory.TRANSFER_FAILED
    assert not list((tmp_path / "downloads").glob("report*"))
    assert not list((tmp_path / "downloads").glob(".sftp-*"))


@pytest.mark.asyncio
async def test_sftp_service_download_file_requires_a_pinned_host_key(tmp_path):
    async with _sftp_test_server(tmp_path) as (server, _server_key):
        with pytest.raises(sftp_service.SFTPDownloadError) as error:
            await _download_from_server(server, None, output_dir=str(tmp_path / "downloads"))

    assert error.value.category == sftp_service.SFTPDownloadErrorCategory.HOST_KEY_MISSING


@pytest.mark.asyncio
async def test_sftp_service_download_file_rejects_mismatched_host_key(tmp_path):
    async with _sftp_test_server(tmp_path) as (server, _server_key):
        mismatched_key = asyncssh.generate_private_key("ssh-ed25519")
        with pytest.raises(sftp_service.SFTPDownloadError) as error:
            await _download_from_server(
                server,
                _sftp_public_key(mismatched_key),
                output_dir=str(tmp_path / "downloads"),
            )

    assert error.value.category == sftp_service.SFTPDownloadErrorCategory.HOST_KEY_MISMATCH


@pytest.mark.asyncio
async def test_sftp_service_download_file_classifies_authentication_failure(tmp_path):
    async with _sftp_test_server(tmp_path) as (server, server_key):
        with pytest.raises(sftp_service.SFTPDownloadError) as error:
            await _download_from_server(
                server,
                _sftp_public_key(server_key),
                password="wrong",
                output_dir=str(tmp_path / "downloads"),
            )

    assert error.value.category == sftp_service.SFTPDownloadErrorCategory.AUTHENTICATION_FAILED


@pytest.mark.asyncio
async def test_sftp_service_download_file_classifies_missing_remote_path(tmp_path):
    async with _sftp_test_server(tmp_path) as (server, server_key):
        with pytest.raises(sftp_service.SFTPDownloadError) as error:
            await _download_from_server(
                server,
                _sftp_public_key(server_key),
                output_dir=str(tmp_path / "downloads"),
            )

    assert error.value.category == sftp_service.SFTPDownloadErrorCategory.PATH_NOT_FOUND


@pytest.mark.asyncio
async def test_sftp_service_download_file_enforces_existing_size_cap(tmp_path, monkeypatch):
    (tmp_path / "report.csv").write_bytes(b"larger than cap")
    monkeypatch.setattr(sftp_service.settings, "MAX_HTTP_DOWNLOAD_FILE_SIZE", 3)

    async with _sftp_test_server(tmp_path) as (server, server_key):
        with pytest.raises(sftp_service.SFTPDownloadError) as error:
            await _download_from_server(
                server,
                _sftp_public_key(server_key),
                output_dir=str(tmp_path / "downloads"),
            )

    assert error.value.category == sftp_service.SFTPDownloadErrorCategory.FILE_TOO_LARGE
    assert not (tmp_path / "downloads" / "report.csv").exists()


@pytest.mark.asyncio
async def test_sftp_service_download_file_rejects_truncated_transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(sftp_service.settings, "ALLOW_SFTP_INTERNAL_HOSTS", True)
    remote_file = MagicMock()
    remote_file.read = AsyncMock(side_effect=[b"short", b""])
    sftp = MagicMock()
    sftp.stat = AsyncMock(return_value=SimpleNamespace(size=100))
    sftp.open = MagicMock(return_value=_acm(remote_file))
    conn = MagicMock()
    conn.start_sftp_client = MagicMock(return_value=_acm(sftp))
    output_dir = tmp_path / "downloads"

    with (
        patch.object(sftp_service.asyncssh, "connect", return_value=_acm(conn)),
        pytest.raises(sftp_service.SFTPDownloadError) as error,
    ):
        await sftp_service.download_file(
            output_dir=str(output_dir),
            remote_path="/report.csv",
            host="sftp.test",
            port=22,
            username="user",
            password="pw",
            host_key="ssh-ed25519 AAAA",
        )

    assert error.value.category == sftp_service.SFTPDownloadErrorCategory.TRANSFER_FAILED
    assert not list(output_dir.glob("report*"))
    assert not list(output_dir.glob(".sftp-*"))


@pytest.mark.asyncio
async def test_sftp_service_download_file_classifies_blocked_host(tmp_path):
    with (
        patch.object(sftp_service, "resolve_fetch_host_ips", side_effect=BlockedHost(host="127.0.0.1")),
        pytest.raises(sftp_service.SFTPDownloadError) as error,
    ):
        await sftp_service.download_file(
            host="sftp.test",
            port=22,
            username="user",
            password="pw",
            remote_path="/report.csv",
            output_dir=str(tmp_path / "downloads"),
            host_key="ssh-ed25519 AAAA",
        )

    assert error.value.category == sftp_service.SFTPDownloadErrorCategory.HOST_BLOCKED


@pytest.mark.asyncio
async def test_sftp_service_download_file_classifies_unresolvable_host(tmp_path):
    with (
        patch.object(sftp_service, "resolve_fetch_host_ips", side_effect=UnresolvableHost(host="sftp.test")),
        pytest.raises(sftp_service.SFTPDownloadError) as error,
    ):
        await sftp_service.download_file(
            host="sftp.test",
            port=22,
            username="user",
            password="pw",
            remote_path="/report.csv",
            output_dir=str(tmp_path / "downloads"),
            host_key="ssh-ed25519 AAAA",
        )

    assert error.value.category == sftp_service.SFTPDownloadErrorCategory.HOST_UNREACHABLE


@pytest.mark.asyncio
async def test_sftp_service_upload_still_allows_missing_host_key(tmp_path, monkeypatch):
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    file_path = _write_file(tmp_path)
    monkeypatch.setattr(sftp_service.settings, "ALLOW_SFTP_INTERNAL_HOSTS", False)

    async with _sftp_test_server(remote_root) as (server, _server_key):
        with patch.object(sftp_service, "resolve_fetch_host_ips", return_value=("127.0.0.1",)):
            result = await sftp_service.upload_file(
                file_path=file_path,
                host="sftp.test",
                port=server.get_port(),
                username="user",
                password="pw",
                remote_path="/incoming",
            )

    assert result == "/incoming/f.csv"
    assert (remote_root / "incoming" / "f.csv").read_text() == "data"


@pytest.mark.asyncio
async def test_sftp_service_upload_file_uses_password_auth_and_remote_path(tmp_path):
    file_path = _write_file(tmp_path)

    with (
        patch("skyvern.forge.sdk.services.sftp_service.asyncssh") as mock_asyncssh,
        patch("skyvern.forge.sdk.services.sftp_service.resolve_fetch_host_ips", return_value=("1.2.3.4",)),
    ):
        sftp = _mock_sftp_asyncssh(mock_asyncssh)

        result = await sftp_service.upload_file(
            file_path=file_path,
            host="h",
            port=22,
            username="u",
            password="pw",
            remote_path="/incoming",
        )

    mock_asyncssh.connect.assert_called_once()
    connect_kwargs = mock_asyncssh.connect.call_args.kwargs
    # Connect to the already-validated address so DNS rebinding cannot change
    # the destination between SSRF validation and socket creation.
    assert connect_kwargs["host"] == "1.2.3.4"
    assert connect_kwargs["host_key_alias"] == "h"
    assert connect_kwargs["port"] == 22
    assert connect_kwargs["username"] == "u"
    assert connect_kwargs["password"] == "pw"
    assert connect_kwargs["known_hosts"] is None
    assert connect_kwargs["config"] is None
    assert connect_kwargs["agent_path"] is None
    assert connect_kwargs["gss_host"] is None
    assert connect_kwargs["client_keys"] is None
    sftp.makedirs.assert_awaited_once_with("/incoming", exist_ok=True)
    sftp.put.assert_awaited_once_with(file_path, "/incoming/f.csv")
    assert result == "/incoming/f.csv"
    mock_asyncssh.import_private_key.assert_not_called()


@pytest.mark.asyncio
async def test_sftp_service_upload_file_uses_private_key_auth_without_remote_path(tmp_path):
    file_path = _write_file(tmp_path)

    with (
        patch("skyvern.forge.sdk.services.sftp_service.asyncssh") as mock_asyncssh,
        patch("skyvern.forge.sdk.services.sftp_service.resolve_fetch_host_ips", return_value=("1.2.3.4",)),
    ):
        sftp = _mock_sftp_asyncssh(mock_asyncssh)

        result = await sftp_service.upload_file(
            file_path=file_path,
            host="h",
            port=22,
            username="u",
            private_key="PEM",
            private_key_passphrase="secret",
            remote_path=None,
        )

    mock_asyncssh.import_private_key.assert_called_once_with("PEM", passphrase="secret")
    connect_kwargs = mock_asyncssh.connect.call_args.kwargs
    assert connect_kwargs["client_keys"] == ["KEYOBJ"]
    assert connect_kwargs["config"] is None
    assert connect_kwargs["agent_path"] is None
    assert connect_kwargs["gss_host"] is None
    assert "password" not in connect_kwargs
    sftp.makedirs.assert_not_awaited()
    sftp.put.assert_awaited_once_with(file_path, "f.csv")
    assert result == "f.csv"


@pytest.mark.asyncio
async def test_sftp_service_upload_file_pins_host_key_on_non_default_port(tmp_path):
    file_path = _write_file(tmp_path)

    with (
        patch("skyvern.forge.sdk.services.sftp_service.asyncssh") as mock_asyncssh,
        patch("skyvern.forge.sdk.services.sftp_service.resolve_fetch_host_ips", return_value=("1.2.3.4",)),
    ):
        _mock_sftp_asyncssh(mock_asyncssh)

        await sftp_service.upload_file(
            file_path=file_path,
            host="h",
            port=2222,
            username="u",
            password="pw",
            host_key="ssh-ed25519 AAAA",
        )

    assert mock_asyncssh.connect.call_args.kwargs["known_hosts"] == b"[h]:2222 ssh-ed25519 AAAA\n"


@pytest.mark.asyncio
async def test_sftp_service_upload_file_pins_host_key_on_default_port(tmp_path):
    file_path = _write_file(tmp_path)

    with (
        patch("skyvern.forge.sdk.services.sftp_service.asyncssh") as mock_asyncssh,
        patch("skyvern.forge.sdk.services.sftp_service.resolve_fetch_host_ips", return_value=("1.2.3.4",)),
    ):
        _mock_sftp_asyncssh(mock_asyncssh)

        await sftp_service.upload_file(
            file_path=file_path,
            host="h",
            port=22,
            username="u",
            password="pw",
            host_key="ssh-ed25519 AAAA",
        )

    assert mock_asyncssh.connect.call_args.kwargs["known_hosts"] == b"h ssh-ed25519 AAAA\n"


@pytest.mark.asyncio
async def test_sftp_service_upload_file_blocks_internal_host(tmp_path):
    file_path = _write_file(tmp_path)

    with (
        patch("skyvern.forge.sdk.services.sftp_service.asyncssh") as mock_asyncssh,
        patch(
            "skyvern.forge.sdk.services.sftp_service.resolve_fetch_host_ips",
            side_effect=BlockedHost(host="169.254.169.254"),
        ),
        pytest.raises(BlockedHost),
    ):
        await sftp_service.upload_file(
            file_path=file_path,
            host="169.254.169.254",
            port=22,
            username="u",
            password="pw",
        )

    mock_asyncssh.connect.assert_not_called()


@pytest.mark.asyncio
async def test_sftp_service_upload_file_skips_guard_when_internal_hosts_allowed(tmp_path, monkeypatch):
    file_path = _write_file(tmp_path)
    monkeypatch.setattr(sftp_service.settings, "ALLOW_SFTP_INTERNAL_HOSTS", True)

    with (
        patch("skyvern.forge.sdk.services.sftp_service.asyncssh") as mock_asyncssh,
        patch("skyvern.forge.sdk.services.sftp_service.resolve_fetch_host_ips") as mock_resolve_fetch_host_ips,
    ):
        _mock_sftp_asyncssh(mock_asyncssh)

        await sftp_service.upload_file(
            file_path=file_path,
            host="h",
            port=22,
            username="u",
            password="pw",
        )

    mock_resolve_fetch_host_ips.assert_not_called()
    mock_asyncssh.connect.assert_called_once()


@pytest.mark.asyncio
async def test_agent_function_upload_file_to_customer_storage_sftp_happy_path(tmp_path):
    file_path = _write_file(tmp_path)
    destination = _sftp_destination()

    with patch("skyvern.forge.agent_functions.sftp_service.upload_file", new_callable=AsyncMock) as mock_upload:
        result = await AgentFunction().upload_file_to_customer_storage(file_path, destination)

    assert result == destination.customer_uri
    mock_upload.assert_awaited_once_with(
        file_path=file_path,
        host="h",
        port=22,
        username="u",
        remote_path="/incoming",
        password="pw",
        private_key=None,
        private_key_passphrase=None,
        host_key=None,
    )


@pytest.mark.asyncio
async def test_agent_function_upload_file_to_customer_storage_sftp_requires_host(tmp_path):
    file_path = _write_file(tmp_path)
    destination = _sftp_destination(sftp_host=None, sftp_username="u", sftp_password="pw")

    with pytest.raises(ValueError):
        await AgentFunction().upload_file_to_customer_storage(file_path, destination)


@pytest.mark.asyncio
async def test_agent_function_upload_file_to_customer_storage_sftp_requires_auth(tmp_path):
    file_path = _write_file(tmp_path)
    destination = _sftp_destination(sftp_host="h", sftp_username="u", sftp_password=None, sftp_private_key=None)

    with pytest.raises(ValueError):
        await AgentFunction().upload_file_to_customer_storage(file_path, destination)


def test_generated_script_emits_sftp_fields():
    block = {
        "label": "sftp_upload",
        "parameters": [],
        "storage_type": "sftp",
        "sftp_host": "h",
        "sftp_port": 2222,
        "sftp_username": "u",
        "sftp_password": "pw",
        "sftp_private_key": "PEM",
        "sftp_private_key_passphrase": "secret",
        "sftp_remote_path": "/incoming",
        "sftp_host_key": "ssh-ed25519 AAAA",
    }

    compact = cst.Module(body=[_build_file_upload_statement(block)]).code.replace(" ", "").replace("\n", "")

    assert "storage_type='sftp'" in compact
    for field in (
        "sftp_host",
        "sftp_port",
        "sftp_username",
        "sftp_password",
        "sftp_private_key",
        "sftp_private_key_passphrase",
        "sftp_remote_path",
        "sftp_host_key",
    ):
        assert f"{field}=" in compact
    assert "sftp_port=2222" in compact


@pytest.mark.parametrize(
    ("download_target", "destination_fields"),
    [
        (
            "sftp",
            {
                "sftp_host": "h",
                "sftp_port": 2222,
                "sftp_username": "u",
                "sftp_password": "pw",
                "sftp_private_key": "PEM",
                "sftp_private_key_passphrase": "secret",
                "sftp_remote_path": "/incoming",
                "sftp_host_key": "ssh-ed25519 AAAA",
            },
        ),
        (
            "s3",
            {
                "s3_bucket": "bucket",
                "aws_access_key_id": "access-key",
                "aws_secret_access_key": "secret-key",
                "region_name": "us-east-1",
            },
        ),
        (
            "azure",
            {
                "azure_storage_account_name": "account",
                "azure_storage_account_key": "account-key",
                "azure_blob_container_name": "container",
                "path": "reports",
            },
        ),
        (
            "google_drive",
            {
                "google_credential_id": "credential-id",
                "google_drive_folder_id": "folder-id",
            },
        ),
    ],
)
def test_generated_download_script_emits_destination_fields(download_target, destination_fields):
    block = {
        "block_type": "file_download",
        "label": "download",
        "navigation_goal": "Download the file",
        "download_target": download_target,
        **destination_fields,
    }

    compact = cst.Module(body=[_build_download_statement("download", block)]).code.replace(" ", "").replace("\n", "")

    assert f"download_target='{download_target}'" in compact
    for field, value in destination_fields.items():
        assert f"{field}={value!r}".replace(" ", "") in compact


def test_generated_website_download_with_stale_prompt_keeps_required_navigation_prompt():
    block = {
        "block_type": "file_download",
        "label": "download",
        "navigation_goal": "Download the file",
        "download_target": "website",
        "s3_bucket": "stale-bucket",
        "aws_access_key_id": "stale-key",
        "aws_secret_access_key": "stale-secret",
        "prompt": "stale selection prompt",
        "path": "stale-path",
        "continue_on_empty": False,
    }

    compact = cst.Module(body=[_build_download_statement("download", block)]).code.replace(" ", "").replace("\n", "")

    assert "prompt='Downloadthefile'" in compact
    assert "navigation_goal=" not in compact
    assert "download_target=" not in compact
    assert "s3_bucket=" not in compact
    assert "aws_access_key_id=" not in compact
    assert "aws_secret_access_key=" not in compact
    assert "path=" not in compact
    assert "continue_on_empty=" not in compact


def test_generated_website_sftp_source_emits_source_and_shared_credentials():
    block = {
        "block_type": "file_download",
        "label": "download",
        "navigation_goal": "Download the file",
        "download_target": "website",
        "sftp_source_path": "/reports/report.csv",
        "sftp_host": "sftp.example.test",
        "sftp_port": 2222,
        "sftp_username": "user",
        "sftp_password": "password",
        "sftp_private_key": "private-key",
        "sftp_private_key_passphrase": "passphrase",
        "sftp_host_key": "ssh-ed25519 AAAA",
        "sftp_remote_path": "/stale-destination",
    }

    compact = cst.Module(body=[_build_download_statement("download", block)]).code.replace(" ", "").replace("\n", "")

    assert "download_target=" not in compact
    assert "sftp_source_path='/reports/report.csv'" in compact
    assert "sftp_host='sftp.example.test'" in compact
    assert "sftp_port=2222" in compact
    assert "sftp_username='user'" in compact
    assert "sftp_password='password'" in compact
    assert "sftp_private_key='private-key'" in compact
    assert "sftp_private_key_passphrase='passphrase'" in compact
    assert "sftp_host_key='ssh-ed25519AAAA'" in compact
    assert "sftp_remote_path=" not in compact


def test_generated_external_download_omits_other_targets_stale_secrets():
    block = {
        "block_type": "file_download",
        "label": "download",
        "navigation_goal": "Download the file",
        "download_target": "s3",
        "s3_bucket": "bucket",
        "aws_access_key_id": "access-key",
        "aws_secret_access_key": "secret-key",
        "sftp_password": "stale-sftp-password",
        "azure_storage_account_key": "stale-azure-key",
    }

    compact = cst.Module(body=[_build_download_statement("download", block)]).code.replace(" ", "").replace("\n", "")

    assert "download_target='s3'" in compact
    assert "s3_bucket='bucket'" in compact
    assert "sftp_password=" not in compact
    assert "azure_storage_account_key=" not in compact


def test_generated_script_omits_absent_sftp_fields():
    block = {
        "label": "sftp_upload",
        "parameters": [],
        "storage_type": "sftp",
        "sftp_host": "h",
        "sftp_username": "u",
        "sftp_password": "pw",
    }

    compact = cst.Module(body=[_build_file_upload_statement(block)]).code.replace(" ", "").replace("\n", "")

    assert "sftp_host=" in compact
    assert "sftp_private_key=" not in compact
    assert "s3_bucket=" not in compact
