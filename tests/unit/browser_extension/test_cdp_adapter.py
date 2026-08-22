from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from aiohttp import ClientSession, ClientWebSocketResponse

from skyvern.browser_extension.cdp_adapter import ExtensionCdpAdapter
from skyvern.browser_extension.errors import (
    BrowserExtensionBrokerError,
    BrowserExtensionNotConnectedError,
    ExtensionRequestError,
)
from skyvern.browser_extension.target_registry import VirtualTargetRegistry


class StubRelay:
    def __init__(self, scoped_tabs: list[dict] | None = None) -> None:
        self.scoped_tabs = scoped_tabs or []
        self.calls: list[tuple[str, dict]] = []
        self.next_tab_id = 100
        self.fail_next: BrowserExtensionBrokerError | ExtensionRequestError | None = None
        self.fail_attach_tab_ids: set[int] = set()
        self.block_attach_tab_id: int | None = None
        self.attach_started = asyncio.Event()
        self.release_attach = asyncio.Event()
        self.block_detach_tab_id: int | None = None
        self.detach_started = asyncio.Event()
        self.release_detach = asyncio.Event()
        self.main_frame_ids: dict[int, str] = {}
        self.block_send_keys: set[tuple[str | None, str]] = set()
        self.fail_send_keys: dict[
            tuple[str | None, str],
            BrowserExtensionBrokerError
            | BrowserExtensionNotConnectedError
            | ExtensionRequestError
            | asyncio.CancelledError,
        ] = {}
        self.send_started: dict[tuple[str | None, str], asyncio.Event] = {}
        self.release_send: dict[tuple[str | None, str], asyncio.Event] = {}
        self.released_tabs: list[int] = []

    async def request(self, op: str, args: dict, timeout: float = 30.0) -> dict:
        self.calls.append((op, args))
        if self.fail_next is not None:
            error = self.fail_next
            self.fail_next = None
            raise error
        if op == "debugger.attach":
            tab_id = args["tabId"]
            if tab_id in self.fail_attach_tab_ids:
                raise ExtensionRequestError("ATTACH_FAILED", "attach failed")
            if tab_id == self.block_attach_tab_id:
                self.attach_started.set()
                await self.release_attach.wait()
        if op == "debugger.detach" and args["tabId"] == self.block_detach_tab_id:
            self.detach_started.set()
            await self.release_detach.wait()
        if op == "tabs.create":
            tab_id = self.next_tab_id
            self.next_tab_id += 1
            return {"tabId": tab_id}
        if op == "debugger.send":
            key = (args.get("sessionId"), args["method"])
            if key in self.block_send_keys:
                self.send_started.setdefault(key, asyncio.Event()).set()
                await self.release_send.setdefault(key, asyncio.Event()).wait()
            if key in self.fail_send_keys:
                raise self.fail_send_keys.pop(key)
            if args["method"] == "Page.getFrameTree" and args["tabId"] in self.main_frame_ids:
                frame_id = self.main_frame_ids[args["tabId"]]
                return {"result": {"frameTree": {"frame": {"id": frame_id}}}}
            return {"result": {"forwardedMethod": args["method"]}}
        return {}

    async def release_tab(self, tab_id: int) -> None:
        self.released_tabs.append(tab_id)
        await self.request("debugger.detach", {"tabId": tab_id}, timeout=2.0)


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
async def test_auto_attach_scope_event_during_blank_tab_creation_attaches_once(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, _ = adapter_server
    relay.block_attach_tab_id = 100
    original_request = relay.request

    async def request_with_scope_event(op: str, args: dict, timeout: float = 30.0) -> dict:
        result = await original_request(op, args, timeout)
        if op == "tabs.create":
            await adapter.handle_extension_event(
                "scope.tabAdded",
                {"tabId": result["tabId"], "url": args["url"], "title": ""},
            )
            asyncio.get_running_loop().call_soon(relay.release_attach.set)
        return result

    relay.request = request_with_scope_event

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
        assert await receive_response(ws, 1) == {"id": 1, "result": {}}
        await receive_event(ws, "Target.attachedToTarget")

    assert [call for call in relay.calls if call[0] == "debugger.attach"] == [
        ("debugger.attach", {"tabId": 100}),
    ]


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
async def test_coded_attach_failed_already_attached_error_propagates_without_unsharing_tab() -> None:
    tab = {"tabId": 11, "url": "https://shared.example", "title": "Shared"}
    relay = StubRelay([tab])
    relay.fail_next = ExtensionRequestError("ATTACH_FAILED", "Another debugger is already attached")
    adapter = ExtensionCdpAdapter(VirtualTargetRegistry(), relay)
    adapter._send = AsyncMock()

    await adapter._handle_client_text(
        None,  # type: ignore[arg-type]
        json.dumps({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}}),
    )

    adapter._send.assert_awaited_once_with(
        None,
        {"id": 1, "error": {"code": -32000, "message": "ATTACH_FAILED: Another debugger is already attached"}},
    )
    assert relay.scoped_tabs == [tab]
    assert not any(op == "debugger.detach" for op, _ in relay.calls)
    assert not any(op == "debugger.send" for op, _ in relay.calls)


