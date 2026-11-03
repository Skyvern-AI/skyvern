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
import itertools
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app as forge_app
from skyvern.forge.sdk.copilot import runtime
from skyvern.forge.sdk.copilot.agent_context import AttachedBrowserDriver
from skyvern.forge.sdk.copilot.browser_target import BROWSER_TARGET_PARAM_NAME, resolve_browser_session_binding
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.runtime import browser_session_lock
from skyvern.forge.sdk.copilot.tools import _cancel_run_task_if_not_final, _trusted_post_drain_status, run_execution
from skyvern.forge.sdk.copilot.tools.run_execution import _run_blocks_and_collect_debug
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from tests.unit.copilot_test_helpers import (
    HANDBACK_WORKFLOW_YAML,
    OWNED_RUN_ID,
    SEARCH_THEN_SELECT_WORKFLOW_YAML,
    OwnedRunRow,
    adopted_detached_tasks,
    install_fresh_run_browser,
    install_run_blocks_harness,
    install_secret_carrying_run_registry,
    make_build_test_ctx,
    make_copilot_ctx,
    refuse_seeded_origin_output_once,
    settle_cancelled_tools,
    stamping_browser_manager,
    stop_activity_renewer,
)


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


async def _build_test_under_backstop(
    ctx: CopilotContext, fire_when: asyncio.Event, block_labels: tuple[str, ...] = ("extract_heading",), **run: Any
) -> None:
    """Expire the stream's real timeout once the tool reaches the phase under test, not after a wall-clock delay."""
    async with asyncio.timeout(None) as deadline:
        ctx.model_stream_deadline = deadline

        async def _expire_when_reached() -> None:
            await fire_when.wait()
            deadline.reschedule(asyncio.get_running_loop().time())

        trigger = asyncio.create_task(_expire_when_reached())
        try:
            await _run_blocks_and_collect_debug({"block_labels": list(block_labels), "parameters": {}}, ctx, **run)
        finally:
            trigger.cancel()


@pytest.mark.asyncio
async def test_a_finished_sibling_does_not_unmark_a_concurrent_leavable_run() -> None:
    ctx = make_copilot_ctx()
    ctx.budget_expiry_state.leavable_run_ids.extend(["wr_left", "wr_sibling"])
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0) as deadline:
            await asyncio.sleep(1)

    run_execution._unmark_leavable_run(ctx, "wr_sibling")

    assert run_execution._backstop_may_leave_run_running(deadline, ctx.budget_expiry_state, "wr_left")
    assert not run_execution._backstop_may_leave_run_running(deadline, ctx.budget_expiry_state, "wr_sibling")


@pytest.mark.asyncio
@pytest.mark.parametrize("block_first_read", [False, True])
async def test_backstop_detaches_an_inline_build_test_instead_of_cancelling_it(
    monkeypatch: pytest.MonkeyPatch, block_first_read: bool
) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch, workflow_yaml=HANDBACK_WORKFLOW_YAML, polled_status="running"
    )
    owned = OwnedRunRow(monkeypatch, block_first_read=block_first_read)
    ctx = make_build_test_ctx(harness, monkeypatch)
    ctx.minted_browser_sessions["pbs_run"] = None
    before = set(run_execution._DETACHED_CLEANUP_TASKS)

    with pytest.raises(TimeoutError):
        await _build_test_under_backstop(ctx, fire_when=owned.polled)
    await settle_cancelled_tools()

    adopted = adopted_detached_tasks(before)
    try:
        assert owned.polled.is_set()
        assert owned.cancel_writes == []
        assert harness["executor_cancelled"] is False
        assert len(adopted) == 1 and not adopted[0].done()
        assert ctx.minted_browser_sessions == {"pbs_run": OWNED_RUN_ID}
        harness["clear"].assert_not_awaited()
    finally:
        for task in adopted:
            task.cancel()
        await asyncio.gather(*adopted, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("run", ["detached", "detached_owes_secrets", "on_chat_browser"])
async def test_backstop_during_submission_cancels_the_run(monkeypatch: pytest.MonkeyPatch, run: str) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch, workflow_yaml=HANDBACK_WORKFLOW_YAML, polled_status="running", dispatch_to_worker=True
    )
    owned = OwnedRunRow(monkeypatch)
    owned.row.status = WorkflowRunStatus.queued
    if run == "detached_owes_secrets":
        install_secret_carrying_run_registry(monkeypatch)
    submitted = asyncio.Event()

    async def _submit_forever(**_kwargs: object) -> None:
        submitted.set()
        await asyncio.Event().wait()

    harness["worker_execute"].side_effect = _submit_forever
    ctx = make_build_test_ctx(harness, monkeypatch, on_chat_browser=run == "on_chat_browser")

    with pytest.raises(TimeoutError):
        await _build_test_under_backstop(ctx, fire_when=submitted)

    assert submitted.is_set()
    assert owned.draft_deletes.await_count == 1
    assert owned.cancel_writes == [OWNED_RUN_ID]
    if run == "detached_owes_secrets":
        assert not browser_session_lock(ctx, session_id="pbs_run").locked()
        assert ("pbs_run", OWNED_RUN_ID) in ctx.awaited_run_secret_handoffs


