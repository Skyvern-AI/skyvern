from __future__ import annotations

import asyncio
import base64
import http.client
import ipaddress
import json
import threading
from socket import SHUT_RDWR
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

DISCOVERY_MAX_BYTES = 64 * 1024
_DISCOVERY_DEADLINE_MESSAGE = "CDP discovery exceeded its deadline"


class CdpDiscoveryError(Exception):
    """The endpoint yielded no websocket URL; ``__cause__`` says why. Messages never name the URL."""


def fetch_discovery(url: str, timeout: float) -> dict[str, Any]:
    """GET /json/version under one deadline, a size cap and no redirects, because the caller chooses this URL.
    Stdlib http.client, because the auto-instrumented HTTP clients copy the URL and its credential onto spans."""
    parts = urlsplit(url)
    connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parts.hostname or "", parts.port, timeout=timeout)
    headers = {}
    if parts.username is not None:
        credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(credentials).decode()}"
    expired = threading.Event()
    # A socket timeout restarts on every byte received, so only shutting the socket down bounds the exchange.
    watchdog = threading.Timer(timeout, _abort_discovery, args=(connection, expired))
    watchdog.start()
    try:
        connection.connect()
        if expired.is_set():
            raise TimeoutError(_DISCOVERY_DEADLINE_MESSAGE)
        connection.request("GET", urlunsplit(("", "", parts.path or "/", parts.query, "")), headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"CDP discovery returned HTTP {response.status}")
        # A longer reply is cut mid-JSON and fails to parse below.
        body = response.read(DISCOVERY_MAX_BYTES)
    except (OSError, http.client.HTTPException) as error:
        if expired.is_set():
            raise TimeoutError(_DISCOVERY_DEADLINE_MESSAGE) from error
        raise
    finally:
        watchdog.cancel()
        connection.close()
    if expired.is_set():
        raise TimeoutError(_DISCOVERY_DEADLINE_MESSAGE)
    return dict(json.loads(body))


def _abort_discovery(connection: http.client.HTTPConnection, expired: threading.Event) -> None:
    expired.set()
    if connection.sock is not None:
        try:
            connection.sock.shutdown(SHUT_RDWR)
        except OSError:
            pass


def _is_loopback(host: str | None) -> bool:
    if not host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() == "localhost" or host.lower().endswith(".localhost")
    return address.is_loopback or address.is_unspecified


def _authority(parts: SplitResult) -> tuple[str, int]:
    return (parts.hostname or "").lower(), parts.port or (443 if parts.scheme in ("https", "wss") else 80)


async def resolve_websocket_url(endpoint_url: str, *, timeout_ms: float) -> str:
    """Turn a CDP endpoint into the websocket URL to dial. The result may name a different host than the endpoint,
    so callers must apply their address policy to it before dialing."""
    parsed = urlsplit(endpoint_url)
    if parsed.scheme in ("ws", "wss"):
        return endpoint_url
    if parsed.scheme not in ("http", "https"):
        raise CdpDiscoveryError(f"unsupported CDP endpoint scheme {parsed.scheme!r}")
    if not parsed.hostname:
        raise CdpDiscoveryError("the CDP endpoint names no host")

    # Userinfo and query ride along, and no error names the URL: either can be the endpoint's credential.
    version_url = urlunsplit(
        (parsed.scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/json/version", parsed.query, "")
    )
    try:
        payload = await asyncio.to_thread(fetch_discovery, version_url, max(timeout_ms / 1000, 1))
    except Exception as exc:
        raise CdpDiscoveryError(f"could not read the endpoint's /json/version ({type(exc).__name__})") from exc

    reported = payload.get("webSocketDebuggerUrl")
    if not reported:
        raise CdpDiscoveryError("the endpoint's /json/version returned no webSocketDebuggerUrl")

    reported_parts = urlsplit(str(reported))
    # A browser behind a tunnel or port-forward reports itself as loopback, which is only reachable where the caller
    # reached it; any other host is where the websocket really lives, as a vendor on a separate host or port reports.
    if _is_loopback(reported_parts.hostname) or _authority(reported_parts) == _authority(parsed):
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit(
            (ws_scheme, parsed.netloc, reported_parts.path or "/", reported_parts.query or parsed.query, "")
        )
    return str(reported)
