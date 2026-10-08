"""Fail-closed, stable-assignment CDP-first screenshot experiment.

Control retains Playwright-first capture and raw-CDP rescue on timeout. Treatment
reverses the order ONLY on the already-eligible path (raw-CDP first, then one bounded Playwright
fallback), each primitive attempted at most once. Missing/false/malformed/error flag, missing run
identity, and every ineligible path resolve to control.
"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.browser_engine import SKYCDP_ENGINE_NAME, BrowserEngineSelection
from skyvern.webeye.browser_errors import BrowserTargetClosedError
from skyvern.webeye.utils import page as page_module
from skyvern.webeye.utils.page import (
    SCREENSHOT_CDP_FIRST_FLAG,
    ScreenshotArm,
    _current_viewpoint_screenshot_helper,
)

_PNG = None


def _png_bytes() -> str:
    global _PNG
    if _PNG is None:
        buffer = BytesIO()
        with Image.new("RGB", (80, 60), (20, 70, 180)) as image:
            image.save(buffer, format="PNG")
        _PNG = base64.b64encode(buffer.getvalue()).decode()
    return _PNG


def _page() -> MagicMock:
    page = MagicMock(spec=Page)
    page.is_closed.return_value = False
    page.url = "https://example.test/screenshot"
    page.viewport_size = {"width": 80, "height": 60}
    page.context.browser.browser_type.name = "chromium"
    page.wait_for_load_state = AsyncMock()
    page.screenshot = AsyncMock(side_effect=PlaywrightTimeoutError("waiting for fonts to load"))
    session = AsyncMock()

    async def send(method: str, params: dict) -> dict:
        if method == "Runtime.evaluate":
            return {"result": {"value": {"deviceScaleFactor": 1, "viewportScale": 1}}}
        return {"data": _png_bytes()}

    session.send.side_effect = send
    page.context.new_cdp_session = AsyncMock(return_value=session)
    return page


def _persistent_page() -> MagicMock:
    """Production-shaped launch_persistent_context page: a Chromium context whose owning Browser handle
    is None (Playwright/Patchright expose none for persistent contexts), CDP still reachable through
    context.new_cdp_session."""
    page = _page()
    page.context.browser = None
    return page


def _provider(variant: Any) -> MagicMock:
    provider = MagicMock()
    if isinstance(variant, Exception):
        provider.get_value_cached = AsyncMock(side_effect=variant)
    else:
        provider.get_value_cached = AsyncMock(return_value=variant)
    return provider


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "BROWSER_CURSOR_VISUALIZATION", False)
    for name in ("SESSION", "CAPTURE", "DETACH"):
        monkeypatch.setattr(page_module, f"CDP_RESCUE_{name}_TIMEOUT_SECONDS", 0.05)


def _use_provider(monkeypatch: pytest.MonkeyPatch, variant: Any) -> MagicMock:
    provider = _provider(variant)
    monkeypatch.setattr(page_module.app, "EXPERIMENTATION_PROVIDER", provider)
    return provider


class _FakeClock:
    """Deterministic monotonic seam; advanced only inside mock side effects, never via wall-clock sleep."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(page_module, "_monotonic", fake)
    return fake


def _observations(log: MagicMock) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for level in ("debug", "info", "warning", "error"):
        for log_call in getattr(log, level).call_args_list:
            if "screenshot.outcome" in log_call.kwargs:
                events.append(log_call.kwargs)
    return events


# --- fail-closed default control ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_context_is_control(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _use_provider(monkeypatch, ScreenshotArm.TREATMENT.value)
    page = _page()
    # timeout -> control rescue produces bytes
    assert await _current_viewpoint_screenshot_helper(page) is not None
    provider.get_value_cached.assert_not_awaited()  # no run identity => never queried
    page.screenshot.assert_awaited_once()  # Playwright ran first (control)


