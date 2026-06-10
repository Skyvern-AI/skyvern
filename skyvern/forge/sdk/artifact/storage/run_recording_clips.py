from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.api.files import calculate_sha256_for_file
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.webeye.video_utils import cut_recording_segment, plan_run_segment, probe_media_duration_seconds

LOG = structlog.get_logger()

# Bound how many runs we clip per session so a heavily reused session can't fan out into an
# unbounded number of ffmpeg jobs at close time.
MAX_RUNS_PER_SESSION = 200

# Artifact-type folder for per-run clips. Kept distinct from "videos" so the session recording
# listing (which scans .../videos) never picks up clips, and used as the URI marker that
# identifies a clip created by this path (vs. a run's own full recording).
RUN_RECORDING_PATH_SEGMENT = "run_recordings"
_CLIP_URI_MARKER = f"/{RUN_RECORDING_PATH_SEGMENT}/"

# (run_id, local_clip_path, filename) -> stored uri
UploadClipFn = Callable[[str, str, str], Awaitable[str]]


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


async def sync_run_recording_clips(
    *,
    organization_id: str,
    browser_session_id: str,
    source_path: str,
    upload_clip: UploadClipFn,
    now: datetime | None = None,
) -> None:
    """Cut each session run's window out of the finalized recording and register it as a
    run-scoped RECORDING artifact (``run_id`` set, ``browser_session_id`` unset).

    The recording is anchored on the wall clock as ``[now - duration, now]``, ``now`` defaulting to
    close time. Best-effort: any ffmpeg/ffprobe/DB/upload failure is logged and skipped, never raised.
    """
    duration = await probe_media_duration_seconds(source_path)
    if not duration:
        LOG.info(
            "Skipping run recording clips: source duration unavailable",
            browser_session_id=browser_session_id,
            source_path=source_path,
        )
        return

    video_start = (now or datetime.now(UTC)) - timedelta(seconds=duration)

    # A session can have more than one source video (e.g. a popup page), each synced separately.
    # Key clips on the source stem so a second source video for the same run is still clipped.
    source_stem = os.path.splitext(os.path.basename(source_path))[0]

    try:
        runs = await app.DATABASE.workflow_runs.get_workflow_runs_for_browser_session(
            browser_session_id=browser_session_id,
            organization_id=organization_id,
            page_size=MAX_RUNS_PER_SESSION,
        )
    except Exception:
        LOG.warning(
            "Skipping run recording clips: failed to list runs for browser session",
            browser_session_id=browser_session_id,
            exc_info=True,
        )
        return

    if len(runs) >= MAX_RUNS_PER_SESSION:
        LOG.warning(
            "Run recording clip generation hit the per-session cap; some runs were not clipped",
            browser_session_id=browser_session_id,
            cap=MAX_RUNS_PER_SESSION,
        )

    for run in runs:
        if run.started_at is None:
            continue
        segment = plan_run_segment(run.started_at, run.finished_at, video_start, duration)
        if segment is None:
            continue
        start_seconds, clip_duration = segment
        try:
            # Scope the clip to the id the run view reads by: task_v2 runs are read by
            # observer_cruise_id, every other run by workflow_run_id.
            task_v2 = await app.DATABASE.observer.get_task_v2_by_workflow_run_id(run.workflow_run_id, organization_id)
            run_id = task_v2.observer_cruise_id if task_v2 else run.workflow_run_id

            existing = await app.DATABASE.artifacts.list_artifacts_for_run_by_type(
                run_id=run_id,
                organization_id=organization_id,
                artifact_type=ArtifactType.RECORDING,
            )
            # Idempotent per (run, source): skip only a clip already made from THIS source video
            # (a run's own full recording, or a clip from another source video, must not suppress
            # it), so a retried sync is a no-op while popup/secondary videos still get their clip.
            clip_basename = f"{run.workflow_run_id}-{source_stem}"
            if any(a.uri and _CLIP_URI_MARKER in a.uri and clip_basename in a.uri for a in existing):
                continue
            async with cut_recording_segment(
                source_path, start_seconds=start_seconds, duration_seconds=clip_duration
            ) as clip_path:
                if clip_path is None:
                    continue
                uri = await upload_clip(run.workflow_run_id, clip_path, f"{clip_basename}.mp4")
                await app.ARTIFACT_MANAGER.create_run_recording_artifact(
                    organization_id=organization_id,
                    run_id=run_id,
                    workflow_run_id=run.workflow_run_id,
                    uri=uri,
                    checksum=calculate_sha256_for_file(clip_path),
                    file_size=_file_size(clip_path),
                )
        except Exception:
            LOG.warning(
                "Failed to generate run recording clip",
                browser_session_id=browser_session_id,
                workflow_run_id=run.workflow_run_id,
                exc_info=True,
            )
            continue
