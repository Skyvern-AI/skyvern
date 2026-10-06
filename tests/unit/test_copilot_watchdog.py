"""Tests for the run poll loop inside ``_run_blocks_and_collect_debug``: its exits, error strings and cancellation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app as forge_app
from skyvern.forge.sdk.copilot.blocker_signal import (
    assert_clean_user_facing_text,
    contains_internal_machinery_leak,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.repair_origin_run import (
    OriginBlockOutput,
    OriginExecutionSettings,
    OriginOutputSnapshot,
    SelectedOutputSource,
)
from skyvern.forge.sdk.copilot.tools import (
    RUN_BLOCKS_SAFETY_CEILING_SECONDS,
    WatchdogExitReason,
    _fallback_page_info,
    _read_progress_sources,
    _run_blocks_and_collect_debug,
    _shared,
    _watchdog_error_message,
    run_execution,
)
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _watchdog_user_facing_summary,
)
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.schemas.workflows import BlockStatus, BlockType
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import ActionStatus
from tests.unit.copilot_test_helpers import SEARCH_THEN_SELECT_WORKFLOW_YAML
from tests.unit.copilot_test_helpers import install_run_blocks_harness as _install_run_harness
from tests.unit.copilot_test_helpers import make_copilot_ctx, run_result_action_row, terminal_extraction_block


def _fake_run(status: str = "running", modified_at: datetime | None = None) -> Any:
    return SimpleNamespace(
        status=status,
        modified_at=modified_at or datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
        browser_session_id=None,
        failure_reason=None,
    )


# ---------------------------------------------------------------------------
# _read_progress_sources: delegation + graceful DB-failure handling.
# ---------------------------------------------------------------------------


class _FakeTasksRepo:
    def __init__(
        self,
        *,
        block_ts: datetime | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        self.block_ts = block_ts
        self.raise_on_call = raise_on_call
        self.call_count = 0

    async def get_workflow_run_block_progress_timestamp(
        self,
        *,
        workflow_run_id: str,
        organization_id: str | None = None,
    ) -> datetime | None:
        self.call_count += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.block_ts


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
    block_ts = datetime(2026, 4, 21, 12, 0, 11, tzinfo=timezone.utc)
    db = _FakeDatabase(
        tasks=_FakeTasksRepo(block_ts=block_ts),
        workflow_runs=_FakeWorkflowRunsRepo(run=run),
    )
    monkeypatch.setattr(forge_app, "DATABASE", db)

    assert await _read_progress_sources(_FakeCtx(), "wr_1") == (run, block_ts)


@pytest.mark.asyncio
async def test_read_progress_sources_swallows_workflow_run_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB read failure on the workflow-run row must not crash the watchdog —
    ``_safe_read_workflow_run`` returns None and the poll continues."""
    from skyvern.forge import app as forge_app

    db = _FakeDatabase(
        tasks=_FakeTasksRepo(block_ts=None),
        workflow_runs=_FakeWorkflowRunsRepo(raise_on_call=RuntimeError("DB flake")),
    )
    monkeypatch.setattr(forge_app, "DATABASE", db)

    assert await _read_progress_sources(_FakeCtx(), "wr_1") == (None, None)


