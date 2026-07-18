"""Reusable contract suite for UpstreamBrowserPort adapters.

Any adapter (generic here, cloud-specific under tests/cloud/) subclasses
UpstreamBrowserPortContract and overrides make_port(); every behavioral
guarantee of the port is asserted once, here.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio
import websockets

from skyvern.proxy.adapters.local_chrome import (
    LocalChromeUpstreamBrowser,
    _terminate_process,
    find_local_chrome_executable,
)
from skyvern.proxy.adapters.memory import InMemoryUpstreamBrowser
from skyvern.proxy.adapters.websocket_upstream import WebSocketUpstreamBrowser
from skyvern.proxy.core.errors import UpstreamConnectError
from skyvern.proxy.core.session import ProxySession, UpstreamClosedError
from skyvern.proxy.ports import UpstreamBrowserPort, UpstreamConnection
from tests.unit.proxy._e2e_gate import require

# Nothing listens on port 1, so a dial there is refused immediately rather than left to
# hang until a timeout — a failure this contract can assert without waiting on one.
UNREACHABLE_WS_URL = "ws://127.0.0.1:1/devtools/browser/test"


class UpstreamBrowserPortContract:
    def make_port(self) -> UpstreamBrowserPort:
        raise NotImplementedError

    def make_session(self) -> ProxySession:
        return ProxySession(session_id="test-session", upstream_ws_url="ws://localhost:0/devtools/browser/test")

    @pytest.mark.asyncio
    async def test_connect_returns_open_connection(self) -> None:
        connection = await self.make_port().connect(self.make_session())
        assert isinstance(connection, UpstreamConnection)
        await connection.send('{"id": 1, "method": "Browser.getVersion"}')
        await connection.close()

    @pytest.mark.asyncio
    async def test_command_receives_frame_with_matching_id(self) -> None:
        connection = await self.make_port().connect(self.make_session())
        try:
            await connection.send('{"id": 42, "method": "Browser.getVersion"}')

            async def receive_until_response() -> None:
                while True:
                    if json.loads(await connection.receive()).get("id") == 42:
                        return

            await asyncio.wait_for(receive_until_response(), timeout=10)
        finally:
            await connection.close()

    @pytest.mark.asyncio
    async def test_send_after_close_raises_upstream_closed(self) -> None:
        connection = await self.make_port().connect(self.make_session())
        await connection.close()
        with pytest.raises(UpstreamClosedError):
            await connection.send('{"id": 1, "method": "Browser.getVersion"}')

    @pytest.mark.asyncio
    async def test_receive_after_close_raises_upstream_closed(self) -> None:
        connection = await self.make_port().connect(self.make_session())
        await connection.close()
        with pytest.raises(UpstreamClosedError):
            await connection.receive()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        connection = await self.make_port().connect(self.make_session())
        await connection.close()
        await connection.close()


class DialingUpstreamBrowserPortContract(UpstreamBrowserPortContract):
    """Extra clauses for an adapter that dials a real remote.

    The port's whole point is that callers never branch on transport exception types
    (skyvern.proxy.core.errors), so an adapter leaking a raw ConnectionRefusedError or a
    library's close exception breaks every caller's error handling while satisfying the
    happy-path clauses above. That guarantee only means something where there is a remote
    to fail: the in-memory loopback has none, so it does not inherit these rather than
    "passing" them vacuously — participation is declared by which contract an adapter
    subclasses, never by a skip.
    """

    def make_unreachable_port_and_session(self) -> tuple[UpstreamBrowserPort, ProxySession]:
        """A port + session whose connect() cannot succeed."""
        raise NotImplementedError

    async def break_remote(self, connection: UpstreamConnection) -> None:
        """Make the remote vanish WITHOUT closing our own end — the interruption a live
        session actually suffers, which is not the same as a local close()."""
        raise NotImplementedError

    @pytest.mark.asyncio
    async def test_failed_dial_raises_the_port_error_taxonomy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # One attempt: the adapter's default budget would spend ~15s of backoff proving
        # a retry behaviour this clause is not about, and a merge gate cannot afford it.
        monkeypatch.setenv("CDP_PROXY_CONNECT_RETRY_ATTEMPTS", "1")
        port, session = self.make_unreachable_port_and_session()
        with pytest.raises(UpstreamConnectError):
            await port.connect(session)

    @pytest.mark.asyncio
    async def test_remote_close_surfaces_as_upstream_closed_on_receive(self) -> None:
        connection = await self.make_port().connect(self.make_session())
        try:
            await self.break_remote(connection)
            with pytest.raises(UpstreamClosedError):
                # Drains whatever was already in flight; the close must arrive as the
                # taxonomy error rather than the transport's own exception.
                for _ in range(100):
                    await asyncio.wait_for(connection.receive(), timeout=10)
        finally:
            await connection.close()

    @pytest.mark.asyncio
    async def test_remote_close_surfaces_as_upstream_closed_on_send(self) -> None:
        connection = await self.make_port().connect(self.make_session())
        try:
            await self.break_remote(connection)
            with pytest.raises(UpstreamClosedError):
                for _ in range(100):
                    await asyncio.wait_for(connection.send('{"id": 1, "method": "Browser.getVersion"}'), timeout=10)
        finally:
            await connection.close()


class TestInMemoryUpstreamBrowser(UpstreamBrowserPortContract):
    def make_port(self) -> UpstreamBrowserPort:
        return InMemoryUpstreamBrowser()

    @pytest.mark.asyncio
    async def test_loopback_echoes_sent_frames(self) -> None:
        connection = await self.make_port().connect(self.make_session())
        await connection.send('{"id": 7, "method": "Target.getTargets"}')
        assert await connection.receive() == '{"id": 7, "method": "Target.getTargets"}'
        await connection.close()


class TestWebSocketUpstreamBrowser(DialingUpstreamBrowserPortContract):
    @pytest_asyncio.fixture(autouse=True)
    async def _echo_server(self):
        async def echo(ws: websockets.ServerConnection) -> None:
            async for message in ws:
                await ws.send(message)

        async with websockets.serve(echo, "127.0.0.1", 0) as server:
            self._server = server
            self._server_port = server.sockets[0].getsockname()[1]
            yield

    def make_port(self) -> UpstreamBrowserPort:
        return WebSocketUpstreamBrowser()

    def make_session(self) -> ProxySession:
        return ProxySession(
            session_id="test-session",
            upstream_ws_url=f"ws://127.0.0.1:{self._server_port}/devtools/browser/test",
        )

    def make_unreachable_port_and_session(self) -> tuple[UpstreamBrowserPort, ProxySession]:
        return WebSocketUpstreamBrowser(), ProxySession(session_id="test-session", upstream_ws_url=UNREACHABLE_WS_URL)

    async def break_remote(self, connection: UpstreamConnection) -> None:
        # Closing the server drops the peer's end of the socket without touching ours.
        self._server.close()
        await self._server.wait_closed()


class TestLocalChromeUpstreamBrowser(DialingUpstreamBrowserPortContract):
    @pytest.fixture(autouse=True)
    def _require_chrome(self) -> None:
        # Skips on a machine without Chrome, but FAILS in CI (CDP_PROXY_E2E_REQUIRED=1):
        # this is the only subclass that exercises the port against a real browser, so
        # a silent skip leaves the contract proven against fakes alone.
        require(find_local_chrome_executable() is not None, "no local Chrome/Chromium executable available")

    def make_port(self) -> UpstreamBrowserPort:
        return LocalChromeUpstreamBrowser()

    def make_unreachable_port_and_session(self) -> tuple[UpstreamBrowserPort, ProxySession]:
        return LocalChromeUpstreamBrowser(executable_path="/nonexistent/no-such-browser"), self.make_session()

    async def break_remote(self, connection: UpstreamConnection) -> None:
        # The launched browser IS the remote here, so killing it is the only way to make
        # the peer vanish without closing our own end. Reaching for the process is why
        # this uses the adapter's own reaper: it is idempotent on an already-dead
        # process, so the clause's close() still tears the profile dir down.
        await _terminate_process(connection._process)  # type: ignore[attr-defined]
