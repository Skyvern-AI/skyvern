from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import litellm
import pytest
from PIL import Image

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot import video_attachment as video_attachment_module
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.video_attachment import (
    MAX_VIDEO_ATTACHMENT_DURATION_SECONDS,
    VideoAttachmentArtifact,
    VideoAttachmentEvidence,
    VideoAttachmentFrame,
    build_video_attachment_message,
    create_video_evidence_artifacts,
    is_video_attachment,
    load_video_attachment_evidence,
    screen_video_frames_for_copilot,
    select_video_keyframes,
)
from skyvern.forge.sdk.schemas.workflow_copilot import (
    CopilotAttachedFile,
    CopilotVideoEvidenceArtifact,
    CopilotVideoObservation,
    WorkflowCopilotChatRequest,
)
from tests.unit.copilot_test_helpers import stub_copilot_agent_loop


def _perception_result(description: str = "The user opens Settings.") -> dict[str, Any]:
    return {"observations": [{"frame_index": 1, "description": description, "confidence": "high"}]}


def _jpeg(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="JPEG")
    return output.getvalue()


@pytest.mark.parametrize("filename", ["demo.mp4", "DEMO.MP4", "demo.webm", "demo.mov"])
def test_supported_video_attachment_extensions_are_case_insensitive(filename: str) -> None:
    assert is_video_attachment(filename)


@pytest.mark.parametrize("filename", ["demo.avi", "demo.mkv", "demo.mp4.txt", "demo"])
def test_other_attachment_extensions_are_not_treated_as_video(filename: str) -> None:
    assert not is_video_attachment(filename)


def test_local_keyframe_selection_collapses_idle_video_but_keeps_visual_changes() -> None:
    blue = _jpeg("blue")
    red = _jpeg("red")
    frames = [blue] * 600
    frames[241] = red
    frames[242] = red

    selected = select_video_keyframes(frames, frames_per_second=2)

    timestamps = [timestamp for timestamp, _ in selected]
    old_uniform_samples = [frames[int((index + 0.5) * len(frames) / 16)] for index in range(16)]
    assert len(selected) < 40
    assert red not in old_uniform_samples
    assert 120.5 in timestamps
    assert timestamps[-1] == 299.5


def test_keyframe_selection_uses_every_available_slice_near_the_frame_limit() -> None:
    frames = [_jpeg("red" if index % 2 else "blue") for index in range(121)]

    selected = select_video_keyframes(frames, frames_per_second=2, max_frames=120)

    assert len(selected) == 120
    assert selected[-1][0] == 60.0


