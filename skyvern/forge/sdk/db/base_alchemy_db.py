import asyncio
import contextvars
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any, AsyncContextManager, AsyncIterator, Awaitable, Callable, TypeVar

import structlog
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from skyvern.forge.sdk.db.exceptions import DatabaseConnectionUnavailableError
from skyvern.utils.contained_effects import contained_effect

LOG = structlog.get_logger()

R = TypeVar("R")

# The binding cap across all attempts. A healthy attempt is already capped well below it by
# DATABASE_POOL_TIMEOUT for checkout plus DATABASE_STATEMENT_TIMEOUT_MS for execution.
_READ_RECOVERY_BUDGET_SECONDS = 120.0
_READ_RECOVERY_ATTEMPTS = 3
_READ_RECOVERY_BACKOFF_SECONDS = 0.2
# A close on a broken socket can block on its ROLLBACK, and the expired deadline will not fire
# a second time to interrupt it.
_READ_RECOVERY_CLOSE_SECONDS = 5.0


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


async def read_with_disconnect_recovery(
    session_factory: "_SessionFactory",
    read: Callable[[AsyncSession], Awaitable[R]],
    *,
    operation: str,
    attempts: int = _READ_RECOVERY_ATTEMPTS,
    backoff_seconds: float = _READ_RECOVERY_BACKOFF_SECONDS,
    budget_seconds: float = _READ_RECOVERY_BUDGET_SECONDS,
    **log_fields: Any,
) -> R:
    """Run an idempotent read, retrying a structurally invalidated connection on a fresh session.

    Recovery starts only when SQLAlchemy itself marked the connection invalidated, so an unrelated
    database error, or an ``OperationalError`` the driver did not tie to a dead connection, raises
    on its first attempt. ``budget_seconds`` covers every attempt end to end -- checkout, execution,
    session cleanup and backoff -- and external cancellation is never absorbed by it.

    A caller that owns the session keeps it; every other read runs on a detached session this
    helper closes under its own bound, so an expired deadline cannot then hang on that close.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_error: BaseException | None = None
    attempt = 0
    try:
        async with asyncio.timeout(budget_seconds) as deadline:
            while attempt < attempts:
                # A caller-owned session carries uncommitted state a fresh connection cannot see, so
                # its invalidation is the owner's to resolve; only an unbound read may reconnect.
                caller_owned = session_factory.current() is not None
                acquire_started = loop.time()
                acquired_at: float | None = None
                try:
                    async with session_factory() if caller_owned else session_factory._detached_session() as session:
                        await session.connection()
                        acquired_at = loop.time()
                        return await read(session)
                except Exception as error:
                    # Entry is strictly the driver's own invalidation flag. Once a disconnect is
                    # confirmed, a retry that cannot even obtain a connection is the same outage, so
                    # it exhausts the budget rather than escaping as an unattributed error. Keyed on
                    # never reaching the query, because a refused checkout is not always a
                    # ``DBAPIError``: asyncpg raises a bare ``ConnectionRefusedError``.
                    invalidated = isinstance(error, DBAPIError) and error.connection_invalidated
                    recovering = attempt > 0 and acquired_at is None
                    if not invalidated and not recovering:
                        raise
                    last_error = error
                    LOG.warning(
                        "Database connection lost during read",
                        operation=operation,
                        db_read_attempt=attempt,
                        db_read_recovering=recovering,
                        db_read_caller_owned_session=caller_owned,
                        db_connection_invalidated=invalidated,
                        db_acquire_seconds=round((acquired_at or loop.time()) - acquire_started, 3),
                        db_query_seconds=round(loop.time() - acquired_at, 3) if acquired_at else None,
                        db_read_elapsed_seconds=round(loop.time() - started, 3),
                        **log_fields,
                    )
                    if caller_owned:
                        raise DatabaseConnectionUnavailableError(operation, attempt + 1) from error
                    attempt += 1
                    if attempt < attempts:
                        await asyncio.sleep(backoff_seconds * (2 ** (attempt - 1)))
    except TimeoutError:
        # ``asyncio.TimeoutError`` is the builtin, so a socket timeout from inside the read would
        # otherwise be reported as this budget expiring and claim attempts that never ran.
        if not deadline.expired():
            raise
        LOG.error(
            "Database read exhausted its recovery budget",
            operation=operation,
            db_read_attempt=attempt,
            db_read_budget_seconds=budget_seconds,
            db_read_elapsed_seconds=round(loop.time() - started, 3),
            **log_fields,
        )
        raise DatabaseConnectionUnavailableError(operation, attempt + 1) from last_error

    LOG.error(
        "Database read could not reconnect",
        operation=operation,
        db_read_attempts_exhausted=True,
        db_read_attempt=attempt,
        db_read_elapsed_seconds=round(loop.time() - started, 3),
        **log_fields,
    )
    raise DatabaseConnectionUnavailableError(operation, attempt) from last_error


class BaseAlchemyDB:
    """Base database client with connection and session management."""

    def __init__(self, db_engine: AsyncEngine) -> None:
        self.engine = db_engine
        self.Session = _SessionFactory(self, async_sessionmaker(bind=db_engine))

    def is_retryable_error(self, error: SQLAlchemyError) -> bool:
        """Check if a database error is retryable. Override in subclasses for specific error handling."""
        return False


@dataclass(frozen=True)
class _SessionEntry:
    session: AsyncSession
    task: asyncio.Task[Any] | None


class _SessionFactory:
    def __init__(self, db: BaseAlchemyDB, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._db = db
        self._sessionmaker = sessionmaker
        self._session_ctx: contextvars.ContextVar[_SessionEntry | None] = contextvars.ContextVar(
            "skyvern_db_session",
            default=None,
        )

    def __call__(self) -> AsyncContextManager[AsyncSession]:
        return self._session()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sessionmaker, name)

    def current(self) -> AsyncSession | None:
        entry = self._session_ctx.get()
        current_task = asyncio.current_task()
        if entry is None or current_task is None or entry.task is not current_task:
            return None
        return entry.session

    @asynccontextmanager
    async def bind_connection(self, connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
        session = self._sessionmaker(bind=connection, join_transaction_mode="rollback_only")
        token = self._session_ctx.set(_SessionEntry(session=session, task=asyncio.current_task()))
        try:
            yield session
        finally:
            self._session_ctx.reset(token)
            await session.close()

    @asynccontextmanager
    async def _detached_session(self) -> AsyncIterator[AsyncSession]:
        """A session on its own connection, detached from any caller transaction.

        It neither joins nor becomes the ambient session, so anything read through it cannot see
        a caller's uncommitted rows. ``read_with_disconnect_recovery`` is its only caller, and
        only for reads it has established no caller owns.
        """
        session = self._sessionmaker()
        try:
            yield session
        finally:
            # An abandoned session is better than an unbounded one: this close runs while a broken
            # connection is the likely cause, and the pool reaps what it leaves behind.
            try:
                async with asyncio.timeout(_READ_RECOVERY_CLOSE_SECONDS):
                    await session.close()
            except TimeoutError:
                with contained_effect("detached database session abandoned after a stalled close"):
                    LOG.warning("Timed out closing a detached database session; abandoning it")

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        existing = self._session_ctx.get()
        current_task = asyncio.current_task()
        if existing is not None and current_task is not None and existing.task is current_task:
            yield existing.session
            return

        session = self._sessionmaker()
        token = self._session_ctx.set(_SessionEntry(session=session, task=current_task))
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
