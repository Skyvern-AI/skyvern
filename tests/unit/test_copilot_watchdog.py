"""Tests for the SKY-9163 progress-based watchdog inside
``_run_blocks_and_collect_debug``.

The full function does too much setup (prepare_workflow, execute_workflow,
parameter-binding invariants) to unit-test end-to-end cheaply. Instead we
target the isolated watchdog surface:

- ``_progress_marker`` — marker stability and field-change sensitivity.
- ``_read_progress_sources`` — correct delegation + graceful handling of
  DB failures.
- ``_watchdog_error_message`` — the regression-guard strings (no
  "timed out", reconciliation-instruction, per-reason body).

Those three are where the SKY-9163 correctness properties live:

1. A stale marker must be exactly equal across two polls when nothing
   changed in the DB (otherwise the watchdog would false-reset on every
   poll, making stagnation detection impossible).
2. Any change in ``run.status`` / ``run.modified_at`` / ``step_ts`` /
   ``block_ts`` must produce a new marker (otherwise the watchdog would
   false-trip on a progressing run).
3. The error messages must not read as retry-invites — that was the
   original bug. "timed out" / "likely stuck repeating failing actions"
   are the exact phrases the LLM used to read as "try again".
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.copilot.tools import (
    RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    RUN_BLOCKS_STAGNATION_WINDOW_SECONDS,
    _any_quiet_block_requested,
    _progress_marker,
    _read_progress_sources,
    _tool_loop_error,
    _watchdog_error_message,
)


def _fake_run(status: str = "running", modified_at: datetime | None = None) -> Any:
    """A bare-minimum stand-in for ``WorkflowRun`` — the marker only reads
    ``.status`` and ``.modified_at``.
    """
    return SimpleNamespace(
        status=status,
        modified_at=modified_at or datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
        browser_session_id=None,
    )


# ---------------------------------------------------------------------------
# _progress_marker: stability + per-field sensitivity.
# ---------------------------------------------------------------------------


def test_progress_marker_stable_for_identical_inputs() -> None:
    """If the DB reports identical values on two successive polls, the marker
    must compare equal. A marker that drifts on repeated reads would make the
    stagnation window unreachable."""
    run = _fake_run()
    step_ts = datetime(2026, 4, 21, 12, 0, 30, tzinfo=timezone.utc)
    block_ts = datetime(2026, 4, 21, 12, 0, 31, tzinfo=timezone.utc)

    m1 = _progress_marker(run, step_ts, block_ts)
    m2 = _progress_marker(run, step_ts, block_ts)

    assert m1 == m2


def test_progress_marker_changes_on_run_status() -> None:
    run1 = _fake_run(status="running")
    run2 = _fake_run(status="queued")
    assert _progress_marker(run1, None, None) != _progress_marker(run2, None, None)


def test_progress_marker_changes_on_run_modified_at() -> None:
    run1 = _fake_run(modified_at=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc))
    run2 = _fake_run(modified_at=datetime(2026, 4, 21, 12, 0, 1, tzinfo=timezone.utc))
    assert _progress_marker(run1, None, None) != _progress_marker(run2, None, None)


def test_progress_marker_changes_on_step_ts() -> None:
    run = _fake_run()
    t1 = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 21, 12, 0, 5, tzinfo=timezone.utc)
    assert _progress_marker(run, t1, None) != _progress_marker(run, t2, None)


def test_progress_marker_changes_on_block_ts() -> None:
    run = _fake_run()
    t1 = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 21, 12, 0, 5, tzinfo=timezone.utc)
    assert _progress_marker(run, None, t1) != _progress_marker(run, None, t2)


def test_progress_marker_tolerates_none_run() -> None:
    """A transient DB read failure can return ``run=None``. The marker must
    still be hashable and comparable."""
    m_none = _progress_marker(None, None, None)
    assert m_none == (None, None, None, None)

    # Two consecutive failed reads produce equal markers → stagnation clock
    # keeps ticking (the right behavior when we can't confirm progress).
    assert _progress_marker(None, None, None) == _progress_marker(None, None, None)


# ---------------------------------------------------------------------------
# _read_progress_sources: delegation + graceful DB-failure handling.
# ---------------------------------------------------------------------------


class _FakeTasksRepo:
    def __init__(
        self,
        *,
        step_ts: datetime | None = None,
        block_ts: datetime | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        self.step_ts = step_ts
        self.block_ts = block_ts
        self.raise_on_call = raise_on_call
        self.call_count = 0

    async def get_workflow_run_progress_timestamps(
        self,
        *,
        workflow_run_id: str,
        organization_id: str | None = None,
    ) -> tuple[datetime | None, datetime | None]:
        self.call_count += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.step_ts, self.block_ts


class _FakeWorkflowRunsRepo:
    def __init__(self, run: Any | None = None, raise_on_call: Exception | None = None) -> None:
        self.run = run
        self.raise_on_call = raise_on_call

    async def get_workflow_run(
        self,
        *,
        workflow_run_id: str,
        organization_id: str,
    ) -> Any:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.run


class _FakeDatabase:
    def __init__(self, tasks: _FakeTasksRepo, workflow_runs: _FakeWorkflowRunsRepo) -> None:
        self.tasks = tasks
        self.workflow_runs = workflow_runs


class _FakeCtx:
    organization_id = "o_test"


@pytest.mark.asyncio
async def test_read_progress_sources_returns_run_and_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge import app as forge_app

    run = _fake_run()
    step_ts = datetime(2026, 4, 21, 12, 0, 10, tzinfo=timezone.utc)
    block_ts = datetime(2026, 4, 21, 12, 0, 11, tzinfo=timezone.utc)
    db = _FakeDatabase(
        tasks=_FakeTasksRepo(step_ts=step_ts, block_ts=block_ts),
        workflow_runs=_FakeWorkflowRunsRepo(run=run),
    )
    monkeypatch.setattr(forge_app, "DATABASE", db)

    read_run, read_step_ts, read_block_ts = await _read_progress_sources(_FakeCtx(), "wr_1")

    assert read_run is run
    assert read_step_ts == step_ts
    assert read_block_ts == block_ts


@pytest.mark.asyncio
async def test_read_progress_sources_swallows_workflow_run_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB read failure on the workflow-run row must not crash the watchdog —
    ``_safe_read_workflow_run`` returns None and the poll continues."""
    from skyvern.forge import app as forge_app

    db = _FakeDatabase(
        tasks=_FakeTasksRepo(step_ts=None, block_ts=None),
        workflow_runs=_FakeWorkflowRunsRepo(raise_on_call=RuntimeError("DB flake")),
    )
    monkeypatch.setattr(forge_app, "DATABASE", db)

    read_run, read_step_ts, read_block_ts = await _read_progress_sources(_FakeCtx(), "wr_1")

    assert read_run is None
    assert read_step_ts is None
    assert read_block_ts is None


