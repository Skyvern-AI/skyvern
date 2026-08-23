from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from skyvern.browser_extension.errors import BrowserExtensionBrokerError

GRANT_VERSION = 1
GRANT_FILENAME = "browser_extension_workstation_grant.json"
_GRANT_SOURCES = frozenset({"pairing", "cli"})
_MAX_GRANT_BYTES = 64 * 1024
_MIN_GRANT_EPOCH = 0
_MAX_GRANT_EPOCH = 4_102_444_800  # 2100-01-01 UTC
_TOKEN_BINDING_LENGTH = 64
_TOKEN_BINDING_HEXDIGITS = frozenset("0123456789abcdef")

LOG = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkstationGrant:
    version: int
    granted_at: float
    source: str
    token_binding: str

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "granted_at": self.granted_at,
            "source": self.source,
            "token_binding": self.token_binding,
        }


def workstation_grant_path(settings_dir: Path | None = None) -> Path:
    """Return the owner-only global grant path for this OS user.

    The path is intentionally shared by all broker instances for the user. This
    design assumes one active broker per user.
    """
    directory = Path.home() / ".skyvern" if settings_dir is None else settings_dir
    return directory / GRANT_FILENAME


def token_binding(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_granted_at(value: object) -> bool:
    if type(value) is int:
        return _MIN_GRANT_EPOCH <= value <= _MAX_GRANT_EPOCH
    if type(value) is float:
        return math.isfinite(value) and _MIN_GRANT_EPOCH <= value <= _MAX_GRANT_EPOCH
    return False


def _valid_token_binding(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _TOKEN_BINDING_LENGTH
        and all(character in _TOKEN_BINDING_HEXDIGITS for character in value)
    )


def validate_workstation_grant(grant: WorkstationGrant, token: str) -> bool:
    if not isinstance(token, str) or not token:
        return False
    return (
        type(grant.version) is int
        and grant.version == GRANT_VERSION
        and isinstance(grant.source, str)
        and grant.source in _GRANT_SOURCES
        and _valid_granted_at(grant.granted_at)
        and _valid_token_binding(grant.token_binding)
        and hmac.compare_digest(grant.token_binding, token_binding(token))
    )


def load_workstation_grant(path: Path, token: str) -> WorkstationGrant | None:
    """Read and validate a grant, rewriting its mode to 0600 when needed."""
    try:
        payload = _read_json(path)
        if set(payload) != {"version", "granted_at", "source", "token_binding"}:
            return None
        version = payload["version"]
        granted_at = payload["granted_at"]
        source = payload["source"]
        binding = payload["token_binding"]
        if (
            type(version) is not int
            or not _valid_granted_at(granted_at)
            or not isinstance(source, str)
            or not _valid_token_binding(binding)
        ):
            return None
        grant = WorkstationGrant(version, float(granted_at), source, binding)
        return grant if validate_workstation_grant(grant, token) else None
    except FileNotFoundError:
        return None
    except Exception:
        LOG.warning("browser_extension_workstation_grant_invalid", path=str(path), exc_info=True)
        return None


def write_workstation_grant(
    path: Path,
    token: str,
    *,
    source: str,
    granted_at: float | None = None,
) -> WorkstationGrant:
    if source not in _GRANT_SOURCES:
        raise BrowserExtensionBrokerError("INVALID_REQUEST", "Workstation grant source is invalid")
    if not token:
        raise BrowserExtensionBrokerError("BROKER_NOT_ENABLED", "Broker authentication token is unavailable")
    timestamp = time.time() if granted_at is None else granted_at
    if not _valid_granted_at(timestamp):
        raise BrowserExtensionBrokerError("INVALID_REQUEST", "Workstation grant timestamp is invalid")
    grant = WorkstationGrant(GRANT_VERSION, float(timestamp), source, token_binding(token))
    _atomic_write_json(path, grant.to_json())
    return grant


def remove_workstation_grant(path: Path) -> bool:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return False
    _validate_file_stat(path, file_stat)
    path.unlink()
    _fsync_directory(path.parent)
    return True


def _read_json(path: Path) -> dict[str, Any]:
    path_stat = path.lstat()
    _validate_file_stat(path, path_stat)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        _validate_file_stat(path, file_stat)
        current_path_stat = path.lstat()
        _validate_file_stat(path, current_path_stat)
        if (file_stat.st_dev, file_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino) or (
            file_stat.st_dev,
            file_stat.st_ino,
        ) != (current_path_stat.st_dev, current_path_stat.st_ino):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Workstation grant changed during validation")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_GRANT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_GRANT_BYTES:
                raise BrowserExtensionBrokerError("UNSAFE_STATE", "Workstation grant is too large")
    finally:
        os.close(fd)
    value = json.loads(b"".join(chunks))
    if not isinstance(value, dict):
        raise BrowserExtensionBrokerError("UNSAFE_STATE", "Workstation grant is invalid")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    directory_stat = _validate_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        _validate_publication_target(path)
        current_stat = _validate_directory(path.parent)
        if (directory_stat.st_dev, directory_stat.st_ino) != (current_stat.st_dev, current_stat.st_ino):
            raise BrowserExtensionBrokerError("UNSAFE_PATH", "Skyvern settings directory changed during publication")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _validate_publication_target(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    _validate_file_stat(path, file_stat)


def _validate_file_stat(path: Path, file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode) or path.is_symlink():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Workstation grant must be a regular file")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Workstation grant has the wrong owner")


def _validate_directory(path: Path) -> os.stat_result:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_stat = path.lstat()
    if not stat.S_ISDIR(directory_stat.st_mode) or path.is_symlink():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Skyvern settings directory is unsafe")
    if hasattr(os, "getuid") and directory_stat.st_uid != os.getuid():
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Skyvern settings directory has the wrong owner")
    if stat.S_IMODE(directory_stat.st_mode) & 0o022:
        raise BrowserExtensionBrokerError("UNSAFE_PATH", "Skyvern settings directory is writable by another user")
    return directory_stat


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
