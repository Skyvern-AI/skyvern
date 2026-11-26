from __future__ import annotations

import asyncio
import errno
import secrets
from http import HTTPStatus
from http.client import HTTPException
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

import structlog
import websockets
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TargetClosedError as PWTargetClosedError
from playwright._impl._errors import TimeoutError as PWTimeoutError
from playwright._impl._helper import DEFAULT_PLAYWRIGHT_LAUNCH_TIMEOUT_IN_MILLISECONDS
from playwright.async_api import Browser, Playwright
from websockets.exceptions import InvalidHandshake
from websockets.http11 import Request, Response

import skyvern.exceptions as skyvern_exceptions
from skyvern.cdp_transport import open_first_pinned_websocket
from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.utils.url_validators import is_allowed_local_browser_host, resolve_fetch_host_ips, validate_browser_host
from skyvern.webeye.browser_acquisition_sample import note_cdp_connect_attempts
from skyvern.webeye.browser_errors import (
    BrowserCdpAcquisitionError,
    BrowserCdpConnectionError,
    BrowserTargetClosedError,
    BrowserTimeoutError,
)
from skyvern.webeye.cdp_connection import redact_cdp_url, strip_browser_address_discriminator
from skyvern.webeye.cdp_discovery import CdpDiscoveryError, resolve_websocket_url

if TYPE_CHECKING:
    from skyvern.webeye.browser_engine import BrowserEngineSelection

LOG = structlog.get_logger()

_CDP_ACQUISITION_DIAGNOSTIC = "Browser driver failed during CDP connect to [remote browser endpoint]."
_DRIVER_TIMEOUT_MARGIN_RATIO = 0.1
_PINNED_ADDRESS_RETRY_ERRNOS = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH})

