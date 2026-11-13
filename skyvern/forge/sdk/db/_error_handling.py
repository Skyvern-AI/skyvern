"""Standardized error handling decorator for database operations.

Eliminates duplicated try/except/log blocks across ~100+ methods in agent_db.py.

Note: All exception logging now flows through this module's logger
(``skyvern.forge.sdk.db._error_handling``) rather than per-file loggers.
Datadog filters keyed on specific logger names should be updated accordingly.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Callable, ParamSpec, TypeVar

import structlog
from sqlalchemy.exc import SQLAlchemyError, StatementError

from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.statement_timeout import is_statement_timeout

LOG = structlog.get_logger()

P = ParamSpec("P")
R = TypeVar("R")

# Business-logic exceptions that should pass through the decorator without
# being logged as unexpected errors.  These are normal control-flow signals,
# not infrastructure failures.
_PASSTHROUGH_EXCEPTIONS: tuple[type[Exception], ...] = (NotFoundError,)


def register_passthrough_exception(exc_type: type[Exception]) -> None:
    """Add an exception type to the pass-through set at import time.

    Call this from modules that define business-logic exceptions which
    ``@db_operation`` should re-raise silently (e.g. ScheduleLimitExceededError).

    **Important:** This must only be called at module import time (top-level),
    not dynamically at runtime.  It mutates a module-level tuple that is read
    by concurrent async exception handlers without locking.
    """
    global _PASSTHROUGH_EXCEPTIONS  # noqa: PLW0603
    if exc_type not in _PASSTHROUGH_EXCEPTIONS:
        _PASSTHROUGH_EXCEPTIONS = (*_PASSTHROUGH_EXCEPTIONS, exc_type)


def _masked_cancellation(exc: BaseException, cancellations_before: int) -> asyncio.CancelledError | None:
    # SQLAlchemy's psycopg pre-ping restores autocommit in a `finally`, replacing a mid-ping cancellation with
    # "can't change 'autocommit' now"; return the original, since anyio scopes only absorb their own CancelledError.
    task = asyncio.current_task()
    if task is None or task.cancelling() <= cancellations_before:
        return None
    seen: set[int] = set()
    ctx = exc.__context__
    while ctx is not None and id(ctx) not in seen:
        if isinstance(ctx, asyncio.CancelledError):
            return ctx
        seen.add(id(ctx))
        ctx = ctx.__context__
    return None


def db_operation(
    operation_name: str,
    log_errors: bool = True,
    expected_errors: tuple[type[Exception], ...] = (),
    warn_on_statement_timeout: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that wraps an async function with standardized DB error handling.

    - Pass-through exceptions (NotFoundError, ScheduleLimitExceededError, etc.):
      logged at WARNING level then re-raised — visible to monitoring but not
      treated as infrastructure errors.
    - expected_errors: logged at DEBUG then re-raised, for conditions the caller
      already handles as a normal outcome.
    - SQLAlchemyError: logged with LOG.error() then re-raised
    - Exception: logged with LOG.error() then re-raised

    Args:
        operation_name: Human-readable name used in log messages for context.
        log_errors: Whether to log errors before re-raising. Set to False when
            stacked under @read_retry() to avoid duplicate log entries.
        expected_errors: Exception types this operation's callers treat as an
            expected, handled outcome rather than a failure. Declaring one keeps
            the wrapper from asserting an error level the caller contradicts.
            Empty by default, so every operation is loud unless it opts in — keep
            a declared set no wider than the caller's own ``except``.
        warn_on_statement_timeout: Log a statement-timeout cancellation at WARNING
            instead of ERROR, for an operation whose every caller handles the
            cancellation and reports it itself. ``expected_errors`` cannot express
            this, since the declarable type is wider than the cancellation. Mirrors
            ``log_statement_timeouts`` on ``@read_retry()``. The event name stays
            ``SQLAlchemyError`` so message-keyed monitors still count the line.

    Usage:
        @db_operation("get_task")
        async def get_task(self, task_id: str) -> Task:
            async with self.Session() as session:
                # just the happy path
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(f"@db_operation requires an async function, got {fn!r}")

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:  # type: ignore[return]
            task = asyncio.current_task()
            cancellations_before = task.cancelling() if task is not None else 0
            if cancellations_before:
                # A caught cancellation leaves its count behind; deliver any pending request before treating it as stale.
                await asyncio.sleep(0)
            try:
                try:
                    return await fn(*args, **kwargs)  # type: ignore[misc]
                except SQLAlchemyError as exc:
                    if isinstance(exc, StatementError):
                        exc.hide_parameters = True
                    cancellation = _masked_cancellation(exc, cancellations_before)
                    if cancellation is not None:
                        raise cancellation from exc
                    raise
            except _PASSTHROUGH_EXCEPTIONS:
                if log_errors:
                    LOG.warning("BusinessLogicError", operation=operation_name, exc_info=True)
                raise
            # Must precede the SQLAlchemyError branch: a declared type is usually a subclass of it.
            except expected_errors as exc:
                if log_errors:
                    LOG.debug(
                        "ExpectedError",
                        operation=operation_name,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                raise
            except SQLAlchemyError as exc:
                if log_errors:
                    if warn_on_statement_timeout and is_statement_timeout(exc):
                        LOG.warning("SQLAlchemyError", operation=operation_name, exc_info=True)
                    else:
                        LOG.exception("SQLAlchemyError", operation=operation_name)
                raise
            except Exception:
                if log_errors:
                    LOG.exception("UnexpectedError", operation=operation_name)
                raise

        return wrapper  # type: ignore[return-value]

    return decorator