@pytest.mark.asyncio
async def test_adopted_group_tab_uses_same_attach_flow_as_session_created_tab() -> None:
    relay = StubRelay()
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    adapter._auto_attach = True
    emit_attached = AsyncMock()
    adapter._emit_attached = emit_attached
    try:
        await adapter.handle_extension_event(
            "tabs.created",
            {"tabId": 50, "url": "https://created.example", "title": "Created"},
        )
        await asyncio.gather(*list(adapter._background_tasks))
        await adapter.handle_extension_event(
            "scope.tabAdded",
            {"tabId": 51, "url": "https://adopted.example", "title": "Adopted"},
        )
        await asyncio.gather(*list(adapter._background_tasks))
    finally:
        await adapter.stop()

    assert [call for call in relay.calls if call[0] == "debugger.attach"] == [
        ("debugger.attach", {"tabId": 50}),
        ("debugger.attach", {"tabId": 51}),
    ]
    assert [call.args[:2] for call in emit_attached.await_args_list] == [
        (50, "tab-50"),
        (51, "tab-51"),
    ]


@pytest.mark.asyncio
async def test_root_target_discovery_waits_for_auto_attach_while_session_commands_continue() -> None:
    scoped_tabs = [
        {"tabId": 41, "url": "https://one.example", "title": "One"},
        {"tabId": 42, "url": "https://two.example", "title": "Two"},
    ]
    relay = StubRelay(scoped_tabs)
    relay.main_frame_ids = {41: "frame-41", 42: "frame-42"}
    relay.block_attach_tab_id = 42
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    await adapter.handle_extension_event("extension.hello", {"scopedTabs": scoped_tabs})
    await adapter.start()
    try:
        async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
            await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
            await asyncio.wait_for(relay.attach_started.wait(), 1)

            attached_session_id = registry.root_session_id(41)
            await ws.send_json({"id": 2, "method": "Target.getTargets", "params": {}})
            await ws.send_json({"id": 3, "sessionId": attached_session_id, "method": "Runtime.enable", "params": {}})

            early_get_targets = None
            session_response = None
            while session_response is None:
                message = await ws.receive_json(timeout=1)
                if message.get("id") == 2:
                    early_get_targets = message
                elif message.get("id") == 3:
                    session_response = message
            assert early_get_targets is None
            assert session_response == {
                "id": 3,
                "sessionId": attached_session_id,
                "result": {"forwardedMethod": "Runtime.enable"},
            }

            relay.release_attach.set()
            assert await receive_response(ws, 1) == {"id": 1, "result": {}}
            targets = await receive_response(ws, 2)
    finally:
        relay.release_attach.set()
        await adapter.stop()

    assert {info["targetId"] for info in targets["result"]["targetInfos"]} == {"frame-41", "frame-42"}


@pytest.mark.asyncio
async def test_auto_attach_failure_reaches_client_instead_of_acknowledging_success() -> None:
    relay = StubRelay([{"tabId": 30, "url": "https://bad.example", "title": "Bad"}])
    relay.fail_attach_tab_ids.add(30)
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    adapter._send = AsyncMock()

    await adapter._handle_client_text(
        None,  # type: ignore[arg-type]
        json.dumps({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}}),
    )

    adapter._send.assert_awaited_once_with(
        None,
        {"id": 1, "error": {"code": -32000, "message": "ATTACH_FAILED: attach failed"}},
    )


