"""Viewport rescue must preserve the image contract and terminate the failed attempt chain."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable, Iterable
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.exceptions import FailedToTakeScreenshot, ScreenshotTargetClosed
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye import action_deadline
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.browser_health import BrowserOperation
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.utils import page as page_module
from skyvern.webeye.utils.page import ScreenshotMode, SkyvernFrame, _current_viewpoint_screenshot_helper
from tests.unit.conftest import (
    MARKER_RGB,
    chromium_context,
    never_answering_origin,
    open_marker_page,
    png_background_colors,
    requires_chromium,
    screenshot_stalls_until_its_own_timeout,
    skycdp_engine_selection,
    stalled_font_css,
)
from tests.unit.scoped_asyncio import ScopedAsyncio

_OTHER = (200, 30, 160)


def _page() -> MagicMock:
    page = MagicMock(spec=Page)
    page.is_closed.return_value = False
    page.url = "https://example.test/screenshot"
    page.viewport_size = {"width": 80, "height": 60}
    page.context.browser.browser_type.name = "chromium"
    page.wait_for_load_state = AsyncMock()
    page.screenshot = AsyncMock(side_effect=PlaywrightTimeoutError("waiting for fonts to load"))
    buffer = BytesIO()
    with Image.new("RGB", (80, 60), (20, 70, 180)) as image:
        image.save(buffer, format="PNG")
    session = AsyncMock()
    session.send.return_value = {"data": base64.b64encode(buffer.getvalue()).decode()}

    async def send(method: str, params: dict) -> dict:
        if method == "Runtime.evaluate":
            return {"result": {"value": {"deviceScaleFactor": 1, "viewportScale": 1}}}
        return session.send.return_value

    session.send.side_effect = send
    page.context.new_cdp_session = AsyncMock(return_value=session)
    return page


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "BROWSER_CURSOR_VISUALIZATION", False)
    for name in ("SESSION", "CAPTURE", "DETACH"):
        monkeypatch.setattr(page_module, f"CDP_RESCUE_{name}_TIMEOUT_SECONDS", 0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(ScreenshotMode))
async def test_timeout_rescues_once_without_animation_retry_and_preserves_file(
    mode: ScreenshotMode, tmp_path: Path
) -> None:
    page = _page()
    path = tmp_path / "nested" / "viewport.png"

    result = await _current_viewpoint_screenshot_helper(page, file_path=str(path), mode=mode)

    assert path.read_bytes() == result
    with Image.open(BytesIO(result)) as image:
        assert image.size == (80, 60)
        assert image.getpixel((20, 20)) == (20, 70, 180)
    page.screenshot.assert_awaited_once()
    page.context.new_cdp_session.assert_awaited_once()
    page.context.new_cdp_session.return_value.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_persistent_context_timeout_is_rescued_over_raw_cdp() -> None:
    # launch_persistent_context exposes no owning Browser, yet CDP still attaches through the context.
    page = _page()
    page.context.browser = None
    result = await _current_viewpoint_screenshot_helper(page)
    with Image.open(BytesIO(result)) as image:
        assert image.size == (80, 60)
    page.screenshot.assert_awaited_once()
    page.context.new_cdp_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "geometry",
    [
        {"deviceScaleFactor": 2, "viewportScale": 1},
        {"deviceScaleFactor": 1, "viewportScale": 2},
        {"deviceScaleFactor": 2, "viewportScale": 0.5},
    ],
)
async def test_scaled_viewports_decline_cdp_and_keep_animation_retry(geometry: dict) -> None:
    page = _page()
    retry_error = PlaywrightTimeoutError("animation retry timed out")
    page.screenshot.side_effect = [page.screenshot.side_effect, retry_error]
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = None
    session.send.return_value = {"result": {"value": geometry}}
    with pytest.raises(FailedToTakeScreenshot) as raised:
        await _current_viewpoint_screenshot_helper(page)
    assert raised.value.__cause__ is retry_error
    assert [call.args[0] for call in session.send.await_args_list] == ["Runtime.evaluate"]
    session.detach.assert_awaited_once()
    assert [call.kwargs["animations"] for call in page.screenshot.await_args_list] == ["disabled", "allow"]


@pytest.mark.asyncio
@pytest.mark.parametrize("animations", ["disabled", "allow"])
async def test_scaled_viewport_decline_retries_inside_the_remaining_deadline(
    animations: Literal["allow", "disabled"],
) -> None:
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = None
    session.send.return_value = {"result": {"value": {"deviceScaleFactor": 2, "viewportScale": 1}}}
    calls: list[tuple[str, float]] = []

    async def finishes_loading_after_the_first_attempt(*, timeout: float, animations: str, **_kwargs: object) -> bytes:
        calls.append((animations, timeout))
        if len(calls) == 1:
            return await screenshot_stalls_until_its_own_timeout(timeout=timeout)
        return b"retry-bytes"

    page.screenshot.side_effect = finishes_loading_after_the_first_attempt
    loop = asyncio.get_running_loop()
    started = loop.time()

    result = await SkyvernFrame.take_scrolling_screenshot(
        page, timeout=1600, mode=ScreenshotMode.LITE, scrolling_number=0, animations=animations, record_health=False
    )

    assert result == b"retry-bytes"
    assert [call_animations for call_animations, _ in calls] == [animations, "allow"]
    (_, primary), (_, retry) = calls
    assert primary <= 1200 and retry <= 1600 - primary
    assert loop.time() - started < 1.6


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["attach", "non_png", "detach"])
async def test_health_silent_capture_failure_logs_nothing_above_info(failure: str) -> None:
    page = _page()
    session = page.context.new_cdp_session.return_value
    if failure == "attach":
        page.context.new_cdp_session.side_effect = PlaywrightError("attach failed")
    if failure in {"non_png", "detach"}:
        session.send.side_effect = None
        session.send.return_value = {"result": {"value": {"deviceScaleFactor": 1, "viewportScale": 1}}, "data": ""}
    if failure == "detach":
        session.detach.side_effect = PlaywrightError("detach failed")
    capture = page_module._CaptureDeadline(
        at=asyncio.get_running_loop().time() + 1, animations="allow", record_health=False
    )

    with capture_logs() as logs, pytest.raises(FailedToTakeScreenshot):
        await _current_viewpoint_screenshot_helper(page, timeout=1000, capture=capture)

    assert [log["event"] for log in logs if log["log_level"] not in {"debug", "info"} or log.get("exc_info")] == []
    page.screenshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_rescue_without_room_before_the_deadline_is_tagged_skipped_and_never_attaches() -> None:
    page = _page()
    with capture_logs() as logs:
        capture = page_module._CaptureDeadline(at=asyncio.get_running_loop().time() + 0.15)
        result = await page_module._cdp_rescue_screenshot(page, None, capture=capture)
    assert result is None
    page.context.new_cdp_session.assert_not_awaited()
    tags = [(log["screenshot.stage"], log["screenshot.outcome"]) for log in logs if "screenshot.outcome" in log]
    assert tags == [("attach", "skipped")]


@pytest.mark.asyncio
async def test_split_capture_reports_the_full_budget_and_the_playwright_share_separately() -> None:
    page = _page()
    capture = page_module._CaptureDeadline(at=asyncio.get_running_loop().time() + 1)
    with capture_logs() as logs:
        await _current_viewpoint_screenshot_helper(page, timeout=1000, capture=capture)
    (attempt,) = (
        log for log in logs if log.get("screenshot.primitive") == "playwright" and "screenshot.eligibility" in log
    )
    assert attempt["screenshot.timeout_budget_ms"] == 1000
    assert 0 < attempt["screenshot.primary_budget_ms"] <= 750


@pytest.mark.asyncio
async def test_scaled_viewport_decline_lets_animation_retry_succeed() -> None:
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = None
    session.send.return_value = {"result": {"value": {"deviceScaleFactor": 2, "viewportScale": 1}}}
    page.screenshot.side_effect = [PlaywrightTimeoutError("first"), b"animation-retry-bytes"]
    result = await _current_viewpoint_screenshot_helper(page)
    assert result == b"animation-retry-bytes"
    assert [call.args[0] for call in session.send.await_args_list] == ["Runtime.evaluate"]
    assert [call.kwargs["animations"] for call in page.screenshot.await_args_list] == ["disabled", "allow"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(ScreenshotMode))
async def test_independent_viewports_can_recover_after_an_earlier_recovery(mode: ScreenshotMode) -> None:
    page = _page()
    first = await _current_viewpoint_screenshot_helper(page, mode=mode)
    second = await _current_viewpoint_screenshot_helper(page, mode=mode)
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"ordinary-frame"
    third = await _current_viewpoint_screenshot_helper(page, mode=mode)
    assert first == second
    assert third == b"ordinary-frame"
    assert page.context.new_cdp_session.await_count == 2
    assert page.screenshot.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(ScreenshotMode))
async def test_successful_capture_keeps_playwright_arguments_and_skips_cdp(mode: ScreenshotMode) -> None:
    page = _page()
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"ordinary-frame"
    assert await _current_viewpoint_screenshot_helper(page, timeout=123, mode=mode) == b"ordinary-frame"
    page.screenshot.assert_awaited_once_with(path=None, timeout=123, full_page=False, animations="disabled")
    page.context.new_cdp_session.assert_not_awaited()
    assert page.wait_for_load_state.await_count == (mode == ScreenshotMode.DETAILED)


def _page_for(kind: str) -> tuple[MagicMock, BrowserEngineSelection | None]:
    page = _page()
    if kind in {"firefox", "webkit"}:
        page.context.browser.browser_type.name = kind
    if kind != "skycdp":
        return page, None
    return page, skycdp_engine_selection()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full_page", "firefox", "webkit", "skycdp"])
async def test_ineligible_captures_keep_animation_retry_and_never_attach(kind: str) -> None:
    page, selection = _page_for(kind)

    async def times_out_then_retry_answers(*, timeout: float, **_kwargs: object) -> bytes:
        if page.screenshot.await_count == 1:
            await screenshot_stalls_until_its_own_timeout(timeout=timeout)
        return b"playwright-retry"

    page.screenshot.side_effect = times_out_then_retry_answers
    capture = page_module._CaptureDeadline(at=asyncio.get_running_loop().time() + 0.3)
    assert (
        await _current_viewpoint_screenshot_helper(
            page, full_page=kind == "full_page", timeout=300, engine_selection=selection, capture=capture
        )
        == b"playwright-retry"
    )
    assert [call.kwargs["animations"] for call in page.screenshot.await_args_list] == ["disabled", "allow"]
    assert all(call.kwargs["full_page"] == (kind == "full_page") for call in page.screenshot.await_args_list)
    assert [call.kwargs["timeout"] for call in page.screenshot.await_args_list] == [300, 300]
    page.context.new_cdp_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("animations", ["disabled", "allow"])
@pytest.mark.parametrize("kind", ["chromium", "firefox", "skycdp"])
async def test_single_viewport_timeout_reaches_the_terminal_mapper_with_one_strike(
    monkeypatch: pytest.MonkeyPatch, kind: str, animations: Literal["allow", "disabled"]
) -> None:
    _production_rescue_timeouts(monkeypatch)
    page, selection = _page_for(kind)
    page.screenshot.side_effect = screenshot_stalls_until_its_own_timeout

    async def never_attaches(*_args: object) -> None:
        await asyncio.Event().wait()

    page.context.new_cdp_session.side_effect = never_attaches
    loop = asyncio.get_running_loop()
    started = loop.time()

    with skyvern_context.scoped(SkyvernContext()) as context, pytest.raises(FailedToTakeScreenshot):
        await SkyvernFrame.take_scrolling_screenshot(
            page,
            timeout=500,
            mode=ScreenshotMode.LITE,
            scrolling_number=0,
            engine_selection=selection,
            animations=animations,
        )

    assert context.browser_health.stuck_operations == {BrowserOperation.SCREENSHOT}
    assert context.browser_health.consecutive_timeouts == 1
    if kind == "chromium":
        assert loop.time() - started < 0.5
        page.screenshot.assert_awaited_once()
    elif animations == "allow":
        assert loop.time() - started < 0.75
        assert page.screenshot.await_args.kwargs["timeout"] == 500
        page.screenshot.assert_awaited_once()
        page.context.new_cdp_session.assert_not_awaited()
    else:
        assert [call.kwargs["animations"] for call in page.screenshot.await_args_list] == ["disabled", "allow"]
        assert [call.kwargs["timeout"] for call in page.screenshot.await_args_list] == [500, 500]
        page.context.new_cdp_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_later_viewport_rescue_falls_back_to_a_split_playwright_capture() -> None:
    page = _page()
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"playwright"
    page.context.new_cdp_session.side_effect = PlaywrightError("attach failed")
    capture = page_module._CaptureDeadline(at=asyncio.get_running_loop().time() + 3, primary_stalled=True)

    assert await _current_viewpoint_screenshot_helper(page, timeout=3000, capture=capture) == b"playwright"

    page.context.new_cdp_session.assert_awaited_once()
    assert page.screenshot.await_args.kwargs["timeout"] <= 3000 * page_module._PRIMARY_CAPTURE_SHARE


@pytest.mark.asyncio
async def test_screenshot_failure_logs_never_carry_the_page_url_secret() -> None:
    page = _page()
    page.url = "https://example.test/signin?token=url-secret-18"
    page.context.new_cdp_session.side_effect = PlaywrightError("attach failure")

    with capture_logs() as logs, pytest.raises(FailedToTakeScreenshot):
        await _current_viewpoint_screenshot_helper(page)

    assert any(log["event"] == "Screenshot timeout" for log in logs)
    assert "url-secret-18" not in repr(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("record_health", [True, False])
@pytest.mark.parametrize(
    ("error", "event", "health_level"),
    [
        (PlaywrightTimeoutError("timed out"), "Screenshot timeout", "warning"),
        (ValueError("bug"), "Screenshot failed", "error"),
    ],
)
async def test_health_silent_capture_logs_its_terminal_failure_at_info(
    record_health: bool, error: Exception, event: str, health_level: str
) -> None:
    page = _page()
    page.screenshot.side_effect = error
    page.context.new_cdp_session.side_effect = PlaywrightError("attach failure")
    capture = page_module._CaptureDeadline(at=asyncio.get_running_loop().time() + 1, record_health=record_health)

    with capture_logs() as logs, pytest.raises(FailedToTakeScreenshot):
        await _current_viewpoint_screenshot_helper(page, timeout=1000, capture=capture)

    terminal = next(log for log in logs if log["event"] == event)
    assert terminal["log_level"] == (health_level if record_health else "info")
    assert bool(terminal.get("exc_info")) == (event == "Screenshot failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_stalled", [False, True])
@pytest.mark.parametrize("stage", ["attach", "geometry", "capture", "detach"])
async def test_a_hung_rescue_stage_ends_inside_the_capture_deadline(
    monkeypatch: pytest.MonkeyPatch, stage: str, primary_stalled: bool
) -> None:
    _production_rescue_timeouts(monkeypatch)
    page = _page()
    session = page.context.new_cdp_session.return_value
    working_send = session.send.side_effect

    async def attach(*_args: object) -> AsyncMock:
        if stage == "attach":
            await asyncio.Event().wait()
        return session

    async def send(method: str, params: dict) -> dict:
        if stage == "geometry" or (stage == "capture" and method == "Page.captureScreenshot"):
            await asyncio.Event().wait()
        return await working_send(method, params)

    async def hang() -> None:
        await asyncio.Event().wait()

    page.context.new_cdp_session.side_effect = attach
    session.send.side_effect = send
    if stage == "detach":
        session.detach.side_effect = hang
    loop = asyncio.get_running_loop()
    capture = page_module._CaptureDeadline(at=loop.time() + 1, primary_stalled=primary_stalled)
    existing_tasks = asyncio.all_tasks()

    with skyvern_context.scoped(SkyvernContext()) as context:
        if stage == "detach":
            assert await _current_viewpoint_screenshot_helper(page, timeout=1000, capture=capture)
        else:
            with pytest.raises(FailedToTakeScreenshot):
                await _current_viewpoint_screenshot_helper(page, timeout=1000, capture=capture)

    assert loop.time() < capture.at
    assert not (asyncio.all_tasks() - existing_tasks)
    if stage != "detach":
        assert context.browser_health.stuck_operations == {BrowserOperation.SCREENSHOT}
        assert context.browser_health.consecutive_timeouts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("bug"), PlaywrightError("capture protocol failure")])
async def test_non_timeout_failure_does_not_become_a_success(error: Exception) -> None:
    page = _page()
    page.screenshot.side_effect = error
    with pytest.raises(FailedToTakeScreenshot) as raised:
        await _current_viewpoint_screenshot_helper(page)
    assert raised.value.__cause__ is error
    page.context.new_cdp_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("on_cancel", ["raise", "wait_again", "return_late_image"])
async def test_capture_the_driver_never_answers_ends_as_a_screenshot_timeout(
    monkeypatch: pytest.MonkeyPatch, on_cancel: str
) -> None:
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", 0)
    monkeypatch.setattr(action_deadline, "DEADLINE_REARM_INTERVAL_SECONDS", 0.05)
    page = _page()

    async def ignores_its_own_timeout(**_kwargs: object) -> bytes:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if on_cancel == "raise":
                raise
            if on_cancel == "return_late_image":
                return b"late"
            await asyncio.Event().wait()
        return b""

    page.screenshot.side_effect = ignores_its_own_timeout
    with skyvern_context.scoped(SkyvernContext()) as context:
        with pytest.raises(FailedToTakeScreenshot):
            await asyncio.wait_for(_current_viewpoint_screenshot_helper(page, timeout=50), timeout=5)

    page.screenshot.assert_awaited_once()
    page.context.new_cdp_session.assert_not_awaited()
    assert context.browser_health.stuck_operations == {BrowserOperation.SCREENSHOT}
    assert context.browser_health.consecutive_timeouts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["attach", "geometry", "capture", "invalid_png"])
async def test_rescue_failure_preserves_original_timeout(stage: str, tmp_path: Path) -> None:
    page = _page()
    original = page.screenshot.side_effect
    session = page.context.new_cdp_session.return_value
    if stage == "attach":
        page.context.new_cdp_session.side_effect = PlaywrightError("attach failure")
    elif stage in {"geometry", "capture"}:
        original_send = session.send.side_effect

        async def fail(method: str, params: dict) -> dict:
            if stage == "geometry" or method == "Page.captureScreenshot":
                raise PlaywrightError("CDP failure")
            return await original_send(method, params)

        session.send.side_effect = fail
    else:
        session.send.return_value = {"data": "not-a-png"}
    path = tmp_path / "failed.png"
    with pytest.raises(FailedToTakeScreenshot) as raised:
        await _current_viewpoint_screenshot_helper(page, file_path=str(path))
    assert raised.value.__cause__ is original
    page.screenshot.assert_awaited_once()
    page.context.new_cdp_session.assert_awaited_once()
    assert not path.exists()
    assert session.detach.await_count == (stage != "attach")


@pytest.mark.asyncio
async def test_failed_scrolling_chain_has_one_cdp_attempt_and_full_page_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = PlaywrightError("CDP failure")
    original = page.screenshot.side_effect
    page.screenshot.side_effect = [original, b"full-page"]
    frame = SimpleNamespace(get_scroll_x_y=AsyncMock(return_value=(0, 0)), safe_scroll_to_x_y=AsyncMock())
    monkeypatch.setattr(SkyvernFrame, "create_instance", AsyncMock(return_value=frame))

    async def capture(**kwargs: object) -> None:
        await _current_viewpoint_screenshot_helper(page, mode=ScreenshotMode.LITE)

    monkeypatch.setattr(page_module, "_scrolling_screenshots_helper", capture)
    assert await SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=2, animations="allow") == b"full-page"
    page.context.new_cdp_session.assert_awaited_once()
    assert [call.kwargs["full_page"] for call in page.screenshot.await_args_list] == [False, True]
    assert page.screenshot.await_args.kwargs["animations"] == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_capture", "attach", "geometry", "capture", "detach"])
async def test_target_close_does_not_retry_the_dead_page(stage: str) -> None:
    page = _page()
    session = page.context.new_cdp_session.return_value

    async def close(*args: object, **kwargs: object) -> None:
        page.is_closed.return_value = True
        raise PlaywrightError("Target page, context or browser has been closed")

    if stage == "before_capture":
        page.is_closed.return_value = True
    elif stage == "attach":
        page.context.new_cdp_session.side_effect = close
    elif stage in {"geometry", "capture"}:
        original_send = session.send.side_effect

        async def close_on_send(method: str, params: dict) -> dict:
            if stage == "geometry" or method == "Page.captureScreenshot":
                await close()
            return await original_send(method, params)

        session.send.side_effect = close_on_send
    else:
        session.detach.side_effect = close

    with pytest.raises(ScreenshotTargetClosed):
        await _current_viewpoint_screenshot_helper(page)
    assert page.screenshot.await_count == (stage != "before_capture")
    assert session.detach.await_count == (stage in {"geometry", "capture", "detach"})


def _recorded_bounds(spent: list[float]) -> ScopedAsyncio:
    # wait_for, timeout and wait bounds above the rescue's stage bounds and every timeout_at never expire, and
    # sleeps return at once; each bound that expires and each nonzero sleep is appended to spent.
    ceiling = max(
        page_module.CDP_RESCUE_SESSION_TIMEOUT_SECONDS,
        page_module.CDP_RESCUE_CAPTURE_TIMEOUT_SECONDS,
        page_module.CDP_RESCUE_DETACH_TIMEOUT_SECONDS,
    )

    def within(delay: float | None) -> float | None:
        return delay if delay is not None and delay <= ceiling else None

    async def wait_for(awaitable: Awaitable[object], timeout: float | None) -> object:
        bound = within(timeout)
        try:
            return await asyncio.wait_for(awaitable, bound)
        except TimeoutError:
            if bound is not None:
                spent.append(bound)
            raise

    class RecordedTimeout(asyncio.Timeout):
        def __init__(self, delay: float | None) -> None:
            self._bound = within(delay)
            super().__init__(None if self._bound is None else asyncio.get_running_loop().time() + self._bound)

        async def __aexit__(
            self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
        ) -> bool | None:
            try:
                return await super().__aexit__(exc_type, exc, tb)
            finally:
                if self._bound is not None and self.expired():
                    spent.append(self._bound)

    def timeout_at(when: float | None) -> asyncio.Timeout:
        return asyncio.timeout(None)

    async def wait(
        fs: Iterable[asyncio.Future[object]], *, timeout: float | None = None, return_when: str = asyncio.ALL_COMPLETED
    ) -> tuple[set[asyncio.Future[object]], set[asyncio.Future[object]]]:
        bound = within(timeout)
        done, pending = await asyncio.wait(fs, timeout=bound, return_when=return_when)
        settled = (
            not pending
            or (return_when == asyncio.FIRST_COMPLETED and bool(done))
            or (return_when == asyncio.FIRST_EXCEPTION and any(not f.cancelled() and f.exception() for f in done))
        )
        if bound is not None and not settled:
            spent.append(bound)
        return done, pending

    async def sleep(delay: float, result: object = None) -> object:
        if delay > 0:
            spent.append(delay)
        await asyncio.sleep(0)
        return result

    return ScopedAsyncio(wait_for=wait_for, timeout=RecordedTimeout, timeout_at=timeout_at, wait=wait, sleep=sleep)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["screenshot", "attach", "geometry", "capture", "detach"])
async def test_cancellation_propagates_and_leaves_no_pending_stage_task(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spent: list[float] = []
    monkeypatch.setattr(page_module, "asyncio", _recorded_bounds(spent))
    page = _page()
    session = page.context.new_cdp_session.return_value
    release = asyncio.Event()
    finished = asyncio.Event()

    async def hang(*args: object, **kwargs: object) -> None:
        # Cancelling as the stage starts lands the cancellation before any bound armed for that stage can expire.
        task.cancel()
        try:
            await release.wait()
        finally:
            finished.set()

    target = {
        "screenshot": page.screenshot,
        "attach": page.context.new_cdp_session,
        "geometry": session.send,
        "capture": session.send,
        "detach": session.detach,
    }[stage]
    original_send = session.send.side_effect
    if stage in {"geometry", "capture"}:

        async def hang_on_send(method: str, params: dict) -> dict:
            if stage == "geometry" or method == "Page.captureScreenshot":
                await hang()
            return await original_send(method, params)

        target.side_effect = hang_on_send
    else:
        target.side_effect = hang
    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(_current_viewpoint_screenshot_helper(page))
    try:
        # A hang guard only.
        await asyncio.wait({task}, timeout=30)
        assert task.done(), f"the cancellation did not end the hung {stage} stage"
        assert finished.is_set()
        assert not (asyncio.all_tasks() - existing_tasks)
        # The cancellation itself ends the hung stage; only the drain of an owned detach waits out its bound.
        assert spent == ([page_module.CDP_RESCUE_DETACH_TIMEOUT_SECONDS] if stage == "detach" else [])
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(asyncio.CancelledError):
        task.result()
    assert session.detach.await_count == (stage in {"geometry", "capture", "detach"})
    if stage == "screenshot":
        page.context.new_cdp_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_during_detach_finishes_owned_cleanup_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An hour-long detach bound cannot expire within the test, so only the release ends the detach.
    monkeypatch.setattr(page_module, "CDP_RESCUE_DETACH_TIMEOUT_SECONDS", 3600)
    page = _page()
    session = page.context.new_cdp_session.return_value
    entered = asyncio.Event()
    release = asyncio.Event()
    detached = asyncio.Event()

    async def detach() -> None:
        entered.set()
        await release.wait()
        detached.set()

    session.detach.side_effect = detach
    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(_current_viewpoint_screenshot_helper(page))
    try:
        # Hang guards only.
        await asyncio.wait_for(entered.wait(), timeout=30)
        task.cancel()
        # A cancellation that skipped the drain would end the operation within these turns of the loop.
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done(), "the cancellation propagated before the owned detach finished"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)
        assert detached.is_set()
        assert not (asyncio.all_tasks() - existing_tasks)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["attach", "geometry", "capture", "detach"])
async def test_complete_operation_budget_includes_readiness_and_cleanup(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Console traceback rendering is unrelated to the coroutine budgets under test.
    monkeypatch.setattr(page_module, "LOG", MagicMock())
    spent: list[float] = []
    monkeypatch.setattr(page_module, "asyncio", _recorded_bounds(spent))
    page = _page()
    session = page.context.new_cdp_session.return_value
    original = page.screenshot.side_effect
    events: list[str] = []
    release = asyncio.Event()
    existing_tasks = asyncio.all_tasks()

    async def readiness(*args: object, timeout: float | None = None) -> None:
        # A page that never loads ends this wait only at the readiness bound, and Playwright reads 0 as no bound.
        if not timeout:
            await release.wait()
        events.append("readiness")
        raise PlaywrightTimeoutError("load guard expired")

    async def screenshot(*args: object, **kwargs: object) -> None:
        events.append("screenshot")
        raise original

    async def attach(*args: object, **kwargs: object) -> AsyncMock:
        if stage == "attach":
            await release.wait()
        return session

    async def capture(*args: object, **kwargs: object) -> dict[str, str]:
        if stage == "geometry" or (stage == "capture" and args[0] == "Page.captureScreenshot"):
            await release.wait()
        if args[0] == "Runtime.evaluate":
            return {"result": {"value": {"deviceScaleFactor": 1, "viewportScale": 1}}}
        return session.send.return_value

    async def detach() -> None:
        try:
            await release.wait()
        finally:
            events.append("detach_finished")

    page.wait_for_load_state.side_effect = readiness
    page.screenshot.side_effect = screenshot
    page.context.new_cdp_session.side_effect = attach
    session.send.side_effect = capture
    session.detach.side_effect = detach
    call = asyncio.create_task(_current_viewpoint_screenshot_helper(page, timeout=40))
    try:
        # A hang guard only: the stage bounds end every wedge long before it.
        await asyncio.wait({call}, timeout=30)
        assert call.done(), f"no bound ended the wedged {stage} stage"
        assert not (asyncio.all_tasks() - existing_tasks)
        assert events[:2] == ["readiness", "screenshot"]
        if stage != "attach":
            assert events[-1] == "detach_finished"
        session_bound = page_module.CDP_RESCUE_SESSION_TIMEOUT_SECONDS
        capture_bound = page_module.CDP_RESCUE_CAPTURE_TIMEOUT_SECONDS
        detach_bound = page_module.CDP_RESCUE_DETACH_TIMEOUT_SECONDS
        # Each wedged stage costs its own bound once, and every arm that opens a session also wedges the detach.
        expected = {
            "attach": [session_bound],
            "geometry": [capture_bound, detach_bound],
            "capture": [capture_bound, detach_bound],
            "detach": [detach_bound],
        }[stage]
        assert spent == expected
    finally:
        release.set()
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
    if stage == "detach":
        assert call.result()
    else:
        with pytest.raises(FailedToTakeScreenshot) as raised:
            call.result()
        assert raised.value.__cause__ is original
    page.screenshot.assert_awaited_once()
    page.context.new_cdp_session.assert_awaited_once()


def _production_rescue_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, seconds in (("SESSION", 5), ("CAPTURE", 5), ("DETACH", 2)):
        monkeypatch.setattr(page_module, f"CDP_RESCUE_{name}_TIMEOUT_SECONDS", seconds)


async def _viewport_capture(page: Page, timeout_ms: int = 3000) -> bytes:
    return await SkyvernFrame.take_scrolling_screenshot(
        page, timeout=timeout_ms, mode=ScreenshotMode.LITE, scrolling_number=0
    )


@pytest.mark.asyncio
@requires_chromium
async def test_real_chromium_font_stalled_scrollable_page_returns_fresh_pixels_in_every_band(
    monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]
) -> None:
    _production_rescue_timeouts(monkeypatch)
    async with chromium_context() as context, never_answering_origin() as origin:
        page = await open_marker_page(context, MARKER_RGB, f"{origin}/", stall_origin=origin, height_px=2400)
        await page.evaluate("window.scrollTo(0, 500)")
        state = RealBrowserState(pw=MagicMock(), browser_context=context, page=page)
        existing_tasks = asyncio.all_tasks()
        start = time.monotonic()
        result = await state.take_fullpage_screenshot()
        elapsed_ms = (time.monotonic() - start) * 1000
        record_property("elapsed_ms", round(elapsed_ms))
        assert elapsed_ms < settings.BROWSER_SCREENSHOT_TIMEOUT_MS
        with Image.open(BytesIO(result)) as image:
            assert image.height >= 2400
        assert png_background_colors(result) == {MARKER_RGB}
        assert await page.evaluate("window.scrollY") == 500
        assert not (asyncio.all_tasks() - existing_tasks)


@pytest.mark.asyncio
@requires_chromium
async def test_real_chromium_healthy_page_keeps_the_playwright_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    _production_rescue_timeouts(monkeypatch)
    rescue = AsyncMock()
    monkeypatch.setattr(page_module, "_cdp_rescue_screenshot", rescue)
    async with chromium_context() as context:
        page = await open_marker_page(context, MARKER_RGB, "http://capture.test/", height_px=2400)
        state = RealBrowserState(pw=MagicMock(), browser_context=context, page=page)
        assert png_background_colors(await state.take_fullpage_screenshot()) == {MARKER_RGB}
        assert png_background_colors(await _viewport_capture(page)) == {MARKER_RGB}
    rescue.assert_not_awaited()


@pytest.mark.asyncio
@requires_chromium
async def test_real_chromium_same_url_sibling_never_supplies_the_stalled_page_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _production_rescue_timeouts(monkeypatch)
    async with chromium_context() as context, never_answering_origin() as origin:
        sibling = await open_marker_page(context, _OTHER, f"{origin}/")
        page = await open_marker_page(context, MARKER_RGB, f"{origin}/", stall_origin=origin)
        assert sibling.url == page.url
        assert png_background_colors(await _viewport_capture(page)) == {MARKER_RGB}


@pytest.mark.asyncio
@requires_chromium
async def test_real_chromium_stall_after_an_earlier_frame_returns_only_the_new_frame(
    monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]
) -> None:
    _production_rescue_timeouts(monkeypatch)
    async with chromium_context() as context, never_answering_origin() as origin:
        page = await open_marker_page(context, _OTHER, f"{origin}/")
        assert png_background_colors(await _viewport_capture(page)) == {_OTHER}
        await page.evaluate(
            "css => { const s = document.createElement('style'); s.textContent = css; document.head.append(s); }",
            stalled_font_css(origin, MARKER_RGB),
        )
        await page.wait_for_function("document.fonts.status === 'loading'")
        start = time.monotonic()
        result = await _viewport_capture(page)
        elapsed_ms = (time.monotonic() - start) * 1000
        record_property("elapsed_ms", round(elapsed_ms))
        assert png_background_colors(result) == {MARKER_RGB}
        assert elapsed_ms < 3000


@pytest.mark.asyncio
@requires_chromium
async def test_real_chromium_page_closed_during_a_stalled_capture_fails_as_target_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _production_rescue_timeouts(monkeypatch)
    async with chromium_context() as context, never_answering_origin() as origin:
        page = await open_marker_page(context, MARKER_RGB, f"{origin}/", stall_origin=origin)
        existing_tasks = asyncio.all_tasks()
        capture = asyncio.create_task(_viewport_capture(page, settings.BROWSER_SCREENSHOT_TIMEOUT_MS))
        await asyncio.sleep(0.5)
        start = time.monotonic()
        await page.close()
        with pytest.raises(ScreenshotTargetClosed):
            await capture
        assert time.monotonic() - start < 2
        assert not (asyncio.all_tasks() - existing_tasks)


@pytest.mark.asyncio
@requires_chromium
async def test_real_chromium_busy_renderer_fails_truthfully_within_the_capture_deadline(
    monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]
) -> None:
    _production_rescue_timeouts(monkeypatch)
    async with chromium_context() as context:
        page = await open_marker_page(context, MARKER_RGB, "http://capture.test/")
        await page.evaluate("setTimeout(() => { while (true) {} }, 0)")
        existing_tasks = asyncio.all_tasks()
        start = time.monotonic()
        with skyvern_context.scoped(SkyvernContext()) as run_context, pytest.raises(FailedToTakeScreenshot):
            await _viewport_capture(page)
        elapsed_ms = (time.monotonic() - start) * 1000
        record_property("elapsed_ms", round(elapsed_ms))
        assert elapsed_ms < 3000
        assert run_context.browser_health.stuck_operations == {BrowserOperation.SCREENSHOT}
        assert run_context.browser_health.consecutive_timeouts == 1
        assert not (asyncio.all_tasks() - existing_tasks)
