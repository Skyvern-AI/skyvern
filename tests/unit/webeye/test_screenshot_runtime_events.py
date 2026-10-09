from __future__ import annotations

import asyncio
import base64
import functools
import time
from collections.abc import Callable, Iterator
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from structlog.testing import capture_logs

from skyvern.exceptions import FailedToTakeScreenshot, ScreenshotTargetClosed
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.browser_errors import BrowserTargetClosedError
from skyvern.webeye.browser_runtime_events import BrowserRuntimeLogContext, browser_runtime_log_context
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.utils import page as page_module
from skyvern.webeye.utils.page import ScreenshotMode, SkyvernFrame
from tests.unit.conftest import DriverReplyProbe, stalling_async_mock
from tests.unit.forge_log_capture import capture_runtime_logs


@pytest.fixture(autouse=True)
def capture_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(SettingsManager.get_settings(), "BROWSER_CURSOR_VISUALIZATION", False)
    with skyvern_context.scoped(SkyvernContext()):
        yield


@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    with BytesIO() as buffer, Image.new("RGB", (80, 60), (20, 70, 180)) as image:
        image.save(buffer, format="PNG")
        png = buffer.getvalue()
    page = MagicMock(spec=Page)
    page.url = "https://example.test/private?token=synthetic-secret"
    page.viewport_size = {"width": 80, "height": 60}
    page.is_closed.return_value = False
    page.context.browser.browser_type.name = "chromium"
    page.wait_for_load_state = AsyncMock()
    page.screenshot = AsyncMock(return_value=png)
    session = AsyncMock()

    async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "Runtime.evaluate":
            return {"result": {"value": {"deviceScaleFactor": 1, "viewportScale": 1}}}
        return {"data": base64.b64encode(png).decode()}

    session.send.side_effect = send
    page.context.new_cdp_session = AsyncMock(return_value=session)
    frame = MagicMock(spec=SkyvernFrame)
    frame.get_scroll_x_y = AsyncMock(return_value=(11, 22))
    frame.is_window_scrollable = AsyncMock(return_value=True)
    frame.get_scroll_width_and_height = AsyncMock(return_value=(80, 120))
    frame.scroll_to_top = AsyncMock(return_value=0)
    frame.scroll_to_next_page = AsyncMock(side_effect=[30, 60])
    monkeypatch.setattr(SkyvernFrame, "create_instance", AsyncMock(return_value=frame))
    return page