@pytest.mark.asyncio
async def test_five_minute_video_is_sampled_densely_in_one_bulk_decode(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    video_path = tmp_path / "five-minutes.mp4"
    video_path.write_bytes(b"video")
    candidate_frames = tuple(f"frame-{index}".encode() for index in range(600))
    extract = AsyncMock(return_value=candidate_frames)
    monkeypatch.setattr(video_attachment_module, "download_file", AsyncMock(return_value=str(video_path)))
    monkeypatch.setattr(
        video_attachment_module,
        "probe_media_duration_seconds",
        AsyncMock(return_value=MAX_VIDEO_ATTACHMENT_DURATION_SECONDS),
    )
    monkeypatch.setattr(video_attachment_module, "extract_video_frames_jpeg", extract)
    monkeypatch.setattr(
        video_attachment_module,
        "select_video_keyframes",
        lambda frames, **_kwargs: tuple((index / 2, frame) for index, frame in enumerate(frames[::5])),
    )

    evidence = await load_video_attachment_evidence(
        [CopilotAttachedFile(file_id="file_5m", filename="five-minutes.mp4")],
        organization_id="org-1",
    )

    extract.assert_awaited_once_with(
        str(video_path),
        frames_per_second=2,
        max_duration_seconds=MAX_VIDEO_ATTACHMENT_DURATION_SECONDS,
        input_format="mov",
    )
    assert len(evidence.frames) == 120
    assert evidence.duration_seconds_by_file_id == (("file_5m", 300.0),)


@pytest.mark.asyncio
async def test_multiple_videos_share_one_turn_wide_frame_budget(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    paths = {}
    for file_id in ("file_1", "file_2"):
        path = tmp_path / f"{file_id}.mp4"
        path.write_bytes(b"video")
        paths[file_id] = str(path)

    async def download(file_id: str, *, organization_id: str) -> str:
        assert organization_id == "org-1"
        return paths[file_id]

    monkeypatch.setattr(video_attachment_module, "download_file", download)
    monkeypatch.setattr(video_attachment_module, "probe_media_duration_seconds", AsyncMock(return_value=300.0))
    monkeypatch.setattr(
        video_attachment_module,
        "extract_video_frames_jpeg",
        AsyncMock(return_value=tuple(f"frame-{index}".encode() for index in range(600))),
    )
    monkeypatch.setattr(
        video_attachment_module,
        "select_video_keyframes",
        lambda frames, *, max_frames, **_kwargs: tuple(
            (index / 2, frame) for index, frame in enumerate(frames[:max_frames])
        ),
    )

    evidence = await load_video_attachment_evidence(
        [
            CopilotAttachedFile(file_id="file_1", filename="one.mp4"),
            CopilotAttachedFile(file_id="file_2", filename="two.mp4"),
        ],
        organization_id="org-1",
    )

    assert len(evidence.frames) == 120
    assert sum(frame.file_id == "file_1" for frame in evidence.frames) == 60
    assert sum(frame.file_id == "file_2" for frame in evidence.frames) == 60


@pytest.mark.asyncio
async def test_short_video_leaves_its_unused_frame_budget_for_a_long_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    paths = {}
    for file_id in ("file_short", "file_long"):
        path = tmp_path / f"{file_id}.mp4"
        path.write_bytes(b"video")
        paths[file_id] = str(path)

    async def download(file_id: str, *, organization_id: str) -> str:
        assert organization_id == "org-1"
        return paths[file_id]

    monkeypatch.setattr(video_attachment_module, "download_file", download)
    monkeypatch.setattr(video_attachment_module, "probe_media_duration_seconds", AsyncMock(return_value=300.0))
    monkeypatch.setattr(
        video_attachment_module,
        "extract_video_frames_jpeg",
        AsyncMock(
            side_effect=[
                (b"short-0", b"short-1"),
                tuple(f"long-{index}".encode() for index in range(600)),
            ]
        ),
    )
    monkeypatch.setattr(
        video_attachment_module,
        "select_video_keyframes",
        lambda frames, *, max_frames, **_kwargs: tuple(
            (index / 2, frame) for index, frame in enumerate(frames[:max_frames])
        ),
    )

    evidence = await load_video_attachment_evidence(
        [
            CopilotAttachedFile(file_id="file_short", filename="short.mp4"),
            CopilotAttachedFile(file_id="file_long", filename="long.mp4"),
        ],
        organization_id="org-1",
    )

    assert len(evidence.frames) == 120
    assert sum(frame.file_id == "file_short" for frame in evidence.frames) == 2
    assert sum(frame.file_id == "file_long" for frame in evidence.frames) == 118


@pytest.mark.asyncio
async def test_rejected_trailing_video_does_not_reduce_a_valid_videos_frame_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    video_path = tmp_path / "valid.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(video_attachment_module, "download_file", AsyncMock(return_value=str(video_path)))
    monkeypatch.setattr(video_attachment_module, "probe_media_duration_seconds", AsyncMock(return_value=300.0))
    monkeypatch.setattr(
        video_attachment_module,
        "extract_video_frames_jpeg",
        AsyncMock(return_value=tuple(f"frame-{index}".encode() for index in range(600))),
    )
    monkeypatch.setattr(
        video_attachment_module,
        "select_video_keyframes",
        lambda frames, *, max_frames, **_kwargs: tuple(
            (index / 2, frame) for index, frame in enumerate(frames[:max_frames])
        ),
    )

    evidence = await load_video_attachment_evidence(
        [
            CopilotAttachedFile(file_id="file_valid", filename="valid.mp4"),
            CopilotAttachedFile(file_id="file_missing", filename="missing.mp4", available=False),
        ],
        organization_id="org-1",
    )

    assert len(evidence.frames) == 120
    assert {frame.file_id for frame in evidence.frames} == {"file_valid"}
    assert evidence.unavailable_filenames == ("missing.mp4",)


@pytest.mark.asyncio
async def test_keyframe_scoring_does_not_block_the_event_loop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"video")
    selection_started = threading.Event()
    release_selection = threading.Event()

    def blocking_selection(frames, **_kwargs):
        selection_started.set()
        release_selection.wait(timeout=1)
        return ((0.0, frames[0]),)

    monkeypatch.setattr(video_attachment_module, "download_file", AsyncMock(return_value=str(video_path)))
    monkeypatch.setattr(video_attachment_module, "probe_media_duration_seconds", AsyncMock(return_value=1.0))
    monkeypatch.setattr(video_attachment_module, "extract_video_frames_jpeg", AsyncMock(return_value=(b"frame",)))
    monkeypatch.setattr(video_attachment_module, "select_video_keyframes", blocking_selection)

    started_at = time.monotonic()
    load_task = asyncio.create_task(
        load_video_attachment_evidence(
            [CopilotAttachedFile(file_id="file_1", filename="demo.mp4")],
            organization_id="org-1",
        )
    )
    await asyncio.to_thread(selection_started.wait, 1)

    assert time.monotonic() - started_at < 0.5
    release_selection.set()
    evidence = await load_task
    assert len(evidence.frames) == 1


@pytest.mark.asyncio
async def test_video_over_five_minutes_is_not_decoded(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    video_path = tmp_path / "too-long.mp4"
    video_path.write_bytes(b"video")
    extract = AsyncMock()
    monkeypatch.setattr(video_attachment_module, "download_file", AsyncMock(return_value=str(video_path)))
    monkeypatch.setattr(
        video_attachment_module,
        "probe_media_duration_seconds",
        AsyncMock(return_value=MAX_VIDEO_ATTACHMENT_DURATION_SECONDS + 0.001),
    )
    monkeypatch.setattr(video_attachment_module, "extract_video_frames_jpeg", extract)

    evidence = await load_video_attachment_evidence(
        [CopilotAttachedFile(file_id="file_long", filename="too-long.mp4")],
        organization_id="org-1",
    )

    extract.assert_not_awaited()
    assert evidence.frames == ()
    assert evidence.too_long_filenames == ("too-long.mp4",)
    assert evidence.too_long_file_ids == ("file_long",)


@pytest.mark.asyncio
async def test_cheap_perception_turns_frames_into_a_compact_timeline() -> None:
    handler = AsyncMock(
        return_value={
            "observations": [
                {"frame_index": 1, "description": "The user opens the settings menu.", "confidence": "high"},
                {"frame_index": 2, "description": "A billing form is visible.", "confidence": "medium"},
                {"frame_index": 2, "description": "duplicate frame", "confidence": "medium"},
                {"frame_index": 9, "description": "frame outside the chunk", "confidence": "medium"},
                {"frame_index": 3, "description": "x" * 501, "confidence": "high"},
                {"frame_index": 4, "description": "bad confidence", "confidence": "certain"},
            ]
        }
    )
    evidence = VideoAttachmentEvidence(
        frames=(
            VideoAttachmentFrame("file_1", "demo.mp4", 4.0, base64.b64encode(b"one").decode()),
            VideoAttachmentFrame("file_1", "demo.mp4", 9.5, base64.b64encode(b"two").decode()),
            VideoAttachmentFrame("file_1", "demo.mp4", 10.0, base64.b64encode(b"three").decode()),
            VideoAttachmentFrame("file_1", "demo.mp4", 11.0, base64.b64encode(b"four").decode()),
        ),
        duration_seconds_by_file_id=(("file_1", 12.0),),
    )

    artifacts, unavailable_ids = await create_video_evidence_artifacts(
        evidence,
        handler=handler,
        organization_id="org-1",
    )

    assert unavailable_ids == frozenset()
    assert artifacts["file_1"].duration_seconds == 12.0
    assert artifacts["file_1"].sampled_frame_count == 4
    assert [observation.timestamp_seconds for observation in artifacts["file_1"].observations] == [4.0, 9.5]
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["screenshots"] == [b"one", b"two", b"three", b"four"]
    assert "observations, not workflow steps" in handler.await_args.kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_video_perception_has_one_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video_attachment_module, "VIDEO_PERCEPTION_TIMEOUT_SECONDS", 0.05)

    async def slow_perception(**_kwargs):
        await asyncio.sleep(0.035)
        return _perception_result()

    evidence = VideoAttachmentEvidence(
        frames=tuple(
            VideoAttachmentFrame("file_1", "demo.mp4", index / 2, base64.b64encode(b"frame").decode())
            for index in range(13)
        ),
        duration_seconds_by_file_id=(("file_1", 6.5),),
    )

    artifacts, unavailable_ids = await create_video_evidence_artifacts(
        evidence,
        handler=slow_perception,
        organization_id="org-1",
    )

    assert artifacts == {}
    assert unavailable_ids == frozenset({"file_1"})


def test_video_message_uses_persisted_timeline_without_replaying_images() -> None:
    artifact = CopilotVideoEvidenceArtifact(
        version="1",
        duration_seconds=300.0,
        sampled_frame_count=84,
        observations=(
            CopilotVideoObservation(timestamp_seconds=4.0, description="The user opens Settings.", confidence="high"),
            CopilotVideoObservation(timestamp_seconds=9.5, description="The user selects Billing.", confidence="high"),
        ),
    )
    message = build_video_attachment_message(
        VideoAttachmentEvidence(
            artifacts=(VideoAttachmentArtifact("file_1", "demo.mp4", artifact),),
        )
    )

    assert message is not None
    assert [part["type"] for part in message["content"]] == ["input_text"]
    text = message["content"][0]["text"]
    assert "4.000s" in text and "9.500s" in text
    assert "84 locally selected frames" in text
    assert "input_image" not in json.dumps(message)


def test_video_message_redacts_a_secret_shaped_filename() -> None:
    artifact = CopilotVideoEvidenceArtifact(
        version="1",
        duration_seconds=1.0,
        sampled_frame_count=1,
        observations=(
            CopilotVideoObservation(
                timestamp_seconds=0.0, description="A settings page is visible.", confidence="high"
            ),
        ),
    )
    secret_filename = "AbcdefghijklMNOP1234567890.mp4"

    message = build_video_attachment_message(
        VideoAttachmentEvidence(artifacts=(VideoAttachmentArtifact("file_1", secret_filename, artifact),))
    )

    assert message is not None
    text = message["content"][0]["text"]
    assert secret_filename not in text
    assert "[REDACTED_SECRET].mp4" in text


def test_video_message_caps_observations_across_cached_and_new_timelines() -> None:
    def artifact(prefix: str) -> CopilotVideoEvidenceArtifact:
        return CopilotVideoEvidenceArtifact(
            version="1",
            duration_seconds=60.0,
            sampled_frame_count=60,
            observations=tuple(
                CopilotVideoObservation(
                    timestamp_seconds=float(index),
                    description=f"{prefix}-{index}",
                    confidence="high",
                )
                for index in range(60)
            ),
        )

    message = build_video_attachment_message(
        VideoAttachmentEvidence(
            artifacts=(
                VideoAttachmentArtifact("file_new", "new.mp4", artifact("new")),
                VideoAttachmentArtifact("file_old", "old.mp4", artifact("old")),
            )
        )
    )

    assert message is not None
    text = message["content"][0]["text"]
    assert text.count("[high confidence]") == 80
    assert "new-20" in text
    assert "new-59" in text
    assert "new-19" not in text
    assert "old-20" in text
    assert "old-59" in text
    assert "old-19" not in text
    assert "40 older video observation(s) were omitted" in text


def test_video_message_explicitly_omits_oldest_timelines_beyond_the_prompt_budget() -> None:
    artifacts = tuple(
        VideoAttachmentArtifact(
            f"file_{index}",
            f"video-{index}.mp4",
            CopilotVideoEvidenceArtifact(
                version="1",
                duration_seconds=1.0,
                sampled_frame_count=1,
                observations=(
                    CopilotVideoObservation(
                        timestamp_seconds=0.0,
                        description=f"observation-{index}",
                        confidence="high",
                    ),
                ),
            ),
        )
        for index in range(81)
    )

    message = build_video_attachment_message(VideoAttachmentEvidence(artifacts=artifacts))

    assert message is not None
    text = message["content"][0]["text"]
    assert text.count("[high confidence]") == 80
    assert "observation-0" in text
    assert "observation-79" in text
    assert "observation-80" not in text
    assert "1 older attached video timeline was omitted" in text


@pytest.mark.asyncio
async def test_video_evidence_uses_org_scoped_downloads_and_removes_temporary_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"video")
    download = AsyncMock(return_value=str(video_path))
    extract = AsyncMock(return_value=(b"frame-0", b"frame-1", b"frame-2", b"frame-3"))
    monkeypatch.setattr("skyvern.forge.sdk.copilot.video_attachment.download_file", download)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.video_attachment.probe_media_duration_seconds",
        AsyncMock(return_value=2.0),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.video_attachment.extract_video_frames_jpeg", extract)
    monkeypatch.setattr(
        video_attachment_module,
        "select_video_keyframes",
        lambda frames, **_kwargs: tuple((index / 2 + 0.25, frame) for index, frame in enumerate(frames)),
    )

    evidence = await load_video_attachment_evidence(
        [CopilotAttachedFile(file_id="file_1", filename="demo.mp4")],
        organization_id="org-1",
    )

    download.assert_awaited_once_with("file_1", organization_id="org-1")
    assert [frame.timestamp_seconds for frame in evidence.frames] == [0.25, 0.75, 1.25, 1.75]
    assert [base64.b64decode(frame.jpeg_base64) for frame in evidence.frames] == [
        b"frame-0",
        b"frame-1",
        b"frame-2",
        b"frame-3",
    ]
    assert evidence.unavailable_filenames == ()
    assert not video_path.exists()


