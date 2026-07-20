"""Reusable contract suite for EventPolicyPort adapters.

Any adapter (generic here, cloud-specific under tests/cloud/) subclasses
EventPolicyPortContract and overrides make_policy(); every behavioral guarantee of
the port is asserted once, here. Each shipped policy config is an adapter of this
port and gets a subclass, so a new rule pack cannot skip the invariants.

tests/unit/proxy/test_contract_teeth.py proves these clauses actually catch a
violation; without it a weakened clause here would stay green forever.
"""

from __future__ import annotations

from skyvern.proxy.adapters.memory import ForwardAllEventPolicy
from skyvern.proxy.core.frames import TARGET_ATTACHED_EVENT, TARGET_DETACHED_EVENT, CdpCommand, CdpEvent
from skyvern.proxy.core.policy import (
    Clock,
    Drop,
    DropReason,
    EventPolicyConfig,
    EventPolicyEngine,
    Forward,
    Rewrite,
)
from skyvern.proxy.core.policy_pack import NOISY_EVENT_PACK_V1
from skyvern.proxy.core.screencast import SCREENCAST_PACK_V1
from skyvern.proxy.core.session import ProxySession
from skyvern.proxy.ports import EventPolicyPort

# Events every adapter must survive: gated-noisy, ordinary, unknown domain, and the
# shapes a client can put on the wire that no protocol doc promises.
SAMPLE_EVENTS = (
    CdpEvent(method="Network.dataReceived", params={"requestId": "r1"}),
    CdpEvent(method="Runtime.consoleAPICalled", params={"type": "log"}),
    CdpEvent(method="Page.screencastFrame", params={"sessionId": 1}),
    CdpEvent(method="Page.loadEventFired", params={}),
    CdpEvent(method="Unknown.somethingNobodyDefined", params=None),
    CdpEvent(method="NoDomainSeparator", params={"a": 1}, session_id="child-1"),
    CdpEvent(method="", params=None),
)

LIFECYCLE_SAMPLES = (
    CdpEvent(method=TARGET_ATTACHED_EVENT, params={"sessionId": "child-1"}),
    CdpEvent(method=TARGET_DETACHED_EVENT, params={"sessionId": "child-1"}),
)

SAMPLE_COMMANDS = (
    CdpCommand(id=1, method="Network.enable"),
    CdpCommand(id=2, method="Network.disable", session_id="child-1"),
    CdpCommand(id=3, method="Unknown.enable"),
    CdpCommand(id=4, method="NoDomainSeparator"),
    CdpCommand(id=5, method="", params={"weird": True}),
)


def frozen_clock() -> Clock:
    """Time never advances, so a throttle budget never refills: the harshest clock a
    rule can face, and deterministic — a contract suite may never race a real clock.
    """
    return lambda: 0.0


class EventPolicyPortContract:
    def make_policy(self) -> EventPolicyPort:
        raise NotImplementedError

    def make_session(self) -> ProxySession:
        return ProxySession(session_id="s1", upstream_ws_url="ws://localhost:0/devtools/browser/test")

    def test_decide_returns_a_policy_decision(self) -> None:
        policy, session = self.make_policy(), self.make_session()
        for event in SAMPLE_EVENTS:
            assert isinstance(policy.decide(event, session), (Forward, Drop, Rewrite))

    def test_lifecycle_events_are_never_dropped(self) -> None:
        """SKY-12500: the driving adapter learns session ownership downstream of this
        gate, so a dropped attach/detach strands that session's owner for good."""
        policy, session = self.make_policy(), self.make_session()
        for event in LIFECYCLE_SAMPLES:
            assert not isinstance(policy.decide(event, session), Drop)

    def test_lifecycle_survives_a_burst_whatever_the_rate_budget(self) -> None:
        """A frozen clock never refills a budget, so a rule that could ever throttle
        lifecycle drops one here."""
        policy, session = self.make_policy(), self.make_session()
        for _ in range(200):
            for event in LIFECYCLE_SAMPLES:
                assert not isinstance(policy.decide(event, session), Drop)

    def test_lifecycle_events_keep_their_routing_identity(self) -> None:
        """Not dropping a lifecycle event is not enough for the client to stay attached.

        The driving adapter learns which session an attach/detach belongs to from the
        event's method and params.sessionId, and it reads them AFTER this gate. An event
        delivered with that identity rewritten away is undeliverable in exactly the way a
        dropped one is: the owner is stranded either way (SKY-12500). So a policy may
        rewrite a lifecycle event, but never into something the router can no longer
        place.
        """
        policy, session = self.make_policy(), self.make_session()
        for event in LIFECYCLE_SAMPLES:
            decision = policy.decide(event, session)
            assert not isinstance(decision, Drop)
            if isinstance(decision, Rewrite):
                assert decision.event.method == event.method
                assert (decision.event.params or {}).get("sessionId") == (event.params or {}).get("sessionId")
                assert decision.event.session_id == event.session_id

    def test_drop_decisions_carry_a_closed_set_reason(self) -> None:
        """SKY-12510: a reason becomes a metric tag, so a free-form one is unbounded
        label cardinality."""
        policy, session = self.make_policy(), self.make_session()
        for event in SAMPLE_EVENTS:
            decision = policy.decide(event, session)
            if isinstance(decision, Drop):
                assert isinstance(decision.reason, DropReason)

    def test_observe_command_accepts_whatever_a_client_sends(self) -> None:
        policy, session = self.make_policy(), self.make_session()
        for command in SAMPLE_COMMANDS:
            policy.observe_command(command, session)

    def test_forget_is_idempotent_and_safe_for_unseen_sessions(self) -> None:
        policy, session = self.make_policy(), self.make_session()
        policy.forget("never-seen-at-all")
        policy.decide(SAMPLE_EVENTS[0], session)
        policy.observe_command(SAMPLE_COMMANDS[0], session)
        policy.forget(session.session_id)
        policy.forget(session.session_id)

    def test_decide_still_answers_after_the_session_is_forgotten(self) -> None:
        """forget() releases state, it does not poison the port: a late event may
        still arrive for a session already torn down."""
        policy, session = self.make_policy(), self.make_session()
        policy.decide(SAMPLE_EVENTS[0], session)
        policy.forget(session.session_id)
        assert isinstance(policy.decide(SAMPLE_EVENTS[0], session), (Forward, Drop, Rewrite))


class TestForwardAllEventPolicyContract(EventPolicyPortContract):
    def make_policy(self) -> EventPolicyPort:
        return ForwardAllEventPolicy()


class TestPassThroughEngineContract(EventPolicyPortContract):
    """The engine with no rules configured — the default deployment shape."""

    def make_policy(self) -> EventPolicyPort:
        return EventPolicyEngine(EventPolicyConfig(), frozen_clock())


class TestNoisyPackEngineContract(EventPolicyPortContract):
    def make_policy(self) -> EventPolicyPort:
        return EventPolicyEngine(NOISY_EVENT_PACK_V1, frozen_clock())


class TestScreencastPackEngineContract(EventPolicyPortContract):
    def make_policy(self) -> EventPolicyPort:
        return EventPolicyEngine(SCREENCAST_PACK_V1, frozen_clock())
