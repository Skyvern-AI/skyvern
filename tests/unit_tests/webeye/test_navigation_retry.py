from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from skyvern.exceptions import FailedToNavigateToUrl
from skyvern.webeye.navigation import navigate_with_retry
from skyvern.webeye.real_browser_state import RealBrowserState


@pytest.mark.parametrize(
    "error_message",
    [
        pytest.param("net::ERR_NAME_NOT_RESOLVED", id="dns-not-resolved"),
        pytest.param("net::ERR_NAME_RESOLUTION_FAILED", id="dns-resolution-failed"),
        pytest.param("net::ERR_INVALID_URL", id="invalid-url"),
        pytest.param("net::ERR_CERT_AUTHORITY_INVALID", id="cert-authority-invalid"),
        pytest.param("net::ERR_CERT_DATE_INVALID", id="cert-date-invalid"),
        pytest.param("net::ERR_SSL_PROTOCOL_ERROR", id="ssl-protocol-error"),
    ],
)
@pytest.mark.asyncio
async def test_skip_inner_retry_error_fails_immediately(error_message: str) -> None:
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=Exception(error_message))
    settle = AsyncMock()
    sleep = AsyncMock()

    with pytest.raises(FailedToNavigateToUrl):
        await navigate_with_retry(
            navigate=lambda strategy: page.goto("http://example.invalid", timeout=30000, wait_until=strategy),
            url="http://example.invalid",
            retry_times=5,
            settle=settle,
            sleep=sleep,
        )

    assert page.goto.call_count == 1
    settle.assert_not_awaited()
    sleep.assert_not_awaited()


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
    settle = AsyncMock()
    sleep = AsyncMock()

    with pytest.raises(FailedToNavigateToUrl):
        await navigate_with_retry(
            navigate=lambda strategy: page.goto("http://example.com", timeout=30000, wait_until=strategy),
            url="http://example.com",
            retry_times=retry_times,
            settle=settle,
            sleep=sleep,
        )

    assert page.goto.call_count == retry_times
    assert sleep.await_count == retry_times - 1
    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_error_recovers_on_retry() -> None:
    page = AsyncMock()
    page.goto = AsyncMock(side_effect=[Exception("net::ERR_CONNECTION_RESET"), None])
    settle = AsyncMock()
    sleep = AsyncMock()

    await navigate_with_retry(
        navigate=lambda strategy: page.goto("http://example.com", timeout=30000, wait_until=strategy),
        url="http://example.com",
        retry_times=3,
        settle=settle,
        sleep=sleep,
    )

    assert page.goto.call_count == 2
    assert sleep.await_count == 1
    settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_page_does_not_retry_permanent_failed_navigation() -> None:
    browser_state = RealBrowserState(pw=AsyncMock())
    browser_state.get_working_page = AsyncMock(return_value=None)
    browser_state.check_and_fix_state = AsyncMock(
        side_effect=FailedToNavigateToUrl(
            url="http://example.invalid",
            error_message="net::ERR_INVALID_URL",
        )
    )
    browser_state.close_current_open_page = AsyncMock(return_value=True)

    with pytest.raises(FailedToNavigateToUrl):
        await browser_state.get_or_create_page(url="http://example.invalid")

    assert browser_state.check_and_fix_state.await_count == 1
    browser_state.close_current_open_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_create_page_retries_dns_error_with_context_recreation() -> None:
    page = AsyncMock()
    browser_state = RealBrowserState(pw=AsyncMock())
    browser_state.get_working_page = AsyncMock(side_effect=[None, page])
    browser_state.check_and_fix_state = AsyncMock(
        side_effect=[
            FailedToNavigateToUrl(
                url="http://example.com",
                error_message="net::ERR_NAME_NOT_RESOLVED",
            ),
            None,
        ]
    )
    browser_state.close_current_open_page = AsyncMock(return_value=True)
    browser_state.validate_browser_context = AsyncMock(return_value=True)
    browser_state._RealBrowserState__assert_page = AsyncMock(return_value=page)

    result = await browser_state.get_or_create_page(url="http://example.com")

    assert result is page
    assert browser_state.check_and_fix_state.await_count == 2
    browser_state.close_current_open_page.assert_awaited_once()
    browser_state.validate_browser_context.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_get_or_create_page_retries_retriable_failed_navigation() -> None:
    page = AsyncMock()
    browser_state = RealBrowserState(pw=AsyncMock())
    browser_state.get_working_page = AsyncMock(side_effect=[None, page])
    browser_state.check_and_fix_state = AsyncMock(
        side_effect=[
            FailedToNavigateToUrl(
                url="http://example.com",
                error_message="net::ERR_CONNECTION_RESET",
            ),
            None,
        ]
    )
    browser_state.close_current_open_page = AsyncMock(return_value=True)
    browser_state.validate_browser_context = AsyncMock(return_value=True)
    browser_state._RealBrowserState__assert_page = AsyncMock(return_value=page)

    result = await browser_state.get_or_create_page(url="http://example.com")

    assert result is page
    assert browser_state.check_and_fix_state.await_count == 2
    browser_state.close_current_open_page.assert_awaited_once()
    browser_state.validate_browser_context.assert_awaited_once_with(page)
