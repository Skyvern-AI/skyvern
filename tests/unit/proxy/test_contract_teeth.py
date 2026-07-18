"""Proof that the reusable port-contract suites actually reject a bad adapter.

A contract suite that asserts nothing passes exactly as green as one that asserts
everything, so "the adapter passes its contract" is worth only as much as the
contract's ability to fail. Every adapter here is deliberately broken in exactly one
way, and MUTANTS pairs each with the clause that must catch it.

Two properties are enforced, and the second is the one that makes the first mean
something:

* every mutant's clause catches it (test_clause_catches_its_mutant), and
* every clause of every covered contract HAS a mutant (test_every_claimed_clause_has_a_
  mutant). Without that, a clause with no mutant could be gutted or deleted with this
  file still green — the covered subset would look like the whole.

Covered contracts are listed in TEETH_COVERED_CONTRACTS. AuthPortContract and
MetricsPortContract are NOT covered here: their clauses drive a whole proxy server
rather than swapping one adapter, so mutating them needs a different harness than this
file's. That is a real gap, named rather than papered over.

These adapters are fixtures, never shipped: nothing outside this file imports them, and
the leading underscore keeps pytest from collecting them as suites of their own.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable, Sequence

import pytest

from skyvern.proxy.adapters.memory import InMemorySessionRegistry, InMemoryUpstreamConnection
from skyvern.proxy.core.frames import KNOWN_CDP_DOMAINS, LIFECYCLE_EVENTS, CdpCommand, CdpEvent
from skyvern.proxy.core.policy import FORWARD, Drop, DropReason, PolicyDecision, Rewrite
from skyvern.proxy.core.session import (
    ProxySession,
    ResolvedSession,
    SessionResolution,
    SessionResolutionStatus,
)
from skyvern.proxy.ports import UpstreamBrowserPort, UpstreamConnection
from tests.unit.proxy.test_event_policy_contract import EventPolicyPortContract
from tests.unit.proxy.test_session_registry_contract import SessionRegistryContract
from tests.unit.proxy.test_upstream_browser_port_contract import (
    DialingUpstreamBrowserPortContract,
    UpstreamBrowserPortContract,
)

SECRET_UPSTREAM_URL = "ws://upstream.internal:9222/devtools/browser/abc?token=secret-token"


def _resolve_clause(contract: object, clause: str) -> Callable[[], object]:
    """Look the clause up BEFORE it is run, so a deleted one cannot masquerade as a
    working one.

    Resolving inside the try below would turn a renamed or removed clause into an
    AttributeError that reads exactly like the contract rejecting the adapter — the
    suite would go green precisely because its assertion had stopped existing. That is
    the failure this whole file exists to make impossible, so it is checked first.
    """
    found = getattr(contract, clause, None)
    if not callable(found):
        raise AssertionError(
            f"{type(contract).__name__} has no clause {clause!r} — a clause that no longer exists cannot catch anything"
        )
    return found


async def _run_clause(contract: object, clause: str) -> None:
    outcome = _resolve_clause(contract, clause)()
    if inspect.isawaitable(outcome):
        await outcome


async def _assert_contract_catches(contract: object, clause: str) -> None:
    """`clause` must fail against the broken adapter behind `contract`."""
    invoke = _resolve_clause(contract, clause)
    try:
        outcome = invoke()
        if inspect.isawaitable(outcome):
            await outcome
    except pytest.skip.Exception as skipped:
        raise AssertionError(f"{clause} SKIPPED instead of failing — an unrun contract is a false green") from skipped
    except (Exception, pytest.fail.Exception):
        return
    raise AssertionError(f"{clause} PASSED against a deliberately broken adapter — the clause has no teeth")


async def _assert_contract_allows(contract: object, clause: str) -> None:
    """`clause` must pass — used to show a narrower clause misses what a wider one catches."""
    await _run_clause(contract, clause)


# --- EventPolicyPort mutants -------------------------------------------------------


class _CompliantPolicy:
    """The baseline each EventPolicy mutant below breaks in exactly one way."""

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        return FORWARD

    def observe_command(self, command: CdpCommand, session: ProxySession) -> None:
        return None

    def forget(self, session_id: str) -> None:
        return None


class _ReturnsNonDecision(_CompliantPolicy):
    """Returns something that is not a PolicyDecision at all."""

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        return None  # type: ignore[return-value]


class _DropsLifecycleEvents(_CompliantPolicy):
    """Violates the never-droppable lifecycle rule (SKY-12500): a dropped attach or
    detach strands that session's owner."""

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        if event.method in LIFECYCLE_EVENTS:
            return Drop(DropReason.POLICY)
        return FORWARD


