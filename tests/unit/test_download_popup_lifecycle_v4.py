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
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

import skyvern.forge.sdk.core.skyvern_context as _skvctx
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import (
    ClickAction,
    InputTextAction,
    SelectOption,
    SelectOptionAction,
    SwitchTabAction,
)
from skyvern.webeye.actions.handler import ActionHandler, _browser_dispatch_committed, handle_switch_tab_action
from skyvern.webeye.actions.responses import ActionAbort, ActionFailure, ActionSuccess, StaleActionAbort
from skyvern.webeye.cdp_download_interceptor import CDPDownloadInterceptor
from tests.unit.helpers import make_organization, make_step, make_task

_HANDLER = "skyvern.webeye.actions.handler"
_AGENT = "skyvern.forge.agent"


class _FakeContext:
    """Stand-in BrowserContext: the ``pages`` list is read by the delta/close logic, and the
    ``on``/``remove_listener`` page-event surface backs the event-driven ``context.on("page")`` ownership
    (a same-context child that joins after the action seam returns without a popup event)."""

    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self._page_cbs: list[Callable[[Any], None]] = []

    def on(self, event: str, cb: Callable[[Any], None]) -> None:
        if event == "page":
            self._page_cbs.append(cb)

    def remove_listener(self, event: str, cb: Callable[[Any], None]) -> None:
        if event == "page" and cb in self._page_cbs:
            self._page_cbs.remove(cb)

    def page_listener_count(self) -> int:
        return len(self._page_cbs)

    def emit_page(self, page: Any) -> None:
        for cb in list(self._page_cbs):
            cb(page)


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

    async def bring_to_front(self) -> None:
        pass

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


def _dispatched(inner: Any) -> Any:
    """Wrap a patched ``_handle_action`` stub so it commits the browser-dispatch boundary before running,
    modeling a stub that reached the real type-handler seam (the common case these tests exercise). The
    real ``_handle_action`` sets this flag at its single dispatch site; a stub that instead models a
    no-dispatch exit (stale abort / already-in-desired-state suppression) sets the flag False itself."""

    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        _browser_dispatch_committed.set(True)
        return await inner(*args, **kwargs)

    return _wrapped


def _enter_patches(stack: ExitStack, app_mock: Any, ctx: SkyvernContext, download_dir: Path, inner: Any) -> None:
    stack.enter_context(patch.object(ActionHandler, "_handle_action", side_effect=_dispatched(inner)))
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


# --- Behavior A: event-driven Page ownership via context.on("page") (SKY-15371) ---------------------
# The deployed shape credits an inline download through the CDP monitor + file-scan lifecycle: no
# Playwright popup/download event fires, and the never-committed marker child joins the initiating
# BrowserContext SHORTLY AFTER handle_action returns -- after the in-seam popup listener and the
# action-window delta backstop have both been torn down. An event-driven ``context.on("page")`` owner,
# armed alongside the popup listener and stopped before the next action, records the late child
# for the existing durable-credit closer.


def test_arm_context_listener_records_after_return_child_and_detaches_on_take() -> None:
    ctx = SkyvernContext(task_id="t-A1")
    context = _FakeContext()

    def cb(child: Any) -> None:
        ctx.record_download_popup_claim("t-A1", child)

    ctx.arm_download_popup_context_listener("t-A1", context, cb)
    assert context.page_listener_count() == 1
    # Idempotent per (task, context): a second download click in the same task must not stack listeners.
    ctx.arm_download_popup_context_listener("t-A1", context, cb)
    assert context.page_listener_count() == 1

    child = object()
    context.emit_page(child)  # joins the context after the seam returned
    assert ctx.download_popup_claims.get("t-A1") == [child]

    taken = ctx.take_download_popup_claims("t-A1")
    assert taken == [child]
    # Detached at the durable claim boundary; a further late child is no longer observed.
    assert context.page_listener_count() == 0
    context.emit_page(object())
    assert ctx.download_popup_claims.get("t-A1") is None


def test_clear_claims_detaches_context_listener() -> None:
    ctx = SkyvernContext(task_id="t-A2")
    context = _FakeContext()
    ctx.arm_download_popup_context_listener("t-A2", context, lambda child: None)
    assert context.page_listener_count() == 1
    ctx.clear_download_popup_claims("t-A2")
    assert context.page_listener_count() == 0


def test_detach_swallows_remove_listener_error() -> None:
    ctx = SkyvernContext(task_id="t-A3")

    class _Boom:
        def on(self, *_a: Any) -> None:
            pass

        def remove_listener(self, *_a: Any) -> None:
            raise RuntimeError("context torn down")

    ctx.arm_download_popup_context_listener("t-A3", _Boom(), lambda child: None)
    # Terminal claim teardown must never raise even if the underlying context is already gone.
    ctx.take_download_popup_claims("t-A3")
    ctx.clear_download_popup_claims("t-A3")


@pytest.mark.asyncio
@pytest.mark.parametrize("late_url", [":", "about:blank", "", "http://origin/receipt"])
async def test_context_page_listener_closes_late_child_after_credit(tmp_path: Path, late_url: str) -> None:
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/documents", events, "opener")
    sibling = _FakePage(context, "http://origin/sibling", events, "sibling")
    context.pages.extend([opener, sibling])
    action = ClickAction(element_id="dl-link", download=False)
    app_mock = _app_mock(action)

    async def inner(*_a: object, **_k: object) -> list[ActionSuccess]:
        # No popup event, no download event, no action-window page delta: the whole in-seam capture
        # surface stays empty (the deployed CDP/file-scan shape).
        return [ActionSuccess()]

    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    # The in-seam popup listener is removed in the action finally; the context page owner is NOT --
    # removing it synchronously at return would miss the after-return child.
    assert context.page_listener_count() == 1, "context.on('page') owner must survive the action finally"

    late_child = _FakePage(context, late_url, events, "late")
    context.pages.append(late_child)
    context.emit_page(late_child)
    candidates = ctx.download_popup_late_candidates.get(task.task_id, [])
    assert any(c is late_child for c in candidates), "the after-return same-context child was not recorded"
    assert not late_child.is_closed(), "discovery alone must not close a page before durable credit"

    foreign = _FakePage(_FakeContext(), late_url, events, "foreign")
    context.emit_page(opener)
    context.emit_page(sibling)
    context.emit_page(foreign)
    assert ctx.download_popup_late_candidates.get(task.task_id) == [late_child]

    browser_state = MagicMock(browser_context=context)
    browser_state.list_valid_pages = AsyncMock(return_value=[opener, sibling])
    with patch(f"{_AGENT}.skyvern_context.current", return_value=ctx):
        await ForgeAgent()._close_credited_download_popups(task, browser_state)

    assert late_child.is_closed()
    assert not any(page.is_closed() for page in (opener, sibling, foreign))
    assert not opener._popup_cbs
    assert context.page_listener_count() == 0
    assert ctx.download_popup_context_listeners == {}
    assert ctx.download_popup_claims == {}
    assert ctx.download_popup_late_candidates == {}


