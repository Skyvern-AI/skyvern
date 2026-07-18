"""Shared upstream-connection ownership for the CDP proxy (SKY-12499).

The proxy owns ONE persistent upstream connection per browser (keyed by the
resolved upstream target), ref-counted across clients: it opens on the first
client and tears down when the last leaves. Two clients on one browser multiplex
the single connection — responses route back by the shared, client-keyed request-id
remapper (no cross-talk) and session-scoped events route by the flat-session
sessionId learned at attach. An upstream failure closes every sharing client
deterministically with no leaked tasks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from skyvern.proxy.adapters.memory import AllowAllAuth, ForwardAllEventPolicy, InMemorySessionRegistry, NoOpMetrics
from skyvern.proxy.adapters.websocket_server import (
    _MAX_PENDING_COMMANDS,
    _UPSTREAM_UNAVAILABLE_CLOSE_CODE,
    CdpProxyServer,
    _AttachIntent,
    _record_attach_intent,
    _SharedUpstream,
)
from skyvern.proxy.core.frames import CdpCommand, CdpEvent, CdpFrame, CdpResponse, decode_frame, encode_frame
from skyvern.proxy.core.pipeline import Direction, MiddlewarePipeline
from skyvern.proxy.core.session import ProxySession, ResolvedSession, UpstreamClosedError

UPSTREAM_URL = "ws://browser.internal/devtools/browser/shared"


class _ControllableUpstream:
    """One upstream connection: auto-answers commands (echoing the method, or a
    sessionId for an attach) and lets a test inject events or fail the socket."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[str] = []
        self.close_calls = 0
        self._closed = False

    async def send(self, raw: str) -> None:
        if self._closed:
            raise UpstreamClosedError("closed")
        self.sent.append(raw)
        frame = decode_frame(raw)
        if isinstance(frame, CdpCommand):
            if frame.method == "Target.attachToTarget":
                cdp_session_id = (frame.params or {}).get("targetId", "sess")
                self._inbox.put_nowait(encode_frame(CdpResponse(id=frame.id, result={"sessionId": cdp_session_id})))
            else:
                self._inbox.put_nowait(encode_frame(CdpResponse(id=frame.id, result={"echo": frame.method})))

    async def receive(self) -> str:
        if self._closed and self._inbox.empty():
            raise UpstreamClosedError("closed")
        item = await self._inbox.get()
        if item is None:
            raise UpstreamClosedError("closed")
        return item

    async def close(self) -> None:
        self.close_calls += 1
        self._closed = True
        self._inbox.put_nowait(None)

    def emit(self, frame: CdpFrame) -> None:
        self._inbox.put_nowait(encode_frame(frame))

    def fail(self) -> None:
        self._closed = True
        self._inbox.put_nowait(None)


class _SilentUpstream(_ControllableUpstream):
    """Never answers a command, so every request stays in flight — the oracle for
    'remap tables are empty after quiescence' (SKY-12500 AC5)."""

    async def send(self, raw: str) -> None:
        if self._closed:
            raise UpstreamClosedError("closed")
        self.sent.append(raw)


class _AttachEventUpstream(_ControllableUpstream):
    """Answers an explicit attach the way Chrome really does: the browser-level
    Target.attachedToTarget EVENT lands BEFORE the command response, so at event time
    the attach has not yet been correlated to the client that asked for it."""

    async def send(self, raw: str) -> None:
        frame = decode_frame(raw)
        if isinstance(frame, CdpCommand) and frame.method == "Target.attachToTarget":
            target_id = (frame.params or {}).get("targetId", "sess")
            self.sent.append(raw)
            self.emit(_attached_to_target(target_id, target_id=target_id))
            self._inbox.put_nowait(encode_frame(CdpResponse(id=frame.id, result={"sessionId": target_id})))
            return
        await super().send(raw)


class _SharedBrowser:
    """UpstreamBrowserPort that hands out one shared connection and counts dials."""

    def __init__(self, connection: _ControllableUpstream) -> None:
        self._connection = connection
        self.connect_calls = 0
        self.sessions: list[ProxySession] = []

    async def connect(self, session: ProxySession) -> _ControllableUpstream:
        self.connect_calls += 1
        self.sessions.append(session)
        return self._connection


class _RedialBrowser:
    """UpstreamBrowserPort that dials a FRESH connection each time (a re-dial after a
    dead connection must not reuse the corpse)."""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.connections: list[_ControllableUpstream] = []

    async def connect(self, session: ProxySession) -> _ControllableUpstream:
        self.connect_calls += 1
        connection = _ControllableUpstream()
        self.connections.append(connection)
        return connection


