from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.service import WorkflowService


def _workflow_run() -> SimpleNamespace:
    return SimpleNamespace(
        browser_session_id="pbs_1",
        browser_address=None,
        started_at=datetime(2026, 6, 4, 16, 0, tzinfo=UTC),
        finished_at=datetime(2026, 6, 4, 16, 30, tzinfo=UTC),
        organization_id="o_1",
        workflow_run_id="wr_1",
    )


def _clip_artifact() -> SimpleNamespace:
    return SimpleNamespace(artifact_id="a_clip", uri="s3://b/browser_sessions/pbs_1/run_recordings/wr_1/wr_1.mp4")


async def _fetch(workflow_run: SimpleNamespace, task_v2: object | None = None) -> tuple[list[str], bool]:
    # _fetch_recording_urls does not use self; a bare object stands in.
    return await WorkflowService._fetch_recording_urls(object(), workflow_run, task_v2, "o_1")


@pytest.mark.asyncio
async def test_prefers_run_scoped_clip_over_session_recording(monkeypatch) -> None:
    list_clips = AsyncMock(return_value=[_clip_artifact()])
    get_session = AsyncMock(
        return_value=[FileInfo(url="session_url", checksum=None, filename=None, modified_at=_workflow_run().started_at)]
    )
    monkeypatch.setattr(app.DATABASE.artifacts, "list_artifacts_for_run_by_type", list_clips, raising=False)
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "is_recording_archived", AsyncMock(return_value=False), raising=False)
    monkeypatch.setattr(
        app.ARTIFACT_MANAGER, "get_share_links_with_bundle_support", AsyncMock(return_value=["clip_url"]), raising=False
    )
    monkeypatch.setattr(app.STORAGE, "get_shared_recordings_in_browser_session", get_session, raising=False)

    urls, archived = await _fetch(_workflow_run())

    assert urls == ["clip_url"]
    assert archived is False
    get_session.assert_not_awaited()  # the multi-hour session recording is not consulted when a clip exists


@pytest.mark.asyncio
async def test_falls_back_to_session_window_when_no_clip(monkeypatch) -> None:
    monkeypatch.setattr(
        app.DATABASE.artifacts, "list_artifacts_for_run_by_type", AsyncMock(return_value=[]), raising=False
    )
    in_window = FileInfo(
        url="session_url", checksum=None, filename=None, modified_at=datetime(2026, 6, 4, 16, 20, tzinfo=UTC)
    )
    monkeypatch.setattr(
        app.STORAGE, "get_shared_recordings_in_browser_session", AsyncMock(return_value=[in_window]), raising=False
    )

    urls, archived = await _fetch(_workflow_run())

    assert urls == ["session_url"]
    assert archived is False


@pytest.mark.asyncio
async def test_unbounded_fallback_serves_late_session_recording(monkeypatch) -> None:
    monkeypatch.setattr(
        app.DATABASE.artifacts, "list_artifacts_for_run_by_type", AsyncMock(return_value=[]), raising=False
    )
    # Finalized ~2h after the window upper bound (16:45) — bounded window misses it, unbounded recovers it.
    late = FileInfo(url="late_url", checksum=None, filename=None, modified_at=datetime(2026, 6, 4, 18, 45, tzinfo=UTC))
    monkeypatch.setattr(
        app.STORAGE, "get_shared_recordings_in_browser_session", AsyncMock(return_value=[late]), raising=False
    )

    urls, _archived = await _fetch(_workflow_run())

    assert urls == ["late_url"]


@pytest.mark.asyncio
async def test_archived_run_clip_reports_archived_without_session_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        app.DATABASE.artifacts,
        "list_artifacts_for_run_by_type",
        AsyncMock(return_value=[_clip_artifact()]),
        raising=False,
    )
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "is_recording_archived", AsyncMock(return_value=True), raising=False)
    get_session = AsyncMock(return_value=[])
    monkeypatch.setattr(app.STORAGE, "get_shared_recordings_in_browser_session", get_session, raising=False)

    urls, archived = await _fetch(_workflow_run())

    assert urls == []
    assert archived is True
    get_session.assert_not_awaited()


def _legacy_artifact() -> SimpleNamespace:
    # A run's own (non-clip) recording — no /run_recordings/ segment.
    return SimpleNamespace(artifact_id="a_legacy", uri="s3://b/v1/prod/o_1/wr_1/step/recording.webm")


