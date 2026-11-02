from __future__ import annotations

import asyncio
import io
import os
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import DownloadFileMaxWaitingTime, DownloadSaveIncompleteError, TaskNotFound
from skyvern.forge import agent as agent_module
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api.files import check_downloading_files_and_wait_for_download_to_complete
from skyvern.forge.sdk.artifact.storage.base import get_download_retry_started_at
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.block import FileDownloadBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.actions.handler import _download_target_path
from skyvern.webeye.actions.models import DetailedAgentStepOutput
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.cdp_download_interceptor import ORIGINAL_FILENAME_MARKER, CDPDownloadInterceptor
from tests.unit._fingerprint_expectations import expected_fingerprint


@pytest.fixture(autouse=True)
def _keyed_fingerprint(fingerprint_secret_key: str) -> str:
    return fingerprint_secret_key


def _finalize_events(cap: list[dict]) -> list[dict]:
    return [e for e in cap if e.get("event") == "download_suffix_finalize_rename"]


def _valid_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("report.txt", b"valid zip")
    return buffer.getvalue()


_VALID_TEST_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _make_task(
    *,
    task_id: str = "task-1",
    organization_id: str = "org-1",
    workflow_run_id: str = "wr-1",
) -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    task.organization_id = organization_id
    task.workflow_run_id = workflow_run_id
    task.browser_session_id = None
    task.status = MagicMock(value="terminated")
    return task


@pytest.mark.asyncio
async def test_finalize_downloaded_files_renames_with_download_suffix(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    rename_mock = MagicMock()

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_files_in_directory", return_value=["uuid-file.zip"]),
        patch("skyvern.forge.agent.rename_file", rename_mock),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
    ):
        renamed = await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix="req-123",
            list_files_before=[],
            randomize_if_missing=False,
        )

    assert renamed == ["uuid-file.zip"]
    rename_mock.assert_called_once_with(os.path.join(download_dir, "uuid-file.zip"), "req-123.zip")


@pytest.mark.asyncio
async def test_finalize_recovers_extension_from_original_name_with_dotted_prefix(tmp_path) -> None:
    agent = ForgeAgent()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    suffix = f"Acme.L.P._{ORIGINAL_FILENAME_MARKER}"
    target_name = "Acme.L.P._export"
    (download_dir / target_name).write_bytes(b"workbook")
    context = SkyvernContext(download_suffix=suffix)
    context.download_suffix_applied_files[target_name] = ("export", suffix)

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch("skyvern.forge.agent.recover_download_extension", return_value=".xlsx") as recover_extension,
    ):
        await agent._finalize_downloaded_files_for_task(
            _make_task(),
            organization_id="org-1",
            download_suffix=suffix,
            list_files_before=[],
            randomize_if_missing=False,
        )

    recover_extension.assert_called_once_with(str(download_dir / target_name), suffix)
    assert (download_dir / f"{target_name}.xlsx").exists()


@pytest.mark.asyncio
async def test_finalize_preserves_explicit_suffix_extension(tmp_path) -> None:
    agent = ForgeAgent()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    target_name = "custom.txt"
    (download_dir / target_name).write_bytes(_VALID_TEST_PDF)
    context = SkyvernContext(download_suffix=target_name)
    context.download_suffix_applied_files[target_name] = ("report.pdf", target_name)

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
    ):
        await agent._finalize_downloaded_files_for_task(
            _make_task(),
            organization_id="org-1",
            download_suffix=target_name,
            list_files_before=[],
            randomize_if_missing=False,
        )

    assert (download_dir / target_name).exists()
    assert not (download_dir / "custom.txt.pdf").exists()


@pytest.mark.asyncio
async def test_finalize_validates_suffix_renamed_file_against_original_filename(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    target_name = "custom.txt"
    (download_dir / target_name).write_bytes(
        b"<!DOCTYPE html><html><head><title>Error</title></head><body><h1>Session expired</h1></body></html>"
    )
    context = SkyvernContext(task_id=task.task_id, download_suffix=target_name)
    context.download_suffix_applied_files[target_name] = ("report.pdf", target_name)

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
    ):
        recorded = await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=target_name,
            list_files_before=[],
            randomize_if_missing=False,
        )

    assert recorded == []
    assert not (download_dir / target_name).exists()
    assert context.consume_download_failures(task.task_id) == ["The requested browser download could not be saved."]


@pytest.mark.asyncio
async def test_finalize_excludes_incomplete_file_created_during_discovery(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    rename_mock = MagicMock()

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch(
            "skyvern.forge.agent.list_files_in_directory",
            return_value=[str(download_dir / "late.txt.crdownload")],
        ),
        patch("skyvern.forge.agent.rename_file", rename_mock),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
    ):
        discovered = await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix="req-123",
            list_files_before=[],
            randomize_if_missing=False,
        )

    assert discovered == []
    rename_mock.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_skips_rename_for_session_file_already_named_by_suffix(tmp_path) -> None:
    # A session download is named at download time by download_suffix, so the watcher syncs it as
    # ``s3://.../req-123.pdf``. Finalize must NOT re-suffix it (which would bump it to req-123_1.pdf).
    agent = ForgeAgent()
    task = _make_task()
    task.browser_session_id = "pbs-1"
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    aws_client = MagicMock()
    aws_client.download_file = AsyncMock(return_value=_VALID_TEST_PDF)
    rename_mock = MagicMock()

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.get_aws_client", return_value=aws_client),
        patch("skyvern.forge.agent.rename_file", rename_mock),
        patch(
            "skyvern.forge.agent.skyvern_context.current",
            return_value=SkyvernContext(
                download_suffix="req-123", download_suffix_applied_files={"req-123.pdf": ("site.pdf", "req-123")}
            ),
        ),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(
            return_value=["s3://bucket/o/pbs-1/req-123.pdf"]
        )
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix="req-123",
            list_files_before=[],
            randomize_if_missing=False,
        )

    rename_mock.assert_not_called()
    assert (download_dir / "req-123.pdf").exists()
    assert not (download_dir / "req-123_1.pdf").exists()


@pytest.mark.asyncio
async def test_finalize_dedupes_two_session_files_sharing_one_suffix(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.browser_session_id = "pbs-1"
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    aws_client = MagicMock()
    aws_client.download_file = AsyncMock(
        side_effect=[_VALID_TEST_PDF, b"%PDF-1.5\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"]
    )

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.get_aws_client", return_value=aws_client),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(
            return_value=[
                "s3://bucket/o/pbs-1/aaaa.pdf",
                "s3://bucket/o/pbs-1/bbbb.pdf",
            ]
        )
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix="req-123",
            list_files_before=[],
            randomize_if_missing=False,
        )

    assert {path.name for path in download_dir.iterdir()} == {"req-123.pdf", "req-123_1.pdf"}
    assert not (download_dir / "aaaa.pdf").exists()
    assert not (download_dir / "bbbb.pdf").exists()


@pytest.mark.asyncio
async def test_finalize_skips_rename_for_local_file_already_named_by_suffix(tmp_path) -> None:
    # A run-directory download named at download time by download_suffix arrives as an absolute path
    # from list_files_in_directory; finalize must not re-suffix it to req-123_1.pdf.
    agent = ForgeAgent()
    task = _make_task()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    (download_dir / "req-123.pdf").write_bytes(_VALID_TEST_PDF)

    rename_mock = MagicMock()
    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.rename_file", rename_mock),
        patch(
            "skyvern.forge.agent.skyvern_context.current",
            return_value=SkyvernContext(
                download_suffix="req-123", download_suffix_applied_files={"req-123.pdf": ("site.pdf", "req-123")}
            ),
        ),
    ):
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix="req-123",
            list_files_before=[],
            randomize_if_missing=False,
        )

    rename_mock.assert_not_called()
    assert (download_dir / "req-123.pdf").exists()
    assert not (download_dir / "req-123_1.pdf").exists()


@pytest.mark.asyncio
async def test_finalize_appends_recovered_extension_to_already_named_file(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    rename_mock = MagicMock()
    file_path = str(download_dir / "ACME_export")

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_files_in_directory", return_value=[file_path]),
        patch("skyvern.forge.agent.recover_download_extension", return_value=".xlsx"),
        patch("skyvern.forge.agent.rename_file", rename_mock),
        patch(
            "skyvern.forge.agent.skyvern_context.current",
            return_value=SkyvernContext(
                download_suffix=f"ACME_{ORIGINAL_FILENAME_MARKER}",
                download_suffix_applied_files={"ACME_export": ("export", f"ACME_{ORIGINAL_FILENAME_MARKER}")},
            ),
        ),
    ):
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=f"ACME_{ORIGINAL_FILENAME_MARKER}",
            list_files_before=[],
            randomize_if_missing=False,
        )

    rename_mock.assert_called_once_with(file_path, "ACME_export.xlsx")


@pytest.mark.asyncio
async def test_finalize_prefixes_site_name_that_matches_template_affixes(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    file_path = str(download_dir / "ACME_report.pdf")
    rename_mock = MagicMock()

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_files_in_directory", return_value=[file_path]),
        patch("skyvern.forge.agent.rename_file", rename_mock),
        patch(
            "skyvern.forge.agent.skyvern_context.current",
            return_value=SkyvernContext(download_suffix=f"ACME_{ORIGINAL_FILENAME_MARKER}"),
        ),
    ):
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=f"ACME_{ORIGINAL_FILENAME_MARKER}",
            list_files_before=[],
            randomize_if_missing=False,
        )

    rename_mock.assert_called_once_with(file_path, "ACME_ACME_report.pdf")


@pytest.mark.asyncio
async def test_finalize_recovered_extension_dedupes_before_rename(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    extensionless_path = str(download_dir / "ACME_export")
    existing_path = str(download_dir / "ACME_export.xlsx")
    (download_dir / "ACME_export").write_bytes(b"new")
    (download_dir / "ACME_export.xlsx").write_bytes(b"existing")
    context = SkyvernContext(
        download_suffix=f"ACME_{ORIGINAL_FILENAME_MARKER}",
        download_suffix_applied_files={"ACME_export": ("export", f"ACME_{ORIGINAL_FILENAME_MARKER}")},
    )

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_files_in_directory", return_value=[extensionless_path, existing_path]),
        patch("skyvern.forge.agent.recover_download_extension", return_value=".xlsx"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
    ):
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=f"ACME_{ORIGINAL_FILENAME_MARKER}",
            list_files_before=[existing_path],
            randomize_if_missing=False,
        )

    assert (download_dir / "ACME_export_1.xlsx").read_bytes() == b"new"
    assert (download_dir / "ACME_export.xlsx").read_bytes() == b"existing"


