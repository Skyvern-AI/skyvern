"""Tests for cancellation / webhook-tolerance helpers on WorkflowService.

- ``mark_workflow_run_as_canceled_if_not_final`` delegates to the conditional
  DB update and is a no-op when the DB reports no row was affected - either
  because the row was already in a terminal state or because no row with that
  id exists.
- ``mark_workflow_run_as_canceled`` rejects transitions to ``canceled`` when
  the run has already reached a terminal state (SKY-9188).
- ``execute_workflow_webhook`` returns cleanly when the workflow row has been
  soft-deleted mid-run — it must not raise ``WorkflowNotFound`` from the
  cleanup path.
- The cancellation-safe finalize pattern used in ``execute_workflow``'s outer
  ``finally`` runs ``_finalize_workflow_run_status`` via ``asyncio.shield``
  so an outer cancel mid-body still restores the real terminal status.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import WorkflowNotFound, WorkflowRunNotFound
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


def _make_updated_row(now: datetime | None = None) -> MagicMock:
    now = now or datetime.now(UTC)
    row = MagicMock()
    row.status = WorkflowRunStatus.canceled
    row.created_at = now - timedelta(seconds=30)
    row.started_at = now - timedelta(seconds=20)
    row.workflow_id = "wf_abc"
    row.organization_id = "org_abc"
    row.run_with = None
    row.ai_fallback = False
    row.trigger_type = None
    row.workflow_schedule_id = None
    return row


@pytest.mark.asyncio
async def test_mark_canceled_if_not_final_logs_duration_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal-transition parity with ``_update_workflow_run_status``: a
    successful conditional cancel must emit the ``Workflow run duration metrics``
    log with the same structured fields, so the metric stays comparable across
    statuses.
    """
    from skyvern.forge.sdk.workflow import service as service_module
    from skyvern.forge.sdk.workflow.service import WorkflowService

    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    updated_row = _make_updated_row(fixed_now)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(service_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "update_workflow_run_if_not_final",
        AsyncMock(return_value=updated_row),
    )
    monkeypatch.setattr(
        WorkflowService,
        "_sync_task_run_from_workflow_run",
        AsyncMock(return_value=None),
    )

    info_calls: list[tuple[str, dict]] = []

    def fake_info(event: str, **kwargs: object) -> None:
        info_calls.append((event, dict(kwargs)))

    monkeypatch.setattr(service_module.LOG, "info", fake_info)

    svc = WorkflowService()
    result = await svc.mark_workflow_run_as_canceled_if_not_final(workflow_run_id="wr_live")

    assert result is updated_row
    metrics_events = [kwargs for event, kwargs in info_calls if event == "Workflow run duration metrics"]
    assert len(metrics_events) == 1
    metrics = metrics_events[0]
    assert metrics["workflow_run_id"] == "wr_live"
    assert metrics["workflow_id"] == "wf_abc"
    assert metrics["organization_id"] == "org_abc"
    assert metrics["workflow_run_status"] == WorkflowRunStatus.canceled
    assert metrics["queued_seconds"] == pytest.approx(10.0, abs=1.0)
    assert metrics["duration_seconds"] == pytest.approx(20.0, abs=1.0)


