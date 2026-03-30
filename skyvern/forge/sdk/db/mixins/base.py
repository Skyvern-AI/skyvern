"""Shared dependencies for mixin modules.

This module is the single import point for base classes and utilities that
sibling mixin modules need (e.g. ``BaseAlchemyDB``, ``read_retry``).
Centralising the import here means every mixin can do
``from .base import BaseAlchemyDB, read_retry`` instead of reaching up to
the parent package, keeping coupling explicit and consistent.
"""

from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB, read_retry

__all__ = ["BaseAlchemyDB", "read_retry"]