@pytest.mark.asyncio
async def test_read_progress_sources_swallows_progress_timestamps_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB read failure on the block timestamp must also not crash — the caller still gets the run
    (if readable) and ``None`` for the timestamp."""
    from skyvern.forge import app as forge_app

    run = _fake_run()
    db = _FakeDatabase(
        tasks=_FakeTasksRepo(raise_on_call=RuntimeError("aggregate query failed")),
        workflow_runs=_FakeWorkflowRunsRepo(run=run),
    )
    monkeypatch.setattr(forge_app, "DATABASE", db)

    assert await _read_progress_sources(_FakeCtx(), "wr_1") == (run, None)


# ---------------------------------------------------------------------------
# _watchdog_error_message: the regression-guard strings.
# ---------------------------------------------------------------------------


class _ErrorCtx:
    """Minimal ``AgentContext`` stand-in for the error-message path."""

    organization_id = "o_test"
    browser_session_id = None
    origin_run_redaction_registry = None


def _install_fallback_page(
    monkeypatch: pytest.MonkeyPatch, page: SimpleNamespace
) -> tuple[SimpleNamespace, SimpleNamespace]:
    browser_state = SimpleNamespace(get_or_create_page=AsyncMock(return_value=page))
    session_manager = SimpleNamespace(get_browser_state=AsyncMock(return_value=browser_state))
    monkeypatch.setattr(forge_app, "PERSISTENT_SESSIONS_MANAGER", session_manager)
    ctx = SimpleNamespace(
        organization_id="o_test",
        browser_session_id="pbs_copilot",
        turn_origin=TurnOrigin.interactive,
        attached_browser_drivers={},
    )
    return session_manager, ctx


@pytest.mark.asyncio
async def test_fallback_page_info_uses_persistent_session_state_without_sdk_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = SimpleNamespace(url="https://example.test/current", title=AsyncMock(return_value="Current page"))
    session_manager, ctx = _install_fallback_page(monkeypatch, page)

    current_url, page_title = await _fallback_page_info(ctx)

    assert current_url == "https://example.test/current"
    assert page_title == "Current page"
    session_manager.get_browser_state.assert_awaited_once_with(
        session_id="pbs_copilot",
        organization_id="o_test",
    )


@pytest.mark.asyncio
async def test_fallback_page_info_bounds_a_title_that_never_resolves_and_keeps_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged renderer hangs `title()` rather than raising it. The bound has to return, and it
    has to keep the url — `page.url` is synchronous, so it is already in hand when the title
    stalls, and most callers of this helper want only the url."""

    async def _never_resolves() -> str:
        await asyncio.Event().wait()
        return "unreachable"

    page = SimpleNamespace(url="https://example.test/wedged", title=_never_resolves)
    _, ctx = _install_fallback_page(monkeypatch, page)
    monkeypatch.setattr(_shared, "_DISCOVERY_PER_CALL_TIMEOUT_SECONDS", 0.05)

    current_url, page_title = await asyncio.wait_for(_fallback_page_info(ctx), timeout=5)

    assert current_url == "https://example.test/wedged"
    assert page_title == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("title_raises", [False, True])
async def test_fallback_page_info_never_pairs_a_title_with_another_documents_url(
    monkeypatch: pytest.MonkeyPatch, title_raises: bool
) -> None:
    page = SimpleNamespace(url="https://portal.fixture.test/login")

    async def _title_across_navigation() -> str:
        page.url = "https://portal.fixture.test/dashboard"
        if title_raises:
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
        return "Ops Portal"

    page.title = _title_across_navigation
    _, ctx = _install_fallback_page(monkeypatch, page)

    assert await _fallback_page_info(ctx) == ("https://portal.fixture.test/dashboard", "")


@pytest.mark.asyncio
async def test_ceiling_error_message_advises_splitting() -> None:
    """The error must tell the LLM to split the workflow, not retry — a longer run won't fit either."""
    quiet_budget = RUN_BLOCKS_SAFETY_CEILING_SECONDS - 10
    msg = await _watchdog_error_message("ceiling", _ErrorCtx(), "wr_test", _fake_run(), quiet_budget)

    assert "timed out" not in msg.lower()
    assert str(quiet_budget) in msg
    assert "split" in msg.lower()
    assert "Run ID: wr_test" in msg
    assert "get_run_results" in msg
    assert "Do NOT re-invoke block-running tools" in msg


@pytest.mark.asyncio
async def test_task_exit_unfinalized_message_reports_last_observed_status() -> None:
    """When ``execute_workflow`` naturally exits but the row isn't terminal,
    the error must name the last-observed status so the LLM has a concrete
    anchor for the follow-up ``get_run_results`` call."""
    run = _fake_run(status="running")
    msg = await _watchdog_error_message(
        "task_exit_unfinalized", _ErrorCtx(), "wr_test", run, RUN_BLOCKS_SAFETY_CEILING_SECONDS - 10
    )

    assert "timed out" not in msg.lower()
    assert "last observed status: running" in msg
    assert "Run ID: wr_test" in msg
    assert "get_run_results" in msg


