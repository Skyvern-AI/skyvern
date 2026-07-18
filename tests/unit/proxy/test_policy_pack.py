from __future__ import annotations

from pathlib import Path

import pytest

from skyvern.proxy.adapters.websocket_server import _cdp_method_tags
from skyvern.proxy.core.frames import (
    LIFECYCLE_EVENTS,
    CdpCommand,
    CdpEvent,
    decode_frame,
    encode_frame,
)
from skyvern.proxy.core.policy import FORWARD, Drop, DropReason, EventPolicyEngine
from skyvern.proxy.core.policy_pack import (
    CONSOLE_BURST_PER_SECOND,
    NOISY_EVENT_PACK_V1,
    UNCONSUMED_EVENTS,
)
from skyvern.proxy.core.session import Principal, ProxySession
from tests.unit.proxy.test_event_policy import FakeClock

FIXTURES_DIR = Path(__file__).parent / "fixtures"
BROADBAND_WORKLOAD = "workload_page_load_broadband.jsonl"
LOOPBACK_WORKLOAD = "workload_page_load_loopback.jsonl"

# Every event the pinned Playwright driver or puppeteer-core registers a handler for,
# enumerated from their sources rather than recalled. The pack must never drop one of
# these: a client library that stops seeing them silently loses page state, which no
# test of ours would notice.
#
# This is a MANUAL SNAPSHOT, and it is the safety net for future drop-set additions —
# an omission here reads as "nothing consumes it" and would wave through a rule that
# breaks a client. Adding to the drop set means re-enumerating BOTH pinned sources:
#
#   playwright: grep -rhoE 'Network\.[a-zA-Z]+' <uv-cache>/playwright/driver/package/lib
#   puppeteer:  grep -rhoE 'Network\.[a-zA-Z]+' <npm pack puppeteer-core>/package/src
#
# THE TRAP: a wrong path greps clean and every method looks unconsumed. Run a positive
# control first — Network.responseReceived and Network.loadingFinished must come back
# non-zero, or the path is wrong and the whole result is noise. Match unquoted: the
# quoted form ('Network.x') only hits some driver builds.
CONSUMED_BY_CLIENT_LIBRARIES = (
    "Network.loadingFailed",
    "Network.loadingFinished",
    "Network.requestServedFromCache",
    "Network.requestWillBeSent",
    "Network.requestWillBeSentExtraInfo",
    "Network.responseReceived",
    "Network.responseReceivedExtraInfo",
    "Network.webSocketClosed",
    "Network.webSocketCreated",
    "Network.webSocketFrameError",
    "Network.webSocketFrameReceived",
    "Network.webSocketFrameSent",
    "Network.webSocketHandshakeResponseReceived",
    "Network.webSocketWillSendHandshakeRequest",
    "Runtime.bindingCalled",
    "Runtime.exceptionThrown",
    "Runtime.executionContextCreated",
    "Runtime.executionContextDestroyed",
    "Runtime.executionContextsCleared",
    "Page.domContentEventFired",
    "Page.fileChooserOpened",
    "Page.frameAttached",
    "Page.frameDetached",
    "Page.frameNavigated",
    "Page.javascriptDialogOpening",
    "Page.lifecycleEvent",
    "Page.loadEventFired",
    "Page.screencastFrame",
    "Target.targetCreated",
    "Target.targetDestroyed",
    "Target.targetInfoChanged",
    "Log.entryAdded",
    "Fetch.authRequired",
    "Fetch.requestPaused",
)

SESSION_ID = "s_1"
CHILD = "child-1"


def _session() -> ProxySession:
    return ProxySession(
        session_id=SESSION_ID,
        upstream_ws_url="ws://upstream.internal:9222/x?token=secret-token",
        principal=Principal(principal_id="o_1", organization_id="o_1"),
    )


def _engine(clock: FakeClock | None = None) -> EventPolicyEngine:
    return EventPolicyEngine(config=NOISY_EVENT_PACK_V1, clock=clock or FakeClock())


