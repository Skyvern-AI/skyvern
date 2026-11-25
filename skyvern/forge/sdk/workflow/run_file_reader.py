"""Worker-side byte reads of one of this run's files by name, for a code block's read_run_file."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

import structlog

from skyvern.config import settings
from skyvern.exceptions import DownloadFileMaxSizeExceeded
from skyvern.forge import app
from skyvern.forge.sdk.api.files import (
    download_file,
    make_run_temp_directory,
    resolve_local_or_download_file,
    resolve_run_download_id,
    uploaded_file_id_for_local_uri,
)
from skyvern.forge.sdk.artifact.signing import parse_artifact_content_url
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext

LOG = structlog.get_logger()
_MB = 1024 * 1024


class RunFileReadError(Exception):
    pass


class RunFileReader:
    """One per code block execution: the run's file list is read once and again only for a name it lacks."""

    def __init__(
        self,
        run_context: WorkflowRunContext,
        *,
        workflow_run_id: str,
        organization_id: str,
        max_bytes: int,
        reserved: Iterable[str] = (),
    ) -> None:
        self._run_context = run_context
        self._workflow_run_id = workflow_run_id
        self._organization_id = organization_id
        self._max_bytes = max_bytes
        self._reserved = tuple(reserved)
        self._files: dict[str, FileInfo] | None = None

    async def read(self, name: str) -> bytes:
        files = self._files
        if files is None or name not in files:
            files = self._files = await self._run_context.run_files(reserved=self._reserved)
        file = files.get(name)
        if file is None:
            raise RunFileReadError(f"This run has no file named {name!r}. The run's files are {list(files)}.")
        if file.file_size is not None and file.file_size > self._max_bytes:
            raise RunFileReadError(self._too_big(name, file.file_size))
        staging = make_run_temp_directory("run_file_")
        try:
            path = await self._fetch(file.url, staging)
            size = os.path.getsize(path)
            if size > self._max_bytes:
                raise RunFileReadError(self._too_big(name, size))
            return await asyncio.to_thread(Path(path).read_bytes)
        except DownloadFileMaxSizeExceeded:
            raise RunFileReadError(self._too_big(name, None)) from None
        except RunFileReadError:
            raise
        except Exception as e:
            # The error text can carry the file's signed URL, so only its type reaches the code.
            LOG.warning("read_run_file could not fetch a run file", run_file=name, exc_info=True)
            raise RunFileReadError(f"Could not read the file {name!r} ({type(e).__name__}).") from None
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def _fetch(self, url: str, staging: str) -> str:
        max_size_mb = max(self._max_bytes // _MB, 1)
        if url.startswith("/") or (
            urlparse(url).scheme == "file" and await uploaded_file_id_for_local_uri(url, self._organization_id) is None
        ):
            run_download_id = resolve_run_download_id(skyvern_context.current(), fallback_run_id=self._workflow_run_id)
            return await resolve_local_or_download_file(
                url, run_download_id, organization_id=self._organization_id, max_size_mb=max_size_mb
            )
        if content_url := parse_artifact_content_url(url, settings.SKYVERN_BASE_URL):
            url = await app.ARTIFACT_MANAGER.remint_content_url_if_unverified(content_url, self._organization_id) or url
        # Staged in this read's own folder, since the default managed-file staging is one shared name per file.
        return await download_file(
            url,
            max_size_mb=max_size_mb,
            output_dir=staging,
            organization_id=self._organization_id,
            preserve_existing_files=True,
            staging_dir=staging,
            limit_managed_file_size=True,
            require_managed_file_size=True,
        )

    def _too_big(self, name: str, size: int | None) -> str:
        actual = f"is {size} bytes" if size is not None else "is over the limit or has no known size"
        return f"The file {name!r} {actual}; read_run_file reads files of at most {self._max_bytes} bytes."
