"""``workflow_run_contexts`` must be evicted when a workflow run is cleaned up.

Each entry holds the run's parameters, secrets, and outputs; without eviction
the dict grows for the life of the process.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.service import WorkflowService


def _context_manager_with(run_ids: list[str]) -> WorkflowContextManager:
    manager = WorkflowContextManager.__new__(WorkflowContextManager)
    manager.workflow_run_contexts = {run_id: MagicMock() for run_id in run_ids}
    return manager


def test_remove_workflow_run_context_evicts_and_is_idempotent() -> None:
    manager = _context_manager_with(["wr_1"])

    manager.remove_workflow_run_context("wr_1")
    assert "wr_1" not in manager.workflow_run_contexts

    # Removing an unknown / already-removed run must not raise.
    manager.remove_workflow_run_context("wr_1")
    manager.remove_workflow_run_context("wr_never_seen")


@pytest.mark.asyncio
async def test_clean_up_workflow_evicts_run_and_child_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    context_manager = _context_manager_with(["wr_parent", "wr_child", "wr_other"])
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", context_manager)

    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.analytics.capture", MagicMock(), raising=False)
    monkeypatch.setattr(service, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))

    child_run = MagicMock()
    child_run.workflow_run_id = "wr_child"
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[child_run]),
        raising=False,
    )
    monkeypatch.setattr(app.BROWSER_MANAGER, "cleanup_for_workflow_run", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock(), raising=False)
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock(), raising=False)

    workflow = MagicMock()
    workflow.persist_browser_session = False
    workflow_run = MagicMock()
    workflow_run.workflow_run_id = "wr_parent"
    workflow_run.organization_id = "org_1"
    workflow_run.browser_address = None
    workflow_run.status = "completed"

    await service.clean_up_workflow(
        workflow=workflow,
        workflow_run=workflow_run,
        need_call_webhook=False,
    )

    assert "wr_parent" not in context_manager.workflow_run_contexts
    assert "wr_child" not in context_manager.workflow_run_contexts
    assert "wr_other" in context_manager.workflow_run_contexts
