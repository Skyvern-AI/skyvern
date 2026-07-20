"""Generic CDP-over-WebSocket upstream adapter.

Dials any hosted-browser vendor or own-infra browser that exposes a CDP ws/wss
endpoint. Connect headers (vendor credentials) are injected from proxy-operator
configuration — never taken from client input.

The transient-error predicate, retry-budget knobs, and browser-address fragment
strip deliberately duplicate ~15 lines of skyvern/webeye/cdp_retry.py and
cdp_connection.py rather than importing them: those modules are Playwright-bound
runner code and the proxy image ships zero runner dependencies (ADR-0010).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import ssl
from typing import Mapping
from urllib.parse import urlparse, urlunparse

import structlog
import websockets
from websockets.exceptions import InvalidHandshake, InvalidStatus, InvalidURI

from skyvern.proxy.core.errors import (
    ProtocolConfigurationError,
    TransientConnectionError,
    UpstreamConnectError,
    VendorAuthError,
    VendorRateLimitError,
)
from skyvern.proxy.core.session import ProxySession, UpstreamClosedError
from skyvern.proxy.ports import UpstreamConnection

LOG = structlog.get_logger(__name__)


class _CredentialRedactingFilter(logging.Filter):
    """Scrub the CDP session token and injected vendor credential out of websockets'
    DEBUG handshake logs so a later level change cannot surface them."""

    _REQUEST_TARGET = re.compile(r"(?im)^([<>]\s*[A-Z]+\s+)\S+(\s+HTTP/\d)")
    _SENSITIVE_HEADER = re.compile(
        r"(?im)^([<>]\s*(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key"
        r"|sec-websocket-key|sec-websocket-accept)\s*:\s*).+$"
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            record.msg, record.args = "websockets log suppressed (unrenderable)", None
            return True
        redacted = self._SENSITIVE_HEADER.sub(r"\1[REDACTED]", self._REQUEST_TARGET.sub(r"\1[REDACTED]\2", message))
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


# websockets DEBUG-logs the handshake target and headers, which carry the CDP session token
# and the injected vendor credential; the filter (always run because dial() pins the
# connection to this logger) scrubs them even if the level is later lowered to DEBUG.
_WEBSOCKETS_LOGGER = logging.getLogger("websockets")
_WEBSOCKETS_LOGGER.setLevel(logging.INFO)
_WEBSOCKETS_LOGGER.addFilter(_CredentialRedactingFilter())

_AUTH_HTTP_STATUSES = frozenset({401, 403, 407})
_RATE_LIMIT_HTTP_STATUS = 429
DEFAULT_OPEN_TIMEOUT_SECONDS = 30.0
_DEFAULT_RETRY_ATTEMPTS = 6
_DEFAULT_BACKOFF_SCHEDULE = (1.0, 2.0, 3.0, 4.0, 5.0)
_MAX_RETRY_ATTEMPTS = 100
_MAX_BACKOFF_SECONDS = 300.0


def resolve_retry_budget() -> tuple[int, tuple[float, ...]]:
    """Resolve (attempts, backoff) from the proxy's env knobs, falling back to the
    defaults when a value is invalid (non-numeric, NaN/inf, or out of bounds) so a
    misconfig cannot shrink the budget or hang the retry loop."""
    try:
        attempts = int(os.environ.get("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", ""))
    except ValueError:
        attempts = _DEFAULT_RETRY_ATTEMPTS
    if not 1 <= attempts <= _MAX_RETRY_ATTEMPTS:
        attempts = _DEFAULT_RETRY_ATTEMPTS
    raw_backoff = os.environ.get("CDP_PROXY_CONNECT_BACKOFF_SECONDS", "")
    try:
        backoff = tuple(float(part) for part in raw_backoff.split(",")) if raw_backoff else ()
    except ValueError:
        backoff = ()
    if not backoff or any(
        not math.isfinite(seconds) or not 0 <= seconds <= _MAX_BACKOFF_SECONDS for seconds in backoff
    ):
        backoff = _DEFAULT_BACKOFF_SCHEDULE
    return attempts, backoff


def merge_connect_headers(operator_headers: Mapping[str, str], session_headers: Mapping[str, str]) -> dict[str, str]:
    """Case-insensitive merge (HTTP header names are case-insensitive); the
    operator-configured value wins so registry data can never override proxy
    credentials or duplicate a header under a different casing."""
    operator_names = {name.lower() for name in operator_headers}
    merged = {name: value for name, value in session_headers.items() if name.lower() not in operator_names}
    merged.update(operator_headers)
    return merged


def strip_browser_address_discriminator(url: str) -> str:
    """Remove local PBS URL fragments used only for browser_address DB uniqueness."""
    parsed = urlparse(url)
    if parsed.fragment.startswith("pbs_"):
        return urlunparse(parsed._replace(fragment=""))
    return url


def endpoint_label(url: str) -> str:
    """Scheme and host only — upstream paths and query strings can carry session tokens."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}"