@pytest.mark.asyncio
async def test_cleanup_task_finalizes_downloads_before_saving(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    last_step = MagicMock()
    last_step.step_id = "step-1"
    call_order: list[str] = []
    session_id = "pbs-cleanup"
    rejected_uri = f"s3://bucket/browser_sessions/{session_id}/downloads/invoice.pdf"
    context = SkyvernContext(task_id=task.task_id, browser_session_id=session_id)

    async def finalize_side_effect(*args, **kwargs):
        call_order.append("rename")
        return ["uuid-file.zip"]

    async def save_side_effect(**kwargs):
        call_order.append("save")

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch.object(agent, "_finalize_downloaded_files_for_task", AsyncMock(side_effect=finalize_side_effect)),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.artifacts.claim_session_download_artifacts_for_run = AsyncMock(return_value=0)
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
        mock_app.STORAGE.save_downloaded_files = AsyncMock(side_effect=save_side_effect)

        await agent.clean_up_task(
            task,
            last_step=last_step,
            need_final_screenshot=False,
            download_suffix="req-123",
            list_files_before=[],
            download_rejections=[rejected_uri],
        )

    assert call_order == ["rename", "save"]
    assert mock_app.DATABASE.artifacts.claim_session_download_artifacts_for_run.await_args.kwargs["excluded_uris"] == [
        rejected_uri
    ]


@pytest.mark.asyncio
async def test_suffix_only_cleanup_does_not_reject_file_without_completion_verdict(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.status = TaskStatus.completed
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    error_page = b"<!DOCTYPE html><html><body><h1>Session expired</h1></body></html>"
    (download_dir / "invoice.pdf").write_bytes(error_page)
    context = SkyvernContext(task_id=task.task_id)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.has_download_interceptor_for_context", return_value=False),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.STORAGE.save_downloaded_files = AsyncMock()

        await agent.clean_up_task(
            task,
            last_step=MagicMock(step_id="step-1"),
            need_final_screenshot=False,
            download_suffix="requested",
            list_files_before=[],
        )

    assert (download_dir / "requested.pdf").read_bytes() == error_page
    assert context.consume_download_failures(task.task_id) == []


@pytest.mark.asyncio
async def test_complete_on_download_fails_when_storage_save_is_partial() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.status = TaskStatus.running
    context = SkyvernContext(task_id=task.task_id)
    update_task = AsyncMock(return_value=task)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch.object(agent, "update_task", update_task),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.STORAGE.save_downloaded_files = AsyncMock(side_effect=DownloadSaveIncompleteError(["file.pdf"]))

        await agent.clean_up_task(
            task,
            last_step=MagicMock(step_id="step-1"),
            need_final_screenshot=False,
            download_completion_pending=True,
        )

    assert update_task.await_args.kwargs["status"] == TaskStatus.failed
    assert "could not be saved" in update_task.await_args.kwargs["failure_reason"]


@pytest.mark.asyncio
async def test_task_v3_cleanup_returns_partial_save_result_without_finalizing() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.status = TaskStatus.running
    feedback: list[str] = []
    update_task = AsyncMock(return_value=task)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=SkyvernContext(task_id=task.task_id)),
        patch.object(agent, "update_task", update_task),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.STORAGE.save_downloaded_files = AsyncMock(side_effect=DownloadSaveIncompleteError(["file.pdf"]))

        await agent.clean_up_task(
            task,
            last_step=MagicMock(step_id="step-1"),
            need_final_screenshot=False,
            download_completion_pending=True,
            download_completion_feedback=feedback,
        )

    assert task.status == TaskStatus.running
    update_task.assert_not_awaited()
    assert feedback == ["Downloaded file 'file.pdf' could not be saved or registered."]


@pytest.mark.asyncio
async def test_task_v3_cleanup_returns_artifact_count_mismatch_without_finalizing() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.status = TaskStatus.running
    interceptor = CDPDownloadInterceptor(network_egress_monitor=MagicMock(), redirect_hop_authorizer=MagicMock())
    interceptor._captured_download_count = 1
    browser_context = MagicMock()
    browser_context._skyvern_cdp_download_interceptor = interceptor
    browser_state = MagicMock(browser_context=browser_context)
    context = SkyvernContext(task_id=task.task_id)
    feedback: list[str] = []
    update_task = AsyncMock(return_value=task)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch.object(agent, "update_task", update_task),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.artifacts.list_artifacts_for_run_by_type = AsyncMock(return_value=[])
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.STORAGE.save_downloaded_files = AsyncMock()

        await agent.clean_up_task(
            task,
            last_step=MagicMock(step_id="step-1"),
            need_final_screenshot=False,
            download_completion_pending=True,
            download_completion_feedback=feedback,
        )

    assert task.status == TaskStatus.running
    update_task.assert_not_awaited()
    assert feedback == ["Only 0 of 1 captured downloads were registered as artifacts."]


@pytest.mark.asyncio
async def test_complete_on_download_fails_when_storage_save_times_out() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.status = TaskStatus.running
    update_task = AsyncMock(return_value=task)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=SkyvernContext(task_id=task.task_id)),
        patch.object(agent, "update_task", update_task),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.STORAGE.save_downloaded_files = AsyncMock(side_effect=asyncio.TimeoutError)

        await agent.clean_up_task(
            task,
            last_step=MagicMock(step_id="step-1"),
            need_final_screenshot=False,
            download_completion_pending=True,
        )

    assert update_task.await_args.kwargs["status"] == TaskStatus.failed
    assert update_task.await_args.kwargs["failure_reason"] == "Timed out while saving downloaded files."


@pytest.mark.asyncio
async def test_complete_on_download_fails_when_captured_file_is_missing_from_artifacts() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.status = TaskStatus.running
    interceptor = CDPDownloadInterceptor(network_egress_monitor=MagicMock(), redirect_hop_authorizer=MagicMock())
    interceptor._captured_download_count = 1
    browser_context = MagicMock()
    browser_context._skyvern_cdp_download_interceptor = interceptor
    browser_state = MagicMock(browser_context=browser_context)
    update_task = AsyncMock(return_value=task)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=SkyvernContext(task_id=task.task_id)),
        patch.object(agent, "update_task", update_task),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.artifacts.list_artifacts_for_run_by_type = AsyncMock(return_value=[])
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.STORAGE.save_downloaded_files = AsyncMock()

        await agent.clean_up_task(
            task,
            last_step=MagicMock(step_id="step-1"),
            need_final_screenshot=False,
            download_completion_pending=True,
        )

    assert update_task.await_args.kwargs["status"] == TaskStatus.failed
    assert update_task.await_args.kwargs["failure_reason"] == (
        "Only 0 of 1 captured downloads were registered as artifacts."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("same_generation", [True, False], ids=["reused-interceptor", "replacement-interceptor"])
async def test_task_v3_cleanup_compares_artifacts_to_task_baseline_and_tally_generation(
    same_generation: bool,
) -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.status = TaskStatus.running
    interceptor = CDPDownloadInterceptor(network_egress_monitor=MagicMock(), redirect_hop_authorizer=MagicMock())
    interceptor._captured_download_count = 9 if same_generation else 2
    tally = interceptor.download_tally()
    download_tally_baseline = tally._replace(
        generation=tally.generation if same_generation else "previous-generation",
        saved=7,
    )
    browser_context = MagicMock()
    browser_context._skyvern_cdp_download_interceptor = interceptor
    browser_state = MagicMock(browser_context=browser_context)
    feedback: list[str] = []
    update_task = AsyncMock(return_value=task)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=SkyvernContext(task_id=task.task_id)),
        patch.object(agent, "update_task", update_task),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        # Two old run artifacts plus one new artifact: the latter still leaves one of this task's
        # two captured downloads unregistered, even though the interceptor was replaced/reset.
        mock_app.DATABASE.artifacts.list_artifacts_for_run_by_type = AsyncMock(return_value=[MagicMock()] * 3)
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.STORAGE.save_downloaded_files = AsyncMock()

        await agent.clean_up_task(
            task,
            last_step=MagicMock(step_id="step-1"),
            need_final_screenshot=False,
            download_completion_pending=True,
            download_completion_feedback=feedback,
            download_artifact_baseline_count=2,
            download_tally_baseline=download_tally_baseline,
        )

    assert task.status == TaskStatus.running
    update_task.assert_not_awaited()
    assert feedback == ["Only 1 of 2 captured downloads were registered as artifacts."]


@pytest.mark.asyncio
async def test_complete_on_download_fails_when_requested_download_failed() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.status = TaskStatus.running
    context = SkyvernContext(task_id=task.task_id)
    reason = "The requested browser download could not be saved."
    context.record_download_failure(task.task_id, reason)
    update_task = AsyncMock(return_value=task)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch("skyvern.forge.agent.has_download_interceptor_for_context", return_value=False),
        patch.object(agent, "update_task", update_task),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.STORAGE.save_downloaded_files = AsyncMock()

        await agent.clean_up_task(
            task,
            last_step=MagicMock(step_id="step-1"),
            need_final_screenshot=False,
            download_completion_pending=True,
        )

    assert update_task.await_args.kwargs["status"] == TaskStatus.failed
    assert update_task.await_args.kwargs["failure_reason"] == reason
    assert context.consume_download_failures(task.task_id) == []


@pytest.mark.asyncio
async def test_complete_on_download_ignores_unsolicited_download_failures() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task.status = TaskStatus.running
    browser_state = MagicMock(browser_context=MagicMock())
    update_task = AsyncMock(return_value=task)
    unsolicited_error = {"error_code": "BROWSER_DOWNLOAD_PARTIAL_FAILURE", "reasoning": "unrequested download"}

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=SkyvernContext(task_id=task.task_id)),
        patch("skyvern.forge.agent.has_download_interceptor_for_context", return_value=True),
        patch("skyvern.forge.agent.consume_unsolicited_download_error_for_context", return_value=unsolicited_error),
        patch.object(agent, "update_task", update_task),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
        mock_app.DATABASE.artifacts.list_artifacts_for_run_by_type = AsyncMock(return_value=[])
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.STORAGE.save_downloaded_files = AsyncMock()

        await agent.clean_up_task(
            task,
            last_step=MagicMock(step_id="step-1"),
            need_final_screenshot=False,
            download_completion_pending=True,
        )

    assert update_task.await_args.kwargs["status"] == TaskStatus.completed
    assert "failure_reason" not in update_task.await_args.kwargs


