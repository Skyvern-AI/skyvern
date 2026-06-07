from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog

from skyvern.config import settings

LOG = structlog.get_logger()

FFMPEG_BINARY = "ffmpeg"
FFMPEG_REMUX_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class PreparedRecordingUpload:
    path: str
    file_extension: str


def _read_file(src_path: str) -> bytes:
    with open(src_path, "rb") as f:
        return f.read()


async def _kill_and_wait_for_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


async def _run_ffmpeg_to_temp(
    src_path: str,
    *,
    suffix: str,
    output_args: list[str],
    timeout_seconds: float,
    operation: str,
) -> str | None:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as dst_tmp:
        dst_path = dst_tmp.name

    keep_output = False
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                FFMPEG_BINARY,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                src_path,
                *output_args,
                dst_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            LOG.warning("ffmpeg subprocess failed to start", operation=operation, src=src_path, exc_info=True)
            return None

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.CancelledError:
            await _kill_and_wait_for_process(proc)
            LOG.warning("ffmpeg subprocess cancelled", operation=operation, src=src_path)
            raise
        except asyncio.TimeoutError:
            await _kill_and_wait_for_process(proc)
            LOG.warning(
                "ffmpeg subprocess timed out", operation=operation, src=src_path, timeout_seconds=timeout_seconds
            )
            return None

        if proc.returncode != 0 or not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
            LOG.warning(
                "ffmpeg subprocess failed",
                operation=operation,
                src=src_path,
                returncode=proc.returncode,
                stderr=stderr.decode(errors="replace")[:500] if stderr else "",
            )
            return None

        keep_output = True
        return dst_path
    finally:
        if not keep_output and os.path.exists(dst_path):
            try:
                os.unlink(dst_path)
            except OSError:
                LOG.debug("failed to cleanup ffmpeg temp output", path=dst_path, exc_info=True)


async def _compress_recording_to_mp4(src_path: str) -> str | None:
    output_args = [
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        settings.VIDEO_COMPRESSION_PRESET,
        "-crf",
        str(settings.VIDEO_COMPRESSION_CRF),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
    ]
    dst_path = await _run_ffmpeg_to_temp(
        src_path,
        suffix=".mp4",
        output_args=output_args,
        timeout_seconds=settings.VIDEO_COMPRESSION_TIMEOUT_SECONDS,
        operation="ffmpeg recording mp4 compression",
    )
    if dst_path is None:
        return None

    try:
        src_size = os.path.getsize(src_path)
        dst_size = os.path.getsize(dst_path)
        LOG.info(
            "Compressed recording to mp4",
            src=src_path,
            original_size_bytes=src_size,
            compressed_size_bytes=dst_size,
            compression_ratio=round(dst_size / src_size, 4) if src_size > 0 else None,
        )
    except OSError:
        LOG.debug("failed to calculate recording compression ratio", src=src_path, dst=dst_path, exc_info=True)
    return dst_path


async def _remux_webm(src_path: str) -> str | None:
    output_args = [
        "-c",
        "copy",
        "-cues_to_front",
        "1",
        "-reserve_index_space",
        "200k",
    ]
    return await _run_ffmpeg_to_temp(
        src_path,
        suffix=".webm",
        output_args=output_args,
        timeout_seconds=FFMPEG_REMUX_TIMEOUT_SECONDS,
        operation="ffmpeg webm remux",
    )


async def _prepare_recording_upload(src_path: str) -> PreparedRecordingUpload | None:
    if shutil.which(FFMPEG_BINARY) is None:
        LOG.warning("ffmpeg binary not found on PATH, returning raw recording", src=src_path)
        return None

    if settings.VIDEO_COMPRESSION_ENABLED:
        compressed_path = await _compress_recording_to_mp4(src_path)
        if compressed_path is not None:
            return PreparedRecordingUpload(path=compressed_path, file_extension="mp4")

        LOG.warning("ffmpeg mp4 compression failed, falling back to stream-copy webm remux", src=src_path)

    remuxed_path = await _remux_webm(src_path)
    if remuxed_path is None:
        LOG.warning("ffmpeg webm remux failed, returning raw webm", src=src_path)
        return None
    return PreparedRecordingUpload(path=remuxed_path, file_extension="webm")


@asynccontextmanager
async def prepare_recording_for_upload(src_path: str) -> AsyncIterator[PreparedRecordingUpload]:
    """Yield the best available recording path for upload."""
    if not os.path.exists(src_path):
        raise FileNotFoundError(src_path)

    prepared = await _prepare_recording_upload(src_path)
    source_extension = os.path.splitext(src_path)[1].lstrip(".").lower() or "webm"
    upload = prepared or PreparedRecordingUpload(path=src_path, file_extension=source_extension)
    try:
        yield upload
    finally:
        if prepared and os.path.exists(prepared.path):
            try:
                os.unlink(prepared.path)
            except OSError:
                LOG.debug("failed to cleanup prepared recording temp output", path=prepared.path, exc_info=True)


@asynccontextmanager
async def prepare_webm_for_upload(src_path: str) -> AsyncIterator[str]:
    """Yield a finalized WebM path for compatibility with existing callers."""
    if not os.path.exists(src_path):
        raise FileNotFoundError(src_path)

    remuxed_path = None
    if shutil.which(FFMPEG_BINARY) is not None:
        remuxed_path = await _remux_webm(src_path)
    try:
        yield remuxed_path or src_path
    finally:
        if remuxed_path and os.path.exists(remuxed_path):
            try:
                os.unlink(remuxed_path)
            except OSError:
                LOG.debug("failed to cleanup prepared webm temp output", path=remuxed_path, exc_info=True)


async def finalize_webm(src_path: str) -> bytes:
    """Return finalized, upload-ready WebM recording bytes.

    Browser recordings can end up with an unfinalized Matroska container when
    ``browser_context.close()`` is killed mid-shutdown (close timeout, OOM,
    pod eviction). The file is left with an "unknown size" Segment, no
    ``Duration`` element, and no ``Cues`` index — so players show no end
    timestamp and can't seek.

    This path intentionally stays WebM because some artifact rows are allocated
    before the final bytes are available. Browser-session S3 uploads use
    ``prepare_recording_for_upload`` and can switch to MP4 dynamically.
    """
    async with prepare_webm_for_upload(src_path) as upload_path:
        return await asyncio.to_thread(_read_file, upload_path)