@pytest.mark.asyncio
async def test_missing_page_context_preserves_control_capture_and_terminal_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    page.context = None
    page.screenshot.side_effect = [PlaywrightTimeoutError("first capture timed out"), b"control-retry"]
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page, timeout=137)
    assert result == b"control-retry"
    provider.get_value_cached.assert_not_awaited()
    assert page.screenshot.await_args_list == [
        call(path=None, timeout=137, full_page=False, animations="disabled"),
        call(path=None, timeout=137, full_page=False, animations="allow"),
    ]
    observations = _observations(log)
    assert all(o["screenshot.arm"] == "control" for o in observations)
    timed_out = [o for o in observations if o["screenshot.outcome"] == "timeout"]
    assert [o["screenshot.eligibility"] for o in timed_out] == ["ineligible_browser"]
    terminal = [o for o in observations if o["screenshot.stage"] == "terminal"]
    assert [(o["screenshot.primitive"], o["screenshot.outcome"]) for o in terminal] == [("playwright", "success")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant", [None, False, True, 1, {}, "control", "off", "malformed", "TREATMENT", PlaywrightError("boom")]
)
async def test_non_treatment_or_error_flag_is_control(variant: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_provider(monkeypatch, variant)
    page = _page()
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1", organization_id="o_1")):
        await _current_viewpoint_screenshot_helper(page)
    # Control: Playwright attempted first (it timed out, then rescued via CDP).
    page.screenshot.assert_awaited_once()
    page.context.new_cdp_session.assert_awaited_once()


# --- treatment ordering ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_treatment_cdp_first_success_skips_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_provider(monkeypatch, "treatment")
    page = _page()
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page)
    assert base64.b64encode(result).decode() == _png_bytes()
    page.context.new_cdp_session.assert_awaited_once()
    page.screenshot.assert_not_awaited()  # CDP produced bytes first; Playwright never ran


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_engine", [False, True])
async def test_treatment_cdp_success_then_target_closes_during_detach_is_target_closed(
    pin_engine: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CDP capture can produce bytes, then the target closes during the synchronously awaited detach.
    A screenshot of a dead target is not a success: the rescued bytes must not be returned, the outer
    result is ScreenshotTargetClosed, terminal telemetry is target_closed, and success is not recorded.
    The terminal record must hold even under a pinned (non-None) engine selection — the production norm —
    so treatment and control stay symmetric for the identical physical race."""
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    record_success = MagicMock()
    monkeypatch.setattr(page_module.skyvern_context, "record_browser_success", record_success)
    selection = None
    if pin_engine:
        # Realistic driver binding: is_engine_error recognizes only native driver errors (not the
        # skyvern-family ScreenshotTargetClosed), which is exactly what bypassed terminal mapping.
        selection = MagicMock(spec=BrowserEngineSelection)
        selection.name = "rustwright"
        selection.is_engine_error.side_effect = lambda exc: isinstance(exc, PlaywrightError)
        selection.is_engine_timeout_error.side_effect = lambda exc: isinstance(exc, PlaywrightTimeoutError)
        selection.classify_error.return_value = None
    page = _page()
    session = page.context.new_cdp_session.return_value

    async def closing_detach() -> None:
        page.is_closed.return_value = True  # target closes during the owned detach

    session.detach.side_effect = closing_detach
    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")),
        pytest.raises(page_module.ScreenshotTargetClosed),
    ):
        await _current_viewpoint_screenshot_helper(page, engine_selection=selection)
    session.detach.assert_awaited_once()  # detach awaited once; no retry/new session
    page.screenshot.assert_not_awaited()  # CDP produced bytes; no Playwright fallback
    record_success.assert_not_called()  # stale bytes are not counted as a success
    terminal = [o for o in _observations(log) if o["screenshot.stage"] == "terminal"]
    assert [(o["screenshot.arm"], o["screenshot.primitive"], o["screenshot.outcome"]) for o in terminal] == [
        ("treatment", "cdp_rescue", "target_closed")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_engine", [False, True])
async def test_treatment_cdp_failure_that_closes_target_skips_fallback(
    pin_engine: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CDP stage failure can classify target_closed and return None with the page already dead. The
    Playwright fallback must not run on a closed target: the post-rescue closure recheck fires before the
    bytes/fallback branch, so the outer result is ScreenshotTargetClosed with a single terminal
    target_closed record, under both None and a realistic pinned engine selection."""
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    record_success = MagicMock()
    monkeypatch.setattr(page_module.skyvern_context, "record_browser_success", record_success)
    selection = None
    if pin_engine:
        selection = MagicMock(spec=BrowserEngineSelection)
        selection.name = "rustwright"
        selection.is_engine_error.side_effect = lambda exc: isinstance(exc, PlaywrightError)
        selection.is_engine_timeout_error.side_effect = lambda exc: isinstance(exc, PlaywrightTimeoutError)
        selection.classify_error.return_value = None
    page = _page()
    session = page.context.new_cdp_session.return_value

    async def failing_send(method: str, params: dict) -> dict:
        page.is_closed.return_value = True  # target dies mid-CDP, before any bytes
        raise PlaywrightError("cdp down")

    session.send.side_effect = failing_send
    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")),
        pytest.raises(page_module.ScreenshotTargetClosed),
    ):
        await _current_viewpoint_screenshot_helper(page, engine_selection=selection)
    page.screenshot.assert_not_awaited()  # fallback never runs on a dead target
    record_success.assert_not_called()
    session.detach.assert_awaited_once()  # created session detached once; no retry/new session
    terminal = [o for o in _observations(log) if o["screenshot.stage"] == "terminal"]
    assert [(o["screenshot.arm"], o["screenshot.primitive"], o["screenshot.outcome"]) for o in terminal] == [
        ("treatment", "cdp_rescue", "target_closed")
    ]


_RESCUE_CAPTURED_EVENT = "Raw CDP rescue screenshot captured"