def runtime_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if "browser_runtime_event" in entry]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,outcome,raised_type",
    [
        (PlaywrightTimeoutError("private timeout detail"), "timeout", FailedToTakeScreenshot),
        (TimeoutError("private deadline detail"), "timeout", FailedToTakeScreenshot),
        (
            PlaywrightError("Target page, context or browser has been closed"),
            "target_closed",
            ScreenshotTargetClosed,
        ),
        (PlaywrightError("Page crashed"), "other_error", FailedToTakeScreenshot),
        (ValueError("private payload"), "other_error", FailedToTakeScreenshot),
    ],
)
async def test_terminal_request_reports_one_bounded_failure(
    page: MagicMock, failure: Exception, outcome: str, raised_type: type[Exception]
) -> None:
    page.screenshot.side_effect = failure
    page.context.new_cdp_session.side_effect = RuntimeError("private provider endpoint")
    with capture_logs() as logs, pytest.raises(raised_type) as raised:
        await SkyvernFrame.take_scrolling_screenshot(page, mode=ScreenshotMode.LITE, scrolling_number=2, timeout=1234)
    assert raised.value.__cause__ is failure
    events = runtime_events(logs)
    assert len(events) == 1
    assert events[0] == {
        "event": "Screenshot request failed",
        "log_level": "warning",
        "browser_runtime_event": "screenshot_failure",
        "workflow_run_id": None,
        "task_id": None,
        "browser_session_id": None,
        "outcome": outcome,
        "screenshot_phase": "scrolling_screenshot",
        "timeout_ms": 1234,
        "elapsed_ms": events[0]["elapsed_ms"],
    }
    assert isinstance(events[0]["elapsed_ms"], (int, float)) and events[0]["elapsed_ms"] >= 0
    if outcome == "timeout" and isinstance(failure, PlaywrightTimeoutError):
        assert [(call.kwargs["full_page"], call.kwargs["animations"]) for call in page.screenshot.call_args_list] == [
            (False, "disabled"),
            (True, "disabled"),
            (True, "allow"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["ordinary", "cdp", "animation", "fullpage"])
async def test_successful_request_and_internal_recovery_emit_no_failure(
    page: MagicMock, tmp_path: Path, recovery: str
) -> None:
    png = page.screenshot.return_value
    path = tmp_path / "screenshot.png"
    attempt = 0

    async def screenshot(**kwargs: Any) -> bytes:
        nonlocal attempt
        attempt += 1
        if recovery == "cdp":
            raise PlaywrightTimeoutError("capture timed out")
        if recovery == "animation" and attempt == 1:
            raise PlaywrightTimeoutError("capture timed out")
        if recovery == "fullpage" and not kwargs["full_page"]:
            raise PlaywrightError("capture failed")
        if kwargs.get("path"):
            Path(kwargs["path"]).write_bytes(png)
        return png

    page.screenshot.side_effect = screenshot
    if recovery == "animation":
        session = page.context.new_cdp_session.return_value
        session.send.side_effect = None
        session.send.return_value = {"result": {"value": {"deviceScaleFactor": 2, "viewportScale": 1}}}
    with capture_logs() as logs:
        result = await SkyvernFrame.take_scrolling_screenshot(
            page, file_path=str(path), mode=ScreenshotMode.LITE, scrolling_number=2
        )
    assert result == path.read_bytes()
    with Image.open(BytesIO(result)) as image:
        assert image.size == (80, 60 if recovery == "fullpage" else 90)
    assert runtime_events(logs) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["setup", "capture"])
async def test_owned_deadline_emits_timeout_after_local_conversion(
    page: MagicMock, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    entered = asyncio.Event()
    if stage == "setup":
        monkeypatch.setattr(SkyvernFrame, "create_instance", stalling_async_mock(entered))
    else:
        page.screenshot = stalling_async_mock(entered)
    with capture_logs() as logs, pytest.raises(TimeoutError):
        await SkyvernFrame.take_scrolling_screenshot(page, mode=ScreenshotMode.LITE, scrolling_number=1, timeout=25)
    assert entered.is_set()
    events = runtime_events(logs)
    assert len(events) == 1 and events[0]["outcome"] == "timeout"
    assert events[0]["timeout_ms"] == 25
    assert events[0]["elapsed_ms"] >= 20


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["owned_deadline", "caller_deadline", "external_cancel"])
async def test_an_abandoned_capture_leaves_the_driver_no_unretrieved_reply(page: MagicMock, stop: str) -> None:
    probe = DriverReplyProbe(PlaywrightTimeoutError("Timeout 25ms exceeded.\nCall log:\ntaking page screenshot"))
    page.screenshot = probe
    capture = functools.partial(
        SkyvernFrame.take_scrolling_screenshot, page, mode=ScreenshotMode.LITE, scrolling_number=1
    )
    if stop == "external_cancel":
        task = asyncio.create_task(capture(timeout=5000))
        await asyncio.wait_for(probe.called.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif stop == "caller_deadline":
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.025):
                await capture(timeout=5000)
    else:
        with pytest.raises(TimeoutError):
            await capture(timeout=25)
    assert not await probe.orphaned_reply()