class _GatedBrowser:
    """UpstreamBrowserPort whose dial blocks until the test opens the gate — used to
    freeze a client mid-acquire and drive the single-flight/attach-seam races."""

    def __init__(self, connection: _ControllableUpstream) -> None:
        self._connection = connection
        self.connect_calls = 0
        self.gate = asyncio.Event()

    async def connect(self, session: ProxySession) -> _ControllableUpstream:
        self.connect_calls += 1
        await self.gate.wait()
        return self._connection


class _ScriptClient:
    """Client ws: yields its scripted frames, then stays live until released. A test
    can `feed` a further frame once the connection is live (e.g. after ownership of a
    session has been learned)."""

    def __init__(self, incoming: list[str]) -> None:
        self.request = SimpleNamespace(headers={}, path="/s1")
        self.sent: list[str] = []
        self.close_code: int | None = None
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        for raw in incoming:
            self._incoming.put_nowait(raw)

    def __aiter__(self) -> _ScriptClient:
        return self

    async def __anext__(self) -> str:
        raw = await self._incoming.get()
        if raw is None:
            raise StopAsyncIteration
        return raw

    def feed(self, raw: str) -> None:
        self._incoming.put_nowait(raw)

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.release()

    def release(self) -> None:
        self._incoming.put_nowait(None)

    def echoes(self) -> set[str]:
        methods = set()
        for wire in self.sent:
            frame = decode_frame(wire)
            if isinstance(frame, CdpResponse) and frame.result and "echo" in frame.result:
                methods.add(frame.result["echo"])
        return methods

    def event_sessions(self) -> set[str]:
        return {decode_frame(wire).session_id for wire in self.sent if isinstance(decode_frame(wire), CdpEvent)}

    def events(self, method: str) -> list[CdpEvent]:
        frames = (decode_frame(wire) for wire in self.sent)
        return [f for f in frames if isinstance(f, CdpEvent) and f.method == method]

    def errors(self) -> list[CdpResponse]:
        frames = (decode_frame(wire) for wire in self.sent)
        return [f for f in frames if isinstance(f, CdpResponse) and f.error is not None]


class _SlowDrainClient(_ScriptClient):
    """A client whose sends are slow, so its teardown drain lingers — widening the
    window in which a racing client could observe stale shared state."""

    async def send(self, raw: str) -> None:
        await asyncio.sleep(0.1)
        self.sent.append(raw)


def _server(browser: object, pipeline: MiddlewarePipeline | None = None) -> CdpProxyServer:
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url=UPSTREAM_URL))
    return CdpProxyServer(
        upstream=browser,  # type: ignore[arg-type]
        sessions=sessions,
        auth=AllowAllAuth(),
        metrics=NoOpMetrics(),
        event_policy=ForwardAllEventPolicy(),
        pipeline=pipeline,
    )


async def _until(predicate, timeout_ticks: int = 2000) -> bool:
    for _ in range(timeout_ticks):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


def _channel_count(server: CdpProxyServer) -> int:
    entry = server._shared_upstreams.get(UPSTREAM_URL)
    return len(entry.channels) if entry is not None else 0


def _command(client_id: int, method: str, params: dict | None = None) -> str:
    return encode_frame(CdpCommand(id=client_id, method=method, params=params))


