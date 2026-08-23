"""Tests for the copilot orphan-workflow cancellation helpers.

Covers:
- ``_cancel_run_task_if_not_final`` cancels ``run_task`` and writes the
  conditional cancel exactly once.
- A SUCCESS path (run_task completes on its own) still calls the conditional
  cancel, but because the row is terminal the real helper would be a no-op.
- An SDK-realistic ``asyncio.wait_for`` timeout around the tool coroutine does
  not leave ``run_task`` running in the background.
- ``_trusted_post_drain_status`` accepts post-drain rows that landed in a
  non-ambiguous terminal state (``completed``/``failed``/``terminated``/
  ``timed_out``) and rejects ``canceled``. At post-drain read time a
  ``canceled`` row can't be told apart from the synthetic ``canceled`` that
  ``mark_workflow_run_as_canceled_if_not_final`` writes as a last-resort
  fallback; callsites that want the legitimate-``canceled`` signal read the
  row BEFORE invoking the cancel helper.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.tools import _cancel_run_task_if_not_final, _trusted_post_drain_status
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


class _FakeService:
    def __init__(self) -> None:
        self.mark_calls: list[str] = []
        self.raise_on_mark: Exception | None = None

    async def mark_workflow_run_as_canceled_if_not_final(
        self,
        workflow_run_id: str,
    ) -> Any:
        self.mark_calls.append(workflow_run_id)
        if self.raise_on_mark is not None:
            raise self.raise_on_mark
        return None


@pytest.mark.asyncio
async def test_cancel_helper_cancels_task_and_writes_conditional_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge import app as forge_app

    service = _FakeService()
    monkeypatch.setattr(forge_app, "WORKFLOW_SERVICE", service)

    async def long_running() -> None:
        await asyncio.sleep(60)

    run_task = asyncio.create_task(long_running())

    await _cancel_run_task_if_not_final(run_task, workflow_run_id="wr_1")

    assert run_task.cancelled() or run_task.done()
    assert service.mark_calls == ["wr_1"]


@pytest.mark.asyncio
async def test_cancel_helper_does_not_raise_on_mark_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secondary errors during cleanup are logged, not propagated — otherwise
    they would replace the original timeout/cancellation surface."""
    from skyvern.forge import app as forge_app

    service = _FakeService()
    service.raise_on_mark = RuntimeError("DB is down")
    monkeypatch.setattr(forge_app, "WORKFLOW_SERVICE", service)

    async def long_running() -> None:
        await asyncio.sleep(60)

    run_task = asyncio.create_task(long_running())

    # Must not raise despite the mark raising.
    await _cancel_run_task_if_not_final(run_task, workflow_run_id="wr_2")


@pytest.mark.asyncio
async def test_cancel_helper_handles_already_completed_run_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_task has already finished (natural completion), the helper
    should still issue the conditional cancel — it is a no-op at the DB layer
    if the row is already terminal, so the result is harmless."""
    from skyvern.forge import app as forge_app

    service = _FakeService()
    monkeypatch.setattr(forge_app, "WORKFLOW_SERVICE", service)

    async def quick() -> None:
        return

    run_task = asyncio.create_task(quick())
    await run_task

    await _cancel_run_task_if_not_final(run_task, workflow_run_id="wr_3")
    assert service.mark_calls == ["wr_3"]


@pytest.mark.asyncio
async def test_sdk_style_wait_for_timeout_does_not_leak_background_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the production failure mode: the OpenAI Agents SDK wraps the
    tool coroutine in ``asyncio.wait_for(..., timeout=N)`` and cancels it on
    timeout. Our CancelledError branch must cancel ``run_task`` through the
    helper so no orphan work is left behind."""
    from skyvern.forge import app as forge_app

    service = _FakeService()
    monkeypatch.setattr(forge_app, "WORKFLOW_SERVICE", service)

    run_task_ref: dict[str, asyncio.Task] = {}
    workflow_work_completed = asyncio.Event()

    async def tool_body() -> None:
        async def workflow_body() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                workflow_work_completed.set()
                raise

        run_task = asyncio.create_task(workflow_body())
        run_task_ref["run_task"] = run_task
        try:
            # Simulate the inner poll loop.
            while True:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(_cancel_run_task_if_not_final(run_task, workflow_run_id="wr_sdk"))
            except asyncio.CancelledError:
                # Detached fallback mirror of the production path.
                fallback = asyncio.ensure_future(_cancel_run_task_if_not_final(run_task, workflow_run_id="wr_sdk"))
                await asyncio.wait_for(asyncio.shield(fallback), timeout=5.0)
            raise

    tool_task = asyncio.ensure_future(tool_body())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(tool_task, timeout=0.2)

    # Workflow's CancelledError handler should have fired via our helper.
    await asyncio.wait_for(workflow_work_completed.wait(), timeout=1.0)

    assert "run_task" in run_task_ref
    assert run_task_ref["run_task"].cancelled() or run_task_ref["run_task"].done()
    assert service.mark_calls == ["wr_sdk"]


