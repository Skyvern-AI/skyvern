import asyncio
from functools import wraps
from typing import Any, Callable

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

LOG = structlog.get_logger()


def read_retry(retries: int = 3) -> Callable:
    """Decorator to retry async database operations on transient failures.

    Args:
        retries: Maximum number of retry attempts (default: 3)
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        async def wrapper(
            base_db: "BaseAlchemyDB",
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            for attempt in range(retries):
                try:
                    return await fn(base_db, *args, **kwargs)
                except SQLAlchemyError as e:
                    if not base_db.is_retryable_error(e):
                        LOG.error("SQLAlchemyError", exc_info=True, attempt=attempt)
                        raise
                    if attempt >= retries - 1:
                        LOG.error("SQLAlchemyError after all retries", exc_info=True, attempt=attempt)
                        raise

                    backoff_time = 0.1 * (2**attempt)
                    LOG.warning(
                        "SQLAlchemyError retrying",
                        attempt=attempt,
                        backoff_time=backoff_time,
                        exc_info=True,
                    )
                    await asyncio.sleep(backoff_time)

                except Exception:
                    LOG.error("UnexpectedError", exc_info=True)
                    raise

            raise RuntimeError(f"Retry logic error in {fn.__name__}")

        return wrapper

    return decorator


class BaseAlchemyDB:
    """Base database client with connection and session management."""

    def __init__(self, db_engine: AsyncEngine) -> None:
        self.engine = db_engine
        self.Session = async_sessionmaker(bind=db_engine)

    def is_retryable_error(self, error: SQLAlchemyError) -> bool:
        """Check if a database error is retryable. Override in subclasses for specific error handling."""
        return False
