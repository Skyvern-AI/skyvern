import asyncio
from datetime import datetime, timedelta
from typing import Any, Union

from skyvern.forge.sdk.cache.base import CACHE_EXPIRE_TIME, MAX_CACHE_ITEM, BaseCache


class LocalCache(BaseCache):
    def __init__(self) -> None:
        # Use a regular dict to store (value, expiration_timestamp) tuples
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = asyncio.Lock()  # Async lock for task-safe access within event loop
        self._default_ttl_seconds = CACHE_EXPIRE_TIME.total_seconds()

    def _normalize_expiration(self, ex: Union[int, timedelta, None]) -> float:
        """Convert expiration parameter to Unix timestamp."""
        if ex is None:
            ex = CACHE_EXPIRE_TIME

        if isinstance(ex, timedelta):
            ttl_seconds = ex.total_seconds()
        else:  # isinstance(ex, int)
            ttl_seconds = float(ex)

        return datetime.now().timestamp() + ttl_seconds

    def _is_expired(self, expiration_timestamp: float) -> bool:
        """Check if an entry has expired."""
        return datetime.now().timestamp() > expiration_timestamp

    def _cleanup_expired(self) -> None:
        """Remove expired entries. Called opportunistically."""
        now = datetime.now().timestamp()
        expired_keys = [key for key, (_, exp) in self._cache.items() if now > exp]
        for key in expired_keys:
            self._cache.pop(key, None)

    async def get(self, key: str) -> Any:
        async with self._lock:
            if key not in self._cache:
                return None

            value, expiration = self._cache[key]

            # Check if expired
            if self._is_expired(expiration):
                del self._cache[key]
                return None

            return value

    async def set(self, key: str, value: Any, ex: Union[int, timedelta, None] = CACHE_EXPIRE_TIME) -> None:
        expiration_timestamp = self._normalize_expiration(ex)

        async with self._lock:
            # Enforce max size by removing oldest entries if needed
            if len(self._cache) >= MAX_CACHE_ITEM and key not in self._cache:
                # Remove oldest entry (simple FIFO - could be improved with LRU)
                if self._cache:
                    oldest_key = next(iter(self._cache))
                    del self._cache[oldest_key]

            self._cache[key] = (value, expiration_timestamp)

            # Opportunistic cleanup every 100 insertions
            if len(self._cache) % 100 == 0:
                self._cleanup_expired()
