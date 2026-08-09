from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import suppress

import aiohttp
import pytest
import pytest_asyncio

import skyvern.browser_extension.broker.client as client_module
from skyvern.browser_extension.broker.client import BrokerTransport, LegacyBridgeOwnerError
from skyvern.browser_extension.broker.protocol import (
    BROKER_FRAME_VERSION,
    BROKER_HEALTH_PATH,
    BROKER_PROTOCOL_VERSION,
    BROKER_WS_PATH,
    build_broker_nonce,
    compute_broker_client_proof,
)
from skyvern.browser_extension.broker.server import BrokerServer
from skyvern.browser_extension.errors import BrowserExtensionNotConnectedError, ExtensionRequestError
from skyvern.browser_extension.relay import ExtensionRelayServer

from ._broker_fakes import EXTENSION_ORIGIN, FakeExtension, wait_for

TOKEN = "broker-server-test-token"


class BrokerHarness:
    def __init__(self, server: BrokerServer) -> None:
        self.server = server
        self.extension: FakeExtension | None = None
        self._transports: list[BrokerTransport] = []
        self.events: dict[str, list[tuple[str, dict]]] = {}
        self.disconnects: dict[str, int] = {}

    @property
    def port(self) -> int:
        return self.server.bound_port

    async def attach_extension(self, scoped_tabs: list[dict] | None = None) -> FakeExtension:
        extension = FakeExtension(self.port, TOKEN)
        await extension.connect(scoped_tabs)
        self.extension = extension
        return extension

    async def add_client(self, name: str) -> BrokerTransport:
        self.events[name] = []
        self.disconnects[name] = 0

        async def on_event(event: str, params: dict) -> None:
            self.events[name].append((event, params))

        async def on_disconnect() -> None:
            self.disconnects[name] += 1

        transport = BrokerTransport(TOKEN, self.port, on_event, on_disconnect)
        await transport.start()
        self._transports.append(transport)
        return transport

    async def close(self) -> None:
        for transport in self._transports:
            await transport.stop()
        if self.extension is not None:
            await self.extension.close()
        await self.server.stop()

    def events_for(self, name: str, event: str) -> list[dict]:
        return [params for recorded, params in self.events[name] if recorded == event]


@pytest.fixture(autouse=True)
def never_spawn_a_real_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "spawn_daemon", lambda port: False)


