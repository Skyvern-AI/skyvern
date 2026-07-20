"""Screencast protection at the proxy boundary (SKY-12502).

An external client that drives Page.startScreencast through the proxy can pull an
unbounded stream of JPEG frames, at whatever size and rate it asks for. The proxy caps
the rate it delivers and bounds the capture params the client requests.

The cap may only ever drop a frame the proxy has ACKED on the client's behalf. Chrome's
screencast is ack-driven: it holds a small number of frames in flight and emits the next
one only once an earlier one comes back acked. A dropped frame is a frame the client
never sees and so never acks, which means a cap that merely drops does not throttle the
stream — it stalls it to 0 FPS and never recovers. Every test here that withholds a frame
therefore also asserts the ack that keeps the browser emitting.

Scope is the EXTERNAL proxied path only. Cloud live-view is VNC + Page.captureScreenshot
and never rides this code.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from skyvern.proxy.adapters.memory import (
    AllowAllAuth,
    ForwardAllEventPolicy,
    InMemorySessionRegistry,
    NoOpMetrics,
)
from skyvern.proxy.adapters.websocket_server import CdpProxyServer, _cdp_method_tags
from skyvern.proxy.core.frames import (
    CdpCommand,
    CdpEvent,
    CdpFrame,
    CdpResponse,
    RequestIdRemapper,
    decode_frame,
    encode_frame,
)
from skyvern.proxy.core.pipeline import Direction, MiddlewarePipeline
from skyvern.proxy.core.policy import FORWARD, EventPolicyEngine, PolicyDecision, Rewrite
from skyvern.proxy.core.screencast import (
    SCREENCAST_FRAME_ACK_METHOD,
    SCREENCAST_FRAME_EVENT,
    SCREENCAST_MAX_DIMENSION,
    SCREENCAST_MAX_FRAMES_PER_SECOND,
    SCREENCAST_MAX_QUALITY,
    SCREENCAST_MIN_EVERY_NTH_FRAME,
    SCREENCAST_PACK_V1,
    START_SCREENCAST_METHOD,
    bound_start_screencast,
    is_screencast_frame,
    screencast_frame_ack,
    screencast_pipeline,
)
from skyvern.proxy.core.session import ProxySession, ResolvedSession
from tests.unit.proxy.test_event_policy import FakeClock
from tests.unit.proxy.test_shared_connection import (
    UPSTREAM_URL,
    _command,
    _ControllableUpstream,
    _ScriptClient,
    _SharedBrowser,
    _until,
)

CDP_SESSION = "sess"


def _screencast_server(browser: object, clock: FakeClock) -> CdpProxyServer:
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url=UPSTREAM_URL))
    return CdpProxyServer(
        upstream=browser,  # type: ignore[arg-type]
        sessions=sessions,
        auth=AllowAllAuth(),
        metrics=NoOpMetrics(),
        event_policy=EventPolicyEngine(config=SCREENCAST_PACK_V1, clock=clock),
        pipeline=screencast_pipeline(),
    )


def _screencast_frame(number: int, session_id: str = CDP_SESSION, data_size: int = 16) -> CdpEvent:
    """A frame as Chrome really sends it: `sessionId` is the FRAME NUMBER, an integer,
    and the CDP session it belongs to is the frame's own session_id. `data_size` stands in
    for the base64 JPEG payload, which is essentially all of a real frame's weight."""
    return CdpEvent(
        method=SCREENCAST_FRAME_EVENT,
        params={
            "data": "/9j/4AAQSkZJRg==".ljust(data_size, "A"),
            "metadata": {"deviceWidth": 1920, "deviceHeight": 1080, "timestamp": 1.0},
            "sessionId": number,
        },
        session_id=session_id,
    )


def _delivered_screencast_bytes(client: _ScriptClient) -> int:
    """Wire bytes of screencast frames the client was actually sent — the quantity the
    cap exists to bound, and the one a frame count alone does not show."""
    return sum(
        len(wire)
        for wire in client.sent
        if isinstance(decode_frame(wire), CdpEvent) and decode_frame(wire).method == SCREENCAST_FRAME_EVENT
    )


