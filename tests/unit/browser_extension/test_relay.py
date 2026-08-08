from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType

import skyvern.browser_extension.relay as relay_module
from skyvern.browser_extension.auth import compute_ext_proof, compute_server_proof
from skyvern.browser_extension.errors import BrowserExtensionNotConnectedError, ExtensionRequestError
from skyvern.browser_extension.relay import ExtensionRelayServer

TOKEN = "test-pairing-token"


class RelayHarness:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.event_received = asyncio.Event()
        self.disconnect_called = asyncio.Event()
        self.server = ExtensionRelayServer(TOKEN, 0, self.on_event, self.on_disconnect)

    async def on_event(self, event: str, params: dict) -> None:
        self.events.append((event, params))
        self.event_received.set()

    async def on_disconnect(self) -> None:
        self.disconnect_called.set()


@pytest_asyncio.fixture
async def relay_harness() -> AsyncGenerator[RelayHarness]:
    harness = RelayHarness()
    await harness.server.start()
    yield harness
    await harness.server.stop()


def relay_url(harness: RelayHarness) -> str:
    return f"ws://127.0.0.1:{harness.server.bound_port}/extension/v1"


def http_url(harness: RelayHarness, path: str) -> str:
    return f"http://127.0.0.1:{harness.server.bound_port}{path}"


def pair_begin_proof(token: str) -> str:
    return hmac.new(token.encode(), b"skyvern-pair-begin-v1", hashlib.sha256).hexdigest()


def interactive_pairing_headers(harness: RelayHarness) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": http_url(harness, ""),
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


async def authenticate(
    session: ClientSession,
    harness: RelayHarness,
    *,
    origin: str | None = "chrome-extension://abcdefghijklmnop",
    send_hello: bool = True,
) -> ClientWebSocketResponse:
    headers = {"Origin": origin} if origin is not None else None
    websocket = await session.ws_connect(relay_url(harness), headers=headers)
    challenge = await websocket.receive_json()
    client_nonce = secrets.token_urlsafe(32)
    await websocket.send_json(
        {
            "v": 1,
            "type": "auth.proof",
            "clientNonce": client_nonce,
            "proof": compute_ext_proof(TOKEN, challenge["serverNonce"], client_nonce),
        }
    )
    auth_ok = await websocket.receive_json()
    assert auth_ok == {
        "v": 1,
        "type": "auth.ok",
        "serverProof": compute_server_proof(TOKEN, client_nonce, challenge["serverNonce"]),
    }
    if send_hello:
        event_count = len(harness.events)
        await websocket.send_json(
            {
                "v": 1,
                "type": "event",
                "event": "extension.hello",
                "params": {"extensionVersion": "1.0.0", "scopedTabs": []},
            }
        )

        async def hello_processed() -> None:
            while len(harness.events) == event_count or not harness.server.connected:
                await asyncio.sleep(0)

        await asyncio.wait_for(hello_processed(), 1)
    return websocket


@pytest.mark.asyncio
async def test_pair_begin_claim_happy_path_and_pair_page_never_contains_token(
    relay_harness: RelayHarness,
) -> None:
    async with ClientSession() as session:
        begin = await session.post(
            http_url(relay_harness, "/pair/begin"),
            json={"v": 1, "proof": pair_begin_proof(TOKEN)},
        )
        assert begin.status == 200
        assert begin.headers["Cache-Control"] == "no-store"
        begin_payload = await begin.json()
        assert begin_payload["v"] == 1
        assert isinstance(begin_payload["nonce"], str)
        assert begin_payload["nonce"]

        page = await session.get(http_url(relay_harness, "/pair"))
        page_body = await page.text()
        assert page.status == 200
        assert TOKEN not in page_body
        assert "dhommdmblflboaledbbfkdaapkadphlp" in page_body
        assert "Pairing request required" in page_body
        assert "skyvern browser extension-pair" in page_body
        assert "Start a new pairing link" not in page_body
        assert "copy-command" not in page_body
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]

        claim = await session.post(
            http_url(relay_harness, "/pair/claim"),
            json={"v": 1, "nonce": begin_payload["nonce"]},
            headers=interactive_pairing_headers(relay_harness),
        )
        assert claim.status == 200
        assert claim.headers["Cache-Control"] == "no-store"
        assert await claim.json() == {"v": 1, "port": relay_harness.server.bound_port, "token": TOKEN}