@pytest.mark.asyncio
@pytest.mark.parametrize("outer_deadline", [False, True])
@pytest.mark.parametrize("scrolling_number", [0, 1])
async def test_cancellation_and_caller_deadline_emit_no_event(
    page: MagicMock, outer_deadline: bool, scrolling_number: int
) -> None:
    entered = asyncio.Event()
    page.screenshot = stalling_async_mock(entered)
    with capture_logs() as logs:
        if outer_deadline:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.025):
                    await SkyvernFrame.take_scrolling_screenshot(
                        page, mode=ScreenshotMode.LITE, scrolling_number=scrolling_number, timeout=1000
                    )
        else:
            task = asyncio.create_task(
                SkyvernFrame.take_scrolling_screenshot(
                    page, mode=ScreenshotMode.LITE, scrolling_number=scrolling_number
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    assert entered.is_set()
    assert runtime_events(logs) == []


@pytest.mark.asyncio
async def test_zero_scroll_successes_do_not_emit_runtime_events(page: MagicMock) -> None:
    with capture_logs() as logs:
        for _ in range(20):
            assert await SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=0)
    assert runtime_events(logs) == []


@pytest.mark.asyncio
async def test_screenshot_closure_does_not_consume_runtime_end_ownership(page: MagicMock) -> None:
    config = structlog.get_config()
    try:
        # Reconfiguration must not strand runtime logs on a processor list retained by an earlier test.
        structlog.configure(processors=list(config["processors"]))
        state = RealBrowserState(pw=MagicMock(), browser_context=page.context)
        state.record_browser_acquisition("attach")
        page.is_closed.return_value = True
        with capture_logs() as logs:
            with pytest.raises(ScreenshotTargetClosed):
                await SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=0)
            assert [entry["browser_runtime_event"] for entry in runtime_events(logs)] == ["screenshot_failure"]
            assert state.get_browser_state_diagnostic() is None
            state._on_browser_context_closed(page.context)
            state._on_browser_context_closed(page.context)
        events = runtime_events(logs)
        assert [entry["browser_runtime_event"] for entry in events] == ["screenshot_failure", "runtime_ended"]
        assert events[0]["outcome"] == "target_closed"
        assert events[1]["disconnect_kind"] == "context_closed"
    finally:
        structlog.configure(**config)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["workflow", "task", "session", "missing"])
@pytest.mark.parametrize("organization_id", ["org-owner", None])
async def test_entry_identity_snapshot_survives_ambient_changes_and_log_processor(
    page: MagicMock, identity: str, organization_id: str | None
) -> None:
    owner = SkyvernContext(
        workflow_run_id="workflow-owner" if identity == "workflow" else None,
        task_id="task-owner" if identity == "task" else None,
        browser_session_id="session-owner" if identity != "missing" else None,
        run_id="must-not-promote",
        organization_id=organization_id,
    )
    unrelated = SkyvernContext(
        workflow_run_id="unrelated-workflow", run_id="unrelated-run", organization_id="unrelated-org"
    )
    listener_context, listener = MagicMock(), MagicMock()
    unrelated.download_popup_context_listeners["unrelated-task"] = [(listener_context, listener)]

    async def fail(**kwargs: Any) -> bytes:
        skyvern_context.set(unrelated)
        raise ValueError("private exception data")

    page.screenshot.side_effect = fail
    with skyvern_context.scoped(owner), capture_runtime_logs() as logs:
        with pytest.raises(FailedToTakeScreenshot):
            await SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=0)
        assert skyvern_context.current() is unrelated
        assert unrelated.download_popup_context_listeners["unrelated-task"] == [(listener_context, listener)]
        listener_context.remove_listener.assert_not_called()
    events = runtime_events(logs)
    assert len(events) == 1
    event = events[0]
    assert runtime_events(owner.log) == [{key: value for key, value in event.items() if key != "log_level"}]
    assert runtime_events(unrelated.log) == []
    assert event["workflow_run_id"] == owner.workflow_run_id
    assert event["task_id"] == owner.task_id
    assert event["browser_session_id"] == owner.browser_session_id
    assert event.get("organization_id") == organization_id
    assert set(event) == {
        "msg",
        "log_level",
        "env",
        "version",
        "browser_runtime_event",
        "workflow_run_id",
        "task_id",
        "browser_session_id",
        "outcome",
        "screenshot_phase",
        "timeout_ms",
        "elapsed_ms",
    } | ({"organization_id"} if organization_id is not None else set())
    assert "private" not in str(event) and "unrelated" not in str(event) and "must-not-promote" not in str(event)


