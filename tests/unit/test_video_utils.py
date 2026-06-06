from __future__ import annotations

import asyncio
import os
import struct
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.config import settings
from skyvern.webeye import video_utils
from skyvern.webeye.video_utils import finalize_webm, prepare_recording_for_upload


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
async def test_prepare_recording_for_upload_invokes_ffmpeg_with_h264_mp4_compression(tmp_path) -> None:
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
            async with prepare_recording_for_upload(src) as prepared:
                with open(prepared.path, "rb") as f:
                    result = f.read()
                extension = prepared.file_extension

    assert result == b"REMUXED_CONTENT"
    assert extension == "mp4"
    assert "-c:v" in captured_args
    assert captured_args[captured_args.index("-c:v") + 1] == "libx264"
    assert "-preset" in captured_args
    assert captured_args[captured_args.index("-preset") + 1] == settings.VIDEO_COMPRESSION_PRESET
    assert "-crf" in captured_args
    assert captured_args[captured_args.index("-crf") + 1] == str(settings.VIDEO_COMPRESSION_CRF)
    assert "-pix_fmt" in captured_args
    assert captured_args[captured_args.index("-pix_fmt") + 1] == "yuv420p"
    assert "-an" in captured_args
    assert "-movflags" in captured_args
    assert captured_args[captured_args.index("-movflags") + 1] == "+faststart"
    assert captured_args[0] == video_utils.FFMPEG_BINARY
    assert captured_args[-2] != src  # output path is the temp file, not input
    assert captured_args[-1].endswith(".mp4")
    assert src in captured_args


@pytest.mark.asyncio
async def test_prepare_recording_for_upload_can_disable_compression_and_stream_copy_remux(tmp_path) -> None:
    src = str(tmp_path / "src.webm")
    _write_unfinalized_webm(src)

    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        output_path = args[-1]
        with open(output_path, "wb") as f:
            f.write(b"REMUXED_CONTENT")
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        return proc

    with patch.object(settings, "VIDEO_COMPRESSION_ENABLED", False):
        with patch.object(video_utils.shutil, "which", return_value="/usr/bin/ffmpeg"):
            with patch.object(video_utils.asyncio, "create_subprocess_exec", side_effect=fake_exec):
                async with prepare_recording_for_upload(src) as prepared:
                    with open(prepared.path, "rb") as f:
                        result = f.read()
                    extension = prepared.file_extension

    assert result == b"REMUXED_CONTENT"
    assert extension == "webm"
    assert "-c" in captured_args and "copy" in captured_args
    assert "-c:v" not in captured_args


@pytest.mark.asyncio
async def test_prepare_recording_for_upload_yields_temp_path_and_cleans_up(tmp_path) -> None:
    src = str(tmp_path / "src.webm")
    _write_unfinalized_webm(src)

    async def fake_exec(*args, **kwargs):
        output_path = args[-1]
        with open(output_path, "wb") as f:
            f.write(b"COMPRESSED_CONTENT")
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        return proc

    with patch.object(video_utils.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(video_utils.asyncio, "create_subprocess_exec", side_effect=fake_exec):
            async with prepare_recording_for_upload(src) as prepared:
                temp_path = prepared.path
                assert prepared.path != src
                assert prepared.file_extension == "mp4"
                with open(prepared.path, "rb") as f:
                    assert f.read() == b"COMPRESSED_CONTENT"

    assert not os.path.exists(temp_path)


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
async def test_finalize_webm_kills_ffmpeg_on_cancellation(tmp_path) -> None:
    src = str(tmp_path / "src.webm")
    _write_unfinalized_webm(src)

    killed = False
    waited = False
    output_paths: list[str] = []

    class FakeProcess:
        returncode = None

        async def communicate(self):
            return b"", b""

        def kill(self):
            nonlocal killed
            killed = True
            self.returncode = -9

        async def wait(self):
            nonlocal waited
            waited = True
            return self.returncode

    async def fake_exec(*args, **kwargs):
        output_path = args[-1]
        output_paths.append(output_path)
        with open(output_path, "wb") as f:
            f.write(b"PARTIAL_OUTPUT")
        return FakeProcess()

    async def immediate_cancel(coro, timeout):
        coro.close()
        raise asyncio.CancelledError()

    with patch.object(video_utils.shutil, "which", return_value="/usr/bin/ffmpeg"):
        with patch.object(video_utils.asyncio, "create_subprocess_exec", side_effect=fake_exec):
            with patch.object(video_utils.asyncio, "wait_for", side_effect=immediate_cancel):
                with pytest.raises(asyncio.CancelledError):
                    await finalize_webm(src)

    assert killed
    assert waited
    assert output_paths
    assert not any(os.path.exists(output_path) for output_path in output_paths)


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
    # Duration element tag is 0x4489 — must be present after finalization.
    assert b"\x44\x89" in output[:4096]
