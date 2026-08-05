from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
import pytest_asyncio
from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType

from skyvern.browser_extension.cdp_adapter import ExtensionCdpAdapter
from skyvern.browser_extension.errors import ExtensionRequestError
from skyvern.browser_extension.target_registry import VirtualTargetRegistry


class StubRelay:
    def __init__(self, scoped_tabs: list[dict] | None = None) -> None:
        self.scoped_tabs = scoped_tabs or []
        self.calls: list[tuple[str, dict]] = []
        self.next_tab_id = 100
        self.fail_next: ExtensionRequestError | None = None
        self.fail_attach_tab_ids: set[int] = set()
        self.block_attach_tab_id: int | None = None
        self.attach_started = asyncio.Event()
        self.release_attach = asyncio.Event()

    async def request(self, op: str, args: dict, timeout: float = 30.0) -> dict:
        self.calls.append((op, args))
        if self.fail_next is not None:
            error = self.fail_next
            self.fail_next = None
            raise error
        if op == "debugger.attach":
            tab_id = args["tabId"]
            if tab_id in self.fail_attach_tab_ids:
                raise ExtensionRequestError("CDP_ERROR", "attach failed")
            if tab_id == self.block_attach_tab_id:
                self.attach_started.set()
                await self.release_attach.wait()
        if op == "tabs.create":
            tab_id = self.next_tab_id
            self.next_tab_id += 1
            return {"tabId": tab_id}
        if op == "debugger.send":
            return {"result": {"forwardedMethod": args["method"]}}
        return {}


@pytest_asyncio.fixture
async def adapter_server() -> AsyncIterator[tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry]]:
    relay = StubRelay()
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    await adapter.start()
    try:
        yield adapter, relay, registry
    finally:
        await adapter.stop()


async def receive_response(ws: ClientWebSocketResponse, request_id: int) -> dict:
    while True:
        message = await ws.receive_json(timeout=2)
        if message.get("id") == request_id:
            return message


async def receive_event(ws: ClientWebSocketResponse, method: str) -> dict:
    while True:
        message = await ws.receive_json(timeout=2)
        if message.get("method") == method:
            return message


def assert_subset(actual: dict, expected: dict) -> None:
    for key, value in expected.items():
        if isinstance(value, dict):
            assert_subset(actual[key], value)
        else:
            assert actual[key] == value


# Derived from chromium/crBrowser.js, chromium/crConnection.js, and chromium/chromium.js.
ROOT_CONNECT_CONTRACT = [
    (
        "Browser.getVersion",
        {"product": "Chrome/999.0.0.0", "userAgent": "Skyvern-Extension-Bridge"},
    ),
    ("Target.setAutoAttach", {}),
    ("Browser.setDownloadBehavior", {}),
    ("Target.getTargetInfo", {"targetInfo": {"targetId": "skyvern-browser", "type": "browser"}}),
]

