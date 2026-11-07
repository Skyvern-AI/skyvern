"""Which address live view is allowed to stream from (SKY-13287).

A live view carries a customer's screen, keyboard and clipboard. The only addresses permitted
here are ones an edge checks a credential on before a byte reaches the browser, plus loopback,
where there is no network hop and no edge to check. Naming the browser's own address instead
would stream that view with no credential on the wire, so it is refused — visibly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Literal

import pytest
from starlette.websockets import WebSocketState
from websockets import ConnectionClosedOK

from skyvern.exceptions import MissingRoutedVncAddressError
from skyvern.forge.sdk.routes.streaming.channels import vnc
from skyvern.forge.sdk.routes.streaming.channels.vnc import (
    VncChannel,
    _build_vnc_url_from_browser_address,
    loop_stream_vnc,
)
from skyvern.forge.sdk.streaming.registries import del_vnc_channel

VNC_PORT = 6080
SESSION_ID = "pbs_live"
TOKEN = f"{SESSION_ID}.minted-secret"
ROUTER_ADDRESS = f"wss://session-router.skyvern.com/{SESSION_ID}?token={TOKEN}"
LEGACY_ADDRESS = f"wss://sessions.skyvern.com/{SESSION_ID}/payload.signature/devtools/browser/b-1"
POD_ADDRESS = "ws://10.0.0.7:9223/devtools/browser/b-1"


def test_a_router_address_yields_a_routed_token_bearing_live_view_url() -> None:
    url = _build_vnc_url_from_browser_address(ROUTER_ADDRESS, VNC_PORT)

    assert url == f"wss://session-router.skyvern.com/vnc/{SESSION_ID}?token={TOKEN}"


def test_a_legacy_address_yields_exactly_what_it_does_today() -> None:
    url = _build_vnc_url_from_browser_address(LEGACY_ADDRESS, VNC_PORT)

    assert url == f"wss://sessions.skyvern.com/vnc/{SESSION_ID}/payload.signature"


def test_a_loopback_address_streams_from_the_local_websockify_listener() -> None:
    """Single-host case (local dev, single-container self-hosting): no network hop leaves the
    machine the API is on, so there is no edge to route through and nothing to authenticate to."""
    url = _build_vnc_url_from_browser_address("ws://127.0.0.1:9224/devtools/browser/b-1#pbs_live", VNC_PORT)

    assert url == f"ws://127.0.0.1:{VNC_PORT}"


@pytest.mark.parametrize(
    "browser_address",
    [
        pytest.param(POD_ADDRESS, id="pod_address"),
        pytest.param("ws://10.0.0.7:6080", id="pod_websockify_address"),
        pytest.param(f"wss://session-router.skyvern.com/{SESSION_ID}", id="router_shape_without_a_token"),
        pytest.param(f"wss://session-router.skyvern.com/{SESSION_ID}?other=x", id="router_shape_wrong_query"),
        pytest.param(f"https://session-router.skyvern.com/{SESSION_ID}?token={TOKEN}", id="not_a_websocket_scheme"),
        pytest.param("", id="empty"),
        pytest.param(None, id="missing"),
    ],
)
def test_no_other_address_yields_a_live_view_url(browser_address: str | None) -> None:
    assert _build_vnc_url_from_browser_address(browser_address, VNC_PORT) is None


def _channel(browser_address: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        browser_session=SimpleNamespace(
            browser_address=browser_address,
            persistent_browser_session_id=SESSION_ID,
            ip_address="10.0.0.7",
        ),
        class_name="VncChannel",
        vnc_port=VNC_PORT,
        x_api_key="sk-test",
        identity={},
    )


@pytest.mark.asyncio
async def test_a_session_with_no_routable_address_fails_instead_of_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session carries a reachable ip_address; before SKY-13287 that was streamed directly,
    with no credential anywhere on the connection."""
    dialed: list[str] = []

    def _record_connect(url: str, **kwargs: object) -> object:
        dialed.append(url)
        raise AssertionError("live view must not dial when no routed address exists")

    monkeypatch.setattr(vnc.websockets, "connect", _record_connect)

    with pytest.raises(MissingRoutedVncAddressError):
        await loop_stream_vnc(_channel(POD_ADDRESS))  # type: ignore[arg-type]

    assert dialed == []