@pytest.mark.asyncio
async def test_two_clients_share_a_single_upstream_connection() -> None:
    connection = _ControllableUpstream()
    browser = _SharedBrowser(connection)
    server = _server(browser)
    client_a = _ScriptClient([_command(1, "Page.enable")])
    client_b = _ScriptClient([_command(1, "Network.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.sent and client_b.sent)

    # One browser instance, dialed once, but both clients' commands reached it.
    assert browser.connect_calls == 1
    assert len(browser.sessions) == 1
    assert len(connection.sent) == 2

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)
    # Torn down only when the last client left — closed exactly once.
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_no_cross_talk_between_two_clients_on_the_shared_connection() -> None:
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    # Both clients use client id 1; only the shared remapper keeps them apart.
    client_a = _ScriptClient([_command(1, "Page.enable")])
    client_b = _ScriptClient([_command(1, "Network.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.echoes() and client_b.echoes())

    # Each client gets its own response back under its own id 1, never the other's.
    assert client_a.echoes() == {"Page.enable"}
    assert client_b.echoes() == {"Network.enable"}
    assert all(decode_frame(wire).id == 1 for wire in client_a.sent)
    assert all(decode_frame(wire).id == 1 for wire in client_b.sent)

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_flat_session_events_route_to_the_owning_client() -> None:
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "tA", "flatten": True})])
    client_b = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "tB", "flatten": True})])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    # Wait until both attach replies land (so both sessionId owners are learned).
    assert await _until(lambda: client_a.sent and client_b.sent)

    connection.emit(CdpEvent(method="Page.loadEventFired", session_id="tA"))
    connection.emit(CdpEvent(method="Page.loadEventFired", session_id="tB"))
    assert await _until(lambda: "tA" in client_a.event_sessions() and "tB" in client_b.event_sessions())

    # A session-scoped event reaches only the client that owns that sessionId.
    assert client_a.event_sessions() == {"tA"}
    assert client_b.event_sessions() == {"tB"}

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_upstream_failure_closes_every_sharing_client_without_leaks() -> None:
    connection = _ControllableUpstream()
    browser = _SharedBrowser(connection)
    server = _server(browser)
    client_a = _ScriptClient([_command(1, "Page.enable")])
    client_b = _ScriptClient([_command(1, "Network.enable")])

    before = set(asyncio.all_tasks())
    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.sent and client_b.sent)

    # The single shared upstream drops: both clients must tear down deterministically.
    connection.fail()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)

    assert connection.close_calls == 1  # one connection, closed once
    leaked = [t for t in set(asyncio.all_tasks()) - before if not t.done()]
    assert leaked == []  # no reader / relay / writer task leaked


@pytest.mark.asyncio
async def test_reader_death_evicts_the_entry_so_a_new_client_never_joins_a_dead_reader() -> None:
    # FIX 1: on reader death the pool entry must be evicted, so a client acquiring
    # during the (slow) teardown drain dials FRESH or is cleanly rejected — never
    # joins the dead reader and hangs. Without eviction the stale entry is reused and
    # the new client's send hits the corpse (no response) — the assertion below fails.
    browser = _RedialBrowser()
    server = _server(browser)
    client_a = _SlowDrainClient([_command(1, "Page.enable")])

    before = set(asyncio.all_tasks())
    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    # A is attached and its reader is live (its slow drain would delay client_a.sent).
    assert await _until(lambda: _channel_count(server) >= 1)
    browser.connections[0].fail()  # the shared upstream dies

    client_c = _ScriptClient([_command(1, "Runtime.enable")])
    task_c = asyncio.create_task(server._handle_client(client_c))  # type: ignore[arg-type]
    # C reaches a definitive outcome instead of hanging on the dead reader.
    assert await _until(
        lambda: client_c.echoes() or client_c.close_code == _UPSTREAM_UNAVAILABLE_CLOSE_CODE or task_c.done()
    )
    assert client_c.echoes() == {"Runtime.enable"} or client_c.close_code == _UPSTREAM_UNAVAILABLE_CLOSE_CODE

    client_c.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_c), timeout=5)
    # Fresh dial (a second connection) or a clean rejection — never a dead-reader join.
    assert browser.connect_calls == 2 or client_c.close_code == _UPSTREAM_UNAVAILABLE_CLOSE_CODE
    leaked = [t for t in set(asyncio.all_tasks()) - before if not t.done()]
    assert leaked == []


@pytest.mark.asyncio
async def test_cancel_at_the_attach_seam_releases_the_ref_and_evicts() -> None:
    # FIX 2: cancelling _handle_client after acquire (ref held) but at the attach seam
    # must still run the release path — no stuck refcount, no pinned entry, no leaked
    # socket/reader/channel. The pool lock is held here to freeze the client exactly at
    # _attach_client (which acquire has already passed), then it is cancelled.
    connection = _ControllableUpstream()
    browser = _GatedBrowser(connection)
    server = _server(browser)
    client = _ScriptClient([])

    before = set(asyncio.all_tasks())
    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: browser.connect_calls == 1)  # in acquire, ref held, dial in-flight
    await server._shared_upstreams_lock.acquire()  # the next attach will block on this
    browser.gate.set()  # dial completes; the client races to attach and blocks on the lock
    for _ in range(50):  # let it reach the attach block (the only place it can be)
        await asyncio.sleep(0)
    assert not client.sent  # never attached (would need the lock we hold)

    task.cancel()
    server._shared_upstreams_lock.release()  # let the teardown's release_shared proceed
    with pytest.raises(asyncio.CancelledError):
        await task

    assert server._shared_upstreams == {}  # entry evicted, ref returned to 0
    assert connection.close_calls == 1  # socket closed
    leaked = [t for t in set(asyncio.all_tasks()) - before if not t.done()]
    assert leaked == []