_CDP_CONNECTION_ERROR_SUBSTR_FALLBACK = (
    "econnrefused",
    "econnreset",
    "enetunreach",
    "ehostunreach",
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


def _is_retryable_pinned_connect_error(exc: Exception) -> bool:
    if isinstance(exc, OSError):
        return exc.errno in _PINNED_ADDRESS_RETRY_ERRNOS
    return isinstance(exc, EOFError) or (isinstance(exc, InvalidHandshake) and isinstance(exc.__cause__, EOFError))


def _is_retryable_discovery_error(exc: Exception) -> bool:
    if not isinstance(exc, CdpDiscoveryError):
        return False
    cause = exc.__cause__
    if isinstance(cause, (OSError, HTTPException)):
        return True
    if not isinstance(cause, InvalidHandshake):
        return False
    response = getattr(cause, "response", None)
    status_code = getattr(response, "status_code", None)
    return isinstance(status_code, int) and 500 <= status_code < 600


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


def _is_selected_engine_error(exc: BaseException, selection: BrowserEngineSelection | None) -> bool:
    if selection is not None:
        return selection.is_engine_error(exc)
    return isinstance(exc, PWError)


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


async def _validate_browser_address_host(browser_address: str) -> tuple[str, ...] | None:
    host = urlparse(browser_address).hostname
    if not host:
        return None
    await asyncio.to_thread(validate_browser_host, host, resolve_dns=False)
    if is_allowed_local_browser_host(host):
        return None
    return await asyncio.to_thread(resolve_fetch_host_ips, host)


class _PinnedCdpRelay:
    def __init__(self, upstream: websockets.ClientConnection) -> None:
        self._upstream = upstream
        self._server: websockets.Server | None = None
        self._path = f"/{secrets.token_urlsafe(32)}"
        self._accepted = False

    async def start(self) -> str:
        self._server = await websockets.serve(
            self._relay,
            "127.0.0.1",
            0,
            max_size=None,
            ping_interval=None,
            process_request=self._admit,
        )
        port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}{self._path}"

    def _admit(self, connection: websockets.ServerConnection, request: Request) -> Response | None:
        if request.path != self._path or "Origin" in request.headers or self._accepted:
            return connection.respond(HTTPStatus.NOT_FOUND, "")
        self._accepted = True
        return None

    async def _relay(self, downstream: websockets.ServerConnection) -> None:
        async def forward(source: websockets.Connection, sink: websockets.Connection) -> None:
            async for message in source:
                await sink.send(message)

        directions = {
            asyncio.create_task(forward(downstream, self._upstream)),
            asyncio.create_task(forward(self._upstream, downstream)),
        }
        try:
            _, pending = await asyncio.wait(directions, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            await self._upstream.close()
            if self._server is not None:
                self._server.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        await self._upstream.close()
        if self._server is not None:
            await self._server.wait_closed()


async def _connect_over_relay(
    playwright: Playwright,
    upstream: websockets.ClientConnection,
    *,
    timeout_ms: float,
) -> Browser:
    relay = _PinnedCdpRelay(upstream)
    try:
        relay_url = await relay.start()
        return await playwright.chromium.connect_over_cdp(relay_url, timeout=timeout_ms)
    except BaseException:
        await relay.close()
        raise


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
    resolved_ips: tuple[str, ...] | None = None
    parsed_browser_address = urlparse(browser_address)
    refresh_discovery = parsed_browser_address.scheme.lower() in ("http", "https")
    validated_ips_by_host: dict[str, tuple[str, ...] | None] = {}
    connect_target: tuple[str, tuple[str, ...], int, str | None, dict[str, str] | None] | None = None
    direct_address = browser_address
    connect_round = 0
    for attempt in range(1, max_attempts + 1):
        connect_started = False
        loop = asyncio.get_running_loop()
        attempt_deadline = loop.time() + DEFAULT_PLAYWRIGHT_LAUNCH_TIMEOUT_IN_MILLISECONDS / 1000
        # Recorded before the attempt (no-op unless an acquisition sample scope is open) so a
        # cancellation mid-connect still reflects the retries made; monotonic-max keeps the highest
        # attempt across a later cross-family fallback that performs its own connect.
        note_cdp_connect_attempts(attempt)
        try:
            async with asyncio.timeout_at(attempt_deadline):
                if not address_validated:
                    resolved_ips = await _validate_browser_address_host(browser_address)
                    if parsed_browser_address.hostname:
                        validated_ips_by_host[parsed_browser_address.hostname.lower()] = resolved_ips
                    address_validated = True
                if validate_browser_address and (connect_target is None or refresh_discovery):
                    websocket_address = await resolve_websocket_url(
                        browser_address,
                        timeout_ms=max((attempt_deadline - loop.time()) * 1000, 1),
                        resolved_ips=resolved_ips,
                        headers=headers,
                    )
                    parsed = urlparse(websocket_address)
                    websocket_host = parsed.hostname.lower() if parsed.hostname else None
                    if websocket_host in validated_ips_by_host:
                        websocket_ips = validated_ips_by_host[websocket_host]
                    else:
                        websocket_ips = await _validate_browser_address_host(websocket_address)
                        if websocket_host:
                            validated_ips_by_host[websocket_host] = websocket_ips
                    direct_address = websocket_address
                    if websocket_ips:
                        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "wss" else 80)
                        connect_target = (
                            websocket_address,
                            websocket_ips,
                            port,
                            parsed.hostname if parsed.scheme == "wss" else None,
                            dict(headers) if headers else None,
                        )
                    else:
                        connect_target = None
            connect_started = True
            if connect_target is None:
                browser = await playwright.chromium.connect_over_cdp(direct_address, headers=headers)
            else:
                connect_url, connect_ips, connect_port, tls_name, connect_headers = connect_target
                offset = connect_round % len(connect_ips)
                connect_round += 1
                connect_ips = connect_ips[offset:] + connect_ips[:offset]
                try:
                    upstream = await open_first_pinned_websocket(
                        connect_url,
                        connect_hosts=connect_ips,
                        connect_port=connect_port,
                        server_hostname=tls_name,
                        headers=connect_headers,
                        deadline=attempt_deadline,
                        max_size=None,
                        ping_interval=None,
                        ping_timeout=None,
                    )
                except Exception as error:
                    if not _is_retryable_pinned_connect_error(error):
                        raise
                    raise BrowserCdpConnectionError("CDP connection to a validated endpoint failed") from None
                remaining_ms = (attempt_deadline - loop.time()) * 1000
                if remaining_ms <= 0:
                    await upstream.close()
                    raise TimeoutError("CDP connection attempt exceeded its deadline")
                driver_timeout_ms = max(remaining_ms * (1 - _DRIVER_TIMEOUT_MARGIN_RATIO), 1)
                async with asyncio.timeout_at(attempt_deadline):
                    browser = await _connect_over_relay(
                        playwright,
                        upstream,
                        timeout_ms=driver_timeout_ms,
                    )
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
            is_discovery_connection_error = _is_retryable_discovery_error(e)
            is_retryable_error = is_resolution_error or is_cdp_error or is_discovery_connection_error
            if not is_retryable_error or attempt == max_attempts:
                if not is_retryable_error and connect_started and _is_selected_engine_error(e, selection):
                    raise BrowserCdpAcquisitionError(_CDP_ACQUISITION_DIAGNOSTIC) from None
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
