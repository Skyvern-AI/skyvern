"""The event-policy engine: decides, per upstream CDP event, what the client sees.

Pure and I/O-free. Policy arrives as a config VALUE and time as an injected clock, so
a decision is a function of (config, clock, observed traffic) alone — which is what
makes throttling testable without real time. The engine emits no telemetry itself:
MetricsPort lives in `skyvern.proxy.ports`, which imports this module, so the decision
trace is emitted by the caller from the returned decision (see the driving adapter).

This module is the MECHANISM. The noisy-domain rule pack (SKY-12501) and org-level
denylists (SKY-12538) are configuration built on top of it and live elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Union

from skyvern.proxy.core.frames import (
    KNOWN_CDP_DOMAINS,
    LIFECYCLE_EVENTS,
    TARGET_DETACHED_EVENT,
    CdpCommand,
    CdpEvent,
    params_session_id,
)
from skyvern.proxy.core.session import ProxySession

Clock = Callable[[], float]

# Bound on the per-session window table. A rule keyed on a client-controlled param
# (e.g. requestId) would otherwise grow one entry per distinct value for the life of
# the session. Mirrors the adapter's pending-command bound. Overflow evicts, which
# refills that key's budget early — fail-open, since forwarding is the safe default.
_MAX_WINDOWS_PER_SESSION = 4096

# Bound on tracked child CDP sessions per proxy session. A client may address a
# sessionId the proxy has never seen attached (the driving adapter leaves an unowned
# session for the browser to accept or reject), so the child id on a command is not
# trustworthy input and cannot be allowed to grow this table without limit.
_MAX_TRACKED_CHILD_SESSIONS = 512

# Placeholder window for a zero-budget (always-drop) rule, which never reaches the
# window table. Only present because a rule's window must be positive.
_UNUSED_WINDOW_SECONDS = 1.0


class DropReason(str, Enum):
    """Why an event was not delivered. Closed set: these become metric tags, and a
    free-form reason would blow up label cardinality (SKY-12510).

    POLICY keeps the `event_policy` wire value the frames_dropped counter already
    emitted before this engine existed, so the existing metric contract holds and
    only the new reasons are additive.
    """

    POLICY = "event_policy"
    THROTTLED = "throttled"


@dataclass(frozen=True, slots=True)
class Forward:
    """Deliver the event as-is.

    Carries no payload on purpose: the caller keeps the frame it already has and its
    existing encode path, so a forwarded event is byte-identical to what the proxy
    delivers with no policy at all. A Forward that carried an event would invite a
    re-serialization the default path must never have.
    """


@dataclass(frozen=True, slots=True)
class Drop:
    reason: DropReason


@dataclass(frozen=True, slots=True)
class Rewrite:
    """Deliver `event` in place of the original (SKY-12501/12538 synthesized errors)."""

    event: CdpEvent


PolicyDecision = Union[Forward, Drop, Rewrite]

# The default decision is a singleton, so the pass-through path allocates nothing per
# event no matter how much traffic runs through it.
FORWARD = Forward()


@dataclass(frozen=True, slots=True)
class RateRule:
    """At most `max_per_window` events of `method` per `window_seconds`, per session.

    First-wins within a window: the events that open the budget are delivered and the
    rest of the burst is dropped. That is what a rate cap means, and it is the only
    honest thing this mechanism does today.

    A budget of zero (`drop()`) never forwards, which is how the pack expresses an
    event no client consumes at all. `relax_when_enabled` lifts a cap entirely for a
    session observed to have enabled the event's domain — interest may only ever
    RELAX a rule, never tighten one, because unobserved interest is indistinguishable
    from absent interest (see `is_domain_enabled`).

    There is deliberately no `coalesce()` here. Collapsing a burst to its LATEST value
    (screencast frames, target-info churn) is the useful shape for freshness, but it
    cannot be done from a per-event decision: at the moment an event arrives, nothing
    knows a newer one is coming, so a budget of one per window delivers the OLDEST and
    drops the newest — stale state, the opposite of coalescing. True latest-wins needs
    to reach into the delivery queue (replace a still-queued event with the newer one)
    or defer a flush, both of which belong with the backpressure code rather than in a
    pure decision function. SKY-12501 shipped no rule that needs it: the queue it would
    reach into is empty on the healthy path, so keyed replacement would collapse almost
    nothing that the client's tail-drop does not already handle.
    """

    method: str
    max_per_window: int
    window_seconds: float
    key_params: tuple[str, ...] = ()
    reason: DropReason = DropReason.THROTTLED
    relax_when_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not self.method.strip():
            raise ValueError("method must be a non-empty string")
        if self.max_per_window < 0:
            raise ValueError("max_per_window must not be negative")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

    @classmethod
    def throttle(
        cls,
        method: str,
        max_per_window: int,
        window_seconds: float,
        key_params: Sequence[str] = (),
        relax_when_enabled: bool = False,
    ) -> RateRule:
        return cls(method, max_per_window, window_seconds, tuple(key_params), DropReason.THROTTLED, relax_when_enabled)

    @classmethod
    def drop(cls, method: str) -> RateRule:
        """Never deliver `method`, whatever the client has enabled.

        The window is immaterial at a zero budget (it is never consulted) and only
        satisfies the positive-window invariant the throttle path relies on.
        """
        return cls(method, 0, _UNUSED_WINDOW_SECONDS, (), DropReason.POLICY)


@dataclass(frozen=True, slots=True)
class EventPolicyConfig:
    """Versioned deployment policy, as data. No rules means pass-through."""

    version: int = 1
    rules: tuple[RateRule, ...] = ()

    def __post_init__(self) -> None:
        methods = [rule.method for rule in self.rules]
        duplicates = {method for method in methods if methods.count(method) > 1}
        if duplicates:
            # Two rules for one method would make the winner depend on tuple order.
            raise ValueError(f"duplicate rules for methods: {sorted(duplicates)}")


@dataclass(slots=True)
class _SessionState:
    # Interest is per CHILD cdp session (None = the browser-level session), because
    # that is the scope a client actually enables a domain on. Keying it by the outer
    # session alone would let a disable on one child silently clear another's.
    enabled_domains: dict[str | None, set[str]] = field(default_factory=dict)
    windows: dict[object, tuple[float, int]] = field(default_factory=dict)


class EventPolicyEngine:
    """Applies an EventPolicyConfig to one proxy's event stream.

    Stateful across calls (throttle windows, per-session domain interest) but pure in
    the sense that matters: no I/O, no ambient clock, no telemetry. `forget()` releases
    a session's state; the caller must invoke it when the session goes away.
    """

    def __init__(self, config: EventPolicyConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._rules: dict[str, RateRule] = {rule.method: rule for rule in config.rules}
        self._max_window_seconds = max((rule.window_seconds for rule in config.rules), default=0.0)
        self._sessions: dict[str, _SessionState] = {}

    @property
    def config(self) -> EventPolicyConfig:
        return self._config

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        if event.method in LIFECYCLE_EVENTS:
            # Structural, never droppable: the caller learns session ownership from
            # these downstream of this gate, so dropping one strands the owner
            # (SKY-12500). No rule can express it, whatever the config says. The detach
            # notice is also where a child session's tracked interest is retired — it
            # is the only signal that the child is gone.
            if event.method == TARGET_DETACHED_EVENT:
                self._forget_child(session, params_session_id(event))
            return FORWARD
        if not self._rules:
            # No policy configured: the engine is a transparent no-op and the caller
            # delivers exactly the bytes it would with no engine at all.
            return FORWARD
        rule = self._rules.get(event.method)
        if rule is None:
            return FORWARD
        if rule.relax_when_enabled and self._declared_interest(session, event):
            # The client asked for this domain outright, so it gets the stream in
            # full. Only True is actionable: False means "never seen enabled", which
            # a rule must never read as disinterest (see is_domain_enabled).
            return FORWARD
        return self._spend_budget(rule, event, session)

    def _declared_interest(self, session: ProxySession, event: CdpEvent) -> bool:
        return self.is_domain_enabled(session, event.method.partition(".")[0], event.session_id)

    def _spend_budget(self, rule: RateRule, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        if rule.max_per_window == 0:
            # A budget that can never be spent needs no window: tracking one would
            # cost an entry per key for a rule whose answer is always the same.
            return Drop(rule.reason)
        state = self._sessions.get(session.session_id)
        if state is None:
            state = self._sessions[session.session_id] = _SessionState()
        now = self._clock()
        key = self._window_key(rule, event)
        window = state.windows.get(key)
        if window is None:
            self._make_room(state, now)
            start, count = now, 0
        elif now - window[0] >= rule.window_seconds:
            start, count = now, 0
        else:
            start, count = window
        if count < rule.max_per_window:
            state.windows[key] = (start, count + 1)
            return FORWARD
        state.windows[key] = (start, count)
        return Drop(rule.reason)

    @staticmethod
    def _window_key(rule: RateRule, event: CdpEvent) -> object:
        if not rule.key_params:
            return rule.method
        params = event.params or {}
        return (rule.method, tuple(str(params.get(name)) for name in rule.key_params))

    def _make_room(self, state: _SessionState, now: float) -> None:
        """Keep the window table bounded before admitting a new key."""
        if len(state.windows) < _MAX_WINDOWS_PER_SESSION:
            return
        # A window older than the longest configured window has expired under every
        # rule, so dropping it only hands that key a fresh budget it was owed anyway.
        cutoff = now - self._max_window_seconds
        for key in [key for key, (start, _) in state.windows.items() if start <= cutoff]:
            del state.windows[key]
        if len(state.windows) < _MAX_WINDOWS_PER_SESSION:
            return
        del state.windows[min(state.windows, key=lambda key: state.windows[key][0])]

    def observe_command(self, command: CdpCommand, session: ProxySession) -> None:
        """Track which CDP domains a session actually asked for, per child session.

        A rule can then gate on the client's declared interest instead of assuming it
        (SKY-12501). Only `<Domain>.enable`/`<Domain>.disable` move this, and only for
        a domain the protocol actually has: every other command is ignored, which is
        also what keeps an invented `<anything>.enable` out of this table.
        """
        domain, separator, verb = command.method.partition(".")
        if not separator or domain not in KNOWN_CDP_DOMAINS:
            return
        if verb not in ("enable", "disable"):
            return
        child = command.session_id
        if verb == "disable":
            state = self._sessions.get(session.session_id)
            if state is not None and child in state.enabled_domains:
                state.enabled_domains[child].discard(domain)
            return
        state = self._sessions.get(session.session_id)
        if state is None:
            state = self._sessions[session.session_id] = _SessionState()
        interest = state.enabled_domains.get(child)
        if interest is None:
            if len(state.enabled_domains) >= _MAX_TRACKED_CHILD_SESSIONS:
                # Oldest-out. A consumer must therefore treat "not enabled" as "not
                # known", never as proof of disinterest, and fail open — see
                # is_domain_enabled.
                del state.enabled_domains[next(iter(state.enabled_domains))]
            interest = state.enabled_domains[child] = set()
        interest.add(domain)

    def is_domain_enabled(self, session: ProxySession, domain: str, cdp_session_id: str | None = None) -> bool:
        """Whether `cdp_session_id` (None = browser level) has enabled `domain`.

        False also means "never seen it enabled", which is not the same as knowing the
        client is uninterested: interest is only observed while the proxy is watching,
        and the tables above are bounded. A rule that drops on False is fail-closed and
        will eventually eat real traffic — gate on True to relax a drop, not to cause
        one (SKY-12501).
        """
        state = self._sessions.get(session.session_id)
        if state is None:
            return False
        return domain in state.enabled_domains.get(cdp_session_id, frozenset())

    def _forget_child(self, session: ProxySession, cdp_session_id: str | None) -> None:
        if cdp_session_id is None:
            return
        state = self._sessions.get(session.session_id)
        if state is not None:
            state.enabled_domains.pop(cdp_session_id, None)

    def forget(self, session_id: str) -> None:
        """Release a session's state. Idempotent; safe for a session never seen."""
        self._sessions.pop(session_id, None)

    def tracked_sessions(self) -> int:
        return len(self._sessions)

    def tracked_children(self, session: ProxySession) -> int:
        state = self._sessions.get(session.session_id)
        return len(state.enabled_domains) if state is not None else 0

    def enabled_domain_count(self, session: ProxySession) -> int:
        state = self._sessions.get(session.session_id)
        if state is None:
            return 0
        return sum(len(domains) for domains in state.enabled_domains.values())