@pytest.mark.asyncio
async def test_unreadable_video_is_nonblocking_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    video_path = tmp_path / "broken.webm"
    video_path.write_bytes(b"not a video")
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.video_attachment.download_file",
        AsyncMock(return_value=str(video_path)),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.video_attachment.probe_media_duration_seconds",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(video_attachment_module, "probe_video_packet_duration_seconds", AsyncMock(return_value=None))

    evidence = await load_video_attachment_evidence(
        [CopilotAttachedFile(file_id="file_2", filename="broken.webm")],
        organization_id="org-1",
    )
    message = build_video_attachment_message(evidence)

    assert evidence.frames == ()
    assert evidence.unavailable_filenames == ("broken.webm",)
    assert message is not None
    assert "could not be inspected" in message["content"][0]["text"]


@pytest.mark.asyncio
async def test_oversize_video_is_not_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    download = AsyncMock()
    monkeypatch.setattr(video_attachment_module, "download_file", download)

    evidence = await load_video_attachment_evidence(
        [
            CopilotAttachedFile(
                file_id="file_large",
                filename="large.mp4",
                size_bytes=30 * 1024 * 1024 + 1,
            )
        ],
        organization_id="org-1",
    )

    download.assert_not_awaited()
    assert evidence.frames == ()
    assert evidence.unavailable_filenames == ("large.mp4",)


