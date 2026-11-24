"""Resolving a DevTools HTTP endpoint into the websocket URL to actually dial.

Chrome's ``/json/version`` reports a ``webSocketDebuggerUrl`` describing itself from *its own*
point of view, which is routinely not reachable from where the client is standing. Two observed
cases from a container on the same docker network as Chromium 151:

- asked with ``Host: localhost``, it answers ``ws://localhost/devtools/browser/<uuid>`` -- no port
  at all, and a hostname that resolves to the client's own container;
- asked by IP literal, it answers ``ws://172.18.0.2:9222/devtools/browser/<uuid>``.

Only the path is trustworthy: it carries the browser's UUID. The authority has to come from the
endpoint the caller supplied, because that is the address the caller can actually reach.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
from pathlib import Path
from typing import Any

import pytest
import websockets
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from skyvern.webeye.skycdp.errors import CdpConnectionError
from skyvern.webeye.skycdp.facade.browser import _resolve_websocket_url

pytestmark = pytest.mark.asyncio


class FakeDiscovery:
    """Stands in for the HTTP fetch of /json/version."""

    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self.payload = payload
        self.requested_url: str | None = None
        self.requested_headers: dict[str, str] | None = None

    def __call__(self, url: str, headers: dict[str, str] | None, timeout: float) -> dict[str, Any]:
        self.requested_url = url
        self.requested_headers = headers
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


async def test_a_ws_url_is_used_as_given() -> None:
    assert await _resolve_websocket_url("ws://browser:9222/devtools/browser/abc", headers=None, timeout_ms=1000) == (
        "ws://browser:9222/devtools/browser/abc"
    )
    assert await _resolve_websocket_url("wss://remote/devtools/browser/x", headers=None, timeout_ms=1000) == (
        "wss://remote/devtools/browser/x"
    )


async def test_portless_localhost_answer_is_rewritten_to_the_endpoint_the_caller_can_reach() -> None:
    """The container case: following Chrome's answer verbatim would dial ws://localhost:80."""
    discovery = FakeDiscovery({"webSocketDebuggerUrl": "ws://localhost/devtools/browser/uuid-1"})
    resolved = await _resolve_websocket_url(
        "http://skycdp-browser:9222", headers={"Host": "localhost"}, timeout_ms=1000, fetch=discovery
    )
    assert resolved == "ws://skycdp-browser:9222/devtools/browser/uuid-1"


async def test_an_answer_naming_a_different_host_is_still_rewritten() -> None:
    discovery = FakeDiscovery({"webSocketDebuggerUrl": "ws://172.18.0.2:9222/devtools/browser/uuid-2"})
    resolved = await _resolve_websocket_url("http://browser:9222", headers=None, timeout_ms=1000, fetch=discovery)
    assert resolved == "ws://browser:9222/devtools/browser/uuid-2"


async def test_https_endpoints_dial_wss() -> None:
    discovery = FakeDiscovery({"webSocketDebuggerUrl": "ws://localhost/devtools/browser/uuid-3"})
    resolved = await _resolve_websocket_url(
        "https://remote.example:443", headers=None, timeout_ms=1000, fetch=discovery
    )
    assert resolved == "wss://remote.example:443/devtools/browser/uuid-3"


async def test_the_discovery_request_targets_json_version_and_carries_supplied_headers() -> None:
    discovery = FakeDiscovery({"webSocketDebuggerUrl": "ws://localhost/devtools/browser/uuid-4"})
    await _resolve_websocket_url(
        "http://browser:9222/", headers={"Host": "localhost", "x-api-key": "k"}, timeout_ms=1000, fetch=discovery
    )
    assert discovery.requested_url == "http://browser:9222/json/version"
    assert discovery.requested_headers == {"Host": "localhost", "x-api-key": "k"}


async def test_a_missing_websocket_url_is_reported_rather_than_guessed() -> None:
    discovery = FakeDiscovery({"Browser": "Chrome/151"})
    with pytest.raises(CdpConnectionError) as excinfo:
        await _resolve_websocket_url("http://browser:9222", headers=None, timeout_ms=1000, fetch=discovery)
    assert "webSocketDebuggerUrl" in str(excinfo.value)


async def test_an_unreachable_endpoint_names_what_it_tried() -> None:
    discovery = FakeDiscovery(OSError("connection refused"))
    with pytest.raises(CdpConnectionError) as excinfo:
        await _resolve_websocket_url("http://browser:9222", headers=None, timeout_ms=1000, fetch=discovery)
    assert "/json/version" in str(excinfo.value)