class _ThrottlesLifecycleAfterABurst(_CompliantPolicy):
    """Forwards the first few lifecycle events, then throttles them.

    Invisible to a single-event check — which is exactly why the contract bursts.
    """

    def __init__(self) -> None:
        self._seen = 0

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        if event.method in LIFECYCLE_EVENTS:
            self._seen += 1
            if self._seen > 10:
                return Drop(DropReason.THROTTLED)
        return FORWARD


class _StripsLifecycleIdentity(_CompliantPolicy):
    """Delivers every lifecycle event, with the sessionId the router needs removed.

    Never drops anything, so a contract that only rejects Drop calls this compliant —
    while the client is stranded exactly as if the event had been dropped.
    """

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        if event.method in LIFECYCLE_EVENTS:
            stripped = {key: value for key, value in (event.params or {}).items() if key != "sessionId"}
            return Rewrite(CdpEvent(method=event.method, params=stripped, session_id=event.session_id))
        return FORWARD


class _FreeFormDropReason(_CompliantPolicy):
    """Reason taken from the wire instead of the closed set: one metric label per
    distinct CDP method, which is the cardinality blowup SKY-12510 bounds."""

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        if event.method in LIFECYCLE_EVENTS:
            return FORWARD
        return Drop(f"unbounded-{event.method}")  # type: ignore[arg-type]


class _RejectsOddCommands(_CompliantPolicy):
    """Trusts the client's command to be well-formed — a client can send anything."""

    def observe_command(self, command: CdpCommand, session: ProxySession) -> None:
        domain = command.method.partition(".")[0]
        if domain not in KNOWN_CDP_DOMAINS:
            raise ValueError(f"unknown domain: {domain}")


class _NonIdempotentForget(_CompliantPolicy):
    """forget() is a teardown path; a second call must not explode."""

    def __init__(self) -> None:
        self._forgotten: set[str] = set()

    def forget(self, session_id: str) -> None:
        if session_id in self._forgotten:
            raise RuntimeError("forget() called twice")
        self._forgotten.add(session_id)


class _PoisonedAfterForget(_CompliantPolicy):
    """forget() leaves the port unusable, so a late event for a torn-down session
    raises instead of being decided."""

    def __init__(self) -> None:
        self._forgotten = False

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        if self._forgotten:
            raise RuntimeError("decide() after forget()")
        return FORWARD

    def forget(self, session_id: str) -> None:
        self._forgotten = True


class _BrokenPolicyContract(EventPolicyPortContract):
    """Binds one broken policy instance to the contract. The instance is shared across
    make_policy() calls because a mutant like the burst-throttler accumulates state the
    clause is meant to notice."""

    def __init__(self, policy: object) -> None:
        self._policy = policy

    def make_policy(self) -> object:  # type: ignore[override]
        return self._policy


# --- UpstreamBrowserPort mutants ---------------------------------------------------


class _NotAConnection:
    """Missing receive(), so it does not satisfy the UpstreamConnection protocol."""

    async def send(self, raw: str) -> None:
        return None

    async def close(self) -> None:
        return None


class _NoMatchingResponseConnection(InMemoryUpstreamConnection):
    """Answers, but never with the id that was asked for."""

    async def receive(self) -> str:
        await super().receive()
        return '{"id": 999, "result": {}}'


class _NonIdempotentCloseConnection(InMemoryUpstreamConnection):
    """close() is a teardown path: a second call must not explode."""

    async def close(self) -> None:
        if self._closed:
            raise RuntimeError("close() called twice")
        await super().close()


