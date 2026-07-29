"""Tests for _wait_for_upload_processing helper and regression guards."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from unittest.mock import AsyncMock, patch

import pytest
from playwright._impl._errors import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.webeye.actions import handler as handler_module
from skyvern.webeye.actions.handler import _wait_for_upload_processing
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection


class _EngineError(Exception):
    pass


class _EngineTimeout(_EngineError):
    pass


async def _never_start():  # pragma: no cover - never awaited
    raise AssertionError("start_driver must not be called")


def _engine_selection() -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name="engine-a",
        start_driver=_never_start,
        error_type=_EngineError,
        timeout_error_type=_EngineTimeout,
        metadata=BrowserEngineMetadata(name="engine-a", version="0.0.0"),
        selection_reason="test",
    )


async def _run_with_page_ready_error(error: BaseException, engine_selection: BrowserEngineSelection | None) -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = error
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        await _wait_for_upload_processing(AsyncMock(), engine_selection=engine_selection)


# ---------------------------------------------------------------------------
# Helper behaviour tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calls_wait_for_page_ready_with_settle_delay() -> None:
    mock_frame = AsyncMock()
    with (
        patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create,
        patch("skyvern.webeye.actions.handler.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        mock_create.return_value = mock_frame
        await _wait_for_upload_processing(AsyncMock())

    # Settle delay before readiness polling
    mock_sleep.assert_awaited_once_with(0.5)
    mock_frame.wait_for_page_ready.assert_awaited_once_with(
        loading_indicator_timeout_ms=3000,
        network_idle_timeout_ms=3000,
        dom_stable_ms=300,
        dom_stability_timeout_ms=2000,
    )


@pytest.mark.asyncio
async def test_swallows_playwright_timeout() -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = PlaywrightTimeoutError("Timeout 3000ms exceeded")
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_swallows_asyncio_timeout() -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = asyncio.TimeoutError()
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_swallows_page_analysis_timeout_during_frame_creation() -> None:
    with patch(
        "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
        new_callable=AsyncMock,
        side_effect=SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page"),
    ):
        await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_swallows_playwright_error() -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = PlaywrightError("Target page, context or browser has been closed")
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_propagates_non_playwright_error() -> None:
    mock_frame = AsyncMock()
    mock_frame.wait_for_page_ready.side_effect = RuntimeError("unexpected bug")
    with patch("skyvern.webeye.actions.handler.SkyvernFrame.create_instance", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_frame
        with pytest.raises(RuntimeError, match="unexpected bug"):
            await _wait_for_upload_processing(AsyncMock())


@pytest.mark.asyncio
async def test_swallows_selected_engine_timeout() -> None:
    # A non-stock engine's native timeout must be tolerated like the stock Playwright timeout.
    await _run_with_page_ready_error(_EngineTimeout("deadline exceeded"), _engine_selection())


@pytest.mark.asyncio
async def test_swallows_selected_engine_error() -> None:
    await _run_with_page_ready_error(_EngineError("target closed"), _engine_selection())


@pytest.mark.asyncio
async def test_propagates_foreign_playwright_error_under_selected_engine() -> None:
    # Under a pinned non-stock engine, a stock Playwright error is foreign and must propagate,
    # not be swallowed as an upload-processing tolerance.
    with pytest.raises(PlaywrightError):
        await _run_with_page_ready_error(PlaywrightError("navigated away"), _engine_selection())


@pytest.mark.asyncio
async def test_propagates_foreign_playwright_timeout_under_selected_engine() -> None:
    with pytest.raises(PlaywrightTimeoutError):
        await _run_with_page_ready_error(PlaywrightTimeoutError("pw timeout"), _engine_selection())


# ---------------------------------------------------------------------------
# Static regression guards
# ---------------------------------------------------------------------------


def _get_all_sleep_calls(source: str) -> list[tuple[int, float | str]]:
    results = []
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        is_sleep = (
            isinstance(func, ast.Attribute)
            and func.attr == "sleep"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
        if is_sleep and call.args:
            arg = call.args[0]
            if isinstance(arg, ast.Constant):
                results.append((node.lineno, arg.value))
    return results


def test_no_fixed_sleep_10_in_handle_upload_file_action() -> None:
    # Regression guard: this function previously blocked the agent for 10s unconditionally.
    source = inspect.getsource(handler_module.handle_upload_file_action)
    for lineno, value in _get_all_sleep_calls(source):
        assert value != 10, f"Found asyncio.sleep(10) at line {lineno}"


def test_no_fixed_sleep_15_in_chain_click() -> None:
    # Regression guard: this function previously blocked the agent for 15s unconditionally.
    source = inspect.getsource(handler_module.chain_click)
    for lineno, value in _get_all_sleep_calls(source):
        assert value != 15, f"Found asyncio.sleep(15) at line {lineno}"
