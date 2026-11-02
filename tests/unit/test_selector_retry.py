"""Tests for _wait_for_selector_with_retry in SkyvernPage.

Covers which errors a selected engine retries, and that code v1 or a missing context disables retries.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class SelectedEngineError(Exception):
    pass


class SelectedEngineTimeout(SelectedEngineError):
    pass


def _selected_engine():
    selection = MagicMock()
    selection.is_engine_timeout_error.side_effect = lambda exc: isinstance(exc, SelectedEngineTimeout)
    return selection


def _mock_skyvern_context(code_version=2):
    """Create a mock SkyvernContext with the given code_version."""
    ctx = MagicMock()
    ctx.code_version = code_version
    return ctx


@pytest.fixture
def mock_page():
    """Create a minimal mock SkyvernPage with the retry method.

    Patches skyvern_context.current() to return code_version=2 so retries are enabled.
    """
    from skyvern.core.script_generations.skyvern_page import SkyvernPage

    page = MagicMock(spec=SkyvernPage)
    page._locator_scope = MagicMock()
    page.engine_selection = None

    # Bind the real method to our mock
    page._wait_for_selector_with_retry = SkyvernPage._wait_for_selector_with_retry.__get__(page)

    # Patch skyvern_context.current() to return code_version=2 so retries are enabled
    with patch(
        "skyvern.core.script_generations.skyvern_page.skyvern_context.current",
        return_value=_mock_skyvern_context(code_version=2),
    ):
        yield page


def _make_locator(wait_for_side_effect=None):
    """Create a mock locator with configurable wait_for behavior."""
    locator = AsyncMock()
    locator.first = locator  # .first returns self for chaining
    if wait_for_side_effect:
        locator.wait_for = AsyncMock(side_effect=wait_for_side_effect)
    else:
        locator.wait_for = AsyncMock()  # succeeds immediately
    return locator


@pytest.mark.asyncio
async def test_selected_engine_native_timeout_is_retried(mock_page):
    locator_miss = _make_locator(wait_for_side_effect=SelectedEngineTimeout("Timeout"))
    locator_hit = _make_locator()
    mock_page.engine_selection = _selected_engine()
    mock_page._locator_scope.locator.side_effect = [locator_miss, locator_hit]

    result = await mock_page._wait_for_selector_with_retry(
        "#target",
        timeout=1000,
        max_retries=1,
        retry_interval=0.01,
    )

    assert result is locator_hit


@pytest.mark.asyncio
async def test_selected_engine_foreign_timeout_is_not_retried(mock_page):
    locator = _make_locator(wait_for_side_effect=PlaywrightTimeoutError("Timeout"))
    mock_page.engine_selection = _selected_engine()
    mock_page._locator_scope.locator.return_value = locator

    with pytest.raises(PlaywrightTimeoutError):
        await mock_page._wait_for_selector_with_retry(
            "#target",
            timeout=1000,
            max_retries=1,
            retry_interval=0.01,
        )

    assert mock_page._locator_scope.locator.call_count == 1


@pytest.mark.asyncio
async def test_execution_context_error_remains_transient_with_selected_engine(mock_page):
    locator_miss = _make_locator(wait_for_side_effect=RuntimeError("Execution context was destroyed"))
    locator_hit = _make_locator()
    mock_page.engine_selection = _selected_engine()
    mock_page._locator_scope.locator.side_effect = [locator_miss, locator_hit]

    result = await mock_page._wait_for_selector_with_retry(
        "#target",
        timeout=1000,
        max_retries=1,
        retry_interval=0.01,
    )

    assert result is locator_hit


@pytest.mark.asyncio
async def test_no_retry_for_code_v1():
    """Code version 1 scripts should NOT retry — fail immediately."""
    from skyvern.core.script_generations.skyvern_page import SkyvernPage

    page = MagicMock(spec=SkyvernPage)
    page._locator_scope = MagicMock()
    page._wait_for_selector_with_retry = SkyvernPage._wait_for_selector_with_retry.__get__(page)

    locator = _make_locator(wait_for_side_effect=PlaywrightTimeoutError("Timeout"))
    page._locator_scope.locator.return_value = locator

    with patch(
        "skyvern.core.script_generations.skyvern_page.skyvern_context.current",
        return_value=_mock_skyvern_context(code_version=1),
    ):
        with pytest.raises(PlaywrightTimeoutError):
            await page._wait_for_selector_with_retry(
                "#element",
                timeout=1000,
                max_retries=2,
                retry_interval=0.01,
            )

    # Should only try once (no retries for v1)
    assert page._locator_scope.locator.call_count == 1


@pytest.mark.asyncio
async def test_no_retry_without_context():
    """When there's no SkyvernContext (e.g. non-script execution), no retries."""
    from skyvern.core.script_generations.skyvern_page import SkyvernPage

    page = MagicMock(spec=SkyvernPage)
    page._locator_scope = MagicMock()
    page._wait_for_selector_with_retry = SkyvernPage._wait_for_selector_with_retry.__get__(page)

    locator = _make_locator(wait_for_side_effect=PlaywrightTimeoutError("Timeout"))
    page._locator_scope.locator.return_value = locator

    with patch(
        "skyvern.core.script_generations.skyvern_page.skyvern_context.current",
        return_value=None,
    ):
        with pytest.raises(PlaywrightTimeoutError):
            await page._wait_for_selector_with_retry(
                "#element",
                timeout=1000,
                max_retries=2,
                retry_interval=0.01,
            )

    assert page._locator_scope.locator.call_count == 1
