from __future__ import annotations

import asyncio
import base64
import inspect
import ssl
import urllib.request
from collections.abc import Callable, Coroutine, Mapping
from typing import Any, TypeVar, cast
from urllib.parse import unquote, urlparse

from websockets import exceptions as websocket_exceptions
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as WebSocketConnect
from websockets.datastructures import Headers
from websockets.exceptions import InvalidHandshake
from websockets.http11 import Response
from websockets.uri import WebSocketURI, parse_uri


class _CompatibilityInvalidProxyStatus(InvalidHandshake):
    def __init__(self, response: Response) -> None:
        self.response = response


def _invalid_proxy_status(response: Response) -> InvalidHandshake:
    try:
        exception_type = websocket_exceptions.InvalidProxyStatus
    except AttributeError:
        exception_type = _CompatibilityInvalidProxyStatus
    return exception_type(response)


def _invalid_proxy_message(message: str) -> InvalidHandshake:
    try:
        exception_type = websocket_exceptions.InvalidProxyMessage
    except AttributeError:
        exception_type = InvalidHandshake
    return exception_type(message)


_CONNECT_SUPPORTS_PROXY = "proxy" in inspect.signature(WebSocketConnect).parameters
_CANDIDATE_STAGGER_SECONDS = 0.3
_T = TypeVar("_T")


def _protocol_uri(uri: str, headers: Mapping[str, str] | None) -> tuple[str, dict[str, str] | None]:
    remaining = dict(headers or {})
    host = next((remaining.pop(key) for key in list(remaining) if key.lower() == "host"), None)
    if host is None:
        return uri, remaining or None
    parsed = urlparse(uri)
    userinfo, separator, _authority = parsed.netloc.rpartition("@")
    return parsed._replace(netloc=f"{userinfo}{separator}{host}").geturl(), remaining or None


def _proxy_parts(proxy: str) -> tuple[str, str, int, str | None, str | None]:
    parsed = urlparse(proxy)
    if parsed.scheme.startswith("socks"):
        raise _invalid_proxy_message("SOCKS proxies are not supported")
    if parsed.scheme not in ("http", "https"):
        raise ValueError("unsupported proxy scheme")
    if parsed.hostname is None or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("invalid proxy URL")
    if parsed.username is not None and parsed.password is None:
        raise ValueError("invalid proxy credentials")
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        unquote(parsed.username) if parsed.username is not None else None,
        unquote(parsed.password) if parsed.password is not None else None,
    )


def _proxy_authorization(username: str | None, password: str | None) -> str | None:
    if username is None:
        return None
    credentials = f"{username}:{password or ''}".encode()
    return f"Basic {base64.b64encode(credentials).decode()}"


async def _open_pinned_stream(
    *,
    host: str,
    port: int,
    secure: bool,
    server_hostname: str | None,
    proxy: str | None,
    forward_proxy: bool,
    ssl_context: ssl.SSLContext | None = None,
    user_agent_header: str | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict[str, str]]:
    if proxy is None:
        context = (ssl_context or ssl.create_default_context()) if secure else None
        reader, writer = await asyncio.open_connection(
            host,
            port,
            ssl=context,
            server_hostname=(server_hostname or host) if context is not None else None,
        )
        return reader, writer, {}

    scheme, proxy_host, proxy_port, username, password = _proxy_parts(proxy)
    proxy_context = ssl.create_default_context() if scheme == "https" else None
    reader, writer = await asyncio.open_connection(
        proxy_host,
        proxy_port,
        ssl=proxy_context,
        server_hostname=proxy_host if proxy_context is not None else None,
    )
    try:
        authorization = _proxy_authorization(username, password)
        if not forward_proxy:
            authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            headers = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
            if authorization is not None:
                headers += f"Proxy-Authorization: {authorization}\r\n"
            if user_agent_header is not None:
                headers += f"User-Agent: {user_agent_header}\r\n"
            writer.write(f"{headers}\r\n".encode())
            await writer.drain()
            status_line = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0]
            try:
                status_parts = status_line.split(b" ", 2)
                version, raw_status = status_parts[:2]
                raw_reason = status_parts[2] if len(status_parts) == 3 else b""
                if version not in (b"HTTP/1.0", b"HTTP/1.1"):
                    raise ValueError
                status = int(raw_status)
            except (IndexError, ValueError) as exc:
                raise _invalid_proxy_message("proxy returned an invalid response") from exc
            if not 200 <= status < 300:
                raise _invalid_proxy_status(Response(status, raw_reason.decode("latin-1"), Headers()))
        if secure:
            await writer.drain()
            if cast(Any, reader)._buffer:
                raise _invalid_proxy_message("proxy sent data before TLS negotiation")
            await writer.start_tls(ssl_context or ssl.create_default_context(), server_hostname=server_hostname or host)
        proxy_headers = {"Proxy-Authorization": authorization} if forward_proxy and authorization is not None else {}
        return reader, writer, proxy_headers
    except BaseException:
        writer.transport.abort()
        raise


