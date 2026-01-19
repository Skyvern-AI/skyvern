from abc import ABC, abstractmethod
from datetime import timedelta
from types import TracebackType
from typing import Any, Protocol, Self, Union, runtime_checkable

CACHE_EXPIRE_TIME = timedelta(weeks=4)
MAX_CACHE_ITEM = 1000


@runtime_checkable
class AsyncLock(Protocol):
    """Protocol for async context manager locks (compatible with redis.asyncio.lock.Lock and NoopLock)."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...


class NoopLock:
    """
    A no-op lock implementation for use when distributed locking is not available.
    Acts as an async context manager that does nothing - suitable for OSS/local deployments.
    """

    def __init__(self, lock_name: str, blocking_timeout: int = 5, timeout: int = 10) -> None:
        self.lock_name = lock_name
        self.blocking_timeout = blocking_timeout
        self.timeout = timeout

    async def __aenter__(self) -> "NoopLock":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass


class BaseCache(ABC):
    @abstractmethod
    async def set(self, key: str, value: Any, ex: Union[int, timedelta, None] = CACHE_EXPIRE_TIME) -> None:
        pass

    @abstractmethod
    async def get(self, key: str) -> Any:
        pass

    def get_lock(self, lock_name: str, blocking_timeout: int = 5, timeout: int = 10) -> AsyncLock:
        """
        Get a distributed lock for the given name.
        Default implementation returns a no-op lock for OSS deployments.
        Cloud implementations should override this to use Redis locks.
        """
        return NoopLock(lock_name, blocking_timeout, timeout)