@pytest.mark.asyncio
async def test_treatment_cdp_success_logs_debug_not_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routine treatment CDP success is a per-capture event; it must land at DEBUG (not the indexed
    INFO tier) while still carrying the treatment arm on the stable event name."""
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page)
    assert base64.b64encode(result).decode() == _png_bytes()
    assert not [c for c in log.info.call_args_list if c.args and c.args[0] == _RESCUE_CAPTURED_EVENT]
    assert any(
        c.args[0] == _RESCUE_CAPTURED_EVENT and c.kwargs["screenshot.arm"] == "treatment"
        for c in log.debug.call_args_list
    )


@pytest.mark.asyncio
async def test_control_rescue_success_still_logs_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control rescue success stays exceptional (only after a Playwright timeout) and keeps INFO."""
    _use_provider(monkeypatch, None)
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page)
    assert base64.b64encode(result).decode() == _png_bytes()
    page.screenshot.assert_awaited_once()  # control: Playwright first (timed out), then CDP rescue
    assert any(
        c.args[0] == _RESCUE_CAPTURED_EVENT and c.kwargs["screenshot.arm"] == "control" for c in log.info.call_args_list
    )
    assert not [c for c in log.debug.call_args_list if c.args and c.args[0] == _RESCUE_CAPTURED_EVENT]


_RESCUE_DECLINED_EVENT = "Raw CDP rescue screenshot declined scaled viewport"


@pytest.mark.asyncio
async def test_treatment_scaled_viewport_decline_logs_debug_not_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under treatment with a persistent scaled viewport, the CDP decline fires on every capture; it must
    land at DEBUG (not the indexed INFO tier) with the treatment arm, and still fall back exactly once."""
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = None
    session.send.return_value = {"result": {"value": {"deviceScaleFactor": 2, "viewportScale": 1}}}  # scaled -> decline
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"pw-fallback"
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page)
    assert result == b"pw-fallback"
    page.screenshot.assert_awaited_once()  # scaled decline -> exactly one Playwright fallback
    assert not [c for c in log.info.call_args_list if c.args and c.args[0] == _RESCUE_DECLINED_EVENT]
    assert any(
        c.args[0] == _RESCUE_DECLINED_EVENT and c.kwargs["screenshot.arm"] == "treatment"
        for c in log.debug.call_args_list
    )


@pytest.mark.asyncio
async def test_control_scaled_viewport_decline_still_logs_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control scaled decline stays exceptional (only after a Playwright timeout) and keeps INFO, then
    follows the existing control animation retry."""
    _use_provider(monkeypatch, None)
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = None
    session.send.return_value = {"result": {"value": {"deviceScaleFactor": 2, "viewportScale": 1}}}  # scaled -> decline
    page.screenshot.side_effect = [PlaywrightTimeoutError("first capture timed out"), b"pw-retry"]
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page)
    assert result == b"pw-retry"  # control: timeout -> scaled decline -> animation retry
    assert any(
        c.args[0] == _RESCUE_DECLINED_EVENT and c.kwargs["screenshot.arm"] == "control" for c in log.info.call_args_list
    )
    assert not [c for c in log.debug.call_args_list if c.args and c.args[0] == _RESCUE_DECLINED_EVENT]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["declined", "cdp_error"])
