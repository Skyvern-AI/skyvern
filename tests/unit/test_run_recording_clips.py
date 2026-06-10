from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.artifact.storage import run_recording_clips
from skyvern.forge.sdk.artifact.storage.run_recording_clips import sync_run_recording_clips

# close time 19:00 with a 3h recording => the video spans 16:00 - 19:00 UTC.
NOW = datetime(2026, 6, 4, 19, 0, 0, tzinfo=UTC)
DURATION = 3 * 60 * 60


def _run(run_id: str, started: datetime, finished: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(workflow_run_id=run_id, started_at=started, finished_at=finished)


def _artifact(uri: str) -> SimpleNamespace:
    return SimpleNamespace(artifact_id="a_x", uri=uri)


@asynccontextmanager
async def _fake_cut(src_path, *, start_seconds, duration_seconds):
    yield "/tmp/clip.mp4"


def _setup(monkeypatch, runs, existing_by_run=None, duration=DURATION, task_v2_by_run=None):
    existing_by_run = existing_by_run or {}
    task_v2_by_run = task_v2_by_run or {}
    get_runs = AsyncMock(return_value=runs)
    list_existing = AsyncMock(
        side_effect=lambda run_id, organization_id, artifact_type: existing_by_run.get(run_id, [])
    )
    get_task_v2 = AsyncMock(side_effect=lambda workflow_run_id, organization_id: task_v2_by_run.get(workflow_run_id))
    create_clip = AsyncMock(return_value="a_clip")
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_runs_for_browser_session", get_runs, raising=False)
    monkeypatch.setattr(app.DATABASE.artifacts, "list_artifacts_for_run_by_type", list_existing, raising=False)
    monkeypatch.setattr(app.DATABASE.observer, "get_task_v2_by_workflow_run_id", get_task_v2, raising=False)
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "create_run_recording_artifact", create_clip, raising=False)
    monkeypatch.setattr(
        run_recording_clips, "probe_media_duration_seconds", AsyncMock(return_value=duration), raising=False
    )
    monkeypatch.setattr(run_recording_clips, "cut_recording_segment", _fake_cut, raising=False)
    monkeypatch.setattr(run_recording_clips, "calculate_sha256_for_file", lambda p: "sha", raising=False)
    return get_runs, list_existing, create_clip


async def _run_clips(upload_clip) -> None:
    await sync_run_recording_clips(
        organization_id="o_1",
        browser_session_id="pbs_1",
        source_path="/tmp/session.mp4",
        upload_clip=upload_clip,
        now=NOW,
    )


def _recording_upload():
    uploaded: list[tuple[str, str, str]] = []

    async def upload_clip(run_id: str, clip_path: str, filename: str) -> str:
        uploaded.append((run_id, clip_path, filename))
        return f"s3://b/run_recordings/{run_id}/{filename}"

    return uploaded, upload_clip


@pytest.mark.asyncio
async def test_creates_run_scoped_clip_per_overlapping_run(monkeypatch) -> None:
    runs = [
        _run("wr_1", datetime(2026, 6, 4, 16, 5, tzinfo=UTC), datetime(2026, 6, 4, 16, 35, tzinfo=UTC)),
        _run("wr_2", datetime(2026, 6, 4, 17, 0, tzinfo=UTC), datetime(2026, 6, 4, 17, 10, tzinfo=UTC)),
    ]
    _get, _list, create_clip = _setup(monkeypatch, runs)
    uploaded, upload_clip = _recording_upload()

    await _run_clips(upload_clip)

    assert [u[0] for u in uploaded] == ["wr_1", "wr_2"]
    assert create_clip.await_count == 2
    created = {c.kwargs["run_id"]: c.kwargs for c in create_clip.await_args_list}
    # Clip filename is stamped with the source-video stem ("session" from /tmp/session.mp4).
    assert created["wr_1"]["uri"] == "s3://b/run_recordings/wr_1/wr_1-session.mp4"
    assert created["wr_1"]["workflow_run_id"] == "wr_1"
    assert created["wr_1"]["checksum"] == "sha"


@pytest.mark.asyncio
async def test_skips_run_that_already_has_a_clip(monkeypatch) -> None:
    runs = [
        _run("wr_1", datetime(2026, 6, 4, 16, 5, tzinfo=UTC), datetime(2026, 6, 4, 16, 35, tzinfo=UTC)),
        _run("wr_2", datetime(2026, 6, 4, 17, 0, tzinfo=UTC), datetime(2026, 6, 4, 17, 10, tzinfo=UTC)),
    ]
    existing = {"wr_1": [_artifact("s3://b/browser_sessions/pbs_1/run_recordings/2026/wr_1/wr_1-session.mp4")]}
    _get, _list, create_clip = _setup(monkeypatch, runs, existing)
    uploaded, upload_clip = _recording_upload()

    await _run_clips(upload_clip)

    assert [u[0] for u in uploaded] == ["wr_2"]
    assert create_clip.await_count == 1


