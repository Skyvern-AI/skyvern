from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from skyvern.constants import BROWSER_CELL_DOWNLOAD_ARTIFACT_ID_PREFIX, BROWSER_CELL_DOWNLOAD_URI_SEGMENT

if TYPE_CHECKING:
    from skyvern.forge.sdk.artifact.models import Artifact

_CELL_DOWNLOAD_URI = re.compile(
    rf"s3://[^/]+/v1/[^/]+/[^/]+/browser_sessions/[^/]+{BROWSER_CELL_DOWNLOAD_URI_SEGMENT}"
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/[^/]+"
)
_CELL_DOWNLOAD_ID = re.compile(rf"{BROWSER_CELL_DOWNLOAD_ARTIFACT_ID_PREFIX}[0-9]+")
CELL_DOWNLOAD_COPIES = ".cell-downloads"


def is_browser_cell_download_uri(uri: str) -> bool:
    return _CELL_DOWNLOAD_URI.fullmatch(uri) is not None


def is_browser_cell_download_artifact(artifact_id: str, uri: str) -> bool:
    return _CELL_DOWNLOAD_ID.fullmatch(artifact_id) is not None and is_browser_cell_download_uri(uri)


def browser_cell_download_artifact_id(uri: str) -> str:
    # Generated numeric artifact IDs have no leading zero.
    return f"{BROWSER_CELL_DOWNLOAD_ARTIFACT_ID_PREFIX}{uuid.uuid5(uuid.NAMESPACE_URL, uri).int}"


def is_cell_download_copy(file: str) -> bool:
    folder = Path(file).parent
    return folder.parent.name == CELL_DOWNLOAD_COPIES and _CELL_DOWNLOAD_ID.fullmatch(folder.name) is not None


def _get_cell_download_copy(run_dir: Path, artifact_id: str) -> Path | None:
    if not _CELL_DOWNLOAD_ID.fullmatch(artifact_id):
        return None
    folder = run_dir / CELL_DOWNLOAD_COPIES / artifact_id
    if not folder.is_dir() or not folder.resolve().is_relative_to(run_dir.resolve()):
        return None
    return next((path for path in folder.iterdir() if path.is_file() and not path.is_symlink()), None)


async def load_verified_cell_download(
    run_dir: Path, artifact: Artifact, retrieve: Callable[[Artifact], Awaitable[bytes | None]]
) -> tuple[Path | None, bytes] | None:
    """Read a scoped registered download. The path is present only for a verified local copy."""
    size = artifact.file_size
    checksum = artifact.checksum
    if size is None or size < 0 or not checksum:
        return None

    def matches(data: bytes) -> bool:
        return len(data) == size and hashlib.sha256(data).hexdigest() == checksum

    def read_local() -> tuple[Path, bytes] | None:
        try:
            copy = _get_cell_download_copy(run_dir, artifact.artifact_id)
            if copy is not None:
                with copy.open("rb") as file:
                    data = file.read(size + 1)
                if matches(data):
                    return copy, data
        except OSError:
            pass
        return None

    if (local := await asyncio.to_thread(read_local)) is not None:
        return local
    data = await retrieve(artifact)
    return (None, data) if data is not None and matches(data) else None


def write_cell_download_copy(run_dir: Path, artifact_id: str, filename: str, data: bytes) -> Path:
    """Publish a private consumer copy identified by the already registered artifact."""
    if (
        not _CELL_DOWNLOAD_ID.fullmatch(artifact_id)
        or filename in {"", ".", ".."}
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError("invalid cell download copy")
    folder = run_dir / CELL_DOWNLOAD_COPIES / artifact_id
    if not folder.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("cell download copy escaped its run directory")
    existing = _get_cell_download_copy(run_dir, artifact_id)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    file = folder / filename
    with tempfile.NamedTemporaryFile(dir=folder.parent, delete=False) as staging:
        temporary = Path(staging.name)
        try:
            staging.write(data)
            staging.close()
            temporary.replace(file)
        finally:
            temporary.unlink(missing_ok=True)
    if existing is not None and existing != file:
        existing.unlink(missing_ok=True)
    return file


def replace_file_extension(path: str, extension: str) -> str:
    normalized_extension = extension.lstrip(".")
    if not normalized_extension:
        return path
    stem, _ = os.path.splitext(path)
    return f"{stem}.{normalized_extension}"