@pytest.mark.asyncio
async def test_task_exit_unfinalized_message_tolerates_unreadable_run() -> None:
    """If the post-drain reread also fails (``run is None``), the message must
    still be well-formed and mention the unreadable state rather than
    crashing on a ``None.status`` access."""
    msg = await _watchdog_error_message(
        "task_exit_unfinalized", _ErrorCtx(), "wr_test", None, RUN_BLOCKS_SAFETY_CEILING_SECONDS - 10
    )

    assert "unreadable" in msg.lower()
    assert "Run ID: wr_test" in msg
    assert "get_run_results" in msg


@pytest.mark.asyncio
async def test_paused_error_message_reports_a_wait_not_an_uncertain_outcome() -> None:
    """This arm is the only one that tells the model to relay its own text to the user, so relaying
    it verbatim has to clear the output guard. It must also not inherit the "outcome is uncertain"
    tail, which would push a re-run of blocks that are still live and waiting on a person."""
    msg = await _watchdog_error_message("paused", _ErrorCtx(), "wr_test", _fake_run(status="paused"), 240)

    assert "paused" in msg.lower()
    assert "tell the user" in msg.lower()
    assert "wr_test" not in msg
    assert contains_internal_machinery_leak(msg) is False
    assert "uncertain" not in msg.lower()
    assert "nothing was cancelled" in msg.lower()


@pytest.mark.asyncio
async def test_non_paused_error_messages_keep_the_run_id_for_the_model() -> None:
    """The other arms never direct a relay — they tell the model to look the run up — so stripping
    the id there would take away the only handle it has."""
    exit_reasons: tuple[WatchdogExitReason, ...] = ("ceiling", "task_exit_unfinalized")
    for exit_reason in exit_reasons:
        msg = await _watchdog_error_message(exit_reason, _ErrorCtx(), "wr_test", _fake_run(), 240)

        assert "Run ID: wr_test" in msg
        assert "tell the user" not in msg.lower()


@pytest.mark.parametrize(
    ("exit_reason", "run", "expected"),
    [
        (
            "paused",
            _fake_run(status="paused"),
            "The run is paused, waiting for a person to approve or reject it.",
        ),
        (
            "ceiling",
            _fake_run(),
            f"The run did not finish within the {RUN_BLOCKS_SAFETY_CEILING_SECONDS - 10}s absolute ceiling.",
        ),
        (
            "task_exit_unfinalized",
            _fake_run(status="running"),
            "The run ended before recording a trustworthy terminal status. Last observed status: running.",
        ),
        (
            "task_exit_unfinalized",
            None,
            "The run ended before recording a trustworthy terminal status.",
        ),
    ],
)
def test_watchdog_user_relayed_text_is_id_free_and_clears_the_output_guard(
    exit_reason: WatchdogExitReason, run: SimpleNamespace | None, expected: str
) -> None:
    reason = _watchdog_user_facing_summary(exit_reason, RUN_BLOCKS_SAFETY_CEILING_SECONDS - 10, run)

    assert reason == expected
    assert contains_internal_machinery_leak(reason) is False
    assert_clean_user_facing_text(reason)


_HUMAN_INTERACTION_WORKFLOW_YAML = """
title: human approval example
workflow_definition:
  parameters: []
  blocks:
    - block_type: wait
      label: request_access
      wait_sec: 1
    - block_type: human_interaction
      label: approve_login
      timeout_seconds: 3600
      sender: automation@example.com
      recipients: ["ops@example.com"]
      subject: Manual sign-in needed
      body: A workflow run is paused and needs someone to sign in.
"""

_EXTRACTION_WORKFLOW_YAML = """
title: extraction example
workflow_definition:
  parameters: []
  blocks:
    - block_type: extraction
      label: extract_heading
      url: https://example.com
      data_extraction_goal: Extract the page heading.
"""

