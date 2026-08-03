from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from skyvern.exceptions import BlockedNavigationDestination, FailedToNavigateToUrl
from skyvern.webeye.navigation import navigate_with_retry, validate_navigation_destination
from skyvern.webeye.real_browser_state import RealBrowserState

# Internal, loopback, link-local, metadata, and local-file targets that must fail closed.
BLOCKED_DESTINATIONS = [
    pytest.param("file:///etc/passwd", id="local-file"),
    pytest.param("file://localhost/etc/shadow", id="local-file-host"),
    pytest.param("http://169.254.169.254/latest/meta-data/", id="cloud-metadata-ip"),
    pytest.param("http://169.254.1.1/", id="link-local"),
    pytest.param("http://192.168.0.10/admin", id="private-192"),
    pytest.param("http://10.0.0.5/", id="private-10"),
    pytest.param("http://127.0.0.1:9000/", id="loopback-ip"),
    pytest.param("http://localhost:8000/admin", id="localhost"),
    pytest.param("http://metadata.google.internal/computeMetadata/v1/", id="metadata-hostname"),
    pytest.param("http://kubernetes.default.svc/api", id="cluster-internal-svc"),
    # Numeric-IP and backslash-authority forms the browser normalizes to an internal host
    # even though stdlib urlparse / ipaddress do not (WHATWG canonicalization catches them).
    pytest.param("http://2130706433/", id="decimal-ip-loopback"),
    pytest.param("http://0177.0.0.1/", id="octal-ip-loopback"),
    pytest.param("http://0x7f.0.0.1/", id="hex-ip-loopback"),
    pytest.param("http://0xa9fea9fe/", id="hex-ip-metadata"),
    pytest.param("http://169.254.43518/", id="shortened-ip-metadata"),
    pytest.param(r"http://169.254.169.254\@example.com/", id="backslash-authority-metadata"),
    pytest.param(r"http:\\169.254.169.254/", id="all-backslash-authority"),
]


class _FakeRequest:
    """Duck-types the slice of playwright.async_api.Request that redirect revalidation reads."""

    def __init__(self, url: str, redirected_from: _FakeRequest | None = None) -> None:
        self.url = url
        self.redirected_from = redirected_from


class _FakeResponse:
    def __init__(self, request: _FakeRequest) -> None:
        self.request = request


def _redirect_response(*urls: str) -> _FakeResponse:
    """Build a page.goto-style response whose redirect chain visited ``urls`` in order."""
    request: _FakeRequest | None = None
    for url in urls:
        request = _FakeRequest(url, redirected_from=request)
    assert request is not None
    return _FakeResponse(request)


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


@pytest.mark.parametrize("url", BLOCKED_DESTINATIONS)
def test_validate_navigation_destination_rejects_internal_and_local_targets(url: str) -> None:
    with pytest.raises(BlockedNavigationDestination):
        validate_navigation_destination(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path",
        "http://example.com/",
        "https://sub.example.co.uk/a?b=c",
        "example.com",  # scheme-less public hosts are still allowed (https is prepended)
    ],
)
def test_validate_navigation_destination_allows_public_targets(url: str) -> None:
    validate_navigation_destination(url)


@pytest.mark.parametrize("url", BLOCKED_DESTINATIONS)
@pytest.mark.asyncio
async def test_navigate_with_retry_blocks_internal_and_local_before_dispatch(url: str) -> None:
    navigate = AsyncMock()
    settle = AsyncMock()
    sleep = AsyncMock()

    with pytest.raises(BlockedNavigationDestination):
        await navigate_with_retry(navigate=navigate, url=url, retry_times=3, settle=settle, sleep=sleep)

    navigate.assert_not_awaited()  # rejected before any request is dispatched
    settle.assert_not_awaited()
    sleep.assert_not_awaited()


# Malformed authorities (unterminated IPv6 literals) that make stdlib urlparse raise a raw
# ValueError; the guard must convert that to BlockedNavigationDestination before dispatch.
MALFORMED_DESTINATIONS = [
    pytest.param("http://[", id="unterminated-ipv6-bracket"),
    pytest.param("http://[::1", id="unclosed-ipv6-literal"),
    pytest.param("https://]", id="stray-ipv6-close-bracket"),
]


