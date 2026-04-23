from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent import ForgeAgent
from skyvern.schemas.runs import RunEngine
from skyvern.webeye.actions.models import DetailedAgentStepOutput


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
        patch("skyvern.forge.agent.otel_trace.get_current_span", return_value=MagicMock()),
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
        patch("skyvern.forge.agent.otel_trace.get_current_span", return_value=MagicMock()),
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
        patch("skyvern.forge.agent.otel_trace.get_current_span", return_value=MagicMock()),
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