@pytest.mark.asyncio
async def test_durationless_video_uses_packet_timeline_for_ordered_samples(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    video_path = tmp_path / "durationless.webm"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(
        video_attachment_module,
        "download_file",
        AsyncMock(return_value=str(video_path)),
    )
    monkeypatch.setattr(video_attachment_module, "probe_media_duration_seconds", AsyncMock(return_value=None))
    packet_duration = AsyncMock(return_value=4.0)
    monkeypatch.setattr(video_attachment_module, "probe_video_packet_duration_seconds", packet_duration)
    extract = AsyncMock(return_value=tuple(b"frame" for _ in range(8)))
    monkeypatch.setattr(video_attachment_module, "extract_video_frames_jpeg", extract)
    monkeypatch.setattr(
        video_attachment_module,
        "select_video_keyframes",
        lambda frames, **_kwargs: tuple((index / 2 + 0.25, frame) for index, frame in enumerate(frames)),
    )

    evidence = await load_video_attachment_evidence(
        [CopilotAttachedFile(file_id="file_3", filename="durationless.webm")],
        organization_id="org-1",
    )

    packet_duration.assert_awaited_once_with(str(video_path), input_format="matroska")
    assert [frame.timestamp_seconds for frame in evidence.frames] == [
        0.25,
        0.75,
        1.25,
        1.75,
        2.25,
        2.75,
        3.25,
        3.75,
    ]


@pytest.mark.asyncio
async def test_video_preprocessing_has_one_nonblocking_wall_time_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    video_path = tmp_path / "slow.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(video_attachment_module, "VIDEO_ATTACHMENT_PROCESSING_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        video_attachment_module,
        "download_file",
        AsyncMock(return_value=str(video_path)),
    )
    monkeypatch.setattr(video_attachment_module, "probe_media_duration_seconds", AsyncMock(return_value=8.0))

    async def never_finishes(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(video_attachment_module, "extract_video_frames_jpeg", never_finishes)

    evidence = await load_video_attachment_evidence(
        [CopilotAttachedFile(file_id="file_4", filename="slow.mp4")],
        organization_id="org-1",
    )

    assert evidence.frames == ()
    assert evidence.unavailable_filenames == ("slow.mp4",)
    assert not video_path.exists()


@pytest.mark.asyncio
async def test_timeout_does_not_also_mark_an_overlength_video_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    long_path = tmp_path / "long.mp4"
    slow_path = tmp_path / "slow.mp4"
    long_path.write_bytes(b"video")
    slow_path.write_bytes(b"video")
    monkeypatch.setattr(video_attachment_module, "VIDEO_ATTACHMENT_PROCESSING_TIMEOUT_SECONDS", 0.01)

    async def download(file_id: str, *, organization_id: str) -> str:
        assert organization_id == "org-1"
        return str(long_path if file_id == "file_long" else slow_path)

    async def never_finishes(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(video_attachment_module, "download_file", download)
    monkeypatch.setattr(
        video_attachment_module,
        "probe_media_duration_seconds",
        AsyncMock(side_effect=[MAX_VIDEO_ATTACHMENT_DURATION_SECONDS + 1, 10.0]),
    )
    monkeypatch.setattr(video_attachment_module, "extract_video_frames_jpeg", never_finishes)

    evidence = await load_video_attachment_evidence(
        [
            CopilotAttachedFile(file_id="file_long", filename="long.mp4"),
            CopilotAttachedFile(file_id="file_slow", filename="slow.mp4"),
        ],
        organization_id="org-1",
    )

    assert evidence.too_long_filenames == ("long.mp4",)
    assert evidence.unavailable_filenames == ("slow.mp4",)


@pytest.mark.asyncio
async def test_video_preprocessing_discards_a_bulk_decode_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    video_path = tmp_path / "partial.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(video_attachment_module, "download_file", AsyncMock(return_value=str(video_path)))
    monkeypatch.setattr(video_attachment_module, "probe_media_duration_seconds", AsyncMock(return_value=2.0))
    monkeypatch.setattr(
        video_attachment_module,
        "extract_video_frames_jpeg",
        AsyncMock(side_effect=RuntimeError("decode failed")),
    )

    evidence = await load_video_attachment_evidence(
        [CopilotAttachedFile(file_id="file_partial", filename="partial.mp4")],
        organization_id="org-1",
    )

    assert evidence.frames == ()
    assert evidence.unavailable_filenames == ("partial.mp4",)
    assert not video_path.exists()


@pytest.mark.asyncio
async def test_video_preprocessing_treats_an_empty_bulk_decode_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    video_path = tmp_path / "partial.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.setattr(video_attachment_module, "download_file", AsyncMock(return_value=str(video_path)))
    monkeypatch.setattr(video_attachment_module, "probe_media_duration_seconds", AsyncMock(return_value=1.0))
    monkeypatch.setattr(video_attachment_module, "extract_video_frames_jpeg", AsyncMock(return_value=()))

    evidence = await load_video_attachment_evidence(
        [CopilotAttachedFile(file_id="file_partial", filename="partial.mp4")],
        organization_id="org-1",
    )

    assert evidence.frames == ()
    assert evidence.unavailable_filenames == ("partial.mp4",)
    assert not video_path.exists()


def test_video_message_preserves_observation_order_and_labels_timeline_as_untrusted() -> None:
    artifact = CopilotVideoEvidenceArtifact(
        version="1",
        duration_seconds=2.0,
        sampled_frame_count=2,
        observations=(
            CopilotVideoObservation(timestamp_seconds=0.5, description="First state", confidence="high"),
            CopilotVideoObservation(timestamp_seconds=1.5, description="Second state", confidence="medium"),
        ),
    )
    evidence = VideoAttachmentEvidence(artifacts=(VideoAttachmentArtifact("file_1", "demo.mp4", artifact),))

    message = build_video_attachment_message(evidence)

    assert message is not None
    content = message["content"]
    assert content[0]["type"] == "input_text"
    assert "timestamped" in content[0]["text"]
    assert "untrusted evidence" in content[0]["text"]
    assert "credentials" in content[0]["text"]
    assert [part["type"] for part in content] == ["input_text"]
    assert content[0]["text"].index("0.500s") < content[0]["text"].index("1.500s")


@pytest.mark.asyncio
async def test_video_frames_use_the_raw_secret_safety_boundary_before_copilot() -> None:
    handler = AsyncMock(return_value={"unsafe_file_ids": []})
    evidence = VideoAttachmentEvidence(
        frames=(VideoAttachmentFrame("file_1", "demo.mp4", 0.5, base64.b64encode(b"frame").decode()),),
        duration_seconds_by_file_id=(("file_1", 1.0),),
    )

    verdict = await screen_video_frames_for_copilot(evidence, handler=handler, organization_id="org-1")

    assert verdict.withheld_file_ids == frozenset()
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["screenshots"] == [b"frame"]
    assert handler.await_args.kwargs["organization_id"] == "org-1"
    assert "never transcribe" in handler.await_args.kwargs["system_prompt"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_result", [{"unsafe_file_ids": ["unknown"]}, {"unsafe_file_ids": [{}]}, {"state": "clean"}, None]
)
async def test_video_safety_screen_fails_closed(raw_result: object) -> None:
    handler = AsyncMock(return_value=raw_result) if raw_result is not None else None
    evidence = VideoAttachmentEvidence(
        frames=(VideoAttachmentFrame("file_1", "demo.mp4", 0.5, base64.b64encode(b"frame").decode()),)
    )

    verdict = await screen_video_frames_for_copilot(evidence, handler=handler, organization_id="org-1")

    assert verdict.unsafe_file_ids == frozenset()
    assert verdict.undecidable_file_ids == frozenset({"file_1"})


@pytest.mark.asyncio
async def test_video_safety_screen_decides_each_file_separately() -> None:
    async def handler(**kwargs: Any) -> object:
        if b"old" in kwargs["screenshots"]:
            return {"unsafe_file_ids": ["file_old"]}
        if b"flaky" in kwargs["screenshots"]:
            raise RuntimeError("rate limited")
        return {"unsafe_file_ids": []}

    evidence = VideoAttachmentEvidence(
        frames=(
            VideoAttachmentFrame("file_old", "old.mp4", 0.5, base64.b64encode(b"old").decode()),
            VideoAttachmentFrame("file_new", "new.mp4", 0.5, base64.b64encode(b"new").decode()),
            VideoAttachmentFrame("file_flaky", "flaky.mp4", 0.5, base64.b64encode(b"flaky").decode()),
        )
    )

    verdict = await screen_video_frames_for_copilot(evidence, handler=handler, organization_id="org-1")

    assert verdict.unsafe_file_ids == frozenset({"file_old"})
    assert verdict.undecidable_file_ids == frozenset({"file_flaky"})
    assert "file_new" not in verdict.withheld_file_ids


@pytest.mark.asyncio
async def test_video_safety_screen_chunks_the_five_minute_frame_set() -> None:
    handler = AsyncMock(side_effect=[{"unsafe_file_ids": []}, {"unsafe_file_ids": []}])
    evidence = VideoAttachmentEvidence(
        frames=tuple(
            VideoAttachmentFrame("file_1", "demo.mp4", index / 2, base64.b64encode(b"frame").decode())
            for index in range(13)
        )
    )

    verdict = await screen_video_frames_for_copilot(evidence, handler=handler, organization_id="org-1")
    assert verdict.withheld_file_ids == frozenset()
    assert handler.await_count == 2
    assert len(handler.await_args_list[0].kwargs["screenshots"]) == 12
    assert len(handler.await_args_list[1].kwargs["screenshots"]) == 1


@pytest.mark.asyncio
async def test_video_safety_chunks_share_one_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video_attachment_module.settings, "COPILOT_RAW_SECRET_SAFETY_TIMEOUT_SECONDS", 0.01)

    async def slow_screen(**_kwargs):
        await asyncio.sleep(60)
        return {"unsafe_file_ids": []}

    evidence = VideoAttachmentEvidence(
        frames=tuple(
            VideoAttachmentFrame("file_1", "demo.mp4", index / 2, base64.b64encode(b"frame").decode())
            for index in range(13)
        )
    )

    verdict = await screen_video_frames_for_copilot(evidence, handler=slow_screen, organization_id="org-1")
    assert verdict.undecidable_file_ids == frozenset({"file_1"})


def test_withheld_video_message_contains_no_frames_or_secret_values() -> None:
    message = build_video_attachment_message(VideoAttachmentEvidence(withheld_filenames=("demo.mp4",)))

    assert message is not None
    assert [part["type"] for part in message["content"]] == ["input_text"]
    assert "withheld" in message["content"][0]["text"].lower()
    assert "non-sensitive" in message["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("current_attachment_ids", [["file_1"], []])
async def test_available_video_attachment_reaches_the_existing_copilot_model_turn(
    monkeypatch: pytest.MonkeyPatch,
    current_attachment_ids: list[str],
) -> None:
    captured: dict[str, Any] = {}

    async def capture_turn(**kwargs: Any) -> SimpleNamespace:
        captured["initial_input"] = kwargs["initial_input"]
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)
    evidence = VideoAttachmentEvidence(
        frames=(VideoAttachmentFrame("file_1", "demo.mp4", 0.5, base64.b64encode(b"frame").decode()),),
        duration_seconds_by_file_id=(("file_1", 1.0),),
    )
    load_evidence = AsyncMock(return_value=evidence)
    monkeypatch.setattr(agent_module, "load_video_attachment_evidence", load_evidence)
    raw_secret_safety_handler = AsyncMock(
        side_effect=[
            {"version": "1", "state": "clean", "handling": "none", "citations": []},
            {"unsafe_file_ids": []},
            _perception_result(),
        ]
    )
    persist_video_evidence_artifacts = AsyncMock()

    result = await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="make a workflow from this",
            workflow_yaml="",
            attached_file_ids=current_attachment_ids,
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=raw_secret_safety_handler,
        api_key="sk-test",
        attached_files=[CopilotAttachedFile(file_id="file_1", filename="demo.mp4")],
        persist_video_evidence_artifacts=persist_video_evidence_artifacts,
    )

    assert result.user_response == "ok"
    load_evidence.assert_awaited_once()
    assert load_evidence.await_args.kwargs["organization_id"] == "org-1"
    assert [item.file_id for item in load_evidence.await_args.args[0]] == ["file_1"]
    assert raw_secret_safety_handler.await_count == 3
    assert isinstance(captured["initial_input"], list)
    video_message = captured["initial_input"][1]
    assert not any(part.get("type") == "input_image" for part in video_message["content"])
    assert "The user opens Settings." in video_message["content"][0]["text"]
    persisted = persist_video_evidence_artifacts.await_args.args[0]["file_1"]
    assert persisted.sampled_frame_count == 1