class _FakeViewerSocket:
    """Mirrors Starlette: a server-side close changes application_state only, and a later send raises."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.application_state = WebSocketState.CONNECTED
        self.inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent: list[bytes] = []
        self.sends_after_close = 0

    async def receive_bytes(self) -> bytes:
        return await self.inbound.get()

    async def send_bytes(self, data: bytes) -> None:
        if self.application_state != WebSocketState.CONNECTED:
            self.sends_after_close += 1
            raise RuntimeError('Cannot call "send" once a close message has been sent.')
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.application_state = WebSocketState.DISCONNECTED


class _FakeNoVncSocket:
    def __init__(self) -> None:
        self.inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.received: list[bytes] = []
        self.sends_after_close = 0
        self.closed = False

    async def __aenter__(self) -> _FakeNoVncSocket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def recv(self) -> bytes:
        data = await self.inbound.get()
        if data is None:
            raise ConnectionClosedOK(None, None)
        return data

    async def send(self, data: bytes) -> None:
        if self.closed:
            self.sends_after_close += 1
            raise ConnectionClosedOK(None, None)
        self.received.append(data)

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.inbound.put_nowait(None)
        # A real close handshake yields, letting a recv() waiting on this socket wake before any cancel lands.
        await asyncio.sleep(0)


def _live_channel(client_id: str, websocket: _FakeViewerSocket) -> VncChannel:
    channel = VncChannel(
        client_id=client_id,
        organization_id="o_test",
        vnc_port=VNC_PORT,
        x_api_key="sk-test",
        websocket=websocket,  # type: ignore[arg-type]
        initial_interactor="user",
    )
    channel.browser_session = SimpleNamespace(  # type: ignore[assignment]
        browser_address="ws://127.0.0.1:9222/devtools/browser/b-1",
        persistent_browser_session_id=SESSION_ID,
    )
    return channel


@pytest.mark.asyncio
@pytest.mark.parametrize("wake", ["viewer", "novnc"])
async def test_a_displaced_relay_closes_its_viewer_socket_without_replaying_input(
    monkeypatch: pytest.MonkeyPatch, wake: Literal["viewer", "novnc"]
) -> None:
    """A second viewer reusing the client id displaces the first; once it leaves, whichever side of the first
    relay wakes next must end the relay and close the first viewer's socket so that viewer redials."""
    client_id = f"displaced-{wake}"
    novnc = _FakeNoVncSocket()
    monkeypatch.setattr(vnc.websockets, "connect", lambda url, **kwargs: novnc)
    viewer = _FakeViewerSocket()
    channel = _live_channel(client_id, viewer)
    relay = asyncio.create_task(loop_stream_vnc(channel))
    try:
        input_before_displacement = b"\x03\x01\x00\x00\x00\x00\x00\x10\x00\x10"
        viewer.inbound.put_nowait(input_before_displacement)
        novnc.inbound.put_nowait(b"frame-before")
        for _ in range(100):
            if novnc.received and viewer.sent:
                break
            await asyncio.sleep(0)
        assert novnc.received == [input_before_displacement]
        assert viewer.sent == [b"frame-before"]

        await _live_channel(client_id, _FakeViewerSocket()).close()
        assert not relay.done()

        if wake == "viewer":
            viewer.inbound.put_nowait(b"\x03\x00")
        else:
            novnc.inbound.put_nowait(b"frame")
        await asyncio.wait_for(relay, timeout=2)

        assert viewer.application_state == WebSocketState.DISCONNECTED
        assert viewer.sends_after_close == 0
        assert novnc.sends_after_close == 0
        expected = [input_before_displacement] + ([b"\x03\x00"] if wake == "viewer" else [])
        assert novnc.received == expected
    finally:
        relay.cancel()
        del_vnc_channel(client_id)