@pytest.mark.asyncio
async def test_cancelling_one_co_waiter_does_not_abort_the_shared_dial() -> None:
    # FIX 3: two first-clients wait on one single-flight dial; cancelling one must not
    # (via unshielded await) cancel the shared dial out from under the other.
    connection = _ControllableUpstream()
    browser = _GatedBrowser(connection)
    server = _server(browser)
    client_a = _ScriptClient([_command(1, "Page.enable")])
    client_b = _ScriptClient([_command(1, "Network.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: browser.connect_calls == 1)  # one dial, both waiting on it

    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a
    browser.gate.set()  # the dial the cancelled waiter shared must still complete

    assert await _until(lambda: client_b.echoes())  # B acquired a live connection
    assert client_b.echoes() == {"Network.enable"}
    assert browser.connect_calls == 1  # the dial was neither aborted nor retried

    client_b.release()
    await asyncio.wait_for(task_b, timeout=5)


def _auto_attach(client_id: int, session_id: str | None = None) -> str:
    return encode_frame(
        CdpCommand(
            id=client_id,
            method="Target.setAutoAttach",
            params={"autoAttach": True, "flatten": True, "waitForDebuggerOnStart": False},
            session_id=session_id,
        )
    )


def _attached_to_target(cdp_session_id: str, parent: str | None = None, target_id: str | None = None) -> CdpEvent:
    return CdpEvent(
        method="Target.attachedToTarget",
        params={"sessionId": cdp_session_id, "targetInfo": {"targetId": target_id or f"target-{cdp_session_id}"}},
        session_id=parent,
    )


@pytest.mark.asyncio
async def test_explicit_attach_notice_reaches_the_attaching_client() -> None:
    # Chrome announces an explicit Target.attachToTarget with a browser-level
    # attachedToTarget EVENT that lands before the response — so the owner is not yet
    # known from the reply. Clients build their session object from that event (it is
    # how a CDP client learns the session exists), so it must reach the attacher and
    # nobody else. Routing it by the in-flight attach is what makes that possible.
    connection = _AttachEventUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "tA", "flatten": True})])
    client_b = _ScriptClient([_command(1, "Page.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.events("Target.attachedToTarget") and client_b.echoes())

    assert client_b.events("Target.attachedToTarget") == []
    # And the co-tenant still never sees that session's traffic.
    connection.emit(CdpEvent(method="Page.loadEventFired", session_id="tA"))
    assert await _until(lambda: "tA" in client_a.event_sessions())
    assert client_b.event_sessions() == set()

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_autoattach_event_learns_the_owner_and_routes_only_to_it() -> None:
    # SKY-12500 (AC3): sessions arrive via autoAttach, not just explicit attach. The
    # client that enabled browser-level autoAttach owns what auto-attaches, so its
    # sessions' events must not reach the co-tenant on the shared connection.
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_auto_attach(1)])
    client_b = _ScriptClient([_command(1, "Page.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.sent and client_b.sent)

    connection.emit(_attached_to_target("S1"))
    connection.emit(CdpEvent(method="Page.loadEventFired", session_id="S1"))
    assert await _until(lambda: "S1" in client_a.event_sessions())

    # A owns S1: it alone sees the attach notice and the session's events.
    assert client_a.events("Target.attachedToTarget")
    assert client_b.events("Target.attachedToTarget") == []
    assert client_b.event_sessions() == set()

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_concurrent_attaches_to_one_target_match_their_own_announcements() -> None:
    # Two clients may attach to the SAME target (an agent and a human debugger on one
    # page): Chrome opens a separate session per attach and announces them in the order
    # asked. Each announcement must go to the client whose attach it answers.
    connection = _SilentUpstream()
    server = _server(_SharedBrowser(connection))
    attach = _command(1, "Target.attachToTarget", {"targetId": "shared-target", "flatten": True})
    client_a = _ScriptClient([attach])
    client_b = _ScriptClient([])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    assert await _until(lambda: len(connection.sent) == 1)  # A's attach is in flight first
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: _channel_count(server) == 2)
    client_b.feed(attach)
    assert await _until(lambda: len(connection.sent) == 2)

    connection.emit(_attached_to_target("S1", target_id="shared-target"))
    connection.emit(_attached_to_target("S2", target_id="shared-target"))
    assert await _until(
        lambda: client_a.events("Target.attachedToTarget") and client_b.events("Target.attachedToTarget")
    )

    # A asked first, so S1 is A's and S2 is B's — neither sees the other's session.
    assert [event.params["sessionId"] for event in client_a.events("Target.attachedToTarget")] == ["S1"]
    assert [event.params["sessionId"] for event in client_b.events("Target.attachedToTarget")] == ["S2"]

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


async def _two_clients(server: CdpProxyServer, first: _ScriptClient, second: _ScriptClient, sent: list[str]) -> tuple:
    """Attach `first`, wait until its opening command is on the wire, then attach
    `second` — so a test can pin the order the two clients' frames reach the browser."""
    task_first = asyncio.create_task(server._handle_client(first))  # type: ignore[arg-type]
    assert await _until(lambda: len(sent) == 1)
    task_second = asyncio.create_task(server._handle_client(second))  # type: ignore[arg-type]
    assert await _until(lambda: _channel_count(server) == 2)
    return task_first, task_second


@pytest.mark.asyncio
async def test_autoattach_announcement_is_not_stolen_by_a_concurrent_explicit_attach() -> None:
    # A runs browser-level autoAttach; B explicitly attaches the SAME target while A's
    # auto-attach announcement is still queued. Nothing in an announcement says which
    # attach it answers — but crediting it to B is the unrecoverable way to be wrong:
    # B's explicit attach has a response that fixes B's own session, while A's
    # auto-attach has none, so a wrong guess hands A's session to B forever.
    connection = _SilentUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_auto_attach(1)])
    client_b = _ScriptClient([])
    task_a, task_b = await _two_clients(server, client_a, client_b, connection.sent)
    client_b.feed(_command(1, "Target.attachToTarget", {"targetId": "T", "flatten": True}))
    assert await _until(lambda: len(connection.sent) == 2)
    shared = server._shared_upstreams[UPSTREAM_URL]
    owner_a = next(iter(shared.channels))  # A attached first

    # Chrome auto-attaches T for A; the announcement lands while B's attach is in flight.
    connection.emit(_attached_to_target("S_A", target_id="T"))
    assert await _until(lambda: "S_A" in shared.session_owner)

    assert shared.session_owner["S_A"] == owner_a
    connection.emit(CdpEvent(method="Page.loadEventFired", session_id="S_A"))
    assert await _until(lambda: "S_A" in client_a.event_sessions())
    assert client_b.event_sessions() == set()  # B never sees A's auto-attached session

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_failed_attach_leaves_no_intent_for_a_later_attach_to_consume() -> None:
    # A's attach ERRORS, so it is never announced. Its intent must not linger: the next
    # client to attach that same target would otherwise be paired with it and have its
    # session handed to A.
    connection = _SilentUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "T", "flatten": True})])
    client_b = _ScriptClient([])
    task_a, task_b = await _two_clients(server, client_a, client_b, connection.sent)

    failed_id = decode_frame(connection.sent[0]).id
    connection.emit(CdpResponse(id=failed_id, error={"code": -32602, "message": "No target with given id found"}))
    assert await _until(lambda: client_a.errors())

    client_b.feed(_command(1, "Target.attachToTarget", {"targetId": "T", "flatten": True}))
    assert await _until(lambda: len(connection.sent) == 2)
    connection.emit(_attached_to_target("S_B", target_id="T"))
    assert await _until(lambda: client_b.events("Target.attachedToTarget"))

    shared = server._shared_upstreams[UPSTREAM_URL]
    assert shared.session_owner["S_B"] == list(shared.channels)[1]  # B's own key
    assert client_a.events("Target.attachedToTarget") == []  # A's dead attach claims nothing

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_failed_autoattach_enable_does_not_leave_the_client_credited() -> None:
    # A's setAutoAttach errors, so autoAttach is NOT on for it; the optimistic record
    # must be undone or A would be credited with sessions it never asked to own.
    connection = _SilentUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_auto_attach(1)])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    assert await _until(lambda: len(connection.sent) == 1)
    shared = server._shared_upstreams[UPSTREAM_URL]
    assert shared.autoattach_clients  # recorded optimistically at send

    failed_id = decode_frame(connection.sent[0]).id
    connection.emit(CdpResponse(id=failed_id, error={"code": -32000, "message": "Not allowed"}))
    assert await _until(lambda: client_a.errors())

    assert shared.autoattach_clients == []
    assert shared.attach_intents == []

    client_a.release()
    await asyncio.wait_for(task_a, timeout=5)


