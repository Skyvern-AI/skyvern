"""Base class for all repository classes extracted from AgentDB mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import _SessionFactory


class BaseRepository:
    """Base for domain-specific repositories.

    Provides the session factory, debug flag, and retryable-error check
    that decorators like ``read_retry`` and ``db_operation`` rely on.
    """

    def __init__(
        self,
        session_factory: _SessionFactory,
        debug_enabled: bool = False,
        is_retryable_error_fn: Callable[[SQLAlchemyError], bool] | None = None,
    ) -> None:
        self.Session = session_factory
        self.debug_enabled = debug_enabled
        self._is_retryable_error_fn = is_retryable_error_fn

    def is_retryable_error(self, error: SQLAlchemyError) -> bool:
        if self._is_retryable_error_fn:
            return self._is_retryable_error_fn(error)
        return False