@pytest.mark.asyncio
async def test_repeated_auto_attach_failure_restores_enabled_state_for_later_scoped_tabs() -> None:
    relay = StubRelay([{"tabId": 30, "url": "https://good.example", "title": "Good"}])
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    await adapter.start()
    try:
        async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
            await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
            assert await receive_response(ws, 1) == {"id": 1, "result": {}}
            await receive_event(ws, "Target.attachedToTarget")

            relay.scoped_tabs.append({"tabId": 31, "url": "https://bad.example", "title": "Bad"})
            relay.fail_attach_tab_ids.add(31)
            await ws.send_json({"id": 2, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
            assert await receive_response(ws, 2) == {
                "id": 2,
                "error": {"code": -32000, "message": "ATTACH_FAILED: attach failed"},
            }
            assert adapter._auto_attach is True

            relay.fail_attach_tab_ids.remove(31)
            await adapter.handle_extension_event(
                "scope.tabAdded", {"tabId": 32, "url": "https://later.example", "title": "Later"}
            )
            later = await receive_event(ws, "Target.attachedToTarget")
    finally:
        await adapter.stop()

    assert later["params"]["targetInfo"]["targetId"] == "tab-32"


@pytest.mark.asyncio
async def test_empty_scope_auto_attach_broker_failure_reaches_client() -> None:
    relay = StubRelay()
    relay.fail_next = BrowserExtensionBrokerError("BROKER_UNAVAILABLE", "broker unavailable")
    adapter = ExtensionCdpAdapter(VirtualTargetRegistry(), relay)
    adapter._send = AsyncMock()

    await adapter._handle_client_text(
        None,  # type: ignore[arg-type]
        json.dumps({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}}),
    )

    adapter._send.assert_awaited_once_with(
        None,
        {"id": 1, "error": {"code": -32000, "message": "BROKER_UNAVAILABLE: broker unavailable"}},
    )
    assert adapter._auto_attach is False


@pytest.mark.asyncio
async def test_auto_attach_replies_before_a_concurrent_scope_event() -> None:
    relay = StubRelay()
    relay.block_attach_tab_id = 100
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    original_request = relay.request
    order: list[str] = []

    async def request_with_scope_event(op: str, args: dict, timeout: float = 30.0) -> dict:
        result = await original_request(op, args, timeout)
        if op == "tabs.create":
            await adapter.handle_extension_event(
                "scope.tabAdded",
                {"tabId": result["tabId"], "url": args["url"], "title": ""},
            )
            asyncio.get_running_loop().call_soon(relay.release_attach.set)
        return result

    relay.request = request_with_scope_event
    adapter._reply = AsyncMock(side_effect=lambda *_args: order.append("reply"))
    adapter._emit_attached = AsyncMock(side_effect=lambda *_args: order.append("attached"))

    await adapter._handle_root_command(None, 1, "Target.setAutoAttach", {"autoAttach": True})  # type: ignore[arg-type]
    await asyncio.gather(*list(adapter._background_tasks))

    assert order == ["reply", "attached"]
    assert [call for call in relay.calls if call[0] == "debugger.attach"] == [
        ("debugger.attach", {"tabId": 100}),
    ]


@pytest.mark.asyncio
async def test_frame_discovery_timeout_does_not_register_a_phantom_attached_page() -> None:
    relay = StubRelay([{"tabId": 32, "url": "https://frame.example", "title": "Frame"}])
    relay.fail_send_keys[(None, "Page.getFrameTree")] = ExtensionRequestError(
        "COMMAND_TIMEOUT", "frame discovery timed out"
    )
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    generation = adapter._begin_tab_scope(32)

    with pytest.raises(ExtensionRequestError, match="frame discovery timed out"):
        await adapter._ensure_attached(relay.scoped_tabs[0], generation=generation)

    assert 32 not in adapter._attached_tabs
    with pytest.raises(KeyError):
        registry.target_id_for_tab(32)
    assert ("debugger.detach", {"tabId": 32}) in relay.calls


@pytest.mark.asyncio
async def test_frame_discovery_not_connected_discards_attachment_and_restores_auto_attach() -> None:
    relay = StubRelay([{"tabId": 32, "url": "https://frame.example", "title": "Frame"}])
    relay.fail_send_keys[(None, "Page.getFrameTree")] = BrowserExtensionNotConnectedError()
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)

    with pytest.raises(BrowserExtensionNotConnectedError):
        await adapter._set_auto_attach(None, 1, {"autoAttach": True}, None)  # type: ignore[arg-type]

    assert adapter._auto_attach is False
    assert 32 not in adapter._attached_tabs
    with pytest.raises(KeyError):
        registry.target_id_for_tab(32)
    assert ("debugger.detach", {"tabId": 32}) in relay.calls


@pytest.mark.asyncio
async def test_frame_discovery_cancellation_discards_attachment_and_restores_auto_attach() -> None:
    relay = StubRelay([{"tabId": 32, "url": "https://frame.example", "title": "Frame"}])
    relay.fail_send_keys[(None, "Page.getFrameTree")] = asyncio.CancelledError()
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)

    with pytest.raises(asyncio.CancelledError):
        await adapter._set_auto_attach(None, 1, {"autoAttach": True}, None)  # type: ignore[arg-type]

    assert adapter._auto_attach is False
    assert 32 not in adapter._attached_tabs
    with pytest.raises(KeyError):
        registry.target_id_for_tab(32)
    assert ("debugger.detach", {"tabId": 32}) in relay.calls


