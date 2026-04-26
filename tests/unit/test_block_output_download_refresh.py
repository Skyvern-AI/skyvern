"""Tests for refresh-on-read of block output downloaded_files (SKY-8861 follow-up).

When a task block completes, ``TaskOutput.from_task`` snapshots the current
``downloaded_files`` URL list into the persisted block output. If the URL
captured at execution time was a legacy presigned S3 URL (because the
artifact row didn't exist yet, or the run pre-dates SKY-8861), the API
fetch would otherwise serve that stale URL.

This change persists ``downloaded_file_artifact_ids`` alongside the URLs
and rebuilds ``downloaded_files`` / ``downloaded_file_urls`` from those
IDs on every workflow-run-status response.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.schemas.files import FileInfo


def _make_artifact(artifact_id: str, uri: str, checksum: str | None = "sha-x") -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=ArtifactType.DOWNLOAD,
        uri=uri,
        organization_id="o_1",
        run_id="wr_1",
        checksum=checksum,
        created_at="2026-04-25T00:00:00Z",
        modified_at="2026-04-25T00:00:00Z",
    )


def test_task_output_carries_artifact_ids_through():
    """``TaskOutput.from_task`` extracts artifact_ids from the FileInfo list so
    they survive into the persisted block output."""
    from skyvern.forge.sdk.schemas.tasks import TaskOutput, TaskStatus

    task = MagicMock()
    task.task_id = "tsk_1"
    task.status = TaskStatus.completed
    task.extracted_information = None
    task.failure_reason = None
    task.errors = []
    task.failure_category = None

    files = [
        FileInfo(
            url="https://api.skyvern.com/v1/artifacts/a_1/content?sig=x",
            checksum="sha-1",
            filename="invoice.pdf",
            artifact_id="a_1",
        ),
        FileInfo(
            url="https://api.skyvern.com/v1/artifacts/a_2/content?sig=y",
            checksum="sha-2",
            filename="report.pdf",
            artifact_id="a_2",
        ),
    ]
    output = TaskOutput.from_task(task, downloaded_files=files)

    assert output.downloaded_file_artifact_ids == ["a_1", "a_2"]
    assert output.downloaded_file_urls == [
        "https://api.skyvern.com/v1/artifacts/a_1/content?sig=x",
        "https://api.skyvern.com/v1/artifacts/a_2/content?sig=y",
    ]


def test_task_output_artifact_ids_none_when_files_lack_artifact_id():
    """Legacy FileInfo without artifact_id (older code path or non-DB path) leaves
    ``downloaded_file_artifact_ids`` None — refresh-on-read becomes a no-op for
    these snapshots and the stored URLs are served as-is."""
    from skyvern.forge.sdk.schemas.tasks import TaskOutput, TaskStatus

    task = MagicMock()
    task.task_id = "tsk_1"
    task.status = TaskStatus.completed
    task.extracted_information = None
    task.failure_reason = None
    task.errors = []
    task.failure_category = None

    files = [
        FileInfo(
            url="https://skyvern-uploads.s3.amazonaws.com/.../legacy.pdf?sig=x",
            checksum="sha-1",
            filename="legacy.pdf",
            artifact_id=None,
        ),
    ]
    output = TaskOutput.from_task(task, downloaded_files=files)

    assert output.downloaded_file_artifact_ids is None
    # URL list still populated for backward compat consumers.
    assert output.downloaded_file_urls == [
        "https://skyvern-uploads.s3.amazonaws.com/.../legacy.pdf?sig=x",
    ]


@pytest.mark.asyncio
async def test_refresh_rebuilds_downloaded_files_from_artifact_ids():
    """The refresh walker rebuilds ``downloaded_files`` and ``downloaded_file_urls``
    from ``downloaded_file_artifact_ids`` so a presigned-URL snapshot becomes a
    short signed artifact URL on every API fetch."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    persisted_block_output = {
        "task_id": "tsk_1",
        "task_screenshot_artifact_ids": [],
        "workflow_screenshot_artifact_ids": [],
        "downloaded_file_artifact_ids": ["a_1"],
        "downloaded_files": [
            {
                "url": "https://skyvern-uploads.s3.amazonaws.com/.../stale.pdf?sig=expired",
                "checksum": "sha-1",
                "filename": "stale.pdf",
                "modified_at": "2026-04-23T00:00:00Z",
            }
        ],
        "downloaded_file_urls": [
            "https://skyvern-uploads.s3.amazonaws.com/.../stale.pdf?sig=expired",
        ],
    }

    fresh_artifact = _make_artifact("a_1", "s3://skyvern-uploads/downloads/local/o_1/wr_1/stale.pdf", checksum="sha-1")
    fresh_file_info = FileInfo(
        url="https://api.skyvern.com/v1/artifacts/a_1/content?expiry=fresh&kid=k&sig=s",
        checksum="sha-1",
        filename="stale.pdf",
        modified_at=fresh_artifact.created_at,
        artifact_id="a_1",
    )

    with (
        patch(
            "skyvern.forge.sdk.workflow.service.app.DATABASE.artifacts.get_artifacts_by_ids",
            new=AsyncMock(return_value=[fresh_artifact]),
        ),
        patch(
            "skyvern.forge.sdk.workflow.service._file_infos_from_download_artifacts",
            return_value=[fresh_file_info],
        ),
    ):
        service = WorkflowService()
        refreshed = await service._refresh_output_urls(
            persisted_block_output, organization_id="o_1", workflow_run_id="wr_1"
        )

    assert refreshed["downloaded_files"][0]["url"].startswith("https://api.skyvern.com/v1/artifacts/a_1/content")
    assert refreshed["downloaded_file_urls"] == [
        "https://api.skyvern.com/v1/artifacts/a_1/content?expiry=fresh&kid=k&sig=s"
    ]
    # checksum from the artifact row is preserved.
    assert refreshed["downloaded_files"][0]["checksum"] == "sha-1"


