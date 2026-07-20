"""In-memory adapters: local dev defaults and contract-test references."""

from __future__ import annotations

import asyncio
from typing import Mapping

from skyvern.proxy.core.frames import CdpCommand, CdpEvent
from skyvern.proxy.core.policy import FORWARD, PolicyDecision
from skyvern.proxy.core.session import (
    Principal,
    ProxySession,
    ResolvedSession,
    SessionResolution,
    UpstreamClosedError,
    principal_owns_resolution,
)
from skyvern.proxy.ports import UpstreamConnection


class InMemoryUpstreamConnection:
    """Loopback upstream: frames sent are echoed back to receive()."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False

    async def send(self, raw: str) -> None:
        if self._closed:
            raise UpstreamClosedError("connection is closed")
        await self._queue.put(raw)

    async def receive(self) -> str:
        if self._closed and self._queue.empty():
            raise UpstreamClosedError("connection is closed")
        raw = await self._queue.get()
        if raw is None:
            raise UpstreamClosedError("connection is closed")
        return raw

    async def close(self) -> None:
        self._closed = True
        await self._queue.put(None)


class InMemoryUpstreamBrowser:
    async def connect(self, session: ProxySession) -> UpstreamConnection:
        return InMemoryUpstreamConnection()


class InMemorySessionRegistry:
    """Authoritative in-memory registry for dev and tests; seed with put()/mark_*()."""

    def __init__(self) -> None:
        self._resolutions: dict[str, SessionResolution] = {}

    async def resolve(self, session_id: str) -> SessionResolution:
        return self._resolutions.get(session_id, SessionResolution.unknown())

    async def invalidate(self, session_id: str) -> None:
        """No-op: this store is authoritative, there is no cached state to drop."""
        return None

    def put(self, session: ResolvedSession, expires_in_seconds: float | None = None) -> None:
        self._resolutions[session.session_id] = SessionResolution.active(session, expires_in_seconds)

    def mark_pending(self, session_id: str, organization_id: str | None = None) -> None:
        self._resolutions[session_id] = SessionResolution.pending(organization_id)

    def mark_closed(self, session_id: str, organization_id: str | None = None) -> None:
        self._resolutions[session_id] = SessionResolution.closed(organization_id)

    def mark_expired(self, session_id: str, organization_id: str | None = None) -> None:
        self._resolutions[session_id] = SessionResolution.expired(organization_id)

    def remove(self, session_id: str) -> None:
        self._resolutions.pop(session_id, None)


class AllowAllAuth:
    async def authenticate(self, credentials: Mapping[str, str]) -> Principal | None:
        return Principal(principal_id="local-dev")

    def authorize(self, principal: Principal, resolution: SessionResolution) -> bool:
        return principal_owns_resolution(principal, resolution)


class StaticKeyAuth:
    """Dev/test AuthPort: a fixed api-key -> Principal table.

    The driving adapter normalizes header/query/path credentials into the same
    mapping, so a key presented any of those ways matches here. Key values are
    never logged.
    """

    def __init__(self, keys: Mapping[str, Principal]) -> None:
        self._keys = dict(keys)

    async def authenticate(self, credentials: Mapping[str, str]) -> Principal | None:
        api_key = credentials.get("x-api-key")
        if not api_key:
            return None
        return self._keys.get(api_key)

    def authorize(self, principal: Principal, resolution: SessionResolution) -> bool:
        return principal_owns_resolution(principal, resolution)


class NoOpMetrics:
    def increment(self, name: str, amount: int = 1, tags: Mapping[str, str] | None = None) -> None:
        return None

    def observe(self, name: str, value: float, tags: Mapping[str, str] | None = None) -> None:
        return None

    def gauge(self, name: str, amount: int, tags: Mapping[str, str] | None = None) -> None:
        return None


class ForwardAllEventPolicy:
    """The default policy: every event forwarded unchanged, nothing tracked.

    Deliberately stateless — with no policy configured the proxy must behave exactly
    as it would with no policy engine at all.
    """

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        return FORWARD

    def observe_command(self, command: CdpCommand, session: ProxySession) -> None:
        return None

    def forget(self, session_id: str) -> None:
        return None