@pytest.mark.asyncio
async def test_persisted_video_timeline_is_reused_without_decode_or_vision_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def capture_turn(**kwargs: Any) -> SimpleNamespace:
        captured["initial_input"] = kwargs["initial_input"]
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)
    load_evidence = AsyncMock()
    monkeypatch.setattr(agent_module, "load_video_attachment_evidence", load_evidence)
    safety_handler = AsyncMock(return_value={"version": "1", "state": "clean", "handling": "none", "citations": []})
    artifact = CopilotVideoEvidenceArtifact(
        version="1",
        duration_seconds=45.0,
        sampled_frame_count=20,
        observations=(
            CopilotVideoObservation(
                timestamp_seconds=8.0,
                description="The user opens the reports page.",
                confidence="high",
            ),
        ),
    )

    result = await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="now use the same video",
            workflow_yaml="",
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=safety_handler,
        api_key="sk-test",
        attached_files=[CopilotAttachedFile(file_id="file_1", filename="demo.mp4", video_evidence=artifact)],
    )

    assert result.user_response == "ok"
    load_evidence.assert_not_awaited()
    assert safety_handler.await_count == 1
    serialized_input = json.dumps(captured["initial_input"])
    assert "The user opens the reports page." in serialized_input
    assert "input_image" not in serialized_input


