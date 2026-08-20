import asyncio
import posixpath
from pathlib import Path
from typing import Any

import asyncssh
import structlog

from skyvern.config import settings
from skyvern.utils.url_validators import resolve_fetch_host_ips

LOG = structlog.get_logger()


def build_remote_target(remote_path: str | None, filename: str) -> str:
    if remote_path:
        return posixpath.join(remote_path, filename)
    return filename


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
    connect_host = host
    host_key_alias: str | None = None
    if not settings.ALLOW_SFTP_INTERNAL_HOSTS:
        resolved_ips = await asyncio.to_thread(resolve_fetch_host_ips, host)
        connect_host = resolved_ips[0]
        # Connect directly to the validated address to prevent a second DNS
        # lookup from rebinding the hostname to an internal target. Keep the
        # configured hostname as the identity used for host-key validation.
        host_key_alias = host

    connect_kwargs: dict[str, Any] = {
        "host": connect_host,
        "port": port,
        "username": username,
        "connect_timeout": connect_timeout,
        # Authenticate only with the credentials configured on the block, never the
        # worker's ambient SSH identity (ssh config, agent, default keys, or GSSAPI).
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
        LOG.warning("SFTP host key verification disabled; no host_key provided", host=host, port=port)
    connect_kwargs["known_hosts"] = known_hosts

    if private_key:
        connect_kwargs["client_keys"] = [
            asyncssh.import_private_key(private_key, passphrase=private_key_passphrase or None)
        ]
    if password:
        connect_kwargs["password"] = password

    async with asyncssh.connect(**connect_kwargs) as conn:
        async with conn.start_sftp_client() as sftp:
            filename = Path(file_path).name
            if remote_path:
                await sftp.makedirs(remote_path, exist_ok=True)
            remote_target = build_remote_target(remote_path, filename)
            await sftp.put(file_path, remote_target)
            return remote_target
