from __future__ import annotations

import asyncio

import structlog
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TargetClosedError as PWTargetClosedError
from playwright._impl._errors import TimeoutError as PWTimeoutError
from playwright.async_api import Browser, Playwright

from skyvern.webeye.cdp_connection import strip_browser_address_discriminator

LOG = structlog.get_logger()

_CDP_CONNECTION_ERROR_SUBSTR_FALLBACK = (
    "econnrefused",
    "econnreset",
    "connect etimedout",
    "browser closed",
    "browser has been closed",
)


def is_cdp_connection_error(exc: Exception) -> bool:
    if isinstance(
        exc, (PWTimeoutError, PWTargetClosedError, ConnectionRefusedError, ConnectionResetError, TimeoutError)
    ):
        return True
    if isinstance(exc, PWError) and any(s in str(exc).lower() for s in _CDP_CONNECTION_ERROR_SUBSTR_FALLBACK):
        return True
    return False


_CDP_RETRY_ATTEMPTS = 3
_CDP_RETRY_BACKOFF_SECONDS = (1, 3)
# Patch this module alias in tests so shard-wide asyncio.sleep mocks do not leak call counts.
_sleep = asyncio.sleep


async def connect_over_cdp_with_retry(
    playwright: Playwright,
    browser_address: str,
    headers: dict[str, str] | None = None,
) -> Browser:
    browser_address = strip_browser_address_discriminator(browser_address)
    for attempt in range(1, _CDP_RETRY_ATTEMPTS + 1):
        try:
            browser = await playwright.chromium.connect_over_cdp(browser_address, headers=headers)
            if attempt > 1:
                LOG.info(
                    "CDP connection recovered after retry",
                    browser_address=browser_address,
                    successful_attempt=attempt,
                )
            return browser
        except Exception as e:
            if not is_cdp_connection_error(e) or attempt == _CDP_RETRY_ATTEMPTS:
                raise
            backoff = (
                _CDP_RETRY_BACKOFF_SECONDS[attempt - 1]
                if attempt - 1 < len(_CDP_RETRY_BACKOFF_SECONDS)
                else _CDP_RETRY_BACKOFF_SECONDS[-1]
            )
            LOG.warning(
                "CDP connection failed, retrying",
                browser_address=browser_address,
                attempt=attempt,
                max_attempts=_CDP_RETRY_ATTEMPTS,
                backoff_seconds=backoff,
                error_type=type(e).__name__,
            )
            await _sleep(backoff)
    raise RuntimeError("unreachable")