_NAVIGATION_WORKFLOW_YAML = """
title: navigation example
workflow_definition:
  parameters: []
  blocks:
    - block_type: navigation
      label: open_page
      url: https://example.com
      navigation_goal: Open the page.
"""

_CODE_WORKFLOW_YAML = """
title: code example
workflow_definition:
  parameters: []
  blocks:
    - block_type: code
      label: click_submit
      code: |
        await page.locator("#submit").click()
"""


def _adopted_detached_tasks(before: set[Any]) -> list[Any]:
    return [task for task in run_execution._DETACHED_CLEANUP_TASKS if task not in before]


def _install_advancing_clock(monkeypatch: pytest.MonkeyPatch, step_seconds: float = 600.0) -> Callable[[], float]:
    elapsed = 0.0

    def _monotonic() -> float:
        nonlocal elapsed
        elapsed += step_seconds
        return elapsed

    monkeypatch.setattr(run_execution, "time", SimpleNamespace(monotonic=_monotonic))
    return lambda: elapsed


@pytest.mark.asyncio
async def test_paused_run_is_reported_as_a_pause_and_left_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run paused at a human_interaction block with nobody responding: the watchdog must leave
    the poll loop immediately, report the pause, and tear nothing down — the executor task, the run
    itself and the pane's run-session association all have to outlive the tool call for an approval
    to be able to resume the run."""
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=_HUMAN_INTERACTION_WORKFLOW_YAML,
        polled_status="paused",
    )
    _install_advancing_clock(monkeypatch)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    ctx.repair_origin_outputs_run_id = "wr_origin"
    origin = OriginOutputSnapshot(
        definition=harness["workflow"].workflow_definition,
        outputs={
            "request_access": OriginBlockOutput(
                status=BlockStatus.completed, has_value=True, created_at=datetime.now(UTC), value={"sent": True}
            )
        },
        settings=OriginExecutionSettings.of(harness["workflow"]),
    )
    ctx.repair_origin_outputs = origin
    ctx.frontier_selected_output_sources = {
        "request_access": SelectedOutputSource("request_access", "wr_origin", "origin", origin)
    }
    ctx.frontier_origin_reused_labels = ["request_access"]
    before = set(run_execution._DETACHED_CLEANUP_TASKS)

    started = time.monotonic()
    result = await _run_blocks_and_collect_debug({"block_labels": ["approve_login"], "parameters": {}}, ctx)
    elapsed = time.monotonic() - started

    assert elapsed < RUN_BLOCKS_SAFETY_CEILING_SECONDS / 10
    assert result["ok"] is False, result
    assert result["data"]["control_signal"]["kind"] == "watchdog_paused", result
    assert "paused" in result["data"]["user_facing_summary"].lower()
    assert "uncertain" not in result["error"].lower()
    assert result["data"]["reused_origin_output_labels"] == ["request_access"]
    assert result["data"]["origin_workflow_run_id"] == "wr_origin"

    harness["cancel_run_task"].assert_not_awaited()
    harness["cooperative_cancel"].assert_not_awaited()
    harness["clear"].assert_not_awaited()
    harness["publish"].assert_awaited_once()

    adopted = _adopted_detached_tasks(before)
    assert len(adopted) == 1
    await asyncio.sleep(0)
    assert harness["executor_cancelled"] is False
    assert not adopted[0].done()

    adopted[0].cancel()
    await asyncio.gather(*adopted, return_exceptions=True)


@pytest.mark.asyncio
async def test_tool_cancelled_while_paused_leaves_the_run_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pause is decided several awaits before the result is returned. A tool timeout landing in
    that window must still leave the run alive, or the person's approval has nothing to resume."""
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=_HUMAN_INTERACTION_WORKFLOW_YAML,
        polled_status="paused",
    )

    async def _cancel_mid_flight(*_args: Any, **_kwargs: Any) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(run_execution, "_watchdog_error_message", _cancel_mid_flight)

    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    before = set(run_execution._DETACHED_CLEANUP_TASKS)

    with pytest.raises(asyncio.CancelledError):
        await _run_blocks_and_collect_debug({"block_labels": ["approve_login"], "parameters": {}}, ctx)

    harness["cancel_run_task"].assert_not_awaited()
    harness["cooperative_cancel"].assert_not_awaited()

    adopted = _adopted_detached_tasks(before)
    assert len(adopted) == 1
    await asyncio.sleep(0)
    assert harness["executor_cancelled"] is False

    adopted[0].cancel()
    await asyncio.gather(*adopted, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_watchdog_terminated_run_still_carries_its_per_block_page_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=SEARCH_THEN_SELECT_WORKFLOW_YAML,
        polled_status="running",
        terminal_blocks=[
            terminal_extraction_block(
                "failed",
                label="select_first_result",
                final_url="https://fixture.test/results/widget",
                task_id="tsk_select",
            ),
            terminal_extraction_block(
                "completed",
                label="run_search",
                final_url="https://fixture.test/results/widget/page-1",
                task_id="tsk_search",
            ),
        ],
        recent_actions=[
            run_result_action_row("tsk_select", ActionType.WAIT, ActionStatus.failed, code_line=9),
            run_result_action_row("tsk_search", ActionType.CLICK, ActionStatus.completed),
        ],
    )
    _install_advancing_clock(monkeypatch)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"

    result = await _run_blocks_and_collect_debug(
        {"block_labels": ["run_search", "select_first_result"], "parameters": {}}, ctx
    )
    data = result["data"]

    assert data["control_signal"]["kind"] == "watchdog_ceiling"
    assert data["observed_block_end_urls"] == {
        "run_search": "https://fixture.test/results/widget/page-1",
        "select_first_result": "https://fixture.test/results/widget",
    }
    assert data["per_block_action_observations"]["run_search"] == ["click completed"]
    assert data["per_block_action_observations"]["select_first_result"] == ["wait failed code_line=9"]
    assert all("action_trace" not in block for block in data["blocks"])