@pytest.mark.asyncio
async def test_read_progress_sources_swallows_progress_timestamps_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB read failure on the aggregate timestamps must also not crash — the
    caller still gets the run (if readable) and ``None`` for the timestamps.
    """
    from skyvern.forge import app as forge_app

    run = _fake_run()
    db = _FakeDatabase(
        tasks=_FakeTasksRepo(raise_on_call=RuntimeError("aggregate query failed")),
        workflow_runs=_FakeWorkflowRunsRepo(run=run),
    )
    monkeypatch.setattr(forge_app, "DATABASE", db)

    read_run, read_step_ts, read_block_ts = await _read_progress_sources(_FakeCtx(), "wr_1")

    assert read_run is run
    assert read_step_ts is None
    assert read_block_ts is None


# ---------------------------------------------------------------------------
# _watchdog_error_message: the regression-guard strings.
# ---------------------------------------------------------------------------


class _ErrorCtx:
    """Minimal ``AgentContext`` stand-in for the error-message path — only
    ``browser_session_id`` is read, and only by ``_fallback_page_info``."""

    organization_id = "o_test"
    browser_session_id = None


@pytest.mark.asyncio
async def test_stagnation_error_message_does_not_invite_retry() -> None:
    """The exact SKY-9163 bug: the old copy said "likely stuck repeating
    failing actions" which the LLM read as "try again". The stagnation
    message must explicitly discourage retry."""
    msg = await _watchdog_error_message("stagnation", _ErrorCtx(), "wr_test", _fake_run())

    assert "timed out" not in msg.lower()
    assert "likely stuck repeating" not in msg.lower()
    assert str(RUN_BLOCKS_STAGNATION_WINDOW_SECONDS) in msg
    assert "Run ID: wr_test" in msg
    assert "get_run_results" in msg
    assert "Do NOT re-invoke block-running tools" in msg


@pytest.mark.asyncio
async def test_ceiling_error_message_advises_splitting() -> None:
    """The ceiling path is rare (a runaway run that keeps making progress
    past 20 min). Its error must tell the LLM to split the workflow, not
    retry — a longer run won't fit either."""
    msg = await _watchdog_error_message("ceiling", _ErrorCtx(), "wr_test", _fake_run())

    assert "timed out" not in msg.lower()
    assert str(RUN_BLOCKS_SAFETY_CEILING_SECONDS) in msg
    assert "split" in msg.lower()
    assert "Run ID: wr_test" in msg
    assert "get_run_results" in msg


@pytest.mark.asyncio
async def test_task_exit_unfinalized_message_reports_last_observed_status() -> None:
    """When ``execute_workflow`` naturally exits but the row isn't terminal,
    the error must name the last-observed status so the LLM has a concrete
    anchor for the follow-up ``get_run_results`` call."""
    run = _fake_run(status="running")
    msg = await _watchdog_error_message("task_exit_unfinalized", _ErrorCtx(), "wr_test", run)

    assert "timed out" not in msg.lower()
    assert "last observed status: running" in msg
    assert "Run ID: wr_test" in msg
    assert "get_run_results" in msg


@pytest.mark.asyncio
async def test_task_exit_unfinalized_message_tolerates_unreadable_run() -> None:
    """If the post-drain reread also fails (``run is None``), the message must
    still be well-formed and mention the unreadable state rather than
    crashing on a ``None.status`` access."""
    msg = await _watchdog_error_message("task_exit_unfinalized", _ErrorCtx(), "wr_test", None)

    assert "unreadable" in msg.lower()
    assert "Run ID: wr_test" in msg
    assert "get_run_results" in msg


