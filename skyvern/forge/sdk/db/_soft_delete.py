"""Soft-delete mixin and query helper for SQLAlchemy models.

Usage:
    # On models:
    class WorkflowModel(SoftDeleteMixin, Base): ...

    # In queries:
    query = exclude_deleted(select(WorkflowModel), WorkflowModel)

    # For deletion:
    workflow.mark_deleted()
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Column, DateTime, Select

_T = TypeVar("_T")


class SoftDeleteMixin:
    """Mixin that adds a ``deleted_at`` column and helpers for soft-delete."""

    deleted_at = Column(DateTime, nullable=True)

    @classmethod
    def not_deleted(cls) -> Any:
        """Return a filter clause that excludes soft-deleted rows."""
        return cls.deleted_at.is_(None)

    def mark_deleted(self) -> None:
        """Set ``deleted_at`` to the current UTC time.

        Idempotent: if ``deleted_at`` is already set, this is a no-op so that
        calling it multiple times does not overwrite the original timestamp.
        """
        if self.deleted_at is None:
            # TODO: migrate to datetime.now(UTC) when the codebase standardizes on aware datetimes
            self.deleted_at = datetime.utcnow()


def exclude_deleted(query: Select[_T], model: type[SoftDeleteMixin]) -> Select[_T]:
    """Append a ``deleted_at IS NULL`` filter to *query* for *model*."""
    return query.where(model.deleted_at.is_(None))