@pytest.mark.asyncio
async def test_non_paused_watchdog_exit_still_cancels_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pause carve-out is scoped to the pause: a run that reaches the ceiling still gets cancelled
    and still releases the run-session association."""
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=_EXTRACTION_WORKFLOW_YAML,
        polled_status="running",
    )
    _install_advancing_clock(monkeypatch)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    before = set(run_execution._DETACHED_CLEANUP_TASKS)

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert result["data"]["control_signal"]["kind"] == "watchdog_ceiling"
    harness["cancel_run_task"].assert_awaited_once()
    harness["clear"].assert_awaited_once()
    assert _adopted_detached_tasks(before) == []

    for relayed in (
        result["data"]["failure_reason"],
        result["data"]["user_facing_summary"],
        result["data"]["control_signal"]["user_facing_summary"],
    ):
        assert relayed
        assert contains_internal_machinery_leak(relayed) is False
        assert_clean_user_facing_text(relayed)
    assert "Run ID:" in result["error"]


@pytest.mark.asyncio
async def test_non_success_watchdog_result_types_selected_failed_block_locators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=_CODE_WORKFLOW_YAML,
        polled_status="running",
    )
    monkeypatch.setattr(
        forge_app.AGENT_FUNCTION,
        "allow_copilot_inline_code_execution",
        MagicMock(return_value=True),
    )
    forge_app.DATABASE.observer.get_workflow_run_blocks = AsyncMock(
        return_value=[
            WorkflowRunBlock(
                label="click_submit",
                block_type=BlockType.CODE,
                status="failed",
                workflow_run_block_id="wrb_click_submit",
                workflow_run_id="wr_paused",
                organization_id="org-1",
                created_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
                modified_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
            )
        ]
    )
    observe = AsyncMock(return_value=[{"authored_selector": "#submit", "unobserved_reason": "run_page_unavailable"}])
    monkeypatch.setattr(run_execution, "_observe_authored_locators", observe)
    _install_advancing_clock(monkeypatch)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"

    result = await _run_blocks_and_collect_debug({"block_labels": ["click_submit"], "parameters": {}}, ctx)

    assert result["data"].get("authored_locator_observations") == [
        {"authored_selector": "#submit", "unobserved_reason": "run_page_unavailable"}
    ], result
    observe.assert_awaited_once_with(
        ctx,
        run_session_id="pbs_run",
        failed_block_code='await page.locator("#submit").click()\n',
        worker_owned=False,
        observation_deadline_exceeded=False,
    )


@pytest.mark.asyncio
async def test_progressing_worker_run_crosses_legacy_boundary_and_returns_terminal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=_EXTRACTION_WORKFLOW_YAML,
        polled_status="running",
        dispatch_to_worker=True,
        terminal_blocks=[
            WorkflowRunBlock(
                label="extract_heading",
                block_type=BlockType.EXTRACTION,
                status="completed",
                failure_reason=None,
                error_codes=[],
                output={"heading": "Example Domain"},
                workflow_run_block_id="wrb_terminal",
                workflow_run_id="wr_paused",
                organization_id="org-1",
                task_id=None,
                final_url="https://example.com/result",
                created_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
                modified_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
            )
        ],
    )
    elapsed = 0.0
    progress = iter(
        (
            (0.0, "running"),
            (120.0, "running"),
            (241.0, "running"),
            (300.0, "completed"),
        )
    )

    async def _read_progress(_ctx: CopilotContext, _run_id: str) -> tuple[Any, datetime]:
        nonlocal elapsed
        elapsed, status = next(progress)
        marker = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=elapsed)
        return _fake_run(status=status, modified_at=marker), marker

    monkeypatch.setattr(run_execution, "_read_progress_sources", _read_progress)
    monkeypatch.setattr(run_execution, "time", SimpleNamespace(monotonic=lambda: elapsed))

    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert elapsed == 300.0
    assert result["ok"] is True, result
    assert result["data"]["workflow_run_id"] == "wr_paused"
    assert result["data"]["overall_status"] == "completed"
    assert result["data"]["current_url"] == "https://example.com/result"
    assert result["data"]["blocks"] == [
        {
            "label": "extract_heading",
            "block_type": "EXTRACTION",
            "status": "completed",
            "workflow_run_block_id": "wrb_terminal",
            "output": {"heading": "Example Domain"},
            "extracted_data": {"heading": "Example Domain"},
        }
    ]
    assert "failure_categories" not in result["data"]
    harness["worker_execute"].assert_awaited_once()
    harness["cooperative_cancel"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_yaml", "label", "block_type"),
    [
        (_NAVIGATION_WORKFLOW_YAML, "open_page", BlockType.NAVIGATION),
        (_EXTRACTION_WORKFLOW_YAML, "extract_heading", BlockType.EXTRACTION),
    ],
    ids=["navigation", "extraction"],
)
async def test_silent_worker_run_finishes_pending_model_call_without_cancel(
    monkeypatch: pytest.MonkeyPatch,
    workflow_yaml: str,
    label: str,
    block_type: BlockType,
) -> None:
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=workflow_yaml,
        polled_status="running",
        dispatch_to_worker=True,
        terminal_blocks=[
            WorkflowRunBlock(
                label=label,
                block_type=block_type,
                status="completed",
                workflow_run_block_id="wrb_terminal",
                workflow_run_id="wr_paused",
                organization_id="org-1",
                created_at=datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
                modified_at=datetime(2026, 4, 21, 12, 3, 3, tzinfo=UTC),
            )
        ],
    )
    elapsed = 0.0
    progress = iter(
        (
            (0.0, "running"),
            (89.0, "running"),
            (91.0, "running"),
            (182.0, "running"),
            (183.0, "completed"),
        )
    )
    marker = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)

    async def _read_progress(_ctx: CopilotContext, _run_id: str) -> tuple[Any, datetime]:
        nonlocal elapsed
        elapsed, status = next(progress)
        return _fake_run(status=status, modified_at=marker), marker

    monkeypatch.setattr(run_execution, "_read_progress_sources", _read_progress)
    monkeypatch.setattr(run_execution, "time", SimpleNamespace(monotonic=lambda: elapsed))
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"

    result = await _run_blocks_and_collect_debug({"block_labels": [label], "parameters": {}}, ctx)

    harness["cooperative_cancel"].assert_not_awaited()
    assert elapsed == 183.0
    assert result["ok"] is True, result
    assert result["data"]["workflow_run_id"] == "wr_paused"
    assert result["data"]["overall_status"] == "completed"
    harness["worker_execute"].assert_awaited_once()


@pytest.mark.asyncio
async def test_never_terminal_worker_run_is_cancelled_at_the_safety_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=_EXTRACTION_WORKFLOW_YAML,
        polled_status="running",
        dispatch_to_worker=True,
    )
    _install_advancing_clock(monkeypatch)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert result["ok"] is False, result
    assert result["data"]["control_signal"]["kind"] == "watchdog_ceiling"
    assert f"{RUN_BLOCKS_SAFETY_CEILING_SECONDS - 10}s" in result["data"]["user_facing_summary"]
    harness["cooperative_cancel"].assert_awaited_once_with("wr_paused")


@pytest.mark.asyncio
async def test_terminal_row_written_before_the_ceiling_cancel_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=_EXTRACTION_WORKFLOW_YAML,
        polled_status="running",
        dispatch_to_worker=True,
    )
    forge_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=_fake_run(status="completed"))
    _install_advancing_clock(monkeypatch)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    harness["cooperative_cancel"].assert_not_awaited()
    assert result["ok"] is True, result
    assert result["data"]["overall_status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "ok"), [("completed", True), ("failed", False)])
async def test_immediate_terminal_worker_result_is_returned_as_recorded(
    monkeypatch: pytest.MonkeyPatch, status: str, ok: bool
) -> None:
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=_EXTRACTION_WORKFLOW_YAML,
        polled_status=status,
        dispatch_to_worker=True,
    )
    clock = _install_advancing_clock(monkeypatch, step_seconds=1.0)
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert clock() < RUN_BLOCKS_SAFETY_CEILING_SECONDS - 10
    harness["cooperative_cancel"].assert_not_awaited()
    assert result["ok"] is ok, result
    assert result["data"]["overall_status"] == status
    assert "control_signal" not in result["data"]


@pytest.mark.asyncio
async def test_externally_cancelled_worker_run_still_cooperatively_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _install_run_harness(
        monkeypatch,
        workflow_yaml=_EXTRACTION_WORKFLOW_YAML,
        polled_status="running",
        dispatch_to_worker=True,
    )
    polling = asyncio.Event()
    reads = 0

    async def _read_progress(_ctx: CopilotContext, _run_id: str) -> tuple[Any, datetime]:
        nonlocal reads
        reads += 1
        marker = datetime(2026, 4, 21, 12, 0, reads, tzinfo=UTC)
        if reads > 1:
            polling.set()
            await asyncio.Event().wait()
        return _fake_run(status="running", modified_at=marker), marker

    monkeypatch.setattr(run_execution, "_read_progress_sources", _read_progress)

    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    run = asyncio.create_task(
        _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)
    )
    await asyncio.wait_for(polling.wait(), timeout=5)

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    harness["cooperative_cancel"].assert_awaited_once_with("wr_paused")


def test_paused_result_records_last_test_ok_as_none() -> None:
    """``None`` is the only honest value: at ``False`` the finalizer rewrites the reply into a
    failed test, and ``True`` would let an unapproved draft count as verified."""
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")

    run_execution._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"workflow_run_id": "wr_paused", "control_signal": {"kind": "watchdog_paused"}},
        },
    )

    assert ctx.last_test_ok is None


def test_non_paused_failure_still_records_last_test_ok_as_false() -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")

    run_execution._record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"workflow_run_id": "wr_ceiling", "control_signal": {"kind": "watchdog_ceiling"}},
        },
    )

    assert ctx.last_test_ok is False


# ---------------------------------------------------------------------------
# Reconciliation guard message: regression guard on "timed out" phrasing.
# The guard itself is tested in test_copilot_cancel_helpers.py; this test is
# specifically about the LLM-facing STRING, which previously said "timed out"
# and read as a retry-invite when combined with LLM priors.
# ---------------------------------------------------------------------------
