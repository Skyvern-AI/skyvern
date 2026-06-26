from __future__ import annotations

import asyncio
from typing import cast

import structlog
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TargetClosedError as PWTargetClosedError
from playwright._impl._errors import TimeoutError as PWTimeoutError
from playwright.async_api import Browser, Playwright

from skyvern.config import settings
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


# Patch this module alias in tests so shard-wide asyncio.sleep mocks do not leak call counts.
_sleep = asyncio.sleep


def _settings_field_default(field_name: str) -> object:
    field = type(settings).model_fields[field_name]
    if field.default_factory is not None:
        return field.default_factory()
    return field.default


def _resolve_retry_budget() -> tuple[int, tuple[float, ...]]:
    """Resolve (attempts, backoff) from settings, falling back to the settings field
    defaults when the runtime value is invalid so a misconfig (e.g. attempts=0) cannot
    silently shrink the budget below the configured default."""
    attempts = settings.CDP_CONNECT_RETRY_ATTEMPTS
    backoff = tuple(settings.CDP_CONNECT_RETRY_BACKOFF_SECONDS)
    if attempts < 1:
        attempts = cast(int, _settings_field_default("CDP_CONNECT_RETRY_ATTEMPTS"))
    if not backoff or any(seconds < 0 for seconds in backoff):
        backoff = tuple(cast("list[float]", _settings_field_default("CDP_CONNECT_RETRY_BACKOFF_SECONDS")))
    return attempts, backoff


async def connect_over_cdp_with_retry(
    playwright: Playwright,
    browser_address: str,
    headers: dict[str, str] | None = None,
    log_browser_address: str | None = None,
) -> Browser:
    browser_address = strip_browser_address_discriminator(browser_address)
    browser_address_for_logs = log_browser_address or browser_address
    max_attempts, backoff_schedule = _resolve_retry_budget()
    for attempt in range(1, max_attempts + 1):
        try:
            browser = await playwright.chromium.connect_over_cdp(browser_address, headers=headers)
            if attempt > 1:
                LOG.info(
                    "CDP connection recovered after retry",
                    browser_address=browser_address_for_logs,
                    successful_attempt=attempt,
                )
            return browser
        except Exception as e:
            if not is_cdp_connection_error(e) or attempt == max_attempts:
                # When the caller passed log_browser_address as a safe label, the raw
                # browser_address may carry session tokens in path/query — Playwright's
                # exception text would otherwise expose them. Re-raise a RuntimeError
                # with only the safe label + error class name.
                if log_browser_address is not None:
                    raise RuntimeError(f"CDP connection to {log_browser_address} failed ({type(e).__name__})") from None
                raise
            backoff = backoff_schedule[attempt - 1] if attempt - 1 < len(backoff_schedule) else backoff_schedule[-1]
            LOG.warning(
                "CDP connection failed, retrying",
                browser_address=browser_address_for_logs,
                attempt=attempt,
                max_attempts=max_attempts,
                backoff_seconds=backoff,
                error_type=type(e).__name__,
            )
            await _sleep(backoff)
    raise RuntimeError("unreachable")
