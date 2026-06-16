"""Tests for the run-scoped RECORDING artifact a code block registers for its browser video.

A code block runs no agent step, so the per-step RECORDING row the agent path creates in
``initialize_execution_state`` never exists and ``_fetch_recording_urls`` finds nothing (the
Recording tab renders empty). ``CodeBlock._ensure_run_recording_artifact`` closes that gap by
registering the row up front; the workflow cleanup's ``persist_video_data`` backfills the bytes.

OSS-synced: synthetic ids and example.* placeholders only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact

_BLOCK_PATH = "skyvern.forge.sdk.workflow.models.block.app"
_MANAGER_PATH = "skyvern.forge.sdk.artifact.manager.app"
_SERVICE_PATH = "skyvern.forge.sdk.workflow.service.app"


def _code_block() -> CodeBlock:
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="recording_output",
        description="recording test output",
        output_parameter_id="op_recording",
        workflow_id="w_recording",
        created_at=now,
        modified_at=now,
    )
    return CodeBlock(label="record_block", code="value = 'ok'", output_parameter=output_parameter)


def _browser_state(video_artifacts: list[VideoArtifact]) -> SimpleNamespace:
    return SimpleNamespace(browser_artifacts=BrowserArtifacts(video_artifacts=video_artifacts))


@pytest.mark.asyncio
async def test_registers_run_scoped_recording_when_video_artifact_has_no_id() -> None:
    video_artifacts = [VideoArtifact(video_path="/tmp/recording.webm", video_data=b"partial-video")]
    browser_state = _browser_state(video_artifacts)
    get_video = AsyncMock(return_value=video_artifacts)
    fake_block = SimpleNamespace(workflow_run_block_id="wrb_1", workflow_run_id="wr_1", organization_id="o_1")
    get_block = AsyncMock(return_value=fake_block)
    create_artifact = AsyncMock(return_value="a_recording")

    with (
        patch(f"{_BLOCK_PATH}.BROWSER_MANAGER.get_video_artifacts", get_video),
        patch(f"{_BLOCK_PATH}.DATABASE.observer.get_workflow_run_block", get_block),
        patch(f"{_BLOCK_PATH}.ARTIFACT_MANAGER.create_workflow_run_block_artifact", create_artifact),
    ):
        await _code_block()._ensure_run_recording_artifact(
            browser_state=browser_state,
            workflow_run_id="wr_1",
            workflow_run_block_id="wrb_1",
            organization_id="o_1",
        )

    get_video.assert_awaited_once()
    assert get_video.call_args.kwargs["finalize"] is False
    assert get_video.call_args.kwargs["workflow_run_id"] == "wr_1"
    create_artifact.assert_awaited_once()
    kwargs = create_artifact.call_args.kwargs
    assert kwargs["artifact_type"] == ArtifactType.RECORDING
    assert kwargs["data"] == b"partial-video"
    assert kwargs["workflow_run_block"] is fake_block
    # The id is stored back so the workflow cleanup updates this exact row.
    assert video_artifacts[0].video_artifact_id == "a_recording"


@pytest.mark.asyncio
async def test_is_idempotent_when_video_artifact_already_registered() -> None:
    video_artifacts = [VideoArtifact(video_path="/tmp/recording.webm", video_artifact_id="a_existing")]
    browser_state = _browser_state(video_artifacts)
    get_video = AsyncMock()
    create_artifact = AsyncMock()

    with (
        patch(f"{_BLOCK_PATH}.BROWSER_MANAGER.get_video_artifacts", get_video),
        patch(f"{_BLOCK_PATH}.ARTIFACT_MANAGER.create_workflow_run_block_artifact", create_artifact),
    ):
        await _code_block()._ensure_run_recording_artifact(
            browser_state=browser_state,
            workflow_run_id="wr_1",
            workflow_run_block_id="wrb_1",
            organization_id="o_1",
        )

    # Already registered (e.g. by an earlier block sharing the browser) — no file read, no insert.
    get_video.assert_not_awaited()
    create_artifact.assert_not_awaited()
    assert video_artifacts[0].video_artifact_id == "a_existing"


@pytest.mark.asyncio
async def test_noop_when_no_video_artifacts() -> None:
    browser_state = _browser_state([])
    get_video = AsyncMock()
    create_artifact = AsyncMock()

    with (
        patch(f"{_BLOCK_PATH}.BROWSER_MANAGER.get_video_artifacts", get_video),
        patch(f"{_BLOCK_PATH}.ARTIFACT_MANAGER.create_workflow_run_block_artifact", create_artifact),
    ):
        await _code_block()._ensure_run_recording_artifact(
            browser_state=browser_state,
            workflow_run_id="wr_1",
            workflow_run_block_id="wrb_1",
            organization_id="o_1",
        )

    get_video.assert_not_awaited()
    create_artifact.assert_not_awaited()


@pytest.mark.asyncio
async def test_artifact_failure_is_swallowed() -> None:
    video_artifacts = [VideoArtifact(video_path="/tmp/recording.webm", video_data=b"partial-video")]
    browser_state = _browser_state(video_artifacts)
    get_video = AsyncMock(return_value=video_artifacts)
    get_block = AsyncMock(
        return_value=SimpleNamespace(workflow_run_block_id="wrb_1", workflow_run_id="wr_1", organization_id="o_1")
    )
    create_artifact = AsyncMock(side_effect=RuntimeError("storage down"))

    with (
        patch(f"{_BLOCK_PATH}.BROWSER_MANAGER.get_video_artifacts", get_video),
        patch(f"{_BLOCK_PATH}.DATABASE.observer.get_workflow_run_block", get_block),
        patch(f"{_BLOCK_PATH}.ARTIFACT_MANAGER.create_workflow_run_block_artifact", create_artifact),
    ):
        # Recording is best-effort: a failure must never surface to the block outcome.
        await _code_block()._ensure_run_recording_artifact(
            browser_state=browser_state,
            workflow_run_id="wr_1",
            workflow_run_block_id="wrb_1",
            organization_id="o_1",
        )

    create_artifact.assert_awaited_once()
    assert video_artifacts[0].video_artifact_id is None


def _recording_artifact(
    *,
    task_id: str | None = None,
    workflow_run_block_id: str | None = None,
    run_id: str | None = None,
) -> Artifact:
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)
    return Artifact(
        created_at=now,
        modified_at=now,
        artifact_id="a_recording",
        artifact_type=ArtifactType.RECORDING,
        uri="s3://bucket/recording.webm",
        organization_id="o_1",
        task_id=task_id,
        workflow_run_block_id=workflow_run_block_id,
        run_id=run_id,
    )


class TestUpdateArtifactDataScopeFallback:
    """A code-block recording artifact has no task_id, so persist_video_data's update must key the
    upload on another scope id instead of raising (which would break workflow cleanup)."""

    @pytest.mark.asyncio
    async def test_keys_upload_by_block_id_when_task_id_absent(self) -> None:
        manager = ArtifactManager()
        artifact = _recording_artifact(workflow_run_block_id="wrb_1", run_id="wr_1")
        with (
            patch(f"{_MANAGER_PATH}.DATABASE.artifacts.get_artifact_by_id", AsyncMock(return_value=artifact)),
            patch(f"{_MANAGER_PATH}.STORAGE.store_artifact", AsyncMock()),
        ):
            await manager.update_artifact_data(artifact_id="a_recording", organization_id="o_1", data=b"video")
            await asyncio.gather(*manager.upload_aiotasks_map["wrb_1"])

        assert list(manager.upload_aiotasks_map.keys()) == ["wrb_1"]

    @pytest.mark.asyncio
    async def test_prefers_task_id_when_present(self) -> None:
        manager = ArtifactManager()
        artifact = _recording_artifact(task_id="tsk_1", workflow_run_block_id="wrb_1")
        with (
            patch(f"{_MANAGER_PATH}.DATABASE.artifacts.get_artifact_by_id", AsyncMock(return_value=artifact)),
            patch(f"{_MANAGER_PATH}.STORAGE.store_artifact", AsyncMock()),
        ):
            await manager.update_artifact_data(artifact_id="a_recording", organization_id="o_1", data=b"video")
            await asyncio.gather(*manager.upload_aiotasks_map["tsk_1"])

        assert "tsk_1" in manager.upload_aiotasks_map
        assert "wrb_1" not in manager.upload_aiotasks_map

    @pytest.mark.asyncio
    async def test_raises_when_no_scope_id_available(self) -> None:
        manager = ArtifactManager()
        artifact = _recording_artifact()
        with (
            patch(f"{_MANAGER_PATH}.DATABASE.artifacts.get_artifact_by_id", AsyncMock(return_value=artifact)),
            patch(f"{_MANAGER_PATH}.STORAGE.store_artifact", AsyncMock()),
        ):
            with pytest.raises(ValueError):
                await manager.update_artifact_data(artifact_id="a_recording", organization_id="o_1", data=b"video")

    @pytest.mark.asyncio
    async def test_returns_scope_key_so_callers_can_drain(self) -> None:
        manager = ArtifactManager()
        artifact = _recording_artifact(workflow_run_block_id="wrb_1", run_id="wr_1")
        with (
            patch(f"{_MANAGER_PATH}.DATABASE.artifacts.get_artifact_by_id", AsyncMock(return_value=artifact)),
            patch(f"{_MANAGER_PATH}.STORAGE.store_artifact", AsyncMock()),
        ):
            key = await manager.update_artifact_data(artifact_id="a_recording", organization_id="o_1", data=b"video")
            await asyncio.gather(*manager.upload_aiotasks_map["wrb_1"])

        assert key == "wrb_1"

    @pytest.mark.asyncio
    async def test_returns_none_when_artifact_missing(self) -> None:
        manager = ArtifactManager()
        with patch(f"{_MANAGER_PATH}.DATABASE.artifacts.get_artifact_by_id", AsyncMock(return_value=None)):
            key = await manager.update_artifact_data(artifact_id="a_missing", organization_id="o_1", data=b"video")

        assert key is None


class TestPersistVideoDataFlushesUploads:
    """The finalized recording upload is keyed on workflow_run_block_id/run_id for a code block, which
    clean_up_workflow's task-id drain never awaits — persist_video_data must flush those keys itself."""

    @pytest.mark.asyncio
    async def test_flushes_the_keys_it_enqueued(self) -> None:
        video_artifacts = [
            VideoArtifact(video_path="/tmp/recording.webm", video_artifact_id="a_recording", video_data=b"video")
        ]
        get_video = AsyncMock(return_value=video_artifacts)
        update_data = AsyncMock(return_value="wrb_1")
        wait_for_uploads = AsyncMock()
        workflow = SimpleNamespace(workflow_id="w_1")
        workflow_run = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1")

        with (
            patch(f"{_SERVICE_PATH}.BROWSER_MANAGER.get_video_artifacts", get_video),
            patch(f"{_SERVICE_PATH}.ARTIFACT_MANAGER.update_artifact_data", update_data),
            patch(f"{_SERVICE_PATH}.ARTIFACT_MANAGER.wait_for_upload_aiotasks", wait_for_uploads),
        ):
            await WorkflowService().persist_video_data(_browser_state(video_artifacts), workflow, workflow_run)

        update_data.assert_awaited_once()
        wait_for_uploads.assert_awaited_once_with(["wrb_1"])

    @pytest.mark.asyncio
    async def test_skips_drain_when_nothing_enqueued(self) -> None:
        get_video = AsyncMock(return_value=[])
        update_data = AsyncMock(return_value=None)
        wait_for_uploads = AsyncMock()
        workflow = SimpleNamespace(workflow_id="w_1")
        workflow_run = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1")

        with (
            patch(f"{_SERVICE_PATH}.BROWSER_MANAGER.get_video_artifacts", get_video),
            patch(f"{_SERVICE_PATH}.ARTIFACT_MANAGER.update_artifact_data", update_data),
            patch(f"{_SERVICE_PATH}.ARTIFACT_MANAGER.wait_for_upload_aiotasks", wait_for_uploads),
        ):
            await WorkflowService().persist_video_data(_browser_state([]), workflow, workflow_run)

        wait_for_uploads.assert_not_awaited()