@pytest.mark.asyncio
async def test_refresh_leaves_legacy_outputs_untouched():
    """Block outputs persisted before this change have no
    ``downloaded_file_artifact_ids`` field. Refresh must not invent rows or
    blow them away — leave the legacy URLs in place so they keep working
    until they expire."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    persisted_block_output = {
        "task_id": "tsk_1",
        "task_screenshot_artifact_ids": [],
        "workflow_screenshot_artifact_ids": [],
        # No downloaded_file_artifact_ids field — legacy snapshot.
        "downloaded_files": [
            {
                "url": "https://skyvern-uploads.s3.amazonaws.com/.../legacy.pdf?sig=x",
                "checksum": "sha-1",
                "filename": "legacy.pdf",
                "modified_at": None,
            }
        ],
        "downloaded_file_urls": [
            "https://skyvern-uploads.s3.amazonaws.com/.../legacy.pdf?sig=x",
        ],
    }

    mock_get = AsyncMock()  # must NOT be called

    with patch(
        "skyvern.forge.sdk.workflow.service.app.DATABASE.artifacts.get_artifacts_by_ids",
        mock_get,
    ):
        service = WorkflowService()
        refreshed = await service._refresh_output_urls(
            persisted_block_output, organization_id="o_1", workflow_run_id="wr_1"
        )

    assert refreshed["downloaded_files"][0]["url"] == "https://skyvern-uploads.s3.amazonaws.com/.../legacy.pdf?sig=x"
    mock_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_handles_missing_artifact_rows():
    """Artifact ids stored on the snapshot may not resolve (row deleted by
    data scrubber, etc.). Don't crash — keep the stored URLs as a last resort."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    persisted_block_output = {
        "task_id": "tsk_1",
        "task_screenshot_artifact_ids": [],
        "workflow_screenshot_artifact_ids": [],
        "downloaded_file_artifact_ids": ["a_missing"],
        "downloaded_files": [
            {
                "url": "https://skyvern-uploads.s3.amazonaws.com/.../old.pdf?sig=x",
                "checksum": "sha-1",
                "filename": "old.pdf",
                "modified_at": None,
            }
        ],
        "downloaded_file_urls": [
            "https://skyvern-uploads.s3.amazonaws.com/.../old.pdf?sig=x",
        ],
    }

    with patch(
        "skyvern.forge.sdk.workflow.service.app.DATABASE.artifacts.get_artifacts_by_ids",
        new=AsyncMock(return_value=[]),
    ):
        service = WorkflowService()
        refreshed = await service._refresh_output_urls(
            persisted_block_output, organization_id="o_1", workflow_run_id="wr_1"
        )

    # Falls back to whatever URL was already stored; doesn't blank out.
    assert refreshed["downloaded_files"][0]["url"] == "https://skyvern-uploads.s3.amazonaws.com/.../old.pdf?sig=x"