def _acks(connection: _ControllableUpstream) -> list[CdpCommand]:
    frames = (decode_frame(wire) for wire in connection.sent)
    return [f for f in frames if isinstance(f, CdpCommand) and f.method == SCREENCAST_FRAME_ACK_METHOD]


def _acked_numbers(connection: _ControllableUpstream) -> list[int]:
    return [(ack.params or {})["sessionId"] for ack in _acks(connection)]


async def _attached_client(server: CdpProxyServer) -> tuple[_ScriptClient, asyncio.Task[None]]:
    """A client that owns CDP session `sess`, so frames scoped to it have a recipient
    and the drop is the policy's doing rather than a routing miss."""
    client = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": CDP_SESSION, "flatten": True})])
    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: bool(client.sent))
    return client, task


@pytest.mark.asyncio
async def test_every_withheld_screencast_frame_is_still_acked_upstream() -> None:
    """THE constraint (AC3). The clock never advances, so the whole burst falls in one
    window and everything past the cap is withheld. The browser must see an ack for each
    withheld frame or it stops emitting and the client's stream is dead for good."""
    connection = _ControllableUpstream()
    server = _screencast_server(_SharedBrowser(connection), FakeClock())
    client, task = await _attached_client(server)

    burst = SCREENCAST_MAX_FRAMES_PER_SECOND * 4
    for number in range(1, burst + 1):
        connection.emit(_screencast_frame(number))
    withheld = burst - SCREENCAST_MAX_FRAMES_PER_SECOND
    # Acks are sent inline by the reader while delivered frames go out through a queue
    # and its writer, so the two settle independently — wait for both.
    assert await _until(
        lambda: (
            len(_acks(connection)) == withheld
            and len(client.events(SCREENCAST_FRAME_EVENT)) == SCREENCAST_MAX_FRAMES_PER_SECOND
        )
    )

    # The cap held on the delivered side...
    assert len(client.events(SCREENCAST_FRAME_EVENT)) == SCREENCAST_MAX_FRAMES_PER_SECOND
    # ...and every frame it withheld was acked upstream, in order, so the browser never
    # runs out of in-flight allowance and the stream keeps flowing.
    assert _acked_numbers(connection) == list(range(SCREENCAST_MAX_FRAMES_PER_SECOND + 1, burst + 1))

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_the_stream_still_flows_after_a_sustained_drop_run() -> None:
    """A withheld run must not be a cliff: once the window rolls the client is served
    again, which is what makes this a throttle rather than a stall."""
    connection = _ControllableUpstream()
    clock = FakeClock()
    server = _screencast_server(_SharedBrowser(connection), clock)
    client, task = await _attached_client(server)

    number = 0
    for window in range(1, 4):
        for _ in range(SCREENCAST_MAX_FRAMES_PER_SECOND * 3):
            number += 1
            connection.emit(_screencast_frame(number))
        delivered_by_now = SCREENCAST_MAX_FRAMES_PER_SECOND * window
        acked_by_now = number - delivered_by_now
        # Each window serves its budget and withholds the rest, and the run of withheld
        # frames in between never stops the next window being served.
        assert await _until(
            lambda: (
                len(client.events(SCREENCAST_FRAME_EVENT)) == delivered_by_now
                and len(_acks(connection)) == acked_by_now
            )
        )
        clock.advance(1.0)

    # No frame is ever both undelivered and unacked: the two sets partition the stream.
    delivered = {(event.params or {})["sessionId"] for event in client.events(SCREENCAST_FRAME_EVENT)}
    assert delivered.isdisjoint(_acked_numbers(connection))
    assert len(delivered) + len(_acked_numbers(connection)) == number

    client.release()
    await asyncio.wait_for(task, timeout=5)


