"""Reusable contract suite for MetricsPort adapters.

Any adapter (the OSS NoOp here, the cloud OTel adapter under tests/cloud/)
subclasses MetricsPortContract and overrides make_metrics()/collect(); every
guarantee the proxy relies on — a pass-through session vs a filtered/errored one
is distinguishable via metrics, an active-session gauge that balances, and no
token or upstream URL in any label — is asserted once, here.

The NoOp subclass cannot record, so collect() returns None and the recording
assertions are skipped; what still runs against NoOp is that driving every
instrumented path calls the port cleanly (no crash, all ops accepted).
"""

from __future__ import annotations

import asyncio
from collections import namedtuple
from types import SimpleNamespace
from typing import Iterable, Iterator, Mapping

import pytest

from skyvern.proxy.adapters.memory import ForwardAllEventPolicy, InMemorySessionRegistry, NoOpMetrics, StaticKeyAuth
from skyvern.proxy.adapters.websocket_server import (
    _MAX_PENDING_COMMANDS,
    CdpProxyServer,
    _track_command,
)
from skyvern.proxy.core.errors import UpstreamConnectError
from skyvern.proxy.core.frames import CdpCommand, CdpEvent, CdpResponse, RequestIdRemapper, decode_frame, encode_frame
from skyvern.proxy.core.pipeline import MiddlewarePipeline
from skyvern.proxy.core.policy import Drop, DropReason, PolicyDecision
from skyvern.proxy.core.session import Principal, ProxySession, ResolvedSession, UpstreamClosedError
from skyvern.proxy.ports import MetricsPort

SESSION_ID = "pbs_metrics_session"
ORG_ID = "o_metrics"
VALID_KEY = "metrics-key-abc123"
SECRET = "upstream-secret-token-do-not-leak"

Record = namedtuple("Record", "name value attributes")

FRAMES_RELAYED = "skyvern.cdp_proxy.frames_relayed"
BYTES_RELAYED = "skyvern.cdp_proxy.bytes_relayed"
FRAMES_DROPPED = "skyvern.cdp_proxy.frames_dropped"
FRAME_DECODE_ERRORS = "skyvern.cdp_proxy.frame_decode_errors"
COMMAND_LATENCY = "skyvern.cdp_proxy.command_latency_seconds"
ACTIVE_SESSIONS = "skyvern.cdp_proxy.active_sessions"
CLIENT_CONNECTED = "skyvern.cdp_proxy.client_connected"
CLIENT_DISCONNECTED = "skyvern.cdp_proxy.client_disconnected"
CONNECTION_REJECTED = "skyvern.cdp_proxy.connection_rejected"


class _FakeClientWebSocket:
    def __init__(
        self,
        path: str,
        incoming: list[str] | None = None,
        headers: Mapping[str, str] | None = None,
        block: bool = True,
    ) -> None:
        self.request = SimpleNamespace(headers=dict(headers or {}), path=path)
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._incoming = list(incoming or [])
        self._block = block
        self._drained = asyncio.Event()

    def __aiter__(self) -> _FakeClientWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._incoming:
            return self._incoming.pop(0)
        # Stay open like a real client until the connection is torn down (block),
        # or end the client stream once drained (block=False) so the client side
        # can terminate a session the upstream never will (e.g. a pipeline drop).
        if self._block:
            await self._drained.wait()
        raise StopAsyncIteration

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        self._drained.set()