def _events(fixture: str) -> list[CdpEvent]:
    frames = (decode_frame(line) for line in (FIXTURES_DIR / fixture).read_text().splitlines() if line.strip())
    return [frame for frame in frames if isinstance(frame, CdpEvent)]


def _replay(events: list[CdpEvent]) -> tuple[int, int]:
    """Forwarded event count and wire bytes for one pass of the pack."""
    engine = _engine()
    session = _session()
    count = bytes_out = 0
    for event in events:
        if isinstance(engine.decide(event, session), Drop):
            continue
        count += 1
        bytes_out += len(encode_frame(event))
    return count, bytes_out


# --- the curated drop set -------------------------------------------------------


@pytest.mark.parametrize("method", UNCONSUMED_EVENTS)
def test_pack_drops_every_unconsumed_noise_event(method: str) -> None:
    engine = _engine()

    decision = engine.decide(CdpEvent(method=method, params={}, session_id=CHILD), _session())

    assert decision == Drop(DropReason.POLICY)


@pytest.mark.parametrize("method", UNCONSUMED_EVENTS)
def test_an_unconsumed_drop_is_not_relaxed_by_declared_interest(method: str) -> None:
    """The drop set is unconditional: no client consumes these even when it has
    enabled the domain, so enabling Network must not resurrect the noise."""
    engine = _engine()
    session = _session()
    engine.observe_command(CdpCommand(id=1, method="Network.enable", session_id=CHILD), session)

    assert engine.decide(CdpEvent(method=method, params={}, session_id=CHILD), session) == Drop(DropReason.POLICY)


@pytest.mark.parametrize("method", CONSUMED_BY_CLIENT_LIBRARIES)
def test_pack_never_drops_an_event_a_client_library_consumes(method: str) -> None:
    engine = _engine()

    assert engine.decide(CdpEvent(method=method, params={}, session_id=CHILD), _session()) is FORWARD


@pytest.mark.parametrize("method", sorted(LIFECYCLE_EVENTS))
def test_pack_forwards_lifecycle_events(method: str) -> None:
    engine = _engine()

    assert engine.decide(CdpEvent(method=method, params={}, session_id=CHILD), _session()) is FORWARD


def test_drop_set_and_consumed_set_are_disjoint() -> None:
    assert not set(UNCONSUMED_EVENTS) & set(CONSUMED_BY_CLIENT_LIBRARIES)


# --- AC2: gating keys off actual Domain.enable ----------------------------------


def test_console_throttle_relaxes_for_a_session_that_enabled_runtime() -> None:
    """AC2: observed interest, not an assumption, is what lifts the cap."""
    clock = FakeClock()
    engine = _engine(clock)
    session = _session()
    engine.observe_command(CdpCommand(id=1, method="Runtime.enable", session_id=CHILD), session)

    burst = [
        engine.decide(CdpEvent(method="Runtime.consoleAPICalled", params={}, session_id=CHILD), session)
        for _ in range(CONSOLE_BURST_PER_SECOND * 3)
    ]

    assert all(decision is FORWARD for decision in burst)


def test_console_throttle_caps_a_burst_when_interest_was_never_observed() -> None:
    clock = FakeClock()
    engine = _engine(clock)
    session = _session()

    burst = [
        engine.decide(CdpEvent(method="Runtime.consoleAPICalled", params={}, session_id=CHILD), session)
        for _ in range(CONSOLE_BURST_PER_SECOND + 5)
    ]

    assert burst[:CONSOLE_BURST_PER_SECOND] == [FORWARD] * CONSOLE_BURST_PER_SECOND
    assert burst[CONSOLE_BURST_PER_SECOND:] == [Drop(DropReason.THROTTLED)] * 5


