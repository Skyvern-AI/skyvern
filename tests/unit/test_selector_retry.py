"""Tests for _wait_for_selector_with_retry in SkyvernPage.

Verifies that the retry mechanism:
- Returns the locator immediately when the element exists
- Retries when the element is not found (simulating page redirect/slow render)
- Gives up after max_retries and raises
- Only retries on element-not-found (TimeoutError), not on other errors
- Re-acquires the locator between retries (handles full DOM replacement)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


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
async def test_element_found_immediately(mock_page):
    """When the element exists, return the locator on the first try — no retries."""
    locator = _make_locator()
    mock_page._locator_scope.locator.return_value = locator

    result = await mock_page._wait_for_selector_with_retry(
        "#my-button",
        timeout=1000,
        max_retries=2,
        retry_interval=0.01,
    )

    assert result is locator
    locator.wait_for.assert_awaited_once_with(state="attached", timeout=1000)


@pytest.mark.asyncio
async def test_element_found_after_one_retry(mock_page):
    """Element missing on first try, found on second — simulates slow page load."""
    locator_miss = _make_locator(wait_for_side_effect=PlaywrightTimeoutError("Timeout"))
    locator_hit = _make_locator()

    mock_page._locator_scope.locator.side_effect = [locator_miss, locator_hit]

    result = await mock_page._wait_for_selector_with_retry(
        "#password-field",
        timeout=1000,
        max_retries=2,
        retry_interval=0.01,
    )

    assert result is locator_hit
    assert mock_page._locator_scope.locator.call_count == 2


@pytest.mark.asyncio
async def test_element_found_after_two_retries(mock_page):
    """Element missing on first two tries, found on third (the last retry)."""
    locator_miss1 = _make_locator(wait_for_side_effect=PlaywrightTimeoutError("Timeout"))
    locator_miss2 = _make_locator(wait_for_side_effect=PlaywrightTimeoutError("Timeout"))
    locator_hit = _make_locator()

    mock_page._locator_scope.locator.side_effect = [locator_miss1, locator_miss2, locator_hit]

    result = await mock_page._wait_for_selector_with_retry(
        "#sign-in",
        timeout=1000,
        max_retries=2,
        retry_interval=0.01,
    )

    assert result is locator_hit
    assert mock_page._locator_scope.locator.call_count == 3


@pytest.mark.asyncio
async def test_all_retries_exhausted_raises(mock_page):
    """When element is never found, raise after all retries are exhausted."""
    locator = _make_locator(wait_for_side_effect=PlaywrightTimeoutError("Timeout"))
    mock_page._locator_scope.locator.return_value = locator

    with pytest.raises(PlaywrightTimeoutError):
        await mock_page._wait_for_selector_with_retry(
            "#nonexistent",
            timeout=1000,
            max_retries=2,
            retry_interval=0.01,
        )

    # Initial attempt + 2 retries = 3 locator acquisitions
    assert mock_page._locator_scope.locator.call_count == 3


@pytest.mark.asyncio
async def test_zero_retries_fails_immediately(mock_page):
    """With max_retries=0, no retries — fail on first miss."""
    locator = _make_locator(wait_for_side_effect=PlaywrightTimeoutError("Timeout"))
    mock_page._locator_scope.locator.return_value = locator

    with pytest.raises(PlaywrightTimeoutError):
        await mock_page._wait_for_selector_with_retry(
            "#gone",
            timeout=1000,
            max_retries=0,
            retry_interval=0.01,
        )

    assert mock_page._locator_scope.locator.call_count == 1


@pytest.mark.asyncio
async def test_navigation_context_error_retried(mock_page):
    """Transient navigation errors ('execution context destroyed') trigger retry."""
    locator_miss = _make_locator(wait_for_side_effect=Exception("Execution context was destroyed"))
    locator_hit = _make_locator()

    mock_page._locator_scope.locator.side_effect = [locator_miss, locator_hit]

    result = await mock_page._wait_for_selector_with_retry(
        "#target",
        timeout=1000,
        max_retries=1,
        retry_interval=0.01,
    )

    assert result is locator_hit


@pytest.mark.asyncio
async def test_non_transient_error_not_retried(mock_page):
    """Non-transient errors (browser crashed, page closed) are NOT retried."""
    locator = _make_locator(wait_for_side_effect=Exception("Browser has been closed"))
    mock_page._locator_scope.locator.return_value = locator

    with pytest.raises(Exception, match="Browser has been closed"):
        await mock_page._wait_for_selector_with_retry(
            "#target",
            timeout=1000,
            max_retries=2,
            retry_interval=0.01,
        )

    # Should fail on first attempt — no retries for non-transient errors
    assert mock_page._locator_scope.locator.call_count == 1


@pytest.mark.asyncio
async def test_locator_reacquired_between_retries(mock_page):
    """The locator is re-acquired from the DOM on each retry.

    When a page navigates (e.g. SSO redirect), the entire DOM is replaced.
    A stale locator from the old page won't find elements on the new page.
    """
    locator_old = _make_locator(wait_for_side_effect=PlaywrightTimeoutError("Timeout"))
    locator_new = _make_locator()

    mock_page._locator_scope.locator.side_effect = [locator_old, locator_new]

    result = await mock_page._wait_for_selector_with_retry(
        "#passwd",
        timeout=1000,
        max_retries=1,
        retry_interval=0.01,
    )

    # Should be the NEW locator, not the old one
    assert result is locator_new
    # Both calls should use the same selector
    calls = mock_page._locator_scope.locator.call_args_list
    assert all(c.args[0] == "#passwd" for c in calls)


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