async def test_treatment_falls_back_to_single_playwright(failure: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value
    if failure == "declined":
        session.send.side_effect = None
        session.send.return_value = {"result": {"value": {"deviceScaleFactor": 2, "viewportScale": 1}}}
    else:
        session.send.side_effect = PlaywrightError("CDP failure")
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"pw-fallback"
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page)
    assert result == b"pw-fallback"
    page.context.new_cdp_session.assert_awaited_once()
    page.screenshot.assert_awaited_once()  # exactly one Playwright fallback
    assert page.screenshot.await_args.kwargs["animations"] == "disabled"
    attempts = [o for o in _observations(log) if o["screenshot.primitive"] == "playwright"]
    assert [(o["screenshot.stage"], o["screenshot.outcome"]) for o in attempts] == [("capture", "success")]
    assert attempts[0] in [log_call.kwargs for log_call in log.debug.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,outcome,exception_type",
    [
        (PlaywrightTimeoutError("fallback timed out"), "timeout", page_module.FailedToTakeScreenshot),
        (PlaywrightError("fallback failed"), "error", page_module.FailedToTakeScreenshot),
        (
            PlaywrightError("Target page, context or browser has been closed"),
            "target_closed",
            page_module.ScreenshotTargetClosed,
        ),
    ],
)
async def test_treatment_fallback_failure_preserves_terminal_attribution_and_attempt_limits(
    failure: Exception, outcome: str, exception_type: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = PlaywrightError("CDP failure")
    page.screenshot.side_effect = failure
    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")),
        pytest.raises(exception_type) as raised,
    ):
        await _current_viewpoint_screenshot_helper(page)
    assert page.context.new_cdp_session.await_count == 1  # no second CDP session
    assert page.screenshot.await_count == 1  # no Playwright retry
    assert raised.value.__cause__ is failure
    observations = _observations(log)
    terminal = [o for o in observations if o["screenshot.stage"] == "terminal"]
    assert [(o["screenshot.arm"], o["screenshot.primitive"], o["screenshot.outcome"]) for o in terminal] == [
        ("treatment", "cdp_rescue", outcome)
    ]
    assert all(o["screenshot.arm"] == "treatment" for o in observations)
    attempts = [o for o in observations if o["screenshot.primitive"] == "playwright"]
    assert [(o["screenshot.stage"], o["screenshot.outcome"]) for o in attempts] == [("capture", outcome)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,outcome,exception_type",
    [
        (RuntimeError("native capture deadline"), "timeout", page_module.FailedToTakeScreenshot),
        (RuntimeError("target was disposed"), "target_closed", page_module.ScreenshotTargetClosed),
    ],
)
async def test_treatment_fallback_uses_selected_engine_error_classification(
    failure: Exception,
    outcome: str,
    exception_type: type[Exception],
    clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    selection = MagicMock(spec=BrowserEngineSelection)
    selection.name = "rustwright"
    selection.is_engine_error.side_effect = lambda exc: exc is failure
    selection.is_engine_timeout_error.side_effect = lambda exc: exc is failure and outcome == "timeout"
    selection.classify_error.side_effect = lambda exc: (
        BrowserTargetClosedError(str(exc)) if exc is failure and outcome == "target_closed" else None
    )
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = PlaywrightError("CDP failure")
    page.screenshot.side_effect = failure
    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")),
        pytest.raises(exception_type) as raised,
    ):
        await _current_viewpoint_screenshot_helper(page, timeout=137, engine_selection=selection)
    assert raised.value.__cause__ is failure
    page.context.new_cdp_session.assert_awaited_once()
    session.detach.assert_awaited_once()
    page.screenshot.assert_awaited_once()
    # Non-advancing fake clock: the full 137 ms budget remains for the single fallback (approx: the
    # deadline round-trips through monotonic seconds, so 137 is not bit-exact).
    assert page.screenshot.await_args.kwargs == {
        "path": None,
        "timeout": pytest.approx(137),
        "full_page": False,
        "animations": "disabled",
    }
    observations = _observations(log)
    terminal = [o for o in observations if o["screenshot.stage"] == "terminal"]
    assert [(o["screenshot.arm"], o["screenshot.primitive"], o["screenshot.outcome"]) for o in terminal] == [
        ("treatment", "cdp_rescue", outcome)
    ]
    attempts = [o for o in observations if o["screenshot.primitive"] == "playwright"]
    assert [(o["screenshot.arm"], o["screenshot.stage"], o["screenshot.outcome"]) for o in attempts] == [
        ("treatment", "capture", outcome)
    ]
    # Telemetry clamps the budget to an integer, and the seconds round-trip can drop the last ms.
    assert attempts[0]["screenshot.timeout_budget_ms"] == pytest.approx(137, abs=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,selection_kind,expected",
    [
        (PlaywrightError("Target page, context or browser has been closed"), "none", "target_closed"),
        (PlaywrightError("Target was disposed"), "closed", "target_closed"),
        (PlaywrightTimeoutError("cdp send timed out"), "none", "timeout"),
        (RuntimeError("native capture deadline"), "timeout", "timeout"),
        (PlaywrightError("some cdp explosion"), "none", "error"),
    ],
)
async def test_treatment_cdp_rescue_failure_outcome_classification(
    exc: Exception, selection_kind: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CDP rescue's own per-primitive telemetry classifies a selected-engine target closure as
    target_closed (so it no longer reports a spurious error when the Playwright fallback then reports
    target_closed), and a native selected-engine timeout as timeout; Python/Playwright timeout and
    generic error classifications are unchanged."""
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    selection = None
    if selection_kind != "none":
        selection = MagicMock(spec=BrowserEngineSelection)
        selection.name = "rustwright"
        selection.is_engine_error.side_effect = lambda e: e is exc
        selection.is_engine_timeout_error.side_effect = lambda e: e is exc and selection_kind == "timeout"
        selection.classify_error.side_effect = lambda e: (
            BrowserTargetClosedError(str(e)) if e is exc and selection_kind == "closed" else None
        )
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = exc
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"pw-fallback"
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page, engine_selection=selection)
    assert result == b"pw-fallback"  # the one bounded Playwright fallback still runs
    page.context.new_cdp_session.assert_awaited_once()  # exactly one CDP attempt
    page.screenshot.assert_awaited_once()  # exactly one bounded Playwright fallback
    cdp_failures = [
        o
        for o in _observations(log)
        if o["screenshot.primitive"] == "cdp_rescue" and o["screenshot.outcome"] != "success"
    ]
    assert cdp_failures, "expected a failed CDP rescue observation"
    assert all(o["screenshot.outcome"] == expected for o in cdp_failures)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detach_exc,kind,expected",
    [
        (PlaywrightError("Target was disposed"), "target_closed", "target_closed"),
        (RuntimeError("native detach deadline"), "timeout", "timeout"),
        (PlaywrightError("detach exploded"), "error", "error"),
    ],
)
async def test_treatment_cdp_detach_failure_outcome_classification(
    detach_exc: Exception, kind: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detach failure telemetry classifies with the same precedence as capture/rescue: a selected-engine
    target closure is target_closed, a native/builtin timeout is timeout, else error. The page itself
    stays open, so the successful CDP capture result is returned unaffected in every case."""
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    selection = None
    if kind != "error":
        selection = MagicMock(spec=BrowserEngineSelection)
        selection.name = "rustwright"
        selection.is_engine_error.side_effect = lambda e: e is detach_exc
        selection.is_engine_timeout_error.side_effect = lambda e: e is detach_exc and kind == "timeout"
        selection.classify_error.side_effect = lambda e: (
            BrowserTargetClosedError(str(e)) if e is detach_exc and kind == "target_closed" else None
        )
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.detach.side_effect = detach_exc
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page, engine_selection=selection)
    assert base64.b64encode(result).decode() == _png_bytes()  # page stays open; capture result unaffected
    page.screenshot.assert_not_awaited()  # CDP-first success, no Playwright fallback
    session.detach.assert_awaited_once()  # exactly one detach attempt
    detach_obs = [o for o in _observations(log) if o["screenshot.stage"] == "detach"]
    assert [o["screenshot.outcome"] for o in detach_obs] == [expected]
    assert all(o["screenshot.primitive"] == "cdp_rescue" and o["screenshot.arm"] == "treatment" for o in detach_obs)


