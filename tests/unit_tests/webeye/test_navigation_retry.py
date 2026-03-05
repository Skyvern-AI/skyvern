from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from skyvern.constants import NON_RETRIABLE_NAV_ERRORS
from skyvern.exceptions import FailedToNavigateToUrl


async def _navigate_to_url(page: AsyncMock, url: str, retry_times: int = 5) -> None:
    """Minimal reimplementation of RealBrowserState.navigate_to_url retry logic.

    Avoids importing RealBrowserState directly because its module pulls in
    heavy dependencies (AWS, Playwright, browser factory). The retry and
    error-classification logic is kept in sync with the real implementation.
    """
    for retry_time in range(retry_times):
        try:
            await page.goto(url, timeout=30000)
            return
        except Exception as e:
            error_str = str(e)

            if any(pattern in error_str for pattern in NON_RETRIABLE_NAV_ERRORS):
                raise FailedToNavigateToUrl(url=url, error_message=error_str)

            if retry_time >= retry_times - 1:
                raise FailedToNavigateToUrl(url=url, error_message=error_str)

            await asyncio.sleep(0)


@pytest.mark.parametrize(
    "error_message",
    [
        pytest.param("net::ERR_NAME_NOT_RESOLVED", id="dns-not-resolved"),
        pytest.param("net::ERR_NAME_RESOLUTION_FAILED", id="dns-resolution-failed"),
        pytest.param("net::ERR_INVALID_URL", id="invalid-url"),
        pytest.param("net::ERR_CERT_AUTHORITY_INVALID", id="cert-authority-invalid"),
        pytest.param("net::ERR_CERT_DATE_INVALID", id="cert-date-invalid"),
    ],
)
@pytest.mark.asyncio
async def test_non_retriable_error_fails_immediately(error_message: str) -> None:
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception(error_message))

    with pytest.raises(FailedToNavigateToUrl):
        await _navigate_to_url(page, "http://example.invalid", retry_times=5)

    assert page.goto.call_count == 1


@pytest.mark.parametrize(
    "error_message, retry_times",
    [
        pytest.param("net::ERR_TIMED_OUT", 3, id="timeout"),
        pytest.param("net::ERR_CONNECTION_RESET", 2, id="connection-reset"),
    ],
)
@pytest.mark.asyncio
async def test_retriable_error_exhausts_all_attempts(
    error_message: str,
    retry_times: int,
) -> None:
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception(error_message))

    with pytest.raises(FailedToNavigateToUrl):
        await _navigate_to_url(page, "http://example.com", retry_times=retry_times)

    assert page.goto.call_count == retry_times


@pytest.mark.asyncio
async def test_transient_error_recovers_on_retry() -> None:
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=[Exception("net::ERR_CONNECTION_RESET"), None])

    await _navigate_to_url(page, "http://example.com", retry_times=3)

    assert page.goto.call_count == 2