def test_unknown_interest_still_delivers_console_events_rather_than_silencing_them() -> None:
    """The fail-safe rule: is_domain_enabled is False for a session the proxy never
    saw enable the domain (bounded tables evict), so False must never mean silence.
    An unobserved session keeps getting events, just rate-capped."""
    clock = FakeClock()
    engine = _engine(clock)
    session = _session()

    assert not engine.is_domain_enabled(session, "Runtime", CHILD)
    for second in range(5):
        clock.advance(1.0)
        decision = engine.decide(CdpEvent(method="Runtime.consoleAPICalled", params={}, session_id=CHILD), session)
        assert decision is FORWARD, f"silenced in window {second}"


def test_interest_is_scoped_to_the_child_session_that_enabled_the_domain() -> None:
    clock = FakeClock()
    engine = _engine(clock)
    session = _session()
    engine.observe_command(CdpCommand(id=1, method="Runtime.enable", session_id=CHILD), session)

    relaxed = [
        engine.decide(CdpEvent(method="Runtime.consoleAPICalled", params={}, session_id=CHILD), session)
        for _ in range(CONSOLE_BURST_PER_SECOND + 5)
    ]
    other = [
        engine.decide(CdpEvent(method="Runtime.consoleAPICalled", params={}, session_id="child-2"), session)
        for _ in range(CONSOLE_BURST_PER_SECOND + 5)
    ]

    assert all(decision is FORWARD for decision in relaxed)
    assert Drop(DropReason.THROTTLED) in other


# --- AC1: before/after on a recorded workload -----------------------------------


def test_recorded_broadband_workload_sheds_most_events_and_half_its_bytes() -> None:
    """AC1, measured on a stream recorded from a real Chrome driven like Playwright
    drives it (flat auto-attach + Runtime/Network/Page enable) against a local page
    over emulated broadband. Thresholds sit below the measured 75.7% / 46.5% so this
    pins the win without pinning the fixture's exact byte count."""
    events = _events(BROADBAND_WORKLOAD)
    baseline_count = len(events)
    baseline_bytes = sum(len(encode_frame(event)) for event in events)

    count, bytes_out = _replay(events)

    assert 1 - count / baseline_count >= 0.50
    assert 1 - bytes_out / baseline_bytes >= 0.30


def test_loopback_workload_reduction_is_the_floor_not_the_expectation() -> None:
    """The same page over loopback: Chrome takes each body in a few huge reads, so
    dataReceived barely fires and the pack has far less to shed. Recorded to keep the
    honest floor visible — the win is a function of how a real network chunks bodies,
    and a fast path is where this pack does least."""
    events = _events(LOOPBACK_WORKLOAD)
    baseline_count = len(events)
    baseline_bytes = sum(len(encode_frame(event)) for event in events)

    count, bytes_out = _replay(events)

    assert 0.10 <= 1 - count / baseline_count < 0.50
    assert 0 < 1 - bytes_out / baseline_bytes < 0.30


def test_replaying_a_recorded_workload_forwards_every_consumed_event_untouched() -> None:
    """Compatibility: the pack sheds only its own drop set. Everything the recorded
    stream carried that a client library reads must survive, byte-for-byte."""
    engine = _engine()
    session = _session()

    for event in _events(BROADBAND_WORKLOAD):
        baseline = encode_frame(event)
        decision = engine.decide(event, session)

        if event.method in UNCONSUMED_EVENTS:
            assert isinstance(decision, Drop)
            continue
        assert decision is FORWARD
        assert encode_frame(event) == baseline


# --- AC3: the reduction is visible per method -----------------------------------


@pytest.mark.parametrize("rule", NOISY_EVENT_PACK_V1.rules, ids=lambda rule: rule.method)
def test_every_gated_method_reports_its_own_metric_bucket(rule) -> None:
    """AC3: a dropped counter tagged cdp_method='other' cannot show which stream the
    reduction came from. Every method this pack names must survive the allowlist."""
    cdp_method, cdp_domain = _cdp_method_tags(rule.method)

    assert cdp_method == rule.method
    assert cdp_domain == rule.method.split(".", 1)[0]


def test_pack_is_versioned() -> None:
    assert NOISY_EVENT_PACK_V1.version == 1
