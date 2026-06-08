"""Tests for persistent browser profile write-back gating.

The persistent browser session should only be written back to S3 when the
workflow run completes successfully.  Crashed or failed runs must NOT
overwrite the shared S3 profile with their dirty state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


def _make_workflow(persist: bool = True) -> MagicMock:
    wf = MagicMock()
    wf.persist_browser_session = persist
    wf.workflow_permanent_id = "wpid_test"
    return wf


def _make_workflow_run(status: WorkflowRunStatus) -> MagicMock:
    wr = MagicMock()
    wr.workflow_run_id = "wr_test"
    wr.organization_id = "o_test"
    wr.status = status
    wr.browser_profile_id = None
    wr.browser_address = None
    wr.webhook_callback_url = None
    wr.created_at = None
    wr.workflow_permanent_id = "wpid_test"
    return wr


def _make_browser_state() -> MagicMock:
    bs = MagicMock()
    bs.browser_artifacts.browser_session_dir = "/tmp/fake_profile"
    return bs


def _patch_clean_up_deps(monkeypatch: pytest.MonkeyPatch, browser_state: MagicMock) -> AsyncMock:
    """Patch all external dependencies of clean_up_workflow. Returns the store mock."""
    store_mock = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_session", store_mock)
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(app.BROWSER_MANAGER, "cleanup_for_workflow_run", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[]),
    )
    return store_mock


@pytest.mark.asyncio
async def test_profile_persisted_on_completed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Completed runs should write the browser profile back to S3."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed)
    browser_state = _make_browser_state()
    store_mock = _patch_clean_up_deps(monkeypatch, browser_state)

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    store_mock.assert_awaited_once_with("o_test", "wpid_test", "/tmp/fake_profile")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        WorkflowRunStatus.failed,
        WorkflowRunStatus.terminated,
        WorkflowRunStatus.canceled,
        WorkflowRunStatus.timed_out,
        WorkflowRunStatus.running,
    ],
)
async def test_profile_not_persisted_on_non_completed_run(
    monkeypatch: pytest.MonkeyPatch,
    status: WorkflowRunStatus,
) -> None:
    """Non-completed runs must NOT write the browser profile back to S3."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(status)
    browser_state = _make_browser_state()
    store_mock = _patch_clean_up_deps(monkeypatch, browser_state)

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    store_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_cookies_persisted_before_store_when_browser_stays_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote-browser / existing-session runs keep the browser alive, so close() never persists the
    sidecar; clean_up_workflow must snapshot session cookies before archiving the profile."""
    from skyvern.forge.sdk.workflow import service as service_module
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed)
    workflow_run.browser_address = "ws://remote-browser"
    browser_state = _make_browser_state()
    store_mock = _patch_clean_up_deps(monkeypatch, browser_state)

    order: list[str] = []
    persist_mock = AsyncMock(side_effect=lambda *a, **k: order.append("persist"))
    store_mock.side_effect = lambda *a, **k: order.append("store")
    monkeypatch.setattr(service_module, "persist_session_cookies", persist_mock)

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    persist_mock.assert_awaited_once_with(browser_state.browser_context, "/tmp/fake_profile")
    assert order == ["persist", "store"]


@pytest.mark.asyncio
async def test_session_cookies_not_double_persisted_when_browser_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the browser closes on completion, close() already wrote the sidecar — clean_up_workflow
    must not persist again."""
    from skyvern.forge.sdk.workflow import service as service_module
    from skyvern.forge.sdk.workflow.service import WorkflowService

    workflow = _make_workflow(persist=True)
    workflow_run = _make_workflow_run(WorkflowRunStatus.completed)
    browser_state = _make_browser_state()
    _patch_clean_up_deps(monkeypatch, browser_state)

    persist_mock = AsyncMock()
    monkeypatch.setattr(service_module, "persist_session_cookies", persist_mock)

    svc = WorkflowService()
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    await svc.clean_up_workflow(workflow=workflow, workflow_run=workflow_run, need_call_webhook=False)

    persist_mock.assert_not_awaited()
