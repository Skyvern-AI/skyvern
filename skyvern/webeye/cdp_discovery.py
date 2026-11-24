from __future__ import annotations

import asyncio
import base64
import http.client
import io
import ipaddress
import json
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import Mapping
from contextlib import suppress
from queue import Empty, Queue
from socket import SHUT_RDWR
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

from websockets.datastructures import Headers
from websockets.exceptions import InvalidHandshake

from skyvern.cdp_transport import open_first_pinned_stream, use_first_pinned_stream

DISCOVERY_MAX_BYTES = 64 * 1024
_DISCOVERY_DEADLINE_MESSAGE = "CDP discovery exceeded its deadline"
_DISCOVERY_CANDIDATE_STAGGER_SECONDS = 0.3


class CdpDiscoveryError(Exception):
    """The endpoint yielded no websocket URL; ``__cause__`` says why. Messages never name the URL."""


def fetch_discovery(
    url: str,
    timeout: float,
    *,
    resolved_ips: tuple[str, ...] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """GET /json/version under one deadline, a size cap and no redirects, because the caller chooses this URL.
    Stdlib http.client, because the auto-instrumented HTTP clients copy the URL and its credential onto spans."""
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    tls_context = ssl.create_default_context() if parts.scheme == "https" and resolved_ips else None
    connection = (
        http.client.HTTPSConnection(hostname, parts.port, timeout=timeout, context=tls_context)
        if parts.scheme == "https"
        else http.client.HTTPConnection(hostname, parts.port, timeout=timeout)
    )
    request_headers = dict(headers or {})
    if parts.username is not None and not any(key.lower() == "authorization" for key in request_headers):
        credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        request_headers["Authorization"] = f"Basic {base64.b64encode(credentials).decode()}"
    expired = threading.Event()
    # A socket timeout restarts on every byte received, so only shutting the socket down bounds the exchange.
    watchdog = threading.Timer(timeout, _abort_discovery, args=(connection, expired))
    watchdog.start()
    try:
        if resolved_ips:
            deadline = time.monotonic() + timeout
            connection.sock = _connect_discovery_address(
                resolved_ips,
                parts.port or (443 if parts.scheme == "https" else 80),
                deadline,
                tls_context=tls_context,
                server_hostname=hostname,
            )
        else:
            connection.connect()
        if expired.is_set():
            raise TimeoutError(_DISCOVERY_DEADLINE_MESSAGE)
        connection.request("GET", urlunsplit(("", "", parts.path or "/", parts.query, "")), headers=request_headers)
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


def _connect_discovery_address(
    addresses: tuple[str, ...],
    port: int,
    deadline: float,
    *,
    tls_context: ssl.SSLContext | None,
    server_hostname: str,
) -> socket.socket:
    results: Queue[socket.socket | OSError] = Queue()
    selected = threading.Event()
    selection_lock = threading.Lock()

    def connect(index: int, address: str) -> None:
        if selected.wait(index * _DISCOVERY_CANDIDATE_STAGGER_SECONDS):
            return
        connected: socket.socket | None = None
        try:
            connected = socket.create_connection(
                (address, port),
                timeout=max(deadline - time.monotonic(), 0.001),
            )
            if tls_context is not None:
                connected = tls_context.wrap_socket(connected, server_hostname=server_hostname)
        except OSError as error:
            if connected is not None:
                connected.close()
            results.put(error)
            return
        with selection_lock:
            if selected.is_set():
                connected.close()
            else:
                selected.set()
                results.put(connected)

    for index, address in enumerate(addresses):
        threading.Thread(target=connect, args=(index, address), daemon=True).start()

    last_error: OSError | None = None
    for _ in addresses:
        try:
            result = results.get(timeout=max(deadline - time.monotonic(), 0.001))
        except Empty as error:
            selected.set()
            raise TimeoutError(_DISCOVERY_DEADLINE_MESSAGE) from last_error or error
        if isinstance(result, OSError):
            last_error = result
        else:
            return result
    assert last_error is not None
    raise last_error


def _abort_discovery(connection: http.client.HTTPConnection, expired: threading.Event) -> None:
    expired.set()
    if connection.sock is not None:
        try:
            connection.sock.shutdown(SHUT_RDWR)
        except OSError:
            pass


def _environment_proxy(parts: SplitResult) -> str | None:
    proxies = urllib.request.getproxies()
    proxy = proxies.get(parts.scheme) or proxies.get("all")
    if not proxy or urllib.request.proxy_bypass(parts.hostname or ""):
        return None
    return proxy


async def _read_proxy_response(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    status_line, raw_headers = head.split(b"\r\n", 1)
    try:
        version, raw_status, _reason = status_line.split(b" ", 2)
        if version not in (b"HTTP/1.0", b"HTTP/1.1") or len(raw_status) != 3:
            raise ValueError
        status_code = int(raw_status)
        headers = http.client.parse_headers(io.BytesIO(raw_headers))
    except (IndexError, ValueError) as exc:
        raise ValueError("CDP discovery returned invalid headers") from exc
    return status_code, await _read_proxy_body(reader, status_code, headers)


async def _read_proxy_body(reader: asyncio.StreamReader, status_code: int, headers: http.client.HTTPMessage) -> bytes:
    if 100 <= status_code < 200 or status_code in (204, 304):
        return b""
    if coding := headers.get("Transfer-Encoding"):
        if coding.lower() != "chunked":
            raise ValueError("CDP discovery returned unsupported transfer encoding")
        body = bytearray()
        while True:
            chunk_size = int((await reader.readuntil(b"\r\n")).split(b";", 1)[0], 16)
            if not chunk_size:
                while await reader.readuntil(b"\r\n") != b"\r\n":
                    pass
                return bytes(body)
            if len(body) + chunk_size > DISCOVERY_MAX_BYTES:
                raise ValueError("CDP discovery response exceeded its size limit")
            chunk = await reader.readexactly(chunk_size + 2)
            if chunk[-2:] != b"\r\n":
                raise ValueError("CDP discovery returned an invalid chunk")
            body.extend(chunk[:-2])
    if raw_length := headers.get("Content-Length"):
        length = int(raw_length)
        if not 0 <= length <= DISCOVERY_MAX_BYTES:
            raise ValueError("CDP discovery response exceeded its size limit")
        return await reader.readexactly(length)
    body = bytearray()
    while chunk := await reader.read(DISCOVERY_MAX_BYTES + 1 - len(body)):
        body.extend(chunk)
        if len(body) > DISCOVERY_MAX_BYTES:
            raise ValueError("CDP discovery response exceeded its size limit")
    return bytes(body)


async def _fetch_discovery_through_proxy(
    url: str,
    timeout: float,
    *,
    resolved_ips: tuple[str, ...] | None,
    headers: dict[str, str] | None,
    proxy_url: str,
) -> dict[str, Any]:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    deadline = asyncio.get_running_loop().time() + timeout
    request_headers = dict(headers or {})
    if not any(key.lower() == "host" for key in request_headers):
        request_headers["Host"] = parts.netloc.rpartition("@")[2]
    if not any(key.lower() == "connection" for key in request_headers):
        request_headers["Connection"] = "close"
    if parts.username is not None and not any(key.lower() == "authorization" for key in request_headers):
        credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        request_headers["Authorization"] = f"Basic {base64.b64encode(credentials).decode()}"
    resource = urlunsplit(("", "", parts.path or "/", parts.query, ""))

    async def request(
        connect_host: str,
        proxy_headers: Mapping[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        forward: bool,
    ) -> dict[str, Any]:
        wire_resource = resource
        if forward:
            authority = f"[{connect_host}]:{port}" if ":" in connect_host else f"{connect_host}:{port}"
            wire_resource = urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))
        wire_headers = Headers({**request_headers, **proxy_headers})
        async with asyncio.timeout_at(deadline):
            writer.write(f"GET {wire_resource} HTTP/1.1\r\n".encode() + wire_headers.serialize())
            await writer.drain()
            status_code, body = await _read_proxy_response(reader)
            if status_code != 200:
                raise ValueError(f"CDP discovery returned HTTP {status_code}")
            return dict(json.loads(body))

    proxy_scheme = urlsplit(proxy_url).scheme.lower()
    if parts.scheme == "http" and proxy_scheme in ("http", "https") and resolved_ips is None:
        return await use_first_pinned_stream(
            connect_hosts=resolved_ips or (hostname,),
            connect_port=port,
            deadline=deadline,
            proxy=proxy_url,
            use=lambda host, proxy_headers, reader, writer: request(host, proxy_headers, reader, writer, forward=True),
        )

    try:
        reader, writer = await open_first_pinned_stream(
            connect_hosts=resolved_ips or (hostname,),
            connect_port=port,
            secure=parts.scheme == "https",
            server_hostname=hostname,
            deadline=deadline,
            proxy=proxy_url,
        )
    except (OSError, TimeoutError, InvalidHandshake):
        raise
    except Exception as exc:
        raise OSError("discovery proxy connection failed") from exc

    try:
        payload = await request("", {}, reader, writer, forward=False)
    except BaseException:
        writer.transport.abort()
        raise
    else:
        writer.close()
        with suppress(Exception):
            async with asyncio.timeout_at(deadline):
                await writer.wait_closed()
    return payload


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


async def resolve_websocket_url(
    endpoint_url: str,
    *,
    timeout_ms: float,
    resolved_ips: tuple[str, ...] | None = None,
    headers: dict[str, str] | None = None,
) -> str:
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
        timeout = max(timeout_ms / 1000, 1)
        proxy_url = _environment_proxy(parsed)
        if proxy_url is None:
            payload = await asyncio.to_thread(
                fetch_discovery,
                version_url,
                timeout,
                resolved_ips=resolved_ips,
                headers=headers,
            )
        else:
            payload = await _fetch_discovery_through_proxy(
                version_url,
                timeout,
                resolved_ips=resolved_ips,
                headers=headers,
                proxy_url=proxy_url,
            )
    except Exception as exc:
        raise CdpDiscoveryError(f"could not read the endpoint's /json/version ({type(exc).__name__})") from exc

    reported = payload.get("webSocketDebuggerUrl")
    if not reported:
        raise CdpDiscoveryError("the endpoint's /json/version returned no webSocketDebuggerUrl")

    reported_parts = urlsplit(str(reported))
    if reported_parts.scheme not in ("ws", "wss") or not reported_parts.hostname:
        raise CdpDiscoveryError("the endpoint's /json/version returned an invalid WebSocket URL")
    # A browser behind a tunnel or port-forward reports itself as loopback, which is only reachable where the caller
    # reached it; any other host is where the websocket really lives, as a vendor on a separate host or port reports.
    if _is_loopback(reported_parts.hostname) or _authority(reported_parts) == _authority(parsed):
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit(
            (ws_scheme, parsed.netloc, reported_parts.path or "/", reported_parts.query or parsed.query, "")
        )
    return str(reported)
