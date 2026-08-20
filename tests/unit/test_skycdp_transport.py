"""Transport-level contract for the raw-CDP client.

The transport owns exactly one websocket to a browser-level DevTools endpoint and multiplexes every
attached target over it (flat mode). It is the only layer that knows about message ids, session
routing, and socket death; everything above it sees awaited results and typed errors.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from skyvern.webeye.skycdp.errors import CdpProtocolError, CdpTargetClosedError, CdpTimeoutError
from skyvern.webeye.skycdp.transport import CdpTransport

pytestmark = pytest.mark.asyncio


class FakeSocket:
    """An in-memory stand-in for a websockets client connection."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, payload: str) -> None:
        if self.closed:
            raise ConnectionError("socket closed")
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        message = await self._inbox.get()
        if message is None:
            raise ConnectionError("socket closed")
        return message

    async def close(self) -> None:
        self.closed = True
        await self._inbox.put(None)

    def push(self, message: dict) -> None:
        self._inbox.put_nowait(json.dumps(message))

    def drop(self) -> None:
        self.closed = True
        self._inbox.put_nowait(None)

    async def wait_for_sent(self, count: int, timeout: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.sent) < count:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"expected {count} sent frames, saw {len(self.sent)}")
            await asyncio.sleep(0)


async def _started_transport() -> tuple[CdpTransport, FakeSocket]:
    socket = FakeSocket()
    transport = CdpTransport(socket)
    await transport.start()
    return transport, socket


async def test_send_assigns_monotonic_ids_and_resolves_matching_result() -> None:
    transport, socket = await _started_transport()
    try:
        pending = asyncio.ensure_future(transport.send("Page.navigate", {"url": "https://example.com"}))
        await socket.wait_for_sent(1)

        frame = socket.sent[0]
        assert frame["method"] == "Page.navigate"
        assert frame["params"] == {"url": "https://example.com"}
        assert isinstance(frame["id"], int)

        socket.push({"id": frame["id"], "result": {"frameId": "F1"}})
        assert await asyncio.wait_for(pending, timeout=1) == {"frameId": "F1"}
    finally:
        await transport.close()


async def test_concurrent_sends_resolve_independently_and_out_of_order() -> None:
    transport, socket = await _started_transport()
    try:
        first = asyncio.ensure_future(transport.send("Runtime.evaluate", {"expression": "1"}))
        second = asyncio.ensure_future(transport.send("Runtime.evaluate", {"expression": "2"}))
        await socket.wait_for_sent(2)

        first_id, second_id = socket.sent[0]["id"], socket.sent[1]["id"]
        assert first_id != second_id

        socket.push({"id": second_id, "result": {"value": 2}})
        socket.push({"id": first_id, "result": {"value": 1}})

        assert await asyncio.wait_for(second, timeout=1) == {"value": 2}
        assert await asyncio.wait_for(first, timeout=1) == {"value": 1}
    finally:
        await transport.close()


async def test_session_id_is_carried_on_the_wire_and_scopes_nothing_else() -> None:
    transport, socket = await _started_transport()
    try:
        pending = asyncio.ensure_future(transport.send("DOM.getDocument", session_id="S1"))
        await socket.wait_for_sent(1)

        assert socket.sent[0]["sessionId"] == "S1"
        socket.push({"id": socket.sent[0]["id"], "sessionId": "S1", "result": {"root": {}}})
        assert await asyncio.wait_for(pending, timeout=1) == {"root": {}}
    finally:
        await transport.close()


async def test_browser_level_send_omits_session_id_key() -> None:
    transport, socket = await _started_transport()
    try:
        pending = asyncio.ensure_future(transport.send("Target.getTargets"))
        await socket.wait_for_sent(1)

        assert "sessionId" not in socket.sent[0]
        socket.push({"id": socket.sent[0]["id"], "result": {"targetInfos": []}})
        await asyncio.wait_for(pending, timeout=1)
    finally:
        await transport.close()


async def test_protocol_error_raises_with_code_and_message() -> None:
    transport, socket = await _started_transport()
    try:
        pending = asyncio.ensure_future(transport.send("DOM.focus", {"nodeId": 7}))
        await socket.wait_for_sent(1)
        socket.push(
            {
                "id": socket.sent[0]["id"],
                "error": {"code": -32000, "message": "Element is not focusable"},
            }
        )

        with pytest.raises(CdpProtocolError) as excinfo:
            await asyncio.wait_for(pending, timeout=1)
        assert "Element is not focusable" in str(excinfo.value)
        assert excinfo.value.code == -32000
        assert excinfo.value.method == "DOM.focus"
    finally:
        await transport.close()