@pytest.mark.asyncio
async def test_cleanup_task_settles_browser_download_before_finalize_and_save(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    last_step = MagicMock(step_id="step-1")
    release = asyncio.Event()
    handler_published = asyncio.Event()
    interceptor = MagicMock()

    class _Settle:
        async def __aenter__(self) -> None:
            await release.wait()
            handler_published.set()

        async def __aexit__(self, *args: object) -> None:
            return None

    interceptor.settle_browser_downloads.return_value = _Settle()
    browser_context = MagicMock()
    browser_context._skyvern_cdp_download_interceptor = interceptor
    browser_state = MagicMock(browser_context=browser_context)

    async def finalize_side_effect(*args: object, **kwargs: object) -> list[str]:
        assert handler_published.is_set()
        return ["final.txt"]

    async def save_side_effect(**kwargs: object) -> None:
        assert handler_published.is_set()

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch.object(agent, "_finalize_downloaded_files_for_task", AsyncMock(side_effect=finalize_side_effect)),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.STORAGE.save_downloaded_files = AsyncMock(side_effect=save_side_effect)
        cleanup = asyncio.create_task(
            agent.clean_up_task(
                task,
                last_step=last_step,
                need_final_screenshot=False,
                download_suffix="req-123",
                list_files_before=[],
            )
        )
        await asyncio.sleep(0)
        assert not cleanup.done()
        release.set()
        await asyncio.wait_for(cleanup, timeout=2)

    interceptor.settle_browser_downloads.assert_called_once_with()


@pytest.mark.asyncio
async def test_cleanup_timeout_bounds_browser_download_handler_drain() -> None:
    agent = ForgeAgent()
    task = _make_task()
    last_step = MagicMock(step_id="step-1")
    never_release = asyncio.Event()
    interceptor = MagicMock()

    class _HangingSettle:
        async def __aenter__(self) -> None:
            await never_release.wait()

        async def __aexit__(self, *args: object) -> None:
            return None

    interceptor.settle_browser_downloads.return_value = _HangingSettle()
    browser_context = MagicMock()
    browser_context._skyvern_cdp_download_interceptor = interceptor
    browser_state = MagicMock(browser_context=browser_context)
    finalize = AsyncMock()
    save = AsyncMock()

    with (
        patch("skyvern.forge.agent.SAVE_DOWNLOADED_FILES_TIMEOUT", 0.01),
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch.object(agent, "_finalize_downloaded_files_for_task", finalize),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
        mock_app.STORAGE.save_downloaded_files = save
        await asyncio.wait_for(
            agent.clean_up_task(
                task,
                last_step=last_step,
                need_final_screenshot=False,
                download_suffix="req-123",
                list_files_before=[],
            ),
            timeout=0.5,
        )

    finalize.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "download_case",
    [
        "saved",
        "rejected_only",
        "transient_failure",
        "cleanup_only_rejected",
        "cleanup_only_rejected_no_suffix",
        "terminal_download_block",
    ],
    ids=["saved", "rejected_only", "transient", "cleanup_only_rejected", "cleanup_only_no_suffix", "terminal"],
)
async def test_execute_step_complete_on_download_does_not_double_finalize(tmp_path, download_case: str) -> None:
    agent = ForgeAgent()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    task = _make_task()
    context = SkyvernContext(task_id=task.task_id, workflow_run_id=task.workflow_run_id)
    task.status = TaskStatus.running
    task.navigation_goal = "Download invoice"
    task.data_extraction_goal = None
    task.complete_criterion = None
    task.terminate_criterion = None
    task.browser_address = None
    task.max_steps_per_run = None
    task.url = "https://example.com"
    task.proxy_location = None
    task.llm_key = None
    task.task_type = "general"

    step = MagicMock()
    step.step_id = "step-1"
    step.order = 0
    step.retry_index = 0
    step.status = "created"

    organization = MagicMock()
    organization.organization_id = task.organization_id
    organization.max_steps_per_run = None

    task_block = MagicMock()
    task_block.complete_on_download = True
    task_block.download_timeout = None
    task_block.download_suffix = None if download_case == "cleanup_only_rejected_no_suffix" else "req-123"

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)

    agent_step_calls = 0

    async def agent_step_side_effect(*args, **kwargs):
        nonlocal agent_step_calls
        agent_step_calls += 1
        if download_case == "rejected_only":
            (download_dir / "invoice.pdf").write_bytes(
                b"<!DOCTYPE html><html><head><title>Error</title></head><body><h1>Session expired</h1></body></html>"
            )
        elif download_case == "saved":
            (download_dir / "uuid-file.zip").write_bytes(_valid_zip_bytes())
        elif download_case in ("cleanup_only_rejected", "cleanup_only_rejected_no_suffix", "terminal_download_block"):
            step.status = StepStatus.completed
        elif agent_step_calls == 1:
            context.record_download_failure(task.task_id, "capture_failed")
            step.status = StepStatus.failed
        else:
            step.status = StepStatus.created
        return step, DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
            cua_response=None,
        )

    async def update_step_side_effect(step_obj, *args, **kwargs):
        if "status" in kwargs:
            step_obj.status = kwargs["status"]
        if "is_last" in kwargs:
            step_obj.is_last = kwargs["is_last"]
        return step_obj

    async def update_task_side_effect(task_obj, *args, **kwargs):
        events.append(("update_task", kwargs.get("status")))
        return task_obj

    update_task = AsyncMock(side_effect=update_task_side_effect)
    agent_step = AsyncMock(side_effect=agent_step_side_effect)
    handle_failed_step = AsyncMock(return_value=step)
    events: list[tuple[str, object]] = []
    real_clean_up_task = agent.clean_up_task

    async def clean_up_task_side_effect(*args, **kwargs):
        events.append(("clean_up_task", kwargs.get("download_completion_pending")))
        if download_case in ("cleanup_only_rejected", "cleanup_only_rejected_no_suffix"):
            (download_dir / "invoice.pdf").write_bytes(
                b"<!DOCTYPE html><html><head><title>Error</title></head><body><h1>Session expired</h1></body></html>"
            )
        return await real_clean_up_task(*args, **kwargs)

    real_handle_completed_step = agent.handle_completed_step

    async def handle_completed_step_side_effect(*args, **kwargs):
        if download_case == "terminal_download_block":
            task.status = TaskStatus.failed
            return False, step, None
        return await real_handle_completed_step(*args, **kwargs)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=context),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=[]),
        patch("skyvern.forge.agent.app") as mock_app,
        patch.object(agent, "initialize_execution_state", AsyncMock(return_value=(step, browser_state, None))),
        patch.object(agent, "agent_step", agent_step),
        patch.object(agent, "update_step", AsyncMock(side_effect=update_step_side_effect)),
        patch.object(agent, "update_task", update_task),
        patch.object(agent, "get_extracted_information_for_task", AsyncMock(return_value=None)),
        patch.object(agent, "handle_completed_step", AsyncMock(side_effect=handle_completed_step_side_effect)),
        patch.object(agent, "clean_up_task", AsyncMock(side_effect=clean_up_task_side_effect)) as clean_up_task,
        patch.object(agent, "handle_failed_step", handle_failed_step),
        patch.object(agent, "_sync_video_artifact_after_step", AsyncMock()),
        patch.object(agent, "update_task_errors_from_detailed_output", AsyncMock(return_value=task)),
    ):
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
        mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
        mock_app.AGENT_FUNCTION.post_step_execution = AsyncMock()
        mock_app.AGENT_FUNCTION.gate_step_completion = AsyncMock(return_value=True)
        mock_app.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=None)
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])

        await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            task_block=task_block,
            close_browser_on_completion=True,
            complete_verification=True,
            engine=RunEngine.skyvern_v1,
        )

    if download_case == "rejected_only":
        assert list(download_dir.iterdir()) == []
        assert clean_up_task.await_args.kwargs["download_rejections"] == [str(download_dir / "invoice.pdf")]
        assert any(
            call.kwargs.get("status") == TaskStatus.failed
            and "The requested browser download could not be saved." in call.kwargs.get("failure_reason", "")
            for call in update_task.await_args_list
        )
    elif download_case in ("cleanup_only_rejected", "cleanup_only_rejected_no_suffix"):
        assert list(download_dir.iterdir()) == []
        assert not any(call.kwargs.get("status") == TaskStatus.completed for call in update_task.await_args_list)
        assert any(
            call.kwargs.get("status") == TaskStatus.failed
            and "The requested browser download could not be saved." in call.kwargs.get("failure_reason", "")
            for call in update_task.await_args_list
        )
    elif download_case == "terminal_download_block":
        assert clean_up_task.await_args.kwargs["download_completion_pending"] is False
    elif download_case == "saved":
        assert (download_dir / "req-123.zip").exists()
        assert not (download_dir / "req-123_1.zip").exists()
        assert not (download_dir / "uuid-file.zip").exists()
    else:
        assert agent_step.await_count == 2
        handle_failed_step.assert_awaited_once()
        assert not any(call.kwargs.get("download_completion_pending") for call in clean_up_task.await_args_list)
    if download_case in ("saved", "rejected_only", "cleanup_only_rejected", "cleanup_only_rejected_no_suffix"):
        assert clean_up_task.await_args.kwargs["download_completion_pending"] is True
        assert events[0] == ("clean_up_task", True)


@pytest.mark.asyncio
async def test_execute_step_reuses_initial_download_baseline_across_recursive_steps(tmp_path) -> None:
    agent = ForgeAgent()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    task = _make_task()
    task.status = SimpleNamespace(value="running")
    task.navigation_goal = "Download invoice"
    task.data_extraction_goal = None
    task.complete_criterion = None
    task.terminate_criterion = None
    task.browser_address = None
    task.max_steps_per_run = None
    task.url = "https://example.com"
    task.proxy_location = None
    task.llm_key = None
    task.task_type = "general"

    step1 = MagicMock()
    step1.step_id = "step-1"
    step1.order = 0
    step1.retry_index = 0
    step1.status = "created"

    step2 = MagicMock()
    step2.step_id = "step-2"
    step2.order = 1
    step2.retry_index = 0
    step2.status = "created"

    organization = MagicMock()
    organization.organization_id = task.organization_id
    organization.max_steps_per_run = None

    task_block = MagicMock()
    task_block.complete_on_download = False
    task_block.download_timeout = None
    task_block.download_suffix = f"Fund_{ORIGINAL_FILENAME_MARKER}"

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)
    context = SkyvernContext(task_id=task.task_id, download_suffix=task_block.download_suffix)

    async def agent_step_side_effect(*args, **kwargs):
        current_step = args[1]
        if current_step.step_id == "step-1":
            _download_target_path(download_dir, "report.pdf").write_bytes(_VALID_TEST_PDF)
            step1.status = "completed"
            return step1, DetailedAgentStepOutput(
                scraped_page=None,
                extract_action_prompt=None,
                llm_response=None,
                actions=None,
                action_results=None,
                actions_and_results=None,
                cua_response=None,
            )
        step2.status = "completed"
        return step2, DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
            cua_response=None,
        )

    async def update_step_side_effect(step_obj, *args, **kwargs):
        return step_obj

    async def update_task_side_effect(task_obj, *args, **kwargs):
        return task_obj

    handle_completed_step_mock = AsyncMock(
        side_effect=[
            (None, None, step2),
            (True, step2, None),
        ]
    )

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=context),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=[]),
        patch.object(
            type(__import__("skyvern.forge.agent", fromlist=["settings"]).settings),
            "execute_all_steps",
            return_value=True,
        ),
        patch("skyvern.forge.agent.app") as mock_app,
        patch.object(
            agent,
            "initialize_execution_state",
            AsyncMock(side_effect=lambda task_obj, step_obj, *_args, **_kwargs: (step_obj, browser_state, None)),
        ),
        patch.object(agent, "agent_step", AsyncMock(side_effect=agent_step_side_effect)),
        patch.object(agent, "update_step", AsyncMock(side_effect=update_step_side_effect)),
        patch.object(agent, "update_task", AsyncMock(side_effect=update_task_side_effect)),
        patch.object(agent, "update_task_errors_from_detailed_output", AsyncMock(return_value=task)),
        patch.object(agent, "handle_completed_step", handle_completed_step_mock),
    ):
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
        mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
        mock_app.AGENT_FUNCTION.post_step_execution = AsyncMock()
        mock_app.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=None)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[])
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])

        await agent.execute_step(
            organization=organization,
            task=task,
            step=step1,
            task_block=task_block,
            close_browser_on_completion=True,
            complete_verification=True,
            engine=RunEngine.skyvern_v1,
        )

    assert (download_dir / "Fund_report.pdf").exists()
    assert not (download_dir / "Fund_Fund_report.pdf").exists()


@pytest.mark.asyncio
async def test_finalize_emits_lineage_with_correlation_fields(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task(task_id="task-A", workflow_run_id="wr-A")
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    stale_ctx = SkyvernContext(task_id="task-0", download_suffix="AllDataExport_ACCT_STALE")

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_files_in_directory", return_value=["uuid-file.zip"]),
        patch("skyvern.forge.agent.rename_file", MagicMock()),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=stale_ctx),
        capture_logs() as cap,
    ):
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix="AllDataExport_ACCT_CURRENT",
            list_files_before=[],
            randomize_if_missing=False,
        )

    events = _finalize_events(cap)
    assert len(events) == 1
    event = events[0]
    assert event["finalize_task_id"] == "task-A"
    assert event["finalize_workflow_run_id"] == "wr-A"
    assert event["pre_rename_filename_fp"] == expected_fingerprint("uuid-file.zip")
    assert event["passed_download_suffix_fp"] == expected_fingerprint("AllDataExport_ACCT_CURRENT")
    assert event["desired_name_fp"] == expected_fingerprint("AllDataExport_ACCT_CURRENT.zip")
    assert event["will_rename"] is True
    # The contextvar suffix is captured alongside the task_block suffix so a divergence (stale context vs
    # late-download) is attributable; here they intentionally differ.
    assert event["context_download_suffix_fp"] == expected_fingerprint("AllDataExport_ACCT_STALE")
    assert event["context_task_id"] == "task-0"
    # Bare task_id/workflow_run_id must NOT be emitted: the forge_log processor overwrites them with the
    # ambient context's values, which under a stale/shared context would mask the finalize target.
    assert "task_id" not in event
    assert "workflow_run_id" not in event