class _SilentReceiveAfterCloseConnection(InMemoryUpstreamConnection):
    """Returns an empty frame after close instead of raising, so a relay loop reads
    silence forever rather than learning the upstream is gone."""

    async def receive(self) -> str:
        if self._closed:
            return ""
        return await super().receive()


class _SilentSendAfterCloseConnection(InMemoryUpstreamConnection):
    """Accepts writes into the void after close instead of raising."""

    async def send(self, raw: str) -> None:
        if self._closed:
            return None
        await super().send(raw)


class _RawTransportErrorConnection:
    """Leaks the transport's own exception once the remote goes away, instead of the
    port taxonomy every caller branches on."""

    def __init__(self, break_send: bool, break_receive: bool) -> None:
        self.broken = False
        self._break_send = break_send
        self._break_receive = break_receive

    async def send(self, raw: str) -> None:
        if self.broken and self._break_send:
            raise ConnectionResetError("raw transport error")

    async def receive(self) -> str:
        if self.broken and self._break_receive:
            raise ConnectionResetError("raw transport error")
        return '{"id": 42, "result": {}}'

    async def close(self) -> None:
        return None


class _FixedConnectionBrowser:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def connect(self, session: ProxySession) -> UpstreamConnection:
        return self._connection  # type: ignore[return-value]


class _ConnectionFactoryBrowser:
    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory

    async def connect(self, session: ProxySession) -> UpstreamConnection:
        return self._factory()  # type: ignore[return-value]


class _RefusingBrowser:
    """Leaks the raw dial failure instead of the port's UpstreamConnectError taxonomy."""

    async def connect(self, session: ProxySession) -> UpstreamConnection:
        raise ConnectionRefusedError("connection refused")


class _BrokenUpstreamContract(UpstreamBrowserPortContract):
    def __init__(self, port: object) -> None:
        self._port = port

    def make_port(self) -> UpstreamBrowserPort:
        return self._port  # type: ignore[return-value]


class _BrokenDialingContract(DialingUpstreamBrowserPortContract):
    """A dialing adapter that leaks raw transport errors rather than the taxonomy."""

    def __init__(self, *, refusing: bool = False, break_send: bool = False, break_receive: bool = False) -> None:
        self._refusing = refusing
        self._connection = _RawTransportErrorConnection(break_send=break_send, break_receive=break_receive)

    def make_port(self) -> UpstreamBrowserPort:
        return _FixedConnectionBrowser(self._connection)  # type: ignore[return-value]

    def make_unreachable_port_and_session(self) -> tuple[UpstreamBrowserPort, ProxySession]:
        return _RefusingBrowser(), self.make_session()  # type: ignore[return-value]

    async def break_remote(self, connection: UpstreamConnection) -> None:
        self._connection.broken = True


# --- SessionRegistryPort mutants ---------------------------------------------------


def _seed_memory_registry(
    active: Sequence[ResolvedSession],
    pending: Sequence[str],
    closed: Sequence[str],
    expired: Sequence[str],
) -> InMemorySessionRegistry:
    registry = InMemorySessionRegistry()
    for session in active:
        registry.put(session)
    for session_id in pending:
        registry.mark_pending(session_id)
    for session_id in closed:
        registry.mark_closed(session_id)
    for session_id in expired:
        registry.mark_expired(session_id)
    return registry


@dataclass(frozen=True)
class _LeakyResolution:
    """A registry's own resolution type that forgets to keep the upstream URL out of
    its repr, so one log line leaks the session token in the URL's query."""

    status: SessionResolutionStatus
    session: ResolvedSession | None = None
    organization_id: str | None = None
    expires_in_seconds: float | None = None

    def __repr__(self) -> str:
        upstream = self.session.upstream_ws_url if self.session else None
        return f"_LeakyResolution(status={self.status.value}, upstream={upstream})"


