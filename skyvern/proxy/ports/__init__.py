"""Driven ports for the CDP proxy core.

Each port is a Protocol; adapters implement them without the core knowing which.
Adding a new hosted-browser vendor means implementing UpstreamBrowserPort alone —
zero core changes. Every adapter must pass the reusable contract suite for its
port (see tests/unit/proxy/).
"""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from skyvern.proxy.core.errors import (
    LaunchEnvironmentError,
    LaunchTimeoutError,
    ProtocolConfigurationError,
    TransientConnectionError,
    UpstreamConnectError,
    VendorAuthError,
    VendorRateLimitError,
)
from skyvern.proxy.core.frames import CdpCommand, CdpEvent
from skyvern.proxy.core.policy import PolicyDecision
from skyvern.proxy.core.session import (
    Principal,
    ProxySession,
    ResolvedSession,
    SessionResolution,
    SessionResolutionStatus,
)


@runtime_checkable
class UpstreamConnection(Protocol):
    """One live browser-side connection; frames are raw CDP JSON strings."""

    async def send(self, raw: str) -> None:
        """Sends one raw frame; raises UpstreamClosedError once the upstream is gone."""
        ...

    async def receive(self) -> str:
        """Returns the next raw frame; raises UpstreamClosedError once the upstream is gone."""
        ...

    async def close(self) -> None:
        """Releases the connection and any resources behind it. Idempotent."""
        ...


@runtime_checkable
class UpstreamBrowserPort(Protocol):
    """Connects to the browser side of a session. The only port a new vendor needs.

    Failures raise UpstreamConnectError subclasses (skyvern.proxy.core.errors) so
    callers never branch on transport exception types. Any credentials the upstream
    needs are attached by the adapter from proxy-operator configuration — never
    taken from client input.
    """

    async def connect(self, session: ProxySession) -> UpstreamConnection: ...


@runtime_checkable
class SessionRegistryPort(Protocol):
    """Resolves a session id into upstream routing at connect/attach time.

    Resolution runs once per client connection, never per message. Unknown,
    pending, closed, and expired sessions come back as explicit negative
    results so callers reject the client before any upstream dial. Session
    lifecycle writes stay with the application; the registry only reads
    (and caches — see TtlCachingSessionRegistry).
    """

    async def resolve(self, session_id: str) -> SessionResolution: ...

    async def invalidate(self, session_id: str) -> None:
        """Drops any cached resolution for the session; called when it closes."""
        ...


@runtime_checkable
class AuthPort(Protocol):
    async def authenticate(self, credentials: Mapping[str, str]) -> Principal | None:
        """Returns the caller's principal, or None to reject the connection.

        credentials is a normalized mapping the driving adapter builds from the
        request header, query string, and URL path — a WS client that cannot set
        headers (e.g. puppeteer) can still carry the key in the URL. The client
        credential is consumed here and never forwarded upstream.
        """
        ...

    def authorize(self, principal: Principal, resolution: SessionResolution) -> bool:
        """Whether `principal` may use the resolved session; runs before any dial.

        A non-owning organization (or an unknown session) must be rejected
        identically to a nonexistent one — no cross-org existence or lifecycle
        oracle.
        """
        ...


@runtime_checkable
class MetricsPort(Protocol):
    """Thin telemetry seam: the core emits through these three ops and never
    imports a telemetry SDK. Adapters (NoOp in OSS, OTel in cloud) map them onto
    counters/histograms/up-down instruments. Tags carry only safe dims (org_id,
    session_id, cdp method/domain, direction, reason) — never URLs or credentials.
    """

    def increment(self, name: str, amount: int = 1, tags: Mapping[str, str] | None = None) -> None:
        """Adds `amount` to a monotonic counter (frames, bytes, connects, rejects)."""
        ...

    def observe(self, name: str, value: float, tags: Mapping[str, str] | None = None) -> None:
        """Records `value` into a histogram (e.g. command round-trip latency, seconds)."""
        ...

    def gauge(self, name: str, amount: int, tags: Mapping[str, str] | None = None) -> None:
        """Shifts an up-down counter by `amount` (+1/-1 for active session count)."""
        ...


@runtime_checkable
class EventPolicyPort(Protocol):
    """Decides what a client sees of the upstream event stream.

    The default (ForwardAllEventPolicy) forwards everything unchanged; a policy is
    what a deployment opts into. Target lifecycle events are never droppable — the
    driving adapter learns session ownership from them downstream of this gate.
    """

    def decide(self, event: CdpEvent, session: ProxySession) -> PolicyDecision:
        """Forward the event as-is, drop it (with a reason for the decision trace),
        or replace it. Forward carries no payload so the caller delivers the frame it
        already holds — an unchanged event is never re-serialized on the way through.
        """
        ...

    def observe_command(self, command: CdpCommand, session: ProxySession) -> None:
        """Feed a client command in, so a policy can gate on the CDP domains the
        session actually enabled rather than assuming its interest."""
        ...

    def forget(self, session_id: str) -> None:
        """Release any state held for the session. Idempotent."""
        ...


__all__ = [
    "AuthPort",
    "EventPolicyPort",
    "LaunchEnvironmentError",
    "LaunchTimeoutError",
    "MetricsPort",
    "PolicyDecision",
    "ProtocolConfigurationError",
    "ResolvedSession",
    "SessionRegistryPort",
    "SessionResolution",
    "SessionResolutionStatus",
    "TransientConnectionError",
    "UpstreamBrowserPort",
    "UpstreamConnectError",
    "UpstreamConnection",
    "VendorAuthError",
    "VendorRateLimitError",
]