# --- Behavior A guard: a deliberate NEW_TAB is not misclaimed and closed by download credit ----------
# The task-scoped context.on("page") owner records every same-context child until the durable claim
# boundary. A deliberate NEW_TAB (the task's last pre-credit action) creates a same-context page that the
# owner records; the ordinary discard-on-reuse cannot protect it because no later action starts on it. The
# creation seam must retire that exact page's claim so the credit consumer never closes an intentional tab.


@pytest.mark.asyncio
async def test_deliberate_new_tab_survives_download_credit(tmp_path: Path) -> None:
    from skyvern.webeye.actions.actions import NewTabAction
    from skyvern.webeye.actions.handler import handle_new_tab_action

    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)

    # Arm the context owner exactly as the false-click download seam does (records a late candidate).
    def on_context_child(child: Any) -> None:
        if child is not opener:
            ctx.record_download_popup_late_candidate(task.task_id, child)

    ctx.arm_download_popup_context_listener(task.task_id, context, on_context_child)

    new_tab = _FakePage(context, "http://origin/new", events, "newtab")

    browser_state = MagicMock()

    async def _new_page() -> Any:
        # Production new_page() joins the page to the context, firing context.on("page").
        context.pages.append(new_tab)
        context.emit_page(new_tab)
        return new_tab

    browser_state.new_page = _new_page
    browser_state.navigate_to_url = AsyncMock()
    browser_state.set_active_page = AsyncMock()

    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = browser_state

    action = NewTabAction(url="http://origin/new")
    with (
        patch(f"{_HANDLER}.app", app_mock),
        patch(f"{_HANDLER}.validate_fetch_url", MagicMock(return_value="http://origin/new")),
        patch(f"{_HANDLER}.skyvern_context.current", return_value=ctx),
    ):
        result = await handle_new_tab_action(action, opener, scraped_page, task, step)
    assert isinstance(result[-1], ActionSuccess)

    # The creation seam retired the exact new tab's claim before durable credit.
    assert all(c is not new_tab for c in ctx.download_popup_late_candidates.get(task.task_id, [])), (
        "the deliberate NEW_TAB claim was not discarded at the creation seam"
    )

    # A durable download credit arrives while the deliberate tab is still open.
    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context
    agent = ForgeAgent()
    with patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx):
        await agent._close_credited_download_popups(task, credit_browser_state)

    assert not new_tab.is_closed(), "a deliberately-created NEW_TAB must not be closed by download credit"
    assert not opener.is_closed(), "the opener must remain open"


# --- Terminal-cleanup listener timing (SKY-15371): keep the owner armed through settle/finalize ------
# clean_up_task must NOT take/detach the task context.on("page") owner at entry: a late marker that joins
# the context during the final screenshot / settle / finalization awaits would be missed even when
# finalization then grants durable credit. The owner stays armed until the durable-credit result is known,
# then strong claims + late candidates are taken exactly once and closed (credit) or expired (no credit).


class _SettleCM:
    """Async context manager standing in for settle_browser_downloads_for_context; runs a hook on enter to
    model a same-context child joining the context while cleanup is awaiting settle."""

    def __init__(self, on_enter: Any) -> None:
        self._on_enter = on_enter

    async def __aenter__(self) -> None:
        self._on_enter()

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _cleanup_patches(
    stack: ExitStack, ctx: SkyvernContext, task: Any, browser_state: Any, on_settle: Any, finalize_result: Any
) -> None:
    app_mock = MagicMock()
    app_mock.DATABASE.tasks.get_task = AsyncMock(return_value=task)
    app_mock.STORAGE.save_downloaded_files = AsyncMock(return_value=None)
    app_mock.BROWSER_MANAGER.get_for_task.return_value = browser_state
    stack.enter_context(patch(f"{_AGENT}.app", app_mock))
    stack.enter_context(patch(f"{_AGENT}.skyvern_context.current", return_value=ctx))
    stack.enter_context(patch(f"{_AGENT}.analytics.capture", MagicMock()))
    stack.enter_context(patch(f"{_AGENT}.has_download_interceptor_for_context", MagicMock(return_value=False)))
    stack.enter_context(patch(f"{_AGENT}.resolve_run_download_id", MagicMock(return_value="run-cleanup")))
    stack.enter_context(patch(f"{_AGENT}.drain_speculative_persist_tasks", AsyncMock()))
    stack.enter_context(
        patch(f"{_AGENT}.settle_browser_downloads_for_context", MagicMock(return_value=_SettleCM(on_settle)))
    )
    stack.enter_context(
        patch.object(ForgeAgent, "_finalize_downloaded_files_for_task", AsyncMock(return_value=finalize_result))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("late_url", [":", "about:blank", "", "http://origin/receipt"])
async def test_late_page_during_settle_is_closed_by_terminal_credit(tmp_path: Path, late_url: str) -> None:
    task, step, _scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)

    # Arm the owner exactly as the false-click download seam does; initial late-candidate set is empty.
    ctx.arm_download_popup_context_listener(
        task.task_id,
        context,
        lambda child: ctx.record_download_popup_late_candidate(task.task_id, child) if child is not opener else None,
    )
    assert ctx.download_popup_late_candidates.get(task.task_id, []) == []

    late_blank = _FakePage(context, late_url, events, "late")

    def _on_settle() -> None:
        context.pages.append(late_blank)
        context.emit_page(late_blank)
        assert not late_blank.is_closed(), "settle has not yet proved durable credit"

    browser_state = MagicMock()
    browser_state.browser_context = context

    with ExitStack() as stack:
        _cleanup_patches(stack, ctx, task, browser_state, _on_settle, finalize_result=["stmt.pdf"])
        await ForgeAgent().clean_up_task(
            task, step, need_final_screenshot=False, download_suffix="stmt", list_files_before=[]
        )

    assert late_blank.is_closed(), "a late marker joining during settle must be closed once terminal credit lands"
    assert not opener.is_closed(), "the opener must remain open"
    assert context.page_listener_count() == 0, "the context owner must be detached after terminal cleanup"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_url", [":", "about:blank", "", "http://origin/receipt"])
async def test_terminal_no_credit_expires_owner_without_close(tmp_path: Path, late_url: str) -> None:
    task, step, _scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    ctx.arm_download_popup_context_listener(
        task.task_id,
        context,
        lambda child: ctx.record_download_popup_late_candidate(task.task_id, child) if child is not opener else None,
    )
    late_blank = _FakePage(context, late_url, events, "late")
    claimed = _FakePage(context, late_url, events, "claimed")
    ctx.record_download_popup_claim(task.task_id, claimed)

    browser_state = MagicMock()
    browser_state.browser_context = context

    with ExitStack() as stack:
        # finalize proves NO durable credit (empty list).
        _cleanup_patches(stack, ctx, task, browser_state, lambda: context.emit_page(late_blank), finalize_result=[])
        await ForgeAgent().clean_up_task(
            task, step, need_final_screenshot=False, download_suffix="stmt", list_files_before=[]
        )

    assert not late_blank.is_closed(), "no durable credit => no destructive close"
    assert not claimed.is_closed()
    assert context.page_listener_count() == 0, "the owner must still be detached/expired on the no-credit path"
    assert ctx.download_popup_late_candidates.get(task.task_id) is None, "late-candidate state must be expired"
    assert ctx.download_popup_claims == {}
    assert ctx.download_popup_context_listeners == {}