@pytest.mark.asyncio
async def test_bare_pair_page_requires_authenticated_pairing_request(
    relay_harness: RelayHarness,
) -> None:
    async with ClientSession() as session:
        page = await session.get(http_url(relay_harness, "/pair"))
        page_body = await page.text()

        assert page.status == 200
        assert relay_harness.server._pairing_nonce is None
        assert "showMissingRequest();" in page_body
        assert "approvalControls.hidden = true" in page_body

        begin = await session.post(
            http_url(relay_harness, "/pair/begin"),
            json={"v": 1},
            headers=interactive_pairing_headers(relay_harness),
        )
        assert begin.status == 403
        assert relay_harness.server._pairing_nonce is None


def test_interactive_pairing_source_accepts_canonical_default_http_port() -> None:
    server = ExtensionRelayServer(TOKEN, 80, AsyncMock())
    request = SimpleNamespace(
        content_type="application/json",
        headers={
            "Origin": "http://127.0.0.1",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
    )

    assert server._is_interactive_pairing_request(request)


@pytest.mark.asyncio
async def test_pair_claim_rejects_cross_site_request_without_consuming_nonce(
    relay_harness: RelayHarness,
) -> None:
    nonce = relay_harness.server.create_pairing_nonce()
    async with ClientSession() as session:
        rejected = await session.post(
            http_url(relay_harness, "/pair/claim"),
            json={"v": 1, "nonce": nonce},
            headers={"Origin": "https://evil.example", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "cross-site"},
        )
        accepted = await session.post(
            http_url(relay_harness, "/pair/claim"),
            json={"v": 1, "nonce": nonce},
            headers=interactive_pairing_headers(relay_harness),
        )

    assert rejected.status == 403
    assert accepted.status == 200


def test_get_or_create_pairing_nonce_reuses_live_nonce_and_rotates_expired_nonce(
    relay_harness: RelayHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 10_000.0
    monkeypatch.setattr(relay_module.time, "monotonic", lambda: now)

    first = relay_harness.server.get_or_create_pairing_nonce()
    assert relay_harness.server.get_or_create_pairing_nonce() == first

    now += 121.0
    assert relay_harness.server.get_or_create_pairing_nonce() != first


@pytest.mark.asyncio
async def test_pair_claim_wrong_nonce_is_forbidden_and_consumes_active_nonce(
    relay_harness: RelayHarness,
) -> None:
    nonce = relay_harness.server.create_pairing_nonce()
    headers = interactive_pairing_headers(relay_harness)
    async with ClientSession() as session:
        wrong = await session.post(
            http_url(relay_harness, "/pair/claim"),
            json={"v": 1, "nonce": secrets.token_urlsafe(32)},
            headers=headers,
        )
        assert wrong.status == 403
        assert await wrong.json() == {"error": "invalid_nonce"}

        consumed = await session.post(
            http_url(relay_harness, "/pair/claim"),
            json={"v": 1, "nonce": nonce},
            headers=headers,
        )
        assert consumed.status == 403


@pytest.mark.asyncio
async def test_pair_claim_nonce_is_single_use(relay_harness: RelayHarness) -> None:
    nonce = relay_harness.server.create_pairing_nonce()
    headers = interactive_pairing_headers(relay_harness)
    async with ClientSession() as session:
        first = await session.post(
            http_url(relay_harness, "/pair/claim"),
            json={"v": 1, "nonce": nonce},
            headers=headers,
        )
        second = await session.post(
            http_url(relay_harness, "/pair/claim"),
            json={"v": 1, "nonce": nonce},
            headers=headers,
        )

        assert first.status == 200
        assert second.status == 403
        assert await second.json() == {"error": "invalid_nonce"}


@pytest.mark.asyncio
async def test_pair_claim_expired_nonce_is_forbidden(
    relay_harness: RelayHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 10_000.0
    monkeypatch.setattr(relay_module.time, "monotonic", lambda: now)
    nonce = relay_harness.server.create_pairing_nonce()
    now += 121.0

    async with ClientSession() as session:
        response = await session.post(
            http_url(relay_harness, "/pair/claim"),
            json={"v": 1, "nonce": nonce},
            headers=interactive_pairing_headers(relay_harness),
        )

        assert response.status == 403
        assert await response.json() == {"error": "invalid_nonce"}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"v": "1", "nonce": "present"}, {"v": 1}])