def _disable_auto_attach(client_id: int) -> str:
    return encode_frame(CdpCommand(id=client_id, method="Target.setAutoAttach", params={"autoAttach": False}))


@pytest.mark.asyncio
async def test_failed_autoattach_disable_restores_the_client_credit() -> None:
    # A enables autoAttach, then asks to disable it, but the browser REJECTS the disable —
    # so autoAttach is still live upstream. The optimistic removal must be undone or A's
    # later auto-attach announcements would be dropped/misrouted while the proxy believes
    # A opted out.
    connection = _SilentUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_auto_attach(1)])
    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    assert await _until(lambda: len(connection.sent) == 1)
    shared = server._shared_upstreams[UPSTREAM_URL]
    owner_a = next(iter(shared.channels))
    assert shared.autoattach_clients == [owner_a]  # enabled optimistically at send

    client_a.feed(_disable_auto_attach(2))
    assert await _until(lambda: len(connection.sent) == 2)
    assert shared.autoattach_clients == []  # removed optimistically at send

    disable_id = decode_frame(connection.sent[1]).id
    connection.emit(CdpResponse(id=disable_id, error={"code": -32000, "message": "Not allowed"}))
    assert await _until(lambda: client_a.errors())

    assert shared.autoattach_clients == [owner_a]  # restored: the disable never took effect

    client_a.release()
    await asyncio.wait_for(task_a, timeout=5)