@pytest.mark.asyncio
async def test_terminal_db_refresh_failure_still_expires_owner(tmp_path: Path) -> None:
    from skyvern.exceptions import TaskNotFound

    task, step, _scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    ctx.arm_download_popup_context_listener(
        task.task_id,
        context,
        lambda child: ctx.record_download_popup_late_candidate(task.task_id, child) if child is not opener else None,
    )

    app_mock = MagicMock()
    app_mock.DATABASE.tasks.get_task = AsyncMock(side_effect=RuntimeError("db down"))
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_AGENT}.app", app_mock))
        stack.enter_context(patch(f"{_AGENT}.skyvern_context.current", return_value=ctx))
        with pytest.raises(TaskNotFound):
            await ForgeAgent().clean_up_task(task, step, need_final_screenshot=False)

    assert context.page_listener_count() == 0, "a DB refresh failure must not leave the owner armed"


@pytest.mark.asyncio
async def test_terminal_settle_exception_expires_owner_without_close(tmp_path: Path) -> None:
    task, step, _scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    ctx.arm_download_popup_context_listener(
        task.task_id,
        context,
        lambda child: ctx.record_download_popup_late_candidate(task.task_id, child) if child is not opener else None,
    )
    late_blank = _FakePage(context, ":", events, "late")
    ctx.record_download_popup_late_candidate(task.task_id, late_blank)

    browser_state = MagicMock()
    browser_state.browser_context = context

    def _boom_settle() -> None:
        raise RuntimeError("settle blew up")

    with ExitStack() as stack:
        _cleanup_patches(stack, ctx, task, browser_state, _boom_settle, finalize_result=["stmt.pdf"])
        # The save block catches its own exceptions; cleanup completes without raising.
        await ForgeAgent().clean_up_task(
            task, step, need_final_screenshot=False, download_suffix="stmt", list_files_before=[]
        )

    assert not late_blank.is_closed(), "a settle failure (no proven credit) must not destructively close a marker"
    assert context.page_listener_count() == 0, "a settle failure must still detach/expire the owner"


# --- Intentional-page ownership exclusion (SKY-15371 PR A) -------------------------------------------
# The context.on("page") owner records EVERY newly-created same-context blank Page, including deliberately
# created tabs (NEW_TAB, magic link) that stay blank until a later navigation. A delayed download credit
# could close such an intentional tab while it is still blank. Each creation seam must retire the exact new
# Page from both registries the instant new_page() returns -- before any await that could let credit fire.


def test_retire_intentional_page_exact_identity_only() -> None:
    ctx = SkyvernContext(task_id="t-int")
    a = object()
    b = object()
    # A genuine same-task blank download late candidate (a) and an intentional page (b).
    ctx.record_download_popup_late_candidate("t-int", a)
    ctx.record_download_popup_late_candidate("t-int", b)
    ctx.record_download_popup_claim("t-int", b)  # b also happened to be a strong claim
    ctx.record_download_popup_claim("other", a)  # a sibling task bucket must be preserved

    ctx.retire_intentional_page("t-int", b)

    assert ctx.download_popup_late_candidates.get("t-int") == [a], "only the intentional page is retired"
    assert "t-int" not in ctx.download_popup_claims, "b was the only strong claim for the task -> bucket dropped"
    assert ctx.download_popup_claims.get("other") == [a], "sibling task bucket preserved"


def test_retire_intentional_page_idempotent_when_unrecorded() -> None:
    ctx = SkyvernContext(task_id="t-int2")
    never_recorded = object()
    # Must not raise and must not create/mutate buckets when the listener never recorded the page.
    ctx.retire_intentional_page("t-int2", never_recorded)
    assert ctx.download_popup_late_candidates.get("t-int2") is None
    assert ctx.download_popup_claims.get("t-int2") is None


@pytest.mark.asyncio
async def test_new_tab_retired_before_navigation_survives_delayed_credit(tmp_path: Path) -> None:
    from skyvern.webeye.actions.actions import NewTabAction
    from skyvern.webeye.actions.handler import handle_new_tab_action

    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    ctx.arm_download_popup_context_listener(
        task.task_id,
        context,
        lambda child: ctx.record_download_popup_late_candidate(task.task_id, child) if child is not opener else None,
    )

    new_tab = _FakePage(context, ":", events, "newtab")  # deliberately-created tab, still blank
    browser_state = MagicMock()

    async def _new_page() -> Any:
        context.pages.append(new_tab)
        context.emit_page(new_tab)  # production: new_page() joins the context, firing context.on("page")
        return new_tab

    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context

    async def _navigate(*_a: Any, **_k: Any) -> None:
        # A durable download credit fires WHILE navigation is still awaiting.
        await ForgeAgent()._close_credited_download_popups(task, credit_browser_state)

    browser_state.new_page = _new_page
    browser_state.navigate_to_url = _navigate
    browser_state.set_active_page = AsyncMock()

    app_mock = MagicMock()
    app_mock.BROWSER_MANAGER.get_for_task.return_value = browser_state

    with (
        patch(f"{_HANDLER}.app", app_mock),
        patch(f"{_HANDLER}.validate_fetch_url", MagicMock(return_value="http://origin/new")),
        patch(f"{_HANDLER}.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
    ):
        result = await handle_new_tab_action(NewTabAction(url="http://origin/new"), opener, scraped_page, task, step)

    assert isinstance(result[-1], ActionSuccess)
    assert not new_tab.is_closed(), "a NEW_TAB retired before navigation must survive a credit firing during nav"


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["late", "claim", "both"])
@pytest.mark.parametrize("credit_during_activation", [True, False], ids=["activation", "after-return"])
@pytest.mark.parametrize("through_dispatcher", [False, True], ids=["handler-first-tab", "dispatcher-last-tab"])
async def test_switch_tab_target_survives_delayed_credit(
    ownership: str, credit_during_activation: bool, through_dispatcher: bool
) -> None:
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    unrelated = _FakePage(context, "http://origin/doc", events, "unrelated")
    target = _FakePage(context, "http://origin/doc", events, "target")
    late_sibling = _FakePage(context, "http://origin/doc", events, "late_sibling")
    claimed_sibling = _FakePage(context, "http://origin/doc", events, "claimed_sibling")
    context.pages.extend([opener, unrelated, late_sibling, claimed_sibling])
    context.pages.insert(len(context.pages) if through_dispatcher else 0, target)
    ctx.arm_download_popup_context_listener(
        task.task_id, context, lambda child: ctx.record_download_popup_late_candidate(task.task_id, child)
    )
    if ownership in ("late", "both"):
        context.emit_page(target)
    if ownership in ("claim", "both"):
        ctx.record_download_popup_claim(task.task_id, target)
    context.emit_page(late_sibling)
    ctx.record_download_popup_claim(task.task_id, claimed_sibling)
    ctx.record_download_popup_claim("other-task", unrelated)
    ctx.record_download_popup_late_candidate("other-task", unrelated)

    action = SwitchTabAction(tab_index=len(context.pages) - 1 if through_dispatcher else 0)
    app_mock = _app_mock(action)
    app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock()
    browser_state = app_mock.BROWSER_MANAGER.get_for_task.return_value
    browser_state.browser_context = context
    browser_state.list_valid_pages = AsyncMock(return_value=list(context.pages))
    active_pages: list[_FakePage] = []
    agent = ForgeAgent()

    async def activate(selected: _FakePage) -> None:
        assert selected is target
        # The dispatcher stops old capture; adopting a target alone must leave capture untouched.
        assert context.page_listener_count() == (0 if through_dispatcher else 1)
        assert not any(p.is_closed() for p in context.pages)
        active_pages.append(selected)
        if credit_during_activation:
            await agent._close_credited_download_popups(task, browser_state)

    browser_state.set_active_page = activate
    with (
        patch(f"{_HANDLER}.app", app_mock),
        patch(f"{_HANDLER}.skyvern_context.current", return_value=ctx),
    ):
        if through_dispatcher:
            results = await ActionHandler.handle_action(scraped_page, task, step, opener, action)
        else:
            results = await handle_switch_tab_action(action, opener, scraped_page, task, step)
        assert isinstance(results[-1], ActionSuccess)
        assert results[-1].skip_remaining_actions
        if not credit_during_activation:
            assert not any(p.is_closed() for p in context.pages)
            await agent._close_credited_download_popups(task, browser_state)

    assert active_pages == [target]
    assert not target.is_closed(), "the selected working tab must survive delayed download credit"
    assert late_sibling.close_terminal, "adopting a target must preserve genuine sibling reservations"
    assert claimed_sibling.close_terminal
    assert not opener.is_closed()
    assert not unrelated.is_closed(), "same-URL and other-task pages must survive"
    assert ctx.download_popup_claims == {"other-task": [unrelated]}
    assert ctx.download_popup_late_candidates == {"other-task": [unrelated]}