@pytest.mark.asyncio
async def test_persisted_video_timeline_is_not_reused_after_the_upload_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def capture_turn(**kwargs: Any) -> SimpleNamespace:
        captured["initial_input"] = kwargs["initial_input"]
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)
    load_evidence = AsyncMock(return_value=VideoAttachmentEvidence(unavailable_filenames=("demo.mp4",)))
    monkeypatch.setattr(agent_module, "load_video_attachment_evidence", load_evidence)
    safety_handler = AsyncMock(return_value={"version": "1", "state": "clean", "handling": "none", "citations": []})
    artifact = CopilotVideoEvidenceArtifact(
        version="1",
        duration_seconds=45.0,
        sampled_frame_count=20,
        observations=(
            CopilotVideoObservation(
                timestamp_seconds=8.0,
                description="The user opens the reports page.",
                confidence="high",
            ),
        ),
    )

    await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="use the old video",
            workflow_yaml="",
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=safety_handler,
        api_key="sk-test",
        attached_files=[
            CopilotAttachedFile(
                file_id="file_1",
                filename="demo.mp4",
                available=False,
                video_evidence=artifact,
            )
        ],
    )

    load_evidence.assert_awaited_once()
    serialized_input = json.dumps(captured["initial_input"])
    assert "The user opens the reports page." not in serialized_input
    assert "could not be inspected" in serialized_input


