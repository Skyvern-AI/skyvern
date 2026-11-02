import asyncio
import posixpath
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiofiles
import asyncssh
import structlog

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOAD_TIMEOUT
from skyvern.exceptions import BlockedHost, UnresolvableHost
from skyvern.forge.sdk.services.file_download_utils import publish_unique_download
from skyvern.utils.url_validators import resolve_fetch_host_ips

LOG = structlog.get_logger()


class SFTPDownloadErrorCategory(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    HOST_KEY_MISSING = "host_key_missing"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    PATH_NOT_FOUND = "path_not_found"
    FILE_TOO_LARGE = "file_too_large"
    HOST_BLOCKED = "host_blocked"
    HOST_UNREACHABLE = "host_unreachable"
    TRANSFER_FAILED = "transfer_failed"
    REGISTRATION_FAILED = "registration_failed"
    CONFIGURATION = "configuration"


class SFTPDownloadError(Exception):
    def __init__(self, category: SFTPDownloadErrorCategory, bytes_transferred: int = 0):
        self.category = category
        self.bytes_transferred = bytes_transferred
        super().__init__(f"SFTP download failed ({category.value})")


def build_remote_target(remote_path: str | None, filename: str) -> str:
    if remote_path:
        return posixpath.join(remote_path, filename)
    return filename


async def _connection_kwargs(
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    private_key: str | None,
    private_key_passphrase: str | None,
    host_key: str | None,
    require_host_key: bool,
    connect_timeout: int,
) -> dict[str, Any]:
    if require_host_key and (not host_key or not host_key.strip()):
        raise SFTPDownloadError(SFTPDownloadErrorCategory.HOST_KEY_MISSING)

    connect_host = host
    host_key_alias: str | None = None
    if not settings.ALLOW_SFTP_INTERNAL_HOSTS:
        resolved_ips = await asyncio.to_thread(resolve_fetch_host_ips, host)
        connect_host = resolved_ips[0]
        # Keep the configured hostname as the identity used for host-key validation.
        host_key_alias = host

    connect_kwargs: dict[str, Any] = {
        "host": connect_host,
        "port": port,
        "username": username,
        "connect_timeout": connect_timeout,
        # Use only block credentials, never the worker's ambient SSH identity.
        "config": None,
        "agent_path": None,
        "gss_host": None,
        "client_keys": None,
    }
    if host_key_alias:
        connect_kwargs["host_key_alias"] = host_key_alias

    if host_key:
        entry = f"[{host}]:{port}" if port != 22 else host
        known_hosts: bytes | None = f"{entry} {host_key}\n".encode()
    else:
        known_hosts = None
        if not require_host_key:
            LOG.warning("SFTP host key verification disabled; no host_key provided", port=port)
    connect_kwargs["known_hosts"] = known_hosts

    if private_key:
        connect_kwargs["client_keys"] = [
            asyncssh.import_private_key(private_key, passphrase=private_key_passphrase or None)
        ]
    if password:
        connect_kwargs["password"] = password

    return connect_kwargs


async def upload_file(
    *,
    file_path: str,
    host: str,
    port: int,
    username: str,
    remote_path: str | None = None,
    password: str | None = None,
    private_key: str | None = None,
    private_key_passphrase: str | None = None,
    host_key: str | None = None,
    connect_timeout: int = 30,
) -> str:
    connect_kwargs = await _connection_kwargs(
        host=host,
        port=port,
        username=username,
        password=password,
        private_key=private_key,
        private_key_passphrase=private_key_passphrase,
        host_key=host_key,
        require_host_key=False,
        connect_timeout=connect_timeout,
    )

    async with asyncssh.connect(**connect_kwargs) as conn, conn.start_sftp_client() as sftp:
        filename = Path(file_path).name
        if remote_path:
            await sftp.makedirs(remote_path, exist_ok=True)
        remote_target = build_remote_target(remote_path, filename)
        await sftp.put(file_path, remote_target)
        return remote_target


async def download_file(
    *,
    output_dir: str,
    remote_path: str,
    host: str,
    port: int,
    username: str,
    password: str | None = None,
    private_key: str | None = None,
    private_key_passphrase: str | None = None,
    host_key: str | None = None,
    connect_timeout: int = 30,
) -> str:
    if not remote_path.strip() or not host.strip() or not username.strip() or not (password or private_key):
        raise SFTPDownloadError(SFTPDownloadErrorCategory.CONFIGURATION)

    filename = Path(remote_path).name
    if filename in {"", ".", ".."}:
        raise SFTPDownloadError(SFTPDownloadErrorCategory.PATH_NOT_FOUND)

    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    byte_count = 0
    max_size = settings.MAX_HTTP_DOWNLOAD_FILE_SIZE
    deadline = asyncio.timeout(BROWSER_DOWNLOAD_TIMEOUT)

    try:
        with tempfile.NamedTemporaryFile(dir=output_dir_path, prefix=".sftp-", delete=False) as local_file:
            temporary_path = Path(local_file.name)

        async with deadline:
            connect_kwargs = await _connection_kwargs(
                host=host,
                port=port,
                username=username,
                password=password,
                private_key=private_key,
                private_key_passphrase=private_key_passphrase,
                host_key=host_key,
                require_host_key=True,
                connect_timeout=connect_timeout,
            )
            async with asyncssh.connect(**connect_kwargs) as conn, conn.start_sftp_client() as sftp:
                attributes = await sftp.stat(remote_path)
                if attributes.size is not None and attributes.size > max_size:
                    raise SFTPDownloadError(SFTPDownloadErrorCategory.FILE_TOO_LARGE)

                async with (
                    sftp.open(remote_path, "rb") as remote_file,
                    aiofiles.open(temporary_path, "wb") as local_file,
                ):
                    try:
                        while chunk := await remote_file.read(64 * 1024):
                            byte_count += len(chunk)
                            if byte_count > max_size:
                                raise SFTPDownloadError(
                                    SFTPDownloadErrorCategory.FILE_TOO_LARGE,
                                    bytes_transferred=byte_count,
                                )
                            await local_file.write(chunk)
                    except OSError as exc:
                        raise SFTPDownloadError(SFTPDownloadErrorCategory.TRANSFER_FAILED, byte_count) from exc

            if attributes.size is not None and byte_count != attributes.size:
                raise SFTPDownloadError(SFTPDownloadErrorCategory.TRANSFER_FAILED, byte_count)

            try:
                published_path = publish_unique_download(temporary_path, output_dir_path, filename)
            except OSError as exc:
                raise SFTPDownloadError(SFTPDownloadErrorCategory.TRANSFER_FAILED, byte_count) from exc
        return str(published_path)
    except SFTPDownloadError:
        raise
    except UnresolvableHost as exc:
        raise SFTPDownloadError(SFTPDownloadErrorCategory.HOST_UNREACHABLE, byte_count) from exc
    except BlockedHost as exc:
        raise SFTPDownloadError(SFTPDownloadErrorCategory.HOST_BLOCKED, byte_count) from exc
    except asyncssh.PermissionDenied as exc:
        raise SFTPDownloadError(SFTPDownloadErrorCategory.AUTHENTICATION_FAILED, byte_count) from exc
    except asyncssh.HostKeyNotVerifiable as exc:
        raise SFTPDownloadError(SFTPDownloadErrorCategory.HOST_KEY_MISMATCH, byte_count) from exc
    except asyncssh.SFTPNoSuchFile as exc:
        raise SFTPDownloadError(SFTPDownloadErrorCategory.PATH_NOT_FOUND, byte_count) from exc
    except asyncssh.SFTPError as exc:
        raise SFTPDownloadError(SFTPDownloadErrorCategory.TRANSFER_FAILED, byte_count) from exc
    except TimeoutError as exc:
        category = (
            SFTPDownloadErrorCategory.TRANSFER_FAILED
            if deadline.expired()
            else SFTPDownloadErrorCategory.HOST_UNREACHABLE
        )
        raise SFTPDownloadError(category, byte_count) from exc
    except (OSError, asyncssh.Error) as exc:
        raise SFTPDownloadError(SFTPDownloadErrorCategory.HOST_UNREACHABLE, byte_count) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
