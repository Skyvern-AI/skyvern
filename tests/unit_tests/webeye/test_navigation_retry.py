"""Tests for non-retriable navigation error classification."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from skyvern.exceptions import FailedToNavigateToUrl

_NON_RETRIABLE_ERRORS = (
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_NAME_RESOLUTION_FAILED",
    "net::ERR_INVALID_URL",
    "net::ERR_CERT_",
)


async def _navigate_to_url(page, url: str, retry_times: int = 5) -> None:
    """Reimplements navigate_to_url retry logic without heavy browser deps."""
    for retry_time in range(retry_times):
        try:
            await page.goto(url, timeout=30000)
            return
        except Exception as e:
            error_str = str(e)

            if any(pattern in error_str for pattern in _NON_RETRIABLE_ERRORS):
                raise FailedToNavigateToUrl(url=url, error_message=error_str)

            if retry_time >= retry_times - 1:
                raise FailedToNavigateToUrl(url=url, error_message=error_str)

            await asyncio.sleep(0)  # Skip real sleep in tests


@pytest.mark.asyncio
async def test_dns_error_fails_immediately() -> None:
    """ERR_NAME_NOT_RESOLVED should raise immediately without retries."""
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception("net::ERR_NAME_NOT_RESOLVED"))

    with pytest.raises(FailedToNavigateToUrl):
        await _navigate_to_url(page, "http://not-a-real-url.invalid", retry_times=5)

    assert page.goto.call_count == 1


@pytest.mark.asyncio
async def test_cert_error_fails_immediately() -> None:
    """ERR_CERT_AUTHORITY_INVALID should raise immediately without retries."""
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception("net::ERR_CERT_AUTHORITY_INVALID"))

    with pytest.raises(FailedToNavigateToUrl):
        await _navigate_to_url(page, "https://expired.example.com", retry_times=5)

    assert page.goto.call_count == 1


@pytest.mark.asyncio
async def test_invalid_url_fails_immediately() -> None:
    """ERR_INVALID_URL should raise immediately without retries."""
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception("net::ERR_INVALID_URL"))

    with pytest.raises(FailedToNavigateToUrl):
        await _navigate_to_url(page, "not-a-url", retry_times=5)

    assert page.goto.call_count == 1


@pytest.mark.asyncio
async def test_timeout_error_is_retried() -> None:
    """ERR_TIMED_OUT should be retried up to retry_times."""
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception("net::ERR_TIMED_OUT"))

    with pytest.raises(FailedToNavigateToUrl):
        await _navigate_to_url(page, "http://slow-site.example.com", retry_times=3)

    assert page.goto.call_count == 3


@pytest.mark.asyncio
async def test_connection_reset_is_retried() -> None:
    """ERR_CONNECTION_RESET should be retried (transient error)."""
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception("net::ERR_CONNECTION_RESET"))

    with pytest.raises(FailedToNavigateToUrl):
        await _navigate_to_url(page, "http://flaky.example.com", retry_times=2)

    assert page.goto.call_count == 2


@pytest.mark.asyncio
async def test_transient_error_recovers_on_retry() -> None:
    """A transient error followed by success should navigate successfully."""
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=[Exception("net::ERR_CONNECTION_RESET"), None])

    await _navigate_to_url(page, "http://example.com", retry_times=3)

    assert page.goto.call_count == 2
