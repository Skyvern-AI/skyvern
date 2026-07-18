"""Failure-taxonomy and header-injection tests for the generic CDP-over-WebSocket adapter."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from http import HTTPStatus
from unittest.mock import patch

import pytest
import websockets

from skyvern.proxy.adapters.websocket_upstream import (
    WebSocketUpstreamBrowser,
    classify_connect_error,
    merge_connect_headers,
    resolve_retry_budget,
)
from skyvern.proxy.core.errors import (
    ProtocolConfigurationError,
    TransientConnectionError,
    VendorAuthError,
    VendorRateLimitError,
)
from skyvern.proxy.core.session import ProxySession


@pytest.fixture(autouse=True)
def _tight_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("CDP_PROXY_CONNECT_BACKOFF_SECONDS", "0")


def test_retry_budget_env_knobs_and_misconfig_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", raising=False)
    monkeypatch.delenv("CDP_PROXY_CONNECT_BACKOFF_SECONDS", raising=False)
    assert resolve_retry_budget() == (6, (1.0, 2.0, 3.0, 4.0, 5.0))
    monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "0")
    monkeypatch.setenv("CDP_PROXY_CONNECT_BACKOFF_SECONDS", "nope")
    assert resolve_retry_budget() == (6, (1.0, 2.0, 3.0, 4.0, 5.0))
    monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("CDP_PROXY_CONNECT_BACKOFF_SECONDS", "-1")
    assert resolve_retry_budget() == (3, (1.0, 2.0, 3.0, 4.0, 5.0))
    monkeypatch.setenv("CDP_PROXY_CONNECT_BACKOFF_SECONDS", "0.5,1.5")
    assert resolve_retry_budget() == (3, (0.5, 1.5))
    monkeypatch.setenv("CDP_PROXY_CONNECT_BACKOFF_SECONDS", "nan")
    assert resolve_retry_budget() == (3, (1.0, 2.0, 3.0, 4.0, 5.0))
    monkeypatch.setenv("CDP_PROXY_CONNECT_BACKOFF_SECONDS", "inf")
    assert resolve_retry_budget() == (3, (1.0, 2.0, 3.0, 4.0, 5.0))
    monkeypatch.setenv("CDP_PROXY_CONNECT_BACKOFF_SECONDS", "301")
    assert resolve_retry_budget() == (3, (1.0, 2.0, 3.0, 4.0, 5.0))
    monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "101")
    monkeypatch.setenv("CDP_PROXY_CONNECT_BACKOFF_SECONDS", "1")
    assert resolve_retry_budget() == (6, (1.0,))


def _session(url: str) -> ProxySession:
    return ProxySession(session_id="test-session", upstream_ws_url=url)


# Bound but never listening: dialing it gets ECONNREFUSED while the bind holds the
# port reserved, avoiding a TOCTOU race with other processes grabbing it.
@pytest.fixture
def refused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()[1]


async def _echo(ws: websockets.ServerConnection) -> None:
    async for message in ws:
        await ws.send(message)


def _reject_with(status: HTTPStatus):
    def handler(connection: websockets.ServerConnection, request):  # type: ignore[no-untyped-def]
        return connection.respond(status, "rejected\n")

    return handler


async def _connect_expecting(status: HTTPStatus, expected_error: type[Exception]) -> Exception:
    async with websockets.serve(_echo, "127.0.0.1", 0, process_request=_reject_with(status)) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(expected_error) as excinfo:
            await WebSocketUpstreamBrowser().connect(_session(f"ws://127.0.0.1:{port}/devtools/browser/test"))
    return excinfo.value


@pytest.mark.asyncio
async def test_refused_connection_raises_transient(refused_port: int) -> None:
    with pytest.raises(TransientConnectionError):
        await WebSocketUpstreamBrowser().connect(_session(f"ws://127.0.0.1:{refused_port}/devtools/browser/test"))


@pytest.mark.asyncio
async def test_malformed_url_raises_protocol_configuration() -> None:
    with pytest.raises(ProtocolConfigurationError) as excinfo:
        await WebSocketUpstreamBrowser().connect(_session("ws://[invalid"))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.asyncio
async def test_auth_rejection_raises_vendor_auth() -> None:
    await _connect_expecting(HTTPStatus.UNAUTHORIZED, VendorAuthError)
    await _connect_expecting(HTTPStatus.FORBIDDEN, VendorAuthError)


@pytest.mark.asyncio
async def test_rate_limit_rejection_raises_vendor_rate_limit() -> None:
    await _connect_expecting(HTTPStatus.TOO_MANY_REQUESTS, VendorRateLimitError)


@pytest.mark.asyncio
async def test_non_websocket_endpoint_raises_protocol_configuration() -> None:
    await _connect_expecting(HTTPStatus.NOT_FOUND, ProtocolConfigurationError)


@pytest.mark.asyncio
async def test_server_error_rejection_is_transient() -> None:
    await _connect_expecting(HTTPStatus.SERVICE_UNAVAILABLE, TransientConnectionError)


@pytest.mark.asyncio
async def test_invalid_uri_raises_protocol_configuration() -> None:
    with pytest.raises(ProtocolConfigurationError):
        await WebSocketUpstreamBrowser().connect(_session("http://127.0.0.1:1/devtools/browser/test"))


@pytest.mark.asyncio
async def test_error_never_leaks_url_path_query_or_cause() -> None:
    async with websockets.serve(_echo, "127.0.0.1", 0, process_request=_reject_with(HTTPStatus.UNAUTHORIZED)) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(VendorAuthError) as excinfo:
            await WebSocketUpstreamBrowser().connect(
                _session(f"ws://127.0.0.1:{port}/devtools/browser/SECRETPATH?token=SECRETTOKEN")
            )
    message = str(excinfo.value)
    assert "SECRETPATH" not in message
    assert "SECRETTOKEN" not in message
    assert f"ws://127.0.0.1:{port}" in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.asyncio
async def test_transient_failure_retries_until_upstream_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "3")
    handshakes = {"count": 0}

    def flaky(connection: websockets.ServerConnection, request):  # type: ignore[no-untyped-def]
        handshakes["count"] += 1
        if handshakes["count"] == 1:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "warming up\n")
        return None

    async with websockets.serve(_echo, "127.0.0.1", 0, process_request=flaky) as server:
        port = server.sockets[0].getsockname()[1]
        connection = await WebSocketUpstreamBrowser().connect(_session(f"ws://127.0.0.1:{port}/devtools/browser/test"))
        await connection.send('{"id": 1, "method": "Browser.getVersion"}')
        assert await connection.receive() == '{"id": 1, "method": "Browser.getVersion"}'
        await connection.close()
    assert handshakes["count"] == 2


@pytest.mark.asyncio
async def test_mid_handshake_drop_is_transient_and_retries_to_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "3")
    connections = {"count": 0}

    async def drop_after_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections["count"] += 1
        await reader.read(1024)
        writer.close()

    server = await asyncio.start_server(drop_after_request, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(TransientConnectionError):
            await WebSocketUpstreamBrowser().connect(_session(f"ws://127.0.0.1:{port}/devtools/browser/test"))
    finally:
        server.close()
        await server.wait_closed()
    assert connections["count"] == 3


@pytest.mark.asyncio
async def test_persistent_transient_rejection_retries_to_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "3")
    handshakes = {"count": 0}

    def reject(connection: websockets.ServerConnection, request):  # type: ignore[no-untyped-def]
        handshakes["count"] += 1
        return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "unavailable\n")

    async with websockets.serve(_echo, "127.0.0.1", 0, process_request=reject) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(TransientConnectionError):
            await WebSocketUpstreamBrowser().connect(_session(f"ws://127.0.0.1:{port}/devtools/browser/test"))
    assert handshakes["count"] == 3


@pytest.mark.asyncio
async def test_non_transient_rejection_short_circuits_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "3")
    handshakes = {"count": 0}

    def reject(connection: websockets.ServerConnection, request):  # type: ignore[no-untyped-def]
        handshakes["count"] += 1
        return connection.respond(HTTPStatus.UNAUTHORIZED, "rejected\n")

    async with websockets.serve(_echo, "127.0.0.1", 0, process_request=reject) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(VendorAuthError):
            await WebSocketUpstreamBrowser().connect(_session(f"ws://127.0.0.1:{port}/devtools/browser/test"))
    assert handshakes["count"] == 1


@pytest.mark.asyncio
async def test_userinfo_credentials_never_leak_in_error_or_logs() -> None:
    async with websockets.serve(_echo, "127.0.0.1", 0, process_request=_reject_with(HTTPStatus.UNAUTHORIZED)) as server:
        port = server.sockets[0].getsockname()[1]
        with (
            patch("skyvern.proxy.adapters.websocket_upstream.LOG") as mock_log,
            pytest.raises(VendorAuthError) as excinfo,
        ):
            await WebSocketUpstreamBrowser().connect(
                _session(f"ws://user:SECRET@127.0.0.1:{port}/devtools/browser/test")
            )
    message = str(excinfo.value)
    assert f"ws://127.0.0.1:{port}" in message
    assert "SECRET" not in message
    assert "user:" not in message
    # The chain must be fully scrubbed: the raw transport exception carries the URL.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    logged = " ".join(
        str(value)
        for call in mock_log.warning.call_args_list + mock_log.info.call_args_list
        for value in call.kwargs.values()
    )
    assert "SECRET" not in logged
    assert "user:" not in logged


@pytest.mark.asyncio
async def test_open_timeout_is_transient() -> None:
    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()
        finally:
            writer.close()

    server = await asyncio.start_server(stall, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(TransientConnectionError):
            await WebSocketUpstreamBrowser(open_timeout_seconds=0.1).connect(
                _session(f"ws://127.0.0.1:{port}/devtools/browser/test")
            )
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_injected_headers_reach_the_upstream_handshake() -> None:
    seen: dict[str, str | None] = {}

    def capture(connection: websockets.ServerConnection, request):  # type: ignore[no-untyped-def]
        seen["authorization"] = request.headers.get("Authorization")
        return None

    async with websockets.serve(_echo, "127.0.0.1", 0, process_request=capture) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = WebSocketUpstreamBrowser(connect_headers={"Authorization": "Bearer test-token"})
        connection = await adapter.connect(_session(f"ws://127.0.0.1:{port}/devtools/browser/test"))
        await connection.close()
    assert seen["authorization"] == "Bearer test-token"


@pytest.mark.asyncio
async def test_browser_address_discriminator_fragment_is_stripped_before_dialing() -> None:
    async with websockets.serve(_echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        connection = await WebSocketUpstreamBrowser().connect(
            _session(f"ws://127.0.0.1:{port}/devtools/browser/test#pbs_123")
        )
        await connection.close()


@pytest.mark.asyncio
async def test_cross_origin_redirect_never_leaks_credentials_and_fails_closed() -> None:
    redirect_target: dict[str, str | None] = {}

    def capture_auth(connection: websockets.ServerConnection, request):  # type: ignore[no-untyped-def]
        redirect_target["authorization"] = request.headers.get("Authorization")
        return None

    async with websockets.serve(_echo, "127.0.0.1", 0, process_request=capture_auth) as target:
        target_port = target.sockets[0].getsockname()[1]

        def redirect_to_target(connection: websockets.ServerConnection, request):  # type: ignore[no-untyped-def]
            response = connection.respond(HTTPStatus.FOUND, "redirecting\n")
            response.headers["Location"] = f"ws://127.0.0.1:{target_port}/devtools/browser/test"
            return response

        async with websockets.serve(_echo, "127.0.0.1", 0, process_request=redirect_to_target) as redirector:
            redirect_port = redirector.sockets[0].getsockname()[1]
            adapter = WebSocketUpstreamBrowser(connect_headers={"Authorization": "Bearer redirect-secret"})
            with pytest.raises(ProtocolConfigurationError) as excinfo:
                await adapter.connect(_session(f"ws://127.0.0.1:{redirect_port}/devtools/browser/test"))
    assert redirect_target.get("authorization") is None
    assert "redirect-secret" not in str(excinfo.value)


def test_tls_verification_failure_classified_as_configuration_not_transient() -> None:
    error = classify_connect_error(ssl.SSLCertVerificationError("certificate verify failed"), "wss://127.0.0.1")
    assert isinstance(error, ProtocolConfigurationError)
    assert not isinstance(error, TransientConnectionError)


@pytest.mark.asyncio
async def test_tls_failure_fails_closed_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "3")
    dials = {"count": 0}

    class _TlsFailingConnector:
        process_redirect = None

        def __await__(self):  # type: ignore[no-untyped-def]
            dials["count"] += 1
            raise ssl.SSLCertVerificationError("certificate verify failed")
            yield  # unreachable; makes __await__ a generator

    monkeypatch.setattr(
        "skyvern.proxy.adapters.websocket_upstream.websockets.connect",
        lambda *args, **kwargs: _TlsFailingConnector(),
    )
    with pytest.raises(ProtocolConfigurationError):
        await WebSocketUpstreamBrowser().connect(_session("wss://127.0.0.1:9/devtools/browser/test"))
    assert dials["count"] == 1


@pytest.mark.asyncio
async def test_websocket_debug_handshake_logs_are_scrubbed_of_credentials(caplog: pytest.LogCaptureFixture) -> None:
    async with websockets.serve(_echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        caplog.set_level(logging.DEBUG, logger="websockets")
        adapter = WebSocketUpstreamBrowser(connect_headers={"Authorization": "Bearer log-secret"})
        connection = await adapter.connect(
            _session(f"ws://127.0.0.1:{port}/devtools/browser/SECRETPATH?token=SECRETTOKEN")
        )
        await connection.close()
    handshake = "\n".join(record.getMessage() for record in caplog.records if record.name == "websockets")
    assert "log-secret" not in handshake
    assert "Bearer" not in handshake
    assert "SECRETPATH" not in handshake
    assert "SECRETTOKEN" not in handshake
    # Guard against a vacuous pass: the DEBUG handshake must actually have been logged.
    assert any("HTTP/1.1" in record.getMessage() for record in caplog.records if record.name == "websockets")


def test_connect_headers_merge_case_insensitively_with_operator_value_winning() -> None:
    merged = merge_connect_headers(
        {"Authorization": "Bearer operator"},
        {"authorization": "Bearer session", "x-routing": "r1"},
    )
    assert merged == {"Authorization": "Bearer operator", "x-routing": "r1"}


def test_connect_headers_merge_keeps_session_only_headers() -> None:
    assert merge_connect_headers({}, {"x-routing": "r1"}) == {"x-routing": "r1"}
