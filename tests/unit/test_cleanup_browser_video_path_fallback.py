"""Path-fallback behavior for ``Agent.cleanup_browser_and_create_artifacts``.

The standalone-task cleanup mirrors the workflow path: a recording attached during browser teardown
arrives as ``VideoArtifact(video_path=..., video_artifact_id=None)``; ``update_artifact_data`` no-ops
on a falsy id, so the MP4 must be promoted to a step-scoped RECORDING artifact via
``create_artifact(path=...)`` before the task's task-id drain runs.

OSS-synced: synthetic ids and example.* placeholders only.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact


def _make_task(task_id: str = "tsk_1", organization_id: str = "o_1") -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    task.organization_id = organization_id
    task.webhook_callback_url = None
    return task


def _make_step(step_id: str = "stp_1", task_id: str = "tsk_1") -> MagicMock:
    step = MagicMock()
    step.step_id = step_id
    step.task_id = task_id
    return step


def _browser_state() -> MagicMock:
    state = MagicMock()
    state.browser_artifacts = BrowserArtifacts()
    state.browser_context = None
    return state


@pytest.mark.asyncio
async def test_cleanup_creates_recording_from_path_when_id_is_none(tmp_path: Path) -> None:
    mp4 = tmp_path / "session.mp4"
    mp4.write_bytes(b"mp4-bytes")
    video_artifacts = [VideoArtifact(video_path=str(mp4))]
    assert video_artifacts[0].video_artifact_id is None

    agent = ForgeAgent()
    task = _make_task()
    last_step = _make_step()
    browser_state = _browser_state()

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.cleanup_for_task = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(return_value=b"")
        mock_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=b"")
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock(return_value="a_recording_path")

        await agent.cleanup_browser_and_create_artifacts(
            close_browser_on_completion=True,
            last_step=last_step,
            task=task,
        )

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_awaited()
    # Bytes streamed by path; do not load the whole video into memory.
    create_call_args = [
        c
        for c in mock_app.ARTIFACT_MANAGER.create_artifact.await_args_list
        if c.kwargs.get("artifact_type") == ArtifactType.RECORDING
    ]
    assert len(create_call_args) == 1
    kwargs = create_call_args[0].kwargs
    assert kwargs["step"] is last_step
    assert kwargs["path"] == str(mp4)
    assert kwargs.get("data") is None
    # The new id is stored back so downstream lookups find the row.
    assert video_artifacts[0].video_artifact_id == "a_recording_path"


@pytest.mark.asyncio
async def test_cleanup_path_fallback_skips_when_path_missing(tmp_path: Path) -> None:
    absent = tmp_path / "missing.mp4"
    video_artifacts = [VideoArtifact(video_path=str(absent))]

    agent = ForgeAgent()
    task = _make_task()
    last_step = _make_step()
    browser_state = _browser_state()

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.cleanup_for_task = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(return_value=b"")
        mock_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=b"")
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()

        await agent.cleanup_browser_and_create_artifacts(
            close_browser_on_completion=True,
            last_step=last_step,
            task=task,
        )

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_awaited()
    recording_calls = [
        c
        for c in mock_app.ARTIFACT_MANAGER.create_artifact.await_args_list
        if c.kwargs.get("artifact_type") == ArtifactType.RECORDING
    ]
    assert recording_calls == []
    assert video_artifacts[0].video_artifact_id is None


@pytest.mark.asyncio
async def test_cleanup_preserves_update_path_for_pre_registered_artifact(tmp_path: Path) -> None:
    # A standard Playwright recording arrives pre-registered (``initialize_execution_state``); the
    # existing data-update path stays in charge and the new path-upload helper stays idle.
    video_artifacts = [
        VideoArtifact(
            video_path=str(tmp_path / "playwright.webm"),
            video_artifact_id="a_existing",
            video_data=b"video",
        )
    ]

    agent = ForgeAgent()
    task = _make_task()
    last_step = _make_step()
    browser_state = _browser_state()

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.cleanup_for_task = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(return_value=b"")
        mock_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=b"")
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()

        await agent.cleanup_browser_and_create_artifacts(
            close_browser_on_completion=True,
            last_step=last_step,
            task=task,
        )

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_awaited_once_with(
        artifact_id="a_existing",
        organization_id="o_1",
        data=b"video",
    )
    recording_calls = [
        c
        for c in mock_app.ARTIFACT_MANAGER.create_artifact.await_args_list
        if c.kwargs.get("artifact_type") == ArtifactType.RECORDING
    ]
    assert recording_calls == []
    assert video_artifacts[0].video_artifact_id == "a_existing"