@pytest.mark.parametrize("url", MALFORMED_DESTINATIONS)
def test_validate_navigation_destination_rejects_malformed_urls(url: str) -> None:
    with pytest.raises(BlockedNavigationDestination):
        validate_navigation_destination(url)


@pytest.mark.parametrize("url", MALFORMED_DESTINATIONS)
@pytest.mark.asyncio
async def test_navigate_with_retry_blocks_malformed_url_before_dispatch(url: str) -> None:
    navigate = AsyncMock()
    settle = AsyncMock()
    sleep = AsyncMock()

    with pytest.raises(BlockedNavigationDestination):
        await navigate_with_retry(navigate=navigate, url=url, retry_times=3, settle=settle, sleep=sleep)

    navigate.assert_not_awaited()  # a parser error must not leak past the guard as a raw ValueError
    settle.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_navigate_with_retry_blocked_destination_is_a_navigation_failure() -> None:
    # Subclass of FailedToNavigateToUrl so existing navigation error handling catches it.
    navigate = AsyncMock()
    with pytest.raises(FailedToNavigateToUrl):
        await navigate_with_retry(
            navigate=navigate, url="http://169.254.169.254/", retry_times=3, settle=AsyncMock(), sleep=AsyncMock()
        )


@pytest.mark.asyncio
async def test_navigate_with_retry_revalidates_redirect_hops_and_fails_closed() -> None:
    navigate = AsyncMock(
        return_value=_redirect_response("https://example.com/start", "http://169.254.169.254/latest/meta-data/")
    )
    settle = AsyncMock()
    sleep = AsyncMock()

    with pytest.raises(BlockedNavigationDestination):
        await navigate_with_retry(
            navigate=navigate, url="https://example.com/start", retry_times=3, settle=settle, sleep=sleep
        )

    assert navigate.await_count == 1  # a blocked redirect is not retried
    settle.assert_not_awaited()  # never settle a page that landed on an internal host
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_navigate_with_retry_allows_public_redirect_chain() -> None:
    navigate = AsyncMock(return_value=_redirect_response("http://example.com/start", "https://example.com/final"))
    settle = AsyncMock()

    await navigate_with_retry(
        navigate=navigate, url="http://example.com/start", retry_times=3, settle=settle, sleep=AsyncMock()
    )

    navigate.assert_awaited_once()
    settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigate_with_retry_allows_public_navigation_without_response() -> None:
    navigate = AsyncMock(return_value=None)
    settle = AsyncMock()

    await navigate_with_retry(
        navigate=navigate, url="https://example.com/", retry_times=3, settle=settle, sleep=AsyncMock()
    )

    navigate.assert_awaited_once()
    settle.assert_awaited_once()


# Empty and about:blank targets are non-egressing continuation/reconnect URLs that reach
# navigate_with_retry (e.g. task.url="" browser_session continuation, `url or "about:blank"`
# on reconnect); the fail-closed guard must let them through rather than reject them.
@pytest.mark.parametrize(
    "url",
    [pytest.param("", id="empty-string"), pytest.param("about:blank", id="about-blank")],
)
def test_validate_navigation_destination_allows_non_navigational_targets(url: str) -> None:
    validate_navigation_destination(url)


@pytest.mark.parametrize(
    "url",
    [pytest.param("", id="empty-string"), pytest.param("about:blank", id="about-blank")],
)
@pytest.mark.asyncio
async def test_navigate_with_retry_allows_non_navigational_targets(url: str) -> None:
    navigate = AsyncMock(return_value=None)
    settle = AsyncMock()

    await navigate_with_retry(navigate=navigate, url=url, retry_times=3, settle=settle, sleep=AsyncMock())

    navigate.assert_awaited_once()
    settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigate_with_retry_revalidates_every_redirect_hop() -> None:
    # An internal earliest hop followed by a long public tail: the whole chain must be
    # validated, not just the hops nearest the final URL.
    internal_first = "http://169.254.169.254/"
    public_tail = [f"https://hop{index}.example.com/" for index in range(14)]
    navigate = AsyncMock(return_value=_redirect_response(internal_first, *public_tail))
    settle = AsyncMock()

    with pytest.raises(BlockedNavigationDestination):
        await navigate_with_retry(
            navigate=navigate, url="https://start.example.com/", retry_times=3, settle=settle, sleep=AsyncMock()
        )

    settle.assert_not_awaited()
