import asyncio
from collections.abc import Awaitable, Callable

import pytest

from skyvern.forge.sdk.experimentation.providers import (
    FEATURE_FLAG_CACHE_BYPASS_NAMES,
    BaseExperimentationProvider,
    DataHashFreshnessCache,
)


class FakeClock:
    def __init__(self) -> None:
        self.current_time = 0.0

    def now(self) -> float:
        return self.current_time

    def advance(self, seconds: float) -> None:
        self.current_time += seconds


class SequenceExperimentationProvider(BaseExperimentationProvider):
    def __init__(self, enabled_results: list[bool]) -> None:
        super().__init__()
        self.enabled_results = enabled_results
        self.enabled_calls = 0
        self.prepare_calls: list[tuple[str, bool]] = []

    async def _prepare_feature_flag_resolution(self, feature_name: str, *, cached: bool) -> None:
        self.prepare_calls.append((feature_name, cached))

    async def _is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        self.enabled_calls += 1
        return self.enabled_results.pop(0)

    async def _get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None

    async def _get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None


def _counting_refresh(counter: list[int]) -> Callable[[], Awaitable[None]]:
    async def refresh() -> None:
        counter[0] += 1

    return refresh


@pytest.mark.asyncio
async def test_data_hash_freshness_cache_uses_ttl_before_refetching() -> None:
    clock = FakeClock()
    cache = DataHashFreshnessCache(ttl_seconds=30, clock=clock.now)
    refresh_count = [0]

    await cache.refresh_if_stale(_counting_refresh(refresh_count))
    clock.advance(29)
    await cache.refresh_if_stale(_counting_refresh(refresh_count))
    clock.advance(2)
    await cache.refresh_if_stale(_counting_refresh(refresh_count))

    assert refresh_count[0] == 2


@pytest.mark.asyncio
async def test_data_hash_freshness_cache_bypass_refetches_each_call() -> None:
    clock = FakeClock()
    cache = DataHashFreshnessCache(ttl_seconds=30, clock=clock.now)
    refresh_count = [0]

    await cache.refresh_if_stale(_counting_refresh(refresh_count), bypass_cache=True)
    clock.advance(1)
    await cache.refresh_if_stale(_counting_refresh(refresh_count), bypass_cache=True)

    assert refresh_count[0] == 2


@pytest.mark.asyncio
async def test_feature_enabled_cached_bypasses_known_kill_switch_flags() -> None:
    provider = SequenceExperimentationProvider([False, True, False])

    assert await provider.is_feature_enabled_cached("RATE_LIMITING_ENABLED", "org_123") is False
    assert await provider.is_feature_enabled_cached("RATE_LIMITING_ENABLED", "org_123") is True
    assert await provider.is_feature_enabled_cached("NOT_A_KILL_SWITCH", "org_123") is False
    assert await provider.is_feature_enabled_cached("NOT_A_KILL_SWITCH", "org_123") is False

    assert "RATE_LIMITING_ENABLED" in FEATURE_FLAG_CACHE_BYPASS_NAMES
    assert provider.enabled_calls == 3
    assert provider.prepare_calls == [
        ("RATE_LIMITING_ENABLED", False),
        ("RATE_LIMITING_ENABLED", False),
        ("NOT_A_KILL_SWITCH", True),
    ]


@pytest.mark.asyncio
async def test_direct_feature_enabled_bypasses_data_hash_ttl_gate() -> None:
    provider = SequenceExperimentationProvider([False, True])

    assert await provider.is_feature_enabled("TEST_FLAG", "org_123") is False
    assert await provider.is_feature_enabled("TEST_FLAG", "org_123") is True

    assert provider.enabled_calls == 2
    assert provider.prepare_calls == [
        ("TEST_FLAG", False),
        ("TEST_FLAG", False),
    ]


@pytest.mark.asyncio
async def test_data_hash_freshness_cache_single_flights_concurrent_cold_misses() -> None:
    cache = DataHashFreshnessCache(ttl_seconds=30)
    refresh_count = 0
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def refresh() -> None:
        nonlocal refresh_count
        refresh_count += 1
        refresh_started.set()
        await release_refresh.wait()

    tasks = [asyncio.create_task(cache.refresh_if_stale(refresh)) for _ in range(10)]
    await refresh_started.wait()
    await asyncio.sleep(0)

    assert refresh_count == 1

    release_refresh.set()
    await asyncio.gather(*tasks)

    assert refresh_count == 1


@pytest.mark.asyncio
async def test_data_hash_freshness_cache_serves_stale_value_during_in_flight_refresh() -> None:
    clock = FakeClock()
    cache = DataHashFreshnessCache(ttl_seconds=30, clock=clock.now)
    local_value = "old"
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def initial_refresh() -> None:
        pass

    await cache.refresh_if_stale(initial_refresh)
    clock.advance(31)

    async def refresh() -> None:
        nonlocal local_value
        refresh_started.set()
        await release_refresh.wait()
        local_value = "new"

    async def read_value() -> str:
        await cache.refresh_if_stale(refresh)
        return local_value

    first_read = asyncio.create_task(read_value())
    await refresh_started.wait()

    assert await asyncio.wait_for(read_value(), timeout=0.05) == "old"

    release_refresh.set()
    assert await first_read == "new"
