from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import Settings
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye.browser_artifacts import BrowserArtifacts


def _task(task_id: str = "tsk_1", organization_id: str = "o_1") -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    task.organization_id = organization_id
    task.webhook_callback_url = None
    return task


def _step(step_id: str = "stp_1", task_id: str = "tsk_1") -> MagicMock:
    step = MagicMock()
    step.step_id = step_id
    step.task_id = task_id
    return step


def _browser_state() -> MagicMock:
    browser_state = MagicMock()
    browser_state.browser_artifacts = BrowserArtifacts()
    browser_state.browser_context = None
    return browser_state


def _workflow() -> MagicMock:
    workflow = MagicMock()
    workflow.workflow_id = "wf_1"
    return workflow


def _workflow_run() -> MagicMock:
    workflow_run = MagicMock()
    workflow_run.workflow_run_id = "wr_1"
    return workflow_run


def test_submission_signal_shadow_setting_defaults_off() -> None:
    assert Settings.model_fields["SKYVERN_SUBMISSION_SIGNAL_SHADOW"].default is False


@pytest.mark.asyncio
async def test_task_cleanup_does_not_schedule_submission_shadow_when_flag_is_off() -> None:
    agent = ForgeAgent()
    task = _task()
    last_step = _step()
    browser_state = _browser_state()
    scheduled: list[dict[str, object]] = []

    def schedule_submission_signal_shadow(**kwargs: object) -> None:
        scheduled.append(kwargs)

    with (
        patch("skyvern.forge.agent.settings.SKYVERN_SUBMISSION_SIGNAL_SHADOW", False),
        patch(
            "skyvern.forge.agent.submission_shadow.schedule_submission_signal_shadow",
            side_effect=schedule_submission_signal_shadow,
        ),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.BROWSER_MANAGER.cleanup_for_task = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[])
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(return_value=b'{"log":{"entries":[]}}')
        mock_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=b"")
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()

        await agent.cleanup_browser_and_create_artifacts(
            close_browser_on_completion=True,
            last_step=last_step,
            task=task,
            browser_session_id="pbs_1",
        )

    assert scheduled == []


@pytest.mark.asyncio
async def test_task_cleanup_schedules_submission_shadow_with_the_har_context() -> None:
    agent = ForgeAgent()
    task = _task()
    last_step = _step()
    browser_state = _browser_state()
    har_data = b'{"log":{"entries":[]}}'
    calls: list[str] = []
    scheduled: list[dict[str, object]] = []

    async def get_har_data(**_: object) -> bytes:
        calls.append("har")
        return har_data

    async def get_browser_console_log(**_: object) -> bytes:
        calls.append("browser_log")
        return b""

    def schedule_submission_signal_shadow(**kwargs: object) -> None:
        calls.append("shadow")
        scheduled.append(kwargs)

    with (
        patch("skyvern.forge.agent.settings.SKYVERN_SUBMISSION_SIGNAL_SHADOW", True),
        patch(
            "skyvern.forge.agent.submission_shadow.schedule_submission_signal_shadow",
            side_effect=schedule_submission_signal_shadow,
        ),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.app") as mock_app,
    ):
        mock_app.BROWSER_MANAGER.cleanup_for_task = AsyncMock(return_value=browser_state)
        mock_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[])
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(side_effect=get_har_data)
        mock_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(side_effect=get_browser_console_log)
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()

        await agent.cleanup_browser_and_create_artifacts(
            close_browser_on_completion=True,
            last_step=last_step,
            task=task,
            browser_session_id="pbs_1",
        )

    assert calls[:3] == ["har", "shadow", "browser_log"]
    assert scheduled == [
        {
            "har_data": har_data,
            "browser_state": browser_state,
            "last_step": last_step,
            "task": task,
            "browser_session_id": "pbs_1",
        }
    ]


@pytest.mark.asyncio
async def test_workflow_har_cleanup_does_not_schedule_submission_shadow_when_flag_is_off() -> None:
    service = WorkflowService()
    browser_state = _browser_state()
    last_step = _step()
    workflow = _workflow()
    workflow_run = _workflow_run()
    scheduled: list[dict[str, object]] = []

    def schedule_submission_signal_shadow(**kwargs: object) -> None:
        scheduled.append(kwargs)

    with (
        patch("skyvern.forge.sdk.workflow.service.settings.SKYVERN_SUBMISSION_SIGNAL_SHADOW", False),
        patch(
            "skyvern.forge.sdk.workflow.service.submission_shadow.schedule_submission_signal_shadow",
            side_effect=schedule_submission_signal_shadow,
        ),
        patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
    ):
        mock_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=b"")
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(return_value=b'{"log":{"entries":[]}}')
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()
        mock_app.ARTIFACT_MANAGER.create_task_archive = AsyncMock()

        await service.persist_har_data(browser_state, last_step, workflow, workflow_run)
        await service._persist_debug_artifacts_bundled(browser_state, last_step, workflow, workflow_run)

    assert scheduled == []