@pytest.mark.asyncio
async def test_finalize_lineage_distinguishes_two_tasks(tmp_path) -> None:
    agent = ForgeAgent()
    records: list[dict] = []
    for account, task_id in (("ACCT_AAA", "task-A"), ("ACCT_BBB", "task-B")):
        download_dir = tmp_path / task_id
        download_dir.mkdir()
        task = _make_task(task_id=task_id, workflow_run_id=f"wr-{task_id}")
        with (
            patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
            patch("skyvern.forge.agent.list_files_in_directory", return_value=["uuid-file.zip"]),
            patch("skyvern.forge.agent.rename_file", MagicMock()),
            patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
            capture_logs() as cap,
        ):
            await agent._finalize_downloaded_files_for_task(
                task,
                organization_id=task.organization_id,
                download_suffix=f"Export_{account}",
                list_files_before=[],
                randomize_if_missing=False,
            )
        records.extend(_finalize_events(cap))

    assert len(records) == 2
    assert {r["finalize_task_id"] for r in records} == {"task-A", "task-B"}
    assert len({r["passed_download_suffix_fp"] for r in records}) == 2  # two iterations distinguishable


@pytest.mark.asyncio
async def test_execute_step_complete_on_download_emits_finalize_lineage(tmp_path) -> None:
    agent = ForgeAgent()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    suffix = "AllDataExport_ACCT_ZZZ"
    real_ctx = SkyvernContext(task_id="task-1", workflow_run_id="wr-1")

    task = _make_task()
    task.status = SimpleNamespace(value="running")
    task.navigation_goal = "Download invoice"
    task.data_extraction_goal = None
    task.complete_criterion = None
    task.terminate_criterion = None
    task.browser_address = None
    task.max_steps_per_run = None
    task.url = "https://example.com"
    task.proxy_location = None
    task.llm_key = None
    task.task_type = "general"

    step = MagicMock()
    step.step_id = "step-1"
    step.order = 0
    step.retry_index = 0
    step.status = "created"

    organization = MagicMock()
    organization.organization_id = task.organization_id
    organization.max_steps_per_run = None

    task_block = MagicMock()
    task_block.label = "bill_usage_download"
    task_block.complete_on_download = True
    task_block.download_timeout = None
    task_block.download_suffix = suffix

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)

    async def agent_step_side_effect(*args, **kwargs):
        (download_dir / "uuid-file.zip").write_bytes(_valid_zip_bytes())
        return step, DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
            cua_response=None,
        )

    async def update_step_side_effect(step_obj, *args, **kwargs):
        if "status" in kwargs:
            step_obj.status = kwargs["status"]
        if "is_last" in kwargs:
            step_obj.is_last = kwargs["is_last"]
        return step_obj

    async def update_task_side_effect(task_obj, *args, **kwargs):
        return task_obj

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=real_ctx),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=real_ctx),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=[]),
        patch("skyvern.forge.agent.app") as mock_app,
        patch.object(agent, "initialize_execution_state", AsyncMock(return_value=(step, browser_state, None))),
        patch.object(agent, "agent_step", AsyncMock(side_effect=agent_step_side_effect)),
        patch.object(agent, "update_step", AsyncMock(side_effect=update_step_side_effect)),
        patch.object(agent, "update_task", AsyncMock(side_effect=update_task_side_effect)),
        patch.object(agent, "update_task_errors_from_detailed_output", AsyncMock(return_value=task)),
        capture_logs() as cap,
    ):
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
        mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
        mock_app.AGENT_FUNCTION.post_step_execution = AsyncMock()
        mock_app.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=None)
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])

        await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            task_block=task_block,
            close_browser_on_completion=True,
            complete_verification=True,
            engine=RunEngine.skyvern_v1,
        )

    finalize_events = _finalize_events(cap)
    assert finalize_events, "finalize lineage was not emitted through the real complete_on_download path"

    event = finalize_events[0]
    assert event["finalize_task_id"] == "task-1"
    # The finalize lineage consumes the task_block-derived suffix and records the contextvar value too,
    # so a task_block-vs-context divergence is attributable at the point the filename is decided.
    assert event["passed_download_suffix_fp"] == expected_fingerprint(suffix)
    assert event["context_download_suffix_fp"] == expected_fingerprint(suffix)
    assert (download_dir / f"{suffix}.zip").exists()


@pytest.mark.asyncio
async def test_wait_for_in_flight_downloads_caps_timeout_and_skips_exhausted_paths() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task_block = SimpleNamespace(download_timeout=500.0)

    wait_mock = AsyncMock()
    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value="/tmp/downloads"),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=["a.pdf", "b.pdf"]),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.wait_for_download_finished", wait_mock),
    ):
        exhausted = {"a.pdf"}
        await agent._wait_for_in_flight_downloads(
            task, task_block, task.organization_id, timeout_cap=30.0, exhausted=exhausted
        )

    # The block's 500s download_timeout is capped to the caller's 30s remaining budget, and the
    # already-exhausted path is excluded rather than re-awaited for its full timeout.
    wait_mock.assert_awaited_once()
    assert wait_mock.await_args.kwargs["downloading_files"] == ["b.pdf"]
    assert wait_mock.await_args.kwargs["timeout"] == 30.0


@pytest.mark.asyncio
async def test_wait_for_in_flight_downloads_skips_entirely_when_cap_is_spent() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task_block = SimpleNamespace(download_timeout=None)

    wait_mock = AsyncMock()
    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value="/tmp/downloads"),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=["a.pdf"]),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.wait_for_download_finished", wait_mock),
    ):
        await agent._wait_for_in_flight_downloads(task, task_block, task.organization_id, timeout_cap=0.0)

    wait_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_in_flight_downloads_adds_timed_out_paths_to_exhausted() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task_block = SimpleNamespace(download_timeout=None)

    wait_mock = AsyncMock(side_effect=DownloadFileMaxWaitingTime(downloading_files=["stuck.pdf"]))
    exhausted: set[str] = set()
    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value="/tmp/downloads"),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=["stuck.pdf"]),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.wait_for_download_finished", wait_mock),
    ):
        await agent._wait_for_in_flight_downloads(task, task_block, task.organization_id, exhausted=exhausted)

    assert exhausted == {"stuck.pdf"}


@pytest.fixture
def fast_in_flight_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "_IN_FLIGHT_DOWNLOAD_CANCEL_POLL_SECONDS", 0.01)


@pytest.mark.usefixtures("fast_in_flight_poll")
@pytest.mark.asyncio
async def test_wait_for_in_flight_downloads_cancels_promptly_when_should_cancel_fires() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task_block = SimpleNamespace(download_timeout=None)

    was_cancelled = False

    async def _slow_wait_for_download_finished(**_kwargs) -> None:
        nonlocal was_cancelled
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            was_cancelled = True
            raise

    should_cancel_calls = 0

    async def _should_cancel() -> bool:
        nonlocal should_cancel_calls
        should_cancel_calls += 1
        return should_cancel_calls >= 2

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value="/tmp/downloads"),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=["a.pdf"]),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.wait_for_download_finished", _slow_wait_for_download_finished),
    ):
        start = time.monotonic()
        cancelled = await agent._wait_for_in_flight_downloads(
            task, task_block, task.organization_id, should_cancel=_should_cancel
        )
        elapsed = time.monotonic() - start

    assert elapsed < 5
    assert was_cancelled is True
    assert cancelled is True


@pytest.mark.asyncio
async def test_wait_for_in_flight_downloads_completes_normally_when_should_cancel_never_fires() -> None:
    agent = ForgeAgent()
    task = _make_task()
    task_block = SimpleNamespace(download_timeout=None)

    completed = False

    async def _fast_wait_for_download_finished(**_kwargs) -> None:
        nonlocal completed
        completed = True

    async def _should_cancel() -> bool:
        return False

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value="/tmp/downloads"),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=["a.pdf"]),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.wait_for_download_finished", _fast_wait_for_download_finished),
    ):
        cancelled = await agent._wait_for_in_flight_downloads(
            task, task_block, task.organization_id, should_cancel=_should_cancel
        )

    assert completed is True
    assert cancelled is False


@pytest.mark.usefixtures("fast_in_flight_poll")
@pytest.mark.asyncio
async def test_native_probe_does_not_delay_user_cancellation_or_outlive_wait(tmp_path) -> None:
    residual = tmp_path / "unfinished.crdownload"
    residual.write_bytes(b"partial")
    probe_started = asyncio.Event()
    probe_reaped = asyncio.Event()

    async def stalled_probe() -> bool:
        probe_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            probe_reaped.set()
        return False

    async def should_cancel() -> bool:
        return probe_started.is_set()

    async with asyncio.timeout(4):
        cancelled = await ForgeAgent()._wait_for_download_finished_cancellable(
            downloading_files=[str(residual)],
            timeout=30,
            should_cancel=should_cancel,
            native_failure_quiescent=stalled_probe,
        )
    assert cancelled and probe_reaped.is_set()
    assert residual.read_bytes() == b"partial"


@pytest.mark.asyncio
async def test_an_already_quiescent_native_failure_does_not_wait_a_poll(tmp_path) -> None:
    residual = tmp_path / "unfinished.crdownload"
    residual.write_bytes(b"partial")
    async with asyncio.timeout(0.5):
        cancelled = await ForgeAgent()._wait_for_download_finished_cancellable(
            downloading_files=[str(residual)],
            timeout=30,
            should_cancel=None,
            native_failure_quiescent=AsyncMock(return_value=True),
        )
    assert cancelled is False
    assert residual.read_bytes() == b"partial"


@pytest.mark.usefixtures("fast_in_flight_poll")
@pytest.mark.asyncio
async def test_unknown_native_liveness_preserves_timeout_cap_and_exhausted(tmp_path) -> None:
    residual = tmp_path / "unfinished.crdownload"
    residual.write_bytes(b"partial")
    task = _make_task()
    exhausted: set[str] = set()
    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=tmp_path),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
    ):
        async with asyncio.timeout(3):
            result = await ForgeAgent()._wait_for_in_flight_downloads(
                task,
                SimpleNamespace(download_timeout=30),
                task.organization_id,
                timeout_cap=0.05,
                exhausted=exhausted,
                native_failure_quiescent=AsyncMock(return_value=False),
            )
    assert result is False and exhausted == {str(residual)}
    assert residual.read_bytes() == b"partial"


@pytest.mark.usefixtures("fast_in_flight_poll")
@pytest.mark.asyncio
async def test_native_probe_error_still_reaps_the_file_wait(tmp_path) -> None:
    residual = tmp_path / "unfinished.crdownload"
    residual.write_bytes(b"partial")
    reaped = asyncio.Event()

    async def file_wait(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            reaped.set()

    with patch("skyvern.forge.agent.wait_for_download_finished", file_wait):
        async with asyncio.timeout(4):
            with pytest.raises(RuntimeError, match="probe unavailable"):
                await ForgeAgent()._wait_for_download_finished_cancellable(
                    downloading_files=[str(residual)],
                    timeout=30,
                    should_cancel=None,
                    native_failure_quiescent=AsyncMock(side_effect=RuntimeError("probe unavailable")),
                )
    assert reaped.is_set()
    assert residual.exists()


@pytest.mark.asyncio
async def test_finalize_skips_session_copy_whose_bytes_already_in_run_dir(tmp_path) -> None:
    # A persistent-session blob: download lands in BOTH the session downloads dir and the run dir
    # (the eager blob carve-out). Finalization must not materialize a second physical copy of
    # identical bytes, or FileUploadBlock uploads the same statement twice and the customer's
    # second signed S3 upload URL 403s (SKY-14276).
    agent = ForgeAgent()
    task = _make_task(workflow_run_id="wr-dedupe")
    task.browser_session_id = "pbs-1"

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    statement_bytes = _VALID_TEST_PDF
    (download_dir / "run-copy.pdf").write_bytes(statement_bytes)  # the eager blob carve-out copy

    session_uri = "s3://bucket/browser_sessions/pbs-1/downloads/Statement.pdf"
    aws_client = MagicMock()
    aws_client.download_file = AsyncMock(return_value=statement_bytes)

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.get_aws_client", return_value=aws_client),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[session_uri])
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=None,
            list_files_before=[],
            randomize_if_missing=False,
        )

    remaining = sorted(p.name for p in download_dir.iterdir())
    assert remaining == ["run-copy.pdf"], f"session copy should be deduped by content, got {remaining}"
    assert (download_dir / "run-copy.pdf").read_bytes() == statement_bytes


