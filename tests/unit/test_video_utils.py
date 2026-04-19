from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.webeye import video_utils
from skyvern.webeye.video_utils import finalize_webm


def _write_unfinalized_webm(path: str) -> bytes:
    """Write a WebM whose Segment has the "unknown size" VINT and no Duration/Cues.

    This mirrors the shape of recordings produced when Chromium's muxer is
    killed before finalizing the container.
    """
    ebml_header = bytes.fromhex(
        "1a45dfa3"  # EBML
        "9f"  # size = 31
        "4286"
        "81"
        "01"  # EBMLVersion = 1
        "42f7"
        "81"
        "01"  # EBMLReadVersion = 1
        "42f2"
        "81"
        "04"  # EBMLMaxIDLength = 4
        "42f3"
        "81"
        "08"  # EBMLMaxSizeLength = 8
        "4282"
        "84"
        "7765626d"  # DocType = "webm"
        "4287"
        "81"
        "02"  # DocTypeVersion = 2
        "4285"
        "81"
        "02"  # DocTypeReadVersion = 2
    )
    segment_header = bytes.fromhex("18538067") + bytes.fromhex("01ffffffffffffff")  # "unknown size"
    # One tiny SimpleBlock-bearing Cluster so ffmpeg sees something.
    cluster_body = bytes.fromhex("e78100")  # Timestamp = 0
    cluster = bytes.fromhex("1f43b675") + struct.pack(">B", 0x80 | len(cluster_body)) + cluster_body
    data = ebml_header + segment_header + cluster
    with open(path, "wb") as f:
        f.write(data)
    return data


@pytest.mark.asyncio
async def test_finalize_webm_missing_file_raises(tmp_path) -> None:
    missing = str(tmp_path / "nope.webm")
    with pytest.raises(FileNotFoundError):
        await finalize_webm(missing)


@pytest.mark.asyncio
async def test_finalize_webm_without_ffmpeg_returns_raw(tmp_path) -> None:
    src = str(tmp_path / "raw.webm")
    expected = _write_unfinalized_webm(src)

    with patch.object(video_utils, "shutil") as mock_shutil:
        mock_shutil.which.return_value = None
        result = await finalize_webm(src)

    assert result == expected


@pytest.mark.asyncio
async def test_finalize_webm_invokes_ffmpeg_with_cues_at_front(tmp_path) -> None:
    src = str(tmp_path / "src.webm")
    _write_unfinalized_webm(src)

    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        # ffmpeg "writes" an output file, then we simulate a successful run
        output_path = args[-1]
        with open(output_path, "wb") as f:
            f.write(b"REMUXED_CONTENT")
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        return proc

    with patch.object(video_utils.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(video_utils.asyncio, "create_subprocess_exec", side_effect=fake_exec):
            result = await finalize_webm(src)

    assert result == b"REMUXED_CONTENT"
    assert "-c" in captured_args and "copy" in captured_args
    assert "-cues_to_front" in captured_args
    # cues_to_front must be followed by "1"
    assert captured_args[captured_args.index("-cues_to_front") + 1] == "1"
    assert "-reserve_index_space" in captured_args
    assert captured_args[0] == video_utils.FFMPEG_BINARY
    assert captured_args[-2] != src  # output path is the temp file, not input
    assert src in captured_args


@pytest.mark.asyncio
async def test_finalize_webm_falls_back_on_ffmpeg_error(tmp_path) -> None:
    src = str(tmp_path / "src.webm")
    expected = _write_unfinalized_webm(src)

    async def fake_exec(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"boom"))
        proc.wait = AsyncMock(return_value=1)
        return proc

    with patch.object(video_utils.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(video_utils.asyncio, "create_subprocess_exec", side_effect=fake_exec):
            result = await finalize_webm(src)

    assert result == expected


@pytest.mark.asyncio
async def test_finalize_webm_falls_back_on_timeout(tmp_path) -> None:
    src = str(tmp_path / "src.webm")
    expected = _write_unfinalized_webm(src)

    async def fake_exec(*args, **kwargs):
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = lambda: None
        proc.wait = AsyncMock(return_value=0)
        return proc

    async def immediate_timeout(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    with patch.object(video_utils.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(video_utils.asyncio, "create_subprocess_exec", side_effect=fake_exec):
            with patch.object(video_utils.asyncio, "wait_for", side_effect=immediate_timeout):
                result = await finalize_webm(src)

    assert result == expected


@pytest.mark.asyncio
async def test_finalize_webm_end_to_end_sets_duration(tmp_path) -> None:
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")

    src = str(tmp_path / "real.webm")
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:size=320x180:rate=25:duration=2",
            "-c:v",
            "libvpx",
            "-f",
            "webm",
            src,
        ]
    )

    output = await finalize_webm(src)
    # Duration element tag is 0x4489 — must be present after remux.
    assert b"\x44\x89" in output[:4096]
