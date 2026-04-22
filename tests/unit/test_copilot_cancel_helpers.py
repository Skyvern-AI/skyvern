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


# ---------------------------------------------------------------------------
# Reconciliation guard via ``_tool_loop_error``
# ---------------------------------------------------------------------------
#
# When a block-running tool times out and the post-drain read doesn't
# reconcile the row to a trustworthy-final status, the tool sets
# ``pending_reconciliation_run_id`` on ``CopilotContext``. Until a
# ``get_run_results`` call observes that run in a trustworthy-final status,
# ``_tool_loop_error`` rejects further block-running tool calls so the LLM
# cannot re-invoke a mutation block whose side effects may have landed.

from types import SimpleNamespace as _NS  # noqa: E402  (grouped with test block)

from skyvern.forge.sdk.copilot.tools import _BLOCK_RUNNING_TOOLS, _tool_loop_error  # noqa: E402


def _guard_ctx(pending_run_id: str | None = None) -> _NS:
    """Minimal ctx stub for ``_tool_loop_error``: only the fields it reads."""
    return _NS(
        consecutive_tool_tracker=[],
        repeated_action_fingerprint_streak_count=0,
        last_test_non_retriable_nav_error=None,
        pending_reconciliation_run_id=pending_run_id,
    )


@pytest.mark.parametrize("tool_name", sorted(_BLOCK_RUNNING_TOOLS))
def test_reconciliation_guard_blocks_block_running_tools(tool_name: str) -> None:
    """With a pending run set, every tool in ``_BLOCK_RUNNING_TOOLS`` is
    rejected with an error that names the run id and directs the LLM to call
    ``get_run_results`` first."""
    ctx = _guard_ctx(pending_run_id="wr_pending_123")
    err = _tool_loop_error(ctx, tool_name)
    assert isinstance(err, str)
    assert "wr_pending_123" in err
    assert "get_run_results" in err


@pytest.mark.parametrize(
    "tool_name",
    ["get_run_results", "update_workflow", "list_credentials"],
)
def test_reconciliation_guard_ignores_non_block_running_tools(tool_name: str) -> None:
    """The guard is scoped to ``_BLOCK_RUNNING_TOOLS``. Planning / metadata
    tools (including ``get_run_results`` itself, which is the tool that can
    CLEAR the flag) must not be rejected."""
    ctx = _guard_ctx(pending_run_id="wr_pending_123")
    assert _tool_loop_error(ctx, tool_name) is None


def test_reconciliation_guard_passes_when_flag_empty() -> None:
    """No pending run → `_tool_loop_error` returns None for block-running
    tools (assuming no other guard trips)."""
    ctx = _guard_ctx(pending_run_id=None)
    for name in _BLOCK_RUNNING_TOOLS:
        assert _tool_loop_error(ctx, name) is None


def test_reconciliation_guard_rejects_empty_string_run_id() -> None:
    """An empty string is treated the same as None — the flag is considered
    not set. Prevents a spurious guard trip if anything ever clears the flag
    to ``''`` instead of ``None``."""
    ctx = _guard_ctx(pending_run_id="")
    for name in _BLOCK_RUNNING_TOOLS:
        assert _tool_loop_error(ctx, name) is None


# ---------------------------------------------------------------------------
# Clearing the reconciliation guard: ``_maybe_clear_reconciliation_flag``
# ---------------------------------------------------------------------------
#
# ``get_run_results_tool`` calls this helper after ``_get_run_results``
# returns. Testing the helper directly keeps the tests targeted and avoids
# the Agents-SDK ``@function_tool`` wrapper machinery.


from skyvern.forge.sdk.copilot.tools import _maybe_clear_reconciliation_flag  # noqa: E402


def _ctx_with_pending(run_id: str | None) -> _NS:
    return _NS(pending_reconciliation_run_id=run_id)


def test_reconciliation_flag_clears_on_matching_trusted_final_status() -> None:
    """Same run_id AND a trustworthy-final status (``completed``/``failed``/
    ``terminated``/``timed_out``) — the flag clears so the LLM can resume."""
    ctx = _ctx_with_pending("wr_match")
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_match",
            "overall_status": WorkflowRunStatus.completed.value,
        },
    }
    _maybe_clear_reconciliation_flag(ctx, result)
    assert ctx.pending_reconciliation_run_id is None


def test_reconciliation_flag_does_not_clear_on_matching_canceled() -> None:
    """The canceled case is the whole reason the guard exists. Even an
    explicit read of the pending run that returns ``canceled`` must keep
    the guard set — the LLM should report to the user and await input."""
    ctx = _ctx_with_pending("wr_match")
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_match",
            "overall_status": WorkflowRunStatus.canceled.value,
        },
    }
    _maybe_clear_reconciliation_flag(ctx, result)
    assert ctx.pending_reconciliation_run_id == "wr_match"


def test_reconciliation_flag_does_not_clear_on_non_matching_run_id() -> None:
    """``get_run_results(workflow_run_id=None)`` resolves internally to the
    most-recent finished run. If that resolves to a different run than the
    one the guard is pending on, the flag must NOT clear."""
    ctx = _ctx_with_pending("wr_match")
    result = {
        "ok": True,
        "data": {
            "workflow_run_id": "wr_other",
            "overall_status": WorkflowRunStatus.completed.value,
        },
    }
    _maybe_clear_reconciliation_flag(ctx, result)
    assert ctx.pending_reconciliation_run_id == "wr_match"


@pytest.mark.parametrize(
    "status",
    [
        WorkflowRunStatus.running.value,
        WorkflowRunStatus.queued.value,
        WorkflowRunStatus.paused.value,
    ],
)
def test_reconciliation_flag_does_not_clear_on_non_final_status(status: str) -> None:
    """A matching run_id but a non-final status (e.g. ``running``) leaves
    the flag set — the run hasn't reached a trustworthy outcome yet."""
    ctx = _ctx_with_pending("wr_match")
    result = {
        "ok": True,
        "data": {"workflow_run_id": "wr_match", "overall_status": status},
    }
    _maybe_clear_reconciliation_flag(ctx, result)
    assert ctx.pending_reconciliation_run_id == "wr_match"


def test_reconciliation_flag_noop_when_unset() -> None:
    """If no reconciliation is pending, the helper must be a silent no-op —
    even when the result shape is unexpected."""
    ctx = _ctx_with_pending(None)
    _maybe_clear_reconciliation_flag(ctx, {"ok": False, "error": "x"})
    assert ctx.pending_reconciliation_run_id is None


def test_reconciliation_flag_noop_on_malformed_result() -> None:
    """A result without a `data` dict (e.g. an error envelope) must leave
    the flag untouched — we only clear when we can affirmatively see a
    trusted status for the pending run_id."""
    ctx = _ctx_with_pending("wr_match")
    _maybe_clear_reconciliation_flag(ctx, {"ok": False, "error": "run not found"})
    assert ctx.pending_reconciliation_run_id == "wr_match"