async def _burst(policy_kind: str, count: int, data_size: int) -> tuple[int, int]:
    """Push `count` screencast frames through a proxy running `policy_kind` and report
    what the client was delivered, as (frames, wire bytes)."""
    from skyvern.proxy.__main__ import build_event_policy, build_pipeline

    connection = _ControllableUpstream()
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url=UPSTREAM_URL))
    server = CdpProxyServer(
        upstream=_SharedBrowser(connection),  # type: ignore[arg-type]
        sessions=sessions,
        auth=AllowAllAuth(),
        metrics=NoOpMetrics(),
        event_policy=build_event_policy(policy_kind),
        pipeline=build_pipeline(policy_kind),
    )
    client, task = await _attached_client(server)
    for number in range(1, count + 1):
        connection.emit(_screencast_frame(number, data_size=data_size))
    # Every frame ends up either delivered or acked, under both policies — which is the
    # settling condition AND the invariant, so this cannot pass by measuring too early.
    assert await _until(lambda: len(client.events(SCREENCAST_FRAME_EVENT)) + len(_acks(connection)) == count)
    delivered = (len(client.events(SCREENCAST_FRAME_EVENT)), _delivered_screencast_bytes(client))
    client.release()
    await asyncio.wait_for(task, timeout=5)
    return delivered


@pytest.mark.asyncio
async def test_a_screencast_heavy_session_is_capped_in_frames_and_in_bytes() -> None:
    """AC1, against the pass-through baseline on the same stream. Bytes are the point:
    frames ARE the payload here, so an uncapped screencast is the most expensive thing an
    external client can ask this proxy to carry."""
    frames = SCREENCAST_MAX_FRAMES_PER_SECOND * 6
    data_size = 20_000  # a modest real JPEG frame

    baseline_frames, baseline_bytes = await _burst("forward-all", frames, data_size)
    capped_frames, capped_bytes = await _burst("screencast-v1", frames, data_size)

    # The baseline is a true pass-through: nothing shed, so the comparison is honest.
    assert baseline_frames == frames
    assert capped_frames == SCREENCAST_MAX_FRAMES_PER_SECOND
    # Both legs of AC1, and by a margin rather than a rounding error.
    assert capped_frames * 6 == baseline_frames
    assert capped_bytes < baseline_bytes // 5
    assert baseline_bytes > frames * data_size  # the payload really was carried


@pytest.mark.asyncio
async def test_the_ack_carries_the_frame_number_and_the_frames_own_cdp_session() -> None:
    """The screencast domain reuses the name `sessionId` for a frame NUMBER. Acking the
    frame's own session_id instead (or reading params.sessionId as a session id, which
    yields None on every real frame) acks nothing the browser is waiting on."""
    connection = _ControllableUpstream()
    server = _screencast_server(_SharedBrowser(connection), FakeClock())
    client, task = await _attached_client(server)

    for number in range(1, SCREENCAST_MAX_FRAMES_PER_SECOND + 3):
        connection.emit(_screencast_frame(number))
    assert await _until(lambda: len(_acks(connection)) == 2)

    ack = _acks(connection)[0]
    assert ack.params == {"sessionId": SCREENCAST_MAX_FRAMES_PER_SECOND + 1}
    assert isinstance((ack.params or {})["sessionId"], int)
    assert ack.session_id == CDP_SESSION

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_the_synthesized_ack_response_never_reaches_the_client() -> None:
    """The ack rides the proxy's own __proxy__ lane, so its reply is consumed here. A
    client that never sent the ack must never see a response for it."""
    connection = _ControllableUpstream()
    server = _screencast_server(_SharedBrowser(connection), FakeClock())
    client, task = await _attached_client(server)

    for number in range(1, SCREENCAST_MAX_FRAMES_PER_SECOND * 3 + 1):
        connection.emit(_screencast_frame(number))
    assert await _until(lambda: len(_acks(connection)) == SCREENCAST_MAX_FRAMES_PER_SECOND * 2)
    await _until(lambda: False, timeout_ticks=50)  # let every ack reply come back

    # The client's only response is the reply to the attach it actually sent.
    responses = [decode_frame(wire) for wire in client.sent if isinstance(decode_frame(wire), CdpResponse)]
    assert len(responses) == 1
    assert not any(r.result and r.result.get("echo") == SCREENCAST_FRAME_ACK_METHOD for r in responses)

    client.release()
    await asyncio.wait_for(task, timeout=5)


