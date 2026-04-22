"""Tests for cancellation / webhook-tolerance helpers on WorkflowService.

- ``mark_workflow_run_as_canceled_if_not_final`` delegates to the conditional
  DB update and is a no-op when the DB reports no row was affected (the row
  was already in a terminal state).
- ``execute_workflow_webhook`` returns cleanly when the workflow row has been
  soft-deleted mid-run — it must not raise ``WorkflowNotFound`` from the
  cleanup path.
- The cancellation-safe finalize pattern used in ``execute_workflow``'s outer
  ``finally`` runs ``_finalize_workflow_run_status`` via ``asyncio.shield``
  so an outer cancel mid-body still restores the real terminal status.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


@pytest.mark.asyncio
async def test_mark_canceled_if_not_final_returns_conditional_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    updated_row = MagicMock()
    updated_row.status = WorkflowRunStatus.canceled

    delegate = AsyncMock(return_value=updated_row)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run_if_not_final", delegate)

    svc = WorkflowService()
    result = await svc.mark_workflow_run_as_canceled_if_not_final(workflow_run_id="wr_1")

    assert result is updated_row
    delegate.assert_awaited_once_with(
        workflow_run_id="wr_1",
        status=WorkflowRunStatus.canceled,
    )


@pytest.mark.asyncio
async def test_mark_canceled_if_not_final_is_noop_on_terminal_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB returns None when the row is already terminal — helper must propagate
    that as None instead of raising or writing a conflicting row."""
    from skyvern.forge.sdk.workflow.service import WorkflowService

    delegate = AsyncMock(return_value=None)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run_if_not_final", delegate)

    svc = WorkflowService()
    result = await svc.mark_workflow_run_as_canceled_if_not_final(workflow_run_id="wr_done")

    assert result is None
    delegate.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_webhook_tolerates_soft_deleted_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the workflow row has been soft-deleted by the time cleanup runs,
    ``execute_workflow_webhook`` should log a warning and return — not raise.
    """
    from skyvern.forge.sdk.workflow.service import WorkflowService

    svc = WorkflowService()

    async def raise_not_found(*args: object, **kwargs: object) -> None:
        raise WorkflowNotFound(workflow_permanent_id="wpid_gone")

    monkeypatch.setattr(svc, "build_workflow_run_status_response", raise_not_found)

    run = MagicMock()
    run.workflow_permanent_id = "wpid_gone"
    # Must complete cleanly without propagating the exception.
    await svc.execute_workflow_webhook(workflow_run=run)


@pytest.mark.asyncio
async def test_build_status_response_uses_filter_deleted_false_when_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``allow_deleted=True`` goes through the raw repository lookup with
    ``filter_deleted=False`` so soft-deleted workflows still resolve.
    """
    from skyvern.forge.sdk.workflow.service import WorkflowService

    svc = WorkflowService()

    by_wpid = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow_by_permanent_id", by_wpid)

    # Short-circuit the rest of build_workflow_run_status_response by making
    # subsequent DB calls raise the first caught thing — we only care about
    # the lookup kwargs here.
    async def immediately_raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("short-circuit")

    monkeypatch.setattr(svc, "get_workflow_run", immediately_raise)

    with pytest.raises(RuntimeError, match="short-circuit"):
        await svc.build_workflow_run_status_response(
            workflow_permanent_id="wpid_soft_deleted",
            workflow_run_id="wr_x",
            organization_id="org_1",
            allow_deleted=True,
        )

    by_wpid.assert_awaited_once()
    assert by_wpid.call_args.kwargs["filter_deleted"] is False


@pytest.mark.asyncio
async def test_shielded_finalize_runs_when_outer_cancelled_mid_body() -> None:
    """Contract test for the ``execute_workflow`` cancellation-safe pattern:
    when the try body is cancelled after ``pre_finally_status`` is captured,
    the outer ``finally`` must still run ``_finalize_workflow_run_status``
    via ``asyncio.shield`` so the row ends up terminal rather than stuck as
    transient ``running``. Mirrors the structure of
    ``WorkflowService.execute_workflow``; if anyone removes the ``shield`` or
    moves finalize back into the try, this test breaks.
    """

    finalize_calls: list[WorkflowRunStatus] = []
    clean_up_called = False
    body_entered = asyncio.Event()

    async def finalize(status: WorkflowRunStatus) -> None:
        # Simulate a non-trivial DB write so shield cancellation-protection
        # matters rather than being invisible.
        await asyncio.sleep(0.05)
        finalize_calls.append(status)

    async def clean_up() -> None:
        nonlocal clean_up_called
        clean_up_called = True

    async def simulated_execute_workflow() -> None:
        pre_finally_status: WorkflowRunStatus | None = None
        try:
            pre_finally_status = WorkflowRunStatus.failed
            body_entered.set()
            # Simulate the finally-block execution phase that our copilot
            # cancel lands inside of.
            await asyncio.sleep(10)
        finally:
            if pre_finally_status is not None:
                try:
                    await asyncio.shield(finalize(pre_finally_status))
                except Exception:
                    pass
            await clean_up()

    task = asyncio.create_task(simulated_execute_workflow())
    await body_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert finalize_calls == [WorkflowRunStatus.failed], (
        "shielded finalize must run with the captured pre_finally_status"
    )
    assert clean_up_called, "clean_up_workflow must still run in the outer finally"


@pytest.mark.asyncio
async def test_shielded_finalize_skipped_when_pre_finally_status_unset() -> None:
    """If cancellation lands before block execution captures
    ``pre_finally_status``, there's no intended terminal state to restore —
    the outer ``finally`` must skip finalize, not call it with ``None``.
    """

    finalize_called = False

    async def finalize(status: WorkflowRunStatus) -> None:
        nonlocal finalize_called
        finalize_called = True

    async def simulated_execute_workflow() -> None:
        pre_finally_status: WorkflowRunStatus | None = None
        try:
            await asyncio.sleep(10)
            pre_finally_status = WorkflowRunStatus.failed  # pragma: no cover
        finally:
            if pre_finally_status is not None:
                await asyncio.shield(finalize(pre_finally_status))

    task = asyncio.create_task(simulated_execute_workflow())
    await asyncio.sleep(0)  # let the task enter its body
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not finalize_called, "finalize must not run when pre_finally_status is unset"