@pytest.mark.asyncio
async def test_finalize_materializes_session_download_when_run_dir_empty(tmp_path) -> None:
    # Load-bearing: on a non-blob persistent session the run dir is empty (SESSION_DIR suppression),
    # so finalization MUST still materialize the one session download — otherwise FileUploadBlock
    # fails the block with an empty run dir. Content dedupe must not regress this.
    agent = ForgeAgent()
    task = _make_task(workflow_run_id="wr-materialize")
    task.browser_session_id = "pbs-2"

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    session_uri = "s3://bucket/browser_sessions/pbs-2/downloads/OnlyStatement.pdf"
    aws_client = MagicMock()
    aws_client.download_file = AsyncMock(return_value=_VALID_TEST_PDF)

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.get_aws_client", return_value=aws_client),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[session_uri])
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=None,
            list_files_before=[],
            randomize_if_missing=False,
        )

    remaining = sorted(p.name for p in download_dir.iterdir())
    assert remaining == ["OnlyStatement.pdf"], f"the single session download must be materialized, got {remaining}"


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["s3", "gs"])
async def test_finalize_rejects_contradictory_persistent_session_download(tmp_path, scheme: str) -> None:
    agent = ForgeAgent()
    task = _make_task(workflow_run_id="wr-reject-session-download")
    task.browser_session_id = "pbs-reject"
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    session_uri = f"{scheme}://bucket/browser_sessions/pbs-reject/downloads/invoice.pdf"
    error_page = b"<!DOCTYPE html><html><body><h1>Session expired</h1></body></html>"
    context = SkyvernContext(task_id=task.task_id)
    object_client = MagicMock()
    object_client.download_file = AsyncMock(return_value=error_page)
    rejected: list[str] = []

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch("skyvern.forge.agent.get_aws_client", return_value=object_client),
        patch("skyvern.forge.agent.get_gcs_client", return_value=object_client),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[session_uri])
        finalized = await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=None,
            list_files_before=[],
            randomize_if_missing=False,
            rejected_into=rejected,
        )

    assert finalized == []
    assert rejected == [session_uri]
    assert list(download_dir.iterdir()) == []
    assert context.consume_download_failures(task.task_id) == ["The requested browser download could not be saved."]


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["s3", "gs"])
@pytest.mark.parametrize(
    ("file_data", "validate_claimed_type", "expected_finalized", "expected_rejection"),
    [
        (None, True, False, False),
        (b"", True, False, True),
        (None, False, True, False),
    ],
    ids=["unreadable-retryable", "empty-pdf-rejected", "v3-behavior-unchanged"],
)
async def test_finalize_does_not_credit_unreadable_session_download(
    tmp_path,
    scheme: str,
    file_data: bytes | None,
    validate_claimed_type: bool,
    expected_finalized: bool,
    expected_rejection: bool,
) -> None:
    agent = ForgeAgent()
    task = _make_task(workflow_run_id="wr-unreadable-session-download")
    task.browser_session_id = "pbs-unreadable"
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    session_uri = f"{scheme}://bucket/browser_sessions/pbs-unreadable/downloads/invoice.pdf"
    context = SkyvernContext(task_id=task.task_id)
    object_client = MagicMock()
    object_client.download_file = AsyncMock(return_value=file_data)
    rejected: list[str] = []

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch("skyvern.forge.agent.get_aws_client", return_value=object_client),
        patch("skyvern.forge.agent.get_gcs_client", return_value=object_client),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[session_uri])
        finalized = await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=None,
            list_files_before=[],
            randomize_if_missing=False,
            validate_claimed_type=validate_claimed_type,
            rejected_into=rejected,
        )

    assert finalized == ([session_uri] if expected_finalized else [])
    assert list(download_dir.iterdir()) == []
    assert rejected == ([session_uri] if expected_rejection else [])
    assert context.consume_download_failures(task.task_id) == (
        ["The requested browser download could not be saved."] if expected_rejection else []
    )


@pytest.mark.asyncio
async def test_finalize_local_only_skips_checksum_snapshot(tmp_path) -> None:
    # Only s3://, gs:// session candidates consult the run-dir checksum snapshot. A local-only
    # finalization has no such candidate, so finalize must not hash the run dir at all (SKY-14276).
    agent = ForgeAgent()
    task = _make_task()  # browser_session_id = None -> no session-storage candidate
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    checksum_spy = MagicMock(return_value="deadbeef")
    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_files_in_directory", return_value=["uuid-file.zip"]),
        patch("skyvern.forge.agent.rename_file", MagicMock()),
        patch("skyvern.forge.agent.calculate_sha256_for_file", checksum_spy),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
    ):
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix="req-123",
            list_files_before=[],
            randomize_if_missing=False,
        )

    checksum_spy.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_materializes_session_download_matching_baseline_before(tmp_path) -> None:
    # A genuinely-new persistent-session download can happen to share bytes with a baseline file that
    # was already in the run dir before this task (an earlier task's download or a staged input). That
    # baseline sits in list_files_before, so it is NOT this task's eager carve-out duplicate. Finalization
    # must still materialize the new session object (and apply its distinct download_suffix), not skip it
    # by hashing the whole run dir against the baseline (SKY-14276).
    agent = ForgeAgent()
    task = _make_task(workflow_run_id="wr-baseline-collision")
    task.browser_session_id = "pbs-3"

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    shared_bytes = _VALID_TEST_PDF
    baseline_path = download_dir / "baseline-from-earlier-task.pdf"
    baseline_path.write_bytes(shared_bytes)

    session_uri = "s3://bucket/browser_sessions/pbs-3/downloads/NewStatement.pdf"
    aws_client = MagicMock()
    aws_client.download_file = AsyncMock(return_value=shared_bytes)

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.get_aws_client", return_value=aws_client),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[session_uri])
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix="req-999",
            list_files_before=[str(baseline_path)],
            randomize_if_missing=False,
        )

    remaining = sorted(p.name for p in download_dir.iterdir())
    assert remaining == ["baseline-from-earlier-task.pdf", "req-999.pdf"], (
        f"new session download must be materialized under its suffix, not deduped against the baseline, got {remaining}"
    )
    assert (download_dir / "req-999.pdf").read_bytes() == shared_bytes


# Shared identity for the run's current BrowserContext. The credit consumer closes a claimed popup
# iff ``popup.context is browser_state.browser_context``, so a popup that should close and the credit
# browser_state must share this object; a foreign-context popup passes a different one. A MagicMock (not
# a bare object) so it also stands in for the real context where cleanup passes it to helpers.
_RUN_CONTEXT = MagicMock(name="run-browser-context")


def _claimed_popup(url: str = ":", *, context: object = _RUN_CONTEXT) -> MagicMock:
    popup = MagicMock(url=url)
    popup.is_closed.return_value = False
    popup.close = AsyncMock()
    popup.context = context
    return popup


class _ContextAccessRaises:
    """A browser_state whose ``browser_context`` read raises (torn-down/recycled session)."""

    @property
    def browser_context(self) -> object:
        raise RuntimeError("browser context gone")


def test_download_popup_claim_record_dedup_take_clear() -> None:
    """The task-scoped claim registry dedups by exact Page identity and pops/clears by task_id, so a
    claim can never be double-recorded or leak into another task's scope."""
    ctx = SkyvernContext(task_id="t1")
    page = _claimed_popup()
    ctx.record_download_popup_claim("t1", page)
    ctx.record_download_popup_claim("t1", page)  # exact-identity dedup
    assert ctx.download_popup_claims["t1"] == [page]

    other = _claimed_popup()
    ctx.record_download_popup_claim("t2", other)
    assert ctx.take_download_popup_claims("t1") == [page]
    assert "t1" not in ctx.download_popup_claims  # take pops

    ctx.clear_download_popup_claims("t2")
    assert "t2" not in ctx.download_popup_claims
    # taking/clearing a missing key is safe and non-mutating
    assert ctx.take_download_popup_claims("missing") == []
    ctx.clear_download_popup_claims("missing")


def test_discard_download_popup_claim_identity_semantics() -> None:
    """discard_download_popup_claim retires only the exact reused Page: siblings survive (G2), an
    emptied bucket's task key is deleted rather than left as an empty list (G3), and an unclaimed page
    or another task's bucket is never mutated (G4)."""
    ctx = SkyvernContext(task_id="t1")
    q = _claimed_popup()
    r = _claimed_popup()
    other = _claimed_popup()
    ctx.record_download_popup_claim("t1", q)
    ctx.record_download_popup_claim("t1", r)
    ctx.record_download_popup_claim("t2", other)

    # G4: an unclaimed page is a no-op and never mutates any bucket.
    assert ctx.discard_download_popup_claim("t1", _claimed_popup()) is False
    assert ctx.download_popup_claims["t1"] == [q, r]
    assert ctx.download_popup_claims["t2"] == [other]

    # G2: discarding one claim leaves its siblings claimable.
    assert ctx.discard_download_popup_claim("t1", q) is True
    assert ctx.download_popup_claims["t1"] == [r]
    assert ctx.discard_download_popup_claim("t1", q) is False  # already gone

    # G3: emptying the bucket deletes the task key (no empty list left behind).
    assert ctx.discard_download_popup_claim("t1", r) is True
    assert "t1" not in ctx.download_popup_claims

    # G4: the other task's bucket is intact throughout; a missing task is a safe no-op.
    assert ctx.download_popup_claims["t2"] == [other]
    assert ctx.discard_download_popup_claim("missing", other) is False