# The three ways a screencast frame can fail to reach the client WITHOUT the policy
# throttle being involved. None is reachable from the shipped config — the middleware
# pipeline carries only the startScreencast bounder and no rule Rewrites a frame — but
# the pipeline and the policy are both extension seams (SKY-12532 rules, SKY-12538's
# Rewrite-to-error, future middleware), and the ack is the property the whole feature
# rests on. Each of these leaves the browser waiting on a frame the client will never
# ack, so each must be paid by the proxy or the stream stalls at 0 FPS.


class _SuppressScreencast:
    """A middleware that drops screencast frames outright."""

    async def __call__(self, frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame | None:
        if direction is Direction.UPSTREAM_TO_CLIENT and is_screencast_frame(frame):
            return None
        return frame


class _RenumberScreencast:
    """A middleware that rewrites a screencast frame's identity — it stays a screencast
    frame, but names a frame number the browser never sent."""

    async def __call__(self, frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame | None:
        if direction is Direction.UPSTREAM_TO_CLIENT and is_screencast_frame(frame):
            assert isinstance(frame, CdpEvent)
            return CdpEvent(
                method=SCREENCAST_FRAME_EVENT,
                params={**(frame.params or {}), "sessionId": 9999},
                session_id=frame.session_id,
            )
        return frame


class _RedactScreencast:
    """A middleware standing in for a rewrite that PRESERVES the frame's identity (e.g.
    blanking the image data). The client can still ack it, so the proxy must not."""

    async def __call__(self, frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame | None:
        if direction is Direction.UPSTREAM_TO_CLIENT and is_screencast_frame(frame):
            assert isinstance(frame, CdpEvent)
            return CdpEvent(
                method=frame.method, params={**(frame.params or {}), "data": ""}, session_id=frame.session_id
            )
        return frame


class _RewriteToNonScreencast:
    """A policy that replaces a screencast frame with some other event — the shape
    SKY-12538's synthesized-error rewrites will take."""

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        if event.method == SCREENCAST_FRAME_EVENT:
            return Rewrite(CdpEvent(method="Log.entryAdded", params={"entry": {}}, session_id=event.session_id))
        return FORWARD

    def observe_command(self, command: CdpCommand, session: ProxySession) -> None:
        return None

    def forget(self, session_id: str) -> None:
        return None


def _server_with(
    connection: _ControllableUpstream,
    event_policy: object | None = None,
    middleware: object | None = None,
) -> CdpProxyServer:
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url=UPSTREAM_URL))
    return CdpProxyServer(
        upstream=_SharedBrowser(connection),  # type: ignore[arg-type]
        sessions=sessions,
        auth=AllowAllAuth(),
        metrics=NoOpMetrics(),
        event_policy=event_policy or ForwardAllEventPolicy(),  # type: ignore[arg-type]
        pipeline=MiddlewarePipeline([middleware] if middleware else []),  # type: ignore[list-item]
    )


@pytest.mark.asyncio
async def test_a_frame_suppressed_by_a_middleware_is_still_acked() -> None:
    """The ack cannot key off the policy decision alone: a middleware that suppresses the
    frame never reaches the policy at all, and the browser is still waiting."""
    connection = _ControllableUpstream()
    server = _server_with(connection, middleware=_SuppressScreencast())
    client, task = await _attached_client(server)

    for number in range(1, 4):
        connection.emit(_screencast_frame(number))
    assert await _until(lambda: _acked_numbers(connection) == [1, 2, 3])

    assert client.events(SCREENCAST_FRAME_EVENT) == []  # the suppression still held

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_frame_renumbered_by_a_middleware_acks_the_original_not_the_rewrite() -> None:
    """The ack must name the frame the BROWSER sent. Derived from the post-pipeline frame
    it would name a frame number the browser never issued, leaving the real one unacked —
    the stall, one indirection further away."""
    connection = _ControllableUpstream()
    server = _server_with(connection, middleware=_RenumberScreencast())
    client, task = await _attached_client(server)

    connection.emit(_screencast_frame(7))
    assert await _until(lambda: _acked_numbers(connection) == [7] and len(client.events(SCREENCAST_FRAME_EVENT)) == 1)

    # The client got the rewrite (and will ack 9999, which settles nothing) — so the
    # original 7 was the proxy's to pay, and it paid exactly that.
    assert [(e.params or {})["sessionId"] for e in client.events(SCREENCAST_FRAME_EVENT)] == [9999]
    assert 9999 not in _acked_numbers(connection)

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_rewrite_that_keeps_the_frames_identity_is_left_for_the_client_to_ack() -> None:
    """The other side of the rule. The delivered frame still names frame 7 on the same
    session, so the client acks it — a second ack from the proxy would spend the browser's
    in-flight allowance twice for one frame."""
    connection = _ControllableUpstream()
    server = _server_with(connection, middleware=_RedactScreencast())
    client, task = await _attached_client(server)

    connection.emit(_screencast_frame(7))
    assert await _until(lambda: len(client.events(SCREENCAST_FRAME_EVENT)) == 1)
    await _until(lambda: False, timeout_ticks=50)

    assert _acks(connection) == []
    assert (client.events(SCREENCAST_FRAME_EVENT)[0].params or {})["sessionId"] == 7

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_frame_rewritten_to_another_event_is_still_acked() -> None:
    """A policy Rewrite delivers something, but not a screencast frame — so the client has
    nothing to ack and the browser is still waiting on the original."""
    connection = _ControllableUpstream()
    server = _server_with(connection, event_policy=_RewriteToNonScreencast())
    client, task = await _attached_client(server)

    connection.emit(_screencast_frame(5))
    assert await _until(lambda: _acked_numbers(connection) == [5] and len(client.events("Log.entryAdded")) == 1)

    assert len(client.events("Log.entryAdded")) == 1  # the rewrite was still delivered
    assert client.events(SCREENCAST_FRAME_EVENT) == []

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_delivered_frame_is_not_acked_by_the_proxy() -> None:
    """Only a withheld frame is the proxy's to ack. Acking a delivered one too would
    double-count against the browser's in-flight allowance and pre-empt the client's own
    ack — the upstream ack behaviour this must preserve."""
    connection = _ControllableUpstream()
    server = _screencast_server(_SharedBrowser(connection), FakeClock())
    client, task = await _attached_client(server)

    for number in range(1, SCREENCAST_MAX_FRAMES_PER_SECOND + 1):
        connection.emit(_screencast_frame(number))
    assert await _until(lambda: len(client.events(SCREENCAST_FRAME_EVENT)) == SCREENCAST_MAX_FRAMES_PER_SECOND)
    await _until(lambda: False, timeout_ticks=50)

    assert _acks(connection) == []

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_frame_with_no_ackable_number_is_forwarded_rather_than_withheld() -> None:
    """Fail-open. A frame the proxy cannot ack is a frame it must not withhold: the
    browser would wait forever on an ack that can never be sent. Forwarding hands the
    ack back to the client, which is the only party that can still send one."""
    connection = _ControllableUpstream()
    server = _screencast_server(_SharedBrowser(connection), FakeClock())
    client, task = await _attached_client(server)

    for number in range(1, SCREENCAST_MAX_FRAMES_PER_SECOND + 1):
        connection.emit(_screencast_frame(number))  # spend the budget
    unackable = CdpEvent(method=SCREENCAST_FRAME_EVENT, params={"data": "x"}, session_id=CDP_SESSION)
    connection.emit(unackable)
    assert await _until(lambda: len(client.events(SCREENCAST_FRAME_EVENT)) == SCREENCAST_MAX_FRAMES_PER_SECOND + 1)

    assert _acks(connection) == []
    assert client.events(SCREENCAST_FRAME_EVENT)[-1].params == {"data": "x"}

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_non_screencast_traffic_is_untouched_by_the_screencast_pack() -> None:
    """Screencast, Input, Network, Fetch and Runtime all multiplex ONE CDP connection.
    The cap is on Page.screencastFrame alone — anything coarser breaks input, downloads
    and recording on the same socket."""
    connection = _ControllableUpstream()
    server = _screencast_server(_SharedBrowser(connection), FakeClock())
    client, task = await _attached_client(server)

    noisy = [
        CdpEvent(method="Network.dataReceived", params={"requestId": "r1"}, session_id=CDP_SESSION),
        CdpEvent(method="Runtime.consoleAPICalled", params={"type": "log"}, session_id=CDP_SESSION),
        CdpEvent(method="Page.loadEventFired", params={"timestamp": 1.0}, session_id=CDP_SESSION),
    ]
    for _ in range(100):
        for event in noisy:
            connection.emit(event)
    assert await _until(lambda: len(client.events("Page.loadEventFired")) == 100)

    # Every non-screencast event got through, however noisy: this pack caps one method.
    assert len(client.events("Network.dataReceived")) == 100
    assert len(client.events("Runtime.consoleAPICalled")) == 100
    assert _acks(connection) == []

    client.release()
    await asyncio.wait_for(task, timeout=5)


def test_start_screencast_params_are_bounded_into_the_range_the_proxy_serves() -> None:
    command = CdpCommand(
        id=7,
        method=START_SCREENCAST_METHOD,
        params={"format": "jpeg", "quality": 100, "maxWidth": 3840, "maxHeight": 2160, "everyNthFrame": 1},
        session_id=CDP_SESSION,
    )

    bounded = bound_start_screencast(command)

    assert (bounded.params or {})["maxWidth"] == SCREENCAST_MAX_DIMENSION
    assert (bounded.params or {})["maxHeight"] == SCREENCAST_MAX_DIMENSION
    assert (bounded.params or {})["everyNthFrame"] == SCREENCAST_MIN_EVERY_NTH_FRAME
    assert (bounded.params or {})["quality"] == SCREENCAST_MAX_QUALITY
    # The codec the client's decoder expects is never swapped, and the command still
    # addresses exactly what it addressed.
    assert (bounded.params or {})["format"] == "jpeg"
    assert bounded.id == 7
    assert bounded.session_id == CDP_SESSION


def test_a_modest_screencast_request_is_left_alone() -> None:
    """A bound is a ceiling, not a setting: a client already asking for less keeps it."""
    command = CdpCommand(
        id=1,
        method=START_SCREENCAST_METHOD,
        params={"maxWidth": 640, "maxHeight": 480, "everyNthFrame": 10, "quality": 30},
    )

    bounded = bound_start_screencast(command)

    assert (bounded.params or {})["maxWidth"] == 640
    assert (bounded.params or {})["maxHeight"] == 480
    assert (bounded.params or {})["everyNthFrame"] == 10
    assert (bounded.params or {})["quality"] == 30


def test_an_omitted_bound_is_imposed_rather_than_left_open() -> None:
    """Every param's absent default is its expensive one: Chrome reads a missing
    maxWidth/maxHeight as 'no bound' and encodes at full viewport size, a missing
    everyNthFrame as every frame, and a missing quality as its own high default. Absent
    is the common case, so it cannot be the unbounded one."""
    bounded = bound_start_screencast(CdpCommand(id=1, method=START_SCREENCAST_METHOD))

    assert (bounded.params or {})["maxWidth"] == SCREENCAST_MAX_DIMENSION
    assert (bounded.params or {})["maxHeight"] == SCREENCAST_MAX_DIMENSION
    assert (bounded.params or {})["everyNthFrame"] == SCREENCAST_MIN_EVERY_NTH_FRAME
    assert (bounded.params or {})["quality"] == SCREENCAST_MAX_QUALITY


@pytest.mark.parametrize("value", [-1, 101, "60", 60.0, True, None])
def test_a_nonsense_quality_is_replaced_by_the_bound(value: object) -> None:
    """Outside [0, 100] is not a request Chrome honours either, so it lands on the
    ceiling rather than being forwarded as-is."""
    bounded = bound_start_screencast(CdpCommand(id=1, method=START_SCREENCAST_METHOD, params={"quality": value}))

    assert (bounded.params or {})["quality"] == SCREENCAST_MAX_QUALITY


@pytest.mark.parametrize("value", [0, -1, "1280", 1.5, True, None])
def test_a_nonsense_dimension_is_replaced_by_the_bound(value: object) -> None:
    """A non-positive or non-integer bound is not a small request — Chrome ignores it
    and encodes unbounded, so it must land on the ceiling, not be honoured as-is."""
    bounded = bound_start_screencast(CdpCommand(id=1, method=START_SCREENCAST_METHOD, params={"maxWidth": value}))

    assert (bounded.params or {})["maxWidth"] == SCREENCAST_MAX_DIMENSION


@pytest.mark.asyncio
async def test_only_the_client_to_upstream_start_command_is_rewritten() -> None:
    """The bound belongs on the way in. A client's own command is what the proxy may
    clamp; nothing on the upstream leg is a startScreencast to rewrite."""
    connection = _ControllableUpstream()
    server = _screencast_server(_SharedBrowser(connection), FakeClock())
    client = _ScriptClient(
        [
            _command(1, "Target.attachToTarget", {"targetId": CDP_SESSION, "flatten": True}),
            _command(2, START_SCREENCAST_METHOD, {"maxWidth": 3840, "everyNthFrame": 1}),
            _command(3, "Page.enable"),
        ]
    )
    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: len(connection.sent) == 3)

    sent = [decode_frame(wire) for wire in connection.sent]
    start = next(f for f in sent if isinstance(f, CdpCommand) and f.method == START_SCREENCAST_METHOD)
    assert (start.params or {})["maxWidth"] == SCREENCAST_MAX_DIMENSION
    assert (start.params or {})["everyNthFrame"] == SCREENCAST_MIN_EVERY_NTH_FRAME
    assert (start.params or {})["quality"] == SCREENCAST_MAX_QUALITY
    # A neighbouring command on the same socket is relayed byte-identically.
    enable = next(f for f in sent if isinstance(f, CdpCommand) and f.method == "Page.enable")
    assert enable.params is None

    client.release()
    await asyncio.wait_for(task, timeout=5)


