"""TTL-caching decorator over any SessionRegistryPort."""

from __future__ import annotations

import time
from typing import Callable

from skyvern.proxy.core.session import SessionResolution, SessionResolutionStatus
from skyvern.proxy.ports import SessionRegistryPort

DEFAULT_TTL_SECONDS = 15.0
DEFAULT_MAX_ENTRIES = 1024

_UNCACHEABLE = frozenset({SessionResolutionStatus.UNKNOWN, SessionResolutionStatus.PENDING})


class TtlCachingSessionRegistry:
    """Caches resolutions so the connect path doesn't hit the backing store every time.

    ACTIVE, CLOSED, and EXPIRED results are cached for ttl_seconds; CLOSED and
    EXPIRED are terminal states that never revert, so caching those negatives is
    safe. UNKNOWN and PENDING are not cached, so a session created or published
    moments later becomes visible immediately. An ACTIVE entry is additionally
    capped at the resolution's expires_in_seconds so a session is never served
    past its own deadline. invalidate() drops the entry, orphans in-flight
    resolves for the id (so a read started before the close cannot re-insert a
    stale entry after it), and forwards to the inner registry.
    """

    def __init__(
        self,
        inner: SessionRegistryPort,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._inner = inner
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._max_entries = max_entries
        self._cache: dict[str, tuple[float, SessionResolution]] = {}
        self._inflight: dict[str, set[object]] = {}

    async def resolve(self, session_id: str) -> SessionResolution:
        now = self._clock()
        cached = self._cache.get(session_id)
        if cached is not None:
            deadline, resolution = cached
            if now < deadline:
                return resolution
            del self._cache[session_id]
        token = object()
        self._inflight.setdefault(session_id, set()).add(token)
        try:
            resolution = await self._inner.resolve(session_id)
        finally:
            tokens = self._inflight.get(session_id)
            flight_valid = tokens is not None and token in tokens
            if tokens is not None:
                tokens.discard(token)
                if not tokens:
                    self._inflight.pop(session_id, None)
        if resolution.status in _UNCACHEABLE or not flight_valid:
            return resolution
        entry_ttl = self._ttl_seconds
        if resolution.expires_in_seconds is not None:
            entry_ttl = min(entry_ttl, resolution.expires_in_seconds)
        if entry_ttl <= 0:
            return resolution
        now = self._clock()
        self._prune(now)
        if len(self._cache) >= self._max_entries:
            # ponytail: FIFO eviction of the oldest insert; swap for real LRU if hit rates ever matter
            self._cache.pop(next(iter(self._cache)))
        self._cache[session_id] = (now + entry_ttl, resolution)
        return resolution

    async def invalidate(self, session_id: str) -> None:
        self._inflight.pop(session_id, None)
        self._cache.pop(session_id, None)
        await self._inner.invalidate(session_id)

    def _prune(self, now: float) -> None:
        # ponytail: O(n) sweep on insert; swap for a heap if per-node session counts ever make it show up
        stale = [key for key, (deadline, _) in self._cache.items() if deadline <= now]
        for key in stale:
            del self._cache[key]