@pytest.mark.asyncio
async def test_concurrent_bound_contexts_do_not_share_identity(page: MagicMock) -> None:
    entered = 0
    all_entered = asyncio.Event()
    owners = [
        SkyvernContext(
            workflow_run_id="workflow-first", browser_session_id="session-first", organization_id="org-first"
        ),
        SkyvernContext(task_id="task-second", browser_session_id="session-second", organization_id="org-second"),
    ]
    contexts = []
    for owner in owners:
        with skyvern_context.scoped(owner):
            contexts.append(BrowserRuntimeLogContext.current())
    contexts.append(BrowserRuntimeLogContext())
    ambient = SkyvernContext(workflow_run_id="ambient", organization_id="unrelated-org")

    async def fail(**kwargs: Any) -> bytes:
        nonlocal entered
        entered += 1
        if entered == 3:
            all_entered.set()
        await all_entered.wait()
        raise ValueError("capture failed")

    page.screenshot.side_effect = fail

    async def capture(context: BrowserRuntimeLogContext) -> None:
        with browser_runtime_log_context(context), pytest.raises(FailedToTakeScreenshot):
            await SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=0)

    with (
        skyvern_context.scoped(ambient),
        capture_runtime_logs() as logs,
    ):
        await asyncio.gather(*(capture(context) for context in contexts))
    events = runtime_events(logs)
    assert len(events) == 3
    assert {(e["workflow_run_id"], e["task_id"], e["browser_session_id"]) for e in events} == {
        ("workflow-first", None, "session-first"),
        (None, "task-second", "session-second"),
        (None, None, None),
    }
    assert {(e["browser_session_id"], e.get("organization_id")) for e in events} == {
        ("session-first", "org-first"),
        ("session-second", "org-second"),
        (None, None),
    }
    assert runtime_events(ambient.log) == []
    for owner in owners:
        persisted = runtime_events(owner.log)
        assert len(persisted) == 1
        event = next(event for event in events if event["browser_session_id"] == owner.browser_session_id)
        assert persisted[0] == {key: value for key, value in event.items() if key != "log_level"}


@pytest.mark.asyncio
async def test_final_fallback_error_determines_outcome(page: MagicMock) -> None:
    final_error = PermissionError("private persistence failure")
    page.context.new_cdp_session.side_effect = RuntimeError("rescue unavailable")
    page.screenshot.side_effect = [PlaywrightTimeoutError("initial timeout"), final_error]
    with capture_logs() as logs, pytest.raises(FailedToTakeScreenshot) as raised:
        await SkyvernFrame.take_scrolling_screenshot(page, mode=ScreenshotMode.LITE, scrolling_number=1)
    assert raised.value.__cause__ is final_error
    assert [entry["outcome"] for entry in runtime_events(logs)] == ["other_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "target_closed", "other_error"])
async def test_selected_engine_failure_classification(page: MagicMock, outcome: str) -> None:
    failure = RuntimeError("target was disposed" if outcome == "target_closed" else "native failure")
    selection = MagicMock(spec=BrowserEngineSelection)
    selection.name = "rustwright"
    selection.is_engine_error.side_effect = lambda exc: exc is failure
    selection.is_engine_timeout_error.side_effect = lambda exc: exc is failure and outcome == "timeout"
    selection.classify_error.side_effect = lambda exc: (
        BrowserTargetClosedError(str(exc)) if exc is failure and outcome == "target_closed" else None
    )
    page.screenshot.side_effect = failure
    page.context.new_cdp_session.side_effect = RuntimeError("rescue unavailable")
    with capture_logs() as logs, pytest.raises(FailedToTakeScreenshot) as raised:
        await SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=0, engine_selection=selection)
    assert raised.value.__cause__ is failure
    assert [entry["outcome"] for entry in runtime_events(logs)] == [outcome]


