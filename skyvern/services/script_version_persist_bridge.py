"""Bridge from v3 persist skills to workflow-script versioning helpers.

`workflow_script_service` owns the real create-version implementations and
registers them at module import time. Persist skills can import this bridge
without importing `workflow_script_service`, which keeps the v3 skill registry
and mint-review imports acyclic. Runtime callers should ensure
`workflow_script_service` has been imported before invoking persist skills.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

_PersistHandler = Callable[..., Awaitable[Any]]

_create_from_review: _PersistHandler | None = None
_create_from_full_code: _PersistHandler | None = None


def register_script_version_persist_handlers(
    *,
    create_from_review: _PersistHandler,
    create_from_full_code: _PersistHandler,
) -> None:
    """Register workflow-script persist helpers without importing their owner.

    The v3 persist skills are imported while the v3 registry is built, and
    `workflow_script_service` also imports v3 mint-review code. A tiny bridge
    keeps those imports acyclic while preserving top-level imports in callers.
    """
    global _create_from_review, _create_from_full_code
    _create_from_review = create_from_review
    _create_from_full_code = create_from_full_code


async def create_script_version_from_review(*args: Any, **kwargs: Any) -> Any:
    if _create_from_review is None:
        raise RuntimeError("script version review persist handler is not registered")
    return await _create_from_review(*args, **kwargs)


async def create_script_version_from_full_code(*args: Any, **kwargs: Any) -> Any:
    if _create_from_full_code is None:
        raise RuntimeError("script version full-code persist handler is not registered")
    return await _create_from_full_code(*args, **kwargs)
