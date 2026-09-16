from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent import ForgeAgent
from skyvern.webeye.browser_artifacts import RecordingPrefixSnapshot


def _make_task(task_id: str = "task-1", organization_id: str = "org-1") -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    task.organization_id = organization_id
    return task


def _make_video_artifact(artifact_id: str | None, video_data: bytes, video_path: str | None = None) -> MagicMock:
    artifact = MagicMock()
    artifact.video_artifact_id = artifact_id
    artifact.video_data = video_data
    artifact.video_path = video_path
    return artifact


def _make_browser_state_with_recordings(artifacts: list[MagicMock]) -> MagicMock:
    browser_state = MagicMock()
    browser_state.browser_artifacts.video_artifacts = artifacts
    return browser_state


@pytest.mark.asyncio
async def test_sync_video_noop_when_browser_state_is_none() -> None:
    """When browser_state is None the method must return without touching any app singletons."""
    agent = ForgeAgent()
    task = _make_task()

    with patch("skyvern.forge.agent.app") as mock_app:
        await agent._sync_video_artifact_after_step(task, browser_state=None)

    mock_app.BROWSER_MANAGER.snapshot_recording_prefixes.assert_not_called()
    mock_app.BROWSER_MANAGER.get_video_artifacts.assert_not_called()
    mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path.assert_not_called()
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_called()


@pytest.mark.asyncio
async def test_sync_video_streams_each_prefix_snapshot() -> None:
    """The ordinary per-step path streams exactly the snapshot prefix of each recording and never
    buffers the whole file through the byte-based update path."""
    agent = ForgeAgent()
    task = _make_task()
    browser_state = MagicMock()

    snapshots = [
        RecordingPrefixSnapshot(video_artifact_id="vid-a", path="/tmp/a.webm", prefix_len=100),
        RecordingPrefixSnapshot(video_artifact_id="vid-b", path="/tmp/b.webm", prefix_len=250),
    ]

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.snapshot_recording_prefixes = MagicMock(return_value=snapshots)
        mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path = AsyncMock()
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()

        await agent._sync_video_artifact_after_step(task, browser_state=browser_state)

    mock_app.BROWSER_MANAGER.snapshot_recording_prefixes.assert_called_once_with(
        browser_state=browser_state, task_id=task.task_id
    )
    # Streaming path only — the whole-file byte path must not run.
    mock_app.BROWSER_MANAGER.get_video_artifacts.assert_not_called()
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_called()
    assert mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path.await_count == 2
    mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path.assert_any_await(
        artifact_id="vid-a", organization_id=task.organization_id, path="/tmp/a.webm", length=100
    )
    mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path.assert_any_await(
        artifact_id="vid-b", organization_id=task.organization_id, path="/tmp/b.webm", length=250
    )


@pytest.mark.asyncio
async def test_sync_video_falls_back_to_byte_path_when_snapshot_is_none() -> None:
    """When the planner returns None (remux/finalize/first-registration required), the exact legacy
    byte-based upload is preserved."""
    agent = ForgeAgent()
    task = _make_task()
    browser_state = MagicMock()

    artifact_a = _make_video_artifact("vid-a", b"bytes-a")
    artifact_b = _make_video_artifact("vid-b", b"bytes-b")

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.snapshot_recording_prefixes = MagicMock(return_value=None)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[artifact_a, artifact_b])
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()
        mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path = AsyncMock()

        await agent._sync_video_artifact_after_step(task, browser_state=browser_state)

    mock_app.BROWSER_MANAGER.get_video_artifacts.assert_awaited_once_with(
        task_id=task.task_id, browser_state=browser_state, finalize=False
    )
    mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path.assert_not_called()
    assert mock_app.ARTIFACT_MANAGER.update_artifact_data.await_count == 2
    # Mid-step byte fallback must NOT seal/supersede a queued streamed prefix (supersede_queued_prefixes=False).
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_any_await(
        artifact_id="vid-a", organization_id=task.organization_id, data=b"bytes-a", supersede_queued_prefixes=False
    )
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_any_await(
        artifact_id="vid-b", organization_id=task.organization_id, data=b"bytes-b", supersede_queued_prefixes=False
    )


@pytest.mark.asyncio
async def test_sync_video_byte_fallback_skips_missing_registered_path_but_processes_sibling() -> None:
    """A registered recording whose local file vanished must not have its stale cached bytes written
    over the newer streamed remote prefix; the byte fallback skips it yet still writes a valid sibling
    returned in the same list (SKY-15288)."""
    agent = ForgeAgent()
    task = _make_task()
    browser_state = MagicMock()

    missing = _make_video_artifact("vid-missing", b"stale", video_path="/nonexistent/gone.webm")
    sibling = _make_video_artifact("vid-ok", b"bytes-ok", video_path=None)

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.snapshot_recording_prefixes = MagicMock(return_value=None)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[missing, sibling])
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()
        mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path = AsyncMock()

        await agent._sync_video_artifact_after_step(task, browser_state=browser_state)

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_awaited_once_with(
        artifact_id="vid-ok", organization_id=task.organization_id, data=b"bytes-ok", supersede_queued_prefixes=False
    )


