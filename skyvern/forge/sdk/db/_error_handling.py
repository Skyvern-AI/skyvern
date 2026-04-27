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
from sqlalchemy.exc import SQLAlchemyError

from skyvern.forge.sdk.db.exceptions import NotFoundError

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


def db_operation(operation_name: str, log_errors: bool = True) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that wraps an async function with standardized DB error handling.

    - Pass-through exceptions (NotFoundError, ScheduleLimitExceededError, etc.):
      logged at WARNING level then re-raised — visible to monitoring but not
      treated as infrastructure errors.
    - SQLAlchemyError: logged with LOG.error() then re-raised
    - Exception: logged with LOG.error() then re-raised

    Args:
        operation_name: Human-readable name used in log messages for context.
        log_errors: Whether to log errors before re-raising. Set to False when
            stacked under @read_retry() to avoid duplicate log entries.

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
            try:
                return await fn(*args, **kwargs)  # type: ignore[misc]
            except _PASSTHROUGH_EXCEPTIONS:
                if log_errors:
                    LOG.warning("BusinessLogicError", operation=operation_name, exc_info=True)
                raise
            except SQLAlchemyError:
                if log_errors:
                    LOG.exception("SQLAlchemyError", operation=operation_name)
                raise
            except Exception:
                if log_errors:
                    LOG.exception("UnexpectedError", operation=operation_name)
                raise

        return wrapper  # type: ignore[return-value]

    return decorator