@pytest.mark.asyncio
async def test_magic_link_page_retired_survives_delayed_credit() -> None:
    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    ctx.arm_download_popup_context_listener(
        task.task_id,
        context,
        lambda child: ctx.record_download_popup_late_candidate(task.task_id, child) if child is not opener else None,
    )

    magic_page = _FakePage(context, ":", events, "magic")  # blank until the returned GotoUrlAction navigates it
    browser_state = MagicMock()

    async def _new_page() -> Any:
        context.pages.append(magic_page)
        context.emit_page(magic_page)
        return magic_page

    browser_state.new_page = _new_page

    from skyvern.forge.sdk.schemas.totp_codes import OTPType

    otp = MagicMock()
    otp.get_otp_type.return_value = OTPType.MAGIC_LINK
    otp.value = "https://magic.example/verify"

    agent = ForgeAgent()
    with (
        patch("skyvern.forge.agent.poll_otp_value", AsyncMock(return_value=otp)),
        patch("skyvern.forge.agent.skyvern_context.ensure_context", return_value=ctx),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
    ):
        actions = await agent.handle_potential_magic_link(
            task, step, scraped_page, browser_state, {"should_verify_by_magic_link": True}
        )
        # A durable download credit fires after the tab was created but before the returned navigation runs.
        credit_browser_state = MagicMock()
        credit_browser_state.browser_context = context
        await agent._close_credited_download_popups(task, credit_browser_state)

    assert actions and actions[0].is_magic_link, "magic-link flow must still return the navigation action"
    assert not magic_page.is_closed(), "a retired magic-link tab must not be closed by a delayed download credit"


# --- Recovered working page is intentional/persistent (SKY-15371 PR A) -------------------------------
# _recover_download_page recreates the working page (direct new_page path) while the download context.on("page")
# owner can still be armed, so the replacement joins the context and is recorded as a blank late candidate. A
# delayed download credit firing during the recovery navigation must not close this deliberate page. (The
# reconnect path builds the page on a fresh context with no owner armed, so it is never recorded -- no test
# theater is invented for it.)


@pytest.mark.asyncio
async def test_recovered_working_page_survives_delayed_credit_direct_new_page(tmp_path: Path) -> None:
    from skyvern.webeye.actions.handler import _recover_download_page

    task, step, scraped_page, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    ctx.arm_download_popup_context_listener(
        task.task_id,
        context,
        lambda child: ctx.record_download_popup_late_candidate(task.task_id, child) if child is not opener else None,
    )

    # A genuine same-task blank download late candidate that MUST still close on credit.
    sibling_dl = _FakePage(context, ":", events, "sibling_dl")
    context.pages.append(sibling_dl)
    ctx.record_download_popup_late_candidate(task.task_id, sibling_dl)

    replacement = _FakePage(context, ":", events, "replacement")  # blank until recovery navigation commits it
    browser_state = MagicMock()

    async def _new_page() -> Any:
        context.pages.append(replacement)
        context.emit_page(replacement)  # the replacement joins the initiating context (recorded by the owner)
        return replacement

    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context

    async def _navigate(*_a: Any, **_k: Any) -> None:
        # A durable download credit fires WHILE the recovery navigation is still awaiting.
        await ForgeAgent()._close_credited_download_popups(task, credit_browser_state)

    browser_state.new_page = _new_page
    browser_state.navigate_to_url = _navigate
    browser_state.set_active_page = AsyncMock()

    with (
        patch(f"{_HANDLER}.skyvern_context.current", return_value=ctx),
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
    ):
        result = await _recover_download_page(
            browser_state, task, page_url_before_download="http://origin/", timeout_seconds=5.0, recovery_site="test"
        )

    assert result is replacement, "recovery must return the replacement working page"
    assert not replacement.is_closed(), "the deliberate replacement working page must survive a delayed credit"
    assert sibling_dl.is_closed(), "a genuine same-task blank download late candidate must still close on credit"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_url", [":", "about:blank", "", "http://origin/receipt"])
async def test_late_and_strong_candidates_close_once_after_credit_regardless_of_url(late_url: str) -> None:
    task, step, _scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    late = _FakePage(context, late_url, events, "late")
    strong = _FakePage(context, late_url, events, "strong")
    foreign = _FakePage(_FakeContext(), late_url, events, "foreign")
    context.pages.extend([opener, late, strong])
    for page in (late, strong, foreign):
        ctx.record_download_popup_late_candidate(task.task_id, page)
    ctx.record_download_popup_claim(task.task_id, strong)
    ctx.record_download_popup_claim(task.task_id, foreign)

    credit_browser_state = MagicMock()
    credit_browser_state.browser_context = context
    agent = ForgeAgent()
    with (
        patch("skyvern.forge.agent.skyvern_context.current", return_value=ctx),
        patch.object(strong, "is_closed", return_value=False),
    ):
        await agent._close_credited_download_popups(task, credit_browser_state)

    assert late.is_closed(), "durable credit closes an owned late candidate regardless of its URL"
    assert strong.is_closed()
    assert events.count("late:close_start") == 1
    assert events.count("strong:close_start") == 1
    assert not foreign.is_closed()
    assert not opener.is_closed()
    assert ctx.download_popup_claims == {}
    assert ctx.download_popup_late_candidates == {}