def test_the_ack_reads_the_frame_number_not_a_session_id() -> None:
    ack = screencast_frame_ack(_screencast_frame(42))

    assert ack is not None
    assert ack.method == SCREENCAST_FRAME_ACK_METHOD
    assert ack.params == {"sessionId": 42}
    assert ack.session_id == CDP_SESSION


@pytest.mark.parametrize("params", [None, {}, {"sessionId": "sess"}, {"sessionId": None}, {"sessionId": True}])
def test_an_unackable_frame_yields_no_ack(params: dict | None) -> None:
    """A frame number that is not an integer is not a frame number. `True` is an int to
    Python and would ack frame 1 — a frame the browser may genuinely be waiting on."""
    assert screencast_frame_ack(CdpEvent(method=SCREENCAST_FRAME_EVENT, params=params)) is None


@pytest.mark.asyncio
async def test_the_pinned_single_client_relay_consumes_the_proxy_acks_reply() -> None:
    """The pinned per-client relay hands frames straight to one client, so it has to
    consume the proxy lane's reply itself — the shared reader's routing is not there to do
    it. An unconsumed reply arrives as a response to whatever the client has in flight
    under that id, and the proxy lane allocates from the same id space precisely so that
    can never happen.

    No production caller today (the served path is the shared reader); this pins the
    contract so wiring it in later cannot quietly resurrect the trap.
    """
    from tests.unit.proxy.test_metrics_contract import _FakeClientWebSocket

    connection = _ControllableUpstream()
    server = _screencast_server(_SharedBrowser(connection), FakeClock())
    remapper = RequestIdRemapper()
    session = ProxySession(session_id="s1", upstream_ws_url=UPSTREAM_URL)
    ws = _FakeClientWebSocket("/s1")
    relay = asyncio.create_task(
        server._relay_upstream_to_client(ws, connection, session, remapper, {})  # type: ignore[arg-type]
    )
    # One frame over a spent budget, so the relay withholds it and pays the ack itself.
    for number in range(1, SCREENCAST_MAX_FRAMES_PER_SECOND + 2):
        connection.emit(_screencast_frame(number))
    # Settles once the ack has gone AND its reply has come back and been consumed (which
    # is what clears the pending mapping) — so this cannot pass by measuring too early.
    assert await _until(lambda: _acked_numbers(connection) == [11] and remapper.pending_count == 0)

    # The ack's reply came back on this same socket and stopped here: the client saw only
    # screencast frames, never a response to a command it never sent.
    sent = [decode_frame(wire) for wire in ws.sent]
    assert [f for f in sent if isinstance(f, CdpResponse)] == []
    assert len([f for f in sent if isinstance(f, CdpEvent)]) == SCREENCAST_MAX_FRAMES_PER_SECOND

    relay.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await relay


