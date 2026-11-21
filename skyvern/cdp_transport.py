from __future__ import annotations

import asyncio
import inspect
import ssl
from collections.abc import Callable, Coroutine, Mapping
from typing import Any, TypeVar
from urllib.parse import urlparse

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as WebSocketConnect
from websockets.uri import WebSocketURI, parse_uri

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


async def _create_pinned_transport(
    protocol_factory: Callable[[], asyncio.Protocol],
    *,
    pinned_uri: WebSocketURI,
    proxy: str | bool | None,
    proxy_source_uri: str | None,
    user_agent_header: str | None,
    connection_kwargs: dict[str, Any],
) -> tuple[asyncio.Transport, asyncio.Protocol]:
    from websockets.asyncio.client import connect_http_proxy, connect_socks_proxy
    from websockets.uri import get_proxy, parse_proxy

    loop = asyncio.get_running_loop()
    if proxy is True:
        assert proxy_source_uri is not None
        proxy = get_proxy(parse_uri(proxy_source_uri))
    if proxy is None:
        return await loop.create_connection(
            protocol_factory,
            host=pinned_uri.host,
            port=pinned_uri.port,
            **connection_kwargs,
        )

    proxy_parsed = parse_proxy(proxy)
    if proxy_parsed.scheme[:5] == "socks":
        sock = await connect_socks_proxy(
            proxy_parsed,
            pinned_uri,
            local_addr=connection_kwargs.pop("local_addr", None),
        )
        return await loop.create_connection(protocol_factory, sock=sock, **connection_kwargs)
    if proxy_parsed.scheme[:4] != "http":
        raise AssertionError("unsupported proxy")

    target_kwargs: dict[str, Any] = {}
    proxy_kwargs: dict[str, Any] = {}
    for key, value in connection_kwargs.items():
        if key.startswith("ssl") or key == "server_hostname":
            target_kwargs[key] = value
        elif key.startswith("proxy_"):
            proxy_kwargs[key[6:]] = value
        else:
            proxy_kwargs[key] = value
    if proxy_parsed.scheme == "https":
        proxy_kwargs.setdefault("ssl", True)
    transport = await connect_http_proxy(
        proxy_parsed,
        pinned_uri,
        user_agent_header=user_agent_header,
        **proxy_kwargs,
    )
    try:
        protocol = protocol_factory()
        transport.set_protocol(protocol)
        target_ssl = target_kwargs.pop("ssl", None)
        if target_ssl is True:
            target_ssl = ssl.create_default_context()
        if target_ssl is not None:
            upgraded = await loop.start_tls(transport, protocol, target_ssl, **target_kwargs)
            assert upgraded is not None
            transport = upgraded
        protocol.connection_made(transport)
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
        **kwargs: Any,
    ) -> None:
        self._connect_host = connect_host
        self._connect_port = connect_port
        self._server_hostname = server_hostname
        self._proxy_source_uri = proxy_source_uri
        super().__init__(uri, **kwargs)

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
            proxy=self.proxy,
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
    if _CONNECT_SUPPORTS_PROXY:
        connector = _PinnedProxyConnect(
            protocol_uri,
            connect_host=connect_host,
            connect_port=connect_port,
            server_hostname=server_hostname,
            proxy_source_uri=uri,
            proxy=proxy,
            **kwargs,
        )
    else:
        if urlparse(protocol_uri).scheme == "wss":
            kwargs["server_hostname"] = server_hostname
        connector = WebSocketConnect(protocol_uri, host=connect_host, port=connect_port, **kwargs)
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
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        kwargs: dict[str, Any] = {}
        if secure:
            kwargs = {"ssl": True, "server_hostname": server_hostname or host}
        pinned_uri = WebSocketURI(secure=secure, host=host, port=connect_port, path="", query="")
        async with asyncio.timeout_at(deadline):
            transport, _ = await _create_pinned_transport(
                lambda: protocol,
                pinned_uri=pinned_uri,
                proxy=proxy,
                proxy_source_uri=None,
                user_agent_header=None,
                connection_kwargs=kwargs,
            )
        return reader, asyncio.StreamWriter(transport, protocol, reader, asyncio.get_running_loop())

    return await _open_first_candidate(
        connect_hosts,
        open_candidate,
        lambda candidate: candidate[1].close(),
        empty_message="endpoint had no connection candidates",
    )