def _environment_websocket_proxy(uri: str) -> str | None:
    parsed = parse_uri(uri)
    if urllib.request.proxy_bypass(f"{parsed.host}:{parsed.port}"):
        return None
    proxies = urllib.request.getproxies()
    schemes = ("wss", "socks", "https") if parsed.secure else ("ws", "socks", "https", "http")
    for scheme in schemes:
        proxy = proxies.get(scheme)
        if proxy is not None:
            if scheme == "socks" and proxy.startswith("http://"):
                return "socks5h://" + proxy[7:]
            return proxy
    return None


async def _create_pinned_transport(
    protocol_factory: Callable[[], asyncio.Protocol],
    *,
    pinned_uri: WebSocketURI,
    proxy: str | bool | None,
    proxy_source_uri: str | None,
    user_agent_header: str | None,
    connection_kwargs: dict[str, Any],
) -> tuple[asyncio.Transport, asyncio.Protocol]:
    loop = asyncio.get_running_loop()
    if proxy is True:
        assert proxy_source_uri is not None
        proxy = _environment_websocket_proxy(proxy_source_uri)
    if proxy is None or proxy is False:
        return await loop.create_connection(
            protocol_factory,
            host=pinned_uri.host,
            port=pinned_uri.port,
            **connection_kwargs,
        )

    target_ssl = connection_kwargs.pop("ssl", None)
    server_hostname = connection_kwargs.pop("server_hostname", None)
    if connection_kwargs:
        raise ValueError("unsupported proxy connection options")
    ssl_context = target_ssl if isinstance(target_ssl, ssl.SSLContext) else None
    _reader, writer, _proxy_headers = await _open_pinned_stream(
        host=pinned_uri.host,
        port=pinned_uri.port,
        secure=pinned_uri.secure,
        server_hostname=server_hostname,
        proxy=proxy,
        forward_proxy=False,
        ssl_context=ssl_context,
        user_agent_header=user_agent_header,
    )
    transport = cast(asyncio.Transport, writer.transport)
    try:
        protocol = protocol_factory()
        transport.set_protocol(protocol)
        protocol.connection_made(transport)
        # Keep the StreamWriter and its transport alive while the replacement protocol owns the connection.
        protocol.__dict__["_pinned_stream_writer"] = writer
        return transport, protocol
    except BaseException:
        transport.abort()
        raise


async def _open_first_candidate(
    connect_hosts: tuple[str, ...],
    open_candidate: Callable[[int, str], Coroutine[Any, Any, _T]],
    close_candidate: Callable[[_T], None],
    *,
    empty_message: str,
) -> _T:
    tasks = {asyncio.create_task(open_candidate(index, host)) for index, host in enumerate(connect_hosts)}
    errors: list[BaseException] = []
    connections: list[_T] = []
    winner: _T | None = None
    try:
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    connections.append(task.result())
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    errors.append(exc)
            if connections:
                winner = connections.pop()
                for pending in tasks:
                    pending.cancel()
                pending_results = await asyncio.gather(*tasks, return_exceptions=True)
                connections.extend(result for result in pending_results if not isinstance(result, BaseException))
                for connection in connections:
                    close_candidate(connection)
                connections.clear()
                tasks.clear()
                result, winner = winner, None
                return result
        if errors:
            raise errors[-1]
        raise OSError(empty_message)
    finally:
        for task in tasks:
            task.cancel()
        pending_results = await asyncio.gather(*tasks, return_exceptions=True)
        connections.extend(result for result in pending_results if not isinstance(result, BaseException))
        if winner is not None:
            connections.append(winner)
        for connection in connections:
            close_candidate(connection)


class _PinnedProxyConnect(WebSocketConnect):
    def __init__(
        self,
        uri: str,
        *,
        connect_host: str,
        connect_port: int,
        server_hostname: str | None,
        proxy_source_uri: str,
        proxy: str | bool | None,
        **kwargs: Any,
    ) -> None:
        self._connect_host = connect_host
        self._connect_port = connect_port
        self._server_hostname = server_hostname
        self._proxy_source_uri = proxy_source_uri
        self._pinned_proxy = proxy
        if _CONNECT_SUPPORTS_PROXY:
            kwargs["proxy"] = proxy
        super().__init__(uri, **kwargs)
        handshake_args = getattr(self, "handshake_args", None)
        if handshake_args is not None:
            self.additional_headers, self.user_agent_header = handshake_args

    async def create_connection(self) -> ClientConnection:
        kwargs = self.connection_kwargs.copy()
        ws_uri = parse_uri(self.uri)

        def factory() -> ClientConnection:
            return self.protocol_factory(ws_uri)

        if ws_uri.secure:
            kwargs.setdefault("ssl", True)
            kwargs.setdefault("server_hostname", self._server_hostname or ws_uri.host)

        pinned_uri = WebSocketURI(
            secure=ws_uri.secure,
            host=self._connect_host,
            port=self._connect_port,
            path=ws_uri.path,
            query=ws_uri.query,
        )
        _, connection = await _create_pinned_transport(
            factory,
            pinned_uri=pinned_uri,
            proxy=self._pinned_proxy,
            proxy_source_uri=self._proxy_source_uri,
            user_agent_header=self.user_agent_header,
            connection_kwargs=kwargs,
        )
        assert isinstance(connection, ClientConnection)
        return connection