async def _drop_autoattach_error(frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame | None:
    if direction is Direction.UPSTREAM_TO_CLIENT and isinstance(frame, CdpResponse) and frame.error is not None:
        return None
    return frame


@pytest.mark.asyncio
async def test_dropped_autoattach_enable_error_does_not_leave_the_client_credited() -> None:
    # An upstream middleware SUPPRESSES the rejected setAutoAttach response. The dropped
    # frame still carries the error, so the intent must settle as a failure — otherwise A
    # stays credited and the next browser-level attach is handed to it.
    connection = _SilentUpstream()
    server = _server(_SharedBrowser(connection), pipeline=MiddlewarePipeline([_drop_autoattach_error]))
    client_a = _ScriptClient([_auto_attach(1)])
    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    assert await _until(lambda: len(connection.sent) == 1)
    shared = server._shared_upstreams[UPSTREAM_URL]
    assert shared.autoattach_clients  # recorded optimistically at send

    enable_id = decode_frame(connection.sent[0]).id
    connection.emit(CdpResponse(id=enable_id, error={"code": -32000, "message": "Not allowed"}))
    # The error is suppressed, so A never receives it; wait for the intent to settle.
    assert await _until(lambda: shared.attach_intents == [])
    assert client_a.errors() == []  # the response was dropped, not delivered

    assert shared.autoattach_clients == []  # settled as a failure despite the suppressed response

    client_a.release()
    await asyncio.wait_for(task_a, timeout=5)


@pytest.mark.asyncio
async def test_ambiguous_attach_announcement_is_withheld_from_the_autoattach_client() -> None:
    # A runs browser-level autoAttach; B explicitly attaches the SAME target. The
    # announcement that arrives is indistinguishable — it could be A's auto-attach or B's
    # explicit attach — so it is withheld from BOTH (fail-closed) rather than delivered to
    # A across the tenant boundary. Durable ownership still goes to A (its auto-attach has
    # no response to self-correct); delivering it correctly needs the interception seam.
    connection = _SilentUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_auto_attach(1)])
    client_b = _ScriptClient([])
    task_a, task_b = await _two_clients(server, client_a, client_b, connection.sent)
    client_b.feed(_command(1, "Target.attachToTarget", {"targetId": "T", "flatten": True}))
    assert await _until(lambda: len(connection.sent) == 2)
    shared = server._shared_upstreams[UPSTREAM_URL]
    owner_a = next(iter(shared.channels))  # A attached first

    connection.emit(_attached_to_target("S_A", target_id="T"))
    # A owns S_A durably, so a following session-scoped event IS delivered to A; the queue
    # is FIFO, so once A sees it the announcement would already be visible had it been sent.
    connection.emit(CdpEvent(method="Page.loadEventFired", session_id="S_A"))
    assert await _until(lambda: "S_A" in client_a.event_sessions())

    assert shared.session_owner["S_A"] == owner_a  # durable ownership still A's
    assert client_a.events("Target.attachedToTarget") == []  # announcement withheld — could be B's attach
    assert client_b.events("Target.attachedToTarget") == []  # and never crosses to the autoAttach client

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


def _bare_shared() -> _SharedUpstream:
    return _SharedUpstream(session=ProxySession(session_id="s1", upstream_ws_url=UPSTREAM_URL))