@pytest.mark.asyncio
async def test_late_candidates_share_claim_close_deadline() -> None:
    task, _step, _scraped, ctx, events = _make_env()
    context = _FakeContext()
    claimed = _FakePage(context, ":", events, "claimed", close_hangs=True)
    late = _FakePage(context, "http://origin/receipt", events, "late")
    ctx.record_download_popup_claim(task.task_id, claimed)
    ctx.record_download_popup_late_candidate(task.task_id, late)
    browser_state = MagicMock(browser_context=context)

    with (
        patch(f"{_AGENT}.skyvern_context.current", return_value=ctx),
        patch(f"{_AGENT}.BROWSER_PAGE_CLOSE_TIMEOUT", 0.05),
    ):
        await asyncio.wait_for(ForgeAgent()._close_credited_download_popups(task, browser_state), timeout=1)

    assert "claimed:close_hang" in events
    assert "late:close_start" not in events, "the hung claim spent the shared budget"
    assert ctx.download_popup_claims == {}
    assert ctx.download_popup_late_candidates == {}


def test_cleanup_outgoing_context_detaches_all_listeners_and_clears_state() -> None:
    ctx = SkyvernContext(task_id="t-out")
    context = _FakeContext()
    ctx.arm_download_popup_context_listener("t-out", context, lambda _p: None)
    ctx.record_download_popup_claim("t-out", object())
    ctx.record_download_popup_late_candidate("t-out", object())
    assert context.page_listener_count() == 1

    # The exact hook reset/replace/_restore invoke on the outgoing context.
    _skvctx._cleanup_outgoing_context(ctx)

    assert context.page_listener_count() == 0, "outgoing-context teardown must detach the context.on('page') owner"
    assert ctx.download_popup_context_listeners == {}
    assert ctx.download_popup_claims == {}
    assert ctx.download_popup_late_candidates == {}


def test_scoped_child_cleanup_does_not_detach_main_task_listener() -> None:
    main_ctx = SkyvernContext(task_id="main")
    context = _FakeContext()
    main_ctx.arm_download_popup_context_listener("main", context, lambda _p: None)
    token = _skvctx._context.set(main_ctx)  # raw set so no cleanup is triggered on the main context
    try:
        child = SkyvernContext(task_id="child")
        with _skvctx.scoped(child):
            pass  # on exit, _restore cleans the CHILD context (empty buckets), not main
        assert context.page_listener_count() == 1, "scoped child teardown must not detach the main task's listener"
        assert "main" in main_ctx.download_popup_context_listeners
    finally:
        _skvctx._context.reset(token)


def test_detach_all_swallows_remove_listener_error_and_clears_rest() -> None:
    ctx = SkyvernContext(task_id="t1")

    class _Boom:
        def on(self, *_a: Any) -> None:
            pass

        def remove_listener(self, *_a: Any) -> None:
            raise RuntimeError("context already gone")

    good = _FakeContext()
    ctx.arm_download_popup_context_listener("t1", _Boom(), lambda _p: None)
    ctx.arm_download_popup_context_listener("t2", good, lambda _p: None)
    ctx.record_download_popup_claim("t1", object())
    ctx.record_download_popup_late_candidate("t2", object())

    ctx.detach_all_download_popup_context_listeners()  # the raising listener must not abort the rest

    assert good.page_listener_count() == 0, "a remove_listener failure on one listener must not skip the others"
    assert ctx.download_popup_context_listeners == {}
    assert ctx.download_popup_claims == {}
    assert ctx.download_popup_late_candidates == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("adopt", ["none", "strong", "late"])
@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
async def test_next_action_stops_capture_but_preserves_prior_reservations(
    tmp_path: Path, adopt: str, outcome: str
) -> None:
    task, step, scraped, ctx, events = _make_env(download_timeout=0.01)
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    first = ClickAction(element_id="download", download=False)
    strong = _FakePage(context, "http://origin/viewer", events, "strong")
    candidate = _FakePage(context, "about:blank", events, "candidate")
    later_popup = _FakePage(context, "http://origin/auth", events, "auth")

    async def first_inner(**_kwargs: Any) -> list[ActionSuccess]:
        context.pages.append(strong)
        context.emit_page(strong)
        for callback in list(opener._popup_cbs):
            callback(strong)
        return [ActionSuccess()]

    with ExitStack() as stack:
        _enter_patches(stack, _app_mock(first), ctx, tmp_path, first_inner)
        await ActionHandler.handle_action(scraped, task, step, opener, first, file_download_false_click_eligible=True)
        context.pages.append(candidate)
        context.emit_page(candidate)
        assert ctx.download_popup_claims[task.task_id] == [strong]
        assert ctx.download_popup_late_candidates[task.task_id] == [strong, candidate]

        async def later_inner(**_kwargs: Any) -> list[ActionSuccess | ActionFailure]:
            context.pages.append(later_popup)
            context.emit_page(later_popup)
            if outcome == "exception":
                raise RuntimeError("unrelated action failed")
            if outcome == "failure":
                return [ActionFailure(Exception("unrelated action failed"))]
            return [ActionSuccess()]

        stack.enter_context(patch.object(ActionHandler, "_handle_action", side_effect=later_inner))
        later = InputTextAction(element_id="verification", text="test")
        if outcome == "exception":
            stack.enter_context(pytest.raises(RuntimeError, match="unrelated action failed"))
        await ActionHandler.handle_action(
            scraped,
            task,
            step,
            {"none": opener, "strong": strong, "late": candidate}[adopt],
            later,
            file_download_false_click_eligible=True,
        )

    assert not strong.is_closed()
    assert not candidate.is_closed()
    with patch(f"{_AGENT}.skyvern_context.current", return_value=ctx):
        await ForgeAgent()._close_credited_download_popups(task, MagicMock(browser_context=context))
    assert strong.is_closed() is (adopt != "strong")
    assert candidate.is_closed() is (adopt != "late")
    assert not later_popup.is_closed()
    assert not opener.is_closed()
    assert context.page_listener_count() == 0
    assert ctx.download_popup_claims == ctx.download_popup_late_candidates == {}


@pytest.mark.parametrize("teardown", ["clear", "context"])
def test_detached_callback_cannot_recreate_reservations(teardown: str) -> None:
    ctx = SkyvernContext(task_id="task")
    context = _FakeContext()
    ctx.arm_download_popup_context_listener(
        "task", context, lambda page: ctx.record_download_popup_late_candidate("task", page)
    )
    queued_callback = context._page_cbs[0]
    context.remove_listener = MagicMock(side_effect=RuntimeError("context recycled"))
    if teardown == "clear":
        ctx.clear_download_popup_claims("task")
    else:
        ctx.detach_all_download_popup_context_listeners()
    queued_callback(object())
    assert ctx.download_popup_late_candidates == {}
    assert ctx.download_popup_context_listeners == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "cancel", "result", "persist"])