# ---------------------------------------------------------------------------
# _any_quiet_block_requested: stagnation bypass for block types that
# legitimately do long-silent work. Without this bypass, a WAIT block with
# wait_sec >= 90, a slow TEXT_PROMPT LLM call, or a HumanInteractionBlock
# pausing for user input would be falsely reported as stagnation and the
# tool would cancel a healthy run.
# ---------------------------------------------------------------------------


def _workflow_with_block_types(*type_value_label_pairs: tuple[str, str]) -> Any:
    """Build a minimal `last_workflow`-shaped object that
    ``_any_quiet_block_requested`` can walk. Each pair is
    ``(block_type_value, label)`` — e.g. ``("wait", "pause1")``.
    """
    blocks = [
        SimpleNamespace(label=label, block_type=SimpleNamespace(value=block_type_value))
        for block_type_value, label in type_value_label_pairs
    ]
    definition = SimpleNamespace(blocks=blocks)
    return SimpleNamespace(workflow_definition=definition)


def test_any_quiet_block_requested_wait() -> None:
    ctx = SimpleNamespace(last_workflow=_workflow_with_block_types(("wait", "pause1")))
    assert _any_quiet_block_requested(ctx, ["pause1"]) is True


def test_any_quiet_block_requested_text_prompt() -> None:
    ctx = SimpleNamespace(last_workflow=_workflow_with_block_types(("text_prompt", "prompt1")))
    assert _any_quiet_block_requested(ctx, ["prompt1"]) is True


def test_any_quiet_block_requested_human_interaction() -> None:
    ctx = SimpleNamespace(last_workflow=_workflow_with_block_types(("human_interaction", "wait_for_user")))
    assert _any_quiet_block_requested(ctx, ["wait_for_user"]) is True


def test_any_quiet_block_requested_mixed_requested_labels_match_quiet_one() -> None:
    """When multiple blocks are requested, having any one quiet type is
    enough to disable stagnation for the whole invocation."""
    ctx = SimpleNamespace(
        last_workflow=_workflow_with_block_types(
            ("navigation", "nav1"),
            ("wait", "pause1"),
            ("extraction", "extract1"),
        )
    )
    assert _any_quiet_block_requested(ctx, ["nav1", "pause1", "extract1"]) is True


def test_any_quiet_block_requested_only_task_blocks_returns_false() -> None:
    """The normal case: task-heavy workflows produce regular step writes.
    Stagnation is safe to enable."""
    ctx = SimpleNamespace(
        last_workflow=_workflow_with_block_types(
            ("navigation", "nav1"),
            ("extraction", "extract1"),
        )
    )
    assert _any_quiet_block_requested(ctx, ["nav1", "extract1"]) is False


def test_any_quiet_block_requested_label_not_in_requested_ignored() -> None:
    """A WAIT block defined in the workflow but not requested in this
    invocation must not disable stagnation."""
    ctx = SimpleNamespace(
        last_workflow=_workflow_with_block_types(
            ("wait", "not_requested_pause"),
            ("navigation", "requested_nav"),
        )
    )
    assert _any_quiet_block_requested(ctx, ["requested_nav"]) is False


def test_any_quiet_block_requested_no_workflow_returns_false() -> None:
    """Defensive: no workflow loaded → no bypass. The loop will use its
    default stagnation behavior (safe for the common case)."""
    ctx = SimpleNamespace(last_workflow=None)
    assert _any_quiet_block_requested(ctx, ["anything"]) is False


def test_any_quiet_block_requested_empty_labels_returns_false() -> None:
    ctx = SimpleNamespace(last_workflow=_workflow_with_block_types(("wait", "pause1")))
    assert _any_quiet_block_requested(ctx, None) is False
    assert _any_quiet_block_requested(ctx, []) is False


# ---------------------------------------------------------------------------
# Reconciliation guard message: regression guard on "timed out" phrasing.
# The guard itself is tested in test_copilot_cancel_helpers.py; this test is
# specifically about the LLM-facing STRING, which previously said "timed out"
# and read as a retry-invite when combined with LLM priors.
# ---------------------------------------------------------------------------


def test_reconciliation_guard_message_does_not_say_timed_out() -> None:
    """The reconciliation guard message is what the LLM reads on the *next*
    block-running tool call after a watchdog exit. The stagnation/ceiling/
    unfinalized error messages all purge "timed out"; the guard message must
    match, otherwise the phrase leaks right back in and the regression is
    only cosmetic."""
    ctx = SimpleNamespace(
        consecutive_tool_tracker=[],
        repeated_action_fingerprint_streak_count=0,
        last_test_non_retriable_nav_error=None,
        pending_reconciliation_run_id="wr_guarded",
    )
    msg = _tool_loop_error(ctx, "update_and_run_blocks")
    assert isinstance(msg, str)
    assert "timed out" not in msg.lower()
    assert "wr_guarded" in msg
    assert "get_run_results" in msg