@pytest.mark.asyncio
@pytest.mark.parametrize("ended_by", ["worker", "watchdog", "watchdog_cancel_in_flight"])
async def test_backstop_after_the_run_ended_does_not_leave_it_to_an_owner(
    monkeypatch: pytest.MonkeyPatch, ended_by: str
) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch, workflow_yaml=HANDBACK_WORKFLOW_YAML, polled_status="running", dispatch_to_worker=True
    )
    owned = OwnedRunRow(monkeypatch, block_first_cancel=ended_by == "watchdog_cancel_in_flight")
    if ended_by == "worker":
        owned.finish("completed")
    else:
        monkeypatch.setattr(run_execution, "time", SimpleNamespace(monotonic=itertools.count(step=600).__next__))
    run_ended = asyncio.Event()

    async def _park_after_the_run(*_args: object, **_kwargs: object) -> None:
        run_ended.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(run_execution, "_halt_turn_if_superseded", _park_after_the_run)
    ctx = make_build_test_ctx(harness, monkeypatch)

    with pytest.raises(TimeoutError):
        await _build_test_under_backstop(
            ctx, fire_when=owned.cancel_started if ended_by == "watchdog_cancel_in_flight" else run_ended
        )

    assert owned.cancel_writes == ([] if ended_by == "worker" else [OWNED_RUN_ID])
    assert owned.row.status == ("completed" if ended_by == "worker" else "canceled")
    owned.draft_deletes.assert_awaited_once()
    harness["clear"].assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", [None, OWNED_RUN_ID])
async def test_a_minted_browser_closes_at_turn_end_when_no_run_owns_it(
    monkeypatch: pytest.MonkeyPatch, run_id: str | None
) -> None:
    """A minted browser with no outstanding run closes at the turn boundary (SKY-18904)."""
    from skyvern.forge import app as forge_app

    monkeypatch.setattr(
        forge_app.PERSISTENT_SESSIONS_MANAGER,
        "get_session",
        AsyncMock(return_value=SimpleNamespace(runnable_id=None)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE,
        "workflow_runs",
        SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=SimpleNamespace(status=WorkflowRunStatus.completed)),
            get_workflow_runs_by_parent_workflow_run_id=AsyncMock(return_value=[]),
        ),
    )
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(run_execution, "close_browser_session_quietly", close)

    ctx = make_copilot_ctx()
    ctx.minted_browser_sessions["pbs_minted"] = run_id

    await run_execution.close_minted_browser_sessions(ctx)

    assert ctx.minted_browser_sessions == {}
    close.assert_awaited_once_with(ctx.organization_id, "pbs_minted", reason=BrowserSessionCloseReason.turn_ended)


