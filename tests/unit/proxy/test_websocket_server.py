from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
from types import SimpleNamespace
from typing import Mapping

import pytest
from websockets import exceptions as websockets_exceptions
from websockets.asyncio import client as websockets_client
from websockets.datastructures import Headers

from skyvern.proxy.adapters.memory import (
    AllowAllAuth,
    ForwardAllEventPolicy,
    InMemorySessionRegistry,
    InMemoryUpstreamBrowser,
    NoOpMetrics,
)
from skyvern.proxy.adapters.websocket_server import CdpProxyServer, _parse_request
from skyvern.proxy.core.session import (
    Principal,
    ProxySession,
    ResolvedSession,
    SessionResolution,
    principal_owns_resolution,
)
from skyvern.proxy.ports import AuthPort, SessionRegistryPort, UpstreamConnection


class FakeClientWebSocket:
    def __init__(self, path: str, incoming: list[str] | None = None, headers: Headers | None = None) -> None:
        self.request = SimpleNamespace(headers=headers if headers is not None else Headers(), path=path)
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._incoming = list(incoming or [])

    def __aiter__(self) -> FakeClientWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason


class RecordingUpstreamBrowser(InMemoryUpstreamBrowser):
    def __init__(self) -> None:
        self.connections: list[UpstreamConnection] = []
        self.sessions: list[ProxySession] = []

    async def connect(self, session: ProxySession) -> UpstreamConnection:
        connection = await super().connect(session)
        self.connections.append(connection)
        self.sessions.append(session)
        return connection


class RecordingAuth:
    def __init__(self) -> None:
        self.credentials: Mapping[str, str] | None = None

    async def authenticate(self, credentials: Mapping[str, str]) -> Principal:
        self.credentials = credentials
        return Principal(principal_id="local-dev")


class CountingRegistry:
    def __init__(self, inner: InMemorySessionRegistry) -> None:
        self._inner = inner
        self.resolve_calls = 0

    async def resolve(self, session_id: str):  # type: ignore[no-untyped-def]
        self.resolve_calls += 1
        return await self._inner.resolve(session_id)

    async def invalidate(self, session_id: str) -> None:
        await self._inner.invalidate(session_id)


class StaticAuth:
    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    async def authenticate(self, credentials: Mapping[str, str]) -> Principal | None:
        return self._principal

    def authorize(self, principal: Principal, resolution: SessionResolution) -> bool:
        return principal_owns_resolution(principal, resolution)


def make_server(
    upstream: RecordingUpstreamBrowser, sessions: SessionRegistryPort, auth: AuthPort | None = None
) -> CdpProxyServer:
    return CdpProxyServer(
        upstream=upstream,
        sessions=sessions,
        auth=auth or AllowAllAuth(),
        metrics=NoOpMetrics(),
        event_policy=ForwardAllEventPolicy(),
    )


def make_resolved_session(session_id: str = "s1", organization_id: str | None = None) -> ResolvedSession:
    return ResolvedSession(
        session_id=session_id,
        upstream_adapter="memory",
        upstream_ws_url="ws://localhost:0/x",
        organization_id=organization_id,
        connect_headers={"x-routing": "r1"},
    )


@pytest.mark.asyncio
async def test_client_disconnect_closes_upstream_connection() -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    sessions.put(make_resolved_session())
    ws = FakeClientWebSocket(path="/s1", incoming=['{"id": 1, "method": "Page.enable"}'])

    await make_server(upstream, sessions)._handle_client(ws)  # type: ignore[arg-type]

    assert len(upstream.connections) == 1
    assert upstream.connections[0]._closed  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_resolved_routing_reaches_the_upstream_connect() -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    sessions.put(make_resolved_session())
    ws = FakeClientWebSocket(path="/s1")

    await make_server(upstream, sessions)._handle_client(ws)  # type: ignore[arg-type]

    assert upstream.sessions[0].upstream_ws_url == "ws://localhost:0/x"
    assert upstream.sessions[0].connect_headers == {"x-routing": "r1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "expected_close_code"),
    [
        ("unknown", 4404),
        ("pending", 4409),
        ("closed", 4410),
        ("expired", 4408),
    ],
)
async def test_negative_resolutions_are_rejected_before_any_upstream_dial(seed: str, expected_close_code: int) -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    if seed == "pending":
        sessions.mark_pending("s1")
    elif seed == "closed":
        sessions.mark_closed("s1")
    elif seed == "expired":
        sessions.mark_expired("s1")
    ws = FakeClientWebSocket(path="/s1")

    await make_server(upstream, sessions)._handle_client(ws)  # type: ignore[arg-type]

    assert ws.close_code == expected_close_code
    assert not upstream.connections


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_state", ["active", "pending", "closed", "expired"])
async def test_foreign_org_sessions_are_indistinguishable_from_unknown_ids(foreign_state: str) -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    if foreign_state == "active":
        sessions.put(make_resolved_session(organization_id="o_owner"))
    elif foreign_state == "pending":
        sessions.mark_pending("s1", organization_id="o_owner")
    elif foreign_state == "closed":
        sessions.mark_closed("s1", organization_id="o_owner")
    elif foreign_state == "expired":
        sessions.mark_expired("s1", organization_id="o_owner")
    auth = StaticAuth(Principal(principal_id="caller", organization_id="o_other"))
    probe_ws = FakeClientWebSocket(path="/s1")
    unknown_ws = FakeClientWebSocket(path="/s_missing")

    await make_server(upstream, sessions, auth=auth)._handle_client(probe_ws)  # type: ignore[arg-type]
    await make_server(upstream, sessions, auth=auth)._handle_client(unknown_ws)  # type: ignore[arg-type]

    assert (
        (probe_ws.close_code, probe_ws.close_reason)
        == (unknown_ws.close_code, unknown_ws.close_reason)
        == (
            4404,
            "unknown session",
        )
    )
    assert not upstream.connections