@pytest.mark.asyncio
async def test_overlength_video_status_is_persisted_and_reported_once(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def capture_turn(**kwargs: Any) -> SimpleNamespace:
        captured["initial_input"] = kwargs["initial_input"]
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)
    monkeypatch.setattr(
        agent_module,
        "load_video_attachment_evidence",
        AsyncMock(
            return_value=VideoAttachmentEvidence(
                too_long_filenames=("demo.mp4",),
                too_long_file_ids=("file_1",),
            )
        ),
    )
    safety_handler = AsyncMock(return_value={"version": "1", "state": "clean", "handling": "none", "citations": []})
    persist_too_long = AsyncMock()

    await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="use this video",
            workflow_yaml="",
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=safety_handler,
        api_key="sk-test",
        attached_files=[CopilotAttachedFile(file_id="file_1", filename="demo.mp4")],
        persist_too_long_video_file_ids=persist_too_long,
    )

    persist_too_long.assert_awaited_once_with(frozenset({"file_1"}))
    assert "exceeded the five-minute limit" in json.dumps(captured["initial_input"])


@pytest.mark.asyncio
async def test_persisted_overlength_video_is_not_reprocessed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def capture_turn(**kwargs: Any) -> SimpleNamespace:
        captured["initial_input"] = kwargs["initial_input"]
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)
    load_evidence = AsyncMock()
    monkeypatch.setattr(agent_module, "load_video_attachment_evidence", load_evidence)
    safety_handler = AsyncMock(return_value={"version": "1", "state": "clean", "handling": "none", "citations": []})

    await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="continue",
            workflow_yaml="",
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=safety_handler,
        api_key="sk-test",
        attached_files=[
            CopilotAttachedFile(
                file_id="file_1",
                filename="demo.mp4",
                video_processing_status="too_long",
            )
        ],
    )

    load_evidence.assert_not_awaited()
    assert "exceeded the five-minute limit" in json.dumps(captured["initial_input"])


@pytest.mark.asyncio
async def test_detected_visual_secret_withholds_frames_from_the_acting_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def capture_turn(**kwargs: Any) -> SimpleNamespace:
        captured["initial_input"] = kwargs["initial_input"]
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)
    evidence = VideoAttachmentEvidence(
        frames=(VideoAttachmentFrame("file_1", "demo.mp4", 0.5, base64.b64encode(b"raw-secret-frame").decode()),)
    )
    monkeypatch.setattr(agent_module, "load_video_attachment_evidence", AsyncMock(return_value=evidence))
    safety_handler = AsyncMock(
        side_effect=[
            {"version": "1", "state": "clean", "handling": "none", "citations": []},
            {"unsafe_file_ids": ["file_1"]},
        ]
    )
    persist_unsafe_video_file_ids = AsyncMock()

    await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="make a workflow from this",
            workflow_yaml="",
            attached_file_ids=["file_1"],
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=safety_handler,
        api_key="sk-test",
        attached_files=[CopilotAttachedFile(file_id="file_1", filename="demo.mp4")],
        persist_unsafe_video_file_ids=persist_unsafe_video_file_ids,
    )

    serialized_input = json.dumps(captured["initial_input"])
    assert "input_image" not in serialized_input
    assert "raw-secret-frame" not in serialized_input
    assert "withheld" in serialized_input.lower()
    persist_unsafe_video_file_ids.assert_awaited_once_with(frozenset({"file_1"}))