def _ledger_with_superseded_browser(ctx: CopilotContext, run_browser_run_id: str | None = "wr_resumed") -> None:
    ctx.minted_browser_sessions.update(
        {"pbs_run": run_browser_run_id, "pbs_old": "wr_old", "pbs_sibling": None, "pbs_chat": "wr_on_chat"}
    )
    ctx.last_run_blocks_browser_session_id = "pbs_old"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_lets_go_by", ["failing", "submitting"])
async def test_a_browser_two_calls_resume_survives_a_supersede_after_one_lets_go(first_lets_go_by: str) -> None:
    ctx = make_copilot_ctx()
    ctx.minted_browser_sessions.update({"pbs_resumed": "wr_prev", "pbs_new": "wr_new"})
    first: set[str] = set()
    second: set[str] = set()
    run_execution._claim_minted_browser(ctx, "pbs_resumed", first)
    run_execution._claim_minted_browser(ctx, "pbs_resumed", second)

    if first_lets_go_by == "submitting":
        ctx.minted_browser_sessions["pbs_resumed"] = "wr_first"
        run_execution._release_minted_browser_claim(ctx, first, restore=False)
    else:
        run_execution._release_minted_browser_claim(ctx, first)
    third: set[str] = set()
    run_execution._claim_minted_browser(ctx, "pbs_resumed", third)
    run_execution._release_minted_browser_claim(ctx, second)
    run_execution.start_superseded_minted_browser_close(ctx, "pbs_new", None)
    assert "pbs_resumed" in ctx.minted_browser_sessions
    assert not ctx.superseded_minted_browser_sessions

    run_execution._release_minted_browser_claim(ctx, third)
    expected = "wr_first" if first_lets_go_by == "submitting" else "wr_prev"
    assert ctx.minted_browser_sessions["pbs_resumed"] == expected
    assert not ctx.minted_browser_claims


