"""Broad-PBS service gate: a caller-provided remote browser (``workflow_run.browser_address``)
must never be terminal-closed by workflow cleanup, even with no ``browser_session_id``. The durable
remote browser's lifetime is not owned by this process, so ``WorkflowService`` forces
``close_browser_on_completion=False`` into the browser manager regardless of the caller's request
(PBS/non-PBS boundary)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye.browser_manager import BrowserCleanupResult


@pytest.mark.asyncio
async def test_clean_up_workflow_browser_forces_close_off_for_browser_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = WorkflowService()
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_addr",
        organization_id="o_test",
        browser_address="http://remote-cdp:9222",
    )
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[]),
    )
    cleanup = AsyncMock(return_value=BrowserCleanupResult(browser_state=None, recording_finalized=False))
    monkeypatch.setattr(service_module.app.BROWSER_MANAGER, "cleanup_for_workflow_run", cleanup)

    # The caller asks to close on completion, but the browser_address gate must override it.
    await svc._clean_up_workflow_browser(workflow_run, close_browser_on_completion=True, browser_session_id=None)

    cleanup.assert_awaited_once()
    assert cleanup.await_args.kwargs["close_browser_on_completion"] is False
    assert cleanup.await_args.kwargs["browser_session_id"] is None


@pytest.mark.asyncio
async def test_clean_up_workflow_browser_allows_close_for_local_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Control: a plain local run (no browser_address, no session id) keeps the caller's close request,
    # so the gate is specific to the broad-PBS remote-address case and does not over-suppress.
    svc = WorkflowService()
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_local",
        organization_id="o_test",
        browser_address=None,
    )
    monkeypatch.setattr(svc, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[]),
    )
    cleanup = AsyncMock(return_value=BrowserCleanupResult(browser_state=None, recording_finalized=False))
    monkeypatch.setattr(service_module.app.BROWSER_MANAGER, "cleanup_for_workflow_run", cleanup)

    await svc._clean_up_workflow_browser(workflow_run, close_browser_on_completion=True, browser_session_id=None)

    cleanup.assert_awaited_once()
    assert cleanup.await_args.kwargs["close_browser_on_completion"] is True
