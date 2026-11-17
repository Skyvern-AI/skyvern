import asyncio
import math
import time
import weakref
from datetime import timedelta
from types import TracebackType
from typing import Any, Self

from cachetools import TLRUCache

from skyvern.forge.sdk.cache.base import CACHE_EXPIRE_TIME, BaseCache


class _ProcessLock:
    # Ignores the Redis-style auto-expiry `timeout`: a holder that hangs blocks waiters until blocking_timeout.
    def __init__(self, lock: asyncio.Lock, blocking_timeout: float) -> None:
        self._lock = lock
        self._blocking_timeout = blocking_timeout

    async def __aenter__(self) -> Self:
        await asyncio.wait_for(self._lock.acquire(), self._blocking_timeout)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._lock.release()


class ProcessLocalCache(BaseCache):
    """Unlike LocalCache, honors each item's expiry and gives real locks, but only within this process."""

    def __init__(self, maxsize: int) -> None:
        self._items: TLRUCache = TLRUCache(maxsize=maxsize, ttu=lambda _key, item, _now: item[1], timer=time.monotonic)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def get(self, key: str) -> Any:
        item = self._items.get(key)
        return None if item is None else item[0]

    async def set(self, key: str, value: Any, ex: int | timedelta | None = CACHE_EXPIRE_TIME) -> None:
        seconds = ex.total_seconds() if isinstance(ex, timedelta) else ex
        self._items[key] = (value, math.inf if seconds is None else time.monotonic() + seconds)

    def get_lock(self, lock_name: str, blocking_timeout: int = 5, timeout: int = 10) -> _ProcessLock:
        lock = self._locks.get(lock_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[lock_name] = lock
        return _ProcessLock(lock, blocking_timeout)
