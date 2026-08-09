"""Authorization boundary for one direct HTTP request or redirect hop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias, TypeVar

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


async def deny_unenrolled_redirect_hop(
    _authorization: RedirectHopAuthorization, _dispatch: RedirectHopDispatcher[T]
) -> T:
    """The named sentinel for a call site with no run-scoped authority to enroll yet.

    A missing authorizer and a deliberately-unenrolled one must be distinguishable at the type
    level, so a bare construction site passes this rather than leaving the argument out or
    defaulting to one that dispatches. It never calls ``dispatch``, so it never lets a hop reach
    the network.
    """
    raise RuntimeError("Redirect hop authorization is not enrolled for this browser session")


def is_unenrolled_redirect_hop_authorizer(authorizer: RedirectHopAuthorizer[Any]) -> bool:
    """Whether this authorizer is the unenrolled sentinel, which denies every hop it is given.

    Call sites use this to report an unenrolled seam as its own operational state instead of
    surfacing the sentinel's denial as an indistinguishable transport failure.
    """
    return authorizer is deny_unenrolled_redirect_hop
