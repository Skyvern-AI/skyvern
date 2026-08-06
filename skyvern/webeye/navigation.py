from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog

from skyvern.constants import PERMANENT_NAV_ERRORS, SKIP_INNER_NAV_RETRY_ERRORS
from skyvern.exceptions import BlockedHost, BlockedNavigationDestination, FailedToNavigateToUrl, InvalidUrl
from skyvern.utils.url_validators import canonical_navigation_host, is_blocked_host

LOG = structlog.get_logger()

NavigateFunc = Callable[[str], Awaitable[object]]
SettleFunc = Callable[[], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]

# Targets that never egress: an empty URL (browser_session continuation) and about:blank
# (reconnect to a fresh page). Blocking these would break those flows; pass them through.
_NON_NAVIGATIONAL_TARGETS = frozenset({"", "about:blank"})

# Defensive bound on the redirect walk. Browsers cap redirect chains well below this, so it
# only guards against a pathological/cyclic ``redirected_from`` link, not real navigation.
_MAX_REDIRECT_HOPS = 100


def validate_navigation_destination(url: str) -> None:
    """Fail closed unless ``url`` targets a public http(s) destination.

    Rejects local-resource schemes (``file://`` and anything other than http/https) and
    private, loopback, link-local, metadata, or otherwise-internal hosts, including
    public-looking names that resolve to internal addresses. Every navigation entry point
    that funnels through navigate_with_retry is guarded identically.
    The host comes from the browser's WHATWG canonicalization, not stdlib urlparse, so
    numeric-IP and backslash authority tricks that resolve to an internal host are caught.
    """
    if url.strip().lower() in _NON_NAVIGATIONAL_TARGETS:
        return

    try:
        host = canonical_navigation_host(url)
    except InvalidUrl as error:
        raise BlockedNavigationDestination(url=url, reason="unsupported scheme or malformed url") from error

    if not host or is_blocked_host(host, resolve_dns=True):
        raise BlockedNavigationDestination(url=url, reason="internal, loopback, link-local, or metadata host")


def _navigation_hop_urls(response: object) -> list[str]:
    # ``navigate`` returns the page.goto result as an opaque object; a real Playwright Response
    # exposes the followed redirect chain via ``request.redirected_from``. Duck-type it so the
    # shared helper stays decoupled from playwright and testable with plain fakes.
    urls: list[str] = []
    request = getattr(response, "request", None)
    for _ in range(_MAX_REDIRECT_HOPS):
        if request is None:
            break
        hop_url = getattr(request, "url", None)
        if isinstance(hop_url, str) and hop_url:
            urls.append(hop_url)
        request = getattr(request, "redirected_from", None)
    return urls


async def revalidate_redirect_chain(
    response: object,
    validate: Callable[[str], object],
    reset_page: NavigateFunc | None = None,
) -> None:
    """Re-check every hop the browser followed, plus the final destination.

    ``validate`` must raise the exception family the call site already handles.
    ``BlockedNavigationDestination`` is NOT a ``BlockedHost`` subclass, so passing the wrong
    validator silently reroutes a blocked hop into the caller's success or fallback branch.
    A provided ``reset_page`` is used to clear a refused destination without replacing its error.
    """
    try:
        for hop_url in _navigation_hop_urls(response):
            await asyncio.to_thread(validate, hop_url)
    except BlockedHost:
        if reset_page is not None:
            try:
                await reset_page("about:blank")
            except Exception:
                LOG.exception("Failed to reset page after redirect refusal")
        raise


async def _revalidate_navigation_response(response: object) -> None:
    await revalidate_redirect_chain(response, validate_navigation_destination)


# Progressive wait_until degradation. Degrading to `domcontentloaded` and
# then `commit` lets navigation succeed once the DOM or response is ready.
_DEGRADATION_MAP: dict[str, list[str]] = {
    "load": ["load", "domcontentloaded", "commit"],
    "domcontentloaded": ["domcontentloaded", "commit"],
    "commit": ["commit"],
}


def is_skip_inner_retry_error(error_message: str) -> bool:
    return any(pattern in error_message for pattern in SKIP_INNER_NAV_RETRY_ERRORS)


def is_permanent_navigation_error(error_message: str) -> bool:
    return any(pattern in error_message for pattern in PERMANENT_NAV_ERRORS)


async def navigate_with_retry(
    navigate: NavigateFunc,
    url: str,
    retry_times: int,
    settle: SettleFunc,
    wait_until: Literal["load", "domcontentloaded", "commit"] = "load",
    sleep: SleepFunc | None = None,
) -> None:
    # Late-bound so a test patching ``asyncio.sleep`` reaches the retry backoff.
    if sleep is None:
        sleep = asyncio.sleep
    degradation = _DEGRADATION_MAP.get(wait_until, [wait_until])

    # Fail closed before any request is dispatched so a blocked target never reaches the browser.
    await asyncio.to_thread(validate_navigation_destination, url)

    for attempt in range(retry_times):
        strategy = degradation[min(attempt, len(degradation) - 1)]
        LOG.info("Trying to navigate to url", url=url, retry_time=attempt, wait_until=strategy)
        try:
            start_time = time.monotonic()
            response = await navigate(strategy)
            # Revalidate the followed redirect chain: page.goto follows redirects at the network
            # layer, so a public entry point can still land on an internal host (SKY-13112).
            await _revalidate_navigation_response(response)
            elapsed = time.monotonic() - start_time
            LOG.info("Page loading time", loading_time=elapsed, url=url, wait_until=strategy)
            await settle()
            LOG.info("Successfully navigated to url", url=url, retry_time=attempt, wait_until=strategy)
            return

        except BlockedNavigationDestination:
            # Blocked destinations are permanent; retrying re-issues the same request.
            raise
        except Exception as error:
            error_str = str(error)

            if is_skip_inner_retry_error(error_str):
                LOG.warning(
                    "Non-retriable navigation error, failing immediately",
                    url=url,
                    error=error_str,
                )
                raise FailedToNavigateToUrl(url=url, error_message=error_str) from error

            if attempt >= retry_times - 1:
                LOG.exception(
                    "Failed to navigate after retries",
                    url=url,
                    retry_times=retry_times,
                    error=error_str,
                )
                raise FailedToNavigateToUrl(url=url, error_message=error_str) from error

            LOG.warning(
                "Error while navigating to url, retrying",
                exc_info=True,
                url=url,
                retry_time=attempt,
                wait_until=strategy,
                error=error_str,
            )
            await sleep(1)