# ---------------------------------------------------------------------------
# _trusted_post_drain_status
# ---------------------------------------------------------------------------
#
# After the poll loop exhausts its budget, ``_cancel_run_task_if_not_final``
# waits briefly for ``execute_workflow``'s shielded finalize, then writes
# synthetic ``canceled`` as a last-resort fallback when nothing else finalized
# the row. At post-drain read time a row with status ``canceled`` could be
# either that synthetic fallback or a legitimate cancel that the drain
# restored — the two are indistinguishable on disk. ``_trusted_post_drain_status``
# therefore only accepts the unambiguous terminal statuses; the callsite
# reads the row BEFORE the cancel helper runs to catch any legitimate
# ``canceled`` self-finalize.


def _run_with_status(status: str) -> SimpleNamespace:
    return SimpleNamespace(status=status)


@pytest.mark.parametrize(
    "status",
    [
        WorkflowRunStatus.completed.value,
        WorkflowRunStatus.failed.value,
        WorkflowRunStatus.terminated.value,
        WorkflowRunStatus.timed_out.value,
    ],
)
def test_trusted_post_drain_accepts_real_terminal_statuses(status: str) -> None:
    """Statuses reachable via ``_finalize_workflow_run_status`` — the drain's
    real finalize step — let the tool fall through into the normal result
    path so ``run_ok`` / ``failure_reason`` land correctly for the LLM."""
    run = _run_with_status(status)
    assert _trusted_post_drain_status(run) == status


def test_trusted_post_drain_rejects_canceled() -> None:
    """``canceled`` at post-drain read time is ambiguous: it could be the
    synthetic fallback that ``mark_workflow_run_as_canceled_if_not_final``
    writes when the run was genuinely stuck, OR a legitimate cancel that
    ``_finalize_workflow_run_status`` restored. The helper rejects both; the
    callsite handles the legitimate case with a pre-cancel read instead."""
    run = _run_with_status(WorkflowRunStatus.canceled.value)
    assert _trusted_post_drain_status(run) is None


@pytest.mark.parametrize(
    "status",
    [
        WorkflowRunStatus.created.value,
        WorkflowRunStatus.queued.value,
        WorkflowRunStatus.running.value,
        WorkflowRunStatus.paused.value,
    ],
)
def test_trusted_post_drain_rejects_non_final_statuses(status: str) -> None:
    """A non-final post-drain status means the drain did not manage to
    reconcile the row within its grace window. Emit the timeout error."""
    run = _run_with_status(status)
    assert _trusted_post_drain_status(run) is None


def test_trusted_post_drain_handles_missing_row() -> None:
    """A missing row (DB read returned None) is treated the same as a
    non-final row: no trusted status, caller emits the timeout error."""
    assert _trusted_post_drain_status(None) is None