def _pinned_connector(
    uri: str,
    *,
    connect_host: str,
    connect_port: int,
    server_hostname: str | None,
    headers: Mapping[str, str] | None,
    proxy: str | bool | None,
    open_timeout: float,
    max_size: int | None,
    ping_interval: float | None,
    ping_timeout: float | None,
    logger: Any,
) -> WebSocketConnect:
    protocol_uri, protocol_headers = _protocol_uri(uri, headers)
    kwargs: dict[str, Any] = {
        "additional_headers": protocol_headers,
        "max_size": max_size,
        "open_timeout": open_timeout,
        "ping_interval": ping_interval,
        "ping_timeout": ping_timeout,
        "logger": logger,
    }
    connector = _PinnedProxyConnect(
        protocol_uri,
        connect_host=connect_host,
        connect_port=connect_port,
        server_hostname=server_hostname,
        proxy_source_uri=uri,
        proxy=proxy,
        **kwargs,
    )
    connector.process_redirect = lambda exc: exc
    return connector


async def _open_pinned_websocket(
    connector: WebSocketConnect,
    connection: ClientConnection,
    *,
    deadline: float,
) -> ClientConnection:
    try:
        async with asyncio.timeout_at(deadline):
            await connection.handshake(connector.additional_headers, connector.user_agent_header)
    except TimeoutError as exc:
        connection.transport.abort()
        raise TimeoutError("timed out during opening handshake") from exc
    except BaseException:
        connection.transport.abort()
        raise
    connection.start_keepalive()
    return connection


async def open_first_pinned_websocket(
    uri: str,
    *,
    connect_hosts: tuple[str, ...],
    connect_port: int,
    server_hostname: str | None,
    headers: Mapping[str, str] | None,
    deadline: float,
    proxy: str | bool | None = True,
    max_size: int | None = None,
    ping_interval: float | None = 20,
    ping_timeout: float | None = 20,
    logger: Any = None,
) -> ClientConnection:
    async def open_candidate(index: int, host: str) -> tuple[WebSocketConnect, ClientConnection]:
        if index:
            await asyncio.sleep(index * _CANDIDATE_STAGGER_SECONDS)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("WebSocket connection attempt exceeded its deadline")
        connector = _pinned_connector(
            uri,
            connect_host=host,
            connect_port=connect_port,
            server_hostname=server_hostname,
            headers=headers,
            proxy=proxy,
            open_timeout=remaining,
            max_size=max_size,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            logger=logger,
        )
        try:
            async with asyncio.timeout_at(deadline):
                return connector, await connector.create_connection()
        except TimeoutError as exc:
            raise TimeoutError("timed out during opening handshake") from exc

    connector, connection = await _open_first_candidate(
        connect_hosts,
        open_candidate,
        lambda candidate: candidate[1].transport.abort(),
        empty_message="WebSocket endpoint had no connection candidates",
    )
    return await _open_pinned_websocket(connector, connection, deadline=deadline)


async def open_first_pinned_stream(
    *,
    connect_hosts: tuple[str, ...],
    connect_port: int,
    secure: bool,
    server_hostname: str | None,
    deadline: float,
    proxy: str | None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    async def open_candidate(index: int, host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if index:
            await asyncio.sleep(index * _CANDIDATE_STAGGER_SECONDS)
        async with asyncio.timeout_at(deadline):
            reader, writer, _ = await _open_pinned_stream(
                host=host,
                port=connect_port,
                secure=secure,
                server_hostname=server_hostname,
                proxy=proxy,
                forward_proxy=False,
            )
        return reader, writer

    return await _open_first_candidate(
        connect_hosts,
        open_candidate,
        lambda candidate: candidate[1].close(),
        empty_message="endpoint had no connection candidates",
    )


async def use_first_pinned_stream(
    *,
    connect_hosts: tuple[str, ...],
    connect_port: int,
    deadline: float,
    proxy: str,
    use: Callable[[str, Mapping[str, str], asyncio.StreamReader, asyncio.StreamWriter], Coroutine[Any, Any, _T]],
) -> _T:
    async def open_candidate(index: int, host: str) -> tuple[asyncio.StreamWriter, _T]:
        if index:
            await asyncio.sleep(index * _CANDIDATE_STAGGER_SECONDS)
        async with asyncio.timeout_at(deadline):
            reader, writer, proxy_headers = await _open_pinned_stream(
                host=host,
                port=connect_port,
                secure=False,
                server_hostname=None,
                proxy=proxy,
                forward_proxy=True,
            )
            try:
                return writer, await use(host, proxy_headers, reader, writer)
            except BaseException:
                writer.transport.abort()
                raise

    writer, result = await _open_first_candidate(
        connect_hosts,
        open_candidate,
        lambda candidate: candidate[0].close(),
        empty_message="endpoint had no connection candidates",
    )
    writer.close()
    return result
