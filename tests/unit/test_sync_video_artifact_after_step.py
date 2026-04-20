from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent import ForgeAgent


def _make_task(task_id: str = "task-1", organization_id: str = "org-1") -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    task.organization_id = organization_id
    return task


def _make_video_artifact(artifact_id: str, video_data: bytes) -> MagicMock:
    artifact = MagicMock()
    artifact.video_artifact_id = artifact_id
    artifact.video_data = video_data
    return artifact


@pytest.mark.asyncio
async def test_sync_video_noop_when_browser_state_is_none() -> None:
    """When browser_state is None the method must return without touching any app singletons."""
    agent = ForgeAgent()
    task = _make_task()

    with patch("skyvern.forge.agent.app") as mock_app:
        await agent._sync_video_artifact_after_step(task, browser_state=None)

    mock_app.BROWSER_MANAGER.get_video_artifacts.assert_not_called()
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_called()


@pytest.mark.asyncio
async def test_sync_video_uploads_each_artifact() -> None:
    """Each video artifact returned by BROWSER_MANAGER must be uploaded via ARTIFACT_MANAGER."""
    agent = ForgeAgent()
    task = _make_task()
    browser_state = MagicMock()

    artifact_a = _make_video_artifact("vid-a", b"bytes-a")
    artifact_b = _make_video_artifact("vid-b", b"bytes-b")

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[artifact_a, artifact_b])
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()

        await agent._sync_video_artifact_after_step(task, browser_state=browser_state)

    # Per-step sync runs while the recording file is still open; finalize=False skips the
    # ffmpeg remux path so long tasks do not spawn one ffmpeg subprocess per step.
    mock_app.BROWSER_MANAGER.get_video_artifacts.assert_awaited_once_with(
        task_id=task.task_id, browser_state=browser_state, finalize=False
    )
    assert mock_app.ARTIFACT_MANAGER.update_artifact_data.await_count == 2
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_any_await(
        artifact_id="vid-a", organization_id=task.organization_id, data=b"bytes-a"
    )
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_any_await(
        artifact_id="vid-b", organization_id=task.organization_id, data=b"bytes-b"
    )


@pytest.mark.asyncio
async def test_sync_video_swallows_exception() -> None:
    """If get_video_artifacts raises, the method must not propagate the exception,
    and the warning log must include task_id and organization_id for traceability."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-err", organization_id="org-err")
    browser_state = MagicMock()

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(side_effect=RuntimeError("storage unavailable"))

        with patch("skyvern.forge.agent.LOG") as mock_log:
            # Should not raise
            await agent._sync_video_artifact_after_step(task, browser_state=browser_state)

        mock_log.warning.assert_called_once()
        _, kwargs = mock_log.warning.call_args
        assert kwargs.get("task_id") == "task-err"
        assert kwargs.get("organization_id") == "org-err"
        assert kwargs.get("exc_info") is True

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_called()
