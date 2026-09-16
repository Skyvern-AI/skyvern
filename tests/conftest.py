"""Fixtures shared by every suite."""

import inspect
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest


class ForcedSinkFailure(RuntimeError):
    """Stands in for a real sink error when a test forces a side effect to fail."""


@pytest.fixture
def failing_sink(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Replace a recording sink with one that raises, to assert the caller's decision survives.

    A contained side effect and an uncontained one have byte-identical happy paths, which
    is why review and CI both miss escapes. Making the sink raise is what tells them
    apart::

        failing_sink(workflow_module.workflow, "upsert_search_attributes")

    Pass ``when`` to break only some calls, for the common shape where one sink serves
    both the happy path and a finalizer and only the finalizer's call runs under the
    conditions that break it. It receives the call's arguments; calls it rejects no-op.

    The replacement matches the original's sync/async kind, and patching a name that does
    not exist is an error rather than a silently passing test.
    """

    def install(
        target: object,
        attribute: str,
        *,
        exc: BaseException | None = None,
        when: Callable[..., bool] | None = None,
    ) -> None:
        is_async = inspect.iscoroutinefunction(getattr(target, attribute))
        failure = exc if exc is not None else ForcedSinkFailure(f"{attribute} was forced to fail")

        def should_fail(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
            return when is None or when(*args, **kwargs)

        if is_async:

            async def async_sink(*args: Any, **kwargs: Any) -> None:
                if should_fail(args, kwargs):
                    raise failure

            monkeypatch.setattr(target, attribute, async_sink)
            return

        def sync_sink(*args: Any, **kwargs: Any) -> None:
            if should_fail(args, kwargs):
                raise failure

        monkeypatch.setattr(target, attribute, sync_sink)

    return install


@pytest.fixture
def no_saved_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give standalone YAML conversion tests an explicit empty saved-workflow lookup."""
    from skyvern.forge import app

    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