@pytest.mark.asyncio
async def test_sync_video_noop_when_no_snapshots() -> None:
    """An empty plan uploads nothing and does not fall back to the byte path."""
    agent = ForgeAgent()
    task = _make_task()
    browser_state = MagicMock()

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.snapshot_recording_prefixes = MagicMock(return_value=[])
        mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path = AsyncMock()

        await agent._sync_video_artifact_after_step(task, browser_state=browser_state)

    mock_app.BROWSER_MANAGER.get_video_artifacts.assert_not_called()
    mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path.assert_not_called()


@pytest.mark.asyncio
async def test_sync_video_swallows_exception() -> None:
    """If planning raises, the method must not propagate; the warning log includes task_id and
    organization_id for traceability."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-err", organization_id="org-err")
    browser_state = MagicMock()

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.BROWSER_MANAGER.snapshot_recording_prefixes = MagicMock(
            side_effect=RuntimeError("storage unavailable")
        )
        mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path = AsyncMock()

        with patch("skyvern.forge.agent.LOG") as mock_log:
            await agent._sync_video_artifact_after_step(task, browser_state=browser_state)

        mock_log.warning.assert_called_once()
        _, kwargs = mock_log.warning.call_args
        assert kwargs.get("task_id") == "task-err"
        assert kwargs.get("organization_id") == "org-err"
        assert kwargs.get("exc_info") is True

    mock_app.ARTIFACT_MANAGER.stream_artifact_prefix_from_path.assert_not_called()
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_execution_state_skips_byte_read_when_all_recordings_registered() -> None:
    """The common step-loop path must not materialize an already-registered recording's whole (growing)
    file: when every tracked recording already has an id, get_video_artifacts is never called and the
    tracked artifact state is retained unchanged (SKY-15288, reviewer P1)."""
    agent = ForgeAgent()
    task = _make_task()
    step = MagicMock()
    registered = [_make_video_artifact("vid-a", b""), _make_video_artifact("vid-b", b"")]
    browser_state = _make_browser_state_with_recordings(registered)

    with (
        patch("skyvern.forge.agent.app") as mock_app,
        patch("skyvern.forge.agent.fail_step_if_browser_already_gone"),
    ):
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock()
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()
        mock_app.BROWSER_MANAGER.set_video_artifact_for_task = MagicMock()

        await agent.initialize_execution_state(task=task, step=step, pre_resolved_browser_state=browser_state)

    mock_app.BROWSER_MANAGER.get_video_artifacts.assert_not_called()
    mock_app.ARTIFACT_MANAGER.create_artifact.assert_not_called()
    mock_app.BROWSER_MANAGER.set_video_artifact_for_task.assert_not_called()
    assert [va.video_artifact_id for va in registered] == ["vid-a", "vid-b"]


@pytest.mark.asyncio
async def test_initialize_execution_state_registers_unregistered_recording() -> None:
    """A not-yet-registered recording (first registration / new page) still reads bytes and registers."""
    agent = ForgeAgent()
    task = _make_task()
    step = MagicMock()
    unregistered = _make_video_artifact(None, b"bytes-a")
    browser_state = _make_browser_state_with_recordings([unregistered])

    with (
        patch("skyvern.forge.agent.app") as mock_app,
        patch("skyvern.forge.agent.fail_step_if_browser_already_gone"),
    ):
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[unregistered])
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock(return_value="new-id-a")
        mock_app.BROWSER_MANAGER.set_video_artifact_for_task = MagicMock()

        await agent.initialize_execution_state(task=task, step=step, pre_resolved_browser_state=browser_state)

    mock_app.BROWSER_MANAGER.get_video_artifacts.assert_awaited_once_with(
        task_id=task.task_id, browser_state=browser_state, finalize=False
    )
    mock_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once()
    assert unregistered.video_artifact_id == "new-id-a"
    mock_app.BROWSER_MANAGER.set_video_artifact_for_task.assert_called_once_with(task, [unregistered])


@pytest.mark.asyncio
async def test_initialize_execution_state_registers_only_new_recording_when_mixed() -> None:
    """A later popup/new-page recording added alongside an already-registered one must not be skipped:
    the byte path runs and registers only the new artifact, leaving the existing id untouched."""
    agent = ForgeAgent()
    task = _make_task()
    step = MagicMock()
    registered = _make_video_artifact("vid-a", b"")
    new = _make_video_artifact(None, b"bytes-b")
    browser_state = _make_browser_state_with_recordings([registered, new])

    with (
        patch("skyvern.forge.agent.app") as mock_app,
        patch("skyvern.forge.agent.fail_step_if_browser_already_gone"),
    ):
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[registered, new])
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock(return_value="new-id-b")
        mock_app.BROWSER_MANAGER.set_video_artifact_for_task = MagicMock()

        await agent.initialize_execution_state(task=task, step=step, pre_resolved_browser_state=browser_state)

    mock_app.BROWSER_MANAGER.get_video_artifacts.assert_awaited_once_with(
        task_id=task.task_id, browser_state=browser_state, finalize=False
    )
    mock_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once()
    assert registered.video_artifact_id == "vid-a"
    assert new.video_artifact_id == "new-id-b"