@pytest.mark.asyncio
async def test_refresh_falls_back_to_workflow_run_lookup_when_artifact_ids_missing():
    """Race / pre-#10580 fallback: snapshot has ``downloaded_files`` with stale
    presigned URLs but ``downloaded_file_artifact_ids`` is null/missing
    (because at block-completion time the artifact rows hadn't been created
    yet). Refresh must look up by workflow_run_id and match by filename so a
    multi-block run doesn't merge sibling blocks' downloads."""
    from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
    from skyvern.forge.sdk.workflow.service import WorkflowService

    persisted_block_output = {
        "task_id": "tsk_1",
        "task_screenshot_artifact_ids": [],
        "workflow_screenshot_artifact_ids": [],
        # No downloaded_file_artifact_ids at all — the bug case from staging.
        "downloaded_file_artifact_ids": None,
        "downloaded_files": [
            {
                "url": "https://skyvern-uploads.s3.amazonaws.com/.../mybook.zip?sig=stale",
                "checksum": "sha-1",
                "filename": "mybook.zip",
                "modified_at": None,
                "artifact_id": None,
            }
        ],
        "downloaded_file_urls": [
            "https://skyvern-uploads.s3.amazonaws.com/.../mybook.zip?sig=stale",
        ],
    }

    matching_artifact = Artifact(
        artifact_id="a_recovered",
        artifact_type=ArtifactType.DOWNLOAD,
        uri="s3://skyvern-uploads/downloads/staging/o_1/wr_1/mybook.zip",
        organization_id="o_1",
        run_id="wr_1",
        checksum="sha-1",
        created_at="2026-04-26T00:00:00Z",
        modified_at="2026-04-26T00:00:00Z",
    )
    sibling_artifact = Artifact(
        artifact_id="a_sibling",
        artifact_type=ArtifactType.DOWNLOAD,
        uri="s3://skyvern-uploads/downloads/staging/o_1/wr_1/other-block-file.zip",
        organization_id="o_1",
        run_id="wr_1",
        checksum="sha-2",
        created_at="2026-04-26T00:00:01Z",
        modified_at="2026-04-26T00:00:01Z",
    )
    fresh_file_info = FileInfo(
        url="https://api.skyvern.com/v1/artifacts/a_recovered/content?sig=fresh",
        checksum="sha-1",
        filename="mybook.zip",
        modified_at=matching_artifact.created_at,
        artifact_id="a_recovered",
    )

    with (
        patch(
            "skyvern.forge.sdk.workflow.service.app.DATABASE.artifacts.list_artifacts_for_run_by_type",
            new=AsyncMock(return_value=[matching_artifact, sibling_artifact]),
        ),
        # Patch the helper at the call site rather than the original symbol —
        # service.py imports the symbol once at module load.
        patch(
            "skyvern.forge.sdk.workflow.service._file_infos_from_download_artifacts",
            return_value=[fresh_file_info],
        ),
    ):
        service = WorkflowService()
        refreshed = await service._refresh_output_urls(
            persisted_block_output, organization_id="o_1", workflow_run_id="wr_1"
        )

    assert len(refreshed["downloaded_files"]) == 1
    assert refreshed["downloaded_files"][0]["url"].startswith(
        "https://api.skyvern.com/v1/artifacts/a_recovered/content"
    )
    assert refreshed["downloaded_file_urls"] == ["https://api.skyvern.com/v1/artifacts/a_recovered/content?sig=fresh"]
    # Sibling artifact must NOT leak in — match was filtered to mybook.zip only.
    assert refreshed["downloaded_files"][0]["filename"] == "mybook.zip"


