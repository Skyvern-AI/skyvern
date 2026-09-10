"""SKY-15371 v4 contract: synchronous FileDownloadBlock action lifecycle.

These tests drive the real caller ``ActionHandler.handle_action``. For an eligible ``download=False``
``ClickAction``/``SelectOptionAction`` inside a FileDownloadBlock that opens a new Page in the initiating
BrowserContext *without firing any Playwright popup/download event* (the deployed anchor incident's CDP shape)
while a task-local file settles after an event-controlled delay, ``handle_action`` must stay pending until the
file completes, then close the exact last action-window Page to terminal state *before* returning.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import ClickAction, SelectOption, SelectOptionAction
from skyvern.webeye.actions.handler import ActionHandler
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor
from tests.unit.helpers import make_organization, make_step, make_task

_HANDLER = "skyvern.webeye.actions.handler"


class _FakeContext:
    """Stand-in BrowserContext: only the ``pages`` list is read by the delta/close logic."""

    def __init__(self) -> None:
        self.pages: list[_FakePage] = []


class _FakePage:
    """Deterministic Page: records popup listeners, close ordering, and terminal state."""

    def __init__(
        self,
        context: _FakeContext,
        url: str,
        events: list[str],
        label: str,
        close_raises: bool = False,
        close_hangs: bool = False,
        close_error_message: str = "target closed while closing popup",
    ) -> None:
        self.context = context
        self.url = url
        self._events = events
        self._label = label
        self._closed = False
        self.close_terminal = False
        self._close_raises = close_raises
        self._close_hangs = close_hangs
        self._close_error_message = close_error_message
        self._popup_cbs: list[Callable[[Any], None]] = []

    def on(self, event: str, cb: Callable[[Any], None]) -> None:
        if event == "popup":
            self._popup_cbs.append(cb)

    def off(self, event: str, cb: Callable[[Any], None]) -> None:
        if event == "popup" and cb in self._popup_cbs:
            self._popup_cbs.remove(cb)

    def remove_listener(self, event: str, cb: Callable[[Any], None]) -> None:
        self.off(event, cb)

    def is_closed(self) -> bool:
        return self._closed

    async def close(self, **_kwargs: Any) -> None:
        self._events.append(f"{self._label}:close_start")
        # Yield once so a terminal-close ordering is observable on the event loop.
        await asyncio.sleep(0)
        if self._close_hangs:
            # Never resolves on its own; the caller's bounded close timeout must cancel this.
            self._events.append(f"{self._label}:close_hang")
            await asyncio.Event().wait()
        if self._close_raises:
            self._events.append(f"{self._label}:close_error")
            raise RuntimeError(self._close_error_message)
        self._closed = True
        self.close_terminal = True
        self._events.append(f"{self._label}:close_terminal")


async def _spin(task: asyncio.Task[Any], turns: int = 300) -> bool:
    """Give the loop up to ``turns`` cycles; return whether ``task`` has completed."""
    for _ in range(turns):
        if task.done():
            return True
        await asyncio.sleep(0)
    return task.done()


def _make_env(**task_overrides: Any) -> tuple[Any, Any, Any, SkyvernContext, list[str]]:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="task-v4", workflow_run_id="wr-v4", **task_overrides)
    step = make_step(now, task, step_id="step-v4", status=StepStatus.created, order=0, output=None)
    ctx = SkyvernContext(task_id=task.task_id)
    events: list[str] = []
    return task, step, MagicMock(), ctx, events


def _enter_patches(stack: ExitStack, app_mock: Any, ctx: SkyvernContext, download_dir: Path, inner: Any) -> None:
    stack.enter_context(patch.object(ActionHandler, "_handle_action", side_effect=inner))
    stack.enter_context(patch(f"{_HANDLER}.app", app_mock))
    stack.enter_context(patch(f"{_HANDLER}.get_download_dir", MagicMock(return_value=str(download_dir))))
    stack.enter_context(patch(f"{_HANDLER}.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.0))
    stack.enter_context(patch(f"{_HANDLER}.skyvern_context.current", return_value=ctx))


def _app_mock(action: Any) -> Any:
    browser_state = MagicMock()
    browser_state.browser_artifacts = MagicMock(needs_cdp_frame_publisher=False, remote_browser_session_id=None)
    browser_state.release_driver_on_close = False
    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = browser_state
    app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=action)
    return app_mock


async def _run_ordering_case(action: Any, tmp_path: Path) -> None:
    """Shared ordering assertion for both Click and SelectOption download=False cases. Opens an event-free
    same-context extra Page, lands the task-local file only after asserting ``handle_action`` is still pending,
    and proves handle_action awaits the file and closes the exact extra Page (terminal) before a sentinel."""
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/documents", events, "opener")
    context.pages.append(opener)

    extra_pages: list[_FakePage] = []
    inner_returned = asyncio.Event()

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        # Action-window Page delta with no Playwright popup/download event (CDP/file-scan shape).
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        inner_returned.set()
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    downloaded_file = tmp_path / "statement.pdf"

    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        handle = asyncio.ensure_future(
            ActionHandler.handle_action(
                scraped_page,
                task,
                step,
                opener,
                action,
                file_download_false_click_eligible=True,
            )
        )
        await asyncio.wait_for(inner_returned.wait(), timeout=5)
        # Baseline: the extra Page exists; the task-local file has NOT completed yet.
        assert extra_pages, "setup: inner action did not open the action-window extra Page"
        extra = extra_pages[0]
        assert not downloaded_file.exists(), "setup: the download file must not exist before release"

        settled_before_file = await _spin(handle)

        # v4 REQUIRED #1: handle_action must remain pending until the task-local file completes.
        assert not settled_before_file, (
            "handle_action returned before the task-local download file completed; it did not "
            "synchronously await the download-settlement path"
        )
        assert not extra.is_closed(), "setup: extra Page must not be closed before the file completes"

        # Event-controlled delay elapses: the stable task-local file lands now.
        downloaded_file.write_bytes(b"%PDF-1.4 statement")

        results = await asyncio.wait_for(handle, timeout=5)

    # A next-scrape sentinel runs only after handle_action returns.
    events.append("sentinel")

    # v4 REQUIRED #2: the exact extra Page reached terminal close before handle_action returned.
    assert extra.is_closed(), "the action-window extra Page was left open when handle_action returned"
    assert extra.close_terminal, "the extra Page close never reached terminal state inside handle_action"
    assert not opener.is_closed(), "the opener page must remain open"
    assert isinstance(results[-1], ActionSuccess)

    # v4 REQUIRED #3: the sentinel cannot observe a non-terminal close -- close precedes sentinel.
    assert "popup:close_terminal" in events, "the extra Page never reached terminal close before the sentinel"
    assert events.index("popup:close_terminal") < events.index("sentinel"), (
        "next-scrape sentinel ran before the extra Page close reached terminal state"
    )


@pytest.mark.asyncio
async def test_click_download_false_awaits_file_then_closes_before_return(tmp_path: Path) -> None:
    await _run_ordering_case(ClickAction(element_id="dl-link", download=False), tmp_path)


@pytest.mark.asyncio
async def test_select_option_download_false_awaits_file_then_closes_before_return(tmp_path: Path) -> None:
    await _run_ordering_case(
        SelectOptionAction(element_id="dl-select", option=SelectOption(label="Download PDF"), download=False),
        tmp_path,
    )


@pytest.mark.asyncio
async def test_event_free_file_created_during_inner_action_still_closes(tmp_path: Path) -> None:
    """Regression: the inner action opens the same-context extra Page AND writes the final local file before
    returning, with no Playwright popup/download event. Because the download baseline is captured BEFORE the
    action, that file is seen as new, finalized, and the exact last delta Page is closed (terminal) before
    handle_action returns. A post-action baseline would miss this file entirely."""
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        # Final local file lands DURING the action, before returning; no popup/download event fires.
        (tmp_path / "statement.pdf").write_bytes(b"%PDF-1.4 statement")
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        results = await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )
    events.append("sentinel")

    assert extra_pages and extra_pages[0].is_closed() and extra_pages[0].close_terminal, (
        "the exact last delta Page must close even when the file lands during the action"
    )
    assert results[-1].downloaded_files == action.downloaded_files == ["statement.pdf"]
    assert results[-1].download_triggered is True
    assert not opener.is_closed(), "the opener page must remain open"
    assert "popup:close_terminal" in events and events.index("popup:close_terminal") < events.index("sentinel")


@pytest.mark.asyncio
async def test_close_failure_is_truthful_not_silent_success(tmp_path: Path) -> None:
    """Boundary: if the exact last delta Page close raises (or never reaches terminal state), the method must
    not report clean cleanup. The completed download is still credited, but the batch is stopped so the next
    step rescrapes rather than selecting the wedged popup, the claim is retained as the late-credit backstop,
    and the failure is not silently swallowed as convergence."""
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []
    # Synthetic secret canary carried by the close exception, standing in for a signed download URL /
    # page content that a raw exc_info payload would leak into the log.
    secret_canary = "https://signed.example/SECRET-CANARY-download-token"

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(
            context, "http://origin/doc", events, "popup", close_raises=True, close_error_message=secret_canary
        )
        context.pages.append(extra)
        extra_pages.append(extra)
        (tmp_path / "statement.pdf").write_bytes(b"%PDF-1.4 statement")
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    log_mock = MagicMock()
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        stack.enter_context(patch(f"{_HANDLER}.LOG", log_mock))
        results = await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    extra = extra_pages[0]
    assert not extra.is_closed(), "a failed close must not report the page as closed"
    assert "popup:close_error" in events, "the close failure must actually have been exercised"
    # Privacy: the close-failure warning must carry only bounded fields -- no exception payload
    # (exc_info) and no leaked exception message/canary.
    close_warnings = [
        call
        for call in log_mock.warning.call_args_list
        if call.args and call.args[0] == "Failed to close action-window download popup after download credit"
    ]
    assert len(close_warnings) == 1, "exactly one close-failure warning is expected"
    warn = close_warnings[0]
    assert "exc_info" not in warn.kwargs, "close-failure warning must not carry an exception payload"
    assert set(warn.kwargs) == {
        "task_id",
        "step_id",
        "workflow_run_id",
        "close_reached_terminal",
        "close_timed_out",
        "close_timeout_seconds",
    }, "close-failure warning must emit only the bounded fields"
    assert secret_canary not in repr(warn), "close-failure warning must not leak the exception message/canary"
    # Truthful non-convergence: stop the batch so the next step rescrapes instead of selecting the wedge.
    assert results[-1].skip_remaining_actions is True
    # The download itself completed, so it is still credited truthfully.
    assert results[-1].download_triggered is True and results[-1].downloaded_files == ["statement.pdf"]
    # The task claim is retained as the late-credit backstop -- not discarded on a failed close.
    assert any(candidate is extra for candidate in ctx.download_popup_claims.get(task.task_id, [])), (
        "a failed close must keep the popup claim for the late-credit backstop"
    )


@pytest.mark.asyncio
async def test_hanging_close_is_bounded_and_truthful(tmp_path: Path) -> None:
    """MUST-FIX: a close that never resolves must be bounded by BROWSER_PAGE_CLOSE_TIMEOUT (patched small) so
    handle_action still returns, routing the timeout into the truthful non-terminal path -- page not reported
    closed, claim retained as the late-credit backstop, batch stopped, download still credited."""
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup", close_hangs=True)
        context.pages.append(extra)
        extra_pages.append(extra)
        (tmp_path / "statement.pdf").write_bytes(b"%PDF-1.4 statement")
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        stack.enter_context(patch(f"{_HANDLER}.BROWSER_PAGE_CLOSE_TIMEOUT", 0.1))
        results = await asyncio.wait_for(
            ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            ),
            timeout=5,
        )

    extra = extra_pages[0]
    assert "popup:close_hang" in events, "the hanging close must actually have been exercised"
    assert not extra.is_closed() and not extra.close_terminal, "a hung close must not be reported as closed"
    # Truthful non-convergence: stop the batch so the next step rescrapes instead of selecting the wedge.
    assert results[-1].skip_remaining_actions is True
    # The download itself completed before the close was attempted, so it is still credited truthfully.
    assert results[-1].download_triggered is True and results[-1].downloaded_files == ["statement.pdf"]
    # The task claim is retained as the late-credit backstop -- not discarded on a timed-out close.
    assert any(candidate is extra for candidate in ctx.download_popup_claims.get(task.task_id, [])), (
        "a timed-out close must keep the popup claim for the late-credit backstop"
    )


@pytest.mark.asyncio
async def test_no_page_delta_returns_immediately_without_entering_grace(tmp_path: Path) -> None:
    """Contract row 1: an eligible action that opens no Page and produces no file must return IMMEDIATELY,
    never entering a fixed no-signal grace. The file-start grace is patched large (30s); if any no-signal
    grace were entered, the sub-second outer wait_for would expire. On the unfixed head the synchronous
    helper runs even with an empty immediate delta and polls the full grace before giving up (RED)."""
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        stack.enter_context(patch(f"{_HANDLER}.FILE_DOWNLOAD_START_SIGNAL_GRACE_SECONDS", 30.0))
        # A sub-second wait_for under a 30s grace proves no fixed no-signal grace is entered at all.
        results = await asyncio.wait_for(
            ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            ),
            timeout=0.5,
        )

    assert not opener.is_closed(), "no Page delta must not trigger any close"
    assert not ctx.download_popup_claims.get(task.task_id), "no Page delta must not record a popup claim"
    assert results[-1].download_triggered is not True, "no Page delta must not credit a download"


@pytest.mark.asyncio
async def test_page_delta_without_completed_file_does_not_close_or_claim_success(tmp_path: Path) -> None:
    """Boundary (row 2, no file): a Page delta with no completed task-local file must not close by count
    nor claim success, and -- pinning the ``file_signal_observed`` conjunct (r3960194368) -- the
    settle/finalize wait must never be invoked when no file signal is present. A short ``download_timeout``
    bounds the file-start poll so the deterministic no-file case gives up fast.
    """
    from skyvern.webeye.actions import handler as handler_mod

    task, step, scraped_page, ctx, events = _make_env(download_timeout=0.3)
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    finalize_spy = AsyncMock(side_effect=handler_mod._finalize_download_artifacts)
    # download dir stays empty -- no completed file ever appears.
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        stack.enter_context(patch(f"{_HANDLER}._finalize_download_artifacts", finalize_spy))
        await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    assert extra_pages and not extra_pages[0].is_closed(), (
        "a Page delta without a completed file must not be closed on page-count alone"
    )
    assert action.download_triggered is not True, "no completed file must not be claimed as a successful download"
    assert finalize_spy.call_count == 0, (
        "the settle/finalize wait must never run without a file signal (file_signal_observed conjunct pin)"
    )


@pytest.mark.asyncio
async def test_incomplete_temp_suffix_is_not_completion_evidence(tmp_path: Path) -> None:
    """Boundary: an in-flight browser temp-suffix file (.crdownload) is not a completed download, so the
    action-window Page is not closed and no success is credited on its presence alone (a short
    ``download_timeout`` bounds the settle poll)."""
    task, step, scraped_page, ctx, events = _make_env(download_timeout=0.3)
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        # An in-flight browser temp file appears but never settles to a final name.
        (tmp_path / "statement.pdf.crdownload").write_bytes(b"partial")
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    assert extra_pages and not extra_pages[0].is_closed(), "an in-flight temp-suffix file must not authorize a close"
    assert action.download_triggered is not True, "an in-flight temp-suffix file must not be credited as completed"


@pytest.mark.asyncio
async def test_foreign_context_page_is_not_close_authority(tmp_path: Path) -> None:
    """Boundary: if working-page recovery reconnects into a different BrowserContext, the pages in that
    replaced context are not action-window deltas and must not be closed or claimed."""
    task, step, scraped_page, ctx, events = _make_env()
    baseline_context = _FakeContext()
    opener = _FakePage(baseline_context, "http://origin/", events, "opener")
    baseline_context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    foreign_context = _FakeContext()
    foreign_page = _FakePage(foreign_context, "http://origin/foreign", events, "foreign")
    foreign_context.pages.append(foreign_page)

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        # Recovery reconnected the initiating page into a different context than the pre-action baseline.
        opener.context = foreign_context
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    assert not foreign_page.is_closed(), "a page in a replaced/foreign context must not be closed"
    assert foreign_page not in ctx.download_popup_claims.get(task.task_id, []), (
        "a page in a replaced/foreign context must not be recorded as an action-window claim"
    )


@pytest.mark.asyncio
async def test_non_download_popup_returns_within_start_grace_not_download_timeout(tmp_path: Path) -> None:
    """P2: a delta Page that never produces a download signal must return after the bounded start-signal grace
    (patched small), not the full download budget (BROWSER_DOWNLOAD_NO_SIGNAL_GRACE_TIME=120s); on the unfixed
    head the poll waits the 120s no-signal grace and the outer wait_for expires (RED)."""
    task, step, scraped_page, ctx, events = _make_env()  # download_timeout=None
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        return [ActionSuccess()]  # no download file ever appears

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        stack.enter_context(patch(f"{_HANDLER}.FILE_DOWNLOAD_START_SIGNAL_GRACE_SECONDS", 0.5, create=True))
        results = await asyncio.wait_for(
            ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            ),
            timeout=2.0,
        )

    extra = extra_pages[0]
    assert not extra.is_closed(), "a non-download popup must not be closed"
    assert results[-1].download_triggered is not True, "no file => no download credit"
    # Retained as the late-credit backstop; the synchronous path only gives up on the bounded start grace.
    assert any(candidate is extra for candidate in ctx.download_popup_claims.get(task.task_id, []))


@pytest.mark.asyncio
async def test_file_start_and_finalization_share_one_overall_deadline(tmp_path: Path) -> None:
    """P2: file-start detection and finalization must share one overall budget, not stack two full timeouts.
    A ``.crdownload`` lands ~0.8s post-action and never settles under download_timeout=1.0. On the unfixed
    head detection (~0.8s) plus a fresh 1.0s finalization wait is ~1.8s; the fix caps the total at the 1.0s
    overall budget. No durable file => no credit, no close."""
    task, step, scraped_page, ctx, events = _make_env(download_timeout=1.0)
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []

    async def _land_partial_then_never_settle() -> None:
        await asyncio.sleep(0.8)
        (tmp_path / "statement.pdf.crdownload").write_bytes(b"partial")

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        asyncio.ensure_future(_land_partial_then_never_settle())
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    started = time.monotonic()
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        stack.enter_context(patch(f"{_HANDLER}.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0.05))
        await asyncio.wait_for(
            ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            ),
            timeout=5.0,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.5, f"detection + finalization must share one overall budget; elapsed={elapsed:.2f}s"
    assert not extra_pages[0].is_closed(), "an unsettled .crdownload must not authorize a close"
    assert action.download_triggered is not True, "an unsettled .crdownload must not be credited"


@pytest.mark.asyncio
async def test_late_start_signal_at_default_grace_zero_still_credits_and_closes(tmp_path: Path) -> None:
    """Negative control (guards the disabled-by-default trap): with the popup grace at its default 0, the
    synchronous detection window must still be the real (unpatched) 10s start grace, so a final file that
    starts ~0.5s after the action is detected, finalized, credited, and the exact last delta Page closed
    before return."""
    task, step, scraped_page, ctx, events = _make_env()  # download_timeout=None, grace default 0
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []

    async def _land_final_after_delay() -> None:
        await asyncio.sleep(0.5)
        (tmp_path / "statement.pdf").write_bytes(b"%PDF-1.4 statement")

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        asyncio.ensure_future(_land_final_after_delay())
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        stack.enter_context(patch(f"{_HANDLER}.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0.05))
        results = await asyncio.wait_for(
            ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            ),
            timeout=5.0,
        )

    extra = extra_pages[0]
    assert extra.is_closed() and extra.close_terminal, "a real late-start download must still close the popup"
    assert results[-1].downloaded_files == action.downloaded_files == ["statement.pdf"]
    assert results[-1].download_triggered is True
    assert not opener.is_closed(), "the opener page must remain open"


@pytest.mark.asyncio
async def test_file_delta_without_page_delta_returns_immediately(tmp_path: Path) -> None:
    """Contract row 3 (RED test 5): a task-local file that lands during the inner action with NO same-context
    Page delta stays owned by the pre-existing download handling. The synchronous false-click path must return
    immediately (no Page wait, popup close, or new attribution) even with the file-start grace patched large.
    On the unfixed head the helper polls the full grace before giving up (RED: the sub-second wait_for expires)."""
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        # A file lands during the action, but no Page is opened in the initiating context.
        (tmp_path / "statement.pdf").write_bytes(b"%PDF-1.4 statement")
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        stack.enter_context(patch(f"{_HANDLER}.FILE_DOWNLOAD_START_SIGNAL_GRACE_SECONDS", 30.0))
        results = await asyncio.wait_for(
            ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            ),
            timeout=0.5,
        )

    assert not opener.is_closed(), "a file-only signal must not close anything"
    assert action.download_triggered is not True and not results[-1].download_triggered, (
        "the false-click path must not add new attribution for a file-only delta"
    )
    assert not ctx.download_popup_claims.get(task.task_id), "no Page delta => no claim"


@pytest.mark.asyncio
async def test_preexisting_interceptor_temp_settling_does_not_credit_or_close(tmp_path: Path) -> None:
    """Contract RED test 7 (P0 r3960194346): a pre-existing interceptor temp file shaped as
    ``<final>.<32-lowercase-hex>.crdownload`` (the interceptor uuid4().hex temp name) that settles to ``<final>``
    during an action that opens a popup is NOT a new signal -- it was in flight before the action. Its identity
    must normalize to the settled final file's, so an unrelated older download completing cannot mis-credit this
    click or close the popup. On the unfixed head only ``.crdownload`` is stripped (RED: popup closed, credited)."""
    import uuid

    task, step, scraped_page, ctx, events = _make_env(download_timeout=0.3)
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    # Pre-existing interceptor temp file, present BEFORE the action (part of the baseline).
    interceptor_temp = tmp_path / f"statement.pdf.{uuid.uuid4().hex}.crdownload"
    interceptor_temp.write_bytes(b"partial-from-earlier-download")

    extra_pages: list[_FakePage] = []

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        # The earlier download settles now: the final file is published and the temp removed.
        (tmp_path / "statement.pdf").write_bytes(b"%PDF-1.4 statement")
        interceptor_temp.unlink()
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    assert extra_pages and not extra_pages[0].is_closed(), (
        "a pre-existing interceptor temp settling during the action must not authorize a close"
    )
    assert action.download_triggered is not True, "an older download settling must not credit this click"
    assert not action.downloaded_files, "no newly attributable file must be credited"


@pytest.mark.asyncio
async def test_new_file_settles_without_waiting_on_unrelated_preexisting_partial(tmp_path: Path) -> None:
    """Contract RED test 8 (P1 r3960194355): an unrelated pre-existing ``.crdownload`` that never settles must
    not extend this action's settlement wait. With a large download budget, the action opens a popup and a
    genuinely new file is already fully settled; finalization must wait only on the newly attributable file
    (none in flight here) and return promptly, crediting only the new file and closing the popup. On the
    unfixed head finalize waits on every ``.crdownload`` in the shared dir, so the never-settling partial holds
    the action for the whole budget (RED)."""
    task, step, scraped_page, ctx, events = _make_env(download_timeout=30.0)
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    # Unrelated pre-existing partial that never settles -- part of the baseline.
    (tmp_path / "old.bin.crdownload").write_bytes(b"stalled-forever")

    extra_pages: list[_FakePage] = []

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        # A genuinely new file that is already fully settled (no .crdownload of its own).
        (tmp_path / "new.pdf").write_bytes(b"%PDF-1.4 new")
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    started = time.monotonic()
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        results = await asyncio.wait_for(
            ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            ),
            timeout=5.0,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"an unrelated pre-existing partial must not extend the wait; elapsed={elapsed:.2f}s"
    assert extra_pages[0].is_closed() and extra_pages[0].close_terminal, "the new download's popup must be closed"
    assert results[-1].downloaded_files == action.downloaded_files == ["new.pdf"]
    assert results[-1].download_triggered is True


@pytest.mark.asyncio
async def test_attributable_in_flight_file_is_waited_on_then_credited_and_closed(tmp_path: Path) -> None:
    """Contract RED test 8 positive half: an immediate same-context popup plus a genuinely new in-flight
    ``statement.pdf.crdownload`` (attributable versus the empty pre-action baseline) must hold ``handle_action``
    in the scoped settlement wait until background settlement renames the temp to the final ``statement.pdf``
    (only AFTER the test asserts ``handle_action`` is still pending), then credit it and close the exact last
    delta Page (terminal). Pins the success direction of ``attributable_downloading`` selection and the
    ``wait_for_download_finished`` settle branch: forcing ``attributable_downloading = []`` or dropping the
    ``elif downloading_files:`` branch finalizes before the final file exists, returning un-credited (RED)."""
    task, step, scraped_page, ctx, events = _make_env(download_timeout=30.0)
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/documents", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []
    inner_returned = asyncio.Event()
    crdownload = tmp_path / "statement.pdf.crdownload"
    final_file = tmp_path / "statement.pdf"

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        # A genuinely new in-flight download temp appears (attributable vs the empty baseline); it has not
        # settled to its final name yet, so only the scoped settlement wait can carry the action to credit.
        crdownload.write_bytes(b"partial")
        inner_returned.set()
        return [ActionSuccess()]

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        handle = asyncio.ensure_future(
            ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            )
        )
        await asyncio.wait_for(inner_returned.wait(), timeout=5)
        assert extra_pages, "setup: inner action did not open the action-window extra Page"
        extra = extra_pages[0]
        assert crdownload.exists() and not final_file.exists(), "setup: only the in-flight temp exists pre-release"

        settled_before_release = await _spin(handle)

        # The scoped settlement wait must hold handle_action while the attributable .crdownload is in flight.
        assert not settled_before_release, (
            "handle_action returned before the attributable .crdownload settled; it did not wait on the "
            "newly attributable in-flight file via wait_for_download_finished"
        )
        assert not extra.is_closed(), "the popup must not close before the attributable file settles"

        # Background settlement: publish the final file and remove the interceptor temp.
        final_file.write_bytes(b"%PDF-1.4 statement")
        crdownload.unlink()

        results = await asyncio.wait_for(handle, timeout=5)

    events.append("sentinel")

    assert extra.is_closed() and extra.close_terminal, (
        "the settled download's popup must reach terminal close before handle_action returns"
    )
    assert results[-1].downloaded_files == action.downloaded_files == ["statement.pdf"], (
        "only the newly attributable settled file must be credited"
    )
    assert results[-1].download_triggered is True
    assert not opener.is_closed(), "the opener page must remain open"
    assert "popup:close_terminal" in events and events.index("popup:close_terminal") < events.index("sentinel")


@pytest.mark.asyncio
async def test_cancellation_during_file_start_wait_propagates_without_close_or_credit(tmp_path: Path) -> None:
    """Cancellation during the row-2 file-start wait (an immediate Page delta, no file yet) must propagate
    as CancelledError without closing the popup or crediting a download."""
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="dl-link", download=False)

    extra_pages: list[_FakePage] = []

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        extra = _FakePage(context, "http://origin/doc", events, "popup")
        context.pages.append(extra)
        extra_pages.append(extra)
        return [ActionSuccess()]  # no file: the helper stays in the file-start grace poll

    app_mock = _app_mock(action)
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        stack.enter_context(patch(f"{_HANDLER}.FILE_DOWNLOAD_START_SIGNAL_GRACE_SECONDS", 2.0))
        stack.enter_context(patch(f"{_HANDLER}.DOWNLOAD_IN_FLIGHT_POLL_INTERVAL_SECONDS", 0.02))
        handle_task = asyncio.ensure_future(
            ActionHandler.handle_action(
                scraped_page, task, step, opener, action, file_download_false_click_eligible=True
            )
        )
        await asyncio.sleep(0.1)  # let the helper enter the file-start grace poll
        handle_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handle_task

    assert extra_pages and not extra_pages[0].is_closed(), "cancellation must not close the popup"
    assert action.download_triggered is not True, "cancellation must not credit a download"


def _bind_interceptor_admitting_within_window(
    context: _FakeContext, state: dict[str, Any]
) -> tuple[CDPDownloadInterceptor, asyncio.Future[Any] | None]:
    """Bind a real interceptor that reads quiescent at gate entry, then -- inside the one admission window, via
    ``state["sleep"]`` (a module-scoped SKY-14332 ``asyncio.sleep`` stand-in) -- injects what a *previous*
    action produced per ``state["mode"]``: a never-finishing CDP handler (``"cdp_handler"``) or a Page opened
    into the initiating context (``"preaction_page"``). The latter must land in the post-window delta baseline."""
    interceptor = CDPDownloadInterceptor(network_egress_monitor=MagicMock(), redirect_hop_authorizer=MagicMock())
    context._skyvern_cdp_download_interceptor = interceptor  # type: ignore[attr-defined]
    pending: asyncio.Future[Any] | None = (
        asyncio.ensure_future(asyncio.Event().wait()) if state["mode"] == "cdp_handler" else None
    )

    async def sleep_injecting_within_window(_delay: float, *_a: object, **_k: object) -> None:
        if not state["injected"]:
            state["injected"] = True
            if pending is not None:
                interceptor._cdp_handler_tasks.add(pending)
            else:
                preaction = _FakePage(context, "http://origin/preaction", state["events"], "preaction")
                context.pages.append(preaction)
                state["preaction_page"] = preaction
        await asyncio.sleep(0)

    state["sleep"] = sleep_injecting_within_window
    return interceptor, pending


@pytest.mark.parametrize("mode", ["cdp_handler", "preaction_page"])
@pytest.mark.asyncio
async def test_preaction_activity_within_admission_window_never_credits_this_click(mode: str, tmp_path: Path) -> None:
    """Contract RED (P2 r3961545249 + r3962647702): whatever a *previous* action produced inside the one
    admission window is not this click's. ``cdp_handler`` -- spending the window admits a never-finishing CDP
    handler, so the gate reads not-quiescent and disables the v4 close (fail open, claim retained, never
    drained); the unbounded early-return misses it and wrongly credits/closes. ``preaction_page`` -- the gate
    is quiescent, but the baseline is snapshotted only AFTER the window, so a Page a prior action opened is
    pre-existing and is never claimed/closed/credited even though the inner action lands a file; snapshotting
    before the gate makes it look new and wrongly closes/credits it (RED)."""
    from skyvern.webeye import cdp_download_interceptor as icept_mod
    from skyvern.webeye.actions import handler as handler_mod
    from tests.unit.scoped_asyncio import ScopedAsyncio

    task, step, scraped_page, ctx, events = _make_env(download_timeout=0.3)
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="nav-link", download=False)
    state: dict[str, Any] = {"injected": False, "mode": mode, "events": events}
    interceptor, pending = _bind_interceptor_admitting_within_window(context, state)

    extra_pages: list[_FakePage] = []

    async def inner(*_args: object, **_kwargs: object) -> list[ActionSuccess]:
        if mode == "cdp_handler":  # a same-window Page delta the gate must veto; preaction_page adds no Page
            extra = _FakePage(context, "http://origin/popup", events, "popup")
            context.pages.append(extra)
            extra_pages.append(extra)
        (tmp_path / "statement.pdf").write_bytes(b"%PDF-1.4 statement")  # previous-action activity's file
        return [ActionSuccess()]

    settle_spy = AsyncMock(side_effect=handler_mod._settle_and_close_false_click_download)
    try:
        with ExitStack() as stack:
            _enter_patches(stack, _app_mock(action), ctx, tmp_path, inner)
            stack.enter_context(patch.object(icept_mod, "asyncio", ScopedAsyncio(sleep=state["sleep"])))
            stack.enter_context(patch(f"{_HANDLER}._settle_and_close_false_click_download", settle_spy))
            results = await asyncio.wait_for(
                ActionHandler.handle_action(
                    scraped_page, task, step, opener, action, file_download_false_click_eligible=True
                ),
                timeout=5.0,
            )

        claims = ctx.download_popup_claims.get(task.task_id, [])
        assert state["injected"], "the gate never spent an admission window, so this case was not exercised"
        assert settle_spy.call_count == 0, "previous-action activity within the window must disable the v4 close"
        assert action.download_triggered is not True and not results[-1].download_triggered, (
            "a file produced by previous-action activity must not be credited to this click"
        )
        if mode == "cdp_handler":
            assert pending is not None and not pending.done(), "the gate must not drain the queued CDP handler"
            assert extra_pages and not extra_pages[0].is_closed(), "a queued handler's file must not close this popup"
            assert any(candidate is extra_pages[0] for candidate in claims), (
                "failing open must preserve the popup claim for the late-credit backstop"
            )
        else:
            preaction = state["preaction_page"]
            assert not preaction.is_closed(), "a Page opened before the click (during the window) must not be closed"
            assert not any(candidate is preaction for candidate in claims), (
                "a pre-action Page is baseline/pre-existing and must not be claimed as this click's delta"
            )
    finally:
        if pending is not None:
            pending.cancel()
            interceptor._cdp_handler_tasks.discard(pending)