@pytest.mark.asyncio
async def test_close_credited_download_popups_closes_current_working_page() -> None:
    """Anchor constraint (G1): a current-action claimed popup that BrowserState has already selected as
    its working page still closes at durable credit. The credit consumer must never skip the working
    page — a current-action popup with no later action-entry reuse must close."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-working-page")
    working = _claimed_popup(":")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    browser_state.get_working_page = AsyncMock(return_value=working)  # the popup IS the working page
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, working)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    working.close.assert_awaited_once()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_closes_claimed_popup_and_clears_claim() -> None:
    """After a durable download credit, the exact recorded popup that is still open and still a live
    page in this run is closed (so the next task does not select it as its working page), and the task's
    claims are cleared. The opener is never recorded as a claim, so it is untouched."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-credit")
    popup = _claimed_popup(":")
    opener = _claimed_popup("https://example.test/documents")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, popup)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    popup.close.assert_awaited_once()
    opener.close.assert_not_called()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_closes_committed_popup() -> None:
    """Product contract: a download that opens a new page has that page closed after the download
    finishes REGARDLESS of URL. A claimed popup that committed to a real URL is therefore still closed
    by the late cleanup -- this fails on the pre-fix head that filtered to the ``":"`` marker only."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-committed")
    committed = _claimed_popup("https://example.test/report.pdf")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, committed)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    committed.close.assert_awaited_once()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_skips_foreign_context_popup() -> None:
    """Scope guard: a claimed popup belonging to a different BrowserContext than the run's current one
    — a recycled/replaced context or a page from another run/context — is left untouched. Eligibility
    is structural ownership (``popup.context is browser_state.browser_context``), not scraper-list
    membership."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-foreign")
    foreign = _claimed_popup(":", context=object())  # not the run's current BrowserContext
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, foreign)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    foreign.close.assert_not_called()
    # The claim is still consumed (taken) by the credit seam, so it cannot resurrect on duplicate credit.
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_drops_stale_claim_after_context_replacement() -> None:
    """Context replacement: if the browser context was recycled/replaced before credit, a claimed popup
    bound to the OLD context is dropped, never closed, because its identity no longer matches
    browser_state.browser_context. (Stored Page objects on a dead connection must not be close()d.)"""
    agent = ForgeAgent()
    task = _make_task(task_id="task-ctx-replaced")
    stale = _claimed_popup(":", context=MagicMock(name="old-context"))
    browser_state = MagicMock()
    browser_state.browser_context = MagicMock(name="new-context")  # replacement context
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, stale)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    stale.close.assert_not_called()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_close_failure_does_not_overturn_credit() -> None:
    """A close that raises is swallowed: the consumer never raises into the durable-credit path, the
    claim is still consumed, and every other claimed popup is still closed."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-close-fail")
    failing = _claimed_popup(":")
    failing.close = AsyncMock(side_effect=RuntimeError("close boom"))
    healthy = _claimed_popup("https://example.test/report.pdf")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, failing)
    ctx.record_download_popup_claim(task.task_id, healthy)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)  # must not raise

    failing.close.assert_awaited_once()
    healthy.close.assert_awaited_once()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_context_access_failure_does_not_overturn_credit() -> None:
    """If reading ``browser_state.browser_context`` raises (torn-down/recycled session) the consumer
    must not propagate. Claims were already atomically taken, so they expire without a close and durable
    credit stands; only a bounded ``context_unavailable`` reason is logged, never an exception
    payload."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-ctx-unavailable")
    popup = _claimed_popup(":")
    browser_state = _ContextAccessRaises()
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, popup)

    with (
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        capture_logs() as logs,
    ):
        await agent._close_credited_download_popups(task, browser_state)  # must not raise

    popup.close.assert_not_called()
    assert task.task_id not in ctx.download_popup_claims  # atomically taken, then expired
    event = next(e for e in logs if e.get("skip_reason") == "context_unavailable")
    assert "exc_info" not in event and "exception" not in event, "no exception payload may be logged"


@pytest.mark.asyncio
async def test_close_credited_download_popups_idempotent_on_already_closed() -> None:
    """An already-closed recorded popup is a no-op (idempotent double-close guard, req #2)."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-closed")
    already_closed = _claimed_popup(":")
    already_closed.is_closed.return_value = True
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[already_closed])
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, already_closed)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    already_closed.close.assert_not_called()


@pytest.mark.asyncio
async def test_close_credited_download_popups_closes_scraper_invalid_same_context_popup() -> None:
    """A claimed popup still open and in the run's current BrowserContext must be closed after durable
    credit even when the scraper-oriented list_valid_pages() omits it (PDF-viewer popups are exactly
    the pages scrapers exclude; cleanup liveness and scraper validity are different contracts). A
    list_valid_pages() identity gate would silently skip it, leaving the popup to linger as the next
    task's working page (the SKY-15371 wedge). Eligibility gates on
    ``popup.context is browser_state.browser_context`` instead."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-scraper-invalid")
    browser_context = MagicMock(name="owning-browser-context")
    popup = _claimed_popup(":")
    popup.context = browser_context
    browser_state = MagicMock()
    browser_state.browser_context = browser_context
    # Scraper excludes the wedged popup (the production silent-skip): it is absent from valid pages.
    browser_state.list_valid_pages = AsyncMock(return_value=[])
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, popup)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    popup.close.assert_awaited_once()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_closes_same_context_popup_when_listing_raises() -> None:
    """A listing failure must not strand a claimed same-context popup. A fail-open ``except: return``
    around list_valid_pages() would abandon the close entirely; the consumer never calls
    list_valid_pages(), so a listing fault cannot overturn the close."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-list-raises")
    browser_context = MagicMock(name="owning-browser-context")
    popup = _claimed_popup(":")
    popup.context = browser_context
    browser_state = MagicMock()
    browser_state.browser_context = browser_context
    browser_state.list_valid_pages = AsyncMock(side_effect=RuntimeError("scrape listing failed"))
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, popup)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    popup.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_credited_download_popups_no_claim_is_noop() -> None:
    """With no recorded claim (no credited download popup) nothing is closed (req #5)."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-empty")
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[_claimed_popup(":")])
    ctx = SkyvernContext(task_id=task.task_id)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    browser_state.list_valid_pages.assert_not_awaited()


