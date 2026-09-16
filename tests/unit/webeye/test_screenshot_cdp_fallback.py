"""Viewport rescue must preserve the image contract and terminate the failed attempt chain."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.exceptions import FailedToTakeScreenshot, ScreenshotTargetClosed
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.browser_engine import SKYCDP_ENGINE_NAME
from skyvern.webeye.utils import page as page_module
from skyvern.webeye.utils.page import ScreenshotMode, SkyvernFrame, _current_viewpoint_screenshot_helper


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


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["full_page", "firefox", "webkit", "unknown_browser", "skycdp"])
async def test_ineligible_captures_keep_animation_retry_and_never_attach(kind: str) -> None:
    page = _page()
    if kind in {"firefox", "webkit"}:
        page.context.browser.browser_type.name = kind
    elif kind == "unknown_browser":
        page.context.browser = None
    selection = None
    if kind == "skycdp":
        selection = SimpleNamespace(
            name=SKYCDP_ENGINE_NAME,
            is_engine_error=lambda exc: isinstance(exc, PlaywrightError),
            is_engine_timeout_error=lambda exc: isinstance(exc, PlaywrightTimeoutError),
        )
    page.screenshot.side_effect = [PlaywrightTimeoutError("first"), b"playwright-retry"]
    assert (
        await _current_viewpoint_screenshot_helper(page, full_page=kind == "full_page", engine_selection=selection)
        == b"playwright-retry"
    )
    assert [call.kwargs["animations"] for call in page.screenshot.await_args_list] == ["disabled", "allow"]
    assert all(call.kwargs["full_page"] == (kind == "full_page") for call in page.screenshot.await_args_list)
    page.context.new_cdp_session.assert_not_awaited()


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
    assert await SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=2) == b"full-page"
    page.context.new_cdp_session.assert_awaited_once()
    assert [call.kwargs["full_page"] for call in page.screenshot.await_args_list] == [False, True]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["screenshot", "attach", "geometry", "capture", "detach"])
async def test_cancellation_propagates_and_leaves_no_pending_stage_task(stage: str) -> None:
    page = _page()
    session = page.context.new_cdp_session.return_value
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def hang(*args: object, **kwargs: object) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
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
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    start = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    assert asyncio.get_running_loop().time() - start < 0.3
    assert finished.is_set()
    assert not (asyncio.all_tasks() - existing_tasks)
    assert session.detach.await_count == (stage in {"geometry", "capture", "detach"})
    if stage == "screenshot":
        page.context.new_cdp_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_during_detach_finishes_owned_cleanup_before_propagating() -> None:
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
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
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
    page = _page()
    session = page.context.new_cdp_session.return_value
    original = page.screenshot.side_effect
    events: list[str] = []
    existing_tasks = asyncio.all_tasks()

    async def readiness(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0.03)
        events.append("readiness")
        raise PlaywrightTimeoutError("load guard expired")

    async def screenshot(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0.04)
        events.append("screenshot")
        raise original

    async def attach(*args: object, **kwargs: object) -> AsyncMock:
        if stage == "attach":
            await asyncio.Event().wait()
        await asyncio.sleep(0.025)
        return session

    async def capture(*args: object, **kwargs: object) -> dict[str, str]:
        if stage == "geometry" or (stage == "capture" and args[0] == "Page.captureScreenshot"):
            await asyncio.Event().wait()
        await asyncio.sleep(0.01)
        if args[0] == "Runtime.evaluate":
            return {"result": {"value": {"deviceScaleFactor": 1, "viewportScale": 1}}}
        return session.send.return_value

    async def detach() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("detach_finished")

    page.wait_for_load_state.side_effect = readiness
    page.screenshot.side_effect = screenshot
    page.context.new_cdp_session.side_effect = attach
    session.send.side_effect = capture
    session.detach.side_effect = detach
    start = asyncio.get_running_loop().time()
    if stage == "detach":
        assert await _current_viewpoint_screenshot_helper(page, timeout=40)
    else:
        with pytest.raises(FailedToTakeScreenshot) as raised:
            await _current_viewpoint_screenshot_helper(page, timeout=40)
        assert raised.value.__cause__ is original
    elapsed = asyncio.get_running_loop().time() - start
    print(f"TOTAL_BUDGET stage={stage} elapsed={elapsed:.4f}s api_budget=0.220s wall_limit=0.750s events={events}")
    # Include logging and event-loop scheduling slack while bounding the entire operation.
    assert 0.11 <= elapsed < 0.75
    page.screenshot.assert_awaited_once()
    page.context.new_cdp_session.assert_awaited_once()
    assert events[:2] == ["readiness", "screenshot"]
    if stage != "attach":
        assert events[-1] == "detach_finished"
    assert not (asyncio.all_tasks() - existing_tasks)