def test_attach_intent_eviction_drops_the_callers_own_oldest_not_a_co_tenants() -> None:
    # At the cap, a flooding client reclaims a slot at its OWN expense (its oldest intent),
    # never a co-tenant's unreconciled announcement — mirrors the remapper's owner-scoped
    # _evict_one_for. The GLOBAL oldest here belongs to a co-tenant, so a naive pop(0)
    # would drop the victim's intent.
    shared = _bare_shared()
    shared.attach_intents.append(_AttachIntent(0, "victim", "v-oldest", False, False))  # global oldest, a co-tenant's
    shared.attach_intents.append(_AttachIntent(1, "flooder", "own-old", False, False))  # flooder's oldest, not index 0
    for i in range(2, _MAX_PENDING_COMMANDS):
        shared.attach_intents.append(_AttachIntent(i, "victim", f"v{i}", False, False))
    assert len(shared.attach_intents) == _MAX_PENDING_COMMANDS

    _record_attach_intent(shared, _AttachIntent(9999, "flooder", "own-new", False, False))

    targets = [intent.target_id for intent in shared.attach_intents]
    assert [intent.client_key for intent in shared.attach_intents].count("victim") == _MAX_PENDING_COMMANDS - 1
    assert "v-oldest" in targets  # the co-tenant's oldest was NOT evicted
    assert "own-old" not in targets  # the flooder's OWN oldest was evicted instead
    assert "own-new" in targets  # its new intent was recorded