# --- treatment one-deadline budget ----------------------------------------------------------------


def test_deadline_remaining_seconds_arithmetic(clock: _FakeClock) -> None:
    assert page_module._deadline_remaining_seconds(None) == float("inf")
    assert page_module._deadline_remaining_seconds(clock.now + 3.0) == 3.0
    assert page_module._deadline_remaining_seconds(clock.now - 1.0) == -1.0
    # min() clips to the stage maximum when the budget is looser and to the budget when it is tighter;
    # None (control) keeps the fixed maximum, a spent budget clips nonpositive.
    assert min(5, page_module._deadline_remaining_seconds(None)) == 5
    assert min(5, page_module._deadline_remaining_seconds(clock.now + 2.0)) == 2.0
    assert min(5, page_module._deadline_remaining_seconds(clock.now - 0.1)) < 0


@pytest.mark.asyncio
async def test_treatment_fallback_receives_only_remaining_budget(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value

    async def failing_send(method: str, params: dict) -> dict:
        clock.advance(2.0)
        raise PlaywrightError("cdp down")

    session.send.side_effect = failing_send
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"pw-fallback"
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page, timeout=20000)
    assert result == b"pw-fallback"
    assert page.screenshot.await_args.kwargs["timeout"] == 18000.0  # 20000 - 2000 elapsed
    fallback = [
        o
        for o in _observations(log)
        if o["screenshot.primitive"] == "playwright" and o["screenshot.stage"] == "capture"
    ]
    assert [o["screenshot.timeout_budget_ms"] for o in fallback] == [18000.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_engine", [False, True])
async def test_treatment_deadline_exhausted_after_cdp_skips_fallback_and_maps_timeout(
    pin_engine: bool, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    selection = None
    if pin_engine:
        selection = MagicMock(spec=BrowserEngineSelection)
        selection.name = "rustwright"
        selection.is_engine_error.return_value = False
        selection.is_engine_timeout_error.return_value = False
        selection.classify_error.return_value = None
    page = _page()
    session = page.context.new_cdp_session.return_value

    async def failing_send(method: str, params: dict) -> dict:
        clock.advance(25.0)
        raise PlaywrightError("cdp down")

    session.send.side_effect = failing_send
    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")),
        pytest.raises(page_module.FailedToTakeScreenshot),
    ):
        await _current_viewpoint_screenshot_helper(page, timeout=20000, engine_selection=selection)
    page.screenshot.assert_not_awaited()  # deadline exhausted -> no Playwright RPC
    observations = _observations(log)
    terminal = [o for o in observations if o["screenshot.stage"] == "terminal"]
    assert [(o["screenshot.arm"], o["screenshot.primitive"], o["screenshot.outcome"]) for o in terminal] == [
        ("treatment", "cdp_rescue", "timeout")
    ]
    skipped = [
        o for o in observations if o["screenshot.primitive"] == "playwright" and o["screenshot.stage"] == "capture"
    ]
    assert [o["screenshot.timeout_budget_ms"] for o in skipped] == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -5])
async def test_treatment_nonpositive_timeout_short_circuits_without_browser_io(
    timeout: float, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value
    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")),
        pytest.raises(page_module.FailedToTakeScreenshot),
    ):
        await _current_viewpoint_screenshot_helper(page, timeout=timeout)
    page.context.new_cdp_session.assert_not_awaited()  # no CDP attach RPC
    page.screenshot.assert_not_awaited()  # no Playwright RPC
    session.detach.assert_not_awaited()  # no session created -> no cleanup
    terminal = [o for o in _observations(log) if o["screenshot.stage"] == "terminal"]
    assert [o["screenshot.outcome"] for o in terminal] == ["timeout"]