@pytest.mark.asyncio
async def test_clean_up_task_clears_download_popup_claims_even_on_early_error() -> None:
    """Task-terminal expiry: clean_up_task drops the task's popup claims at the very top, so a claim
    never survives into a later task/run/persistent-session -- proven here on the early-error path
    (DB refresh fails) which stands in for cancellation/exception exits."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-terminal")
    step = MagicMock()
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, _claimed_popup(":"))

    mock_app = MagicMock()
    mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=None)  # forces TaskNotFound right after the clear

    with (
        patch("skyvern.forge.agent.app", mock_app),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
    ):
        with pytest.raises(TaskNotFound):
            await agent.clean_up_task(task=task, last_step=step)

    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_bounds_a_hung_close() -> None:
    """A hung ``popup.close()`` must not block task completion: the late close is bounded by
    BROWSER_PAGE_CLOSE_TIMEOUT so the consumer always returns and the claim is consumed. Pre-fix the
    close was awaited unbounded and a never-resolving close would hang update_step/update_task."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-hang")
    marker = _claimed_popup(":")

    async def _never_resolves() -> None:
        await asyncio.Event().wait()

    marker.close = AsyncMock(side_effect=_never_resolves)
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, marker)

    with (
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.agent.BROWSER_PAGE_CLOSE_TIMEOUT", 0.01, create=True),
    ):
        # asyncio.wait_for turns the pre-fix unbounded hang into a bounded test failure.
        await asyncio.wait_for(agent._close_credited_download_popups(task, browser_state), timeout=2)

    marker.close.assert_awaited_once()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_batch_bounded_across_multiple_hung_pages() -> None:
    """A same-context popup storm must not delay the credited-task handoff by N x timeout: the whole
    close batch shares one monotonic deadline. Two claimed pages both hang; the first close consumes
    the budget, the second is never attempted, elapsed is ~one timeout (not two), and both claims are
    consumed. Pre-fix each page got a full BROWSER_PAGE_CLOSE_TIMEOUT, so the batch ran ~two timeouts
    and attempted the second page."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-batch-hang")
    timeout_s = 0.2

    async def _never_resolves() -> None:
        await asyncio.Event().wait()

    first = _claimed_popup(":")
    first.close = AsyncMock(side_effect=_never_resolves)
    second = _claimed_popup(":")
    second.close = AsyncMock(side_effect=_never_resolves)
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, first)
    ctx.record_download_popup_claim(task.task_id, second)

    with (
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.agent.BROWSER_PAGE_CLOSE_TIMEOUT", timeout_s),
    ):
        started = time.monotonic()
        await asyncio.wait_for(agent._close_credited_download_popups(task, browser_state), timeout=5)
        elapsed = time.monotonic() - started

    # One shared deadline: the batch is bounded to ~one timeout, not one-per-page.
    assert elapsed < timeout_s * 1.75, f"batch took {elapsed:.3f}s, expected ~one {timeout_s}s timeout"
    first.close.assert_awaited_once()
    second.close.assert_not_called()  # budget exhausted by the first hung close -> not attempted
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_fast_first_then_hung_stays_batch_bounded() -> None:
    """A fast successful close followed by a hung close keeps the first success and stays bounded to the
    single shared deadline rather than first-close-time plus a full per-page timeout."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-fast-then-hang")
    timeout_s = 0.2

    async def _never_resolves() -> None:
        await asyncio.Event().wait()

    fast = _claimed_popup(":")  # default AsyncMock close resolves immediately
    hung = _claimed_popup(":")
    hung.close = AsyncMock(side_effect=_never_resolves)
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, fast)
    ctx.record_download_popup_claim(task.task_id, hung)

    with (
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.agent.BROWSER_PAGE_CLOSE_TIMEOUT", timeout_s),
    ):
        started = time.monotonic()
        await asyncio.wait_for(agent._close_credited_download_popups(task, browser_state), timeout=5)
        elapsed = time.monotonic() - started

    fast.close.assert_awaited_once()  # first success preserved
    assert elapsed < timeout_s * 1.75, f"batch took {elapsed:.3f}s"
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_hung_close_gets_only_remaining_budget() -> None:
    """A slow-but-successful first close followed by a hung close must stay within the single shared
    deadline: the hung close is bounded to the budget the first close left, not a fresh full timeout.
    With per-page full timeouts the batch would reach first-close-time plus a whole timeout."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-remaining-budget")
    timeout_s = 0.3
    slow_first_s = 0.25  # consumes most of the shared budget, but succeeds

    async def _slow_success() -> None:
        await asyncio.sleep(slow_first_s)

    async def _never_resolves() -> None:
        await asyncio.Event().wait()

    slow = _claimed_popup(":")
    slow.close = AsyncMock(side_effect=_slow_success)
    hung = _claimed_popup(":")
    hung.close = AsyncMock(side_effect=_never_resolves)
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, slow)
    ctx.record_download_popup_claim(task.task_id, hung)

    with (
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.agent.BROWSER_PAGE_CLOSE_TIMEOUT", timeout_s),
    ):
        started = time.monotonic()
        await asyncio.wait_for(agent._close_credited_download_popups(task, browser_state), timeout=5)
        elapsed = time.monotonic() - started

    slow.close.assert_awaited_once()
    # Shared deadline caps the whole batch at ~one timeout. Per-page full timeout would give
    # slow_first_s + timeout_s (~0.55s); the remaining-budget bound keeps it near timeout_s.
    assert elapsed < timeout_s * 1.4, f"batch took {elapsed:.3f}s, hung close was not capped to remaining budget"
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_execute_step_complete_on_download_closes_claimed_popup_before_handoff(tmp_path) -> None:
    """Real outer-path regression: driving the actual v1 execute_step complete_on_download credit
    branch, the exact recorded ``":"`` marker popup is closed before the completed-task handoff
    (update_step/update_task). Removing the single _close_credited_download_popups call makes this
    fail (the marker is never closed)."""
    agent = ForgeAgent()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    task = _make_task(task_id="task-outer")
    task.status = TaskStatus.running
    task.navigation_goal = "Download invoice"
    task.data_extraction_goal = None
    task.complete_criterion = None
    task.terminate_criterion = None
    task.browser_address = None
    task.max_steps_per_run = None
    task.url = "https://example.com"
    task.proxy_location = None
    task.llm_key = None
    task.task_type = "general"

    step = MagicMock()
    step.step_id = "step-1"
    step.order = 0
    step.retry_index = 0
    step.status = "created"

    organization = MagicMock()
    organization.organization_id = task.organization_id
    organization.max_steps_per_run = None

    task_block = MagicMock()
    task_block.complete_on_download = True
    task_block.download_timeout = None
    task_block.download_suffix = "req-123"

    order: list[str] = []

    marker = _claimed_popup(":")

    async def _close_marker() -> None:
        order.append("close")

    marker.close = AsyncMock(side_effect=_close_marker)
    opener = _claimed_popup("https://example.com/documents")

    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    browser_state.get_working_page = AsyncMock(return_value=None)

    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, marker)

    async def agent_step_side_effect(*args, **kwargs):
        (download_dir / "uuid-file.zip").write_bytes(_valid_zip_bytes())
        return step, DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
            cua_response=None,
        )

    async def update_step_side_effect(step_obj, *args, **kwargs):
        if "is_last" in kwargs:
            step_obj.is_last = kwargs["is_last"]
        return step_obj

    async def update_task_side_effect(task_obj, *args, **kwargs):
        if "status" in kwargs:
            order.append(f"update_task:{kwargs['status']}")
        return task_obj

    real_clean_up_task = agent.clean_up_task

    async def clean_up_task_side_effect(*args, **kwargs):
        kwargs["need_final_screenshot"] = False
        return await real_clean_up_task(*args, **kwargs)

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=ctx),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=[]),
        patch("skyvern.forge.agent.app") as mock_app,
        patch.object(agent, "initialize_execution_state", AsyncMock(return_value=(step, browser_state, None))),
        patch.object(agent, "agent_step", AsyncMock(side_effect=agent_step_side_effect)),
        patch.object(agent, "update_step", AsyncMock(side_effect=update_step_side_effect)),
        patch.object(agent, "update_task", AsyncMock(side_effect=update_task_side_effect)),
        patch.object(agent, "update_task_errors_from_detailed_output", AsyncMock(return_value=task)),
        patch.object(agent, "clean_up_task", AsyncMock(side_effect=clean_up_task_side_effect)),
    ):
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
        mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
        mock_app.AGENT_FUNCTION.post_step_execution = AsyncMock()
        mock_app.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=None)
        mock_app.STORAGE.save_downloaded_files = AsyncMock()
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])

        await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            task_block=task_block,
            close_browser_on_completion=True,
            complete_verification=True,
            engine=RunEngine.skyvern_v1,
        )

    # The credit branch fired (file was finalized) and closed exactly the marker popup, not the opener.
    assert (download_dir / "req-123.zip").exists()
    marker.close.assert_awaited_once()
    opener.close.assert_not_called()
    assert task.task_id not in ctx.download_popup_claims
    # The close happened before the completed-task handoff (update_step/update_task).
    close_i = order.index("close")
    update_task_indices = [i for i, entry in enumerate(order) if entry.startswith("update_task:")]
    assert update_task_indices and all(close_i < i for i in update_task_indices)


@pytest.mark.asyncio
async def test_execute_step_no_credit_releases_deferred_dispatched_failure_reservation(tmp_path) -> None:
    """Point-(a) wiring on the real execute_step path: when the complete_on_download seam finds NO durable
    credit, a dispatched-then-failed action's deferred reservation is released at the step boundary --
    before the retry scrape -- restoring generic blank-page recovery. Removing the execute_step apply-call
    leaves the marker wedged and blocks #17139 recovery."""
    agent = ForgeAgent()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    task = _make_task(task_id="task-no-credit")
    task.status = SimpleNamespace(value="running")
    task.navigation_goal = "Download invoice"
    task.data_extraction_goal = None
    task.complete_criterion = None
    task.terminate_criterion = None
    task.browser_address = None
    task.max_steps_per_run = None
    task.url = "https://example.com"
    task.proxy_location = None
    task.llm_key = None
    task.task_type = "general"

    step = MagicMock()
    step.step_id = "step-1"
    step.order = 0
    step.retry_index = 0
    step.status = StepStatus.failed

    organization = MagicMock()
    organization.organization_id = task.organization_id
    organization.max_steps_per_run = None

    task_block = MagicMock()
    task_block.complete_on_download = True
    task_block.download_timeout = None
    task_block.download_suffix = "req-123"

    marker = _claimed_popup(":")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    browser_state.get_working_page = AsyncMock(return_value=None)

    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, marker)
    # A prior dispatched-then-failed action deferred its release to this step's credit seam.
    ctx.stash_pending_download_reservation_release(task.task_id, ((), ()))

    async def agent_step_side_effect(*args, **kwargs):
        # No download file lands: the complete_on_download seam finds no credit.
        return step, DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
            cua_response=None,
        )

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=ctx),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=[]),
        patch("skyvern.forge.agent.app") as mock_app,
        patch.object(agent, "initialize_execution_state", AsyncMock(return_value=(step, browser_state, None))),
        patch.object(agent, "agent_step", AsyncMock(side_effect=agent_step_side_effect)),
        patch.object(agent, "update_step", AsyncMock(side_effect=lambda s, *a, **k: s)),
        patch.object(agent, "update_task_errors_from_detailed_output", AsyncMock(return_value=task)),
        patch.object(agent, "handle_failed_step", AsyncMock(return_value=None)),
        patch.object(agent, "clean_up_task", AsyncMock()),
    ):
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
        mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
        mock_app.AGENT_FUNCTION.post_step_execution = AsyncMock()
        mock_app.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=None)
        mock_app.STORAGE.list_downloaded_files_in_browser_session = AsyncMock(return_value=[])

        await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            task_block=task_block,
            close_browser_on_completion=True,
            complete_verification=True,
            engine=RunEngine.skyvern_v1,
        )

    # No credit -> the deferred release fired at the step seam: the marker is freed for blank recovery,
    # not closed, and the pending release is consumed.
    assert not ctx.has_download_popup_claim(task.task_id, marker)
    assert task.task_id not in ctx.pending_download_reservation_release
    marker.close.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "status_code",
        "successful_followup",
        "files_to_finalize",
        "complete_on_download",
        "fail_task_succeeds",
        "expected_status",
    ),
    [
        pytest.param(500, False, [], True, True, TaskStatus.failed, id="5xx-no-file-fails"),
        pytest.param(404, False, [], True, True, None, id="4xx-no-file-keeps-normal-flow"),
        pytest.param(500, True, ["invoice.pdf"], True, True, TaskStatus.completed, id="5xx-followed-by-file-completes"),
        pytest.param(
            500,
            False,
            ["invoice.pdf"],
            True,
            True,
            TaskStatus.completed,
            id="file-arriving-during-wait-completes",
        ),
        pytest.param(500, False, [], False, True, None, id="5xx-without-complete-on-download-keeps-normal-flow"),
        pytest.param(500, False, [], True, False, TaskStatus.failed, id="cancel-wins-before-fail-cleanup"),
    ],
)
async def test_execute_step_handles_file_download_5xx_evidence(
    tmp_path,
    status_code: int,
    successful_followup: bool,
    files_to_finalize: list[str],
    complete_on_download: bool,
    fail_task_succeeds: bool,
    expected_status: TaskStatus | None,
) -> None:
    agent = ForgeAgent()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    task = _make_task(task_id="task-http-5xx")
    task.status = SimpleNamespace(value="running")
    task.navigation_goal = "Download invoice"
    task.data_extraction_goal = None
    task.complete_criterion = None
    task.terminate_criterion = None
    task.browser_address = None
    task.max_steps_per_run = None
    task.url = "https://example.com"
    task.proxy_location = None
    task.llm_key = None
    task.task_type = "general"
    task.error_code_mapping = None

    now = datetime.now(UTC)
    task_block = FileDownloadBlock(
        label="download",
        output_parameter=OutputParameter(
            output_parameter_id="download-output-id",
            workflow_id="workflow-id",
            key="download_output",
            created_at=now,
            modified_at=now,
        ),
        navigation_goal="Download the requested file",
        complete_on_download=complete_on_download,
        download_suffix="invoice",
    )
    download_result = ActionSuccess(download_triggered=False)
    download_result.download_failure_status = status_code
    if status_code >= 500:
        download_result.mark_download_failure_attributed(status_code)
    actions_and_results = [(Action(action_type=ActionType.CLICK), [download_result])]
    if successful_followup:
        saved_file_result = ActionSuccess(download_triggered=True, downloaded_files=["invoice.pdf"])
        actions_and_results.append((Action(action_type=ActionType.CLICK), [saved_file_result]))
    step = MagicMock()
    step.step_id = "step-http-5xx"
    step.task_id = task.task_id
    step.organization_id = task.organization_id
    step.order = 0
    step.retry_index = 0
    step.status = StepStatus.completed
    step.output = AgentStepOutput(
        action_results=[
            result for _, action_results_for_step in actions_and_results for result in action_results_for_step
        ],
        actions_and_results=actions_and_results,
    )

    organization = MagicMock()
    organization.organization_id = task.organization_id
    organization.max_steps_per_run = None
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)
    next_step = MagicMock()
    context = SkyvernContext(task_id=task.task_id)

    async def update_step_side_effect(step_obj, status=None, output=None, is_last=None, **_kwargs):
        if status is not None:
            step_obj.status = status
        if output is not None:
            step_obj.output = output
        if is_last is not None:
            step_obj.is_last = is_last
        return step_obj

    async def fail_task_side_effect(*args, **kwargs):
        if not fail_task_succeeds:
            return False
        return await ForgeAgent.fail_task(agent, *args, **kwargs)

    fail_task_mock = AsyncMock(side_effect=fail_task_side_effect)
    clean_up_task_mock = AsyncMock()

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=context),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_files_in_directory", return_value=[]),
        patch("skyvern.forge.agent.get_download_retry_started_at", AsyncMock(return_value=now)),
        patch("skyvern.forge.agent.record_fail_fast_shadow", AsyncMock()),
        patch.object(
            type(__import__("skyvern.forge.agent", fromlist=["settings"]).settings),
            "execute_all_steps",
            return_value=False,
        ),
        patch("skyvern.forge.agent.app") as mock_app,
        patch.object(agent, "initialize_execution_state", AsyncMock(return_value=(step, browser_state, None))),
        patch.object(agent, "agent_step", AsyncMock(return_value=(step, None))),
        patch.object(agent, "_wait_for_in_flight_downloads", AsyncMock()),
        patch.object(agent, "_finalize_downloaded_files_for_task", AsyncMock(return_value=files_to_finalize)),
        patch.object(agent, "_close_credited_download_popups", AsyncMock()),
        patch.object(agent, "handle_completed_step", AsyncMock(return_value=(None, None, next_step))) as handle_step,
        patch.object(agent, "_sync_video_artifact_after_step", AsyncMock()),
        patch.object(agent, "update_step", AsyncMock(side_effect=update_step_side_effect)),
        patch.object(agent, "update_task", AsyncMock(return_value=task)) as update_task,
        patch.object(agent, "update_task_errors_from_detailed_output", AsyncMock(return_value=task)),
        patch.multiple(agent, fail_task=fail_task_mock, clean_up_task=clean_up_task_mock),
    ):
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
        mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
        mock_app.AGENT_FUNCTION.post_step_execution = AsyncMock()
        mock_app.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
        mock_app.BROWSER_MANAGER.get_for_workflow_run = MagicMock(return_value=None)
        mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled.return_value = False
        mock_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.return_value = set()

        result_step, _detailed_output, result_next_step = await agent.execute_step(
            organization=organization,
            task=task,
            step=step,
            task_block=task_block,
            engine=RunEngine.skyvern_v1,
        )

    if expected_status == TaskStatus.failed:
        assert result_next_step is None
        assert result_step.is_last is True
        handle_step.assert_not_awaited()
        fail_task_mock.assert_awaited_once()
        if fail_task_succeeds:
            update_task.assert_awaited_once()
            assert update_task.await_args.kwargs["status"] == TaskStatus.failed
            assert "HTTP 500" in update_task.await_args.kwargs["failure_reason"]
            clean_up_task_mock.assert_awaited_once()
        else:
            update_task.assert_not_awaited()
            clean_up_task_mock.assert_awaited_once()
            assert clean_up_task_mock.await_args.kwargs["need_call_webhook"] is False
            assert clean_up_task_mock.await_args.kwargs["need_final_screenshot"] is False
            assert clean_up_task_mock.await_args.kwargs["download_suffix"] is None
    elif expected_status == TaskStatus.completed:
        assert result_next_step is None
        handle_step.assert_not_awaited()
        fail_task_mock.assert_not_awaited()
        update_task.assert_not_awaited()
        clean_up_task_mock.assert_awaited_once()
        assert clean_up_task_mock.await_args.kwargs["download_completion_pending"] is True
    else:
        assert result_next_step is next_step
        handle_step.assert_awaited_once()
        update_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_clean_up_task_closes_claimed_popup_on_cleanup_finalization_credit(tmp_path) -> None:
    """Second durable-credit seam: when the ``download_suffix``/``list_files_before`` finalization inside
    clean_up_task proves a new file, the exact recorded ``":"`` marker popup is closed. Pre-fix the
    top-of-cleanup claim clear preempted this seam, dropping the claim without closing the popup."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-cleanup-credit")
    last_step = MagicMock()
    last_step.step_id = "step-1"
    marker = _claimed_popup(":")
    opener = _claimed_popup("https://example.com/documents")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    browser_state.get_working_page = AsyncMock(return_value=None)
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, marker)

    async def finalize_side_effect(*args, **kwargs):
        return ["req-123.zip"]  # cleanup finalization proves a newly landed file

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch.object(agent, "_finalize_downloaded_files_for_task", AsyncMock(side_effect=finalize_side_effect)),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=browser_state)
        mock_app.STORAGE.save_downloaded_files = AsyncMock()

        await agent.clean_up_task(
            task,
            last_step=last_step,
            need_final_screenshot=False,
            download_suffix="req-123",
            list_files_before=[],
        )

    marker.close.assert_awaited_once()
    opener.close.assert_not_called()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_clean_up_task_no_cleanup_credit_expires_claims_without_close(tmp_path) -> None:
    """When cleanup finalization proves no new file, the marker popup is NOT closed, but the claims
    still expire (taken and dropped at cleanup entry)."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-cleanup-nocredit")
    last_step = MagicMock()
    last_step.step_id = "step-1"
    marker = _claimed_popup(":")
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)
    browser_state.list_valid_pages = AsyncMock(return_value=[marker])
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, marker)

    async def finalize_side_effect(*args, **kwargs):
        return []  # nothing finalized -> no durable credit

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch.object(agent, "_finalize_downloaded_files_for_task", AsyncMock(side_effect=finalize_side_effect)),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=browser_state)
        mock_app.STORAGE.save_downloaded_files = AsyncMock()

        await agent.clean_up_task(
            task,
            last_step=last_step,
            need_final_screenshot=False,
            download_suffix="req-123",
            list_files_before=[],
        )

    marker.close.assert_not_called()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_clean_up_task_expires_claims_without_close_when_no_credit(tmp_path) -> None:
    """No durable credit at the terminal boundary: with claims present but cleanup finalization proving
    no new file (a failed/terminated task), the claims are taken (expired) but NO destructive close
    runs. Expiry bounds memory; it never closes a page absent a credit."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-no-credit")
    last_step = MagicMock()
    last_step.step_id = "step-1"
    marker = _claimed_popup(":")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    browser_state.get_working_page = AsyncMock(return_value=None)
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, marker)

    async def finalize_side_effect(*args, **kwargs):
        return []  # no newly landed file -> no durable credit at cleanup

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch.object(agent, "_finalize_downloaded_files_for_task", AsyncMock(side_effect=finalize_side_effect)),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=browser_state)
        mock_app.STORAGE.save_downloaded_files = AsyncMock()

        await agent.clean_up_task(
            task,
            last_step=last_step,
            need_final_screenshot=False,
            download_suffix="req-123",
            list_files_before=[],
        )

    marker.close.assert_not_called()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_closes_multiple_distinct_claims_each_once() -> None:
    """One durable credit closes every distinct popup the task recorded, each exactly once and
    regardless of URL, matching the legacy multi-extra-page cleanup that closed all pages the download
    opened. The opener (never a claim) is untouched."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-multi")
    popup_blank = _claimed_popup(":")
    popup_committed = _claimed_popup("https://example.test/report.pdf")
    popup_about = _claimed_popup("about:blank")
    opener = _claimed_popup("https://example.test/documents")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    for popup in (popup_blank, popup_committed, popup_about):
        ctx.record_download_popup_claim(task.task_id, popup)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    popup_blank.close.assert_awaited_once()
    popup_committed.close.assert_awaited_once()
    popup_about.close.assert_awaited_once()
    opener.close.assert_not_called()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_dedups_exact_duplicate_claim() -> None:
    """Recording the same Page twice yields a single claim, so an exact duplicate closes at most once."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-dup")
    popup = _claimed_popup("https://example.test/report.pdf")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, popup)
    ctx.record_download_popup_claim(task.task_id, popup)  # exact duplicate

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    popup.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_credit_seams_cannot_both_close_the_same_claimed_popup() -> None:
    """pop-once/take semantics: the complete_on_download seam pops the claims, so the later
    cleanup-finalization seam (which takes from the same registry) finds none and cannot re-close the
    same Page."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-two-seams")
    popup = _claimed_popup("https://example.test/report.pdf")
    browser_state = MagicMock()
    browser_state.browser_context = _RUN_CONTEXT
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, popup)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        # complete_on_download seam consumes the claim and closes the popup.
        await agent._close_credited_download_popups(task, browser_state)
        # cleanup-finalization seam takes from the same registry -> already empty.
        cleanup_claims = ctx.take_download_popup_claims(task.task_id)
        await agent._close_claimed_download_popups(task, browser_state, cleanup_claims)

    popup.close.assert_awaited_once()
    assert cleanup_claims == []