def test_the_capped_event_is_a_named_metric_label_not_bucketed_to_other() -> None:
    """A drop counter tagged cdp_method="other" cannot say the reduction came from the
    screencast stream. The pack's own method is allowlisted; the set stays bounded."""
    assert _cdp_method_tags(SCREENCAST_FRAME_EVENT) == (SCREENCAST_FRAME_EVENT, "Page")


def test_the_pack_caps_screencast_frames_and_nothing_else() -> None:
    """The blast radius, as data: one rule, on one event, never relaxed by interest.

    Interest-relaxation would void this cap outright — a screencast client enables Page
    like every other client, so `Page` being enabled says nothing about whether it asked
    for a screencast (Page.startScreencast does), and relaxing on it would exempt exactly
    the clients this bounds.
    """
    assert [rule.method for rule in SCREENCAST_PACK_V1.rules] == [SCREENCAST_FRAME_EVENT]
    rule = SCREENCAST_PACK_V1.rules[0]
    assert rule.max_per_window == SCREENCAST_MAX_FRAMES_PER_SECOND
    assert rule.window_seconds == 1.0
    assert rule.relax_when_enabled is False
    assert SCREENCAST_PACK_V1.version == 1


@pytest.mark.asyncio
async def test_the_default_proxy_neither_caps_nor_acks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filtering is opt-in. With no screencast policy the proxy is a pass-through and the
    client owns the ack loop exactly as it does without a proxy in the path."""
    from skyvern.proxy.__main__ import build_event_policy, build_pipeline

    connection = _ControllableUpstream()
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url=UPSTREAM_URL))
    server = CdpProxyServer(
        upstream=_SharedBrowser(connection),  # type: ignore[arg-type]
        sessions=sessions,
        auth=AllowAllAuth(),
        metrics=NoOpMetrics(),
        event_policy=build_event_policy("forward-all"),
        pipeline=build_pipeline("forward-all"),
    )
    client = _ScriptClient(
        [
            _command(1, "Target.attachToTarget", {"targetId": CDP_SESSION, "flatten": True}),
            _command(2, START_SCREENCAST_METHOD, {"maxWidth": 3840, "everyNthFrame": 1}),
        ]
    )
    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: len(connection.sent) == 2)

    for number in range(1, 101):
        connection.emit(_screencast_frame(number))
    assert await _until(lambda: len(client.events(SCREENCAST_FRAME_EVENT)) == 100)

    # Every frame delivered, no frame acked by us, and the client's own params reached
    # the browser exactly as it wrote them.
    assert _acks(connection) == []
    start = decode_frame(connection.sent[1])
    assert isinstance(start, CdpCommand)
    assert start.params == {"maxWidth": 3840, "everyNthFrame": 1}
    assert encode_frame(start) == encode_frame(
        CdpCommand(id=start.id, method=START_SCREENCAST_METHOD, params={"maxWidth": 3840, "everyNthFrame": 1})
    )

    client.release()
    await asyncio.wait_for(task, timeout=5)