@pytest.mark.asyncio
@pytest.mark.parametrize("run_browser", ["resumed", "fresh"])
@pytest.mark.parametrize(
    "old_browser",
    ["idle", "run_still_live", "browser_call_in_flight", "browser_call_once_run_idle", "browser_operation_unwinding"],
)
async def test_a_submitted_test_closes_only_the_browsers_it_superseded(
    monkeypatch: pytest.MonkeyPatch, old_browser: str, run_browser: str
) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch, workflow_yaml=HANDBACK_WORKFLOW_YAML, polled_status="running"
    )
    owned = OwnedRunRow(monkeypatch)
    run_live_at_first = old_browser in ("run_still_live", "browser_call_once_run_idle")
    liveness_calls = 0

    async def _liveness(*_args: object) -> bool:
        nonlocal liveness_calls
        liveness_calls += 1
        if old_browser == "browser_call_once_run_idle" and liveness_calls == 2:
            await old_browser_call.acquire()
        return run_live_at_first and liveness_calls == 1

    liveness = AsyncMock(side_effect=_liveness)
    lifecycle: list[tuple[str, str]] = []

    async def _close(_org: str, session_id: str, **_kwargs: object) -> bool:
        lifecycle.append(("close", session_id))
        return True

    close = AsyncMock(side_effect=_close)

    def _release_driver(
        _org: str, attached: AttachedBrowserDriver, *, unwinding_operations: set[asyncio.Task[object]] | None
    ) -> asyncio.Future[None]:
        async def _release() -> None:
            if unwinding_operations:
                await asyncio.wait(unwinding_operations)
            lifecycle.append(("release", attached.session_id))

        return asyncio.ensure_future(_release())

    monkeypatch.setattr(run_execution, "_run_in_a_minted_browser_is_still_live", liveness)
    monkeypatch.setattr(run_execution, "close_browser_session_quietly", close)
    monkeypatch.setattr(run_execution, "start_browser_driver_release", _release_driver)
    monkeypatch.setattr(runtime, "_SESSION_CLEANUP_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(forge_app.BROWSER_MANAGER, "end_session_activity_renewal", MagicMock())
    ctx = make_build_test_ctx(harness, monkeypatch)
    ctx.attached_browser_drivers["pbs_old"] = AttachedBrowserDriver("pbs_old", MagicMock())
    unwinding = asyncio.Event()
    if old_browser == "browser_operation_unwinding":
        ctx.admitted_browser_operations["pbs_old"] = {asyncio.create_task(unwinding.wait())}
    loop = asyncio.get_running_loop()
    for session_id in ("pbs_run", "pbs_old"):
        ctx.minted_browser_renewal_checks[session_id] = loop.call_later(3600, lambda: None)
    if run_browser == "fresh":
        install_fresh_run_browser(monkeypatch, "pbs_run")
        ctx.frontier_resume_session_id = None
    _ledger_with_superseded_browser(ctx, "wr_resumed" if run_browser == "resumed" else None)
    old_browser_call = browser_session_lock(ctx, session_id="pbs_old")
    if old_browser == "browser_call_in_flight":
        await old_browser_call.acquire()
    before = set(run_execution._DETACHED_CLEANUP_TASKS)

    with pytest.raises(TimeoutError):
        await _build_test_under_backstop(ctx, fire_when=owned.polled)
    await settle_cancelled_tools()

    adopted = adopted_detached_tasks(before)
    try:
        if old_browser in ("browser_call_in_flight", "browser_call_once_run_idle"):
            close.assert_not_awaited()
            old_browser_call.release()
            await settle_cancelled_tools()
        if old_browser == "browser_operation_unwinding":
            close.assert_not_awaited()
            unwinding.set()
            await settle_cancelled_tools()
        close.assert_awaited_once_with("org-1", "pbs_old", reason=BrowserSessionCloseReason.test_superseded)
        assert lifecycle == [("release", "pbs_old"), ("close", "pbs_old")]
        assert "pbs_old" not in ctx.attached_browser_drivers
        assert ctx.minted_browser_sessions == {"pbs_run": OWNED_RUN_ID, "pbs_sibling": None, "pbs_chat": "wr_on_chat"}
        assert set(ctx.minted_browser_renewal_checks) == {"pbs_run"}
        binding = resolve_browser_session_binding(ctx, {BROWSER_TARGET_PARAM_NAME: "last_run"})
        assert binding.session_id_override is None
        assert binding.unavailable_reason == ctx.last_run_binding_unavailable_reason
        assert ctx.last_run_blocks_browser_session_id is None

        run_execution._commit_run_blocks_record(
            ctx, {"ok": False, "data": {"workflow_run_id": "wr_old", "browser_session_id": "pbs_old", "blocks": []}}
        )
        assert ctx.last_run_blocks_browser_session_id is None
        assert ctx.last_run_binding_unavailable_reason == binding.unavailable_reason

        close.reset_mock()
        liveness.side_effect = None
        liveness.return_value = False
        await run_execution.close_minted_browser_sessions(ctx)
        assert {call.args[1] for call in close.await_args_list} == {"pbs_run", "pbs_sibling", "pbs_chat"}
    finally:
        for check in ctx.minted_browser_renewal_checks.values():
            check.cancel()
        for task in adopted:
            task.cancel()
        await asyncio.gather(*adopted, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_resume_still_before_submission_keeps_its_browser_from_a_sibling_that_submits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch, workflow_yaml=HANDBACK_WORKFLOW_YAML, polled_status="running"
    )
    owned = OwnedRunRow(monkeypatch)
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(run_execution, "close_browser_session_quietly", close)
    monkeypatch.setattr(run_execution, "_run_in_a_minted_browser_is_still_live", AsyncMock(return_value=False))
    install_fresh_run_browser(monkeypatch, "pbs_new")
    resume_stalled = asyncio.Event()
    real_unavailable_refusal = run_execution._canonical_workflow_unavailable_refusal

    async def _stall_the_resume(stalled_ctx: CopilotContext) -> dict[str, Any] | None:
        if not resume_stalled.is_set():
            resume_stalled.set()
            await asyncio.Event().wait()
        return await real_unavailable_refusal(stalled_ctx)

    monkeypatch.setattr(run_execution, "_canonical_workflow_unavailable_refusal", _stall_the_resume)
    ctx = make_build_test_ctx(harness, monkeypatch)
    ctx.minted_browser_sessions.update({"pbs_run": "wr_resumed", "pbs_new": None})
    before = set(run_execution._DETACHED_CLEANUP_TASKS)

    resume = asyncio.create_task(
        _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)
    )
    await resume_stalled.wait()
    with pytest.raises(TimeoutError):
        await _build_test_under_backstop(ctx, fire_when=owned.polled)
    await settle_cancelled_tools()

    adopted = adopted_detached_tasks(before)
    try:
        assert "pbs_run" not in {call.args[1] for call in close.await_args_list}
        assert ctx.minted_browser_sessions == {"pbs_run": None, "pbs_new": OWNED_RUN_ID}

        resume.cancel()
        await asyncio.gather(resume, return_exceptions=True)
        assert ctx.minted_browser_sessions == {"pbs_run": "wr_resumed", "pbs_new": OWNED_RUN_ID}
    finally:
        resume.cancel()
        for task in adopted:
            task.cancel()
        await asyncio.gather(resume, *adopted, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("refused_on_settings_check", [True, False], ids=["after_preparation", "at_dispatch_check"])
async def test_a_resume_refused_after_preparation_lets_its_rerun_close_the_browser_it_left(
    monkeypatch: pytest.MonkeyPatch, refused_on_settings_check: bool
) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch, workflow_yaml=SEARCH_THEN_SELECT_WORKFLOW_YAML, polled_status="running"
    )
    owned = OwnedRunRow(monkeypatch)
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(run_execution, "close_browser_session_quietly", close)
    monkeypatch.setattr(run_execution, "_run_in_a_minted_browser_is_still_live", AsyncMock(return_value=False))
    install_fresh_run_browser(monkeypatch, "pbs_new")
    ctx = make_build_test_ctx(harness, monkeypatch)
    refusals = refuse_seeded_origin_output_once(monkeypatch, ctx, on_settings_check=refused_on_settings_check)
    ctx.minted_browser_sessions.update({"pbs_run": "wr_resumed", "pbs_new": None})
    before = set(run_execution._DETACHED_CLEANUP_TASKS)

    with pytest.raises(TimeoutError):
        await _build_test_under_backstop(
            ctx,
            fire_when=owned.polled,
            block_labels=("run_search", "select_first_result"),
            labels_to_execute=["select_first_result"],
            frontier_start_label="select_first_result",
        )
    await settle_cancelled_tools()

    adopted = adopted_detached_tasks(before)
    try:
        assert refusals == []
        close.assert_awaited_once_with("org-1", "pbs_run", reason=BrowserSessionCloseReason.test_superseded)
        assert ctx.minted_browser_sessions == {"pbs_new": OWNED_RUN_ID}
    finally:
        for task in adopted:
            task.cancel()
        await asyncio.gather(*adopted, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_test_whose_submission_fails_closes_no_earlier_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = await install_run_blocks_harness(
        monkeypatch, workflow_yaml=HANDBACK_WORKFLOW_YAML, polled_status="running", dispatch_to_worker=True
    )
    OwnedRunRow(monkeypatch)
    harness["worker_execute"].side_effect = RuntimeError("submission refused")
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(run_execution, "close_browser_session_quietly", close)
    ctx = make_build_test_ctx(harness, monkeypatch)
    _ledger_with_superseded_browser(ctx)

    with pytest.raises(RuntimeError, match="submission refused"):
        await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)
    await settle_cancelled_tools()

    close.assert_not_awaited()
    assert ctx.minted_browser_sessions["pbs_old"] == "wr_old"
    assert ctx.minted_browser_sessions["pbs_run"] == OWNED_RUN_ID
    assert ctx.last_run_blocks_browser_session_id == "pbs_old"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runnable_id", "runs", "children", "expected_live"),
    [
        pytest.param(
            OWNED_RUN_ID,
            {OWNED_RUN_ID: WorkflowRunStatus.completed},
            {},
            True,
            id="terminal-row-still-holds-session-lease",
        ),
        pytest.param(
            None,
            {
                OWNED_RUN_ID: WorkflowRunStatus.completed,
                "wr_child": WorkflowRunStatus.completed,
                "wr_grandchild": WorkflowRunStatus.running,
            },
            {OWNED_RUN_ID: ["wr_child"], "wr_child": ["wr_grandchild"]},
            True,
            id="live-grandchild-inherited-browser",
        ),
        pytest.param(
            None,
            {
                OWNED_RUN_ID: WorkflowRunStatus.completed,
                "wr_child": WorkflowRunStatus.completed,
                "wr_grandchild": WorkflowRunStatus.completed,
            },
            {OWNED_RUN_ID: ["wr_child"], "wr_child": ["wr_grandchild"]},
            False,
            id="all-descendants-terminal-and-lease-released",
        ),
    ],
)
async def test_minted_browser_liveness_includes_lease_and_all_descendants(
    monkeypatch: pytest.MonkeyPatch,
    runnable_id: str | None,
    runs: dict[str, WorkflowRunStatus],
    children: dict[str, list[str]],
    expected_live: bool,
) -> None:
    from skyvern.forge import app as forge_app

    async def _get_run(run_id: str, _organization_id: str) -> SimpleNamespace | None:
        status = runs.get(run_id)
        return SimpleNamespace(workflow_run_id=run_id, status=status) if status is not None else None

    async def _get_children(*, parent_workflow_run_id: str, organization_id: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(workflow_run_id=child_id) for child_id in children.get(parent_workflow_run_id, [])]

    monkeypatch.setattr(
        forge_app.PERSISTENT_SESSIONS_MANAGER,
        "get_session",
        AsyncMock(return_value=SimpleNamespace(runnable_id=runnable_id)),
    )
    monkeypatch.setattr(
        forge_app.DATABASE,
        "workflow_runs",
        SimpleNamespace(
            get_workflow_run=AsyncMock(side_effect=_get_run),
            get_workflow_runs_by_parent_workflow_run_id=AsyncMock(side_effect=_get_children),
        ),
    )

    assert (
        await run_execution._run_in_a_minted_browser_is_still_live("pbs_minted", OWNED_RUN_ID, "org-1")
    ) is expected_live


