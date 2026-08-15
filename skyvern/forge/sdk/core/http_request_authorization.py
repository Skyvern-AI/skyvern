"""Authorization boundary for one direct HTTP request or redirect hop."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias, TypeVar

from skyvern.forge.sdk.browser_effect_approval import canonicalize_effect_target

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RedirectHopAuthorization:
    """The already-validated live effect presented to the trusted approval consumer."""

    source_url: str | None
    target_url: str
    method: str
    download_scope: str | None = None
    initial_url: str | None = None


RedirectHopDispatcher: TypeAlias = Callable[[tuple[str, ...]], Awaitable[T]]
RedirectHopAuthorizer: TypeAlias = Callable[
    [RedirectHopAuthorization, RedirectHopDispatcher[T]],
    Awaitable[T],
]


@dataclass(frozen=True, slots=True)
class RunScopedRedirectHopAuthorizer:
    download_scope: str

    def __post_init__(self) -> None:
        if not self.download_scope.strip():
            raise ValueError("Run-scoped redirect-hop authority requires a nonempty download scope")

    async def __call__(
        self,
        authorization: RedirectHopAuthorization,
        dispatch: RedirectHopDispatcher[T],
    ) -> T:
        if authorization.method != "GET":
            raise PermissionError("Run-scoped redirect-hop authority permits only GET requests")
        if authorization.download_scope != self.download_scope or not authorization.initial_url:
            raise PermissionError("Redirect hop is not bound to this run-scoped browser download")
        # Bind the first dispatch to the exact URL Chromium reported. Later targets may legitimately
        # move to a CDN or signed-object origin; fetch_file_bytes validates and DNS-pins each of those
        # redirects before presenting the hop here, and strips credentials when the origin changes.
        if authorization.source_url is None and canonicalize_effect_target(
            authorization.target_url
        ) != canonicalize_effect_target(authorization.initial_url):
            raise PermissionError("Redirect hop is not bound to this run-scoped browser download")
        return await dispatch(())


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
