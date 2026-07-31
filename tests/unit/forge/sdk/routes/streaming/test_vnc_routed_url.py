"""Which address live view is allowed to stream from (SKY-13287).

A live view carries a customer's screen, keyboard and clipboard. The only addresses permitted
here are ones an edge checks a credential on before a byte reaches the browser, plus loopback,
where there is no network hop and no edge to check. Naming the browser's own address instead
would stream that view with no credential on the wire, so it is refused — visibly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.exceptions import MissingRoutedVncAddressError
from skyvern.forge.sdk.routes.streaming.channels.vnc import (
    _build_vnc_url_from_browser_address,
    loop_stream_vnc,
)

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

    monkeypatch.setattr("skyvern.forge.sdk.routes.streaming.channels.vnc.websockets.connect", _record_connect)

    with pytest.raises(MissingRoutedVncAddressError):
        await loop_stream_vnc(_channel(POD_ADDRESS))  # type: ignore[arg-type]

    assert dialed == []
