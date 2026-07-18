from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from skyvern.proxy.adapters.memory import ForwardAllEventPolicy
from skyvern.proxy.core.frames import (
    KNOWN_CDP_DOMAINS,
    LIFECYCLE_EVENTS,
    TARGET_ATTACHED_EVENT,
    TARGET_DETACHED_EVENT,
    CdpCommand,
    CdpEvent,
    decode_frame,
    encode_frame,
)
from skyvern.proxy.core.policy import (
    _MAX_TRACKED_CHILD_SESSIONS,
    FORWARD,
    Drop,
    DropReason,
    EventPolicyConfig,
    EventPolicyEngine,
    Forward,
    RateRule,
    Rewrite,
)
from skyvern.proxy.core.session import Principal, ProxySession

# Reusing the shared-connection harness rather than pasting a fourth copy of a proxy
# server double: these two tests must exercise the very routing/ownership flow those
# tests pin, so they have to run against the same fakes.
from tests.unit.proxy.test_shared_connection import (
    _AttachEventUpstream,
    _command,
    _ControllableUpstream,
    _ScriptClient,
    _server,
    _SharedBrowser,
    _until,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SESSION_ID = "s_1"
ORG_ID = "o_1"


class FakeClock:
    """Injected time source: tests advance it explicitly, so a throttle window is
    decided by the test rather than by how long the test took to run."""

    def __init__(self, now: float = 1000.0) -> None:
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _session(session_id: str = SESSION_ID) -> ProxySession:
    return ProxySession(
        session_id=session_id,
        upstream_ws_url="ws://upstream.internal:9222/x?token=secret-token",
        principal=Principal(principal_id=ORG_ID, organization_id=ORG_ID),
        connect_headers={"authorization": "Bearer secret-token"},
    )


def _engine(config: EventPolicyConfig | None = None, clock: FakeClock | None = None) -> EventPolicyEngine:
    return EventPolicyEngine(config=config or EventPolicyConfig(), clock=clock or FakeClock())


def _recorded_events() -> list[CdpEvent]:
    events: list[CdpEvent] = []
    for fixture in sorted(FIXTURES_DIR.glob("*.jsonl")):
        for line in fixture.read_text().splitlines():
            if not line.strip():
                continue
            frame = decode_frame(line)
            if isinstance(frame, CdpEvent):
                events.append(frame)
    return events


# --- AC2: no policy configured -> the engine is a transparent no-op -------------


def test_no_policy_forwards_every_recorded_event_unchanged() -> None:
    engine = _engine()
    session = _session()
    events = _recorded_events()

    assert events, "fixtures must contain events for this to prove anything"
    for event in events:
        assert engine.decide(event, session) is FORWARD


def test_no_policy_leaves_wire_bytes_byte_identical() -> None:
    """AC2: with no policy, the bytes the call site would deliver are byte-for-byte
    what it delivers today. Forward carries no payload, so the call site keeps its
    own already-processed frame and its existing encode path — the engine cannot
    perturb the wire even in principle."""
    engine = _engine()
    session = _session()

    for event in _recorded_events():
        baseline = encode_frame(event)
        decision = engine.decide(event, session)

        assert isinstance(decision, Forward)
        assert encode_frame(event) == baseline


def test_forward_is_a_singleton_carrying_no_payload() -> None:
    engine = _engine()
    event = CdpEvent(method="Page.loadEventFired", params={})

    assert engine.decide(event, _session()) is FORWARD
    assert not hasattr(FORWARD, "event")


# --- AC3: throttle, on an injected clock ---------------------------------------


def test_throttle_forwards_up_to_the_cap_then_drops_within_the_window() -> None:
    clock = FakeClock()
    config = EventPolicyConfig(
        rules=(RateRule.throttle(method="Network.dataReceived", max_per_window=2, window_seconds=10.0),)
    )
    engine = _engine(config, clock)
    session = _session()
    event = CdpEvent(method="Network.dataReceived", params={"encodedDataLength": 1})

    decisions = [engine.decide(event, session) for _ in range(5)]

    assert decisions[0] is FORWARD
    assert decisions[1] is FORWARD
    assert all(decision == Drop(DropReason.THROTTLED) for decision in decisions[2:])


def test_throttle_window_reopens_only_after_the_clock_advances() -> None:
    clock = FakeClock()
    config = EventPolicyConfig(
        rules=(RateRule.throttle(method="Network.dataReceived", max_per_window=1, window_seconds=10.0),)
    )
    engine = _engine(config, clock)
    session = _session()
    event = CdpEvent(method="Network.dataReceived", params={})

    assert engine.decide(event, session) is FORWARD
    assert engine.decide(event, session) == Drop(DropReason.THROTTLED)

    clock.advance(9.9)
    assert engine.decide(event, session) == Drop(DropReason.THROTTLED)

    clock.advance(0.1)
    assert engine.decide(event, session) is FORWARD


def test_throttle_does_not_touch_other_methods() -> None:
    config = EventPolicyConfig(
        rules=(RateRule.throttle(method="Network.dataReceived", max_per_window=1, window_seconds=10.0),)
    )
    engine = _engine(config)
    session = _session()

    engine.decide(CdpEvent(method="Network.dataReceived", params={}), session)

    for _ in range(10):
        assert engine.decide(CdpEvent(method="Page.loadEventFired", params={}), session) is FORWARD


def test_throttle_budget_is_per_session_not_global() -> None:
    """Two sessions must not spend each other's budget — a busy tenant would
    otherwise throttle a quiet co-tenant on the same shared upstream."""
    config = EventPolicyConfig(
        rules=(RateRule.throttle(method="Network.dataReceived", max_per_window=1, window_seconds=10.0),)
    )
    engine = _engine(config)
    event = CdpEvent(method="Network.dataReceived", params={})
    first, second = _session("s_1"), _session("s_2")

    assert engine.decide(event, first) is FORWARD
    assert engine.decide(event, first) == Drop(DropReason.THROTTLED)
    assert engine.decide(event, second) is FORWARD


def test_throttle_keys_split_the_budget_by_param() -> None:
    config = EventPolicyConfig(
        rules=(
            RateRule.throttle(
                method="Network.dataReceived", max_per_window=1, window_seconds=10.0, key_params=("requestId",)
            ),
        )
    )
    engine = _engine(config)
    session = _session()

    assert engine.decide(CdpEvent(method="Network.dataReceived", params={"requestId": "a"}), session) is FORWARD
    assert engine.decide(CdpEvent(method="Network.dataReceived", params={"requestId": "a"}), session) == Drop(
        DropReason.THROTTLED
    )
    assert engine.decide(CdpEvent(method="Network.dataReceived", params={"requestId": "b"}), session) is FORWARD


# --- no coalesce: a rate cap is first-wins, and says so ------------------------


def test_a_rate_cap_is_first_wins_and_there_is_no_coalesce_claiming_otherwise() -> None:
    """A budget of one per window delivers the FIRST event of a burst and drops the
    newer ones — so it must never be dressed up as 'collapse to the latest'. Latest-wins
    coalescing cannot be decided per-event and is deferred to SKY-12501; this pins that
    the misleading shape is not back.
    """
    engine = _engine(EventPolicyConfig(rules=(RateRule.throttle("Target.targetInfoChanged", 1, 5.0),)))
    session = _session()
    burst = [CdpEvent(method="Target.targetInfoChanged", params={"targetInfo": {"n": n}}) for n in range(5)]

    decisions = [engine.decide(event, session) for event in burst]

    # The oldest survives; that is a rate cap, not coalescing.
    assert decisions[0] is FORWARD
    assert all(decision == Drop(DropReason.THROTTLED) for decision in decisions[1:])
    assert not hasattr(RateRule, "coalesce")
    assert not any(reason.name == "COALESCED" for reason in DropReason)


# --- carry-forward #11: lifecycle events are structurally non-droppable --------


@pytest.mark.parametrize("method", sorted(LIFECYCLE_EVENTS))
def test_lifecycle_events_cannot_be_dropped_by_any_rule(method: str) -> None:
    """SKY-12500 learns session ownership from these two events downstream of the
    policy gate. A policy that dropped one would skip the ownership transition and
    strand the session's owner, so the engine must not be able to express it."""
    config = EventPolicyConfig(rules=(RateRule.throttle(method=method, max_per_window=0, window_seconds=10.0),))
    engine = _engine(config)
    session = _session()
    event = CdpEvent(method=method, params={"sessionId": "cdp_1"})

    for _ in range(10):
        assert engine.decide(event, session) is FORWARD


def test_a_zero_budget_rule_still_drops_an_ordinary_event() -> None:
    """Guards the test above: a max_per_window=0 rule really does drop, so the
    lifecycle result proves non-droppability rather than a broken rule."""
    config = EventPolicyConfig(
        rules=(RateRule.throttle(method="Network.dataReceived", max_per_window=0, window_seconds=10.0),)
    )
    engine = _engine(config)

    assert engine.decide(CdpEvent(method="Network.dataReceived", params={}), _session()) == Drop(DropReason.THROTTLED)


def test_lifecycle_events_are_exactly_the_ownership_events() -> None:
    assert LIFECYCLE_EVENTS == frozenset({TARGET_ATTACHED_EVENT, TARGET_DETACHED_EVENT})


# --- per-session domain enablement (consumed by SKY-12501) ---------------------


def test_domain_starts_disabled_and_tracks_enable_then_disable() -> None:
    engine = _engine()
    session = _session()

    assert not engine.is_domain_enabled(session, "Network")

    engine.observe_command(CdpCommand(id=1, method="Network.enable"), session)
    assert engine.is_domain_enabled(session, "Network")

    engine.observe_command(CdpCommand(id=2, method="Network.disable"), session)
    assert not engine.is_domain_enabled(session, "Network")


def test_domain_enablement_is_tracked_per_session() -> None:
    engine = _engine()
    first, second = _session("s_1"), _session("s_2")

    engine.observe_command(CdpCommand(id=1, method="Network.enable"), first)

    assert engine.is_domain_enabled(first, "Network")
    assert not engine.is_domain_enabled(second, "Network")


def test_non_enable_commands_do_not_enable_a_domain() -> None:
    engine = _engine()
    session = _session()

    engine.observe_command(CdpCommand(id=1, method="Network.getCookies"), session)
    engine.observe_command(CdpCommand(id=2, method="Runtime.evaluate", params={"expression": "1"}), session)

    assert not engine.is_domain_enabled(session, "Network")
    assert not engine.is_domain_enabled(session, "Runtime")


def test_interest_is_tracked_per_child_session_not_per_connection() -> None:
    """A client enables a domain on ONE attached page. Keying interest by the outer
    session would let a disable on a sibling child clear it, and a rule reading this
    would then mis-drop that page's traffic while it is still subscribed.
    """
    engine = _engine()
    session = _session()

    engine.observe_command(CdpCommand(id=1, method="Network.enable", session_id="child_a"), session)
    engine.observe_command(CdpCommand(id=2, method="Network.disable", session_id="child_b"), session)

    assert engine.is_domain_enabled(session, "Network", "child_a")
    assert not engine.is_domain_enabled(session, "Network", "child_b")


def test_browser_level_interest_is_distinct_from_a_child_session() -> None:
    engine = _engine()
    session = _session()

    engine.observe_command(CdpCommand(id=1, method="Target.enable"), session)

    assert engine.is_domain_enabled(session, "Target")
    assert not engine.is_domain_enabled(session, "Target", "child_a")


def test_detaching_a_child_retires_its_interest() -> None:
    engine = _engine()
    session = _session()
    engine.observe_command(CdpCommand(id=1, method="Network.enable", session_id="child_a"), session)

    engine.decide(CdpEvent(method=TARGET_DETACHED_EVENT, params={"sessionId": "child_a"}), session)

    assert not engine.is_domain_enabled(session, "Network", "child_a")
    assert engine.tracked_children(session) == 0


def test_detaching_one_child_leaves_a_siblings_interest_alone() -> None:
    engine = _engine()
    session = _session()
    engine.observe_command(CdpCommand(id=1, method="Network.enable", session_id="child_a"), session)
    engine.observe_command(CdpCommand(id=2, method="Network.enable", session_id="child_b"), session)

    engine.decide(CdpEvent(method=TARGET_DETACHED_EVENT, params={"sessionId": "child_b"}), session)

    assert engine.is_domain_enabled(session, "Network", "child_a")
    assert not engine.is_domain_enabled(session, "Network", "child_b")


# --- interest tracking is bounded on both axes ---------------------------------


def test_an_invented_domain_is_never_tracked() -> None:
    """`<anything>.enable` is client input. Only real CDP domains count as interest,
    which is also what bounds the set."""
    engine = _engine()
    session = _session()

    for n in range(5_000):
        engine.observe_command(CdpCommand(id=n, method=f"NotADomain{n}.enable"), session)

    assert not engine.is_domain_enabled(session, "NotADomain0")
    assert engine.tracked_children(session) == 0


def test_enabled_domains_stay_bounded_by_the_protocol_surface() -> None:
    engine = _engine()
    session = _session()

    for n, domain in enumerate(sorted(KNOWN_CDP_DOMAINS) * 3):
        engine.observe_command(CdpCommand(id=n, method=f"{domain}.enable"), session)

    assert engine.tracked_children(session) == 1
    assert engine.enabled_domain_count(session) == len(KNOWN_CDP_DOMAINS)


def test_child_session_churn_stays_bounded() -> None:
    """A client may address a sessionId that was never attached, so the child id is
    untrusted input and cannot grow this table without limit."""
    engine = _engine()
    session = _session()

    for n in range(5_000):
        engine.observe_command(CdpCommand(id=n, method="Network.enable", session_id=f"fabricated_{n}"), session)

    assert engine.tracked_children(session) <= _MAX_TRACKED_CHILD_SESSIONS


def test_forget_session_releases_its_state() -> None:
    """Per-session state must not outlive the session, or a long-lived proxy leaks
    one entry per session it has ever seen."""
    config = EventPolicyConfig(
        rules=(RateRule.throttle(method="Network.dataReceived", max_per_window=1, window_seconds=10.0),)
    )
    engine = _engine(config)
    session = _session()
    engine.observe_command(CdpCommand(id=1, method="Network.enable"), session)
    engine.decide(CdpEvent(method="Network.dataReceived", params={}), session)

    assert engine.tracked_sessions() == 1

    engine.forget(session.session_id)

    assert engine.tracked_sessions() == 0
    assert not engine.is_domain_enabled(session, "Network")
    assert engine.decide(CdpEvent(method="Network.dataReceived", params={}), session) is FORWARD


def test_forget_is_idempotent_for_an_unknown_session() -> None:
    engine = _engine()

    engine.forget("never-seen")

    assert engine.tracked_sessions() == 0


# --- decision traces: bounded tags, no credentials -----------------------------


def test_drop_reasons_are_a_closed_bounded_set() -> None:
    """Decision reasons become metric tags; a free-form reason would blow up label
    cardinality (SKY-12510), so the type itself has to bound them."""
    assert len(DropReason) <= 8
    for reason in DropReason:
        assert reason.value.replace("_", "").isalnum()


def test_decision_traces_never_carry_credentials_or_urls() -> None:
    config = EventPolicyConfig(
        rules=(RateRule.throttle(method="Network.dataReceived", max_per_window=0, window_seconds=10.0),)
    )
    engine = _engine(config)
    session = _session()
    event = CdpEvent(method="Network.dataReceived", params={"url": "https://host.example/p?token=secret-token"})

    decision = engine.decide(event, session)

    assert isinstance(decision, Drop)
    rendered = repr(decision) + decision.reason.value
    for leak in ("secret-token", "upstream.internal", "host.example", "Bearer", "ws://", "https://"):
        assert leak not in rendered


def test_rewrite_carries_the_replacement_event() -> None:
    """The Rewrite variant exists for SKY-12501/12538 (synthesized errors); the
    engine ships the type, not the rules that return it."""
    replacement = CdpEvent(method="Page.loadEventFired", params={"redacted": True})
    decision = Rewrite(replacement)

    assert decision.event is replacement


# --- AC1: the no-policy path is allocation-light -------------------------------


def test_no_policy_decide_is_allocation_light_over_a_large_stream() -> None:
    """AC1 smell test, not a microbenchmark: the default path must not allocate a
    decision per event. FORWARD is a singleton, so a long stream adds no objects."""
    engine = _engine()
    session = _session()
    event = CdpEvent(method="Network.dataReceived", params={"encodedDataLength": 1})

    decisions = {id(engine.decide(event, session)) for _ in range(10_000)}

    assert decisions == {id(FORWARD)}


def test_engine_keeps_up_with_a_high_volume_stream() -> None:
    """AC1: a coarse ceiling that catches an accidental O(rules) rescan or a
    per-event parse. Generous enough not to flake on a loaded CI box."""
    config = EventPolicyConfig(
        rules=(RateRule.throttle(method="Network.dataReceived", max_per_window=100, window_seconds=1.0),)
    )
    engine = _engine(config)
    session = _session()
    events = [CdpEvent(method="Network.dataReceived", params={"n": n}) for n in range(20_000)]

    started = time.perf_counter()
    for event in events:
        engine.decide(event, session)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0


# --- wired into the proxy: the two properties that must survive the engine --------


def _engine_policy(*rules: RateRule) -> EventPolicyEngine:
    return EventPolicyEngine(config=EventPolicyConfig(rules=rules), clock=FakeClock())


@pytest.mark.asyncio
async def test_engine_with_no_policy_delivers_the_same_bytes_as_forward_all() -> None:
    """AC2, end to end: swap the default ForwardAll for an unconfigured engine and the
    client's wire bytes are unchanged. This is the property the whole ticket rests on —
    turning the engine on must be invisible until a rule is configured."""

    # Browser-level (no sessionId), so every one reaches the lone client instead of
    # being dropped as unowned — the recorded events are replayed at browser scope for
    # exactly that reason. The non-ASCII frame is the case where the codec's
    # normalization actually bites, so it is the one most likely to expose a Forward
    # path that re-serializes.
    # Lifecycle events are excluded: they route by ownership rather than broadcast, so
    # a lone client with nothing attached would never receive them. The test above
    # covers them directly.
    stream = [
        CdpEvent(method=event.method, params=event.params)
        for event in _recorded_events()
        if event.method not in LIFECYCLE_EVENTS
    ]
    stream.append(CdpEvent(method="Runtime.consoleAPICalled", params={"text": "café ünicode"}))
    sentinel = CdpEvent(method="Runtime.executionContextsCleared", params={})

    async def deliver(policy: object) -> list[str]:
        connection = _ControllableUpstream()
        server = _server(_SharedBrowser(connection))
        server._event_policy = policy  # type: ignore[assignment]
        client = _ScriptClient([_command(1, "Page.enable")])
        task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
        assert await _until(lambda: bool(client.sent))
        for event in stream:
            connection.emit(event)
        connection.emit(sentinel)
        assert await _until(lambda: any(sentinel.method in raw for raw in client.sent))
        client.release()
        await asyncio.wait_for(task, timeout=5)
        return client.sent

    baseline = await deliver(ForwardAllEventPolicy())
    with_engine = await deliver(_engine_policy())

    assert with_engine == baseline
    # Guards against a vacuous pass: every event really did reach the client, so the
    # comparison covers the whole stream rather than an empty one.
    assert len(baseline) == len(stream) + 2  # + the Page.enable response + the sentinel


@pytest.mark.asyncio
async def test_a_policy_that_would_drop_the_attach_notice_still_learns_ownership() -> None:
    """Carry-forward #11 (SKY-12500 gate): the policy gate runs upstream of the
    ownership bookkeeping, so a dropped Target.attachedToTarget would strand the
    session — its owner never learned, its traffic never routable. The engine treats
    the attach notice as structural, so a rule aimed straight at it changes nothing.
    """
    connection = _AttachEventUpstream()
    server = _server(_SharedBrowser(connection))
    server._event_policy = _engine_policy(  # type: ignore[assignment]
        RateRule.throttle(method=TARGET_ATTACHED_EVENT, max_per_window=0, window_seconds=3600.0)
    )
    client_a = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "tA", "flatten": True})])
    client_b = _ScriptClient([_command(1, "Page.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.events(TARGET_ATTACHED_EVENT) and client_b.echoes())

    # The notice survived the rule, and ownership was learned from it: the attached
    # session's later traffic still routes to its owner and to nobody else.
    assert client_b.events(TARGET_ATTACHED_EVENT) == []
    connection.emit(CdpEvent(method="Page.loadEventFired", session_id="tA"))
    assert await _until(lambda: "tA" in client_a.event_sessions())
    assert client_b.event_sessions() == set()

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)