@pytest.mark.asyncio
async def test_late_cleanup_skips_popup_already_closed_by_in_seam_cleanup() -> None:
    """Sequential topology: after the legacy action-level / #16476 in-seam cleanup actually closes the
    popup (is_closed flips true and the run drops it from valid pages), the late credit cleanup does not
    close it a second time."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-seq")
    popup = _claimed_popup(":")
    closed_state = {"value": False}
    popup.is_closed.side_effect = lambda: closed_state["value"]

    async def _mark_closed() -> None:
        closed_state["value"] = True

    popup.close = AsyncMock(side_effect=_mark_closed)
    browser_state = MagicMock()
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, popup)

    # In-seam cleanup closes the popup first; the run then drops the closed page from its valid pages.
    await popup.close()
    browser_state.list_valid_pages = AsyncMock(return_value=[])

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    popup.close.assert_awaited_once()  # only the in-seam close; late cleanup did not re-close
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_retry_waits_ignore_retained_partial_files(tmp_path) -> None:
    cutoff = datetime.fromtimestamp(200, tz=UTC)
    stale = tmp_path / "previous.pdf.crdownload"
    stale.write_bytes(b"old partial")
    os.utime(stale, (100, 100))
    (tmp_path / "current.pdf").write_bytes(b"complete")
    task = _make_task(workflow_run_id="wr_retry")
    exhausted: set[str] = set()
    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=tmp_path),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        capture_logs() as logs,
    ):
        async with asyncio.timeout(0.5):
            await check_downloading_files_and_wait_for_download_to_complete(
                tmp_path, task.organization_id, timeout=5, attempt_started_at=cutoff
            )
            await ForgeAgent()._wait_for_in_flight_downloads(
                task,
                SimpleNamespace(download_timeout=5),
                task.organization_id,
                attempt_started_at=cutoff,
                exhausted=exhausted,
            )
    assert exhausted == set()
    assert not any("long-time downloading" in row["event"] for row in logs)
    assert stale.read_bytes() == b"old partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [1, None, "lookup_error"])
async def test_retry_waits_keep_unfiltered_fallback(tmp_path, attempt) -> None:
    stale = tmp_path / "previous.pdf.crdownload"
    stale.write_bytes(b"old partial")
    os.utime(stale, (100, 100))
    result = ("wr_retry", 1, datetime.fromtimestamp(200, tz=UTC)) if attempt == 1 else ("wr_retry", 1, None)
    task = _make_task(workflow_run_id="wr_retry")
    exhausted: set[str] = set()
    with (
        patch(
            "skyvern.forge.sdk.artifact.storage.base.resolve_download_attempt",
            new=AsyncMock(
                return_value=result,
                side_effect=RuntimeError("lookup unavailable") if attempt == "lookup_error" else None,
            ),
        ),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=tmp_path),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        capture_logs() as logs,
    ):
        cutoff = await get_download_retry_started_at(task.organization_id, task.workflow_run_id)
        await check_downloading_files_and_wait_for_download_to_complete(
            tmp_path, task.organization_id, timeout=0.01, attempt_started_at=cutoff
        )
        await ForgeAgent()._wait_for_in_flight_downloads(
            task,
            SimpleNamespace(download_timeout=0.01),
            task.organization_id,
            attempt_started_at=cutoff,
            exhausted=exhausted,
        )
    assert cutoff is None
    assert exhausted == {str(stale)}
    assert any("long-time downloading" in row["event"] for row in logs)
    if attempt == "lookup_error":
        assert any("Failed to resolve download attempt" in row["event"] for row in logs)
    assert stale.exists()


@pytest.mark.asyncio
async def test_finalize_does_not_record_html_error_page_saved_as_pdf(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    html_error = b"<!DOCTYPE html><html><head><title>Error</title></head><body><h1>Session expired</h1></body></html>"
    (download_dir / "invoice.pdf").write_bytes(html_error)
    (download_dir / "statement.pdf").write_bytes(_VALID_TEST_PDF)
    (download_dir / "report.csv").write_text("name,total\ninvoice,12\n")
    with zipfile.ZipFile(download_dir / "workbook.xlsx", "w") as workbook:
        workbook.writestr("[Content_Types].xml", b"<Types/>")
        workbook.writestr("xl/workbook.xml", b"<workbook/>")
    context = SkyvernContext(task_id=task.task_id)

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        capture_logs() as logs,
    ):
        recorded = await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=None,
            list_files_before=[],
            randomize_if_missing=False,
        )

    assert any(name.endswith("statement.pdf") for name in recorded)
    assert any(name.endswith("report.csv") for name in recorded)
    assert any(name.endswith("workbook.xlsx") for name in recorded)
    assert not any(name.endswith("invoice.pdf") for name in recorded)
    assert not (download_dir / "invoice.pdf").exists()
    assert context.consume_download_failures(task.task_id) == ["The requested browser download could not be saved."]
    rejected = [
        event
        for event in logs
        if event["event"] == "Rejected downloaded file whose content contradicts its claimed type"
    ]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "html_login_masquerade"
    assert rejected[0]["size"] == len(html_error)
    assert rejected[0]["sniffed_type"] == "html"
    assert not {"body", "content", "url", "filename"}.intersection(rejected[0])


@pytest.mark.asyncio
async def test_finalize_continues_when_download_moves_during_validation(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    vanished_before_read = download_dir / "vanished.pdf"
    vanished_before_unlink = download_dir / "rejected.pdf"
    valid_file = download_dir / "statement.pdf"
    vanished_before_read.write_bytes(_VALID_TEST_PDF)
    vanished_before_unlink.write_bytes(
        b"<!DOCTYPE html><html><head><title>Error</title></head><body><h1>Session expired</h1></body></html>"
    )
    valid_file.write_bytes(_VALID_TEST_PDF)
    context = SkyvernContext(task_id=task.task_id)
    rejected: list[str] = []
    original_open = Path.open
    original_unlink = Path.unlink

    def _open_with_race(path: Path, *args, **kwargs):
        if path == vanished_before_read:
            original_unlink(path)
        return original_open(path, *args, **kwargs)

    def _unlink_with_race(path: Path, *args, **kwargs):
        if path == vanished_before_unlink:
            original_unlink(path)
        return original_unlink(path, *args, **kwargs)

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=context),
        patch.object(Path, "open", new=_open_with_race),
        patch.object(Path, "unlink", new=_unlink_with_race),
    ):
        recorded = await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=None,
            list_files_before=[],
            randomize_if_missing=False,
            rejected_into=rejected,
        )

    assert any(name.endswith("statement.pdf") for name in recorded)
    assert [Path(name).name for name in rejected] == ["rejected.pdf"]
    assert not vanished_before_read.exists()
    assert not vanished_before_unlink.exists()
    assert context.consume_download_failures(task.task_id) == ["The requested browser download could not be saved."]