async def test_target_closed_protocol_errors_are_raised_as_target_closed() -> None:
    transport, socket = await _started_transport()
    try:
        pending = asyncio.ensure_future(transport.send("Runtime.evaluate", session_id="S1"))
        await socket.wait_for_sent(1)
        socket.push(
            {
                "id": socket.sent[0]["id"],
                "error": {"code": -32001, "message": "Session with given id not found."},
            }
        )

        with pytest.raises(CdpTargetClosedError):
            await asyncio.wait_for(pending, timeout=1)
    finally:
        await transport.close()


async def test_events_dispatch_to_subscribers_scoped_by_session() -> None:
    transport, socket = await _started_transport()
    try:
        browser_events: list[dict] = []
        session_events: list[dict] = []
        transport.on("Target.targetCreated", browser_events.append)
        transport.on("Page.loadEventFired", session_events.append, session_id="S1")

        socket.push({"method": "Target.targetCreated", "params": {"targetInfo": {"targetId": "T1"}}})
        socket.push({"method": "Page.loadEventFired", "sessionId": "S1", "params": {"timestamp": 1}})
        socket.push({"method": "Page.loadEventFired", "sessionId": "S2", "params": {"timestamp": 2}})
        await asyncio.sleep(0.05)

        assert browser_events == [{"targetInfo": {"targetId": "T1"}}]
        assert session_events == [{"timestamp": 1}]
    finally:
        await transport.close()


async def test_off_removes_a_subscriber() -> None:
    transport, socket = await _started_transport()
    try:
        seen: list[dict] = []
        transport.on("Target.targetCreated", seen.append)
        transport.off("Target.targetCreated", seen.append)

        socket.push({"method": "Target.targetCreated", "params": {}})
        await asyncio.sleep(0.05)
        assert seen == []
    finally:
        await transport.close()


async def test_a_raising_event_handler_never_kills_the_read_loop() -> None:
    transport, socket = await _started_transport()
    try:
        survivors: list[dict] = []

        def explode(_: dict) -> None:
            raise RuntimeError("handler bug")

        transport.on("Target.targetCreated", explode)
        transport.on("Target.targetCreated", survivors.append)

        socket.push({"method": "Target.targetCreated", "params": {"n": 1}})
        socket.push({"method": "Target.targetCreated", "params": {"n": 2}})
        await asyncio.sleep(0.05)

        assert survivors == [{"n": 1}, {"n": 2}]
        assert not transport.is_closed
    finally:
        await transport.close()


async def test_socket_death_fails_every_inflight_request_with_target_closed() -> None:
    transport, socket = await _started_transport()
    try:
        first = asyncio.ensure_future(transport.send("Page.navigate"))
        second = asyncio.ensure_future(transport.send("Page.reload"))
        await socket.wait_for_sent(2)

        socket.drop()

        for pending in (first, second):
            with pytest.raises(CdpTargetClosedError):
                await asyncio.wait_for(pending, timeout=1)
        assert transport.is_closed
    finally:
        await transport.close()


async def test_send_after_close_fails_fast_rather_than_hanging() -> None:
    transport, socket = await _started_transport()
    await transport.close()

    with pytest.raises(CdpTargetClosedError):
        await asyncio.wait_for(transport.send("Page.navigate"), timeout=1)
    assert socket.closed


async def test_send_times_out_and_stops_tracking_the_request() -> None:
    transport, socket = await _started_transport()
    try:
        with pytest.raises(CdpTimeoutError):
            await transport.send("Page.navigate", timeout=0.05)
        assert transport.inflight_count == 0
    finally:
        await transport.close()


async def test_close_is_idempotent() -> None:
    transport, _ = await _started_transport()
    await transport.close()
    await transport.close()
    assert transport.is_closed


async def test_on_disconnect_callbacks_fire_exactly_once() -> None:
    transport, socket = await _started_transport()
    calls: list[int] = []
    transport.on_disconnect(lambda: calls.append(1))

    socket.drop()
    await asyncio.sleep(0.05)
    await transport.close()

    assert calls == [1]