@pytest.mark.asyncio
async def test_org_owned_session_rejects_principal_without_organization() -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    sessions.put(make_resolved_session(organization_id="o_owner"))
    ws = FakeClientWebSocket(path="/s1")

    await make_server(upstream, sessions)._handle_client(ws)  # type: ignore[arg-type]

    assert ws.close_code == 4404
    assert not upstream.connections


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owned_state", "expected_close_code"),
    [
        ("pending", 4409),
        ("closed", 4410),
        ("expired", 4408),
    ],
)
async def test_owning_org_still_receives_lifecycle_close_codes(owned_state: str, expected_close_code: int) -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    if owned_state == "pending":
        sessions.mark_pending("s1", organization_id="o_owner")
    elif owned_state == "closed":
        sessions.mark_closed("s1", organization_id="o_owner")
    elif owned_state == "expired":
        sessions.mark_expired("s1", organization_id="o_owner")
    ws = FakeClientWebSocket(path="/s1")
    auth = StaticAuth(Principal(principal_id="caller", organization_id="o_owner"))

    await make_server(upstream, sessions, auth=auth)._handle_client(ws)  # type: ignore[arg-type]

    assert ws.close_code == expected_close_code
    assert not upstream.connections


@pytest.mark.asyncio
async def test_org_owned_session_allows_matching_organization() -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    sessions.put(make_resolved_session(organization_id="o_owner"))
    ws = FakeClientWebSocket(path="/s1")
    auth = StaticAuth(Principal(principal_id="caller", organization_id="o_owner"))

    await make_server(upstream, sessions, auth=auth)._handle_client(ws)  # type: ignore[arg-type]

    assert ws.close_code is None
    assert len(upstream.connections) == 1


@pytest.mark.asyncio
async def test_repeated_header_does_not_crash_auth() -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    sessions.put(make_resolved_session())
    headers = Headers()
    headers["Cookie"] = "a=1"
    headers["Cookie"] = "b=2"
    headers["X-Api-Key"] = "secret"
    ws = FakeClientWebSocket(path="/s1", headers=headers)
    auth = RecordingAuth()

    await make_server(upstream, sessions, auth=auth)._handle_client(ws)  # type: ignore[arg-type]

    # A repeated header must not raise an uncaught error; it is rejected as a
    # clean 4401 auth failure (dict(Headers) raises MultipleValuesError, caught).
    assert ws.close_code == 4401
    assert not upstream.connections


@pytest.mark.asyncio
async def test_query_string_in_path_resolves_session_id() -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    sessions.put(make_resolved_session())
    ws = FakeClientWebSocket(path="/s1?token=abc")

    await make_server(upstream, sessions)._handle_client(ws)  # type: ignore[arg-type]

    assert ws.close_code is None
    assert len(upstream.connections) == 1


@pytest.mark.asyncio
async def test_unowned_session_is_allowed() -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    sessions.put(make_resolved_session())
    ws = FakeClientWebSocket(path="/s1")

    await make_server(upstream, sessions)._handle_client(ws)  # type: ignore[arg-type]

    assert ws.close_code is None
    assert len(upstream.connections) == 1


def test_path_credential_without_trailing_session_id_is_not_reused_as_session_id() -> None:
    session_id, credentials = _parse_request({}, "/key/secret-api-key")
    assert credentials["x-api-key"] == "secret-api-key"
    assert session_id == ""


def test_path_credential_keeps_a_distinct_trailing_session_id() -> None:
    session_id, credentials = _parse_request({}, "/key/secret-api-key/s1")
    assert credentials["x-api-key"] == "secret-api-key"
    assert session_id == "s1"


@pytest.mark.asyncio
async def test_malformed_request_target_is_a_clean_close() -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    ws = FakeClientWebSocket(path="//[")  # urlsplit raises ValueError (invalid IPv6)

    await make_server(upstream, sessions)._handle_client(ws)  # type: ignore[arg-type]

    assert ws.close_code == 4401
    assert not upstream.connections