@pytest.mark.asyncio
async def test_client_cannot_detach_a_co_tenants_session_addressed_via_params() -> None:
    # Target.detachFromTarget names its session in params, not the frame's sessionId —
    # so the top-level ownership gate alone would let B detach A's session.
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "tA", "flatten": True})])
    client_b = _ScriptClient([_command(1, "Page.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.sent and client_b.echoes())
    shared = server._shared_upstreams[UPSTREAM_URL]
    assert await _until(lambda: "tA" in shared.session_owner)

    client_b.feed(encode_frame(CdpCommand(id=2, method="Target.detachFromTarget", params={"sessionId": "tA"})))
    assert await _until(lambda: client_b.errors())

    assert client_b.errors()[0].id == 2
    upstream_methods = [f.method for f in map(decode_frame, connection.sent) if isinstance(f, CdpCommand)]
    assert "Target.detachFromTarget" not in upstream_methods  # never reached the browser
    assert "tA" in shared.session_owner  # and A still owns its session

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_nested_autoattach_inherits_the_parent_session_owner() -> None:
    # An auto-attach nested under an owned session (an iframe/worker of A's page)
    # belongs to that session's owner.
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "tA", "flatten": True})])
    client_b = _ScriptClient([_command(1, "Page.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.sent and client_b.sent)

    connection.emit(_attached_to_target("child", parent="tA"))
    connection.emit(CdpEvent(method="Runtime.consoleAPICalled", session_id="child"))
    assert await _until(lambda: "child" in client_a.event_sessions())

    assert client_b.event_sessions() == set()

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_session_scoped_event_for_an_unowned_session_is_never_broadcast() -> None:
    # SKY-12500 (AC1/AC3): before 12500 an event whose sessionId had no known owner
    # fanned out to EVERY client. A session-scoped event must never reach a non-owner;
    # with no owner to route to it is dropped, not broadcast.
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_command(1, "Page.enable")])
    client_b = _ScriptClient([_command(1, "Network.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.echoes() and client_b.echoes())

    connection.emit(CdpEvent(method="Page.loadEventFired", session_id="nobody"))
    # Let the reader route it, then prove it landed nowhere.
    connection.emit(CdpEvent(method="Target.targetCreated", params={"targetInfo": {}}))
    assert await _until(lambda: client_a.events("Target.targetCreated") and client_b.events("Target.targetCreated"))

    assert client_a.event_sessions() == {None}  # the browser-level event only
    assert client_b.event_sessions() == {None}

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_detach_event_releases_the_session_owner() -> None:
    # SKY-12500: ownership is GC'd on detach, so a long-lived client's attach/detach
    # churn cannot grow session_owner without bound.
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_auto_attach(1)])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.sent)
    shared = server._shared_upstreams[UPSTREAM_URL]

    connection.emit(_attached_to_target("S1"))
    assert await _until(lambda: "S1" in shared.session_owner)

    connection.emit(CdpEvent(method="Target.detachedFromTarget", params={"sessionId": "S1"}))
    # The owner still learns its session went away, and the entry is gone with it.
    assert await _until(lambda: client_a.events("Target.detachedFromTarget"))
    assert "S1" not in shared.session_owner

    client_a.release()
    await asyncio.wait_for(task_a, timeout=5)


@pytest.mark.asyncio
async def test_client_cannot_drive_another_clients_session() -> None:
    # SKY-12500 (AC2/AC3): the uplink is ownership-checked — B must not steer A's
    # session, and must fail deterministically rather than hang.
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "tA", "flatten": True})])
    client_b = _ScriptClient([_command(1, "Page.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.sent and client_b.echoes())
    shared = server._shared_upstreams[UPSTREAM_URL]
    assert await _until(lambda: "tA" in shared.session_owner)

    client_b.feed(encode_frame(CdpCommand(id=2, method="Page.navigate", session_id="tA")))
    assert await _until(lambda: client_b.errors())

    # B is told no; the hijack never reached the browser.
    assert client_b.errors()[0].id == 2
    upstream_sessions = [decode_frame(w).session_id for w in connection.sent]
    assert "tA" not in upstream_sessions

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_remap_tables_are_empty_after_quiescence() -> None:
    # SKY-12500 (AC5): commands still in flight when a client leaves must not pin
    # remapper entries, and its learned session owners go with it.
    connection = _SilentUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient(
        [
            _command(1, "Page.enable"),
            _command(2, "Runtime.enable"),
            _command(3, "Target.attachToTarget", {"targetId": "never-answered"}),
            _auto_attach(4),
        ]
    )
    client_b = _ScriptClient([_command(1, "Network.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: len(connection.sent) == 5)  # all in flight, none answered
    shared = server._shared_upstreams[UPSTREAM_URL]
    assert shared.remapper.pending_count == 5
    assert shared.attach_intents and shared.autoattach_clients

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)

    # Every table keyed by a client is empty once they are all gone.
    assert shared.remapper.pending_count == 0
    assert shared.session_owner == {}
    assert shared.attach_intents == []
    assert shared.autoattach_clients == []
    assert shared.channels == {}
    assert server._shared_upstreams == {}


@pytest.mark.asyncio
async def test_pending_mappings_stay_bounded_under_a_silent_upstream() -> None:
    # AC5's steady-state half: one long-lived client whose commands are never
    # answered must not grow the remapper without bound.
    connection = _SilentUpstream()
    server = _server(_SharedBrowser(connection))
    server._max_pending_requests = 8
    client = _ScriptClient([_command(client_id, "Page.enable") for client_id in range(1, 40)])

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: len(connection.sent) == 39)
    shared = server._shared_upstreams[UPSTREAM_URL]

    assert shared.remapper.pending_count == 8

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_many_clients_and_sessions_show_zero_cross_talk() -> None:
    # SKY-12500 (AC1): the concurrency oracle. Every client uses the SAME client-side
    # ids; only the shared remapper and the ownership map keep them apart.
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    client_count, per_client = 8, 10
    clients = [
        _ScriptClient(
            [_command(1, "Target.attachToTarget", {"targetId": f"t{index}", "flatten": True})]
            + [_command(client_id, f"c{index}.m{client_id}") for client_id in range(2, per_client + 2)]
        )
        for index in range(client_count)
    ]

    tasks = [asyncio.create_task(server._handle_client(client)) for client in clients]  # type: ignore[arg-type]
    assert await _until(lambda: all(len(client.echoes()) == per_client for client in clients))

    for index in range(client_count):
        connection.emit(CdpEvent(method="Page.loadEventFired", session_id=f"t{index}"))
    assert await _until(lambda: all(f"t{index}" in clients[index].event_sessions() for index in range(client_count)))

    for index, client in enumerate(clients):
        # Only this client's own responses, under its own ids, and only its session.
        assert client.echoes() == {f"c{index}.m{client_id}" for client_id in range(2, per_client + 2)}
        assert client.event_sessions() == {f"t{index}"}
        assert {decode_frame(wire).id for wire in client.sent if isinstance(decode_frame(wire), CdpResponse)} == set(
            range(1, per_client + 2)
        )

    for client in clients:
        client.release()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


@pytest.mark.asyncio
async def test_departing_client_clears_its_session_owner_entries() -> None:
    # FIX 4: a client's learned sessionId -> client_key entries are dropped on its
    # teardown (the shared connection stays alive for the other client).
    connection = _ControllableUpstream()
    server = _server(_SharedBrowser(connection))
    client_a = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "tA", "flatten": True})])
    client_b = _ScriptClient([_command(1, "Page.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.sent and client_b.sent)
    shared = server._shared_upstreams[UPSTREAM_URL]
    assert "tA" in shared.session_owner

    client_a.release()
    await asyncio.wait_for(task_a, timeout=5)
    assert "tA" not in shared.session_owner  # cleared when A left; B keeps the connection alive

    client_b.release()
    await asyncio.wait_for(task_b, timeout=5)
