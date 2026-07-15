from unittest.mock import AsyncMock, MagicMock, patch

import libcst as cst
import pytest

from skyvern.core.script_generations.generate_script import _build_download_statement, _build_file_upload_statement
from skyvern.exceptions import BlockedHost
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