@pytest.mark.parametrize("dispatched", [True, False])
@pytest.mark.parametrize("prior_reservations", [False, True])
async def test_failed_action_reservation_release_is_keyed_on_browser_dispatch(
    tmp_path: Path, failure: str, dispatched: bool, prior_reservations: bool
) -> None:
    """A failed action's download reservations are decided by the dispatch boundary, not result status.

    A dispatched-then-failed epoch keeps its action-owned reservation so the task's durable-credit seam
    can still close the marker popup (the SKY-15371 dispatch-boundary fix); a no-dispatch failed exit
    (stale abort, already-in-desired-state suppression, pre-handler error) releases its epoch as before.
    Cancellation is the exception: it re-raises through the step loop with no next action and no
    clean_up_task, so the epoch detaches its listener and releases its reservation at the seam even when
    it was dispatched. In every case prior-epoch reservations survive.
    """
    task, step, scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    candidate = _FakePage(context, "about:blank", events, "candidate")
    prior_claim = _FakePage(context, "about:blank", events, "prior-claim")
    prior_late = _FakePage(context, "about:blank", events, "prior-late")
    if prior_reservations:
        ctx.record_download_popup_claim(task.task_id, prior_claim)
        ctx.record_download_popup_late_candidate(task.task_id, prior_late)
    action = ClickAction(element_id="download", download=False)
    app_mock = _app_mock(action)

    async def inner(**_kwargs: Any) -> list[Any]:
        # Mirror the real type-handler seam: a committed browser dispatch sets the boundary flag; a
        # no-dispatch exit (stale abort / desired-state suppression) leaves it False.
        _browser_dispatch_committed.set(dispatched)
        context.emit_page(candidate)
        for callback in list(opener._popup_cbs):
            callback(candidate)
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "exception":
            raise RuntimeError("action failed")
        if failure == "result":
            return [ActionFailure(Exception("action failed"))]
        return [ActionSuccess()]

    if failure == "persist":
        app_mock.DATABASE.workflow_params.create_action = AsyncMock(side_effect=RuntimeError("persistence failed"))
    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        if failure != "result":
            stack.enter_context(pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError))
        await ActionHandler.handle_action(scraped, task, step, opener, action, file_download_false_click_eligible=True)

    assert not candidate.is_closed()
    # A dispatched-then-failed epoch keeps its context owner armed to the next action's boundary (as a
    # successful dispatch does), so an after-return child is still observed; a no-dispatch exit stops it
    # now. Cancellation keeps nothing armed: it has no next-action boundary to invalidate capture at.
    keeps_armed = dispatched and failure != "cancel"
    assert context.page_listener_count() == (1 if keeps_armed else 0)
    # The dispatched-then-failed epoch retains its action-owned candidate until durable credit; the
    # no-dispatch epoch has already released it, and cancellation releases its epoch at the seam.
    assert ctx.has_download_popup_claim(task.task_id, candidate) is keeps_armed

    with patch(f"{_AGENT}.skyvern_context.current", return_value=ctx):
        await ForgeAgent()._close_credited_download_popups(task, MagicMock(browser_context=context))
    # Durable credit closes exactly the reservations still held: the retained candidate, plus any
    # prior-epoch reservations, which always survive an intervening action's failure.
    assert candidate.is_closed() is keeps_armed
    assert prior_claim.is_closed() is prior_reservations
    assert prior_late.is_closed() is prior_reservations
    assert not opener.is_closed()
    assert ctx.download_popup_claims == ctx.download_popup_late_candidates == {}


@pytest.mark.asyncio
async def test_dispatched_cancellation_detaches_listener_and_releases_epoch(tmp_path: Path) -> None:
    """A dispatched action cancelled mid-flight (elapsed-time timeout / user cancel) re-raises through the
    step loop's ``except CancelledError``, which -- unlike every failure handler -- re-raises without
    running clean_up_task. There is therefore no next action and no teardown to invalidate capture at a
    later boundary, so the epoch must detach its ``context.on("page")`` owner and release its own
    reservation right at the cancellation seam. Holding the listener armed (the ordinary failed-dispatch
    behavior) would strand it on the live BrowserContext, where a page that joins during teardown would be
    recorded by the orphaned callback -- recreating a reservation the run can never credit or close."""
    task, step, scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    prior_claim = _FakePage(context, "about:blank", events, "prior-claim")
    ctx.record_download_popup_claim(task.task_id, prior_claim)
    candidate = _FakePage(context, "about:blank", events, "candidate")
    action = ClickAction(element_id="download", download=False)
    app_mock = _app_mock(action)

    async def inner(**_kwargs: Any) -> list[Any]:
        # Committed browser dispatch; a same-context child joins during the armed seam and is recorded by
        # the context owner, then the action is cancelled before it can return.
        _browser_dispatch_committed.set(True)
        context.emit_page(candidate)
        raise asyncio.CancelledError()

    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        with pytest.raises(asyncio.CancelledError):
            await ActionHandler.handle_action(
                scraped, task, step, opener, action, file_download_false_click_eligible=True
            )

    assert context.page_listener_count() == 0, "cancellation left the context owner armed with no boundary to stop it"
    assert not ctx.has_download_popup_claim(task.task_id, candidate), "cancellation must release its own epoch"
    assert ctx.has_download_popup_claim(task.task_id, prior_claim), (
        "a prior-epoch reservation must survive cancellation"
    )
    assert not candidate.is_closed(), "releasing the reservation frees the child; the cancel seam does not close it"

    # The detached callback cannot resurrect a reservation for a page that joins during teardown.
    late_during_teardown = _FakePage(context, "about:blank", events, "late-teardown")
    context.emit_page(late_during_teardown)
    assert not ctx.has_download_popup_claim(task.task_id, late_during_teardown), (
        "an orphaned callback recorded a page after cancellation detached capture"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["stale_skipped", "desired_completed"])
async def test_no_dispatch_exit_releases_epoch_and_stops_listener(tmp_path: Path, exit_kind: str) -> None:
    """A true no-dispatch exit (stale abort -> skipped, already-desired suppression -> completed) never
    owned a download: at wrapper exit its epoch reservations are released and the context listener is
    stopped, so a later durable credit cannot close a page it spuriously captured; prior reservations
    opened before this action survive untouched."""
    task, step, scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    during_seam = _FakePage(context, "about:blank", events, "during-seam")
    prior_claim = _FakePage(context, "about:blank", events, "prior-claim")
    ctx.record_download_popup_claim(task.task_id, prior_claim)
    action = ClickAction(element_id="toggle", download=False)
    app_mock = _app_mock(action)

    async def inner(**_kwargs: Any) -> list[Any]:
        # No browser dispatch commits: a stale abort returns before the type-handler seam, and a
        # desired-state suppression sets the flag back to False. A same-context page still appears
        # during the armed seam and is captured as a current-epoch candidate.
        _browser_dispatch_committed.set(False)
        context.emit_page(during_seam)
        for callback in list(opener._popup_cbs):
            callback(during_seam)
        if exit_kind == "stale_skipped":
            result = StaleActionAbort()
            result.skip_remaining_actions = True
            return [result]
        return [ActionAbort(desired_state_reached=True)]

    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        await ActionHandler.handle_action(scraped, task, step, opener, action, file_download_false_click_eligible=True)

    assert context.page_listener_count() == 0, "no-dispatch exit must stop the armed context listener"
    assert not ctx.has_download_popup_claim(task.task_id, during_seam), "no-dispatch epoch must be released"
    assert ctx.has_download_popup_claim(task.task_id, prior_claim), "prior-epoch reservation must survive"
    # A later durable credit closes only the surviving prior reservation, never the spurious capture.
    with patch(f"{_AGENT}.skyvern_context.current", return_value=ctx):
        await ForgeAgent()._close_credited_download_popups(task, MagicMock(browser_context=context))
    assert not during_seam.is_closed(), "a later credit must not close a page from a no-dispatch epoch"
    assert prior_claim.is_closed(), "the surviving prior reservation is closed by durable credit"