async def test_pair_claim_bad_payload_is_forbidden_and_consumes_active_nonce(
    relay_harness: RelayHarness,
    payload: object,
) -> None:
    nonce = relay_harness.server.create_pairing_nonce()
    headers = interactive_pairing_headers(relay_harness)
    async with ClientSession() as session:
        response = await session.post(http_url(relay_harness, "/pair/claim"), json=payload, headers=headers)
        consumed = await session.post(
            http_url(relay_harness, "/pair/claim"),
            json={"v": 1, "nonce": nonce},
            headers=headers,
        )

        assert response.status == 403
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.json() == {"error": "invalid_nonce"}
        assert consumed.status == 403
        assert await consumed.json() == {"error": "invalid_nonce"}


@pytest.mark.asyncio
async def test_pair_begin_bad_proof_is_forbidden(relay_harness: RelayHarness) -> None:
    async with ClientSession() as session:
        response = await session.post(
            http_url(relay_harness, "/pair/begin"),
            json={"v": 1, "proof": "not-the-proof"},
        )

        assert response.status == 403


@pytest.mark.asyncio
async def test_authentication_and_hello_update_scoped_tabs(relay_harness: RelayHarness) -> None:
    async with ClientSession() as session:
        websocket = await authenticate(session, relay_harness, send_hello=False)

        assert not relay_harness.server.connected
        assert not await relay_harness.server.wait_connected(0.01)

        params = {
            "extensionVersion": "1.0.0",
            "scopedTabs": [{"tabId": 17, "url": "https://example.com", "title": "Example"}],
        }
        await websocket.send_json({"v": 1, "type": "event", "event": "extension.hello", "params": params})
        await asyncio.wait_for(relay_harness.event_received.wait(), 1)

        assert relay_harness.server.connected
        assert await relay_harness.server.wait_connected(0.1)
        assert relay_harness.server.scoped_tabs == [{"tabId": 17, "url": "https://example.com", "title": "Example"}]
        assert relay_harness.events == [("extension.hello", params)]


@pytest.mark.asyncio
async def test_wrong_proof_is_closed_with_4403(relay_harness: RelayHarness) -> None:
    async with ClientSession() as session:
        websocket = await session.ws_connect(relay_url(relay_harness))
        challenge = await websocket.receive_json()
        assert challenge["type"] == "auth.challenge"
        await websocket.send_json(
            {"v": 1, "type": "auth.proof", "clientNonce": secrets.token_urlsafe(32), "proof": "wrong-proof"}
        )

        message = await websocket.receive()

        assert message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
        assert websocket.close_code == 4403
        assert not relay_harness.server.connected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_nonce",
    [
        "short",
        secrets.token_urlsafe(31),
        secrets.token_urlsafe(32) + "=",
        secrets.token_urlsafe(33),
        "!" * 43,
        base64.urlsafe_b64encode(b"\0" * 32).rstrip(b"=").decode("ascii")[:-1] + "B",
    ],
)
async def test_client_nonce_must_be_unpadded_base64url_for_exactly_32_bytes(
    relay_harness: RelayHarness,
    client_nonce: str,
) -> None:
    async with ClientSession() as session:
        websocket = await session.ws_connect(relay_url(relay_harness))
        challenge = await websocket.receive_json()
        await websocket.send_json(
            {
                "v": 1,
                "type": "auth.proof",
                "clientNonce": client_nonce,
                "proof": compute_ext_proof(TOKEN, challenge["serverNonce"], client_nonce),
            }
        )

        message = await websocket.receive()

        assert message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
        assert websocket.close_code == 4403
        assert not relay_harness.server.connected