@pytest.mark.asyncio
async def test_workflow_standalone_har_schedules_submission_shadow_with_the_har_context() -> None:
    service = WorkflowService()
    browser_state = _browser_state()
    last_step = _step()
    workflow = _workflow()
    workflow_run = _workflow_run()
    har_data = b'{"log":{"entries":[]}}'
    calls: list[str] = []
    scheduled: list[dict[str, object]] = []

    async def get_har_data(**_: object) -> bytes:
        calls.append("har")
        return har_data

    def schedule_submission_signal_shadow(**kwargs: object) -> None:
        calls.append("shadow")
        scheduled.append(kwargs)

    async def create_artifact(**_: object) -> None:
        calls.append("artifact")

    with (
        patch("skyvern.forge.sdk.workflow.service.settings.SKYVERN_SUBMISSION_SIGNAL_SHADOW", True),
        patch(
            "skyvern.forge.sdk.workflow.service.submission_shadow.schedule_submission_signal_shadow",
            side_effect=schedule_submission_signal_shadow,
        ),
        patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
    ):
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(side_effect=get_har_data)
        mock_app.ARTIFACT_MANAGER.create_artifact = AsyncMock(side_effect=create_artifact)

        await service.persist_har_data(browser_state, last_step, workflow, workflow_run)

    assert calls == ["har", "shadow", "artifact"]
    assert scheduled == [
        {
            "har_data": har_data,
            "browser_state": browser_state,
            "last_step": last_step,
            "workflow_run": workflow_run,
        }
    ]


@pytest.mark.asyncio
async def test_workflow_bundled_har_schedules_submission_shadow_with_the_har_context() -> None:
    service = WorkflowService()
    browser_state = _browser_state()
    last_step = _step()
    workflow = _workflow()
    workflow_run = _workflow_run()
    har_data = b'{"log":{"entries":[]}}'
    calls: list[str] = []
    scheduled: list[dict[str, object]] = []

    async def get_har_data(**_: object) -> bytes:
        calls.append("har")
        return har_data

    def schedule_submission_signal_shadow(**kwargs: object) -> None:
        calls.append("shadow")
        scheduled.append(kwargs)

    async def create_task_archive(**_: object) -> None:
        calls.append("archive")

    with (
        patch("skyvern.forge.sdk.workflow.service.settings.SKYVERN_SUBMISSION_SIGNAL_SHADOW", True),
        patch(
            "skyvern.forge.sdk.workflow.service.submission_shadow.schedule_submission_signal_shadow",
            side_effect=schedule_submission_signal_shadow,
        ),
        patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
    ):
        mock_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=b"")
        mock_app.BROWSER_MANAGER.get_har_data = AsyncMock(side_effect=get_har_data)
        mock_app.ARTIFACT_MANAGER.create_task_archive = AsyncMock(side_effect=create_task_archive)

        await service._persist_debug_artifacts_bundled(browser_state, last_step, workflow, workflow_run)

    assert calls == ["har", "shadow", "archive"]
    assert scheduled == [
        {
            "har_data": har_data,
            "browser_state": browser_state,
            "last_step": last_step,
            "workflow_run": workflow_run,
        }
    ]


@pytest.mark.asyncio
async def test_wiring_persists_har_when_shadow_scheduler_setup_fails() -> None:
    agent = ForgeAgent()
    service = WorkflowService()
    task = _task()
    browser_state = _browser_state()
    last_step = _step()
    workflow = _workflow()
    workflow_run = _workflow_run()
    har_data = b'{"log":{"entries":[]}}'

    with (
        patch("skyvern.forge.sdk.submission.shadow.settings.SKYVERN_SUBMISSION_SIGNAL_SHADOW", True),
        patch(
            "skyvern.forge.sdk.submission.shadow._prune_pending",
            side_effect=RuntimeError("scheduler setup failed"),
        ),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
        patch("skyvern.forge.agent.app") as agent_app,
        patch("skyvern.forge.sdk.workflow.service.app") as workflow_app,
    ):
        agent_app.BROWSER_MANAGER.cleanup_for_task = AsyncMock(return_value=browser_state)
        agent_app.BROWSER_MANAGER.get_video_artifacts = AsyncMock(return_value=[])
        agent_app.BROWSER_MANAGER.get_har_data = AsyncMock(return_value=har_data)
        agent_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=b"")
        agent_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()
        workflow_app.BROWSER_MANAGER.get_browser_console_log = AsyncMock(return_value=b"")
        workflow_app.BROWSER_MANAGER.get_har_data = AsyncMock(return_value=har_data)
        workflow_app.ARTIFACT_MANAGER.create_artifact = AsyncMock()
        workflow_app.ARTIFACT_MANAGER.create_task_archive = AsyncMock()

        await agent.cleanup_browser_and_create_artifacts(True, last_step, task, browser_session_id="pbs_1")
        await service.persist_har_data(browser_state, last_step, workflow, workflow_run)
        await service._persist_debug_artifacts_bundled(browser_state, last_step, workflow, workflow_run)

    agent_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once_with(
        step=last_step, artifact_type=ArtifactType.HAR, data=har_data
    )
    workflow_app.ARTIFACT_MANAGER.create_artifact.assert_awaited_once_with(
        step=last_step, artifact_type=ArtifactType.HAR, data=har_data
    )
    workflow_app.ARTIFACT_MANAGER.create_task_archive.assert_awaited_once_with(
        step=last_step,
        entries={"har.har": (ArtifactType.HAR, har_data)},
        workflow_run_id=workflow_run.workflow_run_id,
    )