def classify_connect_error(exc: Exception, label: str) -> UpstreamConnectError:
    if isinstance(exc, InvalidStatus):
        status = exc.response.status_code
        if status in _AUTH_HTTP_STATUSES:
            return VendorAuthError(f"upstream at {label} rejected the proxy's credentials (HTTP {status})")
        if status == _RATE_LIMIT_HTTP_STATUS:
            return VendorRateLimitError(f"upstream at {label} is rate limiting connections (HTTP {status})")
        if status >= 500:
            return TransientConnectionError(f"upstream at {label} failed the websocket handshake (HTTP {status})")
        return ProtocolConfigurationError(f"upstream at {label} is not a CDP websocket endpoint (HTTP {status})")
    # Mid-handshake connection drops (LB draining, browser warmup, half-open reset) surface
    # as InvalidHandshake subclasses (InvalidMessage, InvalidProxyMessage, ...) with an
    # EOFError cause — transient, unlike other handshake failures.
    if isinstance(exc, EOFError) or (isinstance(exc, InvalidHandshake) and isinstance(exc.__cause__, EOFError)):
        return TransientConnectionError(f"upstream at {label} dropped the websocket handshake ({type(exc).__name__})")
    if isinstance(exc, (InvalidURI, InvalidHandshake)):
        return ProtocolConfigurationError(
            f"upstream at {label} is not a usable CDP websocket endpoint ({type(exc).__name__})"
        )
    # TLS failures (expired/untrusted/hostname-mismatch cert) subclass OSError but are a
    # config problem, not a transient network one — classify before the broad OSError check.
    if isinstance(exc, ssl.SSLError):
        return ProtocolConfigurationError(f"upstream at {label} failed TLS verification ({type(exc).__name__})")
    # Remaining transients a raw websocket dial can raise: refused/reset/DNS (OSError)
    # and open-timeout (asyncio.TimeoutError).
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return TransientConnectionError(f"connection to upstream at {label} failed ({type(exc).__name__})")
    return UpstreamConnectError(f"connecting to upstream at {label} failed ({type(exc).__name__})")


def _refuse_redirect(exc: Exception) -> Exception:
    # websockets treats a returned exception (rather than a new URI) as "not a redirect" and
    # re-raises it without following, so the injected credential never reaches the redirect target.
    return exc


class WebSocketUpstreamConnection:
    def __init__(self, ws: websockets.ClientConnection) -> None:
        self._ws = ws

    async def send(self, raw: str) -> None:
        try:
            await self._ws.send(raw)
        except websockets.ConnectionClosed as exc:
            raise UpstreamClosedError("upstream websocket closed") from exc

    async def receive(self) -> str:
        try:
            message = await self._ws.recv()
        except websockets.ConnectionClosed as exc:
            raise UpstreamClosedError("upstream websocket closed") from exc
        return message if isinstance(message, str) else message.decode()

    async def close(self) -> None:
        await self._ws.close()


class WebSocketUpstreamBrowser:
    """Connects to any CDP-over-WebSocket endpoint named by the session's upstream URL.

    Retries transient failures on the proxy's own retry budget with a per-attempt
    open timeout, and maps failures onto the port error taxonomy.
    """

    def __init__(
        self,
        connect_headers: Mapping[str, str] | None = None,
        open_timeout_seconds: float = DEFAULT_OPEN_TIMEOUT_SECONDS,
    ) -> None:
        self._connect_headers = dict(connect_headers or {})
        self._open_timeout_seconds = open_timeout_seconds

    async def connect(self, session: ProxySession) -> UpstreamConnection:
        return await self.dial(
            session.upstream_ws_url, merge_connect_headers(self._connect_headers, session.connect_headers)
        )

    async def dial(self, url: str, connect_headers: Mapping[str, str] | None = None) -> UpstreamConnection:
        # Taxonomy errors are always raised OUTSIDE the except suite: raising inside
        # would re-attach the raw transport exception as __context__ at raise time,
        # and its text can echo the token-bearing URL.
        parsed: tuple[str, str] | None = None
        try:
            stripped = strip_browser_address_discriminator(url)
            parsed = (stripped, endpoint_label(stripped))
        except ValueError:
            pass
        if parsed is None:
            raise ProtocolConfigurationError("upstream URL is malformed")
        url, label = parsed
        headers = dict(connect_headers) if connect_headers is not None else self._connect_headers
        max_attempts, backoff_schedule = resolve_retry_budget()
        for attempt in range(1, max_attempts + 1):
            try:
                connector = websockets.connect(
                    url,
                    additional_headers=headers or None,
                    max_size=None,
                    open_timeout=self._open_timeout_seconds,
                    logger=_WEBSOCKETS_LOGGER,
                )
                # A CDP endpoint answering with a 3xx is not one; refusing to follow the
                # redirect stops websockets re-sending the vendor credential to another host.
                connector.process_redirect = _refuse_redirect
                ws = await connector
            except Exception as exc:
                error = classify_connect_error(exc, label)
                error_type = type(exc).__name__
            else:
                if attempt > 1:
                    LOG.info(
                        "upstream CDP connection recovered after retry", endpoint=label, successful_attempt=attempt
                    )
                return WebSocketUpstreamConnection(ws)
            if not isinstance(error, TransientConnectionError) or attempt == max_attempts:
                LOG.warning(
                    "upstream CDP connection failed",
                    endpoint=label,
                    attempt=attempt,
                    error_type=error_type,
                    category=type(error).__name__,
                )
                raise error
            backoff = backoff_schedule[min(attempt - 1, len(backoff_schedule) - 1)]
            LOG.warning(
                "upstream CDP connection failed, retrying",
                endpoint=label,
                attempt=attempt,
                max_attempts=max_attempts,
                backoff_seconds=backoff,
                error_type=error_type,
            )
            await asyncio.sleep(backoff)
        raise TransientConnectionError(f"connection to upstream at {label} failed")