@pytest_asyncio.fixture
async def harness() -> AsyncGenerator[BrokerHarness]:
    server = BrokerServer(TOKEN, 0, idle_timeout_seconds=3600.0)
    await server.start()
    harness = BrokerHarness(server)
    try:
        yield harness
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_two_agents_each_drive_their_own_tab_through_one_extension_socket(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    second = await harness.add_client("second")

    first_tab = (await first.request("tabs.create", {"url": "https://first.test"}))["tabId"]
    second_tab = (await second.request("tabs.create", {"url": "https://second.test"}))["tabId"]

    assert first_tab != second_tab
    await wait_for(lambda: bool(first.scoped_tabs) and bool(second.scoped_tabs))
    assert [tab["tabId"] for tab in first.scoped_tabs] == [first_tab]
    assert [tab["tabId"] for tab in second.scoped_tabs] == [second_tab]

    await first.request("debugger.attach", {"tabId": first_tab})
    await second.request("debugger.attach", {"tabId": second_tab})
    assert ("tabs.create", {"url": "https://first.test"}) in extension.requests
    assert ("debugger.attach", {"tabId": second_tab}) in extension.requests


@pytest.mark.asyncio
async def test_a_new_tab_is_never_offered_to_another_idle_agent(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    second = await harness.add_client("second")

    extension.create_response_gate = asyncio.Event()
    creating = asyncio.create_task(second.request("tabs.create", {"url": "https://second.test"}))
    await wait_for(lambda: bool(harness.server._deferred_tab_added))
    await harness.server._rotate_offers()
    extension.create_response_gate.set()

    second_tab = (await creating)["tabId"]
    await wait_for(lambda: [tab["tabId"] for tab in second.scoped_tabs] == [second_tab])

    assert first.scoped_tabs == []


@pytest.mark.asyncio
async def test_a_new_tab_removed_before_create_returns_is_not_reintroduced(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    second = await harness.add_client("second")

    extension.create_response_gate = asyncio.Event()
    creating = asyncio.create_task(second.request("tabs.create", {"url": "https://second.test"}))
    await wait_for(lambda: bool(harness.server._deferred_tab_added))
    tab_id = next(iter(harness.server._deferred_tab_added))
    await extension.send_event(
        "tabs.created",
        {"tabId": 900, "openerTabId": tab_id, "url": "https://popup.test"},
    )
    await extension.send_event(
        "tabs.created",
        {"tabId": 901, "openerTabId": 900, "url": "https://nested-popup.test"},
    )
    await wait_for(lambda: len(harness.server._deferred_tab_created) == 2)

    await extension.send_event("scope.tabRemoved", {"tabId": tab_id, "reason": "closed"})
    await wait_for(lambda: tab_id in harness.server._discarded_tab_ids)
    extension.create_response_gate.set()
    assert (await creating)["tabId"] == tab_id
    await asyncio.sleep(0.05)

    assert first.scoped_tabs == []
    assert second.scoped_tabs == []
    assert not harness.server._leases.knows_tab(tab_id)
    assert not harness.server._leases.knows_tab(900)
    assert not harness.server._leases.knows_tab(901)


@pytest.mark.asyncio
async def test_a_popup_tree_created_before_its_opener_response_follows_the_creator(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    second = await harness.add_client("second")

    extension.create_response_gate = asyncio.Event()
    creating = asyncio.create_task(second.request("tabs.create", {"url": "https://second.test"}))
    await wait_for(lambda: bool(harness.server._deferred_tab_added))
    opener_tab_id = next(iter(harness.server._deferred_tab_added))
    await extension.send_event(
        "tabs.created",
        {"tabId": 900, "openerTabId": opener_tab_id, "url": "https://popup.test"},
    )
    await extension.send_event(
        "tabs.created",
        {"tabId": 901, "openerTabId": 900, "url": "https://nested-popup.test"},
    )
    await wait_for(lambda: len(harness.server._deferred_tab_created) == 2)
    await harness.server._rotate_offers()
    extension.create_response_gate.set()

    assert (await creating)["tabId"] == opener_tab_id
    await wait_for(lambda: {tab["tabId"] for tab in second.scoped_tabs} == {opener_tab_id, 900, 901})

    assert first.scoped_tabs == []
    assert [event["tabId"] for event in harness.events_for("second", "tabs.created")] == [900, 901]


@pytest.mark.asyncio
async def test_an_extension_disconnect_drops_a_deferred_created_tab(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    await harness.add_client("first")
    second = await harness.add_client("second")

    extension.create_response_gate = asyncio.Event()
    creating = asyncio.create_task(second.request("tabs.create", {"url": "https://second.test"}))
    await wait_for(lambda: bool(harness.server._deferred_tab_added))
    tab_id = next(iter(harness.server._deferred_tab_added))
    await extension.close()

    with pytest.raises(BrowserExtensionNotConnectedError):
        await creating
    await wait_for(lambda: not second.connected)
    assert second.scoped_tabs == []
    assert not harness.server._leases.knows_tab(tab_id)


@pytest.mark.asyncio
async def test_an_agent_cannot_touch_a_tab_another_agent_leased(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    second = await harness.add_client("second")
    leased_tab = (await first.request("tabs.create", {"url": "https://first.test"}))["tabId"]
    extension.requests.clear()

    for operation in ("debugger.attach", "debugger.detach", "tabs.activate", "tabs.remove"):
        with pytest.raises(ExtensionRequestError) as error_info:
            await second.request(operation, {"tabId": leased_tab})
        assert error_info.value.code == "TAB_NOT_SCOPED"

    with pytest.raises(ExtensionRequestError):
        await second.request("debugger.send", {"tabId": leased_tab, "method": "Page.navigate", "params": {}})
    assert extension.requests == []


@pytest.mark.asyncio
async def test_the_lease_gate_normalizes_integer_floats_and_rejects_other_types(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    second = await harness.add_client("second")
    leased_tab = (await first.request("tabs.create", {"url": "https://first.test"}))["tabId"]
    extension.requests.clear()

    await first.request("debugger.attach", {"tabId": float(leased_tab)})
    assert extension.requests == [("debugger.attach", {"tabId": leased_tab})]

    for invalid_tab_id in (str(leased_tab), None, True, leased_tab + 0.5):
        with pytest.raises(ExtensionRequestError) as error_info:
            await second.request("debugger.attach", {"tabId": invalid_tab_id})
        assert error_info.value.code == "TAB_NOT_SCOPED"

    with pytest.raises(ExtensionRequestError) as error_info:
        await second.request("debugger.attach", {"tabId": float(leased_tab)})
    assert error_info.value.code == "TAB_NOT_SCOPED"
    assert extension.requests == [("debugger.attach", {"tabId": leased_tab})]


@pytest.mark.asyncio
async def test_debugger_events_reach_only_the_agent_that_leased_the_tab(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    await harness.add_client("second")
    leased_tab = (await first.request("tabs.create", {"url": "https://first.test"}))["tabId"]

    await extension.send_event(
        "debugger.event",
        {"tabId": leased_tab, "method": "Page.loadEventFired", "params": {}},
    )
    await wait_for(lambda: bool(harness.events_for("first", "debugger.event")))

    assert harness.events_for("second", "debugger.event") == []
    assert harness.events_for("first", "debugger.event")[0]["tabId"] == leased_tab


@pytest.mark.asyncio
async def test_a_popup_follows_the_agent_that_owns_its_opener(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    await harness.add_client("second")
    opener_tab = (await first.request("tabs.create", {"url": "https://first.test"}))["tabId"]

    await extension.send_event(
        "tabs.created",
        {"tabId": 900, "openerTabId": opener_tab, "url": "https://popup.test"},
    )
    await wait_for(lambda: {tab["tabId"] for tab in first.scoped_tabs} == {opener_tab, 900})

    assert harness.events_for("second", "tabs.created") == []
    assert harness.events_for("first", "tabs.created")[0]["tabId"] == 900


@pytest.mark.asyncio
async def test_a_hand_shared_tab_is_offered_to_exactly_one_idle_agent(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    second = await harness.add_client("second")

    await extension.send_event("scope.tabAdded", {"tabId": 500, "url": "https://shared.test", "title": "Shared"})
    await wait_for(lambda: bool(first.scoped_tabs) or bool(second.scoped_tabs))
    await asyncio.sleep(0.05)

    offered = [transport for transport in (first, second) if transport.scoped_tabs]
    assert len(offered) == 1
    assert offered[0].scoped_tabs[0]["tabId"] == 500

    # The offer is only a candidacy until the agent actually acts on the tab.
    await offered[0].request("debugger.attach", {"tabId": 500})
    other = second if offered[0] is first else first
    with pytest.raises(ExtensionRequestError) as error_info:
        await other.request("debugger.attach", {"tabId": 500})
    assert error_info.value.code == "TAB_NOT_SCOPED"


@pytest.mark.asyncio
async def test_a_departing_agent_releases_its_tabs_for_the_next_one(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    leased_tab = (await first.request("tabs.create", {"url": "https://first.test"}))["tabId"]
    await first.request("debugger.attach", {"tabId": leased_tab})
    extension.requests.clear()

    await first.stop()
    await wait_for(lambda: harness.server.client_count == 0)
    await wait_for(lambda: ("debugger.detach", {"tabId": leased_tab}) in extension.requests)

    second = await harness.add_client("second")
    await extension.send_event("scope.tabAdded", {"tabId": leased_tab, "url": "https://first.test", "title": ""})
    await wait_for(lambda: bool(second.scoped_tabs))
    assert second.scoped_tabs[0]["tabId"] == leased_tab


@pytest.mark.asyncio
async def test_closing_a_tab_notifies_only_its_holder_and_frees_the_lease(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    await harness.add_client("second")
    leased_tab = (await first.request("tabs.create", {"url": "https://first.test"}))["tabId"]

    await extension.send_event("scope.tabRemoved", {"tabId": leased_tab, "reason": "closed"})
    await wait_for(lambda: bool(harness.events_for("first", "scope.tabRemoved")))

    assert harness.events_for("second", "scope.tabRemoved") == []
    assert first.scoped_tabs == []


@pytest.mark.asyncio
async def test_an_extension_reconnect_voids_every_agent_s_claims(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    first = await harness.add_client("first")
    leased_tab = (await first.request("tabs.create", {"url": "https://first.test"}))["tabId"]

    await extension.close()
    await wait_for(lambda: not first.connected)
    assert harness.disconnects["first"] == 1
    assert first.scoped_tabs == []

    replacement = await harness.attach_extension(scoped_tabs=[{"tabId": leased_tab, "url": "", "title": ""}])
    await wait_for(lambda: first.connected)
    # The tab survived the reconnect but its debugger attachment did not, so it is unowned again
    # and reoffered rather than silently still leased.
    await wait_for(lambda: [tab["tabId"] for tab in first.scoped_tabs] == [leased_tab])
    assert replacement is harness.extension


@pytest.mark.asyncio
async def test_requests_fail_cleanly_while_no_extension_is_attached(harness: BrokerHarness) -> None:
    transport = await harness.add_client("first")

    with pytest.raises(BrowserExtensionNotConnectedError):
        await transport.request("tabs.create", {"url": "https://example.test"})
    assert not transport.connected


@pytest.mark.asyncio
async def test_a_client_can_mint_a_pairing_nonce_without_the_token_crossing_the_wire(
    harness: BrokerHarness,
) -> None:
    transport = await harness.add_client("first")

    first_nonce = await transport.acquire_pairing_nonce()
    second_nonce = await transport.acquire_pairing_nonce()

    assert first_nonce and second_nonce and first_nonce != second_nonce
    assert TOKEN not in first_nonce
    assert transport.bound_port == harness.port


@pytest.mark.asyncio
async def test_status_reports_the_daemon_without_leaking_tab_contents(harness: BrokerHarness) -> None:
    await harness.attach_extension()
    transport = await harness.add_client("first")
    await transport.request("tabs.create", {"url": "https://secret.test"})

    status = await transport.broker_status()

    assert status["extensionConnected"] is True
    assert status["clients"] == 1
    assert status["protocol"] == BROKER_PROTOCOL_VERSION
    assert "secret.test" not in json.dumps(status)


@pytest.mark.asyncio
async def test_broker_only_operations_stay_off_the_extension_socket(harness: BrokerHarness) -> None:
    extension = await harness.attach_extension()
    transport = await harness.add_client("first")

    await transport.acquire_pairing_nonce()
    await transport.broker_status()
    with pytest.raises(ExtensionRequestError) as error_info:
        await transport.request("tabs.shareEverything", {})

    assert error_info.value.code == "OP_NOT_ALLOWED"
    assert extension.requests == []


@pytest.mark.asyncio
async def test_a_client_without_the_token_is_rejected(harness: BrokerHarness) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://127.0.0.1:{harness.port}{BROKER_WS_PATH}") as websocket:
            challenge = json.loads((await websocket.receive()).data)
            client_nonce = build_broker_nonce()
            await websocket.send_json(
                {
                    "v": BROKER_FRAME_VERSION,
                    "type": "auth.proof",
                    "clientNonce": client_nonce,
                    "proof": compute_broker_client_proof("wrong-token", challenge["serverNonce"], client_nonce),
                }
            )
            closed = await websocket.receive()

    assert closed.type is aiohttp.WSMsgType.CLOSE
    assert websocket.close_code == 4403


@pytest.mark.asyncio
async def test_a_web_page_cannot_reach_the_broker_endpoint(harness: BrokerHarness) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"ws://127.0.0.1:{harness.port}{BROKER_WS_PATH}",
            headers={"Origin": "https://evil.test"},
        ) as websocket:
            closed = await websocket.receive()

    assert closed.type is aiohttp.WSMsgType.CLOSE
    assert websocket.close_code == 4403


@pytest.mark.asyncio
async def test_the_extension_origin_is_also_refused_on_the_broker_endpoint(harness: BrokerHarness) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"ws://127.0.0.1:{harness.port}{BROKER_WS_PATH}",
            headers={"Origin": EXTENSION_ORIGIN},
        ) as websocket:
            closed = await websocket.receive()

    assert closed.type is aiohttp.WSMsgType.CLOSE
    assert websocket.close_code == 4403


@pytest.mark.asyncio
async def test_the_health_endpoint_is_inert(harness: BrokerHarness) -> None:
    await harness.attach_extension()
    transport = await harness.add_client("first")
    await transport.request("tabs.create", {"url": "https://secret.test"})

    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{harness.port}{BROKER_HEALTH_PATH}") as response:
            body = await response.text()
            payload = json.loads(body)

    assert response.status == 200
    assert payload == {"v": BROKER_FRAME_VERSION, "broker": True, "protocol": BROKER_PROTOCOL_VERSION}
    assert TOKEN not in body
    assert "secret.test" not in body


@pytest.mark.asyncio
async def test_a_pre_broker_bridge_is_reported_instead_of_spawning_a_second_daemon() -> None:
    relay = ExtensionRelayServer(TOKEN, 0, _ignore_event)
    await relay.start()
    try:
        transport = BrokerTransport(TOKEN, relay.bound_port, _ignore_event)
        with pytest.raises(LegacyBridgeOwnerError):
            await transport.start()
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_a_newer_client_makes_the_daemon_step_down(
    harness: BrokerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "BROKER_PROTOCOL_VERSION", BROKER_PROTOCOL_VERSION + 1)
    transport = BrokerTransport(TOKEN, harness.port, _ignore_event)
    connecting = asyncio.create_task(transport.start())

    try:
        assert await asyncio.wait_for(harness.server.wait_for_shutdown(), 5.0) == "version_skew"
    finally:
        connecting.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await connecting
        await transport.stop()


@pytest.mark.asyncio
async def test_an_older_client_leaves_the_daemon_alone(harness: BrokerHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "BROKER_PROTOCOL_VERSION", BROKER_PROTOCOL_VERSION - 1)

    transport = await harness.add_client("older")

    assert transport.daemon_protocol == BROKER_PROTOCOL_VERSION
    assert harness.server.client_count == 1


@pytest.mark.asyncio
async def test_the_daemon_shuts_itself_down_once_the_last_agent_leaves() -> None:
    server = BrokerServer(TOKEN, 0, idle_timeout_seconds=0.0)
    await server.start()
    try:
        transport = BrokerTransport(TOKEN, server.bound_port, _ignore_event)
        await transport.start()
        await transport.stop()

        assert await asyncio.wait_for(server.wait_for_shutdown(), 15.0) == "idle"
    finally:
        await server.stop()


async def _ignore_event(event: str, params: dict) -> None:
    return