class _EchoThenCloseUpstreamConnection:
    """Answers each client command with a matching response, then closes so the
    upstream relay finishes only after the response is processed (deterministic)."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.received: list[str] = []
        self._closed = False

    async def send(self, raw: str) -> None:
        if self._closed:
            raise UpstreamClosedError("closed")
        self.received.append(raw)
        frame = decode_frame(raw)
        self._queue.put_nowait(encode_frame(CdpResponse(id=frame.id, result={})))
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


class _ScriptedEventUpstreamConnection:
    """Emits pre-scripted upstream frames then closes; ignores client sends."""

    def __init__(self, scripted: list[str]) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        for raw in scripted:
            self._queue.put_nowait(raw)
        self._queue.put_nowait(None)
        self._closed = False

    async def send(self, raw: str) -> None:
        return None

    async def receive(self) -> str:
        raw = await self._queue.get()
        if raw is None:
            raise UpstreamClosedError("closed")
        return raw

    async def close(self) -> None:
        self._closed = True
        self._queue.put_nowait(None)


class _RaisingCloseUpstreamConnection(_EchoThenCloseUpstreamConnection):
    """Echoes like its parent but raises from close() — stands in for a real
    upstream adapter whose close() does I/O and can fail."""

    async def close(self) -> None:
        raise RuntimeError("upstream close failed")


class _Upstream:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def connect(self, session: ProxySession) -> object:
        return self._connection


class _DropAllEvents:
    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        return Drop(DropReason.POLICY)

    def observe_command(self, command: CdpCommand, session: ProxySession) -> None:
        return None

    def forget(self, session_id: str) -> None:
        return None


class _FailingUpstream:
    async def connect(self, session: ProxySession) -> object:
        raise UpstreamConnectError("upstream dial failed")


class _BlockingUpstreamConnection:
    """Never yields a frame; receive() blocks until close(), keeping the session
    live so the active_sessions gauge can be observed mid-flight."""

    def __init__(self) -> None:
        self._closed = asyncio.Event()

    async def send(self, raw: str) -> None:
        return None

    async def receive(self) -> str:
        await self._closed.wait()
        raise UpstreamClosedError("closed")

    async def close(self) -> None:
        self._closed.set()


class _CaptureUpstreamConnection:
    """Records what the client->upstream leg sends; never yields upstream frames."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self._closed = asyncio.Event()

    async def send(self, raw: str) -> None:
        self.received.append(raw)

    async def receive(self) -> str:
        await self._closed.wait()
        raise UpstreamClosedError("closed")

    async def close(self) -> None:
        self._closed.set()