@pytest.mark.asyncio
async def test_cdp_stage_timeouts_capped_by_remaining_deadline(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value

    async def slow_attach(_page_arg: Any) -> Any:
        clock.advance(25.0)  # exhaust the caller budget during attach
        return session

    page.context.new_cdp_session.side_effect = slow_attach
    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")),
        pytest.raises(page_module.FailedToTakeScreenshot),
    ):
        await _current_viewpoint_screenshot_helper(page, timeout=20000)
    page.context.new_cdp_session.assert_awaited_once()  # session WAS created
    session.send.assert_not_awaited()  # capture pre-guard tripped before any RPC
    session.detach.assert_awaited_once()  # created session still detached
    page.screenshot.assert_not_awaited()  # deadline exhausted -> fallback skipped
    rescue_geometry = [
        o
        for o in _observations(log)
        if o["screenshot.primitive"] == "cdp_rescue" and o["screenshot.stage"] == "geometry"
    ]
    assert [o["screenshot.outcome"] for o in rescue_geometry] == ["timeout"]


@pytest.mark.asyncio
async def test_control_rescue_is_unchanged_by_deadline_plumbing(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, "control")
    clock.advance(100.0)  # a deadline leaked into control would be exhausted here
    page = _page()
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page, timeout=1)
    assert base64.b64encode(result).decode() == _png_bytes()
    page.screenshot.assert_awaited_once()  # control: Playwright first (timed out)
    page.context.new_cdp_session.assert_awaited_once()  # then unbounded CDP rescue returns bytes


@pytest.mark.asyncio
async def test_detach_cleanup_tail_survives_deadline_exhaustion(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_provider(monkeypatch, "treatment")
    page = _page()
    session = page.context.new_cdp_session.return_value

    async def failing_send(method: str, params: dict) -> dict:
        clock.advance(25.0)
        raise PlaywrightError("cdp down")

    session.send.side_effect = failing_send
    existing = asyncio.all_tasks()
    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")),
        pytest.raises(page_module.FailedToTakeScreenshot),
    ):
        await _current_viewpoint_screenshot_helper(page, timeout=20000)
    session.detach.assert_awaited_once()  # cleanup ownership preserved despite exhausted budget
    page.screenshot.assert_not_awaited()
    assert not (asyncio.all_tasks() - existing)  # no leaked/backgrounded cleanup task


@pytest.mark.asyncio
async def test_detach_elapsed_is_charged_to_remaining_budget(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detach is synchronously awaited before the fallback budget is computed, so its elapsed time is
    charged to the caller deadline. Here CDP fails with budget still remaining, but the owned detach
    spends the rest of it -> the fallback is truthfully skipped, detach is still awaited exactly once,
    no task leaks, and the terminal maps to a screenshot timeout."""
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value

    async def failing_send(method: str, params: dict) -> dict:
        raise PlaywrightError("cdp down")  # fails immediately, leaving the caller budget intact

    async def slow_detach() -> None:
        clock.advance(25.0)  # owned cleanup spends the remaining caller budget

    session.send.side_effect = failing_send
    session.detach.side_effect = slow_detach
    existing = asyncio.all_tasks()
    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")),
        pytest.raises(page_module.FailedToTakeScreenshot),
    ):
        await _current_viewpoint_screenshot_helper(page, timeout=20000)
    session.detach.assert_awaited_once()  # cleanup ownership completes, not deferred
    page.screenshot.assert_not_awaited()  # detach consumed the budget -> fallback skipped
    assert not (asyncio.all_tasks() - existing)  # no leaked/backgrounded cleanup task
    terminal = [o for o in _observations(log) if o["screenshot.stage"] == "terminal"]
    assert [(o["screenshot.arm"], o["screenshot.primitive"], o["screenshot.outcome"]) for o in terminal] == [
        ("treatment", "cdp_rescue", "timeout")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["full_page", "firefox", "skycdp"],
)
async def test_treatment_flag_ignored_on_ineligible_path(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _use_provider(monkeypatch, "treatment")
    page = _page()
    if kind == "firefox":
        # A present, non-Chromium Browser (connect_over_cdp shape) stays ineligible — the Chromium gate
        # survives the persistent-context null-safety fix.
        page.context.browser.browser_type.name = "firefox"
    selection = None
    if kind == "skycdp":
        selection = SimpleNamespace(
            name=SKYCDP_ENGINE_NAME,
            is_engine_error=lambda exc: isinstance(exc, PlaywrightError),
            is_engine_timeout_error=lambda exc: isinstance(exc, PlaywrightTimeoutError),
        )
    page.screenshot.side_effect = [PlaywrightTimeoutError("first"), b"playwright-retry"]
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(
            page, full_page=kind == "full_page", engine_selection=selection
        )
    assert result == b"playwright-retry"
    # Ineligible: arm resolves to control without ever consulting the provider.
    provider.get_value_cached.assert_not_awaited()
    page.context.new_cdp_session.assert_not_awaited()


# --- persistent-context (no owning Browser) eligibility -------------------------------------------


@pytest.mark.asyncio
async def test_persistent_context_treatment_reaches_cdp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A launch_persistent_context page (context.browser is None) is raw-CDP eligible: forced treatment
    resolves the flag and attempts raw CDP first, exactly like the connect_over_cdp path. This is the
    production-dominant stealth-Chromium shape that the browser-None gate silently excluded."""
    provider = _use_provider(monkeypatch, "treatment")
    page = _persistent_page()
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"pw-should-not-run"
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page)
    assert base64.b64encode(result).decode() == _png_bytes()  # raw CDP produced the bytes
    provider.get_value_cached.assert_awaited_once()  # eligible -> flag resolved
    page.context.new_cdp_session.assert_awaited_once()  # raw CDP attempted first
    page.screenshot.assert_not_awaited()  # CDP produced bytes; Playwright never ran


