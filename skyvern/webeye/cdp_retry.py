from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import structlog
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TargetClosedError as PWTargetClosedError
from playwright._impl._errors import TimeoutError as PWTimeoutError
from playwright.async_api import Browser, Playwright

from skyvern.config import settings
from skyvern.webeye.browser_errors import (
    BrowserCdpConnectionError,
    BrowserTargetClosedError,
    BrowserTimeoutError,
)
from skyvern.webeye.cdp_connection import strip_browser_address_discriminator

if TYPE_CHECKING:
    from skyvern.webeye.browser_engine import BrowserEngineSelection

LOG = structlog.get_logger()

_CDP_CONNECTION_ERROR_SUBSTR_FALLBACK = (
    "econnrefused",
    "econnreset",
    "connect etimedout",
    "browser closed",
    "browser has been closed",
)

# Stdlib/OS-level socket failures that mean a retryable CDP connect problem no matter which engine is
# selected: they are raised beneath every driver, so no engine's native error families own them. Kept
# as the shared floor under both the selection-aware and stock paths so migrating classification to a
# selected engine never drops the cross-engine transport signal.
_CDP_CONNECTION_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionRefusedError,
    ConnectionResetError,
    TimeoutError,
)


def _has_transport_substring(exc: BaseException) -> bool:
    return any(s in str(exc).lower() for s in _CDP_CONNECTION_ERROR_SUBSTR_FALLBACK)


def is_cdp_connection_error(exc: Exception, selection: BrowserEngineSelection | None = None) -> bool:
    """Decide whether a failed CDP connect/attach is a retryable connection error.

    With no ``selection`` (no engine authority at this call site) classification uses stock
    Playwright's error identities exactly as before, so the default path is byte-for-byte unchanged.
    With a ``selection`` the retry decision keys off THAT run's selected-engine error families
    (retryable-CDP / CDP-connection / target-closed / timeout) via the engine-neutral classifier, so a
    non-stock engine's native classes are recognized instead of stock Playwright's — and a foreign
    error (one this engine never raises) is not classified, so it falls through to ``False`` and the
    caller re-raises it rather than retrying blindly. Under both paths the engine-neutral transport
    floor still applies: stdlib socket errors always retry, and the engine's OWN base error carrying a
    known transport substring retries (guarded by engine identity so a foreign error with a
    coincidentally-matching message never does). This is a retry predicate only; it never wraps or
    normalizes ``exc`` — the native exception the caller re-raises stays intact.
    """
    if isinstance(exc, _CDP_CONNECTION_TRANSPORT_ERRORS):
        return True
    if selection is not None:
        classified = selection.classify_error(exc)
        if isinstance(classified, (BrowserCdpConnectionError, BrowserTargetClosedError, BrowserTimeoutError)):
            return True
        return selection.is_engine_error(exc) and _has_transport_substring(exc)
    if isinstance(exc, (PWTimeoutError, PWTargetClosedError)):
        return True
    if isinstance(exc, PWError) and _has_transport_substring(exc):
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
    selection: BrowserEngineSelection | None = None,
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
            if not is_cdp_connection_error(e, selection) or attempt == max_attempts:
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
                browser_engine=selection.name if selection is not None else None,
            )
            await _sleep(backoff)
    raise RuntimeError("unreachable")
