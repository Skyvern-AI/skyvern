"""Slow-client backpressure tests for the client-facing CDP proxy.

The client-bound relay uses a bounded outbound queue drained by a dedicated
writer, so a slow client never backs up the upstream browser (the runner-CPU
goal). Droppable events are tail-dropped under overflow; a client that stays
overflowed — or cannot even take a command response — is closed with WS 1013.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Mapping

import pytest

from skyvern.proxy.adapters.memory import AllowAllAuth, ForwardAllEventPolicy, InMemorySessionRegistry
from skyvern.proxy.adapters.websocket_server import (
    _BACKPRESSURE_CLOSE_CODE,
    CdpProxyServer,
    _ClientChannel,
    _OutboundQueue,
    _SharedUpstream,
)
from skyvern.proxy.core.frames import CdpCommand, CdpEvent, CdpResponse, RequestIdRemapper, decode_frame, encode_frame
from skyvern.proxy.core.session import Principal, ProxySession, ResolvedSession, UpstreamClosedError

FRAMES_DROPPED = "skyvern.cdp_proxy.frames_dropped"
BACKPRESSURE_CLOSED = "skyvern.cdp_proxy.client_backpressure_closed"


class _RecordingMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def increment(self, name: str, amount: int = 1, tags: Mapping[str, str] | None = None) -> None:
        self.calls.append(("increment", name, dict(tags or {})))

    def observe(self, name: str, value: float, tags: Mapping[str, str] | None = None) -> None:
        self.calls.append(("observe", name, dict(tags or {})))

    def gauge(self, name: str, amount: int, tags: Mapping[str, str] | None = None) -> None:
        self.calls.append(("gauge", name, dict(tags or {})))

    def count(self, name: str, reason: str | None = None) -> int:
        return sum(1 for _op, n, tags in self.calls if n == name and (reason is None or tags.get("reason") == reason))


class _BlockingClient:
    """A client whose first send() blocks until the connection is closed, so the
    writer stalls and the outbound queue fills — a stuck slow client. Any `incoming`
    frames are yielded first, for tests that need it to speak before it stalls."""

    def __init__(self, incoming: list[str] | None = None) -> None:
        self.request = SimpleNamespace(headers={}, path="/s1")
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._incoming = list(incoming or [])
        self._closed = asyncio.Event()

    def __aiter__(self) -> _BlockingClient:
        return self

    async def __anext__(self) -> str:
        if self._incoming:
            return self._incoming.pop(0)
        await self._closed.wait()
        raise StopAsyncIteration

    async def send(self, raw: str) -> None:
        self.sent.append(raw)
        await self._closed.wait()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        self._closed.set()


class _FloodingUpstream:
    def __init__(self, count: int) -> None:
        self._remaining = count

    async def send(self, raw: str) -> None:
        return None

    async def receive(self) -> str:
        if self._remaining <= 0:
            raise UpstreamClosedError("drained")
        self._remaining -= 1
        return encode_frame(CdpEvent(method="Network.dataReceived", params={"n": self._remaining}))

    async def close(self) -> None:
        return None


class _ScriptedUpstream:
    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)

    async def send(self, raw: str) -> None:
        return None

    async def receive(self) -> str:
        if not self._frames:
            raise UpstreamClosedError("drained")
        return self._frames.pop(0)

    async def close(self) -> None:
        return None


class _StaticUpstreamBrowser:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def connect(self, session: ProxySession) -> object:
        return self._connection


class _NoCloseWs:
    """Never sends; used to assert the relay itself initiates a clean close."""

    def __init__(self) -> None:
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send(self, raw: str) -> None:
        return None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason


def _session() -> ProxySession:
    return ProxySession(
        session_id="s1", upstream_ws_url="ws://x/y", principal=Principal(principal_id="p", organization_id="o1")
    )


def _server(
    metrics: object, connection: object, maxsize: int, close_after: int, maxbytes: int = 1_000_000
) -> CdpProxyServer:
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url="ws://x/y"))
    return CdpProxyServer(
        upstream=_StaticUpstreamBrowser(connection),  # type: ignore[arg-type]
        sessions=sessions,
        auth=AllowAllAuth(),
        metrics=metrics,  # type: ignore[arg-type]
        event_policy=ForwardAllEventPolicy(),
        client_queue_maxsize=maxsize,
        client_backpressure_close_after=close_after,
        client_queue_maxbytes=maxbytes,
    )


class _SlowSendClient:
    """Sends with a small delay, so a queued upstream response is still in flight
    when the upstream completes — exercising the graceful drain."""

    def __init__(self, incoming: list[str]) -> None:
        self.request = SimpleNamespace(headers={}, path="/s1")
        self.sent: list[str] = []
        self.close_code: int | None = None
        self._incoming = list(incoming)
        self._closed = asyncio.Event()

    def __aiter__(self) -> _SlowSendClient:
        return self

    async def __anext__(self) -> str:
        if self._incoming:
            return self._incoming.pop(0)
        await self._closed.wait()
        raise StopAsyncIteration

    async def send(self, raw: str) -> None:
        await asyncio.sleep(0.02)
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self._closed.set()


class _EchoThenCloseUpstream:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False

    async def send(self, raw: str) -> None:
        frame = decode_frame(raw)
        self._queue.put_nowait(encode_frame(CdpResponse(id=frame.id, result={"ok": True})))
        self._queue.put_nowait(None)

    async def receive(self) -> str:
        if self._closed and self._queue.empty():
            raise UpstreamClosedError("closed")
        raw = await self._queue.get()
        if raw is None:
            raise UpstreamClosedError("closed")
        return raw

    async def close(self) -> None:
        self._closed = True
        self._queue.put_nowait(None)


class _BlockingUpstream:
    def __init__(self) -> None:
        self._closed = asyncio.Event()

    async def send(self, raw: str) -> None:
        return None

    async def receive(self) -> str:
        await self._closed.wait()
        raise UpstreamClosedError("closed")

    async def close(self) -> None:
        self._closed.set()


class _FloodingLargeUpstream:
    def __init__(self, count: int, frame_bytes: int) -> None:
        self._remaining = count
        self._payload = "x" * frame_bytes

    async def send(self, raw: str) -> None:
        return None

    async def receive(self) -> str:
        if self._remaining <= 0:
            raise UpstreamClosedError("drained")
        self._remaining -= 1
        return encode_frame(CdpEvent(method="Network.dataReceived", params={"body": self._payload}))

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_final_upstream_response_is_delivered_before_teardown() -> None:
    # On clean upstream completion the queued command response must reach the client
    # before teardown, not be silently lost when the writer is cancelled.
    client = _SlowSendClient([encode_frame(CdpCommand(id=1, method="Page.enable"))])
    server = _server(_RecordingMetrics(), _EchoThenCloseUpstream(), maxsize=64, close_after=64)

    await asyncio.wait_for(server._handle_client(client), timeout=5)  # type: ignore[arg-type]

    assert any('"id":1' in frame for frame in client.sent)  # the response was delivered
    assert client.close_code != _BACKPRESSURE_CLOSE_CODE  # drained in time, not force-closed


@pytest.mark.asyncio
async def test_cancelling_handle_client_reaps_all_children_cleanly() -> None:
    metrics = _RecordingMetrics()
    client = _BlockingClient()
    server = _server(metrics, _BlockingUpstream(), maxsize=8, close_after=8)

    before = set(asyncio.all_tasks())
    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    for _ in range(500):  # wait until the session is live (the +1 gauge fired)
        if any(name.endswith("active_sessions") for _op, name, _tags in metrics.calls):
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    leaked = [t for t in set(asyncio.all_tasks()) - before if not t.done()]
    assert leaked == []  # no child relay/writer task leaked
    gauges = [1 for _op, name, _tags in metrics.calls if name.endswith("active_sessions")]
    assert len(gauges) == 2  # +1 then -1: teardown ran despite cancellation


@pytest.mark.asyncio
async def test_byte_budget_trips_backpressure_under_the_frame_count_limit() -> None:
    # The count bound is huge but the byte bound is small and each frame is large,
    # so memory pressure (not frame count) trips tail-drop and the 1013 close.
    metrics = _RecordingMetrics()
    client = _BlockingClient()
    server = _server(
        metrics, _FloodingLargeUpstream(count=500, frame_bytes=10_000), maxsize=100_000, close_after=3, maxbytes=50_000
    )

    await asyncio.wait_for(server._handle_client(client), timeout=5)  # type: ignore[arg-type]

    assert client.close_code == _BACKPRESSURE_CLOSE_CODE
    assert metrics.count(FRAMES_DROPPED, reason="backpressure") >= 1


@pytest.mark.asyncio
async def test_sustained_event_overflow_drops_then_closes_the_slow_client() -> None:
    metrics = _RecordingMetrics()
    client = _BlockingClient()
    server = _server(metrics, _FloodingUpstream(count=200), maxsize=2, close_after=3)

    await asyncio.wait_for(server._handle_client(client), timeout=5)  # type: ignore[arg-type]

    assert client.close_code == _BACKPRESSURE_CLOSE_CODE
    assert metrics.count(FRAMES_DROPPED, reason="backpressure") >= 1
    assert metrics.count(BACKPRESSURE_CLOSED) == 1


@pytest.mark.asyncio
async def test_non_positive_constructor_bound_is_clamped_and_backpressure_still_fires() -> None:
    # A <=0 maxsize would make asyncio.Queue unbounded and silently disable
    # backpressure; the constructor must clamp it like the env path does.
    metrics = _RecordingMetrics()
    client = _BlockingClient()
    server = _server(metrics, _FloodingUpstream(count=200), maxsize=0, close_after=-5)
    assert server._client_queue_maxsize == 1
    assert server._client_backpressure_close_after == 1

    await asyncio.wait_for(server._handle_client(client), timeout=5)  # type: ignore[arg-type]

    assert client.close_code == _BACKPRESSURE_CLOSE_CODE  # queue stayed bounded
    assert metrics.count(BACKPRESSURE_CLOSED) == 1


@pytest.mark.asyncio
async def test_backpressure_never_blocks_the_upstream_read_loop() -> None:
    # The upstream floods faster than the (stalled) client drains; the relay must
    # still finish (by dropping + closing) rather than deadlock on the client.
    metrics = _RecordingMetrics()
    client = _BlockingClient()
    server = _server(metrics, _FloodingUpstream(count=10_000), maxsize=4, close_after=8)

    await asyncio.wait_for(server._handle_client(client), timeout=5)  # type: ignore[arg-type]

    assert client.close_code == _BACKPRESSURE_CLOSE_CODE


@pytest.mark.asyncio
async def test_a_command_response_is_never_dropped_slow_client_is_closed_instead() -> None:
    # A response is non-droppable: losing it desyncs the client, so a client too
    # slow to take even one is closed immediately (no drop, no wait for close_after).
    metrics = _RecordingMetrics()
    remapper = RequestIdRemapper()
    upstream_cmd = remapper.to_upstream("client", CdpCommand(id=1, method="Page.enable"))
    upstream = _ScriptedUpstream([encode_frame(CdpResponse(id=upstream_cmd.id, result={}))])
    outbound = _OutboundQueue(max_frames=1, max_bytes=1_000_000)
    outbound.put_nowait("prefill-so-the-queue-is-full")
    ws = _NoCloseWs()
    server = _server(metrics, upstream, maxsize=1, close_after=100)
    command_starts: dict[int, tuple[str, float]] = {upstream_cmd.id: ("Page.enable", 0.0)}

    await asyncio.wait_for(
        server._relay_upstream_to_client(ws, upstream, _session(), remapper, command_starts, outbound),  # type: ignore[arg-type]
        timeout=5,
    )

    assert ws.close_code == _BACKPRESSURE_CLOSE_CODE
    assert metrics.count(FRAMES_DROPPED, reason="backpressure") == 0  # the response was not dropped
    assert metrics.count(BACKPRESSURE_CLOSED) == 1


@pytest.mark.asyncio
async def test_a_session_lifecycle_event_is_never_dropped_slow_client_is_closed_instead() -> None:
    # SKY-12500: attachedToTarget/detachedFromTarget are how a client builds and retires
    # its session objects. Tail-dropping one leaves it silently desynced from the browser
    # — holding a session it can never use, or believing a dead one is live — so like a
    # response they are non-droppable and a client too slow to take one is closed.
    metrics = _RecordingMetrics()
    lifecycle = CdpEvent(method="Target.attachedToTarget", params={"sessionId": "S"})
    upstream = _ScriptedUpstream([encode_frame(lifecycle)])
    outbound = _OutboundQueue(max_frames=1, max_bytes=1_000_000)
    outbound.put_nowait("prefill-so-the-queue-is-full")
    ws = _NoCloseWs()
    server = _server(metrics, upstream, maxsize=1, close_after=100)

    await asyncio.wait_for(
        server._relay_upstream_to_client(ws, upstream, _session(), RequestIdRemapper(), {}, outbound),  # type: ignore[arg-type]
        timeout=5,
    )

    assert ws.close_code == _BACKPRESSURE_CLOSE_CODE
    assert metrics.count(FRAMES_DROPPED, reason="backpressure") == 0  # never silently dropped
    assert metrics.count(BACKPRESSURE_CLOSED) == 1


@pytest.mark.asyncio
async def test_shared_delivery_holds_the_same_lifecycle_rule() -> None:
    # The same guarantee at the shared multi-client choke point, not only the pinned
    # single-client relay.
    metrics = _RecordingMetrics()
    server = _server(metrics, _ScriptedUpstream([]), maxsize=1, close_after=100)
    shared = _SharedUpstream(session=_session())
    channel = _ClientChannel("c1", _OutboundQueue(max_frames=1, max_bytes=1_000_000))
    channel.outbound.put_nowait("prefill-so-the-queue-is-full")
    shared.channels["c1"] = channel
    lifecycle = CdpEvent(method="Target.detachedFromTarget", params={"sessionId": "S"})

    server._deliver_to_channel(shared, channel, lifecycle, encode_frame(lifecycle))

    assert channel.closed.is_set()
    assert channel.close_code == _BACKPRESSURE_CLOSE_CODE
    assert metrics.count(FRAMES_DROPPED, reason="backpressure") == 0
    assert metrics.count(BACKPRESSURE_CLOSED) == 1


@pytest.mark.asyncio
async def test_a_client_that_cannot_reclaim_a_slot_is_closed_not_served_at_a_co_tenants_cost() -> None:
    # SKY-12500: the shared pending table is full of a CO-TENANT's promised responses.
    # Admitting this client's command would mean evicting one of them, and a lost
    # response desyncs that victim invisibly — so the newcomer is closed instead.
    metrics = _RecordingMetrics()
    remapper = RequestIdRemapper(max_pending=1)
    victim = remapper.to_upstream("co-tenant", CdpCommand(id=1, method="Page.enable"))
    upstream = _ScriptedUpstream([])
    client = _BlockingClient([encode_frame(CdpCommand(id=1, method="Runtime.enable"))])
    server = _server(metrics, upstream, maxsize=8, close_after=8)

    await asyncio.wait_for(
        server._relay_client_to_upstream(client, upstream, _session(), remapper, {}),  # type: ignore[arg-type]
        timeout=5,
    )

    assert client.close_code == _BACKPRESSURE_CLOSE_CODE
    assert metrics.count(BACKPRESSURE_CLOSED) == 1
    # The victim's mapping survived intact and still resolves to the victim.
    assert remapper.to_client(CdpResponse(id=victim.id, result={})) == ("co-tenant", CdpResponse(id=1, result={}))


@pytest.mark.asyncio
async def test_healthy_client_within_the_bound_is_never_dropped_or_closed() -> None:
    metrics = _RecordingMetrics()
    upstream = _ScriptedUpstream([encode_frame(CdpEvent(method="Page.loadEventFired", params={}))])
    outbound = _OutboundQueue(max_frames=64, max_bytes=1_000_000)
    ws = _NoCloseWs()
    server = _server(metrics, upstream, maxsize=64, close_after=8)

    with pytest.raises(UpstreamClosedError):  # relay ends when the scripted upstream drains
        await asyncio.wait_for(
            server._relay_upstream_to_client(ws, upstream, _session(), RequestIdRemapper(), {}, outbound),  # type: ignore[arg-type]
            timeout=5,
        )

    assert ws.close_code is None
    assert metrics.count(FRAMES_DROPPED) == 0
    assert metrics.count(BACKPRESSURE_CLOSED) == 0
    assert outbound.get_nowait()  # the event was enqueued for delivery
