from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Iterable

from skyvern.proxy.core.frames import CdpFrame
from skyvern.proxy.core.session import ProxySession


class Direction(enum.Enum):
    CLIENT_TO_UPSTREAM = "client_to_upstream"
    UPSTREAM_TO_CLIENT = "upstream_to_client"


Middleware = Callable[[CdpFrame, Direction, ProxySession], Awaitable[CdpFrame | None]]


class MiddlewarePipeline:
    """Runs each frame through an ordered middleware chain; returning None drops the frame."""

    def __init__(self, middlewares: Iterable[Middleware] | None = None) -> None:
        self._middlewares = list(middlewares or ())

    def add(self, middleware: Middleware) -> None:
        self._middlewares.append(middleware)

    async def process(self, frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame | None:
        current: CdpFrame | None = frame
        for middleware in self._middlewares:
            if current is None:
                return None
            current = await middleware(current, direction, session)
        return current