class _LeakyRegistry:
    """Correct routing, leaky repr — exactly one clause broken."""

    def __init__(self, inner: InMemorySessionRegistry) -> None:
        self._inner = inner

    async def resolve(self, session_id: str) -> SessionResolution:
        resolution = await self._inner.resolve(session_id)
        return _LeakyResolution(  # type: ignore[return-value]
            resolution.status, resolution.session, resolution.organization_id, resolution.expires_in_seconds
        )

    async def invalidate(self, session_id: str) -> None:
        await self._inner.invalidate(session_id)


class _RoutesStatusRegistry:
    """Hands back full routing for a session the control plane says is not usable, so a
    client reaches a browser that is pending, closed or already expired."""

    def __init__(self, inner: InMemorySessionRegistry, status: SessionResolutionStatus) -> None:
        self._inner = inner
        self._status = status

    async def resolve(self, session_id: str) -> SessionResolution:
        resolution = await self._inner.resolve(session_id)
        if resolution.status is self._status:
            return SessionResolution.active(
                ResolvedSession(
                    session_id=session_id,
                    upstream_adapter="websocket",
                    upstream_ws_url=SECRET_UPSTREAM_URL,
                    organization_id="o_1",
                )
            )
        return resolution

    async def invalidate(self, session_id: str) -> None:
        await self._inner.invalidate(session_id)


class _RoutesUnknownRegistry(_RoutesStatusRegistry):
    def __init__(self, inner: InMemorySessionRegistry) -> None:
        super().__init__(inner, SessionResolutionStatus.UNKNOWN)


class _LosesRoutingOnActiveRegistry:
    """Reports ACTIVE but without the routing the caller needs to dial anything."""

    def __init__(self, inner: InMemorySessionRegistry) -> None:
        self._inner = inner

    async def resolve(self, session_id: str) -> SessionResolution:
        resolution = await self._inner.resolve(session_id)
        if resolution.status is SessionResolutionStatus.ACTIVE:
            return SessionResolution(SessionResolutionStatus.ACTIVE, None, resolution.organization_id)
        return resolution

    async def invalidate(self, session_id: str) -> None:
        await self._inner.invalidate(session_id)


class _NonIdempotentInvalidateRegistry:
    """invalidate() is a teardown path fired on close; a repeat must not explode."""

    def __init__(self, inner: InMemorySessionRegistry) -> None:
        self._inner = inner
        self._invalidated: set[str] = set()

    async def resolve(self, session_id: str) -> SessionResolution:
        return await self._inner.resolve(session_id)

    async def invalidate(self, session_id: str) -> None:
        if session_id in self._invalidated:
            raise RuntimeError("invalidate() called twice")
        self._invalidated.add(session_id)


class _BrokenRegistryContract(SessionRegistryContract):
    def __init__(self, wrap: Callable[[InMemorySessionRegistry], object]) -> None:
        self._wrap = wrap

    def make_registry(
        self,
        *,
        active: tuple[ResolvedSession, ...] = (),
        pending: tuple[str, ...] = (),
        closed: tuple[str, ...] = (),
        expired: tuple[str, ...] = (),
    ) -> object:  # type: ignore[override]
        return self._wrap(_seed_memory_registry(active, pending, closed, expired))


# --- the registry ------------------------------------------------------------------


@dataclass(frozen=True)
class Mutant:
    """One adapter broken in exactly one way, and the clause that must catch it."""

    contract: type
    clause: str
    make: Callable[[], object]


