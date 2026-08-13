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

import json
from typing import Any

import pytest

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


async def test_a_host_header_is_turned_into_a_uri_rewrite_plus_connection_routing() -> None:
    """Chrome checks the Host header on the websocket upgrade too, not just on /json/version.

    The websockets client derives Host from the URI and ignores an override in additional_headers, so
    honouring a caller's Host means putting it in the URI and routing the TCP connection separately.
    """
    from skyvern.webeye.skycdp.facade.browser import plan_websocket_dial

    plan = plan_websocket_dial("ws://skycdp-browser:9222/devtools/browser/uuid", headers={"Host": "localhost"})
    # The caller's Host is sent verbatim. Silently appending the original port would change what the
    # server sees, and a Host header is an explicit instruction about exactly that.
    assert plan.uri == "ws://localhost/devtools/browser/uuid"
    assert plan.connect_host == "skycdp-browser"
    assert plan.connect_port == 9222
    # The Host must not also be sent as an ordinary header, or it goes on the wire twice.
    assert "Host" not in (plan.headers or {})


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
