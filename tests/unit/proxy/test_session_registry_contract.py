"""Reusable contract suite for SessionRegistryPort adapters.

Any adapter (generic here, cloud-specific under tests/cloud/) subclasses
SessionRegistryContract and overrides make_registry(); every behavioral
guarantee of the port is asserted once, here.
"""

from __future__ import annotations

import asyncio

import pytest

from skyvern.proxy.adapters.caching import TtlCachingSessionRegistry
from skyvern.proxy.adapters.memory import InMemorySessionRegistry
from skyvern.proxy.core.session import ResolvedSession, SessionResolution, SessionResolutionStatus
from skyvern.proxy.ports import SessionRegistryPort

SECRET_UPSTREAM_URL = "ws://upstream.internal:9222/devtools/browser/abc?token=secret-token"


def make_resolved_session(session_id: str = "pbs_active", organization_id: str | None = "o_1") -> ResolvedSession:
    return ResolvedSession(
        session_id=session_id,
        upstream_adapter="websocket",
        upstream_ws_url=SECRET_UPSTREAM_URL,
        organization_id=organization_id,
    )


class SessionRegistryContract:
    def make_registry(
        self,
        *,
        active: tuple[ResolvedSession, ...] = (),
        pending: tuple[str, ...] = (),
        closed: tuple[str, ...] = (),
        expired: tuple[str, ...] = (),
    ) -> SessionRegistryPort:
        raise NotImplementedError

    @pytest.mark.asyncio
    async def test_unknown_session_is_an_explicit_negative(self) -> None:
        resolution = await self.make_registry().resolve("pbs_missing")
        assert resolution.status is SessionResolutionStatus.UNKNOWN
        assert resolution.session is None

    @pytest.mark.asyncio
    async def test_active_session_resolves_full_routing(self) -> None:
        seeded = make_resolved_session()
        registry = self.make_registry(active=(seeded,))
        resolution = await registry.resolve(seeded.session_id)
        assert resolution.status is SessionResolutionStatus.ACTIVE
        assert resolution.session == seeded

    @pytest.mark.asyncio
    async def test_pending_session_is_rejected_without_routing_data(self) -> None:
        resolution = await self.make_registry(pending=("pbs_pending",)).resolve("pbs_pending")
        assert resolution.status is SessionResolutionStatus.PENDING
        assert resolution.session is None

    @pytest.mark.asyncio
    async def test_closed_session_is_rejected_without_routing_data(self) -> None:
        resolution = await self.make_registry(closed=("pbs_closed",)).resolve("pbs_closed")
        assert resolution.status is SessionResolutionStatus.CLOSED
        assert resolution.session is None

    @pytest.mark.asyncio
    async def test_expired_session_is_rejected_without_routing_data(self) -> None:
        resolution = await self.make_registry(expired=("pbs_expired",)).resolve("pbs_expired")
        assert resolution.status is SessionResolutionStatus.EXPIRED
        assert resolution.session is None

    @pytest.mark.asyncio
    async def test_resolution_repr_never_leaks_the_upstream_url(self) -> None:
        seeded = make_resolved_session()
        resolution = await self.make_registry(active=(seeded,)).resolve(seeded.session_id)
        assert "secret-token" not in repr(resolution)
        assert seeded.upstream_ws_url not in repr(resolution)

    @pytest.mark.asyncio
    async def test_invalidate_is_safe_and_idempotent_for_unknown_sessions(self) -> None:
        registry = self.make_registry()
        await registry.invalidate("pbs_missing")
        await registry.invalidate("pbs_missing")