@pytest.mark.asyncio
async def test_frame_discovery_broker_failure_discards_attachment_and_restores_auto_attach() -> None:
    relay = StubRelay([{"tabId": 32, "url": "https://frame.example", "title": "Frame"}])
    relay.fail_send_keys[(None, "Page.getFrameTree")] = BrowserExtensionBrokerError(
        "EXTENSION_RESET_IN_PROGRESS", "extension reset in progress"
    )
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    adapter._send = AsyncMock()

    await adapter._handle_client_text(
        None,  # type: ignore[arg-type]
        json.dumps({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}}),
    )

    adapter._send.assert_awaited_once_with(
        None,
        {
            "id": 1,
            "error": {
                "code": -32000,
                "message": "EXTENSION_RESET_IN_PROGRESS: extension reset in progress",
            },
        },
    )
    assert adapter._auto_attach is False
    assert 32 not in adapter._attached_tabs
    with pytest.raises(KeyError):
        registry.target_id_for_tab(32)
    assert ("debugger.detach", {"tabId": 32}) in relay.calls


@pytest.mark.asyncio
async def test_scope_revoked_during_frame_discovery_discards_the_attachment() -> None:
    relay = StubRelay([{"tabId": 33, "url": "https://racy.example", "title": "Racy"}])
    frame_key = (None, "Page.getFrameTree")
    relay.block_send_keys.add(frame_key)
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    generation = adapter._begin_tab_scope(33)

    attaching = asyncio.create_task(adapter._ensure_attached(relay.scoped_tabs[0], generation=generation))
    await relay.send_started.setdefault(frame_key, asyncio.Event()).wait()
    adapter._revoke_tab_scope(33)
    relay.release_send.setdefault(frame_key, asyncio.Event()).set()

    assert await attaching is None
    assert 33 not in adapter._attached_tabs
    assert ("debugger.detach", {"tabId": 33}) in relay.calls


@pytest.mark.asyncio
async def test_auto_attach_scope_revocation_cancellation_rolls_back_prior_tabs() -> None:
    relay = StubRelay(
        [
            {"tabId": 33, "url": "https://first.example", "title": "First"},
            {"tabId": 34, "url": "https://revoked.example", "title": "Revoked"},
        ]
    )
    relay.block_detach_tab_id = 34
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    original_request = relay.request
    revoked_detach_finished = asyncio.Event()

    async def request_with_scope_revocation(op: str, args: dict, timeout: float = 30.0) -> dict:
        result = await original_request(op, args, timeout)
        if op == "debugger.send" and args["tabId"] == 34 and args["method"] == "Page.getFrameTree":
            adapter._revoke_tab_scope(34)
        if op == "debugger.detach" and args["tabId"] == 34:
            revoked_detach_finished.set()
        return result

    relay.request = request_with_scope_revocation
    attaching = asyncio.create_task(
        adapter._set_auto_attach(None, 1, {"autoAttach": True}, None)  # type: ignore[arg-type]
    )
    await relay.detach_started.wait()

    attaching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attaching

    relay.release_detach.set()
    await asyncio.wait_for(revoked_detach_finished.wait(), timeout=1)

    assert adapter._auto_attach is False
    assert adapter._attached_tabs == set()
    assert ("debugger.detach", {"tabId": 33}) in relay.calls
    with pytest.raises(KeyError):
        registry.target_id_for_tab(33)
    with pytest.raises(KeyError):
        registry.target_id_for_tab(34)


@pytest.mark.asyncio
async def test_auto_attach_transactional_failure_restores_state_without_second_response() -> None:
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

            assert await receive_response(ws, 1) == {
                "id": 1,
                "error": {"code": -32000, "message": "ATTACH_FAILED: attach failed"},
            }
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
            "params": {
                "flatten": True,
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "filter": [{"type": "iframe", "exclude": False}],
            },
        },
    )
    assert relay.calls[1][1]["sessionId"] == "child-12"
    assert detached["params"] == {"sessionId": "child-12"}
    with pytest.raises(KeyError):
        registry.resolve_session("child-12")