MUTANTS: tuple[Mutant, ...] = (
    # EventPolicyPortContract
    Mutant(
        EventPolicyPortContract,
        "test_decide_returns_a_policy_decision",
        lambda: _BrokenPolicyContract(_ReturnsNonDecision()),
    ),
    Mutant(
        EventPolicyPortContract,
        "test_lifecycle_events_are_never_dropped",
        lambda: _BrokenPolicyContract(_DropsLifecycleEvents()),
    ),
    Mutant(
        EventPolicyPortContract,
        "test_lifecycle_survives_a_burst_whatever_the_rate_budget",
        lambda: _BrokenPolicyContract(_ThrottlesLifecycleAfterABurst()),
    ),
    Mutant(
        EventPolicyPortContract,
        "test_lifecycle_events_keep_their_routing_identity",
        lambda: _BrokenPolicyContract(_StripsLifecycleIdentity()),
    ),
    Mutant(
        EventPolicyPortContract,
        "test_drop_decisions_carry_a_closed_set_reason",
        lambda: _BrokenPolicyContract(_FreeFormDropReason()),
    ),
    Mutant(
        EventPolicyPortContract,
        "test_observe_command_accepts_whatever_a_client_sends",
        lambda: _BrokenPolicyContract(_RejectsOddCommands()),
    ),
    Mutant(
        EventPolicyPortContract,
        "test_forget_is_idempotent_and_safe_for_unseen_sessions",
        lambda: _BrokenPolicyContract(_NonIdempotentForget()),
    ),
    Mutant(
        EventPolicyPortContract,
        "test_decide_still_answers_after_the_session_is_forgotten",
        lambda: _BrokenPolicyContract(_PoisonedAfterForget()),
    ),
    # UpstreamBrowserPortContract
    Mutant(
        UpstreamBrowserPortContract,
        "test_connect_returns_open_connection",
        lambda: _BrokenUpstreamContract(_FixedConnectionBrowser(_NotAConnection())),
    ),
    Mutant(
        UpstreamBrowserPortContract,
        "test_command_receives_frame_with_matching_id",
        lambda: _BrokenUpstreamContract(_ConnectionFactoryBrowser(_NoMatchingResponseConnection)),
    ),
    Mutant(
        UpstreamBrowserPortContract,
        "test_send_after_close_raises_upstream_closed",
        lambda: _BrokenUpstreamContract(_ConnectionFactoryBrowser(_SilentSendAfterCloseConnection)),
    ),
    Mutant(
        UpstreamBrowserPortContract,
        "test_receive_after_close_raises_upstream_closed",
        lambda: _BrokenUpstreamContract(_ConnectionFactoryBrowser(_SilentReceiveAfterCloseConnection)),
    ),
    Mutant(
        UpstreamBrowserPortContract,
        "test_close_is_idempotent",
        lambda: _BrokenUpstreamContract(_ConnectionFactoryBrowser(_NonIdempotentCloseConnection)),
    ),
    # DialingUpstreamBrowserPortContract
    Mutant(
        DialingUpstreamBrowserPortContract,
        "test_failed_dial_raises_the_port_error_taxonomy",
        lambda: _BrokenDialingContract(refusing=True),
    ),
    Mutant(
        DialingUpstreamBrowserPortContract,
        "test_remote_close_surfaces_as_upstream_closed_on_receive",
        lambda: _BrokenDialingContract(break_receive=True),
    ),
    Mutant(
        DialingUpstreamBrowserPortContract,
        "test_remote_close_surfaces_as_upstream_closed_on_send",
        lambda: _BrokenDialingContract(break_send=True),
    ),
    # SessionRegistryContract
    Mutant(
        SessionRegistryContract,
        "test_unknown_session_is_an_explicit_negative",
        lambda: _BrokenRegistryContract(_RoutesUnknownRegistry),
    ),
    Mutant(
        SessionRegistryContract,
        "test_active_session_resolves_full_routing",
        lambda: _BrokenRegistryContract(_LosesRoutingOnActiveRegistry),
    ),
    Mutant(
        SessionRegistryContract,
        "test_pending_session_is_rejected_without_routing_data",
        lambda: _BrokenRegistryContract(lambda inner: _RoutesStatusRegistry(inner, SessionResolutionStatus.PENDING)),
    ),
    Mutant(
        SessionRegistryContract,
        "test_closed_session_is_rejected_without_routing_data",
        lambda: _BrokenRegistryContract(lambda inner: _RoutesStatusRegistry(inner, SessionResolutionStatus.CLOSED)),
    ),
    Mutant(
        SessionRegistryContract,
        "test_expired_session_is_rejected_without_routing_data",
        lambda: _BrokenRegistryContract(lambda inner: _RoutesStatusRegistry(inner, SessionResolutionStatus.EXPIRED)),
    ),
    Mutant(
        SessionRegistryContract,
        "test_resolution_repr_never_leaks_the_upstream_url",
        lambda: _BrokenRegistryContract(_LeakyRegistry),
    ),
    Mutant(
        SessionRegistryContract,
        "test_invalidate_is_safe_and_idempotent_for_unknown_sessions",
        lambda: _BrokenRegistryContract(_NonIdempotentInvalidateRegistry),
    ),
)

