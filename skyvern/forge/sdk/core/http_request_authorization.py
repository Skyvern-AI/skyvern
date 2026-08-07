"""Authorization boundary for one direct HTTP request or redirect hop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeAlias, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RedirectHopAuthorization:
    """The already-validated live effect presented to the trusted approval consumer."""

    source_url: str | None
    target_url: str
    method: str


RedirectHopDispatcher: TypeAlias = Callable[[tuple[str, ...]], Awaitable[T]]
RedirectHopAuthorizer: TypeAlias = Callable[
    [RedirectHopAuthorization, RedirectHopDispatcher[T]],
    Awaitable[T],
]


async def authorize_request_hop_once(
    authorizer: RedirectHopAuthorizer[T],
    authorization: RedirectHopAuthorization,
    dispatcher: RedirectHopDispatcher[T],
) -> T:
    """Authorize exactly one in-scope network dispatch for a validated hop."""
    authorizing_task = asyncio.current_task()
    active = True
    dispatched = False

    async def dispatch_once(resolved_values: tuple[str, ...]) -> T:
        nonlocal dispatched
        if not active:
            raise RuntimeError("Redirect hop dispatcher is no longer active")
        if asyncio.current_task() is not authorizing_task:
            raise RuntimeError("Redirect hop dispatcher must run in the authorizing task")
        if dispatched:
            raise RuntimeError("Redirect hop dispatcher can only be invoked once")
        dispatched = True
        return await dispatcher(resolved_values)

    try:
        return await authorizer(authorization, dispatch_once)
    finally:
        active = False