@pytest.mark.asyncio
async def test_child_detach_during_post_registration_window_emits_and_unregisters(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, _, registry = adapter_server
    registry.register_tab(17, "https://example.com", "Example")
    child_session_id = "child-17"
    replay_started = asyncio.Event()
    release_replay = asyncio.Event()

    async def block_replay(session_id: str) -> None:
        assert session_id == child_session_id
        replay_started.set()
        await release_replay.wait()

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
        await receive_response(ws, 1)
        await receive_event(ws, "Target.attachedToTarget")

        with patch.object(adapter, "_replay_buffered_child_events", side_effect=block_replay):
            await adapter.handle_extension_event(
                "debugger.event",
                {
                    "tabId": 17,
                    "method": "Target.attachedToTarget",
                    "params": {
                        "sessionId": child_session_id,
                        "targetInfo": {
                            "targetId": "frame-17",
                            "type": "iframe",
                            "title": "",
                            "url": "https://example.com/frame",
                            "attached": True,
                            "canAccessOpener": False,
                            "browserContextId": "skyvern-default",
                        },
                        "waitingForDebugger": False,
                    },
                },
            )
            await receive_event(ws, "Target.attachedToTarget")
            await asyncio.wait_for(replay_started.wait(), 1)
            assert child_session_id in adapter._pending_child_sessions
            assert registry.resolve_session(child_session_id) == (17, child_session_id)

            await adapter.handle_extension_event(
                "debugger.event",
                {
                    "tabId": 17,
                    "method": "Target.detachedFromTarget",
                    "params": {"sessionId": child_session_id},
                },
            )
            detached = await receive_event(ws, "Target.detachedFromTarget")

            await ws.send_json({"id": 2, "sessionId": child_session_id, "method": "Runtime.enable", "params": {}})
            missing = await receive_response(ws, 2)
            release_replay.set()

    assert detached["params"] == {"sessionId": child_session_id}
    assert missing == {
        "id": 2,
        "sessionId": child_session_id,
        "error": {"code": -32001, "message": "session not found"},
    }


@pytest.mark.asyncio
async def test_child_detach_before_probe_completes_is_swallowed_and_tombstones_initialization(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(18, "https://example.com", "Example")
    child_session_id = "child-18"
    probe_key = (child_session_id, "Target.setAutoAttach")
    relay.block_send_keys.add(probe_key)

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
        await receive_response(ws, 1)
        await receive_event(ws, "Target.attachedToTarget")

        await adapter.handle_extension_event(
            "debugger.event",
            {
                "tabId": 18,
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": child_session_id,
                    "targetInfo": {
                        "targetId": "frame-18",
                        "type": "iframe",
                        "title": "",
                        "url": "https://example.com/frame",
                        "attached": True,
                        "canAccessOpener": False,
                        "browserContextId": "skyvern-default",
                    },
                    "waitingForDebugger": False,
                },
            },
        )
        await asyncio.wait_for(relay.send_started.setdefault(probe_key, asyncio.Event()).wait(), 1)
        initialization_task = next(task for task in adapter._background_tasks if not task.done())

        await adapter.handle_extension_event(
            "debugger.event",
            {
                "tabId": 18,
                "method": "Target.detachedFromTarget",
                "params": {"sessionId": child_session_id},
            },
        )
        with pytest.raises(TimeoutError):
            await ws.receive_json(timeout=0.05)

        relay.release_send[probe_key].set()
        await asyncio.wait_for(initialization_task, 1)

    with pytest.raises(KeyError):
        registry.resolve_session(child_session_id)


@pytest.mark.asyncio
async def test_nested_child_attach_during_parent_probe_is_replayed_in_parent_first_order(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(13, "https://parent.example", "Parent")
    root_session_id = registry.root_session_id(13)
    parent_session_id = "parent-child-13"
    grandchild_session_id = "grandchild-13"
    parent_probe_key = (parent_session_id, "Target.setAutoAttach")
    relay.block_send_keys.add(parent_probe_key)

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
        await receive_response(ws, 1)
        await receive_event(ws, "Target.attachedToTarget")
        relay.calls.clear()

        parent_params = {
            "sessionId": parent_session_id,
            "targetInfo": {
                "targetId": "parent-frame-13",
                "type": "iframe",
                "title": "",
                "url": "https://parent.example/frame",
                "attached": True,
                "canAccessOpener": False,
                "browserContextId": "skyvern-default",
            },
            "waitingForDebugger": False,
        }
        await adapter.handle_extension_event(
            "debugger.event",
            {"tabId": 13, "method": "Target.attachedToTarget", "params": parent_params},
        )
        await asyncio.wait_for(relay.send_started.setdefault(parent_probe_key, asyncio.Event()).wait(), 1)

        grandchild_params = {
            "sessionId": grandchild_session_id,
            "targetInfo": {
                "targetId": "grandchild-frame-13",
                "type": "iframe",
                "title": "",
                "url": "https://grandchild.example/frame",
                "attached": True,
                "canAccessOpener": False,
                "browserContextId": "skyvern-default",
            },
            "waitingForDebugger": False,
        }
        await adapter.handle_extension_event(
            "debugger.event",
            {
                "tabId": 13,
                "sessionId": parent_session_id,
                "method": "Target.attachedToTarget",
                "params": grandchild_params,
            },
        )

        relay.release_send[parent_probe_key].set()
        parent_attached = await receive_event(ws, "Target.attachedToTarget")
        grandchild_attached = await receive_event(ws, "Target.attachedToTarget")
        assert registry.resolve_session(grandchild_session_id) == (13, grandchild_session_id)

    assert parent_attached["sessionId"] == root_session_id
    assert parent_attached["params"] == parent_params
    assert grandchild_attached["sessionId"] == parent_session_id
    assert grandchild_attached["params"] == grandchild_params


@pytest.mark.asyncio
async def test_nested_child_attach_is_detached_when_parent_probe_fails(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(15, "https://parent.example", "Parent")
    parent_session_id = "parent-child-15"
    grandchild_session_id = "grandchild-15"
    parent_probe_key = (parent_session_id, "Target.setAutoAttach")
    relay.block_send_keys.add(parent_probe_key)
    relay.fail_send_keys[parent_probe_key] = ExtensionRequestError("INTERNAL", "parent probe failed")

    async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
        await receive_response(ws, 1)
        await receive_event(ws, "Target.attachedToTarget")
        relay.calls.clear()

        await adapter.handle_extension_event(
            "debugger.event",
            {
                "tabId": 15,
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": parent_session_id,
                    "targetInfo": {
                        "targetId": "parent-frame-15",
                        "type": "iframe",
                        "title": "",
                        "url": "https://parent.example/frame",
                        "attached": True,
                        "canAccessOpener": False,
                        "browserContextId": "skyvern-default",
                    },
                    "waitingForDebugger": False,
                },
            },
        )
        await asyncio.wait_for(relay.send_started.setdefault(parent_probe_key, asyncio.Event()).wait(), 1)

        await adapter.handle_extension_event(
            "debugger.event",
            {
                "tabId": 15,
                "sessionId": parent_session_id,
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": grandchild_session_id,
                    "targetInfo": {
                        "targetId": "grandchild-frame-15",
                        "type": "iframe",
                        "title": "",
                        "url": "https://grandchild.example/frame",
                        "attached": True,
                        "canAccessOpener": False,
                        "browserContextId": "skyvern-default",
                    },
                    "waitingForDebugger": False,
                },
            },
        )

        relay.release_send[parent_probe_key].set()
        with pytest.raises(TimeoutError):
            await ws.receive_json(timeout=0.05)
        with pytest.raises(KeyError):
            registry.resolve_session(parent_session_id)
        with pytest.raises(KeyError):
            registry.resolve_session(grandchild_session_id)

    assert (
        "debugger.send",
        {
            "tabId": 15,
            "sessionId": grandchild_session_id,
            "method": "Runtime.runIfWaitingForDebugger",
            "params": {},
        },
    ) in relay.calls
    assert (
        "debugger.send",
        {
            "tabId": 15,
            "method": "Target.detachFromTarget",
            "params": {"sessionId": grandchild_session_id},
        },
    ) in relay.calls


@pytest.mark.asyncio
async def test_pending_child_detach_does_not_wait_for_buffered_grandchild_cleanup(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(19, "https://parent.example", "Parent")
    parent_session_id = "parent-child-19"
    grandchild_session_id = "grandchild-19"
    resume_key = (grandchild_session_id, "Runtime.runIfWaitingForDebugger")
    relay.block_send_keys.add(resume_key)
    adapter._pending_child_sessions.add(parent_session_id)
    adapter._pending_child_events[parent_session_id] = [
        {
            "tabId": 19,
            "sessionId": parent_session_id,
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": grandchild_session_id,
                "targetInfo": {
                    "targetId": "grandchild-frame-19",
                    "type": "iframe",
                    "title": "",
                    "url": "https://grandchild.example/frame",
                    "attached": True,
                    "canAccessOpener": False,
                    "browserContextId": "skyvern-default",
                },
                "waitingForDebugger": False,
            },
        }
    ]

    await asyncio.wait_for(
        adapter.handle_extension_event(
            "debugger.event",
            {
                "tabId": 19,
                "method": "Target.detachedFromTarget",
                "params": {"sessionId": parent_session_id},
            },
        ),
        0.5,
    )
    await asyncio.wait_for(relay.send_started.setdefault(resume_key, asyncio.Event()).wait(), 0.5)
    relay.release_send.setdefault(resume_key, asyncio.Event()).set()

    async def grandchild_detached() -> None:
        expected = (
            "debugger.send",
            {
                "tabId": 19,
                "method": "Target.detachFromTarget",
                "params": {"sessionId": grandchild_session_id},
            },
        )
        while expected not in relay.calls:
            await asyncio.sleep(0)

    await asyncio.wait_for(grandchild_detached(), 0.5)
    assert parent_session_id not in adapter._pending_child_sessions
    assert parent_session_id not in adapter._pending_child_events


@pytest.mark.asyncio
async def test_child_initialization_keeps_live_outer_alias_when_another_alias_is_dead(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, _, registry = adapter_server
    registry.register_tab(20, "https://parent.example", "Parent")
    adapter._auto_attach = True
    dead_alias = registry.create_root_session_alias(20)
    live_alias = registry.create_root_session_alias(20)
    assert registry.remove_root_session_alias(dead_alias)
    child_session_id = "child-20"
    target_info = {
        "targetId": "frame-20",
        "type": "iframe",
        "title": "",
        "url": "https://child.example/frame",
        "attached": True,
        "canAccessOpener": False,
        "browserContextId": "skyvern-default",
    }
    event_params = {
        "sessionId": child_session_id,
        "targetInfo": target_info,
        "waitingForDebugger": False,
    }
    adapter._pending_child_sessions.add(child_session_id)

    with patch.object(adapter, "_emit_to_sessions", wraps=adapter._emit_to_sessions) as emit_to_sessions:
        await adapter._initialize_child_target(
            20,
            child_session_id,
            target_info,
            event_params,
            [dead_alias, live_alias],
        )

    assert registry.resolve_session(child_session_id) == (20, child_session_id)
    emit_to_sessions.assert_awaited_once_with("Target.attachedToTarget", event_params, [live_alias])


@pytest.mark.asyncio
async def test_child_initialization_resumes_and_detaches_when_all_outer_aliases_are_dead(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(21, "https://parent.example", "Parent")
    adapter._auto_attach = True
    outer_session_ids = registry.root_session_ids(21)
    registry.remove_tab(21)
    child_session_id = "child-21"
    target_info = {
        "targetId": "frame-21",
        "type": "iframe",
        "title": "",
        "url": "https://child.example/frame",
        "attached": True,
        "canAccessOpener": False,
        "browserContextId": "skyvern-default",
    }
    adapter._pending_child_sessions.add(child_session_id)

    await adapter._initialize_child_target(
        21,
        child_session_id,
        target_info,
        {"sessionId": child_session_id, "targetInfo": target_info, "waitingForDebugger": False},
        outer_session_ids,
    )

    assert (
        "debugger.send",
        {
            "tabId": 21,
            "sessionId": child_session_id,
            "method": "Runtime.runIfWaitingForDebugger",
            "params": {},
        },
    ) in relay.calls
    assert (
        "debugger.send",
        {
            "tabId": 21,
            "method": "Target.detachFromTarget",
            "params": {"sessionId": child_session_id},
        },
    ) in relay.calls
    assert child_session_id not in adapter._pending_child_sessions
    with pytest.raises(KeyError):
        registry.resolve_session(child_session_id)


@pytest.mark.asyncio
async def test_child_auto_attach_failure_skips_session_and_navigation_lifecycle_continues() -> None:
    relay = StubRelay([{"tabId": 14, "url": "https://page.example", "title": "Page"}])
    registry = VirtualTargetRegistry()
    adapter = ExtensionCdpAdapter(registry, relay)
    await adapter.start()
    try:
        async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
            await ws.send_json({"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}})
            await receive_response(ws, 1)
            await receive_event(ws, "Target.attachedToTarget")
            relay.calls.clear()
            relay.fail_send_keys[("unsupported-child", "Target.setAutoAttach")] = ExtensionRequestError(
                "INTERNAL", "child target does not support auto-attach"
            )

            await adapter.handle_extension_event(
                "debugger.event",
                {
                    "tabId": 14,
                    "method": "Target.attachedToTarget",
                    "params": {
                        "sessionId": "unsupported-child",
                        "targetInfo": {
                            "targetId": "unsupported-target",
                            "type": "iframe",
                            "title": "",
                            "url": "https://frame.example",
                            "attached": True,
                            "canAccessOpener": False,
                            "browserContextId": "skyvern-default",
                        },
                        "waitingForDebugger": False,
                    },
                },
            )
            await asyncio.sleep(0)

            with pytest.raises(KeyError):
                registry.resolve_session("unsupported-child")
            with pytest.raises(TimeoutError):
                await ws.receive_json(timeout=0.05)

            root_session_id = registry.root_session_id(14)
            await ws.send_json(
                {
                    "id": 2,
                    "sessionId": root_session_id,
                    "method": "Page.navigate",
                    "params": {"url": "https://destination.example"},
                }
            )
            assert (await receive_response(ws, 2))["result"] == {"forwardedMethod": "Page.navigate"}

            lifecycle_params = {"name": "DOMContentLoaded", "frameId": "main"}
            await adapter.handle_extension_event(
                "debugger.event",
                {
                    "tabId": 14,
                    "method": "Page.lifecycleEvent",
                    "params": lifecycle_params,
                },
            )
            lifecycle = await receive_event(ws, "Page.lifecycleEvent")
            assert lifecycle["sessionId"] == root_session_id
            assert lifecycle["params"] == lifecycle_params
    finally:
        await adapter.stop()

    assert (
        "debugger.send",
        {
            "tabId": 14,
            "method": "Target.detachFromTarget",
            "params": {"sessionId": "unsupported-child"},
        },
    ) in relay.calls


@pytest.mark.asyncio
async def test_slow_session_command_does_not_block_independent_session_command() -> None:
    relay = StubRelay()
    registry = VirtualTargetRegistry()
    registry.register_tab(16, "https://page.example", "Page")
    session_id = registry.root_session_id(16)
    navigate_key = (None, "Page.navigate")
    relay.block_send_keys.add(navigate_key)
    adapter = ExtensionCdpAdapter(registry, relay)
    await adapter.start()
    try:
        async with ClientSession() as client, client.ws_connect(adapter.cdp_ws_url) as ws:
            await ws.send_json(
                {
                    "id": 1,
                    "sessionId": session_id,
                    "method": "Page.navigate",
                    "params": {"url": "https://destination.example"},
                }
            )
            await asyncio.wait_for(relay.send_started.setdefault(navigate_key, asyncio.Event()).wait(), 1)

            await ws.send_json(
                {
                    "id": 2,
                    "sessionId": session_id,
                    "method": "Runtime.runIfWaitingForDebugger",
                    "params": {},
                }
            )
            fast_response = await receive_response(ws, 2)
            assert fast_response["result"] == {"forwardedMethod": "Runtime.runIfWaitingForDebugger"}

            relay.release_send.setdefault(navigate_key, asyncio.Event()).set()
            navigate_response = await receive_response(ws, 1)
            assert navigate_response["result"] == {"forwardedMethod": "Page.navigate"}
    finally:
        relay.release_send.setdefault(navigate_key, asyncio.Event()).set()
        await adapter.stop()


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
async def test_old_client_disconnect_does_not_cancel_new_client_command(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    old_cancel_started = asyncio.Event()
    release_old_cancel = asyncio.Event()
    original_cancel_client_tasks = adapter._cancel_client_tasks

    async def block_old_cancel(*args: object) -> None:
        old_cancel_started.set()
        await release_old_cancel.wait()
        await original_cancel_client_tasks(*args)

    async with ClientSession() as client:
        await client.ws_connect(adapter.cdp_ws_url)
        with patch.object(adapter, "_cancel_client_tasks", side_effect=block_old_cancel):
            disconnect_task = asyncio.create_task(adapter.on_extension_disconnect())
            await asyncio.wait_for(old_cancel_started.wait(), 1)

            new = await client.ws_connect(adapter.cdp_ws_url)
            registry.register_tab(24, "https://example.com", "Example")
            session_id = registry.root_session_id(24)
            send_key = (None, "Runtime.evaluate")
            relay.block_send_keys.add(send_key)
            await new.send_json(
                {"id": 1, "sessionId": session_id, "method": "Runtime.evaluate", "params": {"expression": "1+1"}}
            )
            await asyncio.wait_for(relay.send_started.setdefault(send_key, asyncio.Event()).wait(), 1)

            release_old_cancel.set()
            await asyncio.wait_for(disconnect_task, 2)
            relay.release_send[send_key].set()

            assert await receive_response(new, 1) == {
                "id": 1,
                "sessionId": session_id,
                "result": {"forwardedMethod": "Runtime.evaluate"},
            }
            await asyncio.sleep(0)
            assert adapter._client_tasks == {}
            await new.close()


@pytest.mark.asyncio
async def test_browser_close_detaches_all_tabs_when_client_closes_after_reply(
    adapter_server: tuple[ExtensionCdpAdapter, StubRelay, VirtualTargetRegistry],
) -> None:
    adapter, relay, registry = adapter_server
    registry.register_tab(22, "https://example.com", "Example")
    registry.register_tab(23, "https://other.example", "Other")
    relay.block_detach_tab_id = 22

    async with ClientSession() as client:
        ws = await client.ws_connect(adapter.cdp_ws_url)
        await ws.send_json({"id": 9, "method": "Browser.close", "params": {}})
        assert await receive_response(ws, 9) == {"id": 9, "result": {}}
        close_task = asyncio.create_task(ws.close())
        await asyncio.wait_for(relay.detach_started.wait(), 1)
        await asyncio.sleep(0.05)
        relay.release_detach.set()
        await asyncio.wait_for(close_task, 2)

    assert {args["tabId"] for op, args in relay.calls if op == "debugger.detach"} == {22, 23}
    assert all(op != "tabs.remove" for op, _ in relay.calls)
    assert set(relay.released_tabs) == {22, 23}