@pytest.mark.asyncio
async def test_minted_browser_liveness_fails_closed_when_session_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge import app as forge_app

    monkeypatch.setattr(
        forge_app.PERSISTENT_SESSIONS_MANAGER,
        "get_session",
        AsyncMock(side_effect=RuntimeError("database is down")),
    )

    assert await run_execution._run_in_a_minted_browser_is_still_live("pbs_minted", OWNED_RUN_ID, "org-1")


@pytest.mark.asyncio
async def test_minted_browser_closes_after_a_leased_run_becomes_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    liveness = AsyncMock(side_effect=[True, False])
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(run_execution, "_run_in_a_minted_browser_is_still_live", liveness)
    monkeypatch.setattr(run_execution, "RUN_BLOCKS_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(run_execution, "close_browser_session_quietly", close)
    ctx = make_copilot_ctx()
    ctx.minted_browser_sessions["pbs_minted"] = OWNED_RUN_ID

    await run_execution.close_minted_browser_sessions(ctx)
    await asyncio.sleep(0)

    assert ctx.minted_browser_sessions == {}
    assert liveness.await_count == 2
    close.assert_awaited_once_with(ctx.organization_id, "pbs_minted", reason=BrowserSessionCloseReason.turn_ended)


def _kept_run_reads(monkeypatch: pytest.MonkeyPatch, *, runs: dict[str, tuple[WorkflowRunStatus, str | None]]) -> None:
    monkeypatch.setattr(
        forge_app.PERSISTENT_SESSIONS_MANAGER, "get_session", AsyncMock(return_value=SimpleNamespace(runnable_id=None))
    )

    async def _children(*, parent_workflow_run_id: str, organization_id: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(workflow_run_id=rid)
            for rid, (_, parent) in runs.items()
            if parent == parent_workflow_run_id
        ]

    monkeypatch.setattr(
        forge_app.DATABASE,
        "workflow_runs",
        SimpleNamespace(
            get_workflow_run=AsyncMock(
                side_effect=lambda run_id, _org: SimpleNamespace(workflow_run_id=run_id, status=runs[run_id][0])
            ),
            get_workflow_runs_by_parent_workflow_run_id=AsyncMock(side_effect=_children),
        ),
    )


@pytest.mark.asyncio
async def test_a_kept_browser_stays_renewed_while_a_child_run_outlives_its_final_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = stamping_browser_manager(monkeypatch)
    monkeypatch.setattr(forge_app, "BROWSER_MANAGER", manager)
    monkeypatch.setattr(run_execution, "RUN_BLOCKS_POLL_INTERVAL_SECONDS", 0)
    runs = {OWNED_RUN_ID: (WorkflowRunStatus.completed, None), "wr_child": (WorkflowRunStatus.running, OWNED_RUN_ID)}
    _kept_run_reads(monkeypatch, runs=runs)
    closed = asyncio.Event()

    async def _close(*_args: object, **_kwargs: object) -> bool:
        closed.set()
        return True

    monkeypatch.setattr(run_execution, "close_browser_session_quietly", _close)
    ctx = make_copilot_ctx()
    ctx.minted_browser_sessions["pbs_kept"] = OWNED_RUN_ID

    try:
        await run_execution.close_minted_browser_sessions(ctx)
        for _ in range(20):
            await asyncio.sleep(0)
        assert not closed.is_set()
        assert "pbs_kept" in manager._renewable_session_ids()

        runs["wr_child"] = (WorkflowRunStatus.completed, OWNED_RUN_ID)
        await asyncio.wait_for(closed.wait(), timeout=1)
        for _ in range(5):
            await asyncio.sleep(0)
        assert "pbs_kept" not in manager._renewable_session_ids()
    finally:
        await stop_activity_renewer(manager)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_stamp_fails", [False, True])
async def test_a_kept_browser_stops_renewing_at_the_cap(
    monkeypatch: pytest.MonkeyPatch, first_stamp_fails: bool
) -> None:
    touch = AsyncMock(side_effect=[RuntimeError("db down"), None] if first_stamp_fails else None)
    manager = stamping_browser_manager(monkeypatch, touch)
    monkeypatch.setattr(forge_app, "BROWSER_MANAGER", manager)
    monkeypatch.setattr(run_execution, "_KEPT_RUN_ACTIVITY_RENEWAL_CAP_SECONDS", 0.05)

    renewal = asyncio.create_task(run_execution._renew_kept_browser_activity("pbs_kept"))
    try:
        await asyncio.sleep(0.01)
        assert "pbs_kept" in manager._renewable_session_ids()
        await asyncio.wait_for(renewal, timeout=1)
        assert "pbs_kept" not in manager._renewable_session_ids()
    finally:
        renewal.cancel()
        await stop_activity_renewer(manager)


def test_a_build_test_acquisition_copy_records_into_the_turns_minted_browser_ledger() -> None:
    """Build-test acquisition mints into a ``replace(ctx)`` copy, so the ledger the turn closes from
    has to be the same object -- a copied one would silently leave every test browser open."""
    ctx = make_copilot_ctx()
    acquisition_ctx = replace(ctx)
    acquisition_ctx.minted_browser_sessions["pbs_minted"] = None

    assert ctx.minted_browser_sessions == {"pbs_minted": None}