@pytest.mark.asyncio
async def test_suppressed_scroll_restore_timeout_is_still_success(page: MagicMock) -> None:
    frame = await SkyvernFrame.create_instance(page)
    frame.safe_scroll_to_x_y.side_effect = TimeoutError("restore deadline")
    with capture_logs() as logs:
        result = await SkyvernFrame.take_scrolling_screenshot(page, mode=ScreenshotMode.LITE, scrolling_number=1)
    assert result.startswith(page_module._PNG_SIGNATURE)
    assert runtime_events(logs) == []


@pytest.mark.asyncio
async def test_logging_failure_preserves_original_capture_exception(
    page: MagicMock, failing_sink: Callable[..., None]
) -> None:
    failure = ValueError("capture failed")
    page.screenshot.side_effect = failure
    failing_sink(
        page_module.LOG,
        "warning",
        when=lambda *args, **kwargs: kwargs.get("browser_runtime_event") == "screenshot_failure",
    )
    original_context = skyvern_context.current()
    with pytest.raises(FailedToTakeScreenshot) as raised:
        await SkyvernFrame.take_scrolling_screenshot(page, scrolling_number=0)
    assert raised.value.__cause__ is failure
    assert skyvern_context.current() is original_context


def _viewport_capture(png: bytes, *, frames: int, then: Callable[..., Any]) -> Callable[..., Any]:
    """``page.screenshot`` that answers the first ``frames`` viewport captures, then defers to ``then``."""
    answered = 0

    async def screenshot(**kwargs: Any) -> bytes:
        nonlocal answered
        if not kwargs["full_page"] and answered < frames:
            answered += 1
            return png
        return await then(**kwargs)

    return screenshot


def _stall(entered: asyncio.Event) -> Callable[..., Any]:
    async def stall(**kwargs: Any) -> bytes:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return stall


@pytest.mark.asyncio
@pytest.mark.parametrize("frames_before_stall", [1, 2])
async def test_post_action_screenshot_keeps_captured_viewports_at_its_own_deadline(
    page: MagicMock, monkeypatch: pytest.MonkeyPatch, frames_before_stall: int
) -> None:
    frame = await SkyvernFrame.create_instance(page)
    frame.scroll_to_next_page.side_effect = [30, 60, 90]
    entered = asyncio.Event()
    page.screenshot.side_effect = _viewport_capture(
        page.screenshot.return_value, frames=frames_before_stall, then=_stall(entered)
    )
    monkeypatch.setattr(
        SkyvernFrame,
        "take_scrolling_screenshot",
        staticmethod(functools.partial(SkyvernFrame.take_scrolling_screenshot, timeout=150)),
    )
    state = RealBrowserState(pw=MagicMock(), browser_context=page.context)
    monkeypatch.setattr(state, "get_working_page", AsyncMock(return_value=page))
    with capture_logs() as logs:
        png = await state.take_post_action_screenshot(scrolling_number=3)
    assert entered.is_set()
    with Image.open(BytesIO(png)) as image:
        assert image.size == (80, 60 + 30 * (frames_before_stall - 1))
    [partial] = [entry for entry in logs if entry.get("incomplete_reason") == "capture_timeout"]
    assert partial["frame_count"] == frames_before_stall
    assert partial["scroll_positions"] == [0, 30][:frames_before_stall]
    assert [entry["outcome"] for entry in runtime_events(logs)] == ["timeout"]


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_partial,frames", [(False, 2), (True, 0)])
async def test_default_and_frameless_scrolling_screenshot_timeouts_still_raise(
    page: MagicMock, keep_partial: bool, frames: int
) -> None:
    frame = await SkyvernFrame.create_instance(page)
    frame.scroll_to_next_page.side_effect = [30, 60, 90]
    page.screenshot.side_effect = _viewport_capture(
        page.screenshot.return_value, frames=frames, then=_stall(asyncio.Event())
    )
    with pytest.raises(TimeoutError):
        await SkyvernFrame.take_scrolling_screenshot(
            page, mode=ScreenshotMode.LITE, scrolling_number=3, timeout=100, keep_partial_on_timeout=keep_partial
        )


