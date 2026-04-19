from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

import structlog

LOG = structlog.get_logger()

FFMPEG_BINARY = "ffmpeg"
FFMPEG_REMUX_TIMEOUT_SECONDS = 30


async def finalize_webm(src_path: str) -> bytes:
    """Return the bytes of a WebM recording with a valid end timestamp.

    Browser recordings can end up with an unfinalized Matroska container when
    ``browser_context.close()`` is killed mid-shutdown (close timeout, OOM,
    pod eviction). The file is left with an "unknown size" Segment, no
    ``Duration`` element, and no ``Cues`` index — so players show no end
    timestamp and can't seek. Remuxing via ffmpeg stream-copy rewrites the
    container with a backfilled Segment size, a Duration element, and Cues
    at the front for HTTP streaming. VP8 clusters are copied unmodified.

    Falls back to the raw bytes if ffmpeg is unavailable, times out, or
    errors so recording upload is never regressed relative to today.
    """
    if not os.path.exists(src_path):
        raise FileNotFoundError(src_path)

    if shutil.which(FFMPEG_BINARY) is None:
        LOG.warning("ffmpeg binary not found on PATH, returning raw webm bytes", src=src_path)
        with open(src_path, "rb") as f:
            return f.read()

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as dst_tmp:
        dst_path = dst_tmp.name

    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_BINARY,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            src_path,
            "-c",
            "copy",
            "-cues_to_front",
            "1",
            "-reserve_index_space",
            "200k",
            dst_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_REMUX_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            LOG.warning("ffmpeg remux timed out, returning raw webm bytes", src=src_path)
            with open(src_path, "rb") as f:
                return f.read()

        if proc.returncode != 0 or not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
            LOG.warning(
                "ffmpeg remux failed, returning raw webm bytes",
                src=src_path,
                returncode=proc.returncode,
                stderr=stderr.decode(errors="replace")[:500] if stderr else "",
            )
            with open(src_path, "rb") as f:
                return f.read()

        with open(dst_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(dst_path):
            try:
                os.unlink(dst_path)
            except OSError:
                LOG.debug("failed to cleanup ffmpeg temp output", path=dst_path, exc_info=True)