class TestInMemorySessionRegistry(SessionRegistryContract):
    def make_registry(
        self,
        *,
        active: tuple[ResolvedSession, ...] = (),
        pending: tuple[str, ...] = (),
        closed: tuple[str, ...] = (),
        expired: tuple[str, ...] = (),
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

    @pytest.mark.asyncio
    async def test_put_preserves_adapter_selector_and_connect_headers(self) -> None:
        seeded = ResolvedSession(
            session_id="pbs_vendor",
            upstream_adapter="hosted-pool-a",
            upstream_ws_url="wss://pool.internal/session/1",
            organization_id="o_2",
            connect_headers={"authorization": "Bearer operator-token"},
        )
        registry = self.make_registry(active=(seeded,))
        resolution = await registry.resolve("pbs_vendor")
        assert resolution.session == seeded
        assert resolution.session.connect_headers == {"authorization": "Bearer operator-token"}

    @pytest.mark.asyncio
    async def test_remove_returns_session_to_unknown(self) -> None:
        registry = self.make_registry(active=(make_resolved_session(),))
        registry.remove("pbs_active")
        resolution = await registry.resolve("pbs_active")
        assert resolution.status is SessionResolutionStatus.UNKNOWN


class TestTtlCachingSessionRegistry(SessionRegistryContract):
    def make_registry(
        self,
        *,
        active: tuple[ResolvedSession, ...] = (),
        pending: tuple[str, ...] = (),
        closed: tuple[str, ...] = (),
        expired: tuple[str, ...] = (),
    ) -> SessionRegistryPort:
        inner = TestInMemorySessionRegistry().make_registry(
            active=active, pending=pending, closed=closed, expired=expired
        )
        return TtlCachingSessionRegistry(inner)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingRegistry:
    def __init__(self, inner: SessionRegistryPort) -> None:
        self.inner = inner
        self.resolve_calls = 0
        self.invalidated: list[str] = []

    async def resolve(self, session_id: str) -> SessionResolution:
        self.resolve_calls += 1
        return await self.inner.resolve(session_id)

    async def invalidate(self, session_id: str) -> None:
        self.invalidated.append(session_id)
        await self.inner.invalidate(session_id)


def make_cached_counting_registry(
    inner: InMemorySessionRegistry, ttl_seconds: float = 15.0
) -> tuple[TtlCachingSessionRegistry, CountingRegistry, FakeClock]:
    clock = FakeClock()
    counting = CountingRegistry(inner)
    return TtlCachingSessionRegistry(counting, ttl_seconds=ttl_seconds, clock=clock), counting, clock


@pytest.mark.asyncio
async def test_repeat_resolve_within_ttl_hits_backing_store_once() -> None:
    inner = InMemorySessionRegistry()
    inner.put(make_resolved_session())
    cached, counting, _ = make_cached_counting_registry(inner)

    first = await cached.resolve("pbs_active")
    second = await cached.resolve("pbs_active")

    assert counting.resolve_calls == 1
    assert first == second


@pytest.mark.asyncio
async def test_resolution_refreshes_after_ttl_elapses() -> None:
    inner = InMemorySessionRegistry()
    inner.put(make_resolved_session())
    cached, counting, clock = make_cached_counting_registry(inner, ttl_seconds=15.0)

    await cached.resolve("pbs_active")
    clock.advance(15.0)
    inner.mark_closed("pbs_active")
    resolution = await cached.resolve("pbs_active")

    assert counting.resolve_calls == 2
    assert resolution.status is SessionResolutionStatus.CLOSED


@pytest.mark.asyncio
async def test_invalidate_on_close_drops_cache_and_forwards() -> None:
    inner = InMemorySessionRegistry()
    inner.put(make_resolved_session())
    cached, counting, _ = make_cached_counting_registry(inner)

    assert (await cached.resolve("pbs_active")).status is SessionResolutionStatus.ACTIVE
    inner.mark_closed("pbs_active")
    await cached.invalidate("pbs_active")
    resolution = await cached.resolve("pbs_active")

    assert resolution.status is SessionResolutionStatus.CLOSED
    assert counting.invalidated == ["pbs_active"]


@pytest.mark.asyncio
async def test_unknown_and_pending_are_not_cached() -> None:
    inner = InMemorySessionRegistry()
    inner.mark_pending("pbs_pending")
    cached, counting, _ = make_cached_counting_registry(inner)

    assert (await cached.resolve("pbs_new")).status is SessionResolutionStatus.UNKNOWN
    assert (await cached.resolve("pbs_pending")).status is SessionResolutionStatus.PENDING
    inner.put(make_resolved_session(session_id="pbs_new"))
    inner.put(make_resolved_session(session_id="pbs_pending"))

    assert (await cached.resolve("pbs_new")).status is SessionResolutionStatus.ACTIVE
    assert (await cached.resolve("pbs_pending")).status is SessionResolutionStatus.ACTIVE
    assert counting.resolve_calls == 4


@pytest.mark.asyncio
async def test_terminal_negatives_are_cached() -> None:
    inner = InMemorySessionRegistry()
    inner.mark_closed("pbs_closed")
    inner.mark_expired("pbs_expired")
    cached, counting, _ = make_cached_counting_registry(inner)

    for _ in range(2):
        assert (await cached.resolve("pbs_closed")).status is SessionResolutionStatus.CLOSED
        assert (await cached.resolve("pbs_expired")).status is SessionResolutionStatus.EXPIRED

    assert counting.resolve_calls == 2


@pytest.mark.asyncio
async def test_active_cache_entry_is_capped_at_the_session_deadline() -> None:
    inner = InMemorySessionRegistry()
    inner.put(make_resolved_session(), expires_in_seconds=5.0)
    cached, counting, clock = make_cached_counting_registry(inner, ttl_seconds=15.0)

    await cached.resolve("pbs_active")
    clock.advance(3.0)
    await cached.resolve("pbs_active")
    assert counting.resolve_calls == 1

    clock.advance(2.0)
    inner.mark_expired("pbs_active")
    resolution = await cached.resolve("pbs_active")

    assert counting.resolve_calls == 2
    assert resolution.status is SessionResolutionStatus.EXPIRED


class DelayedRegistry:
    """Inner registry whose first resolve blocks until released, to race invalidate()."""

    def __init__(self, resolution: SessionResolution) -> None:
        self.resolution = resolution
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.resolve_calls = 0

    async def resolve(self, session_id: str) -> SessionResolution:
        self.resolve_calls += 1
        if self.resolve_calls == 1:
            self.entered.set()
            await self.release.wait()
        return self.resolution

    async def invalidate(self, session_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_invalidate_during_inflight_resolve_never_caches_the_stale_result() -> None:
    inner = DelayedRegistry(SessionResolution.active(make_resolved_session()))
    cached = TtlCachingSessionRegistry(inner, ttl_seconds=60.0)

    inflight = asyncio.create_task(cached.resolve("pbs_active"))
    await inner.entered.wait()
    await cached.invalidate("pbs_active")
    inner.release.set()
    stale = await inflight

    assert stale.status is SessionResolutionStatus.ACTIVE
    inner.resolution = SessionResolution.closed()
    fresh = await cached.resolve("pbs_active")
    assert fresh.status is SessionResolutionStatus.CLOSED
    assert inner.resolve_calls == 2


@pytest.mark.asyncio
async def test_cache_size_is_bounded_with_oldest_entry_evicted() -> None:
    inner = InMemorySessionRegistry()
    for index in range(3):
        inner.put(make_resolved_session(session_id=f"pbs_{index}"))
    counting = CountingRegistry(inner)
    cached = TtlCachingSessionRegistry(counting, ttl_seconds=60.0, clock=FakeClock(), max_entries=2)

    for index in range(3):
        await cached.resolve(f"pbs_{index}")
    assert len(cached._cache) == 2

    await cached.resolve("pbs_0")
    assert counting.resolve_calls == 4