@pytest.mark.asyncio
async def test_mark_canceled_if_not_final_syncs_task_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal-transition parity: a successful conditional cancel must spawn
    the ``_sync_task_run_from_workflow_run`` write-through so downstream
    consumers reading ``task_runs`` see the cancel event.
    """
    from skyvern.forge.sdk.workflow.service import WorkflowService

    updated_row = _make_updated_row()
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "update_workflow_run_if_not_final",
        AsyncMock(return_value=updated_row),
    )
    sync_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(WorkflowService, "_sync_task_run_from_workflow_run", sync_mock)

    svc = WorkflowService()
    await svc.mark_workflow_run_as_canceled_if_not_final(workflow_run_id="wr_sync")

    # Let the fire-and-forget background task drain.
    for _ in range(3):
        await asyncio.sleep(0)

    sync_mock.assert_awaited_once()
    call_args = sync_mock.await_args
    assert call_args is not None
    assert call_args.args[0] is updated_row
    assert call_args.args[1] == "wr_sync"
    assert call_args.args[2] == WorkflowRunStatus.canceled


@pytest.mark.asyncio
async def test_mark_canceled_if_not_final_skips_side_effects_on_terminal_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the conditional update returns ``None`` (row already terminal),
    neither the duration-metrics log nor the task_runs sync may fire — the
    terminal status's own ``_update_workflow_run_status`` call already emitted
    them.
    """
    from skyvern.forge.sdk.workflow import service as service_module
    from skyvern.forge.sdk.workflow.service import WorkflowService

    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "update_workflow_run_if_not_final",
        AsyncMock(return_value=None),
    )
    sync_mock = AsyncMock(side_effect=AssertionError("_sync_task_run_from_workflow_run must not run on no-op"))
    monkeypatch.setattr(WorkflowService, "_sync_task_run_from_workflow_run", sync_mock)

    info_calls: list[str] = []

    def fake_info(event: str, **kwargs: object) -> None:
        info_calls.append(event)

    monkeypatch.setattr(service_module.LOG, "info", fake_info)

    svc = WorkflowService()
    result = await svc.mark_workflow_run_as_canceled_if_not_final(workflow_run_id="wr_already_done")

    assert result is None
    assert "Workflow run duration metrics" not in info_calls
    sync_mock.assert_not_awaited()


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
@pytest.mark.parametrize(
    "terminal_status",
    [
        WorkflowRunStatus.completed,
        WorkflowRunStatus.failed,
        WorkflowRunStatus.terminated,
        WorkflowRunStatus.timed_out,
        WorkflowRunStatus.canceled,
    ],
)
async def test_mark_canceled_rejects_transition_on_terminal_row(
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: WorkflowRunStatus,
) -> None:
    """SKY-9188: ``mark_workflow_run_as_canceled`` must not overwrite a
    finalized status. The underlying conditional update returns ``None`` for
    terminal rows; the service helper must propagate the existing row back to
    callers without writing ``canceled``.
    """
    from skyvern.forge.sdk.workflow.service import WorkflowService

    existing_row = MagicMock()
    existing_row.status = terminal_status

    conditional_update = AsyncMock(return_value=None)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run_if_not_final", conditional_update)
    get_workflow_run = AsyncMock(return_value=existing_row)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", get_workflow_run)
    # A guard that accidentally falls through to the unconditional write would
    # call ``update_workflow_run`` — fail loudly if that happens.
    unconditional_update = AsyncMock(side_effect=AssertionError("unconditional update_workflow_run must not be called"))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", unconditional_update)

    svc = WorkflowService()
    result = await svc.mark_workflow_run_as_canceled(workflow_run_id="wr_final")

    assert result is existing_row
    assert result.status == terminal_status
    conditional_update.assert_awaited_once_with(
        workflow_run_id="wr_final",
        status=WorkflowRunStatus.canceled,
    )
    unconditional_update.assert_not_called()


@pytest.mark.asyncio
async def test_mark_canceled_writes_when_row_is_non_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SKY-9188: a non-terminal run still transitions to ``canceled`` through
    the conditional update path.
    """
    from skyvern.forge.sdk.workflow.service import WorkflowService

    canceled_row = MagicMock()
    canceled_row.status = WorkflowRunStatus.canceled

    conditional_update = AsyncMock(return_value=canceled_row)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run_if_not_final", conditional_update)
    get_workflow_run = AsyncMock(side_effect=AssertionError("get_workflow_run must not be called on the happy path"))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", get_workflow_run)

    svc = WorkflowService()
    result = await svc.mark_workflow_run_as_canceled(workflow_run_id="wr_running")

    assert result is canceled_row
    conditional_update.assert_awaited_once_with(
        workflow_run_id="wr_running",
        status=WorkflowRunStatus.canceled,
    )
    get_workflow_run.assert_not_called()


@pytest.mark.asyncio
async def test_mark_canceled_raises_when_row_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the conditional update rejects (returns ``None``) AND the row no
    longer exists, there is no sensible ``WorkflowRun`` to return — raise
    ``WorkflowRunNotFound`` rather than silently pretending the cancel
    succeeded.
    """
    from skyvern.forge.sdk.workflow.service import WorkflowService

    conditional_update = AsyncMock(return_value=None)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run_if_not_final", conditional_update)
    get_workflow_run = AsyncMock(return_value=None)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", get_workflow_run)

    svc = WorkflowService()

    with pytest.raises(WorkflowRunNotFound):
        await svc.mark_workflow_run_as_canceled(workflow_run_id="wr_missing")


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
