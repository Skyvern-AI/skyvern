"""Finalized-byte preference and path-fallback behavior for recording cleanup.

The standalone-task cleanup mirrors the workflow path: a recording attached during browser teardown
arrives as ``VideoArtifact(video_path=..., video_artifact_id=None)``. Finalized ``video_data`` must be
promoted to a step-scoped RECORDING artifact, with ``create_artifact(path=...)`` reserved for artifacts
whose data is empty.

OSS-synced: synthetic ids and example.* placeholders only.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER
from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact


def _make_task(
    task_id: str = "tsk_1",
    organization_id: str = "o_1",
    workflow_run_id: str | None = None,
) -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    task.organization_id = organization_id
    task.workflow_run_id = workflow_run_id
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
async def test_workflow_cleanup_creates_recording_from_finalized_data(tmp_path: Path) -> None:
    webm = tmp_path / "session.webm"
    webm.write_bytes(b"raw-bytes")
    video_artifacts = [VideoArtifact(video_path=str(webm), video_data=b"finalized-bytes", video_artifact_id=None)]
    last_task = _make_task()
    last_step = _make_step()
    workflow = SimpleNamespace(workflow_id="w_1")
    workflow_run = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1")

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id = AsyncMock(return_value=[last_task])
        mock_app.DATABASE.tasks.get_latest_step = AsyncMock(return_value=last_step)
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock(return_value="a_recording_data")
        mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks = AsyncMock()

        await WorkflowService().persist_video_data(_browser_state(), workflow, workflow_run)

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_awaited()
    mock_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once()
    kwargs = mock_app.ARTIFACT_MANAGER.create_artifact.call_args.kwargs
    assert kwargs["step"] is last_step
    assert kwargs["artifact_type"] == ArtifactType.RECORDING
    assert kwargs["data"] == b"finalized-bytes"
    assert kwargs.get("path") is None
    assert video_artifacts[0].video_artifact_id == "a_recording_data"
    mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks.assert_awaited_once_with(["tsk_1"])


@pytest.mark.asyncio
async def test_workflow_cleanup_creates_recording_from_finalized_data_when_path_is_missing(tmp_path: Path) -> None:
    missing_webm = tmp_path / "missing.webm"
    assert not missing_webm.exists()
    video_artifacts = [
        VideoArtifact(video_path=str(missing_webm), video_data=b"finalized-bytes", video_artifact_id=None)
    ]
    last_task = _make_task()
    last_step = _make_step()
    workflow = SimpleNamespace(workflow_id="w_1")
    workflow_run = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1")

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id = AsyncMock(return_value=[last_task])
        mock_app.DATABASE.tasks.get_latest_step = AsyncMock(return_value=last_step)
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock(return_value="a_recording_data")
        mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks = AsyncMock()

        await WorkflowService().persist_video_data(_browser_state(), workflow, workflow_run)

    mock_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once_with(
        step=last_step,
        artifact_type=ArtifactType.RECORDING,
        data=b"finalized-bytes",
    )
    assert video_artifacts[0].video_artifact_id == "a_recording_data"
    mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks.assert_awaited_once_with(["tsk_1"])


@pytest.mark.asyncio
async def test_workflow_cleanup_falls_back_to_recording_path_when_data_is_empty(tmp_path: Path) -> None:
    webm = tmp_path / "session.webm"
    webm.write_bytes(b"raw-bytes")
    video_artifacts = [VideoArtifact(video_path=str(webm), video_data=b"", video_artifact_id=None)]
    last_task = _make_task()
    last_step = _make_step()
    workflow = SimpleNamespace(workflow_id="w_1")
    workflow_run = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1")

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.DATABASE.tasks.get_tasks_by_workflow_run_id = AsyncMock(return_value=[last_task])
        mock_app.DATABASE.tasks.get_latest_step = AsyncMock(return_value=last_step)
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock()
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock(return_value="a_recording_path")
        mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks = AsyncMock()

        await WorkflowService().persist_video_data(_browser_state(), workflow, workflow_run)

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_awaited()
    mock_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once()
    kwargs = mock_app.ARTIFACT_MANAGER.create_artifact.call_args.kwargs
    assert kwargs["step"] is last_step
    assert kwargs["artifact_type"] == ArtifactType.RECORDING
    assert kwargs["path"] == str(webm)
    assert kwargs.get("data") is None
    assert video_artifacts[0].video_artifact_id == "a_recording_path"
    mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks.assert_awaited_once_with(["tsk_1"])


@pytest.mark.asyncio
async def test_cleanup_creates_recording_from_finalized_data(tmp_path: Path) -> None:
    webm = tmp_path / "session.webm"
    webm.write_bytes(b"raw-bytes")
    video_artifacts = [VideoArtifact(video_path=str(webm), video_data=b"finalized-bytes", video_artifact_id=None)]

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
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock(return_value="a_recording_data")

        await agent.cleanup_browser_and_create_artifacts(
            close_browser_on_completion=True,
            last_step=last_step,
            task=task,
        )

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_awaited()
    create_call_args = [
        c
        for c in mock_app.ARTIFACT_MANAGER.create_artifact.await_args_list
        if c.kwargs.get("artifact_type") == ArtifactType.RECORDING
    ]
    assert len(create_call_args) == 1
    kwargs = create_call_args[0].kwargs
    assert kwargs["step"] is last_step
    assert kwargs["data"] == b"finalized-bytes"
    assert kwargs.get("path") is None
    assert video_artifacts[0].video_artifact_id == "a_recording_data"


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
    webm = tmp_path / "playwright.webm"
    webm.write_bytes(b"video")
    video_artifacts = [
        VideoArtifact(
            video_path=str(webm),
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

    # Terminal finalize (no-extension branch) must supersede queued prefixes.
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_awaited_once_with(
        artifact_id="a_existing",
        organization_id="o_1",
        data=b"video",
        supersede_queued_prefixes=True,
    )
    recording_calls = [
        c
        for c in mock_app.ARTIFACT_MANAGER.create_artifact.await_args_list
        if c.kwargs.get("artifact_type") == ArtifactType.RECORDING
    ]
    assert recording_calls == []
    assert video_artifacts[0].video_artifact_id == "a_existing"


@pytest.mark.asyncio
async def test_cleanup_terminal_extension_branch_supersedes_queued_prefixes(tmp_path: Path) -> None:
    """Task terminal finalize, extension branch: passes file_extension AND supersede_queued_prefixes=True."""
    webm = tmp_path / "playwright.webm"
    webm.write_bytes(b"video")
    video_artifacts = [
        VideoArtifact(
            video_path=str(webm),
            video_artifact_id="a_existing",
            video_data=b"video",
            video_file_extension="mp4",
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
        file_extension="mp4",
        supersede_queued_prefixes=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", [None, "mp4"])
async def test_workflow_persist_terminal_supersedes_queued_prefixes(tmp_path: Path, extension: str | None) -> None:
    """Workflow/task-v2/code-block terminal finalize supersedes queued prefixes on both branches."""
    webm = tmp_path / "session.webm"
    webm.write_bytes(b"video")
    video_artifacts = [
        VideoArtifact(
            video_path=str(webm),
            video_artifact_id="a_existing",
            video_data=b"video",
            video_file_extension=extension,
        )
    ]
    workflow = SimpleNamespace(workflow_id="wf_1")
    workflow_run = SimpleNamespace(workflow_run_id="wfr_1", organization_id="o_1")

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock(return_value="task_1")
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()
        mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks = AsyncMock()

        await WorkflowService().persist_video_data(_browser_state(), workflow, workflow_run)

    expected: dict = {
        "artifact_id": "a_existing",
        "organization_id": "o_1",
        "data": b"video",
        "supersede_queued_prefixes": True,
    }
    if extension is not None:
        expected["file_extension"] = extension
    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_awaited_once_with(**expected)


@pytest.mark.asyncio
async def test_cleanup_intermediate_does_not_supersede_queued_prefixes(tmp_path: Path) -> None:
    """Intermediate task cleanup (browser NOT closed): the recording is still growing and its per-step
    prefixes are still legitimately streaming, so it must NOT seal/supersede the live key even though the
    non-finalized snapshot carries a truthy .webm extension (SKY-15288, thread r3918986927)."""
    webm = tmp_path / "playwright.webm"
    webm.write_bytes(b"partial")
    video_artifacts = [
        VideoArtifact(
            video_path=str(webm),
            video_artifact_id="a_existing",
            video_data=b"partial",
            video_file_extension="webm",  # non-finalize snapshots still set a truthy extension
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
            close_browser_on_completion=False,
            last_step=last_step,
            task=task,
        )

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_awaited_once_with(
        artifact_id="a_existing",
        organization_id="o_1",
        data=b"partial",
        file_extension="webm",
        supersede_queued_prefixes=False,
    )


@pytest.mark.asyncio
async def test_workflow_persist_intermediate_does_not_supersede_queued_prefixes(tmp_path: Path) -> None:
    """Intermediate workflow persist (browser NOT closed, persistent/shared session): must not seal the
    live recording key while prefixes are still streaming (SKY-15288, thread r3918986927)."""
    webm = tmp_path / "session.webm"
    webm.write_bytes(b"partial")
    video_artifacts = [
        VideoArtifact(
            video_path=str(webm),
            video_artifact_id="a_existing",
            video_data=b"partial",
            video_file_extension="webm",
        )
    ]
    workflow = SimpleNamespace(workflow_id="wf_1")
    workflow_run = SimpleNamespace(workflow_run_id="wfr_1", organization_id="o_1")

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock(return_value="task_1")
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()
        mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks = AsyncMock()

        await WorkflowService().persist_video_data(
            _browser_state(), workflow, workflow_run, close_browser_on_completion=False
        )

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_awaited_once_with(
        artifact_id="a_existing",
        organization_id="o_1",
        data=b"partial",
        file_extension="webm",
        supersede_queued_prefixes=False,
    )


@pytest.mark.asyncio
async def test_cleanup_skips_stale_update_when_registered_path_missing(tmp_path: Path) -> None:
    """Task terminal finalize: a registered recording whose local file has vanished must NOT overwrite the
    newer streamed prefix with the stale cached bytes get_video_artifacts could no longer refresh
    (SKY-15288)."""
    missing = tmp_path / "gone.webm"
    assert not missing.exists()
    video_artifacts = [
        VideoArtifact(
            video_path=str(missing),
            video_artifact_id="a_existing",
            video_data=b"stale-cached-bytes",
            video_file_extension="webm",
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

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_awaited()
    recording_calls = [
        c
        for c in mock_app.ARTIFACT_MANAGER.create_artifact.await_args_list
        if c.kwargs.get("artifact_type") == ArtifactType.RECORDING
    ]
    assert recording_calls == []
    assert video_artifacts[0].video_artifact_id == "a_existing"


@pytest.mark.asyncio
async def test_workflow_persist_skips_stale_update_when_registered_path_missing(tmp_path: Path) -> None:
    """Workflow terminal finalize: same missing-path preservation guard as the task path (SKY-15288)."""
    missing = tmp_path / "gone.webm"
    assert not missing.exists()
    video_artifacts = [
        VideoArtifact(
            video_path=str(missing),
            video_artifact_id="a_existing",
            video_data=b"stale-cached-bytes",
            video_file_extension="webm",
        )
    ]
    workflow = SimpleNamespace(workflow_id="wf_1")
    workflow_run = SimpleNamespace(workflow_run_id="wfr_1", organization_id="o_1")

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock(return_value="task_1")
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()
        mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks = AsyncMock()

        await WorkflowService().persist_video_data(_browser_state(), workflow, workflow_run)

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_not_awaited()
    mock_app.ARTIFACT_MANAGER.create_artifact.assert_not_awaited()
    mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks.assert_not_awaited()
    assert video_artifacts[0].video_artifact_id == "a_existing"


@pytest.mark.asyncio
async def test_cleanup_uploads_registered_vendor_bytes_when_path_is_none() -> None:
    """Vendor/CDP recordings register with no local path and supply bytes out of band; the missing-path
    guard keys on a truthy-but-absent path, so it must NOT swallow a pathless upload (SKY-15288)."""
    video_artifacts = [VideoArtifact(video_path=None, video_artifact_id="a_existing", video_data=b"vendor-bytes")]
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
        data=b"vendor-bytes",
        supersede_queued_prefixes=True,
    )


@pytest.mark.asyncio
async def test_workflow_persist_uploads_registered_vendor_bytes_when_path_is_none() -> None:
    """Workflow path mirror of the vendor/CDP pathless-bytes guard (SKY-15288)."""
    video_artifacts = [VideoArtifact(video_path=None, video_artifact_id="a_existing", video_data=b"vendor-bytes")]
    workflow = SimpleNamespace(workflow_id="wf_1")
    workflow_run = SimpleNamespace(workflow_run_id="wfr_1", organization_id="o_1")

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=video_artifacts)
        mock_app.ARTIFACT_MANAGER.update_artifact_data = AsyncMock(return_value="task_1")
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()
        mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks = AsyncMock()

        await WorkflowService().persist_video_data(_browser_state(), workflow, workflow_run)

    mock_app.ARTIFACT_MANAGER.update_artifact_data.assert_awaited_once_with(
        artifact_id="a_existing",
        organization_id="o_1",
        data=b"vendor-bytes",
        supersede_queued_prefixes=True,
    )
    mock_app.ARTIFACT_MANAGER.create_artifact.assert_not_awaited()
    mock_app.ARTIFACT_MANAGER.wait_for_upload_aiotasks.assert_awaited_once_with(["task_1"])


@pytest.mark.asyncio
@pytest.mark.parametrize("redaction_enabled", [False, True])
async def test_cleanup_gates_har_and_console_redaction_on_run_gate(
    redaction_enabled: bool,
) -> None:
    har_data = json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "request": {
                            "headers": [{"name": "Authorization", "value": "Bearer token"}],
                        }
                    }
                ]
            }
        }
    ).encode()
    console_log = b"console secret: console-secret"
    task = _make_task(workflow_run_id="wr_1")
    browser_state = _browser_state()
    context_manager = SimpleNamespace(
        artifact_redaction_enabled=MagicMock(return_value=redaction_enabled),
        get_secret_values_for_run=MagicMock(return_value={"console-secret"}),
        runtime_secret_values_for_artifacts=MagicMock(return_value=set()),
    )

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER = context_manager
        mock_app.BROWSER_MANAGER.cleanup_for_task = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[])
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(return_value=har_data)
        mock_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=console_log)
        mock_app.ARTIFACT_MANAGER.create_task_archive = AsyncMock()

        await ForgeAgent().cleanup_browser_and_create_artifacts(
            close_browser_on_completion=True,
            last_step=_make_step(),
            task=task,
        )

    entries = mock_app.ARTIFACT_MANAGER.create_task_archive.await_args.kwargs["entries"]
    stored_har = entries["har.har"][1]
    stored_console_log = entries["browser_console.log"][1]
    context_manager.artifact_redaction_enabled.assert_called_once_with("wr_1")
    if redaction_enabled:
        assert REDACTED_SECRET_PLACEHOLDER.encode() in stored_har
        assert b"Bearer token" not in stored_har
        assert REDACTED_SECRET_PLACEHOLDER.encode() in stored_console_log
        assert b"console-secret" not in stored_console_log
        context_manager.get_secret_values_for_run.assert_called_once_with("wr_1")
    else:
        # Opted-out with no runtime secrets registered: bytes pass through, and only the
        # runtime floor (not the gated per-run set) was consulted.
        assert stored_har == har_data
        assert stored_console_log == console_log
        context_manager.get_secret_values_for_run.assert_not_called()
        context_manager.runtime_secret_values_for_artifacts.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_floors_runtime_secret_when_opted_out() -> None:
    # A runtime-resolved code must not survive into task HAR/console even without the
    # per-run Mask-Secrets opt-in.
    har_data = json.dumps(
        {"log": {"entries": [{"request": {"headers": [{"name": "X-Code", "value": "code 424242"}]}}]}}
    ).encode()
    console_log = b"typed code 424242"
    task = _make_task(workflow_run_id="wr_1")
    browser_state = _browser_state()
    context_manager = SimpleNamespace(
        artifact_redaction_enabled=MagicMock(return_value=False),
        get_secret_values_for_run=MagicMock(side_effect=AssertionError("gated set must not be consulted")),
        runtime_secret_values_for_artifacts=MagicMock(return_value={"424242"}),
    )

    with patch("skyvern.forge.agent.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER = context_manager
        mock_app.BROWSER_MANAGER.cleanup_for_task = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[])
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(return_value=har_data)
        mock_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=console_log)
        mock_app.ARTIFACT_MANAGER.create_task_archive = AsyncMock()

        await ForgeAgent().cleanup_browser_and_create_artifacts(
            close_browser_on_completion=True,
            last_step=_make_step(),
            task=task,
        )

    entries = mock_app.ARTIFACT_MANAGER.create_task_archive.await_args.kwargs["entries"]
    assert b"424242" not in entries["har.har"][1]
    assert b"424242" not in entries["browser_console.log"][1]