@pytest.mark.asyncio
async def test_missing_proof_times_out_with_4403(
    relay_harness: RelayHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(relay_module, "_AUTH_TIMEOUT_SECONDS", 0.01)
    async with ClientSession() as session:
        websocket = await session.ws_connect(relay_url(relay_harness))
        challenge = await websocket.receive_json()
        assert challenge["type"] == "auth.challenge"

        message = await websocket.receive()

        assert message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
        assert websocket.close_code == 4403


@pytest.mark.asyncio
async def test_origin_validation_rejects_web_origin_and_allows_missing_origin(relay_harness: RelayHarness) -> None:
    async with ClientSession() as session:
        rejected = await session.ws_connect(relay_url(relay_harness), headers={"Origin": "https://example.com"})

        message = await rejected.receive()

        assert message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
        assert rejected.close_code == 4403

        allowed = await session.ws_connect(relay_url(relay_harness))
        challenge = await allowed.receive_json()

        assert challenge["type"] == "auth.challenge"
        await allowed.close()


@pytest.mark.asyncio
async def test_request_response_error_and_timeout_paths(relay_harness: RelayHarness) -> None:
    async with ClientSession() as session:
        websocket = await authenticate(session, relay_harness)

        success_task = asyncio.create_task(relay_harness.server.request("tabs.list", {}))
        success_request = await websocket.receive_json()
        assert success_request == {"v": 1, "type": "request", "id": "r-1", "op": "tabs.list", "args": {}}
        await websocket.send_json(
            {"v": 1, "type": "response", "id": success_request["id"], "ok": True, "result": {"tabs": []}}
        )
        assert await success_task == {"tabs": []}

        error_task = asyncio.create_task(relay_harness.server.request("debugger.attach", {"tabId": 17}))
        error_request = await websocket.receive_json()
        await websocket.send_json(
            {
                "v": 1,
                "type": "response",
                "id": error_request["id"],
                "ok": False,
                "error": {"code": "TAB_NOT_SCOPED", "message": "tab is not shared"},
            }
        )
        with pytest.raises(ExtensionRequestError) as error_info:
            await error_task
        assert error_info.value.code == "TAB_NOT_SCOPED"
        assert error_info.value.message == "tab is not shared"

        timeout_task = asyncio.create_task(relay_harness.server.request("tabs.list", {}, timeout=0.01))
        timeout_request = await websocket.receive_json()
        assert timeout_request["id"] == "r-3"
        with pytest.raises(ExtensionRequestError) as timeout_info:
            await timeout_task
        assert timeout_info.value.code == "INTERNAL"
        assert timeout_info.value.message == "extension request timed out: tabs.list"


@pytest.mark.asyncio
async def test_concurrent_same_session_requests_are_written_in_issue_order() -> None:
    server = ExtensionRelayServer(TOKEN, 0, lambda _event, _params: asyncio.sleep(0))
    arrivals: list[int] = []

    class OvertakingWebSocket:
        closed = False

        async def send_json(self, frame: dict) -> None:
            sequence = frame["args"]["params"]["sequence"]
            if sequence == 0:
                await asyncio.sleep(0.01)
            arrivals.append(sequence)
            server._pending.pop(frame["id"]).set_result({"sequence": sequence})

    server._websocket = OvertakingWebSocket()  # type: ignore[assignment]
    server._connected_event.set()
    tasks = []
    for sequence in range(5):
        tasks.append(
            asyncio.create_task(
                server.request(
                    "debugger.send",
                    {
                        "tabId": 17,
                        "sessionId": "child-17",
                        "method": "Runtime.evaluate",
                        "params": {"sequence": sequence},
                    },
                )
            )
        )
        await asyncio.sleep(0)

    assert await asyncio.gather(*tasks) == [{"sequence": sequence} for sequence in range(5)]
    assert arrivals == list(range(5))


@pytest.mark.asyncio
async def test_slow_first_response_does_not_delay_second_request_arrival(
    relay_harness: RelayHarness,
) -> None:
    async with ClientSession() as session:
        websocket = await authenticate(session, relay_harness)
        first_task = asyncio.create_task(
            relay_harness.server.request(
                "debugger.send",
                {"tabId": 17, "sessionId": "child-17", "method": "Runtime.enable", "params": {}},
            )
        )
        first_request = await websocket.receive_json()
        second_task = asyncio.create_task(
            relay_harness.server.request(
                "debugger.send",
                {
                    "tabId": 17,
                    "sessionId": "child-17",
                    "method": "Runtime.runIfWaitingForDebugger",
                    "params": {},
                },
            )
        )

        second_request = await websocket.receive_json(timeout=0.5)
        assert [first_request["id"], second_request["id"]] == ["r-1", "r-2"]
        await websocket.send_json({"v": 1, "type": "response", "id": second_request["id"], "ok": True, "result": {}})
        assert await second_task == {}
        assert not first_task.done()

        await websocket.send_json({"v": 1, "type": "response", "id": first_request["id"], "ok": True, "result": {}})
        assert await first_task == {}


@pytest.mark.asyncio
async def test_response_larger_than_default_aiohttp_limit_reaches_requester(relay_harness: RelayHarness) -> None:
    async with ClientSession() as session:
        websocket = await authenticate(session, relay_harness)
        response_task = asyncio.create_task(relay_harness.server.request("debugger.send", {}))
        request = await websocket.receive_json()
        large_payload = "x" * (6 * 1024 * 1024)

        await websocket.send_json(
            {"v": 1, "type": "response", "id": request["id"], "ok": True, "result": {"data": large_payload}}
        )

        assert await response_task == {"data": large_payload}


@pytest.mark.asyncio
async def test_new_authenticated_connection_replaces_old_connection(relay_harness: RelayHarness) -> None:
    async with ClientSession() as session:
        first = await authenticate(session, relay_harness)
        second_task = asyncio.create_task(authenticate(session, relay_harness))
        first_message = await first.receive()
        second = await second_task

        assert first_message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
        assert first.close_code == 4000
        assert relay_harness.server.connected
        assert not second.closed
        assert relay_harness.disconnect_called.is_set()


@pytest.mark.asyncio
async def test_scope_events_add_create_and_remove_scoped_tabs(relay_harness: RelayHarness) -> None:
    async with ClientSession() as session:
        websocket = await authenticate(session, relay_harness)
        await websocket.send_json(
            {
                "v": 1,
                "type": "event",
                "event": "scope.tabAdded",
                "params": {"tabId": 21, "url": "https://example.com/one", "title": "One"},
            }
        )
        await websocket.send_json(
            {
                "v": 1,
                "type": "event",
                "event": "tabs.created",
                "params": {"tabId": 22, "openerTabId": 21, "url": "https://example.com/two"},
            }
        )
        await websocket.send_json(
            {
                "v": 1,
                "type": "event",
                "event": "scope.tabRemoved",
                "params": {"tabId": 21, "reason": "unshared"},
            }
        )

        async def all_events_received() -> None:
            while len(relay_harness.events) < 4:
                await asyncio.sleep(0)

        await asyncio.wait_for(all_events_received(), 1)
        assert relay_harness.server.scoped_tabs == [{"tabId": 22, "url": "https://example.com/two", "title": ""}]


@pytest.mark.asyncio
async def test_disconnect_fails_pending_requests_clears_tabs_and_calls_callback(
    relay_harness: RelayHarness,
) -> None:
    async with ClientSession() as session:
        websocket = await authenticate(session, relay_harness)
        relay_harness.event_received.clear()
        await websocket.send_json(
            {
                "v": 1,
                "type": "event",
                "event": "extension.hello",
                "params": {
                    "extensionVersion": "1.0.0",
                    "scopedTabs": [{"tabId": 17, "url": "about:blank", "title": ""}],
                },
            }
        )
        await asyncio.wait_for(relay_harness.event_received.wait(), 1)

        pending_request = asyncio.create_task(relay_harness.server.request("tabs.list", {}))
        await websocket.receive_json()
        await websocket.close()

        with pytest.raises(BrowserExtensionNotConnectedError):
            await pending_request
        await asyncio.wait_for(relay_harness.disconnect_called.wait(), 1)
        assert not relay_harness.server.connected
        assert relay_harness.server.scoped_tabs == []


@pytest.mark.asyncio
async def test_extension_ping_receives_pong(relay_harness: RelayHarness) -> None:
    async with ClientSession() as session:
        websocket = await authenticate(session, relay_harness)

        await websocket.send_json({"v": 1, "type": "ping"})

        assert await websocket.receive_json() == {"v": 1, "type": "pong"}


@pytest.mark.asyncio
async def test_request_without_extension_fails_immediately(relay_harness: RelayHarness) -> None:
    with pytest.raises(BrowserExtensionNotConnectedError):
        await relay_harness.server.request("tabs.list", {})