@pytest.mark.asyncio
async def test_complete_capture_is_unchanged_when_keeping_partials(page: MagicMock) -> None:
    frame = await SkyvernFrame.create_instance(page)
    frame.scroll_to_next_page.side_effect = [30, 60, 30, 60]
    default = await SkyvernFrame.take_scrolling_screenshot(page, mode=ScreenshotMode.LITE, scrolling_number=2)
    with capture_logs() as logs:
        kept = await SkyvernFrame.take_scrolling_screenshot(
            page, mode=ScreenshotMode.LITE, scrolling_number=2, keep_partial_on_timeout=True
        )
    assert kept == default
    assert not [entry for entry in logs if "incomplete_reason" in entry]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["external_cancel", "deadline_cancel_collision", "caller_deadline"])
async def test_foreign_cancellation_never_becomes_a_partial_screenshot(page: MagicMock, stop: str) -> None:
    frame = await SkyvernFrame.create_instance(page)
    frame.scroll_to_next_page.side_effect = [30, 60, 90]
    entered = asyncio.Event()
    png = page.screenshot.return_value
    capturing: list[asyncio.Task[bytes]] = []
    if stop == "deadline_cancel_collision":

        async def collide(**kwargs: Any) -> bytes:
            # Block past the own deadline without yielding, then cancel the capture the way a foreign
            # canceller does: both land together.
            time.sleep(0.06)
            capturing[0].cancel()
            return await _stall(entered)(**kwargs)

        page.screenshot.side_effect = _viewport_capture(png, frames=1, then=collide)
    else:
        page.screenshot.side_effect = _viewport_capture(png, frames=1, then=_stall(entered))
    capture = functools.partial(
        SkyvernFrame.take_scrolling_screenshot,
        page,
        mode=ScreenshotMode.LITE,
        scrolling_number=3,
        keep_partial_on_timeout=True,
    )
    if stop == "caller_deadline":
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await capture(timeout=5000)
    else:
        task = asyncio.create_task(capture(timeout=20 if stop == "deadline_cancel_collision" else 5000))
        capturing.append(task)
        await asyncio.wait_for(entered.wait(), timeout=2)
        if stop == "external_cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    assert entered.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["target_closed", "malformed_frame"])
async def test_keeping_partials_preserves_target_closed_and_stitch_failures(page: MagicMock, failure: str) -> None:
    frame = await SkyvernFrame.create_instance(page)
    frame.scroll_to_next_page.side_effect = [30, 60, 90]
    if failure == "target_closed":

        async def closed(**kwargs: Any) -> bytes:
            raise PlaywrightError("Target page, context or browser has been closed")

        page.screenshot.side_effect = _viewport_capture(page.screenshot.return_value, frames=1, then=closed)
        expected: type[Exception] = ScreenshotTargetClosed
    else:
        page.screenshot.side_effect = _viewport_capture(b"not-a-png", frames=1, then=_stall(asyncio.Event()))
        expected = TimeoutError
    with pytest.raises(expected):
        await SkyvernFrame.take_scrolling_screenshot(
            page, mode=ScreenshotMode.LITE, scrolling_number=3, timeout=150, keep_partial_on_timeout=True
        )


@pytest.mark.asyncio
async def test_a_timeout_that_is_not_the_capture_deadline_keeps_raising(
    page: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = await SkyvernFrame.create_instance(page)
    frame.scroll_to_next_page.side_effect = [30, 60, 90]
    png = page.screenshot.return_value
    viewports = 0

    # A pinned engine re-raises non-engine errors raw, so the full-page fallback can surface a builtin TimeoutError.
    async def capture(**kwargs: Any) -> bytes:
        nonlocal viewports
        if kwargs.get("full_page"):
            raise TimeoutError("fallback's own timeout")
        viewports += 1
        if viewports == 1:
            return png
        raise PlaywrightError("viewport capture failed")

    monkeypatch.setattr(page_module, "_current_viewpoint_screenshot_helper", capture)
    with pytest.raises(TimeoutError, match="fallback's own timeout"):
        await SkyvernFrame.take_scrolling_screenshot(
            page, mode=ScreenshotMode.LITE, scrolling_number=3, timeout=5000, keep_partial_on_timeout=True
        )