@pytest.mark.asyncio
async def test_refresh_fallback_skips_when_no_filename():
    """Snapshot has downloaded_files but each entry lacks filename.
    No match key → don't lookup; leave snapshot untouched."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    persisted_block_output = {
        "task_id": "tsk_1",
        "task_screenshot_artifact_ids": [],
        "workflow_screenshot_artifact_ids": [],
        "downloaded_file_artifact_ids": None,
        "downloaded_files": [
            {
                "url": "https://example.com/x",
                "checksum": None,
                "filename": None,
                "modified_at": None,
                "artifact_id": None,
            }
        ],
        "downloaded_file_urls": ["https://example.com/x"],
    }

    mock_list = AsyncMock()  # must NOT be called

    with patch(
        "skyvern.forge.sdk.workflow.service.app.DATABASE.artifacts.list_artifacts_for_run_by_type",
        mock_list,
    ):
        service = WorkflowService()
        refreshed = await service._refresh_output_urls(
            persisted_block_output, organization_id="o_1", workflow_run_id="wr_1"
        )

    assert refreshed["downloaded_files"][0]["url"] == "https://example.com/x"
    mock_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_fallback_skips_when_run_lookup_finds_no_match():
    """Filename in snapshot doesn't match any current run artifact (e.g.,
    legacy run that pre-dates artifact rows entirely). Leave snapshot untouched
    rather than blanking the URL."""
    from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
    from skyvern.forge.sdk.workflow.service import WorkflowService

    persisted_block_output = {
        "task_id": "tsk_1",
        "task_screenshot_artifact_ids": [],
        "workflow_screenshot_artifact_ids": [],
        "downloaded_file_artifact_ids": None,
        "downloaded_files": [
            {
                "url": "https://skyvern-uploads.s3.amazonaws.com/.../legacy.zip?sig=x",
                "checksum": "sha-1",
                "filename": "legacy.zip",
                "modified_at": None,
                "artifact_id": None,
            }
        ],
        "downloaded_file_urls": [
            "https://skyvern-uploads.s3.amazonaws.com/.../legacy.zip?sig=x",
        ],
    }

    different_artifact = Artifact(
        artifact_id="a_other",
        artifact_type=ArtifactType.DOWNLOAD,
        uri="s3://skyvern-uploads/downloads/staging/o_1/wr_1/different.zip",
        organization_id="o_1",
        run_id="wr_1",
        checksum="sha-9",
        created_at="2026-04-26T00:00:00Z",
        modified_at="2026-04-26T00:00:00Z",
    )

    with patch(
        "skyvern.forge.sdk.workflow.service.app.DATABASE.artifacts.list_artifacts_for_run_by_type",
        new=AsyncMock(return_value=[different_artifact]),
    ):
        service = WorkflowService()
        refreshed = await service._refresh_output_urls(
            persisted_block_output, organization_id="o_1", workflow_run_id="wr_1"
        )

    # No match → stored URL preserved.
    assert refreshed["downloaded_files"][0]["url"] == "https://skyvern-uploads.s3.amazonaws.com/.../legacy.zip?sig=x"
