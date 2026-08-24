from __future__ import annotations

import re
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing

from skyvern.exceptions import BlockedHost, BlockedNavigationDestination, FailedToNavigateToUrl, UnresolvableHost
from skyvern.webeye.navigation import (
    navigate_with_retry,
    redact_url_secrets,
    revalidate_redirect_chain,
    validate_navigation_destination,
)
from skyvern.webeye.real_browser_state import RealBrowserState


@pytest.fixture(autouse=True)
def _resolve_navigation_hosts_to_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolves_public(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_public)


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
        pytest.param("net::ERR_SOCKS_CONNECTION_FAILED", id="socks-connection-failed"),
        pytest.param("net::ERR_SOCKS_CONNECTION_HOST_UNREACHABLE", id="socks-host-unreachable"),
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


def test_validate_navigation_destination_allows_a_host_the_worker_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fails_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise OSError("dns unavailable")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", fails_dns)

    validate_navigation_destination("https://public.example.test/path")


@pytest.mark.parametrize("url", BLOCKED_DESTINATIONS)
def test_validate_navigation_destination_still_refuses_internal_targets_when_dns_fails(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    def fails_dns(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
        raise OSError("dns unavailable")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", fails_dns)

    with pytest.raises(BlockedNavigationDestination):
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


@pytest.mark.asyncio
async def test_revalidate_redirect_chain_checks_every_hop_and_the_final_destination() -> None:
    response = _redirect_response(
        "https://entry.example.test/a",
        "https://mid.example.test/b",
        "https://final.example.test/c",
    )

    seen: list[str] = []
    reset_page = AsyncMock()
    await revalidate_redirect_chain(response, seen.append, reset_page)

    assert sorted(seen) == [
        "https://entry.example.test/a",
        "https://final.example.test/c",
        "https://mid.example.test/b",
    ]
    reset_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_revalidate_redirect_chain_propagates_the_validators_exception() -> None:
    response = _redirect_response(
        "https://entry.example.test/a",
        "http://169.254.169.254/latest/meta-data/",
    )

    refusal = BlockedHost("169.254.169.254")
    reset_page = AsyncMock()

    def refuse_metadata(url: str) -> None:
        if "169.254.169.254" in url:
            raise refusal

    with pytest.raises(BlockedHost) as exc_info:
        await revalidate_redirect_chain(response, refuse_metadata, reset_page)

    assert exc_info.value is refusal
    reset_page.assert_awaited_once_with("about:blank")


@pytest.mark.parametrize("refusal", [BlockedHost("blocked.test"), UnresolvableHost("unresolvable.test")])
@pytest.mark.asyncio
async def test_revalidate_redirect_chain_preserves_refusal_when_page_reset_fails(
    monkeypatch: pytest.MonkeyPatch,
    refusal: BlockedHost,
) -> None:
    response = _redirect_response(
        "https://entry.example.test/a",
        "http://169.254.169.254/latest/meta-data/",
    )
    reset_page = AsyncMock(side_effect=RuntimeError("reset failed"))
    log_exception = MagicMock()
    monkeypatch.setattr("skyvern.webeye.navigation.LOG.exception", log_exception)

    def refuse_metadata(url: str) -> None:
        if "169.254.169.254" in url:
            raise refusal

    with pytest.raises(type(refusal)) as exc_info:
        await revalidate_redirect_chain(response, refuse_metadata, reset_page)

    assert exc_info.value is refusal
    reset_page.assert_awaited_once_with("about:blank")
    log_exception.assert_called_once_with("Failed to reset page after redirect refusal")


@pytest.mark.asyncio
async def test_revalidate_redirect_chain_tolerates_a_response_without_a_request() -> None:
    calls: list[str] = []

    await revalidate_redirect_chain(None, calls.append)
    await revalidate_redirect_chain(SimpleNamespace(request=None), calls.append)

    assert calls == []


def test_redact_url_secrets_keeps_only_scheme_and_host() -> None:
    assert redact_url_secrets("https://portal.example.com/verify/abc?token=xyz#frag") == (
        "https://portal.example.com/<redacted>"
    )
    assert redact_url_secrets("https://portal.example.com:8443/verify?token=xyz") == (
        "https://portal.example.com:8443/<redacted>"
    )
    assert redact_url_secrets("not a url") == "<redacted>"


def test_redact_url_secrets_drops_basic_auth_credentials() -> None:
    """netloc carries user:password@; a redactor must not republish it."""
    redacted = redact_url_secrets("https://tok:s3cret@portal.example.com/verify?token=xyz")

    assert "s3cret" not in redacted
    assert "tok" not in redacted
    assert redacted == "https://portal.example.com/<redacted>"


@pytest.mark.asyncio
async def test_a_refused_secret_destination_does_not_report_the_real_url() -> None:
    """A self-hosted portal's link can legitimately resolve to a private host."""
    secret = "http://127.0.0.1/verify?token=super-secret-token-value"

    async def navigate(strategy: str) -> object:
        raise AssertionError("must not navigate to a blocked destination")

    async def settle() -> None:
        return None

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(BlockedNavigationDestination) as excinfo:
            await navigate_with_retry(
                navigate=navigate,
                url=secret,
                retry_times=2,
                settle=settle,
                log_url=redact_url_secrets(secret),
            )

    assert "super-secret-token-value" not in str(excinfo.value)
    assert "super-secret-token-value" not in repr(logs)
    # The chained cause would carry the original message into any rendered traceback.
    assert excinfo.value.__cause__ is None


@pytest.mark.asyncio
async def test_log_url_keeps_the_real_url_out_of_logs_and_the_failure_reason() -> None:
    """A sign-in link is a bearer credential: navigate to it, but never log or report it."""
    secret = "https://portal.example.com/verify?token=super-secret-token-value"

    async def navigate(strategy: str) -> object:
        # Playwright names the destination in its own message, so the secret arrives via the
        # error text as well as the url field.
        raise RuntimeError(f"Page.goto: net::ERR_CONNECTION_REFUSED at {secret}")

    async def settle() -> None:
        return None

    async def no_sleep(_seconds: float) -> None:
        return None

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(FailedToNavigateToUrl) as excinfo:
            await navigate_with_retry(
                navigate=navigate,
                url=secret,
                retry_times=2,
                settle=settle,
                sleep=no_sleep,
                log_url=redact_url_secrets(secret),
            )

    assert "super-secret-token-value" not in str(excinfo.value)
    assert "super-secret-token-value" not in repr(logs)
    # The host still reaches the operator, so a failure stays diagnosable. re.search rather
    # than ``in``: CodeQL's py/incomplete-url-substring-sanitization reads a hostname-literal
    # ``in`` check as broken sanitization; this is an assertion, not a sanitizer.
    assert any(re.search(r"portal\.example\.com", repr(entry)) for entry in logs)


@pytest.mark.asyncio
async def test_a_refused_redirect_hop_is_redacted_for_a_secret_caller() -> None:
    """The hop that gets refused is a different URL than the one requested; redact that one."""
    secret = "https://portal.example.com/verify?token=super-secret-token-value"
    # A literal loopback address is refused before DNS, unlike the hostnames the suite's
    # autouse fixture resolves to a public address.
    hop = "http://127.0.0.1/r?dest=internal&session=hop-secret-value"

    async def navigate(strategy: str) -> object:
        return SimpleNamespace(request=SimpleNamespace(url=hop, redirected_from=None))

    async def settle() -> None:
        return None

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(BlockedNavigationDestination) as excinfo:
            await navigate_with_retry(
                navigate=navigate,
                url=secret,
                retry_times=2,
                settle=settle,
                log_url=redact_url_secrets(secret),
            )

    assert "hop-secret-value" not in str(excinfo.value)
    assert "hop-secret-value" not in repr(logs)
    assert "super-secret-token-value" not in str(excinfo.value)
    # The refused hop's host still identifies what was blocked.
    assert "127.0.0.1" in str(excinfo.value)
    assert excinfo.value.__cause__ is None


@pytest.mark.asyncio
async def test_navigation_still_targets_the_real_url_when_a_display_url_is_given() -> None:
    secret = "https://portal.example.com/verify?token=super-secret-token-value"
    navigated: list[str] = []

    async def navigate(strategy: str) -> object:
        navigated.append(secret)
        return None

    async def settle() -> None:
        return None

    await navigate_with_retry(
        navigate=navigate,
        url=secret,
        retry_times=1,
        settle=settle,
        log_url=redact_url_secrets(secret),
    )

    assert navigated == [secret]
