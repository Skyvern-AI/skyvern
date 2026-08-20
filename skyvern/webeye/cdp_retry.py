from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

import structlog
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TargetClosedError as PWTargetClosedError
from playwright._impl._errors import TimeoutError as PWTimeoutError
from playwright.async_api import Browser, Playwright

import skyvern.exceptions as skyvern_exceptions
from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.utils.url_validators import is_allowed_local_browser_host, resolve_fetch_host_ips, validate_browser_host
from skyvern.webeye.browser_errors import (
    BrowserCdpConnectionError,
    BrowserTargetClosedError,
    BrowserTimeoutError,
)
from skyvern.webeye.cdp_connection import redact_cdp_url, strip_browser_address_discriminator

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
# These three subclasses, never `OSError` itself: this predicate also gates cloud quarantine, and
# `OSError` covers permanent local failures (missing or unexecutable browser binary, EACCES, EMFILE)
# where retrying cannot succeed and pulling the address from the pool is wrong.
_CDP_CONNECTION_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionRefusedError,
    ConnectionResetError,
    TimeoutError,
)


def _has_transport_substring(exc: BaseException) -> bool:
    return any(s in str(exc).lower() for s in _CDP_CONNECTION_ERROR_SUBSTR_FALLBACK)


def _is_unresolvable_host(exc: Exception) -> bool:
    # Deliberate forward-compat, not dead code: UnresolvableHost exists on neither this branch nor
    # main. #14201 introduces it and makes resolve_fetch_host_ips raise it instead of BlockedHost, so
    # the exact type(exc) is BlockedHost check below is what carries this until that sibling lands.
    unresolvable_host_type = cast(type[BlockedHost] | None, getattr(skyvern_exceptions, "UnresolvableHost", None))
    if unresolvable_host_type is not None and isinstance(exc, unresolvable_host_type):
        return True
    return type(exc) is BlockedHost and isinstance(exc.__context__, (OSError, UnicodeError))


def is_cdp_connection_error(exc: Exception, selection: BrowserEngineSelection | None = None) -> bool:
    """Decide whether a failed CDP connect/attach is a retryable connection error.

    Engine-neutral CDP errors normalized by a lower boundary are recognized with or without a
    ``selection``. With no ``selection`` (no engine authority at this call site), native error
    classification uses stock Playwright's identities. With a ``selection`` the retry decision keys off THAT run's
    selected-engine error families
    (retryable-CDP / CDP-connection / target-closed / timeout) via the engine-neutral classifier, so a
    non-stock engine's native classes are recognized instead of stock Playwright's — and a foreign
    error (one this engine never raises) is not classified, so it falls through to ``False`` and the
    caller re-raises it rather than retrying blindly. Under both paths the engine-neutral transport
    floor still applies: stdlib socket errors always retry, and the engine's OWN base error carrying a
    known transport substring retries (guarded by engine identity so a foreign error with a
    coincidentally-matching message never does). This is a retry predicate only; it never wraps or
    normalizes ``exc`` — the native exception the caller re-raises stays intact.
    """
    if isinstance(exc, BrowserCdpConnectionError):
        return True
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


async def _validate_browser_address_host(browser_address: str) -> None:
    host = urlparse(browser_address).hostname
    if not host:
        return
    await asyncio.to_thread(validate_browser_host, host, resolve_dns=False)
    if not is_allowed_local_browser_host(host):
        await asyncio.to_thread(resolve_fetch_host_ips, host)


async def connect_over_cdp_with_retry(
    playwright: Playwright,
    browser_address: str,
    headers: dict[str, str] | None = None,
    log_browser_address: str | None = None,
    selection: BrowserEngineSelection | None = None,
    validate_browser_address: bool = True,
    retry: bool = True,
) -> Browser:
    browser_address = strip_browser_address_discriminator(browser_address)
    browser_address_for_logs = log_browser_address or redact_cdp_url(browser_address)
    max_attempts, backoff_schedule = _resolve_retry_budget()
    if not retry:
        max_attempts = 1
    address_validated = not validate_browser_address
    for attempt in range(1, max_attempts + 1):
        try:
            if not address_validated:
                await _validate_browser_address_host(browser_address)
                address_validated = True
            browser = await playwright.chromium.connect_over_cdp(browser_address, headers=headers)
            if attempt > 1:
                LOG.info(
                    "CDP connection recovered after retry",
                    browser_address=browser_address_for_logs,
                    successful_attempt=attempt,
                )
            return browser
        except Exception as e:
            is_resolution_error = _is_unresolvable_host(e)
            if isinstance(e, BlockedHost) and not is_resolution_error:
                raise
            is_cdp_error = is_cdp_connection_error(e, selection)
            is_retryable_error = is_resolution_error or is_cdp_error
            if not is_retryable_error or attempt == max_attempts:
                if log_browser_address is not None:
                    error_type = BrowserCdpConnectionError if is_retryable_error else RuntimeError
                    raise error_type(f"CDP connection to {log_browser_address} failed ({type(e).__name__})") from None
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