@pytest.mark.asyncio
async def test_duplicate_credential_header_is_a_clean_close() -> None:
    upstream = RecordingUpstreamBrowser()
    sessions = InMemorySessionRegistry()
    headers = Headers()
    headers["x-api-key"] = "k1"
    headers["x-api-key"] = "k2"
    ws = FakeClientWebSocket(path="/s1")
    ws.request.headers = headers

    await make_server(upstream, sessions)._handle_client(ws)  # type: ignore[arg-type]

    assert ws.close_code == 4401
    assert not upstream.connections


@pytest.mark.asyncio
async def test_credential_only_path_resolves_empty_session_and_rejects_4404() -> None:
    upstream = RecordingUpstreamBrowser()
    inner = InMemorySessionRegistry()
    counting = CountingRegistry(inner)
    ws = FakeClientWebSocket(path="/key/secret-api-key")

    await make_server(upstream, counting)._handle_client(ws)  # type: ignore[arg-type]

    assert counting.resolve_calls == 1
    assert ws.close_code == 4404
    assert not upstream.connections


@pytest.mark.asyncio
async def test_session_is_resolved_once_per_connection_not_per_message() -> None:
    upstream = RecordingUpstreamBrowser()
    inner = InMemorySessionRegistry()
    inner.put(make_resolved_session())
    counting = CountingRegistry(inner)
    frames = [f'{{"id": {frame_id}, "method": "Page.enable"}}' for frame_id in range(1, 6)]
    ws = FakeClientWebSocket(path="/s1", incoming=frames)

    await make_server(upstream, counting)._handle_client(ws)  # type: ignore[arg-type]

    assert counting.resolve_calls == 1


# ---- serve_forever lifecycle: health endpoint + signal drain ----------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_listener(port: int, *, up: bool, attempts: int = 500) -> None:
    for _ in range(attempts):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if not up:
                return
        else:
            writer.close()
            if up:
                return
        await asyncio.sleep(0.01)
    raise AssertionError(f"listener on port {port} never became {'reachable' if up else 'refused'}")


async def _http_get(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read(-1)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


def _lifecycle_server(port: int) -> CdpProxyServer:
    sessions = InMemorySessionRegistry()
    sessions.put(make_resolved_session())
    return CdpProxyServer(
        upstream=RecordingUpstreamBrowser(),
        sessions=sessions,
        auth=AllowAllAuth(),
        metrics=NoOpMetrics(),
        event_policy=ForwardAllEventPolicy(),
        host="127.0.0.1",
        port=port,
    )


@pytest.mark.asyncio
async def test_serve_forever_healthz_and_sigterm_drain() -> None:
    port = _free_port()
    task = asyncio.create_task(_lifecycle_server(port).serve_forever())
    try:
        await _wait_for_listener(port, up=True)
        status, body = await _http_get(port, "/healthz")
        assert (status, body) == (200, b"ok")

        async with websockets_client.connect(f"ws://127.0.0.1:{port}/s1") as client:
            await client.ping()
            os.kill(os.getpid(), signal.SIGTERM)
            # Drain: the listener refuses new work while the live relay survives.
            await _wait_for_listener(port, up=False)
            await client.ping()
        await asyncio.wait_for(task, timeout=10)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_serve_forever_force_closes_stragglers_after_drain_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CDP_PROXY_DRAIN_TIMEOUT_SECONDS", "1")
    port = _free_port()
    task = asyncio.create_task(_lifecycle_server(port).serve_forever())
    client = None
    try:
        await _wait_for_listener(port, up=True)
        client = await websockets_client.connect(f"ws://127.0.0.1:{port}/s1")
        os.kill(os.getpid(), signal.SIGTERM)

        # The straggler never closes; the drain budget expires and the server
        # exits anyway, closing the client with a clean 1001 instead of a RST.
        await asyncio.wait_for(task, timeout=10)
        with pytest.raises(websockets_exceptions.ConnectionClosed):
            await asyncio.wait_for(client.recv(), timeout=5)
        assert client.protocol.close_code == 1001
    finally:
        if client is not None:
            await client.close()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_second_signal_during_drain_force_closes_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Drain budget is huge on purpose: only the second signal can end the wait,
    # so a fast completion proves the fast-exit path rather than the timeout.
    monkeypatch.setenv("CDP_PROXY_DRAIN_TIMEOUT_SECONDS", "3600")
    port = _free_port()
    task = asyncio.create_task(_lifecycle_server(port).serve_forever())
    client = None
    try:
        await _wait_for_listener(port, up=True)
        client = await websockets_client.connect(f"ws://127.0.0.1:{port}/s1")
        os.kill(os.getpid(), signal.SIGTERM)
        await _wait_for_listener(port, up=False)
        os.kill(os.getpid(), signal.SIGTERM)

        await asyncio.wait_for(task, timeout=10)
        with pytest.raises(websockets_exceptions.ConnectionClosed):
            await asyncio.wait_for(client.recv(), timeout=5)
        assert client.protocol.close_code == 1001
    finally:
        if client is not None:
            await client.close()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