@pytest.mark.asyncio
async def test_second_source_video_for_same_run_still_clipped(monkeypatch) -> None:
    # A popup/secondary video is synced separately; a clip from the first source must not
    # suppress clipping the second source for the same run (Codex P2).
    runs = [_run("wr_1", datetime(2026, 6, 4, 16, 5, tzinfo=UTC), datetime(2026, 6, 4, 16, 35, tzinfo=UTC))]
    existing = {"wr_1": [_artifact("s3://b/browser_sessions/pbs_1/run_recordings/2026/wr_1/wr_1-session.mp4")]}
    _get, _list, create_clip = _setup(monkeypatch, runs, existing)
    uploaded, upload_clip = _recording_upload()

    # Second source video (a popup), distinct stem. Clips are always re-encoded to mp4.
    await sync_run_recording_clips(
        organization_id="o_1",
        browser_session_id="pbs_1",
        source_path="/tmp/popup.webm",
        upload_clip=upload_clip,
        now=NOW,
    )

    assert uploaded == [("wr_1", "/tmp/clip.mp4", "wr_1-popup.mp4")]
    assert create_clip.await_count == 1


@pytest.mark.asyncio
async def test_does_not_skip_when_only_full_recording_exists(monkeypatch) -> None:
    # A run's own (non-clip) RECORDING artifact must not suppress clip creation (issue 4).
    runs = [_run("wr_1", datetime(2026, 6, 4, 16, 5, tzinfo=UTC), datetime(2026, 6, 4, 16, 35, tzinfo=UTC))]
    existing = {"wr_1": [_artifact("s3://b/v1/prod/o_1/wr_1/step/recording.webm")]}  # not a run_recordings/ clip
    _get, _list, create_clip = _setup(monkeypatch, runs, existing)
    uploaded, upload_clip = _recording_upload()

    await _run_clips(upload_clip)

    assert uploaded == [("wr_1", "/tmp/clip.mp4", "wr_1-session.mp4")]
    assert create_clip.await_count == 1


@pytest.mark.asyncio
async def test_task_v2_clip_written_under_observer_cruise_id(monkeypatch) -> None:
    # task_v2 runs are read by observer_cruise_id, so the clip must be scoped to it (issue 3).
    runs = [_run("wr_1", datetime(2026, 6, 4, 16, 5, tzinfo=UTC), datetime(2026, 6, 4, 16, 35, tzinfo=UTC))]
    task_v2 = {"wr_1": SimpleNamespace(observer_cruise_id="oc_1")}
    _get, _list, create_clip = _setup(monkeypatch, runs, task_v2_by_run=task_v2)
    _uploaded, upload_clip = _recording_upload()

    await _run_clips(upload_clip)

    create_clip.assert_awaited_once()
    kwargs = create_clip.await_args.kwargs
    assert kwargs["run_id"] == "oc_1"  # read-path id
    assert kwargs["workflow_run_id"] == "wr_1"  # original run preserved


@pytest.mark.asyncio
async def test_skips_run_with_no_overlap(monkeypatch) -> None:
    # Finished before the video (16:00) started — belongs to an earlier recording.
    runs = [_run("wr_old", datetime(2026, 6, 4, 15, 0, tzinfo=UTC), datetime(2026, 6, 4, 15, 30, tzinfo=UTC))]
    _get, _list, create_clip = _setup(monkeypatch, runs)
    uploaded, upload_clip = _recording_upload()

    await _run_clips(upload_clip)

    assert uploaded == []
    assert create_clip.await_count == 0


@pytest.mark.asyncio
async def test_no_duration_returns_early_without_enumerating_runs(monkeypatch) -> None:
    get_runs, _list, create_clip = _setup(monkeypatch, [], duration=None)
    await _run_clips(AsyncMock())
    get_runs.assert_not_awaited()
    create_clip.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_run_failure_does_not_block_others(monkeypatch) -> None:
    runs = [
        _run("wr_boom", datetime(2026, 6, 4, 16, 5, tzinfo=UTC), datetime(2026, 6, 4, 16, 35, tzinfo=UTC)),
        _run("wr_ok", datetime(2026, 6, 4, 17, 0, tzinfo=UTC), datetime(2026, 6, 4, 17, 10, tzinfo=UTC)),
    ]
    _get, _list, create_clip = _setup(monkeypatch, runs)
    uploaded: list[str] = []

    async def upload_clip(run_id: str, clip_path: str, filename: str) -> str:
        if run_id == "wr_boom":
            raise RuntimeError("upload exploded")
        uploaded.append(run_id)
        return f"s3://b/{run_id}.mp4"

    await _run_clips(upload_clip)

    assert uploaded == ["wr_ok"]
    assert create_clip.await_count == 1
