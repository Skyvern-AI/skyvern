"""``workflow_run_contexts`` must be evicted when a workflow run is cleaned up.

Each entry holds the run's parameters, secrets, and outputs; without eviction
the dict grows for the life of the process.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.har_evidence import HarEvidence, HarEvidenceAvailability
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.webeye.browser_manager import BrowserCleanupResult
from tests.unit.conftest import cancel_once_parked


def _context_manager_with(run_ids: list[str]) -> WorkflowContextManager:
    manager = WorkflowContextManager.__new__(WorkflowContextManager)
    manager.workflow_run_contexts = {}
    for run_id in run_ids:
        context = MagicMock()
        context.drain_failure_evidence_capture = AsyncMock()
        manager.workflow_run_contexts[run_id] = context
    return manager


def test_remove_workflow_run_context_evicts_and_is_idempotent() -> None:
    manager = _context_manager_with(["wr_1"])

    manager.remove_workflow_run_context("wr_1")
    assert "wr_1" not in manager.workflow_run_contexts

    # Removing an unknown / already-removed run must not raise.
    manager.remove_workflow_run_context("wr_1")
    manager.remove_workflow_run_context("wr_never_seen")


def test_has_workflow_run_context_tracks_process_local_liveness() -> None:
    # The non-PBS sharing predicate reads this as its run-liveness signal: True only while the run's
    # context is registered in THIS process, flipping to False once cleanup removes it.
    manager = _context_manager_with(["wr_live"])

    assert manager.has_workflow_run_context("wr_live") is True
    assert manager.has_workflow_run_context("wr_absent") is False

    manager.remove_workflow_run_context("wr_live")
    assert manager.has_workflow_run_context("wr_live") is False


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
    monkeypatch.setattr(
        app.BROWSER_MANAGER,
        "cleanup_for_workflow_run",
        AsyncMock(return_value=BrowserCleanupResult(browser_state=None, recording_finalized=False)),
        raising=False,
    )
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


def _diagnostic_cleanup(monkeypatch: pytest.MonkeyPatch, annotate: AsyncMock) -> tuple[WorkflowService, list[str]]:
    service = WorkflowService()
    order: list[str] = []
    evidence = HarEvidence(availability=HarEvidenceAvailability.available)
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", _context_manager_with(["wr_1"]))
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.analytics.capture", MagicMock(), raising=False)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.is_retry_eligible_run", AsyncMock(return_value=False))
    monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[]), raising=False)
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_workflow_run_terminal", AsyncMock(), raising=False)
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock(), raising=False)
    monkeypatch.setattr(app.STORAGE, "save_downloaded_files", AsyncMock(), raising=False)
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.service.uploaded_file_service.delete_files_attached_to_run", AsyncMock()
    )
    monkeypatch.setattr(service, "_drain_failure_evidence_captures", AsyncMock())
    monkeypatch.setattr(service, "persist_video_data", AsyncMock())
    monkeypatch.setattr(
        service, "persist_debug_artifacts", AsyncMock(side_effect=lambda *a, **k: order.append("har") or evidence)
    )
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.annotate_failed_workflow_run", annotate)
    monkeypatch.setattr(
        service, "execute_workflow_webhook", AsyncMock(side_effect=lambda *a, **k: order.append("webhook"))
    )
    return service, order


def _cleanup_result() -> MagicMock:
    return MagicMock(
        browser_state=MagicMock(),
        tasks=[MagicMock()],
        all_workflow_task_ids=[],
        child_workflow_run_ids=[],
        close_browser_on_completion=True,
        browser_session_write_back_attempted=True,
    )


@pytest.mark.asyncio
async def test_clean_up_workflow_records_the_final_diagnostic_after_the_har_and_before_the_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    annotate = AsyncMock()
    service, order = _diagnostic_cleanup(monkeypatch, annotate)
    annotate.side_effect = lambda *a, **k: order.append("diagnostic")
    workflow_run = MagicMock(workflow_run_id="wr_1", organization_id="org_1", browser_address=None, status="failed")

    await service.clean_up_workflow(
        workflow=MagicMock(),
        workflow_run=workflow_run,
        api_key="sk",
        browser_cleanup_result=_cleanup_result(),
        final_diagnostic_owner=workflow_run,
    )

    assert order == ["har", "diagnostic", "webhook"]
    assert annotate.await_args.args[0] is workflow_run


@pytest.mark.asyncio
@pytest.mark.parametrize("need_call_webhook", [True, False])
async def test_cancelling_the_diagnostic_still_runs_the_cleanup_tail_and_evicts_before_propagating(
    monkeypatch: pytest.MonkeyPatch, need_call_webhook: bool
) -> None:
    parked, release = asyncio.Event(), asyncio.Event()

    async def park(*_: object, **__: object) -> None:
        parked.set()
        await release.wait()

    service, order = _diagnostic_cleanup(monkeypatch, AsyncMock(side_effect=park))
    context = SkyvernContext(workflow_run_id="wr_1")
    context.totp_codes["tsk_w"] = "123456"
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.skyvern_context.current", lambda: context)
    for target, step in (
        (app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks"),
        (app.STORAGE, "save_downloaded_files"),
    ):
        monkeypatch.setattr(target, step, AsyncMock(side_effect=lambda *_, step=step, **__: order.append(step)))
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.service.uploaded_file_service.delete_files_attached_to_run",
        AsyncMock(side_effect=lambda *_, **__: order.append("attachments")),
    )
    cleanup_result = _cleanup_result()
    cleanup_result.all_workflow_task_ids = ["tsk_w"]
    workflow_run = MagicMock(workflow_run_id="wr_1", organization_id="org_1", browser_address=None, status="failed")
    state_at_exit: list[tuple[bool, bool]] = []

    async def owned() -> None:
        try:
            await service.clean_up_workflow(
                workflow=MagicMock(),
                workflow_run=workflow_run,
                api_key="sk",
                browser_cleanup_result=cleanup_result,
                need_call_webhook=need_call_webhook,
                final_diagnostic_owner=workflow_run,
            )
        finally:
            state_at_exit.append(
                ("tsk_w" in context.totp_codes, "wr_1" in app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts)
            )

    cleanup = await cancel_once_parked(owned(), parked, release)

    assert cleanup.cancelled()
    tail = ["wait_for_upload_aiotasks", "save_downloaded_files", "attachments"]
    assert order == ["har", *tail, *(["webhook"] if need_call_webhook else [])]
    assert state_at_exit == [(False, False)]