ROOT_CONNECT_PARAMS = {
    "Browser.getVersion": {},
    "Target.setAutoAttach": {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
    "Browser.setDownloadBehavior": {"behavior": "allowAndName", "eventsEnabled": True},
    "Target.getTargetInfo": {},
}


@pytest.mark.asyncio
async def test_capability_path_is_required(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, _, _ = adapter_server
    wrong_url = adapter.cdp_ws_url.rsplit("/", 1)[0] + "/wrong"

    async with ClientSession() as client:
        response = await client.get(wrong_url.replace("ws://", "http://"))
        assert response.status == 404
        response.release()

        ws = await client.ws_connect(adapter.cdp_ws_url)
        assert not ws.closed
        await ws.close()


@pytest.mark.asyncio
async def test_root_connect_contract_fixture(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, _, _ = adapter_server

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        for request_id, (method, minimal_reply) in enumerate(ROOT_CONNECT_CONTRACT, start=1):
            await ws.send_json({"id": request_id, "method": method, "params": ROOT_CONNECT_PARAMS[method]})
            response = await receive_response(ws, request_id)
            assert "error" not in response
            assert_subset(response["result"], minimal_reply)


@pytest.mark.asyncio
async def test_auto_attach_creates_blank_tab_when_scope_is_empty(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
        assert await receive_response(ws, 1) == {"id": 1, "result": {}}
        attached = await receive_event(ws, "Target.attachedToTarget")
        root_session_id = registry.root_session_id(100)

    assert relay.calls[:2] == [
        ("tabs.create", {"url": "about:blank"}),
        ("debugger.attach", {"tabId": 100}),
    ]
    assert attached["params"] == {
        "sessionId": root_session_id,
        "targetInfo": {
            "targetId": "tab-100",
            "type": "page",
            "title": "",
            "url": "about:blank",
            "attached": True,
            "canAccessOpener": False,
            "browserContextId": "skyvern-default",
        },
        "waitingForDebugger": False,
    }


@pytest.mark.asyncio
async def test_auto_attach_existing_and_later_scoped_tabs() -> None:
    relay = StubRelay(
        [
            {"tabId": 7, "url": "https://one.example", "title": "One"},
            {"tabId": 8, "url": "https://two.example", "title": "Two"},
        ]
    )
    relay.fail_next = ExtensionRequestError("CDP_ERROR", "Another debugger is already attached")
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    await adapter.start()
    try:
        async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
            await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
            await receive_response(ws, 1)
            events = [await receive_event(ws, "Target.attachedToTarget") for _ in range(2)]

            await adapter.handle_extension_event(
                "scope.tabAdded", {"tabId": 9, "url": "https://three.example", "title": "Three"}
            )
            later = await receive_event(ws, "Target.attachedToTarget")

            await adapter.handle_extension_event(
                "tabs.created", {"tabId": 10, "openerTabId": 7, "url": "https://popup.example"}
            )
            popup = await receive_event(ws, "Target.attachedToTarget")
    finally:
        await adapter.stop()

    assert [call for call in relay.calls if call[0] == "debugger.attach"] == [
        ("debugger.attach", {"tabId": 7}),
        ("debugger.attach", {"tabId": 8}),
        ("debugger.attach", {"tabId": 9}),
        ("debugger.attach", {"tabId": 10}),
    ]
    assert [event["params"]["targetInfo"]["targetId"] for event in events] == ["tab-7", "tab-8"]
    assert later["params"]["targetInfo"]["targetId"] == "tab-9"
    assert popup["params"]["targetInfo"]["openerId"] == "tab-7"


@pytest.mark.asyncio
async def test_auto_attach_post_reply_failure_skips_failed_tab_without_second_response() -> None:
    relay = StubRelay(
        [
            {"tabId": 30, "url": "https://bad.example", "title": "Bad"},
            {"tabId": 31, "url": "https://good.example", "title": "Good"},
        ]
    )
    relay.fail_attach_tab_ids.add(30)
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    await adapter.start()
    try:
        async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
            await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})

            assert await receive_response(ws, 1) == {"id": 1, "result": {}}
            attached = await receive_event(ws, "Target.attachedToTarget")
            assert attached["params"]["targetInfo"]["targetId"] == "tab-31"
            with pytest.raises(TimeoutError):
                await ws.receive_json(timeout=0.05)
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_tab_removal_tombstones_in_flight_background_attach() -> None:
    relay = StubRelay([{"tabId": 40, "url": "https://existing.example", "title": "Existing"}])
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    await adapter.start()
    try:
        async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
            await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
            await receive_response(ws, 1)
            await receive_event(ws, "Target.attachedToTarget")

            relay.block_attach_tab_id = 41
            await adapter.handle_extension_event(
                "scope.tabAdded", {"tabId": 41, "url": "https://racy.example", "title": "Racy"}
            )
            await asyncio.wait_for(relay.attach_started.wait(), 1)
            removal_task = asyncio.create_task(
                adapter.handle_extension_event("scope.tabRemoved", {"tabId": 41, "reason": "unshared"})
            )
            await asyncio.sleep(0)
            relay.release_attach.set()
            await removal_task

            with pytest.raises(KeyError):
                registry.root_session_id(41)
            with pytest.raises(TimeoutError):
                await ws.receive_json(timeout=0.05)
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_session_command_routes_to_relay_and_preserves_session_id(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(42, "https://example.com", "Example")
    session_id = registry.root_session_id(42)

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json(
            {"id": 5, "sessionId": session_id, "method": "Runtime.evaluate", "params": {"expression": "1+1"}}
        )
        response = await receive_response(ws, 5)

        await ws.send_json({"id": 6, "sessionId": "missing", "method": "Runtime.enable", "params": {}})
        missing = await receive_response(ws, 6)

    assert relay.calls[-1] == (
        "debugger.send",
        {"tabId": 42, "method": "Runtime.evaluate", "params": {"expression": "1+1"}},
    )
    assert response == {"id": 5, "sessionId": session_id, "result": {"forwardedMethod": "Runtime.evaluate"}}
    assert missing == {
        "id": 6,
        "sessionId": "missing",
        "error": {"code": -32001, "message": "session not found"},
    }


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("Network.getAllCookies", {}),
        ("Network.getCookies", {"urls": ["https://unshared.example"]}),
    ],
)
@pytest.mark.asyncio
async def test_session_command_rejects_denied_cdp_methods(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
    method: str,
    params: dict,
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(42, "https://example.com", "Example")
    session_id = registry.root_session_id(42)

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "sessionId": session_id, "method": method, "params": params})
        response = await receive_response(ws, 1)

    assert response == {
        "id": 1,
        "sessionId": session_id,
        "error": {
            "code": -32000,
            "message": "CDP_METHOD_NOT_ALLOWED: The requested CDP method is not allowed.",
        },
    }
    assert relay.calls == []


