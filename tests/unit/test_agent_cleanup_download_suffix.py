from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import DownloadFileMaxWaitingTime, TaskNotFound
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.schemas.runs import RunEngine
from skyvern.webeye.actions.models import DetailedAgentStepOutput
from tests.unit._fingerprint_expectations import expected_fingerprint


@pytest.fixture(autouse=True)
def _keyed_fingerprint(fingerprint_secret_key: str) -> str:
    return fingerprint_secret_key


def _finalize_events(cap: list[dict]) -> list[dict]:
    return [e for e in cap if e.get("event") == "download_suffix_finalize_rename"]


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
    aws_client.download_file = AsyncMock(return_value=b"data")
    rename_mock = MagicMock()

    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.get_aws_client", return_value=aws_client),
        patch("skyvern.forge.agent.rename_file", rename_mock),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
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
    aws_client.download_file = AsyncMock(side_effect=[b"a", b"b"])

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
    (download_dir / "req-123.pdf").write_bytes(b"x")

    rename_mock = MagicMock()
    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.rename_file", rename_mock),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
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
async def test_cleanup_task_finalizes_downloads_before_saving(tmp_path) -> None:
    agent = ForgeAgent()
    task = _make_task()
    last_step = MagicMock()
    last_step.step_id = "step-1"
    call_order: list[str] = []

    async def finalize_side_effect(*args, **kwargs):
        call_order.append("rename")
        return ["uuid-file.zip"]

    async def save_side_effect(**kwargs):
        call_order.append("save")

    with (
        patch("skyvern.forge.agent.analytics.capture"),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch.object(agent, "_finalize_downloaded_files_for_task", AsyncMock(side_effect=finalize_side_effect)),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.STORAGE.save_downloaded_files = AsyncMock(side_effect=save_side_effect)

        await agent.clean_up_task(
            task,
            last_step=last_step,
            need_final_screenshot=False,
            download_suffix="req-123",
            list_files_before=[],
        )

    assert call_order == ["rename", "save"]


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
async def test_execute_step_complete_on_download_does_not_double_finalize(tmp_path) -> None:
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

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)

    async def agent_step_side_effect(*args, **kwargs):
        (download_dir / "uuid-file.zip").write_text("dummy")
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
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=MagicMock()),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
        patch("skyvern.forge.agent.list_downloading_files_in_directory", return_value=[]),
        patch("skyvern.forge.agent.app") as mock_app,
        patch.object(agent, "initialize_execution_state", AsyncMock(return_value=(step, browser_state, None))),
        patch.object(agent, "agent_step", AsyncMock(side_effect=agent_step_side_effect)),
        patch.object(agent, "update_step", AsyncMock(side_effect=update_step_side_effect)),
        patch.object(agent, "update_task", AsyncMock(side_effect=update_task_side_effect)),
        patch.object(agent, "update_task_errors_from_detailed_output", AsyncMock(return_value=task)),
    ):
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
        mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
        mock_app.AGENT_FUNCTION.post_step_execution = AsyncMock()
        mock_app.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
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

    assert (download_dir / "req-123.zip").exists()
    assert not (download_dir / "req-123_1.zip").exists()
    assert not (download_dir / "uuid-file.zip").exists()


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
    task_block.download_suffix = "req-123"

    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=None)

    async def agent_step_side_effect(*args, **kwargs):
        current_step = args[1]
        if current_step.step_id == "step-1":
            (download_dir / "uuid-file.zip").write_text("dummy")
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
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=MagicMock()),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
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

    assert (download_dir / "req-123.zip").exists()
    assert not (download_dir / "uuid-file.zip").exists()


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
        (download_dir / "uuid-file.zip").write_text("dummy")
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
    statement_bytes = b"STATEMENT-BYTES-A"
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
    aws_client.download_file = AsyncMock(return_value=b"ONLY-STATEMENT-BYTES")

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
    shared_bytes = b"SHARED-BYTES-BASELINE-AND-SESSION"
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


def _claimed_popup(url: str = ":") -> MagicMock:
    popup = MagicMock(url=url)
    popup.is_closed.return_value = False
    popup.close = AsyncMock()
    return popup


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
    browser_state.list_valid_pages = AsyncMock(return_value=[opener, popup])
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
    browser_state.list_valid_pages = AsyncMock(return_value=[committed])
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, committed)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    committed.close.assert_awaited_once()
    assert task.task_id not in ctx.download_popup_claims


@pytest.mark.asyncio
async def test_close_credited_download_popups_skips_popup_not_in_run_pages() -> None:
    """A recorded popup no longer among the run's live pages is left untouched (scope guard)."""
    agent = ForgeAgent()
    task = _make_task(task_id="task-stray")
    stray = _claimed_popup(":")
    opener = _claimed_popup("https://example.test/documents")
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[opener])
    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, stray)

    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, browser_state)

    stray.close.assert_not_called()


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
    browser_state.list_valid_pages = AsyncMock(return_value=[marker])
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
async def test_execute_step_complete_on_download_closes_claimed_popup_before_handoff(tmp_path) -> None:
    """Real outer-path regression: driving the actual v1 execute_step complete_on_download credit
    branch, the exact recorded ``":"`` marker popup is closed before the completed-task handoff
    (update_step/update_task). Removing the single _close_credited_download_popups call makes this
    fail (the marker is never closed)."""
    agent = ForgeAgent()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    task = _make_task(task_id="task-outer")
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
    browser_state.get_working_page = AsyncMock(return_value=None)
    browser_state.list_valid_pages = AsyncMock(return_value=[opener, marker])

    ctx = SkyvernContext(task_id=task.task_id)
    ctx.record_download_popup_claim(task.task_id, marker)

    async def agent_step_side_effect(*args, **kwargs):
        (download_dir / "uuid-file.zip").write_text("dummy")
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
        patch.object(agent, "clean_up_task", AsyncMock()),
    ):
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=None)
        mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
        mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
        mock_app.AGENT_FUNCTION.post_step_execution = AsyncMock()
        mock_app.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
        mock_app.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
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
    browser_state.get_working_page = AsyncMock(return_value=None)
    browser_state.list_valid_pages = AsyncMock(return_value=[opener, marker])
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
    browser_state.list_valid_pages = AsyncMock(return_value=[opener, popup_blank, popup_committed, popup_about])
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
    browser_state.list_valid_pages = AsyncMock(return_value=[popup])
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
    browser_state.list_valid_pages = AsyncMock(return_value=[popup])
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