@pytest.mark.asyncio
async def test_persistent_context_control_uses_cdp_rescue_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy control-arm CDP rescue also applies to persistent contexts: after the Playwright
    capture times out, the raw-CDP rescue runs through the context (no owning Browser needed) instead of
    falling through to the animation retry."""
    _use_provider(monkeypatch, None)  # control
    page = _persistent_page()
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        result = await _current_viewpoint_screenshot_helper(page)
    assert base64.b64encode(result).decode() == _png_bytes()  # rescued over raw CDP
    page.screenshot.assert_awaited_once()  # Playwright first (timed out)
    page.context.new_cdp_session.assert_awaited_once()  # then CDP rescue via the context


# --- arm observability & assignment ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_treatment_arm_is_observable_on_attempt_and_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        await _current_viewpoint_screenshot_helper(page)
    arms = {o["screenshot.arm"] for o in _observations(log)}
    assert arms == {"treatment"}


@pytest.mark.asyncio
async def test_treatment_fallback_arm_is_observable(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_provider(monkeypatch, "treatment")
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = PlaywrightError("CDP failure")
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"pw-fallback"
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        await _current_viewpoint_screenshot_helper(page)
    obs = _observations(log)
    assert any(o["screenshot.arm"] == "treatment" and o["screenshot.stage"] == "terminal" for o in obs)
    assert all(o["screenshot.arm"] == "treatment" for o in obs)


@pytest.mark.asyncio
async def test_stable_assignment_uses_run_id_and_does_not_oscillate(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _use_provider(monkeypatch, "treatment")
    provider.get_value_cached.side_effect = ["treatment", "control", None]
    with skyvern_context.scoped(
        SkyvernContext(workflow_run_id="wr_1", organization_id="o_1", workflow_permanent_id="wpid_1")
    ):
        for _ in range(3):
            fresh = _page()
            await _current_viewpoint_screenshot_helper(fresh)
            fresh.context.new_cdp_session.assert_awaited_once()  # always CDP-first, no oscillation
            fresh.screenshot.assert_not_awaited()
    provider.get_value_cached.assert_awaited_once_with(
        SCREENSHOT_CDP_FIRST_FLAG,
        "wr_1",
        properties={"organization_id": "o_1", "workflow_permanent_id": "wpid_1"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_value", [None, False, "malformed", PlaywrightError("provider unavailable")])
async def test_control_assignment_including_provider_errors_is_pinned(
    first_value: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _use_provider(monkeypatch, None)
    provider.get_value_cached.side_effect = [first_value, "treatment"]
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        for _ in range(3):
            page = _page()
            await _current_viewpoint_screenshot_helper(page)
            page.screenshot.assert_awaited_once()
            page.context.new_cdp_session.assert_awaited_once()
    provider.get_value_cached.assert_awaited_once_with(SCREENSHOT_CDP_FIRST_FLAG, "wr_1", properties={})


@pytest.mark.asyncio
async def test_concurrent_first_captures_share_one_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _use_provider(monkeypatch, None)
    entered = asyncio.Event()
    release = asyncio.Event()
    all_started = asyncio.Event()
    started = 0
    resolutions = 0
    pages = [_page() for _ in range(4)]

    async def resolve(*args: object, **kwargs: object) -> str:
        nonlocal resolutions
        resolutions += 1
        variant = "treatment" if resolutions == 1 else "control"
        entered.set()
        await release.wait()
        return variant

    async def capture(page: MagicMock) -> bytes:
        nonlocal started
        started += 1
        if started == len(pages):
            all_started.set()
        return await _current_viewpoint_screenshot_helper(page)

    provider.get_value_cached.side_effect = resolve
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        tasks = [asyncio.create_task(capture(page)) for page in pages]
        try:
            await asyncio.wait_for(entered.wait(), timeout=0.5)
            await asyncio.wait_for(all_started.wait(), timeout=0.5)
            assert resolutions == 1
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert all(base64.b64encode(result).decode() == _png_bytes() for result in results)
    for page in pages:
        page.screenshot.assert_not_awaited()
    provider.get_value_cached.assert_awaited_once_with(SCREENSHOT_CDP_FIRST_FLAG, "wr_1", properties={})


@pytest.mark.asyncio
async def test_changed_distinct_id_resolves_again_with_current_targeting(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _use_provider(monkeypatch, None)
    provider.get_value_cached.side_effect = ["treatment", "control"]
    context = SkyvernContext(organization_id="o_1")
    with skyvern_context.scoped(context):
        for identity, workflow, expected_arm in [("wr_1", "wpid_1", "treatment"), ("wr_2", "wpid_2", "control")]:
            context.workflow_run_id = identity
            context.workflow_permanent_id = workflow
            for _ in range(2):
                page = _page()
                await _current_viewpoint_screenshot_helper(page)
                assert page.screenshot.await_count == (expected_arm == "control")
    assert provider.get_value_cached.await_args_list == [
        call(
            SCREENSHOT_CDP_FIRST_FLAG, "wr_1", properties={"organization_id": "o_1", "workflow_permanent_id": "wpid_1"}
        ),
        call(
            SCREENSHOT_CDP_FIRST_FLAG, "wr_2", properties={"organization_id": "o_1", "workflow_permanent_id": "wpid_2"}
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "organization_id,workflow_permanent_id,properties",
    [
        (None, None, {}),
        ("", "", {}),
        (" ", "\t", {}),
        ("o_1", None, {"organization_id": "o_1"}),
        (None, "wpid_1", {"workflow_permanent_id": "wpid_1"}),
    ],
)
async def test_targeting_uses_only_non_empty_authoritative_properties(
    organization_id: str | None,
    workflow_permanent_id: str | None,
    properties: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _use_provider(monkeypatch, "treatment")
    context = SkyvernContext(
        workflow_run_id="wr_1",
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_id="version_id_is_not_permanent_id",
        root_workflow_run_id="parent_run_is_not_current_run",
    )
    with skyvern_context.scoped(context):
        await _current_viewpoint_screenshot_helper(_page())
    provider.get_value_cached.assert_awaited_once_with(SCREENSHOT_CDP_FIRST_FLAG, "wr_1", properties=properties)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ids,expected_id",
    [
        ({"workflow_run_id": "wr_1", "task_id": "tsk_1", "task_v2_id": "tsk_v2_1", "run_id": "run_1"}, "wr_1"),
        ({"task_id": "tsk_1", "task_v2_id": "tsk_v2_1", "run_id": "run_1"}, "tsk_1"),
        ({"task_v2_id": "tsk_v2_1", "run_id": "run_1"}, "tsk_v2_1"),
        ({"run_id": "run_1"}, "run_1"),
        ({}, None),
    ],
)
async def test_assignment_uses_execution_identity_precedence(
    ids: dict[str, str], expected_id: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _use_provider(monkeypatch, "treatment")
    with skyvern_context.scoped(SkyvernContext(**ids)):
        await _current_viewpoint_screenshot_helper(_page())
    if expected_id is None:
        provider.get_value_cached.assert_not_awaited()
    else:
        provider.get_value_cached.assert_awaited_once_with(SCREENSHOT_CDP_FIRST_FLAG, expected_id, properties={})


@pytest.mark.asyncio
async def test_cancelled_resolution_does_not_pin_control_or_block_the_next_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _use_provider(monkeypatch, None)
    entered = asyncio.Event()

    async def resolve(*args: object, **kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    provider.get_value_cached.side_effect = resolve
    cancelled_page = _page()
    existing_tasks = asyncio.all_tasks()
    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
        task = asyncio.create_task(_current_viewpoint_screenshot_helper(cancelled_page))
        try:
            await asyncio.wait_for(entered.wait(), timeout=0.5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)
        provider.get_value_cached.side_effect = None
        provider.get_value_cached.return_value = "treatment"
        page = _page()
        await asyncio.wait_for(_current_viewpoint_screenshot_helper(page), timeout=0.5)
    cancelled_page.screenshot.assert_not_awaited()
    cancelled_page.context.new_cdp_session.assert_not_awaited()
    page.screenshot.assert_not_awaited()
    page.context.new_cdp_session.assert_awaited_once()
    assert provider.get_value_cached.await_count == 2
    assert not (asyncio.all_tasks() - existing_tasks)


# --- helper boolean contract ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("cdp_first,expected", [(True, b"cdp"), (False, b"control")])
async def test_page_screenshot_helper_boolean_selects_capture_path(
    cdp_first: bool, expected: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp = AsyncMock(return_value=b"cdp")
    control = AsyncMock(return_value=b"control")
    monkeypatch.setattr(page_module, "_cdp_first_screenshot", cdp)
    monkeypatch.setattr(page_module, "_control_screenshot", control)
    result = await page_module._page_screenshot_helper(_page(), cdp_first=cdp_first)
    assert result == expected
    assert cdp.await_count == int(cdp_first)
    assert control.await_count == int(not cdp_first)


@pytest.mark.asyncio
async def test_page_screenshot_helper_defaults_to_control(monkeypatch: pytest.MonkeyPatch) -> None:
    cdp = AsyncMock(return_value=b"cdp")
    control = AsyncMock(return_value=b"control")
    monkeypatch.setattr(page_module, "_cdp_first_screenshot", cdp)
    monkeypatch.setattr(page_module, "_control_screenshot", control)
    result = await page_module._page_screenshot_helper(_page())
    assert result == b"control"
    cdp.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_during_treatment_cdp_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_provider(monkeypatch, "treatment")
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

    page.context.new_cdp_session.side_effect = hang
    existing = asyncio.all_tasks()

    async def run() -> None:
        with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_1")):
            await _current_viewpoint_screenshot_helper(page)

    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    assert finished.is_set()
    assert not (asyncio.all_tasks() - existing)
    page.screenshot.assert_not_awaited()
    _ = session