@pytest.mark.asyncio
async def test_page_attach_aliases_are_unique_route_commands_and_receive_root_events() -> None:
    relay = StubRelay([{"tabId": 42, "url": "https://example.com", "title": "Example"}])
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    await adapter.start()
    try:
        async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
            await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
            await receive_response(ws, 1)
            primary_attached = await receive_event(ws, "Target.attachedToTarget")
            primary_session_id = primary_attached["params"]["sessionId"]

            for request_id in (2, 3):
                await ws.send_json(
                    {
                        "id": request_id,
                        "method": "Target.attachToTarget",
                        "params": {"targetId": "tab-42", "flatten": True},
                    }
                )
            first_alias = (await receive_response(ws, 2))["result"]["sessionId"]
            second_alias = (await receive_response(ws, 3))["result"]["sessionId"]

            assert len({primary_session_id, first_alias, second_alias}) == 3
            await ws.send_json({"id": 4, "sessionId": first_alias, "method": "Runtime.enable", "params": {}})
            assert (await receive_response(ws, 4))["sessionId"] == first_alias

            event_params = {"name": "networkAlmostIdle", "frameId": "main"}
            await adapter.handle_extension_event(
                "debugger.event",
                {"tabId": 42, "method": "Page.lifecycleEvent", "params": event_params},
            )
            routed_events = [await receive_event(ws, "Page.lifecycleEvent") for _ in range(3)]
            assert {event["sessionId"] for event in routed_events} == {
                primary_session_id,
                first_alias,
                second_alias,
            }

            await ws.send_json({"id": 5, "method": "Target.detachFromTarget", "params": {"sessionId": first_alias}})
            assert await receive_response(ws, 5) == {"id": 5, "result": {}}
            assert all(op != "debugger.detach" for op, _ in relay.calls)
            with pytest.raises(KeyError):
                registry.resolve_session(first_alias)
            assert registry.resolve_session(second_alias) == (42, None)
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_browser_alias_routes_root_commands_and_detaches_independently(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, _, registry = adapter_server

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.attachToBrowserTarget", "params": {}})
        first_alias = (await receive_response(ws, 1))["result"]["sessionId"]
        await ws.send_json({"id": 2, "method": "Target.attachToBrowserTarget", "params": {}})
        second_alias = (await receive_response(ws, 2))["result"]["sessionId"]

        assert first_alias != second_alias
        await ws.send_json({"id": 3, "sessionId": first_alias, "method": "Browser.getVersion", "params": {}})
        version = await receive_response(ws, 3)
        assert version["sessionId"] == first_alias
        assert version["result"]["protocolVersion"] == "1.3"

        await ws.send_json({"id": 4, "method": "Target.detachFromTarget", "params": {"sessionId": first_alias}})
        assert await receive_response(ws, 4) == {"id": 4, "result": {}}
        assert not registry.is_browser_session_alias(first_alias)
        assert registry.is_browser_session_alias(second_alias)

        await ws.send_json({"id": 5, "sessionId": first_alias, "method": "Browser.getVersion", "params": {}})
        assert (await receive_response(ws, 5))["error"]["code"] == -32001


@pytest.mark.asyncio
async def test_child_attach_recurses_and_detach_unregisters(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(12, "https://example.com", "Example")
    root_session_id = registry.root_session_id(12)

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
        await receive_response(ws, 1)
        await receive_event(ws, "Target.attachedToTarget")
        relay.calls.clear()

        target_info = {
            "targetId": "frame-12",
            "type": "iframe",
            "title": "",
            "url": "https://example.com/frame",
            "attached": True,
            "canAccessOpener": False,
            "browserContextId": "skyvern-default",
        }
        await adapter.handle_extension_event(
            "debugger.event",
            {
                "tabId": 12,
                "method": "Target.attachedToTarget",
                "params": {"sessionId": "child-12", "targetInfo": target_info, "waitingForDebugger": False},
            },
        )
        attached = await receive_event(ws, "Target.attachedToTarget")

        await ws.send_json({"id": 2, "sessionId": "child-12", "method": "Runtime.enable", "params": {}})
        await receive_response(ws, 2)

        await adapter.handle_extension_event(
            "debugger.event",
            {"tabId": 12, "method": "Target.detachedFromTarget", "params": {"sessionId": "child-12"}},
        )
        detached = await receive_event(ws, "Target.detachedFromTarget")

    assert attached["sessionId"] == root_session_id
    assert relay.calls[0] == (
        "debugger.send",
        {
            "tabId": 12,
            "sessionId": "child-12",
            "method": "Target.setAutoAttach",
            "params": {"flatten": True, "autoAttach": True, "waitForDebuggerOnStart": False},
        },
    )
    assert relay.calls[1][1]["sessionId"] == "child-12"
    assert detached["params"] == {"sessionId": "child-12"}
    with pytest.raises(KeyError):
        registry.resolve_session("child-12")


@pytest.mark.asyncio
async def test_get_target_info_returns_registered_child_target(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, _, registry = adapter_server
    registry.register_tab(13, "https://example.com", "Example")
    child_target_info = {
        "targetId": "frame-13",
        "type": "iframe",
        "title": "Frame",
        "url": "https://example.com/frame",
        "attached": True,
        "canAccessOpener": False,
        "browserContextId": "skyvern-default",
    }
    registry.register_child_session(13, "child-13", child_target_info)

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.getTargetInfo", "params": {"targetId": "frame-13"}})
        response = await receive_response(ws, 1)

    assert response == {"id": 1, "result": {"targetInfo": child_target_info}}


@pytest.mark.asyncio
async def test_create_and_close_target(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, _ = adapter_server

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.createTarget", "params": {"url": "https://example.com"}})
        attached = await receive_event(ws, "Target.attachedToTarget")
        created = await receive_response(ws, 1)

        await ws.send_json({"id": 2, "method": "Target.closeTarget", "params": {"targetId": "tab-100"}})
        closed = await receive_response(ws, 2)
        await adapter.handle_extension_event("scope.tabRemoved", {"tabId": 100, "reason": "closed"})
        detached = await receive_event(ws, "Target.detachedFromTarget")
        destroyed = await receive_event(ws, "Target.targetDestroyed")

    assert created == {"id": 1, "result": {"targetId": "tab-100"}}
    assert attached["params"]["targetInfo"]["url"] == "https://example.com"
    assert closed == {"id": 2, "result": {"success": True}}
    assert detached["params"]["sessionId"].startswith("sess-tab-100-")
    assert destroyed["params"] == {"targetId": "tab-100"}
    assert ("tabs.remove", {"tabId": 100}) in relay.calls


@pytest.mark.asyncio
async def test_create_target_and_scope_event_register_tab_once(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    original_request = relay.request

    async def request_with_scope_event(op: str, args: dict, timeout: float = 30.0) -> dict:
        result = await original_request(op, args, timeout)
        if op == "tabs.create":
            await adapter.handle_extension_event(
                "scope.tabAdded",
                {"tabId": result["tabId"], "url": args["url"], "title": ""},
            )
        return result

    relay.request = request_with_scope_event

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": True}})
        assert await receive_response(ws, 1) == {"id": 1, "result": {}}
        browser_created = await receive_event(ws, "Target.targetCreated")
        assert browser_created["params"]["targetInfo"]["targetId"] == "skyvern-browser"

        with patch.object(registry, "register_tab", wraps=registry.register_tab) as register_tab:
            await ws.send_json({"id": 2, "method": "Target.createTarget", "params": {"url": "https://example.com"}})
            target_created = await receive_event(ws, "Target.targetCreated")
            await receive_event(ws, "Target.attachedToTarget")
            created = await receive_response(ws, 2)

        assert register_tab.call_count == 1
        assert target_created["params"]["targetInfo"]["targetId"] == "tab-100"
        assert created == {"id": 2, "result": {"targetId": "tab-100"}}
        assert len(registry.list_page_targets()) == 1
        await adapter.handle_extension_event(
            "scope.tabAdded",
            {"tabId": 100, "url": "https://example.com", "title": ""},
        )
        with pytest.raises(TimeoutError):
            await ws.receive_json(timeout=0.05)


@pytest.mark.asyncio
async def test_debugger_detached_emits_destroyed_and_navigation_updates_target(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, _, registry = adapter_server
    registry.register_tab(15, "https://old.example", "Old")
    session_id = registry.root_session_id(15)

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await adapter.handle_extension_event(
            "debugger.event",
            {
                "tabId": 15,
                "method": "Page.frameNavigated",
                "params": {"frame": {"id": "main", "url": "https://new.example"}},
            },
        )
        navigated = await receive_event(ws, "Page.frameNavigated")
        assert navigated["sessionId"] == session_id
        assert registry.target_info("tab-15")["url"] == "https://new.example"

        await adapter.handle_extension_event("debugger.detached", {"tabId": 15, "reason": "canceled_by_user"})
        detached = await receive_event(ws, "Target.detachedFromTarget")
        destroyed = await receive_event(ws, "Target.targetDestroyed")

    assert detached["params"] == {"sessionId": session_id, "targetId": "tab-15"}
    assert destroyed["params"] == {"targetId": "tab-15"}
    with pytest.raises(KeyError):
        registry.root_session_id(15)


@pytest.mark.asyncio
async def test_unknown_root_and_extension_request_errors(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(18, "https://example.com", "Example")
    session_id = registry.root_session_id(18)

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Nope.missing", "params": {}})
        unknown = await receive_response(ws, 1)

        relay.fail_next = ExtensionRequestError("CDP_ERROR", "command failed")
        await ws.send_json({"id": 2, "sessionId": session_id, "method": "Runtime.enable", "params": {}})
        failed = await receive_response(ws, 2)

    assert unknown == {"id": 1, "error": {"code": -32601, "message": "'Nope.missing' wasn't found"}}
    assert failed == {
        "id": 2,
        "sessionId": session_id,
        "error": {"code": -32000, "message": "CDP_ERROR: command failed"},
    }


@pytest.mark.asyncio
async def test_second_client_rejected_and_disconnect_allows_future_client(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, _, registry = adapter_server
    registry.register_tab(20, "https://example.com", "Example")

    async with ClientSession() as client:
        first = await client.ws_connect(adapter.cdp_ws_url)
        second = await client.ws_connect(adapter.cdp_ws_url)
        await second.receive(timeout=2)
        assert second.close_code == 4409

        await adapter.on_extension_disconnect()
        await first.receive(timeout=2)
        assert first.close_code == 1001
        assert registry.list_page_targets() == []

        future = await client.ws_connect(adapter.cdp_ws_url)
        await future.send_json({"id": 1, "method": "Browser.getVersion", "params": {}})
        assert (await receive_response(future, 1))["result"]["protocolVersion"] == "1.3"
        await future.close()


@pytest.mark.asyncio
async def test_browser_close_replies_then_closes_without_removing_tabs(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(22, "https://example.com", "Example")

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 9, "method": "Browser.close", "params": {}})
        assert await receive_response(ws, 9) == {"id": 9, "result": {}}
        close_message = await ws.receive(timeout=2)
        assert close_message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

    assert ("debugger.detach", {"tabId": 22}) in relay.calls
    assert all(op != "tabs.remove" for op, _ in relay.calls)
