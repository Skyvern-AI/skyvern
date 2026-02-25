import asyncio
import contextvars
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, AsyncContextManager, AsyncIterator, Callable

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from skyvern.exceptions import SkyvernException
from skyvern.forge.sdk.db.exceptions import NotFoundError

LOG = structlog.get_logger()

# Application exceptions that should pass through @db_operation without logging.
# These represent expected control flow, not infrastructure failures.
_PASSTHROUGH_EXCEPTIONS: tuple[type[Exception], ...] = (
    NotFoundError,
    SkyvernException,
    ValueError,
)


def db_operation(name: str) -> Callable:
    """Decorator for consistent database operation error handling.

    Wraps async database methods with standardized error handling:
    - Application exceptions (NotFoundError, SkyvernException, ValueError)
      pass through without logging.
    - SQLAlchemyError is logged and re-raised.
    - Unexpected exceptions are logged and re-raised.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except _PASSTHROUGH_EXCEPTIONS:
                raise
            except SQLAlchemyError:
                LOG.exception("SQLAlchemyError", operation=name)
                raise
            except Exception:
                LOG.exception("UnexpectedError", operation=name)
                raise

        return wrapper

    return decorator


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

                    backoff_time = 0.2 * (2**attempt)
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
        self.Session = _SessionFactory(self, async_sessionmaker(bind=db_engine))

    def is_retryable_error(self, error: SQLAlchemyError) -> bool:
        """Check if a database error is retryable. Override in subclasses for specific error handling."""
        return False


class _SessionFactory:
    def __init__(self, db: BaseAlchemyDB, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._db = db
        self._sessionmaker = sessionmaker
        self._session_ctx: contextvars.ContextVar[AsyncSession | None] = contextvars.ContextVar(
            "skyvern_db_session",
            default=None,
        )

    def __call__(self) -> AsyncContextManager[AsyncSession]:
        return self._session()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sessionmaker, name)

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        existing_session = self._session_ctx.get()
        if existing_session is not None:
            yield existing_session
            return

        session = self._sessionmaker()
        token = self._session_ctx.set(session)
        try:
            yield session
        finally:
            self._session_ctx.reset(token)
            try:
                await session.close()
            except SQLAlchemyError as e:
                # Handle transient errors during session cleanup gracefully.
                # This can happen on replicas when the connection is terminated due to
                # WAL replay conflicts. Since the actual DB operation already completed
                # successfully (we're in finally block cleanup), we just log and continue.
                if self._db.is_retryable_error(e):
                    LOG.warning(
                        "Transient error during session close (suppressed)",
                        error=str(e),
                    )
                else:
                    raise