async def test_an_unsupported_scheme_is_rejected() -> None:
    with pytest.raises(CdpConnectionError):
        await _resolve_websocket_url("tcp://browser:9222", headers=None, timeout_ms=1000)


async def test_a_chrome_style_host_header_rejection_is_explained() -> None:
    """Chrome answers 200 with a plain-text refusal, not JSON, when the Host header displeases it."""
    discovery = FakeDiscovery(json.JSONDecodeError("Expecting value", "Host header is specified", 0))
    with pytest.raises(CdpConnectionError) as excinfo:
        await _resolve_websocket_url("http://browser:9222", headers=None, timeout_ms=1000, fetch=discovery)
    message = str(excinfo.value)
    assert "Host" in message and "9222" in message


@pytest.mark.parametrize("proxy_supported", [True, False])
async def test_a_host_header_is_turned_into_a_uri_rewrite_plus_connection_routing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proxy_supported: bool
) -> None:
    """Chrome checks the Host header on the websocket upgrade too, not just on /json/version.

    The websockets client derives Host from the URI and ignores an override in additional_headers, so
    honouring a caller's Host means putting it in the URI and routing the TCP connection separately.
    """
    from skyvern.webeye.skycdp.facade.browser import plan_websocket_dial

    plan = plan_websocket_dial(
        "ws://synthetic-user:synthetic-pass@skycdp-browser:9222/devtools/browser/uuid",
        headers={"Host": "localhost"},
    )
    # The caller's Host is sent verbatim. Silently appending the original port would change what the
    # server sees, and a Host header is an explicit instruction about exactly that.
    assert plan.uri == "ws://synthetic-user:synthetic-pass@localhost/devtools/browser/uuid"
    assert plan.connect_host == "skycdp-browser"
    assert plan.connect_port == 9222
    # The Host must not also be sent as an ordinary header, or it goes on the wire twice.
    assert "Host" not in (plan.headers or {})

    from skyvern import cdp_transport as websocket_transport
    from skyvern.cdp_transport import open_first_pinned_stream, open_first_pinned_websocket

    tls_name = "tls.example.test"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, tls_name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(tls_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(certificate_path, key_path)
    seen_sni: list[str | None] = []
    seen_hosts: list[str | None] = []
    seen_connect: list[str] = []
    tls_context.set_servername_callback(lambda _socket, server_name, _context: seen_sni.append(server_name))

    async def capture_host(_connection: websockets.ServerConnection, request: Any) -> None:
        seen_hosts.append(request.headers.get("Host"))
        await asyncio.sleep(0.7)

    async def echo(connection: websockets.ServerConnection) -> None:
        await connection.close()

    async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        seen_connect.append(request.split(b"\r\n", 1)[0].decode())
        target_port = int(request.split(b" ", 2)[1].rsplit(b":", 1)[1])
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", target_port)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async def forward(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            try:
                while chunk := await source.read(65536):
                    sink.write(chunk)
                    await sink.drain()
            finally:
                sink.close()

        await asyncio.gather(forward(reader, upstream_writer), forward(upstream_reader, writer))

    async def raw_tls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.close()

    poisoned_reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"

    async def forward_poisoned(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while chunk := await source.read(65536):
                sink.write(chunk)
                await sink.drain()
        except ConnectionError:
            pass
        finally:
            sink.close()

    async def poisoned_connect_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        target_port = int(request.split(b" ", 2)[1].rsplit(b":", 1)[1])
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", target_port)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n" + poisoned_reply)
        await writer.drain()
        await asyncio.gather(forward_poisoned(reader, upstream_writer), forward_poisoned(upstream_reader, writer))

    async def reasonless_auth_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 407\r\n\r\n")
        await writer.drain()
        writer.close()

    async def malformed_connect_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"not-http\r\n\r\n")
        await writer.drain()
        writer.close()

    raw_tls_server = await asyncio.start_server(raw_tls, "127.0.0.1", 0, ssl=tls_context)
    async with (
        raw_tls_server,
        websockets.serve(echo, "127.0.0.1", 0, ssl=tls_context, process_request=capture_host) as tls_server,
    ):
        tls_port = tls_server.sockets[0].getsockname()[1]
        raw_tls_port = raw_tls_server.sockets[0].getsockname()[1]
        proxy_server = await asyncio.start_server(proxy, "127.0.0.1", 0)
        async with proxy_server:
            proxy_port = proxy_server.sockets[0].getsockname()[1]
            monkeypatch.setenv("SSL_CERT_FILE", str(certificate_path))
            monkeypatch.setattr(websocket_transport, "_CONNECT_SUPPORTS_PROXY", proxy_supported)
            monkeypatch.setenv("wss_proxy", f"http://127.0.0.1:{proxy_port}")
            if proxy_supported:
                monkeypatch.delattr("websockets.asyncio.client.connect_http_proxy", raising=False)
                monkeypatch.delattr("websockets.asyncio.client.connect_socks_proxy", raising=False)
                monkeypatch.delattr("websockets.uri.get_proxy", raising=False)
                monkeypatch.delattr("websockets.uri.parse_proxy", raising=False)
            monkeypatch.setenv("no_proxy", "")
            connection = await open_first_pinned_websocket(
                f"wss://{tls_name}:{tls_port}/devtools/browser/fake",
                connect_hosts=("127.0.0.1", "127.0.0.1", "127.0.0.1"),
                connect_port=tls_port,
                server_hostname=tls_name,
                headers={"Host": "localhost"},
                deadline=asyncio.get_running_loop().time() + 4,
            )
            await connection.close()

            if proxy_supported:
                _reader, writer = await open_first_pinned_stream(
                    connect_hosts=("127.0.0.1",),
                    connect_port=raw_tls_port,
                    secure=True,
                    server_hostname=tls_name,
                    deadline=asyncio.get_running_loop().time() + 4,
                    proxy=f"http://127.0.0.1:{proxy_port}",
                )
                writer.close()
                await writer.wait_closed()

            poisoned_connect_server = await asyncio.start_server(poisoned_connect_proxy, "127.0.0.1", 0)
            async with poisoned_connect_server:
                with pytest.raises(
                    websockets.exceptions.InvalidHandshake, match="proxy sent data before TLS negotiation"
                ):
                    await open_first_pinned_stream(
                        connect_hosts=("127.0.0.1",),
                        connect_port=raw_tls_port,
                        secure=True,
                        server_hostname=tls_name,
                        deadline=asyncio.get_running_loop().time() + 4,
                        proxy=f"http://127.0.0.1:{poisoned_connect_server.sockets[0].getsockname()[1]}",
                    )

            reasonless_auth_server = await asyncio.start_server(reasonless_auth_proxy, "127.0.0.1", 0)
            malformed_connect_server = await asyncio.start_server(malformed_connect_proxy, "127.0.0.1", 0)
            async with reasonless_auth_server, malformed_connect_server:
                with pytest.raises(websockets.exceptions.InvalidHandshake) as auth_exc:
                    await open_first_pinned_stream(
                        connect_hosts=("127.0.0.1",),
                        connect_port=raw_tls_port,
                        secure=False,
                        server_hostname=None,
                        deadline=asyncio.get_running_loop().time() + 4,
                        proxy=f"http://127.0.0.1:{reasonless_auth_server.sockets[0].getsockname()[1]}",
                    )
                assert auth_exc.value.response.status_code == 407  # type: ignore[attr-defined]

                with pytest.raises(websockets.exceptions.InvalidHandshake):
                    await open_first_pinned_stream(
                        connect_hosts=("127.0.0.1",),
                        connect_port=raw_tls_port,
                        secure=False,
                        server_hostname=None,
                        deadline=asyncio.get_running_loop().time() + 4,
                        proxy=f"http://127.0.0.1:{malformed_connect_server.sockets[0].getsockname()[1]}",
                    )

            with pytest.raises(websockets.exceptions.InvalidHandshake, match="SOCKS proxies are not supported"):
                websocket_transport._proxy_parts("socks5://127.0.0.1:1080")

    assert seen_connect == [f"CONNECT 127.0.0.1:{tls_port} HTTP/1.1"] + (
        [f"CONNECT 127.0.0.1:{raw_tls_port} HTTP/1.1"] if proxy_supported else []
    )
    assert seen_sni == [tls_name] * (2 if proxy_supported else 1)
    assert seen_hosts == ["localhost"]

    release_candidates = asyncio.Event()
    candidate_connections: list[Any] = []

    class CandidateConnection:
        def __init__(self) -> None:
            self.closed = False
            self.handshake_count = 0
            self.transport = self

        def abort(self) -> None:
            self.closed = True

        async def handshake(self, _headers: Any, _user_agent: Any) -> None:
            self.handshake_count += 1

        def start_keepalive(self) -> None:
            pass

        async def close(self) -> None:
            self.closed = True

    class CandidateConnector:
        additional_headers = None
        user_agent_header = None

        def __init__(self, create_connection: Any) -> None:
            self.create_connection = create_connection

    def candidate_connector(*args: Any, **kwargs: Any) -> CandidateConnector:
        async def create_connection() -> CandidateConnection:
            candidate = CandidateConnection()
            candidate_connections.append(candidate)
            if len(candidate_connections) == 2:
                release_candidates.set()
            await release_candidates.wait()
            return candidate

        return CandidateConnector(create_connection)

    monkeypatch.setattr(websocket_transport, "_pinned_connector", candidate_connector)
    winner = await open_first_pinned_websocket(
        "ws://cdp.example.test/devtools/browser/fake",
        connect_hosts=("192.0.2.1", "192.0.2.2"),
        connect_port=80,
        server_hostname=None,
        headers=None,
        deadline=asyncio.get_running_loop().time() + 2,
    )

    assert len(candidate_connections) == 2
    assert sum(candidate.handshake_count for candidate in candidate_connections) == 1
    assert sum(candidate.closed for candidate in candidate_connections) == 1
    await winner.close()
    assert all(candidate.closed for candidate in candidate_connections)

    candidates_started = asyncio.Event()
    cancellation_cleanup_started = asyncio.Event()
    selected_connection = CandidateConnection()

    def cancelled_candidate_connector(*args: Any, **kwargs: Any) -> CandidateConnector:
        async def create_connection() -> CandidateConnection:
            if kwargs["connect_host"] == "192.0.2.1":
                await candidates_started.wait()
                return selected_connection
            candidates_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_cleanup_started.set()
                await asyncio.Event().wait()
                raise

        return CandidateConnector(create_connection)

    monkeypatch.setattr(websocket_transport, "_pinned_connector", cancelled_candidate_connector)
    cancelled_connect = asyncio.create_task(
        open_first_pinned_websocket(
            "ws://cdp.example.test/devtools/browser/fake",
            connect_hosts=("192.0.2.1", "192.0.2.2"),
            connect_port=80,
            server_hostname=None,
            headers=None,
            deadline=asyncio.get_running_loop().time() + 2,
        )
    )
    await cancellation_cleanup_started.wait()
    cancelled_connect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_connect

    assert selected_connection.closed

    if proxy_supported:
        from skyvern.webeye.skycdp.facade.browser import _open_websocket

        proxy_requests: list[bytes] = []

        async def refusing_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            proxy_requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        async with websockets.serve(echo, "127.0.0.1", 0) as relay_server:
            relay_port = relay_server.sockets[0].getsockname()[1]
            refusing_proxy_server = await asyncio.start_server(refusing_proxy, "127.0.0.1", 0)
            async with refusing_proxy_server:
                monkeypatch.setenv("ws_proxy", f"http://127.0.0.1:{refusing_proxy_server.sockets[0].getsockname()[1]}")
                monkeypatch.setenv("no_proxy", "")
                relay_connection = await _open_websocket(
                    f"ws://127.0.0.1:{relay_port}/relay", headers=None, timeout_ms=1_000
                )
                await relay_connection.close()

        assert proxy_requests == []


async def test_a_host_header_with_its_own_port_is_respected() -> None:
    from skyvern.webeye.skycdp.facade.browser import plan_websocket_dial

    plan = plan_websocket_dial("ws://browser:9222/devtools/browser/u", headers={"Host": "localhost:1234"})
    assert plan.uri == "ws://localhost:1234/devtools/browser/u"
    assert (plan.connect_host, plan.connect_port) == ("browser", 9222)


async def test_without_a_host_header_nothing_is_rerouted() -> None:
    from skyvern.webeye.skycdp.facade.browser import plan_websocket_dial

    plan = plan_websocket_dial("ws://browser:9222/devtools/browser/u", headers={"x-api-key": "k"})
    assert plan.uri == "ws://browser:9222/devtools/browser/u"
    assert plan.connect_host is None and plan.connect_port is None
    assert plan.headers == {"x-api-key": "k"}


async def test_the_host_header_lookup_is_case_insensitive() -> None:
    from skyvern.webeye.skycdp.facade.browser import plan_websocket_dial

    plan = plan_websocket_dial("ws://browser:9222/d/u", headers={"host": "localhost"})
    assert plan.uri == "ws://localhost/d/u"
    assert plan.connect_host == "browser"