@pytest.mark.asyncio
async def test_historical_unsafe_video_does_not_hide_a_clean_reattachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def capture_turn(**kwargs: Any) -> SimpleNamespace:
        captured["initial_input"] = kwargs["initial_input"]
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)
    old_frame = base64.b64encode(b"old-secret-frame").decode()
    new_frame = base64.b64encode(b"new-clean-frame").decode()
    monkeypatch.setattr(
        agent_module,
        "load_video_attachment_evidence",
        AsyncMock(
            return_value=VideoAttachmentEvidence(
                frames=(
                    VideoAttachmentFrame("file_old", "old.mp4", 0.5, old_frame),
                    VideoAttachmentFrame("file_new", "new.mp4", 0.5, new_frame),
                ),
                duration_seconds_by_file_id=(("file_old", 1.0), ("file_new", 1.0)),
            )
        ),
    )
    safety_handler = AsyncMock(
        side_effect=[
            {"version": "1", "state": "clean", "handling": "none", "citations": []},
            {"unsafe_file_ids": ["file_old"]},
            {"unsafe_file_ids": []},
            _perception_result("The clean replacement opens Settings."),
        ]
    )

    result = await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="use the replacement video",
            workflow_yaml="",
            attached_file_ids=["file_new"],
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=safety_handler,
        api_key="sk-test",
        attached_files=[
            CopilotAttachedFile(file_id="file_new", filename="new.mp4"),
            CopilotAttachedFile(file_id="file_old", filename="old.mp4"),
        ],
    )

    assert result.user_response == "ok"
    serialized_input = json.dumps(captured["initial_input"])
    assert old_frame not in serialized_input
    assert new_frame not in serialized_input
    assert "The clean replacement opens Settings." in serialized_input
    assert "1 attached video was withheld" in serialized_input


@pytest.mark.asyncio
async def test_persisted_unsafe_video_is_never_decoded_or_rescreened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def capture_turn(**kwargs: Any) -> SimpleNamespace:
        captured["initial_input"] = kwargs["initial_input"]
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, capture_turn)
    new_frame = base64.b64encode(b"new-clean-frame").decode()
    load_evidence = AsyncMock(
        return_value=VideoAttachmentEvidence(
            frames=(VideoAttachmentFrame("file_new", "new.mp4", 0.5, new_frame),),
            duration_seconds_by_file_id=(("file_new", 1.0),),
        )
    )
    monkeypatch.setattr(agent_module, "load_video_attachment_evidence", load_evidence)
    safety_handler = AsyncMock(
        side_effect=[
            {"version": "1", "state": "clean", "handling": "none", "citations": []},
            {"unsafe_file_ids": []},
            _perception_result("The replacement opens Settings."),
        ]
    )

    result = await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="use the replacement video",
            workflow_yaml="",
            attached_file_ids=["file_new"],
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=safety_handler,
        api_key="sk-test",
        attached_files=[
            CopilotAttachedFile(file_id="file_new", filename="new.mp4"),
            CopilotAttachedFile(
                file_id="file_old",
                filename="old.mp4",
                video_safety_status="unsafe",
            ),
        ],
    )

    assert result.user_response == "ok"
    assert [item.file_id for item in load_evidence.await_args.args[0]] == ["file_new"]
    serialized_input = json.dumps(captured["initial_input"])
    assert new_frame not in serialized_input
    assert "The replacement opens Settings." in serialized_input
    assert "1 attached video was withheld" in serialized_input


@pytest.mark.asyncio
async def test_video_timeline_can_fallback_across_vision_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transient = litellm.APIConnectionError(message="primary failed", llm_provider="openai", model="primary")
    run_with_enforcement = AsyncMock(side_effect=transient)
    stub_copilot_agent_loop(monkeypatch, run_with_enforcement)

    def resolve_model_config(_handler, *, copilot_config=None, llm_key_override=None):
        del copilot_config
        key = llm_key_override or "PRIMARY"
        return f"model-{key}", object(), key, key == "PRIMARY"

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_resolver.resolve_model_config",
        resolve_model_config,
    )
    monkeypatch.setattr(
        agent_module,
        "load_video_attachment_evidence",
        AsyncMock(
            return_value=VideoAttachmentEvidence(
                frames=(VideoAttachmentFrame("file_1", "demo.mp4", 0.5, base64.b64encode(b"frame").decode()),),
                duration_seconds_by_file_id=(("file_1", 1.0),),
            )
        ),
    )
    safety_handler = AsyncMock(
        side_effect=[
            {"version": "1", "state": "clean", "handling": "none", "citations": []},
            {"unsafe_file_ids": []},
            _perception_result(),
        ]
    )

    result = await agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            workflow_permanent_id="wfp-1",
            workflow_id="wf-1",
            workflow_copilot_chat_id="chat-1",
            message="make a workflow from this",
            workflow_yaml="",
            attached_file_ids=["file_1"],
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=safety_handler,
        api_key="sk-test",
        attached_files=[CopilotAttachedFile(file_id="file_1", filename="demo.mp4")],
        config=CopilotConfig(fallback_llm_key="SECONDARY"),
    )

    assert run_with_enforcement.await_count == 2
    assert result.user_response
