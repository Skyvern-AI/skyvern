import asyncio
import time
from functools import wraps
from typing import Any, Callable, Type, TypeVar, cast

import structlog

LOG = structlog.get_logger()

# Type for any callable (sync or async)
F = TypeVar("F", bound=Callable[..., Any])


def retry(
    exceptions: Type[Exception] | tuple[Type[Exception], ...] | None = None,
    tries: int = 3,
    delay: float = 3.0,
    backoff: float = 2.0,
) -> Callable[[F], F]:
    """
    Decorator to retry a function a specified number of times with a delay between attempts.
    Works with both async and sync functions.

    Args:
        exceptions: A tuple of exceptions to catch and retry on.
        tries: The total number attempts to make.
        delay: The initial delay between attempts.
        backoff: The factor by which the delay increases after each attempt.
    """
    if exceptions is None:
        exceptions = Exception

    def retry_decorator(func: F) -> F:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            remaining_tries, current_delay = tries, delay
            while remaining_tries > 1:
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    LOG.warning(f"Retrying {func.__name__} in {current_delay} seconds... {e}")
                    await asyncio.sleep(current_delay)
                    remaining_tries -= 1
                    current_delay *= backoff
            return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            remaining_tries, current_delay = tries, delay
            while remaining_tries > 1:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    LOG.warning(f"Retrying {func.__name__} in {current_delay} seconds... {e}")
                    time.sleep(current_delay)
                    remaining_tries -= 1
                    current_delay *= backoff
            return func(*args, **kwargs)

        # Return async wrapper if function is async, otherwise sync wrapper
        if asyncio.iscoroutinefunction(func):
            return cast(F, async_wrapper)
        return cast(F, sync_wrapper)

    return retry_decorator
