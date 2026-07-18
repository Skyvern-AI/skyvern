"""HTTP discovery endpoint tests for the client-facing CDP proxy.

CDP clients (Playwright, puppeteer) GET /json/version — /json/list, /json — to
learn the browser websocket url before the WS upgrade. The proxy must serve these
authenticated and return PROXY-SCOPED debugger urls, never the upstream url.
"""

from __future__ import annotations

import json

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from skyvern.proxy.adapters.memory import (
    ForwardAllEventPolicy,
    InMemorySessionRegistry,
    InMemoryUpstreamBrowser,
    NoOpMetrics,
    StaticKeyAuth,
)
from skyvern.proxy.adapters.websocket_server import (
    CdpProxyServer,
    _discovery_payload,
    _proxy_scoped_ws_url,
    _split_discovery_target,
)
from skyvern.proxy.core.session import Principal, ResolvedSession

VALID_KEY = "disco-key-abc123"
SECRET = "upstream-secret-token-do-not-leak"


class _FakeConnection:
    """Stands in for websockets.ServerConnection.respond during process_request."""

    def respond(self, status: object, text: str) -> Response:
        body = text.encode()
        headers = Headers([("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))])
        return Response(int(status), "Unauthorized", headers, body)


class _RecordingMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def increment(self, name: str, amount: int = 1, tags: object = None) -> None:
        self.calls.append(("increment", name, dict(tags or {})))

    def observe(self, name: str, value: float, tags: object = None) -> None:
        self.calls.append(("observe", name, dict(tags or {})))

    def gauge(self, name: str, amount: int, tags: object = None) -> None:
        self.calls.append(("gauge", name, dict(tags or {})))


class _CountingRegistry(InMemorySessionRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.resolve_calls = 0

    async def resolve(self, session_id: str):  # type: ignore[no-untyped-def]
        self.resolve_calls += 1
        return await super().resolve(session_id)


def _server(auth: object, metrics: object | None = None, sessions: object | None = None) -> CdpProxyServer:
    return CdpProxyServer(
        upstream=InMemoryUpstreamBrowser(),
        sessions=sessions or InMemorySessionRegistry(),
        auth=auth,  # type: ignore[arg-type]
        metrics=metrics or NoOpMetrics(),
        event_policy=ForwardAllEventPolicy(),
    )


def _request(path: str, headers: dict[str, str] | None = None) -> Request:
    hdrs = Headers()
    hdrs["Host"] = "proxy.example:9223"
    for name, value in (headers or {}).items():
        hdrs[name] = value
    return Request(path=path, headers=hdrs)


async def _discover(server: CdpProxyServer, path: str, headers: dict[str, str] | None = None) -> Response | None:
    return await server._process_request(_FakeConnection(), _request(path, headers))  # type: ignore[arg-type]


# ---- pure splitter ----------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/s1/json/version", ("/s1", "/json/version")),
        ("/s1/json/version/", ("/s1", "/json/version")),  # Playwright appends a trailing slash
        ("/s1/json/list", ("/s1", "/json/list")),
        ("/s1/json", ("/s1", "/json")),
        ("/json/version", ("/", "/json/version")),  # session-less
        ("/key/mykey/s1/json/version", ("/key/mykey/s1", "/json/version")),
        ("/s1/json/version?token=x", ("/s1?token=x", "/json/version")),
        ("/s1", None),  # a plain WS target, not discovery
        ("/s1/json/other", None),
        ("/", None),
    ],
)
def test_split_discovery_target(target: str, expected: tuple[str, str] | None) -> None:
    assert _split_discovery_target(target) == expected


def test_proxy_scoped_ws_url_never_points_at_upstream() -> None:
    url = _proxy_scoped_ws_url("proxy.example:9223", None, "/s1")
    assert url == "ws://proxy.example:9223/s1"


@pytest.mark.parametrize(
    ("forwarded_proto", "scheme"),
    [
        ("https", "wss"),
        ("HTTPS", "wss"),  # case-insensitive
        (" https ", "wss"),  # whitespace-padded
        ("https, http", "wss"),  # proxy comma-chain: first hop is the client scheme
        ("http", "ws"),
        (None, "ws"),
        ("", "ws"),
    ],
)
def test_proxy_scoped_ws_url_parses_forwarded_proto(forwarded_proto: str | None, scheme: str) -> None:
    assert _proxy_scoped_ws_url("proxy.example", forwarded_proto, "/s1").startswith(f"{scheme}://")


def test_websocket_server_import_suppresses_websockets_debug_logging() -> None:
    # The driving adapter must self-suppress the credential-bearing websockets DEBUG
    # logs at import, not lean on a sibling module's import side effect.
    import importlib
    import logging

    from skyvern.proxy.adapters import websocket_server

    logging.getLogger("websockets").setLevel(logging.DEBUG)
    importlib.reload(websocket_server)
    assert logging.getLogger("websockets").level == logging.INFO


def test_discovery_payload_version_carries_the_ws_debugger_url() -> None:
    payload = _discovery_payload("/json/version", "ws://p/s1", "s1")
    assert payload["webSocketDebuggerUrl"] == "ws://p/s1"


def test_discovery_payload_list_is_an_array_of_targets() -> None:
    payload = _discovery_payload("/json/list", "ws://p/s1", "s1")
    assert isinstance(payload, list) and payload[0]["webSocketDebuggerUrl"] == "ws://p/s1"


# ---- process_request --------------------------------------------------------


def _body(response: Response) -> object:
    return json.loads(bytes(response.body).decode())


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["/json/version", "/json/version/", "/json/list", "/json"])
async def test_authenticated_discovery_returns_proxy_scoped_url(suffix: str) -> None:
    server = _server(StaticKeyAuth({VALID_KEY: Principal(principal_id="p")}))
    response = await _discover(server, f"/s1{suffix}", headers={"x-api-key": VALID_KEY})
    assert response is not None and response.status_code == 200
    body = _body(response)
    url = body["webSocketDebuggerUrl"] if isinstance(body, dict) else body[0]["webSocketDebuggerUrl"]
    # Points back at the proxy (Host header), scoped to the requested session id.
    assert url == "ws://proxy.example:9223/s1"


@pytest.mark.asyncio
async def test_non_discovery_target_is_passed_through_to_the_ws_handshake() -> None:
    server = _server(StaticKeyAuth({VALID_KEY: Principal(principal_id="p")}))
    assert await _discover(server, "/s1", headers={"x-api-key": VALID_KEY}) is None


@pytest.mark.asyncio
async def test_unauthenticated_discovery_is_rejected_401() -> None:
    metrics = _RecordingMetrics()
    server = _server(StaticKeyAuth({VALID_KEY: Principal(principal_id="p")}), metrics=metrics)
    response = await _discover(server, "/s1/json/version")  # no key
    assert response is not None and response.status_code == 401
    reasons = {tags.get("reason") for _op, name, tags in metrics.calls if name.endswith("connection_rejected")}
    assert "unauthorized" in reasons


@pytest.mark.asyncio
async def test_malformed_discovery_target_is_rejected_401() -> None:
    server = _server(StaticKeyAuth({VALID_KEY: Principal(principal_id="p")}))
    response = await _discover(server, "//[/json/version", headers={"x-api-key": VALID_KEY})
    assert response is not None and response.status_code == 401


@pytest.mark.asyncio
async def test_discovery_preserves_url_credentials_for_headerless_clients() -> None:
    # A puppeteer-style client carries its key in the path (it cannot set headers);
    # the returned ws url must keep it so the follow-up WS connect still authenticates.
    server = _server(StaticKeyAuth({VALID_KEY: Principal(principal_id="p")}))
    response = await _discover(server, f"/key/{VALID_KEY}/s1/json/version")
    assert response is not None and response.status_code == 200
    assert _body(response)["webSocketDebuggerUrl"] == f"ws://proxy.example:9223/key/{VALID_KEY}/s1"


@pytest.mark.asyncio
async def test_discovery_response_is_never_cached() -> None:
    # The webSocketDebuggerUrl can carry a header-less client's own API key, so a
    # shared intermediary must never retain the body.
    server = _server(StaticKeyAuth({VALID_KEY: Principal(principal_id="p")}))
    response = await _discover(server, "/s1/json/version", headers={"x-api-key": VALID_KEY})
    assert response is not None
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_discovery_never_resolves_or_leaks_the_upstream() -> None:
    # Discovery must not dial or even resolve the session — so it cannot leak the
    # upstream url and cannot become a cross-org existence oracle.
    sessions = _CountingRegistry()
    sessions.put(
        ResolvedSession(
            session_id="s1",
            upstream_adapter="memory",
            upstream_ws_url=f"ws://vendor.internal:9222/live?token={SECRET}",
            organization_id="o_owner",
        )
    )
    server = _server(StaticKeyAuth({VALID_KEY: Principal(principal_id="p")}), sessions=sessions)
    response = await _discover(server, "/s1/json/version", headers={"x-api-key": VALID_KEY})
    assert response is not None
    assert sessions.resolve_calls == 0
    text = bytes(response.body).decode()
    assert SECRET not in text and "vendor.internal" not in text