@pytest.mark.asyncio
async def test_legacy_run_recording_does_not_preempt_session_recording(monkeypatch) -> None:
    # A non-clip run recording (e.g. an in-progress per-step snapshot) must NOT be served over
    # the finalized session recording for a browser-session run (Codex P2).
    monkeypatch.setattr(
        app.DATABASE.artifacts,
        "list_artifacts_for_run_by_type",
        AsyncMock(return_value=[_legacy_artifact()]),
        raising=False,
    )
    in_window = FileInfo(
        url="session_url", checksum=None, filename=None, modified_at=datetime(2026, 6, 4, 16, 20, tzinfo=UTC)
    )
    monkeypatch.setattr(
        app.STORAGE, "get_shared_recordings_in_browser_session", AsyncMock(return_value=[in_window]), raising=False
    )
    get_links = AsyncMock(return_value=["legacy_url"])
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "get_share_links_with_bundle_support", get_links, raising=False)

    urls, _archived = await _fetch(_workflow_run())

    assert urls == ["session_url"]
    get_links.assert_not_awaited()  # the legacy run recording is not served


@pytest.mark.asyncio
async def test_session_run_own_recording_not_served_when_unfinalized(monkeypatch) -> None:
    # A browser-session run's own (non-clip) recording is never finalized — its browser stays
    # open on completion, so the per-run webm has no Duration/Cues and won't play. It must NOT
    # be served as a last resort; the clip / finalized session recording cover session runs once
    # the session closes (SKY-11086: "recording shows up but the video doesn't work").
    monkeypatch.setattr(
        app.DATABASE.artifacts,
        "list_artifacts_for_run_by_type",
        AsyncMock(return_value=[_legacy_artifact()]),
        raising=False,
    )
    monkeypatch.setattr(
        app.STORAGE, "get_shared_recordings_in_browser_session", AsyncMock(return_value=[]), raising=False
    )
    get_links = AsyncMock(return_value=["legacy_url"])
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "get_share_links_with_bundle_support", get_links, raising=False)
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "is_recording_archived", AsyncMock(return_value=False), raising=False)

    urls, archived = await _fetch(_workflow_run())

    assert urls == []
    assert archived is False
    get_links.assert_not_awaited()  # the unfinalized per-run recording is never served for a session run


@pytest.mark.asyncio
async def test_browser_address_run_own_recording_not_served_when_unfinalized(monkeypatch) -> None:
    # Same invariant for a run pinned to a remote browser via browser_address (no browser_session_id):
    # its browser also stays open on completion, so its own per-run recording never finalizes.
    monkeypatch.setattr(
        app.DATABASE.artifacts,
        "list_artifacts_for_run_by_type",
        AsyncMock(return_value=[_legacy_artifact()]),
        raising=False,
    )
    get_session = AsyncMock(return_value=[])
    monkeypatch.setattr(app.STORAGE, "get_shared_recordings_in_browser_session", get_session, raising=False)
    get_links = AsyncMock(return_value=["legacy_url"])
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "get_share_links_with_bundle_support", get_links, raising=False)

    run = _workflow_run()
    run.browser_session_id = None
    run.browser_address = "ws://browser.example:9222"

    urls, archived = await _fetch(run)

    assert urls == []
    assert archived is False
    get_links.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_run_recording_served_for_non_session_run(monkeypatch) -> None:
    # A non-session task run's own recording is the last resort (keeps normal task recordings working).
    monkeypatch.setattr(
        app.DATABASE.artifacts,
        "list_artifacts_for_run_by_type",
        AsyncMock(return_value=[_legacy_artifact()]),
        raising=False,
    )
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "is_recording_archived", AsyncMock(return_value=False), raising=False)
    monkeypatch.setattr(
        app.ARTIFACT_MANAGER,
        "get_share_links_with_bundle_support",
        AsyncMock(return_value=["legacy_url"]),
        raising=False,
    )
    get_session = AsyncMock(return_value=[])
    monkeypatch.setattr(app.STORAGE, "get_shared_recordings_in_browser_session", get_session, raising=False)

    run = _workflow_run()
    run.browser_session_id = None  # non-session run

    urls, _archived = await _fetch(run)

    assert urls == ["legacy_url"]
    get_session.assert_not_awaited()  # no browser session to fall back to