class _RecordingMetrics:
    """MetricsPort that records the full call sequence (order + amount + tags),
    so a test can prove a live +1 gauge, not just a final net-0."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float, dict[str, str]]] = []

    def increment(self, name: str, amount: int = 1, tags: Mapping[str, str] | None = None) -> None:
        self.calls.append(("increment", name, amount, dict(tags or {})))

    def observe(self, name: str, value: float, tags: Mapping[str, str] | None = None) -> None:
        self.calls.append(("observe", name, value, dict(tags or {})))

    def gauge(self, name: str, amount: int, tags: Mapping[str, str] | None = None) -> None:
        self.calls.append(("gauge", name, amount, dict(tags or {})))


def _bare_session() -> ProxySession:
    return ProxySession(
        session_id=SESSION_ID,
        upstream_ws_url="ws://x/y",
        principal=Principal(principal_id=ORG_ID, organization_id=ORG_ID),
    )


def _relay_server(metrics: MetricsPort, pipeline: MiddlewarePipeline | None = None) -> CdpProxyServer:
    return CdpProxyServer(
        upstream=_Upstream(None),  # type: ignore[arg-type]
        sessions=InMemorySessionRegistry(),
        auth=StaticKeyAuth({}),
        metrics=metrics,
        event_policy=ForwardAllEventPolicy(),  # type: ignore[arg-type]
        pipeline=pipeline,
    )


def _sum(records: Iterable[Record], name: str) -> float:
    return sum(record.value for record in records if record.name == name)


def _has(records: Iterable[Record], name: str) -> bool:
    return any(record.name == name for record in records)


def _label_values(records: Iterable[Record]) -> Iterator[str]:
    for record in records:
        for value in record.attributes.values():
            yield str(value)


class MetricsPortContract:
    def make_metrics(self) -> MetricsPort:
        raise NotImplementedError

    def collect(self, metrics: MetricsPort) -> list[Record] | None:
        """Emitted metrics for `metrics`, or None if the adapter cannot record."""
        return None

    def _server(
        self,
        metrics: MetricsPort,
        *,
        upstream: object,
        event_policy: object | None = None,
        pipeline: MiddlewarePipeline | None = None,
        sessions: InMemorySessionRegistry | None = None,
    ) -> CdpProxyServer:
        if sessions is None:
            sessions = InMemorySessionRegistry()
            sessions.put(
                ResolvedSession(
                    session_id=SESSION_ID,
                    upstream_adapter="memory",
                    upstream_ws_url=f"ws://vendor.internal:9222/live?token={SECRET}",
                    organization_id=ORG_ID,
                    connect_headers={"authorization": f"Bearer {SECRET}"},
                )
            )
        return CdpProxyServer(
            upstream=upstream,  # type: ignore[arg-type]
            sessions=sessions,
            auth=StaticKeyAuth({VALID_KEY: Principal(principal_id=ORG_ID, organization_id=ORG_ID)}),
            metrics=metrics,
            event_policy=event_policy or ForwardAllEventPolicy(),  # type: ignore[arg-type]
            pipeline=pipeline,
        )

    def _client(self, incoming: list[str] | None = None, block: bool = True) -> _FakeClientWebSocket:
        return _FakeClientWebSocket(
            path=f"/{SESSION_ID}", incoming=incoming, headers={"x-api-key": VALID_KEY}, block=block
        )

    async def _run_passthrough(
        self, metrics: MetricsPort, method: str = "Page.enable"
    ) -> tuple[_FakeClientWebSocket, _EchoThenCloseUpstreamConnection]:
        connection = _EchoThenCloseUpstreamConnection()
        client = self._client([encode_frame(CdpCommand(id=7, method=method))])
        server = self._server(metrics, upstream=_Upstream(connection))
        await server._handle_client(client)  # type: ignore[arg-type]
        return client, connection

    async def _run_filtered(self, metrics: MetricsPort) -> None:
        event = encode_frame(CdpEvent(method="Network.requestWillBeSent", params={}))
        server = self._server(
            metrics, upstream=_Upstream(_ScriptedEventUpstreamConnection([event])), event_policy=_DropAllEvents()
        )
        await server._handle_client(self._client())  # type: ignore[arg-type]

    async def _run_unmatched_response(self, metrics: MetricsPort) -> None:
        # A response whose id was never mapped by the remapper — a genuinely
        # reachable path (late/duplicate upstream reply).
        response = encode_frame(CdpResponse(id=999, result={}))
        server = self._server(metrics, upstream=_Upstream(_ScriptedEventUpstreamConnection([response])))
        await server._handle_client(self._client())  # type: ignore[arg-type]

    async def _run_pipeline_dropped(self, metrics: MetricsPort) -> None:
        async def drop(frame: object, direction: object, session: object) -> None:
            return None

        # The pipeline drops the only client frame, so no upstream reply ever
        # arrives; a non-blocking client ends the stream to tear the session down.
        server = self._server(
            metrics, upstream=_Upstream(_EchoThenCloseUpstreamConnection()), pipeline=MiddlewarePipeline([drop])
        )
        await server._handle_client(self._client([encode_frame(CdpCommand(id=1, method="Page.enable"))], block=False))  # type: ignore[arg-type]

    async def _run_errored(self, metrics: MetricsPort) -> None:
        server = self._server(metrics, upstream=_Upstream(_EchoThenCloseUpstreamConnection()))
        await server._handle_client(self._client(["}not a frame{"]))  # type: ignore[arg-type]

    async def _run_rejected(self, metrics: MetricsPort, path: str, key: str, sessions=None) -> None:  # type: ignore[no-untyped-def]
        server = self._server(metrics, upstream=_Upstream(_EchoThenCloseUpstreamConnection()), sessions=sessions)
        ws = _FakeClientWebSocket(path=path, headers={"x-api-key": key})
        await server._handle_client(ws)  # type: ignore[arg-type]

    async def _run_connect_failure(self, metrics: MetricsPort) -> _FakeClientWebSocket:
        server = self._server(metrics, upstream=_FailingUpstream())
        ws = self._client([encode_frame(CdpCommand(id=1, method="Page.enable"))])
        await server._handle_client(ws)  # type: ignore[arg-type]
        return ws

    @pytest.mark.asyncio
    async def test_passthrough_session_relays_frames_in_both_directions(self) -> None:
        metrics = self.make_metrics()
        await self._run_passthrough(metrics)
        records = self.collect(metrics)
        if records is None:
            return
        assert _sum(records, FRAMES_RELAYED) >= 2
        assert _sum(records, FRAMES_DROPPED) == 0
        assert _has(records, COMMAND_LATENCY)

    @pytest.mark.asyncio
    async def test_bytes_relayed_matches_wire_size_by_direction(self) -> None:
        metrics = self.make_metrics()
        client, connection = await self._run_passthrough(metrics)
        records = self.collect(metrics)
        if records is None:
            return
        upstream_bytes = sum(len(wire) for wire in connection.received)
        client_bytes = sum(len(wire) for wire in client.sent)
        assert upstream_bytes > 0 and client_bytes > 0
        by_direction: dict[str | None, float] = {}
        for record in records:
            if record.name == BYTES_RELAYED:
                key = record.attributes.get("direction")
                by_direction[key] = by_direction.get(key, 0) + record.value
        assert by_direction.get("client_to_upstream") == upstream_bytes
        assert by_direction.get("upstream_to_client") == client_bytes

    @pytest.mark.asyncio
    async def test_unknown_cdp_domain_and_method_bucket_to_other(self) -> None:
        metrics = self.make_metrics()
        await self._run_passthrough(metrics, method="Bogus.doThing")
        records = self.collect(metrics)
        if records is None:
            return
        latency = [r for r in records if r.name == COMMAND_LATENCY]
        assert latency
        assert all(r.attributes.get("cdp_domain") == "other" for r in latency)
        assert all(r.attributes.get("cdp_method") == "other" for r in latency)

    @pytest.mark.asyncio
    async def test_known_domain_unknown_method_buckets_method_and_hides_client_string(self) -> None:
        # A method under a known domain carrying a token: the domain is kept, but
        # the method (client-controlled) must collapse to "other" so it can neither
        # mint unbounded series nor smuggle the token into a label.
        metrics = self.make_metrics()
        await self._run_passthrough(metrics, method=f"Page.{SECRET}")
        records = self.collect(metrics)
        if records is None:
            return
        latency = [r for r in records if r.name == COMMAND_LATENCY]
        assert latency
        assert all(r.attributes.get("cdp_domain") == "Page" for r in latency)
        assert all(r.attributes.get("cdp_method") == "other" for r in latency)
        for value in _label_values(records):
            assert SECRET not in value

    @pytest.mark.asyncio
    async def test_upstream_connect_failure_is_rejected_and_recorded(self) -> None:
        metrics = self.make_metrics()
        ws = await self._run_connect_failure(metrics)
        assert ws.close_code == 1011
        records = self.collect(metrics)
        if records is None:
            return
        rejected = [r for r in records if r.name == CONNECTION_REJECTED]
        assert any(r.attributes.get("reason") == "upstream_unavailable" for r in rejected)
        # The gauge never went up (the dial failed), so it must not have gone down.
        assert _sum(records, ACTIVE_SESSIONS) == 0

    @pytest.mark.asyncio
    async def test_filtered_session_is_distinguishable_from_passthrough(self) -> None:
        metrics = self.make_metrics()
        await self._run_filtered(metrics)
        records = self.collect(metrics)
        if records is None:
            return
        dropped = [r for r in records if r.name == FRAMES_DROPPED]
        assert _sum(dropped, FRAMES_DROPPED) >= 1
        assert any(r.attributes.get("reason") == "event_policy" for r in dropped)

    @pytest.mark.asyncio
    async def test_frames_dropped_reasons_are_labelled(self) -> None:
        metrics = self.make_metrics()
        await self._run_filtered(metrics)
        await self._run_unmatched_response(metrics)
        await self._run_pipeline_dropped(metrics)
        records = self.collect(metrics)
        if records is None:
            return
        reasons = {r.attributes.get("reason") for r in records if r.name == FRAMES_DROPPED}
        assert {"event_policy", "unmatched_response", "pipeline"} <= reasons

    @pytest.mark.asyncio
    async def test_errored_session_records_a_frame_decode_error(self) -> None:
        metrics = self.make_metrics()
        await self._run_errored(metrics)
        records = self.collect(metrics)
        if records is None:
            return
        assert _sum(records, FRAME_DECODE_ERRORS) >= 1

    @pytest.mark.asyncio
    async def test_every_rejection_reason_is_labelled(self) -> None:
        metrics = self.make_metrics()
        await self._run_rejected(metrics, path="//[", key=VALID_KEY)  # urlsplit raises -> malformed_request
        await self._run_rejected(metrics, path=f"/{SESSION_ID}", key="wrong-key")  # bad key -> unauthorized
        await self._run_rejected(metrics, path="/s_unknown", key=VALID_KEY)  # unseeded id -> unknown_session
        for status in ("pending", "closed", "expired"):  # owned but not routable -> lifecycle reason
            registry = InMemorySessionRegistry()
            getattr(registry, f"mark_{status}")(SESSION_ID, organization_id=ORG_ID)
            await self._run_rejected(metrics, path=f"/{SESSION_ID}", key=VALID_KEY, sessions=registry)
        await self._run_connect_failure(metrics)  # dial fails -> upstream_unavailable
        records = self.collect(metrics)
        if records is None:
            return
        reasons = {r.attributes.get("reason") for r in records if r.name == CONNECTION_REJECTED}
        assert {
            "malformed_request",
            "unauthorized",
            "unknown_session",
            "pending",
            "closed",
            "expired",
            "upstream_unavailable",
        } <= reasons

    @pytest.mark.asyncio
    async def test_active_session_gauge_is_recorded_and_balances(self) -> None:
        metrics = self.make_metrics()
        await self._run_passthrough(metrics)
        records = self.collect(metrics)
        if records is None:
            return
        assert _sum(records, CLIENT_CONNECTED) == 1
        assert _sum(records, CLIENT_DISCONNECTED) == 1
        assert _has(records, ACTIVE_SESSIONS)
        assert _sum(records, ACTIVE_SESSIONS) == 0

    @pytest.mark.asyncio
    async def test_active_session_gauge_balances_when_upstream_close_raises(self) -> None:
        metrics = self.make_metrics()
        connection = _RaisingCloseUpstreamConnection()
        server = self._server(metrics, upstream=_Upstream(connection))
        with pytest.raises(RuntimeError):
            await server._handle_client(self._client([encode_frame(CdpCommand(id=7, method="Page.enable"))]))  # type: ignore[arg-type]
        records = self.collect(metrics)
        if records is None:
            return
        assert _has(records, ACTIVE_SESSIONS)
        assert _sum(records, ACTIVE_SESSIONS) == 0

    @pytest.mark.asyncio
    async def test_no_token_or_upstream_url_appears_in_any_label(self) -> None:
        metrics = self.make_metrics()
        # Drive every emission path so redaction is checked across all of them.
        await self._run_passthrough(metrics)
        await self._run_filtered(metrics)
        await self._run_unmatched_response(metrics)
        await self._run_errored(metrics)
        await self._run_rejected(metrics, path="/s_unknown", key=VALID_KEY)
        records = self.collect(metrics)
        if records is None:
            return
        labels = list(_label_values(records))
        assert labels  # the driven paths emitted labelled metrics
        for value in labels:
            assert SECRET not in value
            assert "vendor.internal" not in value


class TestNoOpMetricsContract(MetricsPortContract):
    def make_metrics(self) -> MetricsPort:
        return NoOpMetrics()


def _gauge_amounts(metrics: _RecordingMetrics) -> list[float]:
    return [amount for op, name, amount, _tags in metrics.calls if op == "gauge" and name == ACTIVE_SESSIONS]


@pytest.mark.asyncio
async def test_active_gauge_emits_a_live_plus_one_before_the_teardown_minus_one() -> None:
    metrics = _RecordingMetrics()
    harness = TestNoOpMetricsContract()
    server = harness._server(metrics, upstream=_Upstream(_EchoThenCloseUpstreamConnection()))
    await server._handle_client(harness._client([encode_frame(CdpCommand(id=7, method="Page.enable"))]))  # type: ignore[arg-type]
    assert _gauge_amounts(metrics) == [1, -1]


@pytest.mark.asyncio
async def test_active_gauge_decrements_on_midflight_teardown() -> None:
    metrics = _RecordingMetrics()
    harness = TestNoOpMetricsContract()
    connection = _BlockingUpstreamConnection()
    client = harness._client(block=True)  # stays live until we end the stream
    server = harness._server(metrics, upstream=_Upstream(connection))
    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    for _ in range(100):
        if 1 in _gauge_amounts(metrics):
            break
        await asyncio.sleep(0)
    assert _gauge_amounts(metrics) == [1]  # live +1, no -1 yet
    client._drained.set()  # client stream ends -> finally cancels the pending relay
    await task
    assert _gauge_amounts(metrics) == [1, -1]


def test_track_command_is_bounded_by_the_cap() -> None:
    command_starts: dict[int, tuple[str, float]] = {}
    for upstream_id in range(_MAX_PENDING_COMMANDS + 50):
        _track_command(command_starts, upstream_id, "Page.enable")
    assert len(command_starts) == _MAX_PENDING_COMMANDS
    assert (_MAX_PENDING_COMMANDS + 49) in command_starts  # newest kept
    assert 0 not in command_starts  # oldest evicted


@pytest.mark.asyncio
async def test_out_of_order_responses_match_and_drain_command_starts() -> None:
    metrics = _RecordingMetrics()
    server = _relay_server(metrics)
    session = _bare_session()
    remapper = RequestIdRemapper()
    command_starts: dict[int, tuple[str, float]] = {}
    commands = _FakeClientWebSocket(
        "/x",
        incoming=[
            encode_frame(CdpCommand(id=10, method="Page.enable")),
            encode_frame(CdpCommand(id=11, method="Network.enable")),
            encode_frame(CdpCommand(id=12, method="Runtime.enable")),
        ],
        block=False,
    )
    await server._relay_client_to_upstream(commands, _CaptureUpstreamConnection(), session, remapper, command_starts)
    assert len(command_starts) == 3
    # Respond out of order (3, 1, 2) then close.
    responses = _ScriptedEventUpstreamConnection([encode_frame(CdpResponse(id=i, result={})) for i in (3, 1, 2)])
    try:
        await server._relay_upstream_to_client(_FakeClientWebSocket("/x"), responses, session, remapper, command_starts)
    except UpstreamClosedError:
        pass
    assert command_starts == {}  # empty after quiescence
    assert sum(1 for op, name, *_ in metrics.calls if op == "observe" and name == COMMAND_LATENCY) == 3


@pytest.mark.asyncio
async def test_dropped_and_unmatched_responses_evict_their_command_starts() -> None:
    metrics = _RecordingMetrics()
    session = _bare_session()

    async def drop(frame: object, direction: object, sess: object) -> None:
        return None

    # A pipeline-dropped response frees its pending latency entry AND its id mapping:
    # the response will never be delivered, so every table its upstream id sits in has
    # to let go of it (SKY-12500 AC5).
    dropping = _relay_server(metrics, pipeline=MiddlewarePipeline([drop]))
    remapper = RequestIdRemapper()
    suppressed = remapper.to_upstream("client", CdpCommand(id=1, method="Page.enable"))
    command_starts: dict[int, tuple[str, float]] = {suppressed.id: ("Page.enable", 0.0)}
    dropped = _ScriptedEventUpstreamConnection([encode_frame(CdpResponse(id=suppressed.id, result={}))])
    try:
        await dropping._relay_upstream_to_client(_FakeClientWebSocket("/x"), dropped, session, remapper, command_starts)
    except UpstreamClosedError:
        pass
    assert suppressed.id not in command_starts
    assert remapper.pending_count == 0

    # An unmatched response (id never remapped) also evicts and is labelled.
    passthrough = _relay_server(metrics)
    unmatched_starts: dict[int, tuple[str, float]] = {7: ("Page.enable", 0.0)}
    unmatched = _ScriptedEventUpstreamConnection([encode_frame(CdpResponse(id=7, result={}))])
    try:
        await passthrough._relay_upstream_to_client(
            _FakeClientWebSocket("/x"), unmatched, session, RequestIdRemapper(), unmatched_starts
        )
    except UpstreamClosedError:
        pass
    assert 7 not in unmatched_starts
