"""Structured, low-cardinality screenshot telemetry: arm/primitive/stage/outcome must be truthful.

These tests fix the observability contract; they must not constrain capture selection or retry, which
``test_screenshot_cdp_fallback.py`` owns.
"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.webeye.browser_engine import SKYCDP_ENGINE_NAME
from skyvern.webeye.utils import page as page_module
from skyvern.webeye.utils.page import (
    ScreenshotArm,
    ScreenshotEligibility,
    ScreenshotOutcome,
    ScreenshotPrimitive,
    ScreenshotStage,
    _current_viewpoint_screenshot_helper,
    _screenshot_observation_fields,
)


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

    async def send(method: str, params: dict) -> dict:
        if method == "Runtime.evaluate":
            return {"result": {"value": {"deviceScaleFactor": 1, "viewportScale": 1}}}
        return {"data": base64.b64encode(buffer.getvalue()).decode()}

    session.send.side_effect = send
    page.context.new_cdp_session = AsyncMock(return_value=session)
    return page


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "BROWSER_CURSOR_VISUALIZATION", False)
    for name in ("SESSION", "CAPTURE", "DETACH"):
        monkeypatch.setattr(page_module, f"CDP_RESCUE_{name}_TIMEOUT_SECONDS", 0.05)


def _log_events(log: MagicMock) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for level in ("debug", "info", "warning", "error"):
        for call in getattr(log, level).call_args_list:
            events.append(call.kwargs)
    return events


def _observations(log: MagicMock) -> list[dict[str, Any]]:
    return [kwargs for kwargs in _log_events(log) if "screenshot.outcome" in kwargs]


# --- pure builder ---------------------------------------------------------------------------------


def test_observation_fields_are_bounded_and_truthful() -> None:
    fields = _screenshot_observation_fields(
        arm=ScreenshotArm.CONTROL,
        primitive=ScreenshotPrimitive.CDP_RESCUE,
        stage=ScreenshotStage.CAPTURE,
        outcome=ScreenshotOutcome.SUCCESS,
        elapsed_ms=-5,
        timeout_budget_ms=1234.7,
        eligibility=ScreenshotEligibility.ELIGIBLE,
    )
    assert fields == {
        "screenshot.arm": "control",
        "screenshot.primitive": "cdp_rescue",
        "screenshot.stage": "capture",
        "screenshot.outcome": "success",
        "screenshot.elapsed_ms": 0,
        "screenshot.timeout_budget_ms": 1234,
        "screenshot.eligibility": "eligible",
    }


def test_observation_fields_omit_optional_measures_when_absent() -> None:
    fields = _screenshot_observation_fields(
        arm=ScreenshotArm.CONTROL,
        primitive=ScreenshotPrimitive.PLAYWRIGHT,
        stage=ScreenshotStage.TERMINAL,
        outcome=ScreenshotOutcome.ERROR,
    )
    assert set(fields) == {
        "screenshot.arm",
        "screenshot.primitive",
        "screenshot.stage",
        "screenshot.outcome",
    }


# --- CDP rescue stage matrix ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_rescue_success_reports_validation_after_file_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    path = tmp_path / "nested" / "screenshot.png"
    result = await _current_viewpoint_screenshot_helper(page, file_path=str(path))
    assert path.read_bytes() == result
    cdp = [o for o in _observations(log) if o.get("screenshot.primitive") == "cdp_rescue"]
    assert [(o["screenshot.arm"], o["screenshot.stage"], o["screenshot.outcome"]) for o in cdp] == [
        ("control", "validation", "success")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["attach", "geometry", "capture", "validation"])
@pytest.mark.parametrize(
    "failure,expected_outcome",
    [
        (TimeoutError("deadline exceeded"), "timeout"),
        (PlaywrightTimeoutError("protocol timeout"), "timeout"),
        (PlaywrightError("protocol failure"), "error"),
    ],
)
async def test_cdp_rescue_failure_reports_stage_and_outcome(
    stage: str, failure: Exception, expected_outcome: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    original_timeout = page.screenshot.side_effect
    session = page.context.new_cdp_session.return_value
    if stage == "attach":
        page.context.new_cdp_session.side_effect = failure
    elif stage in {"geometry", "capture"}:

        async def fail(method: str, params: dict) -> dict:
            if stage == "geometry" or method == "Page.captureScreenshot":
                raise failure
            return {"result": {"value": {"deviceScaleFactor": 1, "viewportScale": 1}}}

        session.send.side_effect = fail
    else:
        monkeypatch.setattr(Path, "write_bytes", MagicMock(side_effect=failure))

    with pytest.raises(page_module.FailedToTakeScreenshot) as raised:
        await _current_viewpoint_screenshot_helper(page, file_path=str(tmp_path / "screenshot.png"))
    assert raised.value.__cause__ is original_timeout
    cdp = [o for o in _observations(log) if o.get("screenshot.primitive") == "cdp_rescue"]
    assert [(o["screenshot.stage"], o["screenshot.outcome"]) for o in cdp] == [(stage, expected_outcome)]


@pytest.mark.asyncio
async def test_cdp_rescue_invalid_png_reports_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value

    async def bad_png(method: str, params: dict) -> dict:
        if method == "Runtime.evaluate":
            return {"result": {"value": {"deviceScaleFactor": 1, "viewportScale": 1}}}
        return {"data": base64.b64encode(b"not-a-png").decode()}

    session.send.side_effect = bad_png
    with pytest.raises(page_module.FailedToTakeScreenshot):
        await _current_viewpoint_screenshot_helper(page)
    cdp = [o for o in _observations(log) if o.get("screenshot.primitive") == "cdp_rescue"]
    assert [(o["screenshot.stage"], o["screenshot.outcome"]) for o in cdp] == [("validation", "error")]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stage", [None, "capture", "detach"])
@pytest.mark.parametrize("failure", ["timeout", "error"])
async def test_detach_failure_is_observable_without_changing_capture_or_cancellation(
    cancel_stage: str | None, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value
    capture_entered = asyncio.Event()
    detach_entered = asyncio.Event()
    release_detach = asyncio.Event()
    detach_finished = asyncio.Event()

    async def capture(*args: object, **kwargs: object) -> None:
        capture_entered.set()
        await asyncio.Event().wait()

    async def detach() -> None:
        detach_entered.set()
        try:
            if failure == "timeout":
                await asyncio.Event().wait()
            else:
                await release_detach.wait()
                raise PlaywrightError("detach failed")
        finally:
            detach_finished.set()

    if cancel_stage == "capture":
        session.send.side_effect = capture
    session.detach.side_effect = detach
    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(_current_viewpoint_screenshot_helper(page))
    try:
        if cancel_stage == "capture":
            await asyncio.wait_for(capture_entered.wait(), timeout=0.5)
            task.cancel()
        await asyncio.wait_for(detach_entered.wait(), timeout=0.5)
        if cancel_stage == "detach":
            task.cancel()
        release_detach.set()
        if cancel_stage is not None:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)
        else:
            assert (await asyncio.wait_for(task, timeout=0.5)).startswith(page_module._PNG_SIGNATURE)
        assert detach_finished.is_set()
        assert not (asyncio.all_tasks() - existing_tasks)
        detach_observations = [o for o in _observations(log) if o.get("screenshot.stage") == "detach"]
        assert len(detach_observations) == 1
        observation = detach_observations[0]
        assert observation["screenshot.arm"] == "control"
        assert observation["screenshot.primitive"] == "cdp_rescue"
        assert observation["screenshot.outcome"] == failure
        assert observation["screenshot.timeout_budget_ms"] == 50
        assert observation["screenshot.elapsed_ms"] >= 0
    finally:
        release_detach.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_scaled_viewport_decline_emits_declined_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = None
    session.send.return_value = {"result": {"value": {"deviceScaleFactor": 2, "viewportScale": 1}}}
    page.screenshot.side_effect = [PlaywrightTimeoutError("first"), b"animation-retry-bytes"]
    assert await _current_viewpoint_screenshot_helper(page) == b"animation-retry-bytes"
    cdp = [o for o in _observations(log) if o.get("screenshot.primitive") == "cdp_rescue"]
    assert any(o["screenshot.outcome"] == "declined" and o["screenshot.stage"] == "geometry" for o in cdp)


# --- eligibility ----------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,reason",
    [
        ("full_page", "ineligible_full_page"),
        ("firefox", "ineligible_browser"),
        ("skycdp", "ineligible_engine"),
    ],
)
async def test_ineligible_paths_emit_decline_reason(kind: str, reason: str, monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    if kind == "firefox":
        page.context.browser.browser_type.name = "firefox"
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
    eligibilities = {o.get("screenshot.eligibility") for o in _observations(log)}
    assert reason in eligibilities


@pytest.mark.asyncio
async def test_eligible_timeout_records_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    await _current_viewpoint_screenshot_helper(page)
    eligibilities = {o.get("screenshot.eligibility") for o in _observations(log)}
    assert "eligible" in eligibilities


# --- terminal outcome -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_success_emits_playwright_success(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    page.screenshot.side_effect = None
    page.screenshot.return_value = b"ordinary-frame"
    assert await _current_viewpoint_screenshot_helper(page, timeout=123) == b"ordinary-frame"
    terminal = [o for o in _observations(log) if o.get("screenshot.stage") == "terminal"]
    assert any(
        o["screenshot.outcome"] == "success"
        and o["screenshot.arm"] == "control"
        and o["screenshot.timeout_budget_ms"] == 123
        for o in terminal
    )


@pytest.mark.asyncio
async def test_terminal_timeout_emits_timeout_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(page_module, "LOG", log)
    page = _page()
    session = page.context.new_cdp_session.return_value
    session.send.side_effect = PlaywrightError("CDP failure")
    with pytest.raises(page_module.FailedToTakeScreenshot):
        await _current_viewpoint_screenshot_helper(page)
    terminal = [o for o in _observations(log) if o.get("screenshot.stage") == "terminal"]
    assert any(o["screenshot.outcome"] == "timeout" for o in terminal)