# The contracts this file claims to give teeth. Every clause each one declares must have
# a mutant below — see test_every_claimed_clause_has_a_mutant.
TEETH_COVERED_CONTRACTS: tuple[type, ...] = (
    EventPolicyPortContract,
    UpstreamBrowserPortContract,
    DialingUpstreamBrowserPortContract,
    SessionRegistryContract,
)


def _declared_clauses(contract: type) -> set[str]:
    """Clauses the contract declares ITSELF — an inherited one belongs to the parent
    that declared it and is covered by the parent's own mutants."""
    return {name for name in vars(contract) if name.startswith("test_")}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutant", MUTANTS, ids=lambda m: f"{m.contract.__name__}.{m.clause}")
async def test_clause_catches_its_mutant(mutant: Mutant) -> None:
    await _assert_contract_catches(mutant.make(), mutant.clause)


def test_every_claimed_clause_has_a_mutant() -> None:
    """The guard that makes this file's claim true rather than partial.

    Without it, teeth cover whatever subset happened to get a mutant, and a clause added
    later — or one already here — can be gutted with everything still green.
    """
    for contract in TEETH_COVERED_CONTRACTS:
        declared = _declared_clauses(contract)
        covered = {mutant.clause for mutant in MUTANTS if mutant.contract is contract}
        assert declared == covered, (
            f"{contract.__name__}: clauses without a mutant: {sorted(declared - covered)}; "
            f"mutants for clauses that no longer exist: {sorted(covered - declared)}"
        )


@pytest.mark.asyncio
async def test_burst_clause_catches_lifecycle_throttling_a_single_event_check_misses() -> None:
    # The narrow clause passes: the first attach/detach still gets through. Only the
    # burst finds it, which is what earns that clause its place in the contract.
    contract = _BrokenPolicyContract(_ThrottlesLifecycleAfterABurst())
    await _assert_contract_allows(contract, "test_lifecycle_events_are_never_dropped")
    await _assert_contract_catches(contract, "test_lifecycle_survives_a_burst_whatever_the_rate_budget")


@pytest.mark.asyncio
async def test_identity_clause_catches_a_rewrite_no_drop_check_would_notice() -> None:
    # The stripping policy drops nothing, so the never-dropped clause is satisfied while
    # the client is stranded anyway. That gap is why the identity clause exists.
    contract = _BrokenPolicyContract(_StripsLifecycleIdentity())
    await _assert_contract_allows(contract, "test_lifecycle_events_are_never_dropped")
    await _assert_contract_catches(contract, "test_lifecycle_events_keep_their_routing_identity")


@pytest.mark.asyncio
async def test_a_deleted_clause_is_reported_rather_than_read_as_a_catch() -> None:
    """Deleting a clause is the cheapest way to make a broken adapter pass its contract.

    It must not be mistaken for the contract working: the lookup fails the same way a
    rejected adapter does, so without an explicit check this file goes green exactly
    when its assertions stop existing.
    """
    with pytest.raises(AssertionError, match="no clause"):
        await _assert_contract_catches(_BrokenPolicyContract(_CompliantPolicy()), "test_clause_someone_renamed_away")


@pytest.mark.asyncio
async def test_the_teeth_helper_itself_fails_when_an_adapter_is_actually_compliant() -> None:
    """The guard on the guard: if _assert_contract_catches treated a passing clause as
    caught, every test above would pass no matter how toothless the contracts got."""
    with pytest.raises(AssertionError, match="no teeth"):
        await _assert_contract_catches(
            _BrokenPolicyContract(_CompliantPolicy()), "test_lifecycle_events_are_never_dropped"
        )