@pytest.mark.asyncio
async def test_dispatched_failure_without_credit_releases_at_step_seam(tmp_path: Path) -> None:
    """A dispatched-then-failed marker stays reserved between the failure and the step's credit seam --
    so #17139 blank recovery cannot close it mid-flight -- and is released the instant the seam finds no
    credit, restoring recovery eligibility before the retry scrape."""
    task, step, scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    marker = _FakePage(context, "about:blank", events, "marker")
    action = ClickAction(element_id="download", download=False)
    app_mock = _app_mock(action)

    async def inner(**_kwargs: Any) -> list[Any]:
        _browser_dispatch_committed.set(True)
        context.emit_page(marker)
        for callback in list(opener._popup_cbs):
            callback(marker)
        return [ActionFailure(Exception("dispatched then failed"))]

    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        await ActionHandler.handle_action(scraped, task, step, opener, action, file_download_false_click_eligible=True)

    # Between failure and the credit seam the marker is still reserved: the #17139 guard refuses to close it.
    assert ctx.has_download_popup_claim(task.task_id, marker), "dispatched-failed marker must stay reserved pre-seam"
    # The step's complete-on-download seam runs and finds no durable credit: release the deferred epoch now.
    ctx.apply_pending_download_reservation_release(task.task_id)
    assert not ctx.has_download_popup_claim(task.task_id, marker), "uncredited marker must release at the seam"
    assert not marker.is_closed(), "release frees the marker for blank recovery; it does not close it"


@pytest.mark.asyncio
async def test_dispatched_failure_late_child_after_return_is_captured_and_closed_by_credit(tmp_path: Path) -> None:
    """A dispatched action can fail for a reason unrelated to its click (a challenge-solver error or a
    post-click timeout) while the download popup it opened joins the BrowserContext only AFTER
    ``handle_action`` returns -- before the next action begins. The failure path must keep the
    ``context.on("page")`` owner armed to that next-action boundary exactly as the success path does, so
    the late child is recorded and closed by durable credit. Detaching capture at the failed epoch's own
    exit reopens the after-return/before-next-action race this PR exists to close."""
    task, step, scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    action = ClickAction(element_id="download", download=False)
    app_mock = _app_mock(action)

    async def inner(**_kwargs: Any) -> list[Any]:
        # Committed browser dispatch, then an unrelated failure. Nothing joins the context during the
        # seam: the popup arrives only after handle_action has already returned.
        _browser_dispatch_committed.set(True)
        return [ActionFailure(Exception("challenge solver failed after the click was dispatched"))]

    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, inner)
        await ActionHandler.handle_action(scraped, task, step, opener, action, file_download_false_click_eligible=True)

    # Symmetric with the success path: a failed dispatch keeps its context owner armed past return so an
    # after-return child is still observed before the next action's invalidation boundary.
    assert context.page_listener_count() == 1, (
        "a failed dispatch detached the context owner at its own exit; a popup joining before the next action is missed"
    )
    late_child = _FakePage(context, "about:blank", events, "late")
    context.pages.append(late_child)
    context.emit_page(late_child)
    assert ctx.has_download_popup_claim(task.task_id, late_child), (
        "the after-return child of a failed dispatch was not captured"
    )
    assert not late_child.is_closed(), "discovery alone must not close a page before durable credit"

    with patch(f"{_AGENT}.skyvern_context.current", return_value=ctx):
        await ForgeAgent()._close_credited_download_popups(task, MagicMock(browser_context=context))
    assert late_child.is_closed(), "durable credit must close the exact after-return child"
    assert not opener.is_closed()
    assert context.page_listener_count() == 0
    assert ctx.download_popup_claims == ctx.download_popup_late_candidates == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("second_outcome", ["success", "failure", "no_dispatch"])
@pytest.mark.parametrize("after_no_credit_seam", [False, True])
async def test_dispatched_failure_reservations_survive_a_later_same_batch_action(
    tmp_path: Path, second_outcome: str, after_no_credit_seam: bool
) -> None:
    """A dispatched-then-failed action's owned reservations must survive a later action in the SAME
    batch (the batch continues on ``stop_execution_on_failure=False`` or the duplicate-element fallback)
    so durable credit can still close the marker. The deferred release is applied only at the step's
    complete-on-download no-credit seam -- never at the next action's entry -- while that next action's
    entry still stops the previous capture listener."""
    task, step, scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    strong = _FakePage(context, "about:blank", events, "strong")
    late = _FakePage(context, "about:blank", events, "late")
    action1 = ClickAction(element_id="download", download=False)
    app_mock = _app_mock(action1)

    async def first_inner(**_kwargs: Any) -> list[Any]:
        _browser_dispatch_committed.set(True)
        for callback in list(opener._popup_cbs):
            callback(strong)
        context.emit_page(late)
        return [ActionFailure(Exception("dispatched then failed"))]

    with ExitStack() as stack:
        _enter_patches(stack, app_mock, ctx, tmp_path, first_inner)
        await ActionHandler.handle_action(scraped, task, step, opener, action1, file_download_false_click_eligible=True)
        assert ctx.has_download_popup_claim(task.task_id, strong), "strong claim must survive the failure"
        assert ctx.has_download_popup_claim(task.task_id, late), "late candidate must survive the failure"
        assert context.page_listener_count() == 1, (
            "the failed epoch's listener stays armed until the next action's entry (symmetric with success)"
        )

        # A second action in the same batch runs BEFORE any credit seam. It must not release action1's
        # owned reservations at its entry.
        later_page = _FakePage(context, "http://origin/next", events, "later")

        async def second_inner(**_kwargs: Any) -> list[Any]:
            _browser_dispatch_committed.set(second_outcome != "no_dispatch")
            context.emit_page(later_page)
            if second_outcome == "failure":
                return [ActionFailure(Exception("second action failed"))]
            if second_outcome == "no_dispatch":
                return [ActionAbort(desired_state_reached=True)]
            return [ActionSuccess()]

        stack.enter_context(patch.object(ActionHandler, "_handle_action", side_effect=second_inner))
        second_action = ClickAction(element_id="fallback", download=False)
        await ActionHandler.handle_action(
            scraped, task, step, opener, second_action, file_download_false_click_eligible=True
        )

    # action1's owned reservations still survive after the second action ran.
    assert ctx.has_download_popup_claim(task.task_id, strong), "the next action must not release action1's strong claim"
    assert ctx.has_download_popup_claim(task.task_id, late), "the next action must not release action1's late candidate"
    assert (task.task_id in ctx.pending_download_reservation_release) is (second_outcome != "success")
    if after_no_credit_seam:
        ctx.apply_pending_download_reservation_release(task.task_id)
    retained = not after_no_credit_seam or second_outcome == "success"
    assert ctx.has_download_popup_claim(task.task_id, strong) is retained
    assert ctx.has_download_popup_claim(task.task_id, late) is retained

    # Durable credit now closes exactly action1's owned popups.
    with patch(f"{_AGENT}.skyvern_context.current", return_value=ctx):
        await ForgeAgent()._close_credited_download_popups(task, MagicMock(browser_context=context))
    assert strong.is_closed() is retained
    assert late.is_closed() is retained
    assert later_page.is_closed() is (retained and second_outcome != "no_dispatch")
    assert not opener.is_closed()
    assert ctx.download_popup_claims == ctx.download_popup_late_candidates == {}


def test_earliest_pending_snapshot_retained_and_preserves_prior_reservations() -> None:
    """Multiple dispatched failures in one step keep the EARLIEST pending pre-dispatch snapshot: at the
    no-credit seam every reservation minted since that first failure is released, while reservations
    that predate it survive. A later no-dispatch retain cannot disturb the pending epoch."""
    ctx = SkyvernContext(task_id="task")
    prior = object()
    minted_first = object()
    minted_second = object()
    ctx.record_download_popup_claim("task", prior)

    # First dispatched failure stashes its pre-action snapshot (prior only), then mints a claim.
    ctx.stash_pending_download_reservation_release("task", ctx.snapshot_download_popup_reservations("task"))
    ctx.record_download_popup_claim("task", minted_first)
    # A second dispatched failure later in the batch must NOT overwrite the earliest snapshot.
    ctx.stash_pending_download_reservation_release("task", ctx.snapshot_download_popup_reservations("task"))
    ctx.record_download_popup_late_candidate("task", minted_second)

    # The no-credit seam applies the earliest snapshot: everything minted since the first failure goes,
    # the pre-existing reservation stays.
    ctx.apply_pending_download_reservation_release("task")
    assert ctx.has_download_popup_claim("task", prior), "a reservation predating the first failure must survive"
    assert not ctx.has_download_popup_claim("task", minted_first), "a claim minted after the first failure is released"
    assert not ctx.has_download_popup_claim("task", minted_second), "a candidate minted later is released too"
    assert "task" not in ctx.pending_download_reservation_release, "the pending release is consumed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_kind", "setup_result"),
    [
        (kind, result)
        for kind in ("click", "select")
        for result in ("abort", "success", "no_op_skip", "no_op_complete", "exception", "failure")
    ]
    + [("input", "unreported")],
)
async def test_setup_effects_determine_wrapper_reservations(
    tmp_path: Path, setup_result: str, action_kind: str
) -> None:
    """Drive real setup dispatch: only explicit no-effect results release the epoch; exceptions retain it."""
    task, step, _scraped, ctx, events = _make_env()
    context = _FakeContext()
    opener = _FakePage(context, "http://origin/", events, "opener")
    context.pages.append(opener)
    setup_popup = _FakePage(context, "about:blank", events, "setup-popup")
    action: ClickAction | SelectOptionAction | InputTextAction = (
        SelectOptionAction(element_id="download", option=SelectOption(label="receipt"))
        if action_kind == "select"
        else ClickAction(element_id="download", download=False)
    )
    if action_kind == "input":
        action = InputTextAction(element_id="download", text="receipt")
        ctx.record_download_popup_claim(task.task_id, setup_popup)
        ctx.stash_pending_download_reservation_release(task.task_id, ((), ()))
    committed = not setup_result.startswith("no_op")

    scraped_page = MagicMock()
    scraped_page.id_to_element_dict = {"download": object()}

    app_mock = _app_mock(action)
    app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock()

    async def effectful_setup(*_args: Any, **_kwargs: Any) -> list[Any]:
        context.emit_page(setup_popup)
        if setup_result == "exception":
            raise RuntimeError("setup failed after a possible browser effect")
        if setup_result == "failure":
            return [ActionFailure(RuntimeError("setup caught an uncertain click failure"))]
        if setup_result == "unreported":
            return [ActionAbort()]
        if not committed:
            return [ActionAbort(desired_state_reached=setup_result == "no_op_complete")]
        if setup_result == "abort":
            return [ActionAbort(setup_performed=True)]
        return [ActionSuccess(setup_performed=True)]

    with ExitStack() as stack:
        stack.enter_context(patch(f"{_HANDLER}.app", app_mock))
        stack.enter_context(patch(f"{_HANDLER}.get_download_dir", MagicMock(return_value=str(tmp_path))))
        stack.enter_context(patch(f"{_HANDLER}.skyvern_context.current", return_value=ctx))
        stack.enter_context(patch(f"{_HANDLER}.settings.FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0.0))
        stack.enter_context(patch.dict(ActionHandler._setup_action_types, {action.action_type: effectful_setup}))
        await ActionHandler.handle_action(
            scraped_page, task, step, opener, action, file_download_false_click_eligible=True
        )

    if action_kind == "input":
        ctx.apply_pending_download_reservation_release(task.task_id)
    assert _browser_dispatch_committed.get() is committed
    assert ctx.has_download_popup_claim(task.task_id, setup_popup) is committed
    # A committed dispatch keeps its context owner armed to the next action's boundary for every outcome
    # that reaches the arming seam -- including a later exception/failure (symmetric with success). Only a
    # pre-arm no-effect exit ("unreported") never armed it.
    capture_survives = committed and setup_result != "unreported"
    assert context.page_listener_count() == int(capture_survives)
    assert not setup_popup.is_closed()
    later_page = _FakePage(context, "about:blank", events, "later")
    context.emit_page(later_page)
    with patch(f"{_AGENT}.skyvern_context.current", return_value=ctx):
        await ForgeAgent()._close_credited_download_popups(task, MagicMock(browser_context=context))
    assert setup_popup.is_closed() is committed
    assert later_page.is_closed() is capture_survives
    assert not opener.is_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["refresh", "screenshot", "context", "settle", "finalize"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_terminal_fault_releases_all_download_reservations(phase: str, cancel: bool) -> None:
    task, step, _scraped, ctx, events = _make_env()
    context = _FakeContext()
    candidate = _FakePage(context, "about:blank", events, "candidate")
    ctx.record_download_popup_claim(task.task_id, candidate)
    ctx.record_download_popup_late_candidate(task.task_id, candidate)
    ctx.arm_download_popup_context_listener(task.task_id, context, lambda page: None)
    browser_state = MagicMock(browser_context=context)
    browser_state.get_working_page = AsyncMock(return_value=candidate)
    fault = asyncio.CancelledError() if cancel else RuntimeError("terminal fault")

    def fail() -> None:
        raise fault

    with ExitStack() as stack:
        _cleanup_patches(stack, ctx, task, browser_state, fail if phase == "settle" else lambda: None, [])
        if phase == "refresh":
            stack.enter_context(patch(f"{_AGENT}.app.DATABASE.tasks.get_task", AsyncMock(side_effect=fault)))
        elif phase == "screenshot":
            browser_state.take_fullpage_screenshot = AsyncMock(side_effect=fault)
        elif phase == "context":
            type(browser_state).browser_context = PropertyMock(side_effect=fault)
        elif phase == "finalize":
            stack.enter_context(
                patch.object(ForgeAgent, "_finalize_downloaded_files_for_task", AsyncMock(side_effect=fault))
            )
        if cancel or phase in {"refresh", "context"}:
            stack.enter_context(pytest.raises(asyncio.CancelledError if cancel else Exception))
        await ForgeAgent().clean_up_task(
            task, step, need_final_screenshot=phase == "screenshot", download_suffix="file", list_files_before=[]
        )
    assert not candidate.is_closed()
    assert context.page_listener_count() == 0
    assert ctx.download_popup_claims == ctx.download_popup_late_candidates == {}
